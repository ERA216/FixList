"""Previous work shown on a ticket, using an isolated database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import config, db, service


class TicketRepairHistoryTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "history.db",
                                         LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        self.account = service.start_workspace("Мастерская", "Компрессор", "C-03")
        service.use_company(self.account["company"]["id"])
        self.addCleanup(db.set_company, 1)

    def add(self, at, kind="overheating", *, equipment=None, custom=None):
        return service.add_workspace_event(
            self.account["admin"]["id"], equipment or self.account["equipment_id"],
            "Компрессор остановился, простой 10 минут.", at, kind, 10, custom_event_type=custom,
        )

    def complete(self, ticket_id, at, outcome):
        with patch("backend.db.now_iso", return_value=at.isoformat()):
            service.resolve_ticket(ticket_id, "Мастер", outcome)

    def test_only_three_prior_matching_records_with_work_completed_before_current(self):
        current_time = datetime(2024, 3, 15, 12)
        self.add(current_time - timedelta(days=31))
        self.add(current_time - timedelta(days=30))
        first = self.add(current_time - timedelta(days=12))
        second = self.add(current_time - timedelta(days=7))
        recent = self.add(current_time - timedelta(days=4))
        self.complete(first, current_time - timedelta(days=3), "Очистили радиатор")
        self.complete(second, current_time - timedelta(days=5), "Заменили датчик")
        self.complete(recent, current_time + timedelta(days=1), "Работа завершилась после нового сигнала")
        self.add(current_time - timedelta(days=1), "electrical")
        another = service.add_workspace_equipment(self.account["admin"]["id"], "Насос", "P-01")
        self.add(current_time - timedelta(days=1), equipment=another)
        self.add(current_time)
        current = self.add(current_time)
        self.add(current_time + timedelta(days=1))

        history = service.ticket_repair_history(current)
        self.assertEqual([entry["id"] for entry in history["entries"]], [recent, second, first])
        self.assertEqual(history["occurrences_30d"], 5)
        self.assertEqual([entry["resolution"] for entry in history["entries"]],
                         [None, "Заменили датчик", "Очистили радиатор"])
        self.assertEqual(history["repeat_after_repair"], {
            "id": first, "resolved_at": "2024-03-12T12:00:00", "elapsed_seconds": 3 * 24 * 3600,
        })
        proposal = service.ticket_recommendation(current)
        self.assertIn(f"заявкой №{first}", proposal["proposed_solution"]["solution"])
        self.assertIn("12.03.2024", proposal["proposed_solution"]["solution"])
        self.assertNotIn(f"№{recent}", proposal["proposed_solution"]["solution"])

    def test_custom_type_is_normalized_and_an_approved_plan_is_not_a_repair(self):
        at = datetime(2024, 3, 1)
        previous = self.add(at, "other", custom="  Утечка   воздуха")
        plan = service.ticket_recommendation(previous)
        service.confirm_ticket_solution(previous, self.account["admin"]["id"], plan)
        self.add(at, "other", custom="Утечка масла")
        generic = self.add(at, "other")
        current = self.add(at + timedelta(days=1), "other", custom="утечка воздуха")
        current_generic = self.add(at + timedelta(days=1), "other")

        history = service.ticket_repair_history(current)
        self.assertEqual([entry["id"] for entry in history["entries"]], [previous])
        self.assertIsNone(history["entries"][0]["resolution"])
        self.assertIsNone(history["repeat_after_repair"])
        self.assertIn(f"записью №{previous}", service.ticket_recommendation(current)["proposed_solution"]["solution"])
        self.assertFalse(service.ticket_repair_history(current_generic)["entries"])
        self.assertIsNotNone(service.ticket(generic))

    def test_archive_matches_by_code_on_same_turbine_and_keeps_source_records_intact(self):
        demo = service.login("FIXLIST", "boss", "1111")
        service.use_company(demo["company_id"])
        all_rows = service.tickets(["archived"], limit=None)
        current = next(
            row for row in all_rows
            if sum(
                other["equipment_id"] == row["equipment_id"]
                and other["source_record"]["code"] == row["source_record"]["code"]
                and other["occurred_at"] < row["occurred_at"] for other in all_rows
            ) >= 3
        )
        history = service.ticket_repair_history(current["id"])
        self.assertEqual(len(history["entries"]), 3)
        for entry in history["entries"]:
            original = service.ticket(entry["id"])
            self.assertEqual(original["equipment_id"], current["equipment_id"])
            self.assertEqual(original["source_record"]["code"], current["source_record"]["code"])
            self.assertLess(entry["occurred_at"], current["occurred_at"])
            self.assertIsNone(entry["resolution"])
        self.assertIsNone(history["repeat_after_repair"])
        self.assertEqual(len(service.archive_report()["rows"]), 177)

        service.use_company(self.account["company"]["id"])
        with self.assertRaisesRegex(ValueError, "не найдена"):
            service.ticket_repair_history(current["id"])

    def test_legacy_offset_times_are_compared_on_one_timeline(self):
        at = datetime(2024, 3, 5, 12)
        previous = self.add(at - timedelta(days=2))
        later = self.add(at - timedelta(days=1))
        current = self.add(at)
        current_utc = at.astimezone(timezone.utc)
        previous_time = (current_utc - timedelta(minutes=60)).astimezone(timezone(timedelta(hours=12)))
        later_time = (current_utc + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-12)))
        repair_time = current_utc - timedelta(minutes=30)
        with db.connect() as conn:
            conn.execute("UPDATE incidents SET occurred_at=? WHERE id=?", (previous_time.isoformat(), previous))
            conn.execute("UPDATE incidents SET occurred_at=? WHERE id=?", (later_time.isoformat(), later))
        self.complete(previous, repair_time, "Проверили привод")

        history = service.ticket_repair_history(current)
        self.assertEqual([entry["id"] for entry in history["entries"]], [previous])
        self.assertEqual(history["repeat_after_repair"]["elapsed_seconds"], 30 * 60)

    def test_supported_early_local_dates_work_on_windows(self):
        previous = self.add(datetime(1900, 1, 1, 10))
        self.complete(previous, datetime(1900, 1, 1, 11), "Проверили охлаждение")
        current = self.add(datetime(1900, 1, 2, 11))
        history = service.ticket_repair_history(current)
        self.assertEqual([entry["id"] for entry in history["entries"]], [previous])
        self.assertEqual(history["entries"][0]["resolution"], "Проверили охлаждение")
        self.assertEqual(history["repeat_after_repair"]["elapsed_seconds"], 24 * 3600)

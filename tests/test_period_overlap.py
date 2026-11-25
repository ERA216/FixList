"""Verify how a downtime interval contributes to adjacent reporting periods."""
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend import briefing, config, db, finance, service


class NovemberFirst(date):
    @classmethod
    def today(cls):
        return cls(2024, 11, 1)


class PeriodOverlapTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "period.db", LLM_PROVIDER="mock", SMTP_PORT=0))
        service.bootstrap()
        self.addCleanup(db.set_company, 1)
        account = service.start_workspace("Цех", "Компрессор", "C-03", hour_cost_kzt=50_000)
        service.use_company(account["company"]["id"])
        self.admin = account["admin"]["id"]
        self.equipment = account["equipment_id"]
        self.event = service.add_workspace_event(
            self.admin, self.equipment, "Компрессор остановился", datetime(2024, 10, 31, 23, 30),
            "overheating", 120, 5_000,
        )

    def test_chat_splits_downtime_and_cost_on_a_month_boundary(self):
        october = service.ask_ai("Сколько часов простоя было у C-03 с 1 по 31 октября 2024?")
        november = service.ask_ai("Сколько часов простоя было у C-03 с 1 по 1 ноября 2024?")
        self.assertIn("0,5 ч простоя", october["answer"])
        self.assertIn("1,5 ч простоя", november["answer"])
        self.assertEqual([row["id"] for row in november["sources"]], [self.event])

        october_money = service.ask_ai("Какие потери у C-03 с 1 по 31 октября 2024?")["answer"]
        november_money = service.ask_ai("Какие потери у C-03 с 1 по 1 ноября 2024?")["answer"]
        self.assertIn("25 000 ₸", october_money)
        self.assertIn("30 000 ₸", october_money)
        self.assertIn("75 000 ₸", november_money)
        self.assertNotIn("80 000 ₸", november_money)

    def test_daily_brief_counts_carry_in_as_downtime_and_not_as_new_incident(self):
        with patch.object(briefing, "date", NovemberFirst):
            day = service.today_summary()
            self.assertEqual(day["incidents_today"], 0)
            self.assertEqual(day["downtime_h_today"], 1.5)
            self.assertEqual(day["loss_today_kzt"], 75_000)
            self.assertEqual(day["parts_today_kzt"], 0)
            self.assertIn("1,5 ч", service.daily_brief()["headline"])
        with patch.object(finance, "date", NovemberFirst), patch.object(config, "ANALYSIS_WINDOW_DAYS", 1):
            report = service.company_money_scenario()
            self.assertEqual((report["hours"], report["parts_kzt"], report["event_count"]), (1.5, 0, 1))

    def test_overlapping_csv_stop_merges_and_shift_log_stays_on_recorded_day(self):
        header = service.data_import_templates()[1].decode("utf-8-sig").splitlines()[0]
        csv = (header + "\nEV-QA;C-03;2024-11-01 00:30;overheating;Простой;90;2000\n").encode()
        service.import_company_data(self.admin, events_csv=csv)
        with patch.object(db, "now_iso", return_value="2024-10-31T23:50:00"):
            service.save_record("Запись смены о 60 мин простоя", [
                {"equipment_id": self.equipment, "failure_type": "overheating", "downtime_min": 60},
            ], mode="rules")
        october = (date(2024, 10, 31),) * 2
        november = (date(2024, 11, 1),) * 2
        all_rows = db.incidents()
        self.assertEqual(finance.for_events(all_rows, october)["hours"], 1.5)
        self.assertEqual(finance.for_events(all_rows, november)["hours"], 2)
        with patch.object(briefing, "date", NovemberFirst):
            day = service.today_summary()
            self.assertEqual(day["incidents_today"], 1)
            self.assertEqual(day["downtime_h_today"], 2)
            self.assertEqual(day["parts_today_kzt"], 2_000)
            self.assertEqual(day["loss_today_kzt"], 102_000)

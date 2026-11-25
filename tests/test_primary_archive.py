"""Primary-workspace migration and UI checks; all databases are isolated."""
from datetime import date
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from time import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import archive, config, db, open_data, service
from backend.schemas import kzt


class PrimaryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = patch.multiple(config, ROOT=root, DB_PATH=root / "test.db", LLM_PROVIDER="mock")
        self.settings.start()
        self.addCleanup(self.settings.stop)
        db.set_company(1)
        service.reset_demo()
        self.admin = service.login("FIXLIST", "boss", "1111")
        self.cid = self.admin["company_id"]
        self.addCleanup(db.set_company, 1)

    def install(self):
        archive.install(self.cid)
        service.use_company(self.cid)

    def test_source_events_replace_synthetic_incidents(self):
        self.assertTrue(any(row["is_demo"] for row in db.incidents()))
        self.install()
        rows = db.incidents()
        self.assertEqual(len(rows), 177)
        self.assertTrue(all(not row["is_demo"] and row["source_dataset"] == "kelmarsh" for row in rows))
        self.assertTrue(all(row["status"] == "archived" and row["resolution"] is None and row["assignee"] is None for row in rows))
        self.assertTrue(all(row["parts_cost_kzt"] is None and row["hour_cost_kzt"] is None and row["cause"] is None for row in rows))
        self.assertEqual({row["id"] for row in service.equipment_options()}, {f"KEL{i:02d}" for i in range(1, 7)})
        self.assertFalse(service.effects())
        self.assertEqual(service.ticket_counts()["archived"], 177)

    def test_original_timestamp_and_seconds_survive_database_import(self):
        self.install()
        row = next(row for row in db.incidents() if row["source_record"]["message"] == "Overload gear oil pump")
        self.assertEqual(row["occurred_at"], "2016-05-26T08:34:30")
        self.assertEqual(row["downtime_seconds"], 34060)
        self.assertAlmostEqual(row["downtime_min"], 34060 / 60)
        self.assertEqual(row["source_record"]["started_at"], "2016-05-26T08:34:30+00:00")
        self.assertIsNone(row["action"])

    def test_backup_exists_and_retains_previous_records(self):
        previous = len(db.incidents())
        self.install()
        backup = config.ROOT / ".runtime-backups" / "test.before-kelmarsh.db"
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], previous)

    def test_migration_is_idempotent_and_preserves_user_workflow(self):
        self.install()
        ticket = service.tickets(["archived"])[0]
        service.reopen_ticket(ticket["id"])
        service.approve_ticket(ticket["id"], "Пользователь FixList")
        self.assertFalse(archive.install(self.cid))
        service.bootstrap()
        updated = next(row for row in service.tickets(limit=999) if row["id"] == ticket["id"])
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(len(db.incidents()), 177)
        self.assertEqual(updated["approved_by"], "Пользователь FixList")

    def test_other_company_and_manual_records_survive(self):
        other = service.register_company("Другая компания")
        service.use_company(other["company"]["id"])
        result = service.analyze_text("Станок №1 перегрелся, простой 20 минут.")
        service.save_record(result["drafts"][0]["equipment_name"] + " перегрелся, простой 20 минут.", result["drafts"], result["mode"])
        before = db.incidents()
        archive.install(self.cid)
        self.assertEqual(db.incidents(), before)
        self.assertIsNone(service.archive_context())

    def test_new_worker_record_does_not_inherit_demo_prices_or_causes(self):
        self.install()
        result = service.analyze_text("Kelmarsh 1: заменили подшипник, простой 20 минут.")
        self.assertEqual(result["drafts"][0]["equipment_id"], "KEL01")
        self.assertIsNone(result["drafts"][0]["parts_cost_kzt"])
        self.assertEqual(result["drafts"][0]["hypotheses"], [])
        service.save_record("Kelmarsh 1: заменили подшипник, простой 20 минут.", result["drafts"], result["mode"])
        latest = db.incidents()[-1]
        self.assertIsNone(latest["parts_cost_kzt"])
        self.assertIsNone(latest["source_dataset"])

    def test_archive_daily_summary_clips_to_selected_day(self):
        self.install()
        view = service.archive_daily(date(2016, 5, 26))
        self.assertGreaterEqual(view["stats"]["downtime_seconds"], 34060)
        self.assertLessEqual(view["stats"]["downtime_seconds"], 6 * 86400)

    def test_board_solution_uses_the_archived_signal_without_a_repair_claim(self):
        from streamlit.testing.v1 import AppTest

        self.install()
        record = service.tickets(["archived"], limit=1)[0]
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login("FIXLIST", "boss", "1111")
        app.session_state["session-expires-at"] = time() + 3600
        app.run()
        app.button(key=f"archive-open-{record['id']}").click().run()
        self.assertFalse(app.exception)
        headings = [item.value for item in app.subheader]
        self.assertLess(headings.index("Что делали в прошлый раз"), headings.index("Решение ИИ"))
        self.assertTrue(any(f"Тот же код {record['source_record']['code']}" in item.value for item in app.caption))
        self.assertTrue(any("Результат работ на момент этого события не указан" in item.value for item in app.caption))
        self.assertTrue(any(item.value == "**Как проверить результат**" for item in app.markdown))
        self.assertTrue(any(f"Код {record['source_record']['code']}" in item.value for item in app.text))
        self.assertEqual(service.ticket(record["id"])["status"], "archived")
        self.assertIsNone(service.ticket(record["id"])["resolution"])
        next(button for button in app.button if button.label == "Решить с ИИ").click()
        app.button(key=f"archive-open-{record['id']}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="archive-board-filter").value, "В работе")
        self.assertEqual(service.ticket(record["id"])["status"], "approved")
        self.assertEqual(service.ticket(record["id"])["source_record"], record["source_record"])
        self.assertIsNone(service.ticket(record["id"])["resolution"])
        self.assertEqual(len(service.archive_report()["rows"]), 177)
        app.button(key=f"archive-open-{record['id']}").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "Подтверждённое решение" for item in app.subheader))

        pressure = next(row for row in db.incidents() if row["source_record"]["message"] == "Low hydraulic pressure")
        recommendation = service.ticket_recommendation(pressure["id"])
        self.assertIn("Если сервис подтвердит утечку", recommendation["solution"])
        self.assertIn("давление удерживается в допустимом диапазоне", recommendation["verify"])

    def test_archive_manual_approval_assigns_the_selected_worker(self):
        from streamlit.testing.v1 import AppTest

        self.install()
        record = service.tickets(["archived"], limit=1)[0]
        incident_id = record["id"]
        worker = service.login("FIXLIST", "master", "2222")
        service.reopen_ticket(incident_id, expected_status="archived")
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = self.admin
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["archive-board-filter"] = "Новые задачи"
        app.run()
        app.button(key=f"archive-open-{incident_id}").click().run()
        app.selectbox(key=f"ticket-assignee-{incident_id}").set_value(worker["id"])
        app.button(key="archive-approve-task").click()
        app.button(key=f"archive-open-{incident_id}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="archive-board-filter").value, "В работе")
        updated = service.ticket(incident_id)
        self.assertEqual(updated["assignee_user_id"], worker["id"])
        self.assertIsNone(updated["confirmed_solution"])
        self.assertEqual(updated["source_record"], record["source_record"])

    def test_admin_can_see_and_assign_an_active_task_started_without_a_worker(self):
        from streamlit.testing.v1 import AppTest

        self.install()
        record = service.tickets(["archived"], limit=1)[0]
        incident_id = record["id"]
        service.confirm_ticket_solution(incident_id, self.admin["id"], service.ticket_recommendation(incident_id))
        active = service.ticket(incident_id)
        worker = service.login("FIXLIST", "master", "2222")

        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = self.admin
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["archive-board-filter"] = "В работе"
        app.run()
        self.assertTrue(any("Исполнитель" in item.value and "Не назначен" in item.value for item in app.markdown))
        app.button(key=f"archive-open-{incident_id}").click().run()
        self.assertTrue(any(item.label == "Назначить исполнителя" for item in app.expander))
        app.selectbox(key=f"ticket-edit-assignee-{incident_id}-0").set_value(worker["id"])
        next(button for button in app.button if button.label == "Сохранить исполнителя").click()
        app.button(key=f"archive-open-{incident_id}").click().run()

        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "Исполнитель сохранён." for item in app.success))
        self.assertTrue(any(f"{worker['display_name']} · {worker['login']}" in item.value for item in app.markdown))
        assigned = service.ticket(incident_id)
        self.assertEqual(assigned["status"], "approved")
        self.assertEqual(assigned["assignee_user_id"], worker["id"])
        self.assertEqual(assigned["confirmed_solution"], active["confirmed_solution"])
        self.assertEqual(assigned["taken_at"], active["taken_at"])

    def test_help_requests_are_available_under_in_progress(self):
        from streamlit.testing.v1 import AppTest

        self.install()
        incident_id = service.tickets(["archived"], limit=1)[0]["id"]
        worker = service.login("FIXLIST", "master", "2222")
        service.reopen_ticket(incident_id, expected_status="archived", actor_user_id=self.admin["id"])
        service.approve_ticket(incident_id, self.admin["display_name"], expected_status="new",
                               assignee_user_id=worker["id"], actor_user_id=self.admin["id"])
        service.escalate_ticket(incident_id, worker["display_name"], "Ждём запчасть",
                                expected_status="approved", actor_user_id=worker["id"])

        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = self.admin
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["archive-board-filter"] = "Нужна помощь"
        app.run()

        self.assertFalse(app.exception)
        filters = app.radio(key="archive-board-filter")
        self.assertEqual(filters.options, ["Архив событий", "Новые задачи", "В работе", "Проверены"])
        self.assertEqual(filters.value, "В работе")
        app.button(key=f"archive-open-{incident_id}").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Ждём запчасть" in item.value for item in app.warning))
        app.button(key=f"ticket-resume-{incident_id}").click()
        app.button(key=f"archive-open-{incident_id}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(service.ticket(incident_id)["status"], "approved")
        self.assertEqual(service.ticket(incident_id)["assignee_user_id"], worker["id"])

    def test_chat_reports_unknown_cost_and_future_failure_honestly(self):
        self.install()
        answer = service.ask_ai("Какой убыток у Kelmarsh 1?")
        self.assertIn("нет тарифов", answer["answer"])
        answer = service.ask_ai("Когда сломается Kelmarsh 1?")
        self.assertIn("нет", answer["answer"])
        answer = service.ask_ai("Что происходило на Kelmarsh 1?")
        self.assertIn("32 запис", answer["answer"])
        self.assertEqual(answer["mode"], "rules")

    def test_main_sections_use_real_data_without_extra_navigation(self):
        from streamlit.testing.v1 import AppTest
        self.install()
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login("FIXLIST", "boss", "1111")
        app.session_state["session-expires-at"] = time() + 3600
        app.run()
        app.button(key="nav_Состояние оборудования").click().run()
        self.assertEqual(app.session_state["page"], "Состояние оборудования")
        self.assertFalse(app.exception)
        for page in ["Панель задач", "Спросить ИИ", "Состояние оборудования", "Сводка дня", "Аномалии", "План экономии", "История и эффект", "Сотрудники"]:
            app.session_state["page"] = page
            app.run()
            self.assertFalse(app.exception, f"{page}: {app.exception}")
            self.assertFalse(any(button.label == "09   Реальные данные" for button in app.button))
        app.session_state["page"] = "План экономии"
        app.run()
        hours = service.archive_report()["stats"]["downtime_seconds"] / 3600
        self.assertTrue(any(f"≈ {kzt(hours * 50_000)} ₸" in item.value for item in app.markdown))
        app.number_input(key="archive-hour-rate").set_value(10000).run()
        self.assertFalse(app.exception)
        app.number_input(key="archive-reduction").set_value(10).run()
        app.number_input(key="archive-investment").set_value(50000).run()
        self.assertFalse(app.exception)
        self.assertTrue(any(f"≈ {kzt(hours * 1000)} ₸" in item.value for item in app.markdown))
        self.assertTrue(any("Расчётная окупаемость" in item.value for item in app.markdown))
        app.button(key="nav_Спросить ИИ").click().run()
        app.chat_input[0].set_value("насколько убытков я ушел").run()
        self.assertTrue(any(f"≈ {kzt(hours * 1000)} ₸" in item.value for item in app.markdown))
        app.button(key="chat-money").click().run()
        self.assertEqual(app.session_state["page"], "План экономии")
        self.assertEqual(app.number_input(key="archive-hour-rate").value, 10000)
        self.assertEqual(app.number_input(key="archive-reduction").value, 10)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()

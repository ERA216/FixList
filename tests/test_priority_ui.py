"""Regression checks for the task queue and review flows."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from time import time
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from backend import config, db, service


class PriorityUiTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=folder, DB_PATH=folder / "priority.db",
                                         LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        self.account = service.start_workspace("Мастерская", "Насос", "P-01")
        self.admin = self.account["admin"]
        self.company = self.account["company"]
        service.use_company(self.company["id"])
        self.worker = service.add_workers(self.company["id"], "Механик")[0]
        self.addCleanup(db.set_company, 1)

    def app(self, user, page):
        login = service.login(self.company["code"], user["login"], user["pin"])
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = login
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["page"] = page
        return app

    def test_assigned_task_is_first_and_the_full_queue_can_be_paged(self):
        start = datetime.now().replace(microsecond=0) - timedelta(days=2)
        company_id, equipment = self.company["id"], self.account["equipment_id"]
        with db.connect() as conn:
            for index in range(101):
                at = (start + timedelta(minutes=index)).isoformat()
                text = f"Задача из очереди номер {index}" if index else "Старая личная задача"
                log_id = db.add_shift_log(text, "processed", "manual", created_at=at, conn=conn)
                ticket_id = db.add_incident(log_id, {"equipment_id": equipment, "failure_type": "other", "downtime_min": 0},
                                            occurred_at=at, conn=conn, company_id=company_id)
                conn.execute("UPDATE incidents SET status='approved', assignee_user_id=? WHERE id=?",
                             (self.worker["id"] if index == 0 else None, ticket_id))

        app = self.app(self.worker, "Мои задачи").run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "Задачи к решению · 101" for item in app.subheader))
        cards = [item.value for item in app.markdown if 'class="tcard' in item.value]
        self.assertEqual(len(cards), 12)
        self.assertIn("Старая личная задача", cards[0])
        app.number_input(key="worker-task-page").set_value(9).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Задача из очереди номер 1</div>" in item.value for item in app.markdown))

    def test_escalated_task_can_return_with_the_same_plan_and_worker(self):
        incident = service.add_workspace_event(self.admin["id"], self.account["equipment_id"],
                                                "Насос перегрелся.", datetime.now(), "overheating")
        proposal = service.ticket_recommendation(incident)
        service.confirm_ticket_solution(incident, self.admin["id"], proposal, assignee_user_id=self.worker["id"])
        service.escalate_ticket(incident, self.worker["display_name"], "Ждём запчасть", actor_user_id=self.worker["id"])
        before = service.ticket(incident)

        app = self.app(self.admin, "Панель задач").run()
        app.button(key=f"archive-open-{incident}").click().run()
        app.button(key=f"ticket-resume-{incident}").click()
        app.button(key=f"archive-open-{incident}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="company-board-status").value, "approved")
        after = service.ticket(incident)
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["confirmed_solution"], before["confirmed_solution"])
        self.assertEqual(after["assignee_user_id"], self.worker["id"])

    def test_parts_cost_remains_unknown_until_reporter_enters_a_value(self):
        app = self.app(self.worker, "Новая запись").run()
        for count, amount in ((1, None), (2, 12345)):
            app.text_area(key="raw").set_value(f"P-01 стоял 30 минут. Заменили ремень. Запись {count}.")
            next(button for button in app.button if button.label.startswith("Разобрать запись")).click().run()
            self.assertFalse(app.exception)
            cost = app.number_input(key=f"report-parts-cost-{count}_0")
            self.assertIsNone(cost.value)
            if amount is not None:
                cost.set_value(amount)
            next(button for button in app.button if button.label == "Подтвердить и сохранить").click().run()
            self.assertFalse(app.exception)
            row = next(item for item in db.incidents() if f"Запись {count}." in item["raw_text"])
            self.assertEqual(row["parts_cost_kzt"], amount)

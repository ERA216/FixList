"""Ticket assignments, account isolation and upgrade of existing work."""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import config, db, service


class TicketAssignmentTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "assignment.db",
                                         LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        self.workspace = service.start_workspace("Мастерская", "Компрессор", "C-03")
        self.company_id = self.workspace["company"]["id"]
        self.admin = self.workspace["admin"]
        self.worker, self.peer = service.add_workers(self.company_id, "Алексей\nАлексей")
        service.use_company(self.company_id)
        self.addCleanup(db.set_company, 1)

    def add_ticket(self):
        return service.add_workspace_event(
            self.admin["id"], self.workspace["equipment_id"],
            "Компрессор остановился из-за перегрева, простой 10 минут.",
            datetime.now(), "overheating", 10,
        )

    def approve(self, incident_id, worker, *, actor=None):
        service.approve_ticket(incident_id, "Мастер", expected_status="new",
                               assignee_user_id=worker["id"], actor_user_id=(actor or self.admin)["id"])

    def test_manual_assignment_is_visible_only_to_the_selected_worker(self):
        assigned = self.add_ticket()
        legacy = self.add_ticket()
        self.approve(assigned, self.worker)
        service.approve_ticket(legacy, "Мастер")

        ticket = service.ticket(assigned)
        self.assertEqual(ticket["status"], "approved")
        self.assertEqual(ticket["assignee_user_id"], self.worker["id"])
        self.assertEqual(ticket["assignee_name"], self.worker["display_name"])
        self.assertEqual(ticket["assignee_login"], self.worker["login"])
        self.assertEqual(ticket["approved_by"], self.admin["display_name"])
        self.assertIsNone(ticket["assignee"])
        self.assertCountEqual([row["id"] for row in service.tickets(["approved"], worker_user_id=self.worker["id"])],
                              [assigned, legacy])
        self.assertEqual([row["id"] for row in service.tickets(["approved"], worker_user_id=self.peer["id"])], [legacy])

        with self.assertRaisesRegex(ValueError, "другому сотруднику"):
            service.resolve_ticket(assigned, "Алексей", "Проверили", actor_user_id=self.peer["id"])
        with self.assertRaisesRegex(ValueError, "другому сотруднику"):
            service.escalate_ticket(assigned, "Алексей", "Нужен датчик", actor_user_id=self.peer["id"])
        with self.assertRaisesRegex(ValueError, "Войдите"):
            service.resolve_ticket(assigned, "Алексей", "Проверили")
        service.resolve_ticket(assigned, "Алексей", "Проверили", actor_user_id=self.worker["id"])
        self.assertEqual(service.ticket(assigned)["assignee_user_id"], self.worker["id"])
        self.assertEqual(service.ticket(assigned)["status"], "resolved")
        service.reopen_ticket(assigned, actor_user_id=self.admin["id"])
        self.assertIsNone(service.ticket(assigned)["assignee_user_id"])

    def test_manual_assignment_requires_an_admin_and_a_worker_in_the_same_company(self):
        other = service.start_workspace("Другой цех", "Насос", "P-01")
        outsider = service.add_workers(other["company"]["id"], "Оператор")[0]
        service.use_company(self.company_id)
        incident = self.add_ticket()
        with self.assertRaisesRegex(ValueError, "только администратору"):
            self.approve(incident, self.worker, actor=self.peer)
        for ineligible in (self.admin, outsider):
            with self.assertRaisesRegex(ValueError, "сотрудника этого кабинета"):
                self.approve(incident, ineligible)
        self.assertEqual(service.ticket(incident)["status"], "new")
        self.assertIsNone(service.ticket(incident)["assignee_user_id"])
        self.assertEqual(service.tickets(worker_user_id=outsider["id"]), [])

    def test_ai_plan_and_assignment_are_saved_together_and_escalation_keeps_worker(self):
        incident = self.add_ticket()
        plan = service.ticket_recommendation(incident)
        with self.assertRaisesRegex(ValueError, "сотрудника этого кабинета"):
            service.confirm_ticket_solution(incident, self.admin["id"], plan, assignee_user_id=self.admin["id"])
        ticket = service.ticket(incident)
        self.assertEqual(ticket["status"], "new")
        self.assertIsNone(ticket["confirmed_solution"])
        self.assertIsNone(ticket["assignee_user_id"])

        service.confirm_ticket_solution(incident, self.admin["id"], plan, assignee_user_id=self.worker["id"])
        with self.assertRaisesRegex(ValueError, "Откройте карточку заново"):
            service.confirm_ticket_solution(incident, self.admin["id"], plan, assignee_user_id=self.peer["id"])
        ticket = service.ticket(incident)
        self.assertEqual(ticket["status"], "approved")
        self.assertEqual(ticket["confirmed_solution"]["solution"], plan["proposed_solution"]["solution"])
        self.assertEqual(ticket["assignee_user_id"], self.worker["id"])

        service.escalate_ticket(incident, "Алексей", "Нужен датчик", actor_user_id=self.worker["id"])
        self.assertEqual(service.ticket(incident)["status"], "escalated")
        self.assertEqual(service.ticket(incident)["assignee_user_id"], self.worker["id"])
        service.resolve_ticket(incident, self.admin["display_name"], "Заменили датчик", actor_user_id=self.admin["id"])
        ticket = service.ticket(incident)
        self.assertEqual(ticket["assignee_user_id"], self.worker["id"])
        self.assertEqual(ticket["assignee"], self.admin["display_name"])

    def test_admin_resumes_escalated_ai_work_without_replacing_plan_or_worker(self):
        incident = self.add_ticket()
        plan = service.ticket_recommendation(incident)
        service.confirm_ticket_solution(incident, self.admin["id"], plan, assignee_user_id=self.worker["id"])
        before = service.ticket(incident)

        service.escalate_ticket(incident, "Алексей", "Нужна запчасть", actor_user_id=self.worker["id"])
        escalated = service.ticket(incident)
        self.assertEqual(escalated["escalation_note"], "Нужна запчасть")
        self.assertEqual(service.tickets(["approved"], worker_user_id=self.worker["id"]), [])

        service.resume_ticket(incident, self.admin["id"], expected_status="escalated")
        after = service.ticket(incident)
        self.assertEqual(after["status"], "approved")
        for field in ("confirmed_solution", "assignee_user_id", "taken_at", "approved_by"):
            self.assertEqual(after[field], before[field])
        self.assertIsNone(after["escalation_note"])
        self.assertIsNone(after["assignee"])
        self.assertEqual([task["id"] for task in service.tickets(["approved"], worker_user_id=self.worker["id"])],
                         [incident])
        self.assertEqual(service.tickets(["approved"], worker_user_id=self.peer["id"]), [])
        with self.assertRaisesRegex(ValueError, "Откройте карточку заново"):
            service.resume_ticket(incident, self.admin["id"])

    def test_resume_rejects_wrong_role_company_and_stale_status(self):
        incident = self.add_ticket()
        self.approve(incident, self.worker)
        service.escalate_ticket(incident, "Алексей", "Нужен датчик", actor_user_id=self.worker["id"])

        with self.assertRaisesRegex(ValueError, "администратор этого кабинета"):
            service.resume_ticket(incident, self.worker["id"])
        with self.assertRaisesRegex(ValueError, "Откройте карточку заново"):
            service.resume_ticket(incident, self.admin["id"], expected_status="new")

        other = service.start_workspace("Другой кабинет", "Насос", "P-01")
        service.use_company(other["company"]["id"])
        with self.assertRaisesRegex(ValueError, "Откройте карточку заново"):
            service.resume_ticket(incident, other["admin"]["id"])

        service.use_company(self.company_id)
        self.assertEqual(service.ticket(incident)["status"], "escalated")
        self.assertEqual(service.ticket(incident)["escalation_note"], "Нужен датчик")

    def test_migration_leaves_legacy_tasks_available_to_workers(self):
        legacy = self.add_ticket()
        service.approve_ticket(legacy, "Мастер")
        with db.connect() as conn:
            conn.execute("ALTER TABLE incidents DROP COLUMN assignee_user_id")
        db.init_db()

        self.assertEqual(service.ticket(legacy)["approved_by"], "Мастер")
        self.assertIsNone(service.ticket(legacy)["assignee_user_id"])
        self.assertEqual([row["id"] for row in service.tickets(["approved"], worker_user_id=self.worker["id"])], [legacy])

    def test_admin_can_assign_existing_work_and_change_executor_without_restarting(self):
        incident = self.add_ticket()
        plan = service.ticket_recommendation(incident)
        service.confirm_ticket_solution(incident, self.admin["id"], plan)
        before = service.ticket(incident)
        self.assertIsNone(before["assignee_user_id"])

        service.assign_ticket(incident, self.admin["id"], self.worker["id"], expected_assignee_user_id=None)
        self.assertEqual(service.ticket(incident)["assignee_user_id"], self.worker["id"])
        with self.assertRaisesRegex(ValueError, "Откройте карточку заново"):
            service.assign_ticket(incident, self.admin["id"], self.peer["id"], expected_assignee_user_id=None)

        service.assign_ticket(incident, self.admin["id"], self.peer["id"], expected_assignee_user_id=self.worker["id"])
        after = service.ticket(incident)
        self.assertEqual(after["assignee_user_id"], self.peer["id"])
        self.assertEqual(after["assignee_login"], self.peer["login"])
        for field in ("status", "taken_at", "approved_by", "confirmed_solution", "resolution"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(service.tickets(["approved"], worker_user_id=self.worker["id"]), [])
        self.assertEqual([row["id"] for row in service.tickets(["approved"], worker_user_id=self.peer["id"])], [incident])

    def test_changing_assignment_requires_own_company_admin_worker_and_active_ticket(self):
        other = service.start_workspace("Другой цех", "Насос", "P-01")
        outsider = service.add_workers(other["company"]["id"], "Оператор")[0]
        service.use_company(self.company_id)
        incident = self.add_ticket()
        with self.assertRaisesRegex(ValueError, "недоступна для назначения"):
            service.assign_ticket(incident, self.admin["id"], self.worker["id"], expected_assignee_user_id=None)
        service.approve_ticket(incident, "Мастер")

        for actor in (self.peer, other["admin"]):
            with self.assertRaisesRegex(ValueError, "администратор этого кабинета"):
                service.assign_ticket(incident, actor["id"], self.worker["id"], expected_assignee_user_id=None)
        with self.assertRaisesRegex(ValueError, "сотрудника этого кабинета"):
            service.assign_ticket(incident, self.admin["id"], outsider["id"], expected_assignee_user_id=None)
        self.assertIsNone(service.ticket(incident)["assignee_user_id"])

        service.resolve_ticket(incident, "Мастер", "Проверили", actor_user_id=self.admin["id"])
        with self.assertRaisesRegex(ValueError, "недоступна для назначения"):
            service.assign_ticket(incident, self.admin["id"], self.worker["id"], expected_assignee_user_id=None)
        self.assertEqual(service.ticket(incident)["status"], "resolved")

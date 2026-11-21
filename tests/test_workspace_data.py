"""Self-service data entry and workspace isolation with a temporary database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
from threading import Barrier
from time import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import analytics, config, db, extract, service
from backend.schemas import ExtractionResult
from backend.ticket_plan import CauseSelection


class WorkspaceDataTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "test.db", LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        self.addCleanup(db.set_company, 1)

    def test_first_save_creates_workspace_without_a_lead(self):
        with self.assertRaisesRegex(ValueError, "оборудования"):
            service.start_workspace("Новый кабинет", " ")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 1)

        account = service.start_workspace("Новый кабинет", "Насос", "P1", "Цех", 30_000)
        company, admin = account["company"], account["admin"]
        service.use_company(company["id"])
        self.assertEqual(service.login(company["code"], admin["login"], admin["pin"])["id"], admin["id"])
        self.assertEqual([item["name"] for item in service.equipment_options()], ["Насос"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)

    def test_events_and_equipment_stay_in_their_workspace(self):
        first = service.start_workspace("Первая компания", "Насос", "P1")
        second = service.start_workspace("Вторая компания", "Компрессор", "P1")
        service.use_company(second["company"]["id"])
        with self.assertRaisesRegex(ValueError, "своего справочника"):
            service.add_workspace_event(second["admin"]["id"], first["equipment_id"], "Выбило автомат", datetime.now(), None)
        with self.assertRaisesRegex(ValueError, "администратору"):
            service.add_workspace_equipment(first["admin"]["id"], "Фильтр")
        self.assertFalse(service.history())

        service.use_company(first["company"]["id"])
        with self.assertRaisesRegex(ValueError, "уже используется"):
            service.add_workspace_equipment(first["admin"]["id"], "Фильтр", "p1")
        event = service.add_workspace_event(first["admin"]["id"], first["equipment_id"], "Выбило автомат", datetime.now(), None, 25)
        self.assertEqual(service.tickets(["new"])[0]["id"], event)
        self.assertEqual(service.history()[0]["Простой, мин"], 25)

        demo = service.login("FIXLIST", "boss", "1111")
        service.use_company(demo["company_id"])
        with self.assertRaisesRegex(ValueError, "собственного кабинета"):
            service.add_workspace_equipment(demo["id"], "Фильтр")
        self.assertEqual(len(service.archive_report()["rows"]), 177)
        self.assertEqual(len(service.equipment_options()), 6)

    def test_ticket_plan_uses_only_matching_evidence_and_keeps_the_ticket_open(self):
        account = service.start_workspace("Мастерская", "Компрессор", "C-01")
        company, admin = account["company"], account["admin"]
        service.use_company(company["id"])
        equipment = account["equipment_id"]
        incident = service.add_workspace_event(admin["id"], equipment, "Перегрев. На радиаторе много пыли.",
                                               datetime.now(), "overheating")
        with patch("backend.config.ai_enabled", return_value=True), patch("backend.llm.call_json") as model:
            model.return_value = CauseSelection.model_validate({
                "cause_ids": ["C07", "C06"], "answer": "Радиатор уже заменили.",
            })
            plan = service.ticket_recommendation(incident)
            self.assertEqual(plan["mode"], "ai")
            self.assertEqual([item["id"] for item in plan["hypotheses"]], ["C06"])
            self.assertEqual(plan["hypotheses"][0]["signs"], ["пыли"])
            self.assertIn("очистку радиатора", plan["hypotheses"][0]["solution"])
            self.assertIn("сигнал перегрева не повторяется", plan["hypotheses"][0]["verify"])
            self.assertNotIn("уже заменили", str(plan))
            model.return_value = CauseSelection(cause_ids=["C99"])
            fallback = service.ticket_recommendation(incident)
            self.assertEqual(fallback["mode"], "rules")
            self.assertEqual(fallback["hypotheses"][0]["solution"], plan["hypotheses"][0]["solution"])
        rejected = service.add_workspace_event(admin["id"], equipment, "Перегрев, пыли на радиаторе нет.",
                                               datetime.now(), "overheating")
        recommendation = service.ticket_recommendation(rejected)
        self.assertEqual(recommendation["hypotheses"], [])
        self.assertIn("Поручите специалисту", recommendation["solution"])
        self.assertNotIn("очистку радиатора", recommendation["solution"])
        other = service.add_workspace_equipment(admin["id"], "Насос", "P-01")
        text = "C-01 перегрелся, простой 10 минут. P-01 перегрелся, на радиаторе много пыли, простой 15 минут."
        ids = service.save_record(text, [
            {"equipment_id": equipment, "failure_type": "overheating", "downtime_min": 10},
            {"equipment_id": other, "failure_type": "overheating", "downtime_min": 15},
        ])["incident_ids"]
        self.assertFalse(service.ticket_recommendation(ids[0])["hypotheses"])
        self.assertEqual(service.ticket_recommendation(ids[1])["hypotheses"][0]["id"], "C06")
        self.assertEqual(service.ticket(incident)["status"], "new")
        self.assertIsNone(service.ticket(incident)["resolution"])

    def test_task_board_confirms_the_plan_and_records_the_actual_work_separately(self):
        from streamlit.testing.v1 import AppTest

        account = service.start_workspace("Мастерская", "Компрессор", "C-01")
        company, admin = account["company"], account["admin"]
        service.use_company(company["id"])
        incident = service.add_workspace_event(admin["id"], account["equipment_id"], "Перегрев. На радиаторе много пыли.",
                                                datetime.now(), "overheating")
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login(company["code"], admin["login"], admin["pin"])
        app.session_state["session-expires-at"] = time() + 3600
        app.run()
        app.button(key=f"archive-open-{incident}").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(any(item.label == "Предложить решение с ИИ" for item in app.button))
        self.assertTrue(any("Загрязнение радиатора и вентиляции" in item.value for item in app.markdown))
        self.assertTrue(any("очистку радиатора" in item.value for item in app.markdown))
        self.assertTrue(any("сигнал перегрева не повторяется" in item.value for item in app.markdown))
        self.assertTrue(any(item.label == "Что сделали" for item in app.text_input))
        self.assertFalse(any(item.key == f"ai_fix_{incident}" for item in app.button))
        service.use_company(company["id"])
        self.assertEqual(service.ticket(incident)["status"], "new")
        self.assertIsNone(service.ticket(incident)["resolution"])
        self.assertIsNone(service.ticket(incident)["confirmed_solution"])

        # AppTest runs the full page, so also trigger the dialog for its widget events.
        next(button for button in app.button if button.label == "Решить с ИИ").click()
        app.button(key=f"archive-open-{incident}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="company-board-status").value, "approved")
        service.use_company(company["id"])
        ticket = service.ticket(incident)
        self.assertEqual(ticket["status"], "approved")
        self.assertIsNone(ticket["resolution"])
        self.assertIn("очистку радиатора", ticket["confirmed_solution"]["solution"])

        worker = service.add_workers(company["id"], "Сотрудник")[0]
        worker_app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        worker_app.session_state["user"] = service.login(company["code"], worker["login"], worker["pin"])
        worker_app.session_state["session-expires-at"] = time() + 3600
        worker_app.session_state["page"] = "Мои задачи"
        worker_app.run()
        self.assertFalse(worker_app.exception)
        self.assertTrue(any(item.value == "Подтверждённое решение" for item in worker_app.subheader))
        self.assertTrue(any("очистку радиатора" in item.value for item in worker_app.markdown))

        app.button(key=f"archive-open-{incident}").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "Подтверждённое решение" for item in app.subheader))
        self.assertFalse(any(item.label == "Решить с ИИ" for item in app.button))
        app.button(key=f"close_board_{incident}").click()
        app.button(key=f"archive-open-{incident}").click().run()
        self.assertTrue(any("Опишите, что сделали" in item.value for item in app.error))
        app.text_input(key=f"done_board_{incident}").set_value("Очистили радиатор, температура в норме.")
        app.button(key=f"close_board_{incident}").click()
        app.button(key=f"archive-open-{incident}").click().run()
        self.assertFalse(app.exception)
        service.use_company(company["id"])
        resolved = service.ticket(incident)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution"], "Очистили радиатор, температура в норме.")
        self.assertEqual(resolved["confirmed_solution"], ticket["confirmed_solution"])

    def test_only_one_admin_can_confirm_a_current_proposal(self):
        account = service.start_workspace("Мастерская", "Компрессор", "C-01")
        company, admin = account["company"], account["admin"]
        service.use_company(company["id"])
        incident = service.add_workspace_event(admin["id"], account["equipment_id"], "Перегрев. На радиаторе много пыли.",
                                                datetime.now(), "overheating")
        proposal = service.ticket_recommendation(incident)
        worker = service.add_workers(company["id"], "Сотрудник")[0]
        with self.assertRaisesRegex(ValueError, "администратор"):
            service.confirm_ticket_solution(incident, worker["id"], proposal)
        with self.assertRaisesRegex(ValueError, "для другой задачи"):
            service.confirm_ticket_solution(incident + 1, admin["id"], proposal)
        service.approve_ticket(incident, admin["display_name"])
        with self.assertRaisesRegex(ValueError, "уже изменилась"):
            service.confirm_ticket_solution(incident, admin["id"], proposal)
        current = service.ticket_recommendation(incident)
        simultaneous = Barrier(2, timeout=5)

        def confirm(_):
            service.use_company(company["id"])
            simultaneous.wait()
            try:
                service.confirm_ticket_solution(incident, admin["id"], current)
            except ValueError:
                return False
            return True

        with ThreadPoolExecutor(2) as executor:
            self.assertCountEqual(executor.map(confirm, range(2)), [False, True])
        self.assertEqual(service.ticket(incident)["confirmed_solution"]["solution"], current["proposed_solution"]["solution"])

    def test_event_form_can_create_and_reuse_equipment_by_name(self):
        account = service.start_workspace("Мастерская", "Насос")
        service.use_company(account["company"]["id"])
        admin = account["admin"]["id"]
        with self.assertRaisesRegex(ValueError, "Опишите"):
            service.add_workspace_event(admin, None, "", datetime.now(), None, equipment_name="Компрессор")
        self.assertEqual([item["name"] for item in service.equipment_options()], ["Насос"])

        first = service.add_workspace_event(admin, None, "Отключился датчик", datetime.now(), None, 25,
                                            equipment_name=" Компрессор   цеха ")
        second = service.add_workspace_event(admin, None, "Компрессор перегрелся", datetime.now(), None,
                                             equipment_name="КОМПРЕССОР цеха")
        first_ticket, second_ticket = service.ticket(first), service.ticket(second)
        self.assertEqual(first_ticket["equipment_id"], second_ticket["equipment_id"])
        self.assertEqual(first_ticket["equipment_name"], "Компрессор цеха")
        self.assertEqual(first_ticket["downtime_min"], 25)
        self.assertEqual(len(service.equipment_options()), 2)

    def test_landing_to_first_equipment_and_task(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25).run()
        next(field for field in app.text_input if field.label == "Название кабинета").set_value("Мастерская")
        next(field for field in app.text_input if field.label == "Название оборудования").set_value("Компрессор")
        next(button for button in app.button if button.label == "Сохранить и открыть кабинет").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["page"], "Панель задач")
        self.assertEqual(app.session_state["user"]["company_name"], "Мастерская")
        self.assertFalse(any(button.key == "nav_Свои данные" for button in app.button))
        equipment = next(field for field in app.selectbox if field.label == "Оборудование")
        self.assertIsNone(equipment.value)
        service.use_company(app.session_state["user"]["company_id"])
        equipment.set_value(service.equipment_options()[0]["id"])
        next(field for field in app.text_area if field.label == "Что произошло?").set_value("Компрессор перегрелся")
        next(button for button in app.button if button.label == "Сохранить событие").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(next(field for field in app.selectbox if field.label == "Оборудование").value)
        self.assertTrue(any("добавлено в задачи и историю" in msg.value for msg in app.success))
        next(button for button in app.button if button.label == "Открыть задачу →").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["page"], "Панель задач")
        self.assertTrue(any('archive-detail-header' in row.value and "Компрессор" in row.value and "Перегрев" in row.value for row in app.markdown))
        self.assertTrue(any(button.key == "approve" for button in app.button))

    def test_custom_event_type_appears_in_form_card_and_history(self):
        from streamlit.testing.v1 import AppTest

        account = service.start_workspace("Мастерская", "Компрессор", "C-01")
        company, admin = account["company"], account["admin"]
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login(company["code"], admin["login"], admin["pin"])
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["show-workspace-forms"] = True
        app.run()
        self.assertFalse(any(field.label == "Название события" for field in app.text_input))
        next(field for field in app.selectbox if field.label == "Оборудование").set_value(account["equipment_id"])
        next(field for field in app.text_area if field.label == "Что произошло?").set_value("На линии упало давление")
        next(field for field in app.selectbox if field.label == "Тип события").set_value("other").run()
        title = next(field for field in app.text_input if field.label == "Название события")
        self.assertEqual(next(field for field in app.text_area if field.label == "Что произошло?").value, "На линии упало давление")
        title.set_value("  Утечка сжатого воздуха  ")
        next(button for button in app.button if button.label == "Сохранить событие").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("archive-ticket-description" in item.value and "Утечка сжатого воздуха" in item.value for item in app.markdown))
        next(button for button in app.button if button.label == "Открыть задачу →").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("archive-detail-header" in item.value and "Утечка сжатого воздуха" in item.value for item in app.markdown))
        service.use_company(company["id"])
        ticket = service.tickets()[0]
        self.assertEqual(ticket["failure_type"], "other")
        self.assertEqual(ticket["raw_text"], "На линии упало давление")
        self.assertEqual(service.history("Утечка сжатого воздуха")[0]["Тип"], "Утечка сжатого воздуха")

    def test_board_hides_warnings_while_entering_data(self):
        from streamlit.testing.v1 import AppTest

        account = service.start_workspace("Моя компания", "Насос цеха", "P1")
        service.use_company(account["company"]["id"])
        for _ in range(4):
            service.add_workspace_event(account["admin"]["id"], account["equipment_id"], "Выбило автомат", datetime.now(), None, 25)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login(account["company"]["code"], account["admin"]["login"], account["admin"]["pin"])
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["show-workspace-forms"] = True
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(any('class="warn-tag"' in row.value for row in app.markdown))

        next(button for button in app.button if button.label == "Скрыть формы").click().run()
        warning = next(row.value for row in app.markdown if 'class="warn-tag"' in row.value)
        self.assertIn("Насос цеха", warning)
        self.assertNotIn("Kelmarsh", warning)

    def test_against_similar_names_and_long_downtime(self):
        account = service.start_workspace("Мастерская", "Насос", "P-01")
        service.use_company(account["company"]["id"])
        admin = account["admin"]["id"]
        feed = service.add_workspace_equipment(admin, "Насос подачи", "P-02")
        draft = service.analyze_text("Насос подачи перегрелся, простой 48 часов.")["drafts"][0]
        self.assertEqual(draft["equipment_id"], feed)
        self.assertEqual(draft["downtime_min"], 2880)
        result = service.save_record("Насос подачи перегрелся, простой 48 часов.", [draft])
        self.assertEqual(service.ticket(result["incident_ids"][0])["downtime_min"], 2880)

        service.add_workspace_equipment(admin, "Насос подачи", "P-03")
        ambiguous = service.analyze_text("Насос подачи перегрелся, простой 30 минут.")["drafts"][0]
        self.assertIsNone(ambiguous["equipment_id"])
        identified = service.analyze_text("Насос подачи P-02 перегрелся, простой 30 минут.")["drafts"][0]
        self.assertEqual(identified["equipment_id"], feed)
        self.assertEqual(identified["downtime_min"], 30)
        too_long = service.analyze_text("P-02 перегрелся, простой 999999 часов.")["drafts"][0]
        self.assertIn("downtime", too_long["issues"])

    def test_model_cannot_swap_downtime_between_equipment(self):
        account = service.start_workspace("Мастерская", "Компрессор", "C-01")
        service.use_company(account["company"]["id"])
        compressor = account["equipment_id"]
        pump = service.add_workspace_equipment(account["admin"]["id"], "Насос", "P-01")
        raw = "C-01 перегрелся, простой 10 минут. P-01 перегрелся, простой 80 минут."
        result = ExtractionResult.model_validate({"incidents": [
            {"equipment_id": compressor, "failure_type": "overheating", "downtime_min": 80, "downtime_quote": "80 минут", "confidence": 0.95},
            {"equipment_id": pump, "failure_type": "overheating", "downtime_min": 10, "downtime_quote": "10 минут", "confidence": 0.95},
        ]})
        drafts, _ = extract.validate(raw, result)
        self.assertEqual([draft["downtime_min"] for draft in drafts], [None, None])
        self.assertEqual([draft["downtime_quote"] for draft in drafts], [None, None])
        self.assertTrue(all("downtime_unverified" in draft["issues"] for draft in drafts))
        similar = result.model_copy(deep=True)
        similar.incidents[0].downtime_min = 11
        similar.incidents[0].downtime_quote = "11 минут"
        drafts, _ = extract.validate(raw.replace("80", "11"), similar)
        self.assertEqual([draft["downtime_min"] for draft in drafts], [None, None])
        drafts, _ = extract.validate(raw, extract.rule_based(raw))
        self.assertEqual([(d["equipment_id"], d["downtime_min"]) for d in drafts], [(compressor, 10), (pump, 80)])
        ambiguous = "Компрессор и насос перегрелись, простой 80 минут."
        drafts, _ = extract.validate(ambiguous, result)
        self.assertTrue(all(draft["downtime_min"] is None for draft in drafts))
        unknown = "C-01 перегрелся, а P-999 простаивал 80 минут."
        drafts, _ = extract.validate(unknown, result)
        self.assertIsNone(drafts[0]["downtime_min"])
        numbered = service.add_workspace_equipment(account["admin"]["id"], "Станок №3")
        draft = service.analyze_text("Станок №3 остановился, простой 20 минут.")["drafts"][0]
        self.assertEqual((draft["equipment_id"], draft["downtime_min"]), (numbered, 20))

    def test_overlapping_equipment_downtime_counts_once_in_company_totals(self):
        account = service.start_workspace("Мастерская", "Насос", "P-01", hour_cost_kzt=30_000)
        service.use_company(account["company"]["id"])
        admin, pump = account["admin"]["id"], account["equipment_id"]
        second = service.add_workspace_equipment(admin, "Компрессор", "C-01", hour_cost_kzt=20_000)
        start = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        for equipment, offset, minutes, parts in [(pump, 0, 120, 500), (pump, 1, 120, 0), (second, 1, 60, 0)]:
            service.add_workspace_event(admin, equipment, "Остановка", start + timedelta(hours=offset), "sensor", minutes, parts)
        report = service.company_money_scenario()
        self.assertEqual((report["hours"], report["cost_kzt"], report["event_count"]), (4, 110_000, 3))
        self.assertEqual(service.dashboard()["total_loss_kzt"], 110_500)
        self.assertEqual(service.today_summary()["loss_today_kzt"], 110_500)
        self.assertIn("≈ 90 000 ₸", service.ask_ai("Какие потери у P-01?")["answer"])
        self.assertIn("3 ч простоя", service.ask_ai("Сколько часов простоя у P-01?")["answer"])

    def test_extraction_distinguishes_downtime_from_other_durations(self):
        company = service.start_workspace("Мастерская", "Насос", "P-01")
        service.use_company(company["company"]["id"])
        for utterance, expected in (
            ("простой 1 час 30 минут", 90),
            ("простой час и 15 минут", 75),
            ("простоя не было, осмотр занял 40 минут", 0),
            ("осмотр занял 40 минут", None),
            ("осмотр занял 40 минут, простой 1,5 часа", 90),
        ):
            with self.subTest(utterance=utterance):
                raw = f"P-01: {utterance}"
                draft = service.analyze_text(raw)["drafts"][0]
                self.assertEqual(draft["downtime_min"], expected)
        wrong = ExtractionResult.model_validate({"incidents": [{
            "equipment_id": company["equipment_id"], "failure_type": "other", "downtime_min": 40,
            "downtime_quote": "40 минут", "confidence": 0.9,
        }]})
        drafts, _ = extract.validate("P-01: простоя не было, осмотр занял 40 минут", wrong)
        self.assertIsNone(drafts[0]["downtime_min"])
        self.assertIn("downtime_unverified", drafts[0]["issues"])

    def test_custom_events_drive_separate_repeats_and_savings(self):
        account = service.start_workspace("Мастерская", "Насос", "P-01", hour_cost_kzt=1000)
        service.use_company(account["company"]["id"])
        admin, pump = account["admin"]["id"], account["equipment_id"]
        now = datetime.now()
        for index, label in enumerate(("Утечка воздуха", "Заедание клапана", "Трещина кожуха")):
            service.add_workspace_event(admin, pump, label, now - timedelta(days=index), "other", 30, custom_event_type=label)
        self.assertFalse(service.dashboard()["anomalies"])
        self.assertIn("нет событий одного типа", service.ask_ai("Какие события повторяются на P-01?")["answer"])
        labels = ("утечка воздуха", "УТЕЧКА  ВОЗДУХА")
        for index, label in enumerate(labels, 3):
            service.add_workspace_event(admin, pump, label, now - timedelta(days=index), "other", 30, custom_event_type=label)
        signals = service.dashboard()["anomalies"]
        self.assertEqual([(s["failure_label"], s["failures"]) for s in signals], [("Утечка воздуха", 3)])
        reply = service.ask_ai("Какие события повторяются на P-01?")["answer"]
        self.assertIn("Утечка воздуха — 3 записи", reply)
        self.assertNotIn("Трещина", reply)
        stats = service.savings_plan(pump, "other", "утечка воздуха")["stats"]
        self.assertEqual((stats["failures"], stats["loss_kzt"]), (3, 1500))

    def test_legacy_future_record_does_not_change_risk_or_evidence(self):
        account = service.start_workspace("Мастерская", "Насос")
        service.use_company(account["company"]["id"])
        admin, pump = account["admin"]["id"], account["equipment_id"]
        now = datetime.now()
        for days in (10, 8, 4):
            service.add_workspace_event(admin, pump, "Перегрев", now - timedelta(days=days), "overheating", 20)
        signals = service.dashboard()["anomalies"]
        health = service.equipment_health()
        old = service.add_workspace_event(admin, pump, "Будущий импорт", now, "overheating", 300)
        with db.connect() as conn:
            conn.execute("UPDATE incidents SET occurred_at=?, source_dataset='company_csv' WHERE id=?",
                         ((now + timedelta(days=2)).isoformat(), old))
        self.assertEqual(service.dashboard()["anomalies"], signals)
        self.assertEqual(service.equipment_health(), health)
        self.assertEqual(analytics.group_stats(pump, "overheating")["intervals_days"], [2, 4])
        self.assertIsNotNone(service.ticket(old))

    def test_personal_tickets_use_user_id_and_preserve_unambiguous_legacy_records(self):
        account = service.start_workspace("Мастерская", "Насос")
        company, pump = account["company"]["id"], account["equipment_id"]
        service.use_company(company)
        first = service.add_workers(company, "Мастер смены")[0]
        draft = {"equipment_id": pump, "failure_type": "other", "downtime_min": 5}
        legacy = service.save_record("Старая заявка", [draft], author="Мастер смены")["incident_ids"][0]
        self.assertEqual([t["id"] for t in service.tickets(author_user_id=first["id"])], [legacy])
        second = service.add_workers(company, "Мастер смены")[0]
        own = service.save_record("Моя заявка", [draft], author_user_id=first["id"])["incident_ids"][0]
        inspection = service.save_inspection([{"equipment_id": pump, "state": "remark", "note": "Течёт"}],
                                             second["display_name"], author_user_id=second["id"])["cards"][0]
        self.assertEqual([t["id"] for t in service.tickets(author_user_id=first["id"])], [own])
        self.assertEqual([t["id"] for t in service.tickets(author_user_id=second["id"])], [inspection])
        self.assertIsNotNone(service.ticket(legacy))
        other = service.start_workspace("Соседняя мастерская", "Пресс")
        service.use_company(other["company"]["id"])
        self.assertEqual(service.tickets(author_user_id=first["id"]), [])

    def test_only_one_concurrent_ticket_transition_can_succeed(self):
        account = service.start_workspace("Мастерская", "Насос")
        company = account["company"]["id"]
        service.use_company(company)
        event = service.add_workspace_event(account["admin"]["id"], account["equipment_id"], "Остановка", datetime.now(), "other")
        service.approve_ticket(event, "Администратор")
        read_status = Barrier(2, timeout=5)

        def transition(action):
            service.use_company(company)
            expected_status = service.ticket(event)["status"]
            read_status.wait()
            try:
                action(expected_status)
                return True
            except ValueError as error:
                self.assertIn("текущего статуса", str(error))
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            closed = pool.submit(transition, lambda status: service.resolve_ticket(event, "Мастер", "Починили", expected_status=status))
            escalated = pool.submit(transition, lambda status: service.escalate_ticket(event, "Оператор", "Нужна помощь", expected_status=status))
            self.assertNotEqual(closed.result(), escalated.result())
        ticket = service.ticket(event)
        self.assertEqual(ticket["status"], "resolved" if closed.result() else "escalated")
        self.assertEqual(bool(ticket["resolved_at"]), closed.result())
        self.assertEqual(bool(ticket["resolution"]), closed.result())

    def test_partial_losses_keep_parts_visible_without_zero_total(self):
        from streamlit.testing.v1 import AppTest

        account = service.start_workspace("Мастерская", "Насос без тарифа")
        service.use_company(account["company"]["id"])
        admin = account["admin"]["id"]
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in (0, 3, 6):
            service.add_workspace_event(admin, account["equipment_id"], "Порвался ремень", start + timedelta(hours=hour), "belt_break", 120, 2000)
        known = service.add_workspace_equipment(admin, "Насос с тарифом", hour_cost_kzt=30000)
        service.add_workspace_event(admin, known, "Порвался ремень", datetime.now(), "belt_break", 60, 1000)

        dash = service.dashboard()
        self.assertIsNone(dash["total_loss_kzt"])
        self.assertEqual(dash["parts_kzt"], 7000)
        self.assertEqual(dash["by_equipment"][0]["loss_kzt"], 31000)
        self.assertIsNone(dash["anomalies"][0]["loss_kzt"])
        self.assertIsNone(service.today_summary()["loss_today_kzt"])
        self.assertIn("запчасти 7 000 ₸", service.daily_brief()["headline"])
        self.assertIn("запчасти 7 000 ₸", service.ask_ai("Какие потери?")["answer"])
        plan = service.savings_plan(account["equipment_id"], "belt_break")
        self.assertIsNone(plan["stats"]["loss_kzt"])
        self.assertTrue(any(measure["savings_kzt"] is None for measure in plan["measures"]))

        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        admin_login = account["admin"]
        app.session_state["user"] = service.login(account["company"]["code"], admin_login["login"], admin_login["pin"])
        app.session_state["session-expires-at"] = time() + 3600
        app.session_state["page"] = "План экономии"
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("≈ 330 000 ₸" in item.value for item in app.markdown))
        app.number_input(key="company-money-hour-rate").set_value(10000).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("≈ 90 000 ₸" in item.value for item in app.markdown))
        self.assertTrue(any("Запчасти по журналу · 7 000 ₸" in item.value for item in app.caption))

    def test_reanalysis_saves_only_fields_from_current_text(self):
        from streamlit.testing.v1 import AppTest

        account = service.start_workspace("Мастерская", "Насос", "P-01")
        company = account["company"]["id"]
        service.use_company(company)
        compressor = service.add_workspace_equipment(account["admin"]["id"], "Компрессор", "C-01")
        worker = service.add_workers(company, "Мастер")[0]
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.session_state["user"] = service.login(account["company"]["code"], worker["login"], worker["pin"])
        app.session_state["session-expires-at"] = time() + 3600
        app.run()
        app.button(key="nav_Новая запись").click().run()

        def button(label):
            return next(item for item in app.button if item.label.strip() == label)

        app.text_area(key="raw").set_value("P-01 перегрелся, простой 15 минут.")
        button("Разобрать запись  →").click().run()
        app.text_area(key="raw").set_value("C-01 гудит, простой 48 часов.").run()
        self.assertTrue(button("Подтвердить и сохранить").proto.disabled)
        button("Разобрать запись  →").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="eq_2_0").value, compressor)
        self.assertEqual(app.number_input(key="dt_2_0").value, 2880)
        button("Подтвердить и сохранить").click().run()
        service.use_company(company)
        self.assertEqual([(row["equipment_id"], row["downtime_min"]) for row in service.tickets()], [(compressor, 2880)])

    def test_url_cannot_restore_session_and_expiry_requires_login(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
        app.query_params["s"] = "legacy-session-token"
        app.run()
        self.assertNotIn("s", app.query_params)
        self.assertFalse(any(item.key == "nav_Панель задач" for item in app.button))
        for label, value in (("Код компании", "FIXLIST"), ("Логин", "boss"), ("PIN-код", "1111")):
            next(item for item in app.text_input if item.label == label).set_value(value)
        next(item for item in app.button if item.label == "Войти →").click().run()
        self.assertTrue(any(item.key == "nav_Панель задач" for item in app.button))
        self.assertNotIn("s", app.query_params)
        app.session_state["session-expires-at"] = time() - 1
        app.run()
        self.assertFalse(any(item.key == "nav_Панель задач" for item in app.button))
        self.assertTrue(any("Сеанс завершён" in item.value for item in app.info))


if __name__ == "__main__":
    unittest.main()

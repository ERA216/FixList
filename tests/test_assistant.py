"""Check evidence, follow-ups, and calculations for the two chat workspaces."""
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import archive, config, db, service
from backend.assistant import SourceSelection
from backend.schemas import kzt


class AssistantTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "test.db", LLM_PROVIDER="mock", SMTP_PORT=0))
        service.bootstrap()
        self.addCleanup(db.set_company, 1)
        self.account = service.start_workspace("Цех", "Насос", "P-01")
        service.use_company(self.account["company"]["id"])
        self.admin = self.account["admin"]["id"]
        self.pump = self.account["equipment_id"]
        self.feed = service.add_workspace_equipment(self.admin, "Насос подачи", "P-02", hour_cost_kzt=30000)
        self.last_feed = self.record(self.feed, 90, 1)
        self.record(self.feed, 300, 15)
        self.record(self.pump, 480, 2)

    def record(self, equipment_id, minutes, days_ago=0):
        return service.add_workspace_event(self.admin, equipment_id, "Перегрев", datetime.now() - timedelta(days=days_ago),
                                           "overheating", minutes, 2000)

    def test_filters_specific_equipment_period_and_remembers_followup(self):
        first = service.ask_ai("Что происходило на насосе подачи за последние 7 дней?")
        self.assertIn("1,5 ч простоя", first["answer"])
        self.assertEqual([row["id"] for row in first["sources"]], [self.last_feed])
        followup = service.ask_ai("А сколько было потерь?", [first])
        self.assertIn("47 000 ₸", followup["answer"])
        self.assertEqual([row["id"] for row in followup["sources"]], [self.last_feed])
        self.assertIn("нет полной оценки", service.ask_ai("Какие потери у насоса P-01?")["answer"])

    def test_open_ticket_counts_and_priority_are_based_on_workflow(self):
        for _ in range(20):
            self.record(self.feed, 15)
        service.approve_ticket(self.last_feed, "Администратор")
        service.escalate_ticket(self.last_feed, "Мастер", "Нужен датчик")
        answer = service.ask_ai("Какие заявки открыты?")
        self.assertIn("Открытых заявок: 23", answer["answer"])
        self.assertIn("Нужна помощь: 1", answer["answer"])
        priority = service.ask_ai("Что проверить в первую очередь?")
        self.assertEqual([row["id"] for row in priority["sources"]], [self.last_feed])
        self.assertIn("Нужен датчик", priority["answer"])
        another = service.register_company("Другой цех")
        service.use_company(another["company"]["id"])
        self.assertIn("не найдена", service.ask_ai(f"Что было в записи #{self.last_feed}?")["answer"])

    def test_archive_dates_counts_code_and_comparison_match_public_data(self):
        demo = service.login("FIXLIST", "boss", "1111")
        service.use_company(demo["company_id"])
        month = archive.report("KEL01", date(2016, 1, 1), date(2016, 1, 31))
        answer = service.ask_ai("Сколько часов простоя у Kelmarsh 1 в январе?")
        self.assertIn(f"{month['stats']['events']} записи", answer["answer"])
        self.assertTrue(all(row["equipment"] == "Kelmarsh 1" for row in answer["sources"]))
        self.assertIn("UTC", answer["period"])
        for query in ("Сколько часов простоя у Kelmarsh 1 с 1 по 31 января 2016 года?",
                      "Сколько часов простоя у Kelmarsh 1 за 1–31 января?"):
            span = service.ask_ai(query)
            self.assertEqual(span["period"], "01.01.2016 — 31.01.2016 (UTC)")
            self.assertEqual(span["answer"], answer["answer"])
        crossed = service.ask_ai("Сколько часов простоя у Kelmarsh 1 с 28 января по 2 февраля 2016 года?")
        self.assertEqual(crossed["period"], "28.01.2016 — 02.02.2016 (UTC)")
        bad = service.ask_ai("Что было на Kelmarsh 1 с 31 по 1 января?")
        self.assertIn("Дата начала", bad["answer"])
        self.assertEqual("14.01.2016 (UTC)", service.ask_ai("Что было 14 января 2016?")["period"])
        self.assertEqual(["KEL01"], service.ask_ai("Что произошло на Келмарш #1?")["context"]["equipment"])
        self.assertEqual(["KEL01"], service.ask_ai("Что произошло по коду оборудования: Kelmarsh1?")["context"]["equipment"])
        ranked = service.ask_ai("Какая турбина простаивала дольше?")
        longest = max(archive.report()["stats"]["equipment"], key=lambda row: row["hours"])["name"]
        self.assertTrue(ranked["answer"].splitlines()[1].startswith(f"- {longest}:"))
        code = service.ask_ai("Сколько записей по коду #111?")
        self.assertTrue(code["sources"])
        self.assertTrue(all(row["code"] == "111" for row in code["sources"]))
        self.assertIn("За запрошенный период данных нет", service.ask_ai("Что происходило сегодня?")["answer"])

    def test_ambiguous_equipment_and_unknown_causes_remain_explicit(self):
        service.add_workspace_equipment(self.admin, "Насос подачи", "P-03")
        self.assertIn("Уточните код", service.ask_ai("Что происходило на насосе подачи?")["answer"])
        self.assertIn("причина и результат ремонта не указаны", service.ask_ai("Почему перегрелся P-02?")["answer"])

    def test_unknown_equipment_code_does_not_fall_back_to_other_equipment(self):
        known = service.ask_ai("Какие потери у оборудования P-02?")
        self.assertTrue(known["sources"])
        for query, code in (("Какие потери у оборудования P-999?", "P-999"),
                            ("Сравни потери у P-02 и P-999.", "P-999"),
                            ("Какие потери по коду оборудования: «НЕИЗВЕСТНЫЙ»?", "НЕИЗВЕСТНЫЙ")):
            with self.subTest(query=query):
                answer = service.ask_ai(query, [known])
                self.assertIn(f"«{code}» не найдено", answer["answer"])
                self.assertFalse(answer["sources"])

    def test_loss_question_prices_archive_hours_and_accepts_explicit_assumptions(self):
        demo = service.login("FIXLIST", "boss", "1111")
        service.use_company(demo["company_id"])
        seconds = archive.report()["stats"]["downtime_seconds"]
        default = service.ask_ai("насколько убытков я ушел")["answer"]
        self.assertIn(f"≈ {kzt(seconds / 3600 * 50_000)} ₸", default)
        self.assertIn(f"≈ {kzt(seconds / 3600 * 10_000)} ₸", default)
        self.assertIn("Пример тарифа", default)
        january = archive.report("KEL01", date(2016, 1, 1), date(2016, 1, 31))["stats"]["downtime_seconds"]
        custom = service.ask_ai("Сколько можно сэкономить на Kelmarsh 1 в январе при тарифе 60 000 ₸/ч, если сократить простой на 10%?",
                                hourly_rate=40_000, reduction_percent=30)["answer"]
        self.assertIn(f"≈ {kzt(january / 3600 * 6_000)} ₸", custom)
        self.assertIn("60 000 ₸/ч", custom)
        self.assertIn("10%", custom)
        invalid = service.ask_ai("Какие потери при тарифе -1000 ₸/ч?")["answer"]
        self.assertIn("Укажите тариф от 0", invalid)

    def test_money_followup_preserves_tariff_scope_and_explicit_changes(self):
        demo = service.login("FIXLIST", "boss", "1111")
        service.use_company(demo["company_id"])
        first = service.ask_ai("Сколько можно сэкономить на Kelmarsh 1 в январе при тарифе 60 000 ₸/ч?")
        for question in ("А если сократить его на 10%?", "Если сократить простой на 10%?"):
            with self.subTest(question=question):
                answer = service.ask_ai(question, [first])
                self.assertEqual(answer["period"], first["period"])
                self.assertEqual(answer["context"]["equipment"], ["KEL01"])
                self.assertIn("≈ 2 317 840 ₸", answer["answer"])
                self.assertIn("60 000 ₸/ч", answer["answer"])
        changed = service.ask_ai("А при тарифе 30 000 ₸/ч?", [first, answer])
        self.assertIn("≈ 1 158 920 ₸", changed["answer"])
        hours = service.ask_ai("А сколько часов было простоя?", [first])
        self.assertEqual(hours["context"]["topic"], "downtime")
        self.assertNotIn("₸", hours["answer"])

    def test_money_followup_keeps_specific_record(self):
        first = service.ask_ai(f"Какие потери в записи #{self.last_feed}?")
        answer = service.ask_ai("А если снизить простой на 10%?", [first])
        self.assertIn("≈ 4 500 ₸", answer["answer"])
        self.assertEqual([row["id"] for row in answer["sources"]], [self.last_feed])

    def test_private_estimate_uses_scenario_rate_only_for_unpriced_hours(self):
        report = service.company_money_scenario(hourly_rate=10_000, reduction_percent=10)
        self.assertEqual(report["hours"], 14.5)
        self.assertEqual(report["unrated_hours"], 8)
        self.assertEqual(report["cost_kzt"], 275_000)
        self.assertEqual(report["savings_kzt"], 27_500)
        self.assertEqual(report["parts_kzt"], 6000)
        answer = service.ask_ai("Сколько можно сэкономить?", hourly_rate=10_000, reduction_percent=10)["answer"]
        self.assertIn("≈ 27 500 ₸", answer)
        self.assertIn("запчасти 6 000 ₸", answer)
        self.assertEqual(service.company_money_scenario(hourly_rate=0)["cost_kzt"], 195_000)

    def test_provider_can_sort_sources_but_cannot_add_facts_or_ids(self):
        question = "Что происходило на P-02 за последние 30 дней?"
        baseline = service.ask_ai(question)
        older = baseline["sources"][-1]["id"]
        with patch("backend.config.ai_enabled", return_value=True), patch("backend.llm.call_json") as call:
            call.return_value = SourceSelection.model_validate({
                "answer": "Клапан заменили. Насос уже работает.", "source_ids": [older],
            })
            good = service.ask_ai(question)
            self.assertEqual(good["mode"], "ai")
            self.assertEqual(good["answer"], baseline["answer"])
            self.assertEqual([row["id"] for row in good["sources"]], [older, self.last_feed])
            self.assertTrue(all(row["description"] == "Перегрев" for row in good["sources"]))
            call.return_value = SourceSelection(source_ids=[999876])
            rejected = service.ask_ai(question)
            self.assertEqual(rejected["answer"], baseline["answer"])
            self.assertEqual(rejected["sources"], baseline["sources"])
            self.assertEqual(rejected["mode"], "rules")
            self.assertTrue(rejected["warning"])

    def test_workers_receive_estimate_without_admin_navigation(self):
        from streamlit.testing.v1 import AppTest

        company = self.account["company"]
        worker = service.add_workers(company["id"], "Мастер")[0]
        users = (service.login(company["code"], worker["login"], worker["pin"]), service.login("FIXLIST", "master", "2222"))
        for user in users:
            with self.subTest(company=user["company_code"]):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=25)
                app.session_state["user"] = user
                app.session_state["session-expires-at"] = time() + 3600
                app.session_state["page"] = "Спросить ИИ"
                app.run()
                app.chat_input[0].set_value("Сколько можно сэкономить?").run()
                self.assertFalse(app.exception)
                self.assertTrue(any("**Возможная экономия: ≈" in item.value for item in app.markdown))
                self.assertFalse(any(button.key == "chat-money" for button in app.button))
                self.assertEqual(app.session_state["page"], "Спросить ИИ")


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for evidence in an incident drafted from the shift log."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend import config, db, extract, service
from backend.schemas import ExtractionResult


class ReportExtractionTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=folder, DB_PATH=folder / "test.db",
                                         LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        self.account = service.start_workspace("Проверка отчётов", "Компрессор", "C-03")
        service.use_company(self.account["company"]["id"])
        self.addCleanup(db.set_company, 1)
        self.equipment = self.account["equipment_id"]
        self.admin = self.account["admin"]

    def test_equipment_code_does_not_become_duration(self):
        for note, minutes, quote in (
            ("C-03 час стоял, 20 минут проверяли", 60, "час"),
            ("C-03 стоял час до ошибки E12. Простоя не было после перезапуска.", 60, "час"),
            ("C-03 стоял 1ч30мин после ошибки E12.", 90, "1ч30мин"),
        ):
            with self.subTest(note=note):
                draft = service.analyze_text(note)["drafts"][0]
                self.assertEqual((draft["equipment_id"], draft["downtime_min"]), (self.equipment, minutes))
                self.assertEqual(draft["downtime_quote"], quote)

    def test_signal_code_does_not_become_another_machine(self):
        service.add_workspace_equipment(self.admin["id"], "Насос", "E12")
        for note in (
            "C-03 стоял 10 минут из-за ошибки E12",
            "C-03 стоял 10 минут. Код ошибки: E12.",
            "C-03 стоял 10 минут, сработал аварийный сигнал E12.",
        ):
            with self.subTest(note=note):
                drafts = service.analyze_text(note)["drafts"]
                self.assertEqual([(d["equipment_id"], d["downtime_min"]) for d in drafts],
                                 [(self.equipment, 10)])

    def test_unsupported_model_facts_are_removed_before_normal_save(self):
        raw = "C-03 перегрелся. Датчик был исправен, датчик не заменили. Ничего не меняли, простой 40 минут."

        def model(system, message, schema, purpose):
            if purpose != "extract":
                return schema.model_validate({"hypotheses": []})
            return schema.model_validate({"incidents": [{
                "equipment_id": self.equipment, "failure_type": "overheating", "downtime_min": 40,
                "downtime_quote": "40 минут", "confidence": 0.98, "cause": "Сгорел датчик и оплавился щит",
                "action": "заменили датчик", "parts": [{"name": "контактор", "qty": 50}],
            }]})

        with patch("backend.llm.call_json", side_effect=model):
            result = service.analyze_text(raw)
        draft = result["drafts"][0]
        self.assertEqual(result["mode"], "ai")
        self.assertTrue(draft["needs_review"])
        self.assertTrue({"cause_removed", "action_unverified", "parts_unverified"} <= set(draft["issues"]))
        self.assertIsNone(draft["cause"])
        self.assertIsNone(draft["action"])
        self.assertEqual(draft["parts"], [])
        self.assertIsNone(draft["parts_cost_kzt"])
        saved = service.save_record(raw, [draft], result["mode"], author_user_id=self.admin["id"])
        row = db.incidents(incident_id=saved["incident_ids"][0])[0]
        self.assertIsNone(row["cause"])
        self.assertIsNone(row["action"])
        self.assertEqual(row["parts_json"], "[]")
        self.assertFalse(row["parts_cost_kzt"])

    def test_completed_work_and_quantity_are_scoped_to_the_correct_machine(self):
        pump = service.add_workspace_equipment(self.admin["id"], "Насос", "P-01")
        raw = "C-03 перегрелся. Простой 40 минут. P-01 остановился из-за пыли. Заменили 2 контактора. Простой 30 минут."
        facts = {
            "failure_type": "electrical", "cause": "пыли", "action": "Заменили 2 контактора",
            "parts": [{"name": "контактор", "qty": 2}], "confidence": 0.98,
        }
        result = ExtractionResult.model_validate({"incidents": [
            {**facts, "equipment_id": self.equipment, "downtime_min": 40},
            {**facts, "equipment_id": pump, "downtime_min": 30},
        ]})
        first, second = extract.validate(raw, result)[0]
        self.assertEqual((first["cause"], first["action"], first["parts"]), (None, None, []))
        self.assertEqual((second["cause"], second["action"], second["parts_cost_kzt"]),
                         ("пыли", "Заменили 2 контактора", 18000))
        self.assertEqual(second["parts"], [{"name": "контактор", "qty": 2}])

    def test_rules_keep_completed_replacement_but_ignore_plans_and_negation(self):
        for text, expected in (
            ("C-03: порвался ремень. Поставили новый. Простой 30 минут.", [{"name": "ремень", "qty": 1}]),
            ("C-03: нужно заменить 2 контактора, простой 30 минут.", []),
            ("C-03: не заменили датчик, простой 30 минут.", []),
            ("C-03: заменили бы датчик. Простой 30 минут.", []),
            ("C-03: датчик исправен. Поставили новый. Простой 30 минут.", []),
            ("C-03: заменили датчики, простой 30 минут.", []),
            ("C-03: заменили 2 контактора, простой 30 минут.", [{"name": "контактор", "qty": 2}]),
        ):
            with self.subTest(text=text):
                self.assertEqual(service.analyze_text(text)["drafts"][0]["parts"], expected)

"""Exercise Gemini's HTTP contract and actual app paths without credentials or live traffic."""
from copy import deepcopy
from datetime import datetime
import json
from json import loads
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

from backend import config, db, llm, service
from backend.schemas import ExtractionResult


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body
        self.text = json.dumps(body)

    def json(self):
        return self.body


def generated(payload, *, finish="STOP"):
    return Response(body={"candidates": [{"finishReason": finish, "content": {"parts": [
        {"text": "hidden reasoning", "thought": True}, {"text": json.dumps(payload, ensure_ascii=False)},
    ]}}]})


class GeminiIntegrationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "gemini.db", SMTP_HOST="",
                                         LLM_PROVIDER="gemini", GEMINI_API_KEY="fake-test-key", GEMINI_API_KEY_2="",
                                         LLM_MODEL="gemini-test-model", GEMINI_FALLBACK_MODELS=["gemini-backup-model"]))
        self.enterContext(patch("backend.llm.time.sleep"))
        llm._working.clear()
        self.addCleanup(llm._working.clear)
        service.bootstrap()
        self.admin = service.login("FIXLIST", "boss", "1111")
        service.use_company(self.admin["company_id"])
        self.addCleanup(db.set_company, 1)
        self.payloads = []

    def provider(self, url, headers, json, timeout):
        self.assertEqual(headers["x-goog-api-key"], "fake-test-key")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-test-model:generateContent")
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, config.LLM_TIMEOUT)
        schema = json["generationConfig"]["responseJsonSchema"]
        self.assertIn("source_ids", schema["properties"])
        self.assertEqual(json["generationConfig"]["responseMimeType"], "application/json")
        payload = loads(json["contents"][0]["parts"][0]["text"])
        self.payloads.append(payload)
        record = payload["records"][0]
        reply = {"source_ids": [record["id"]],
                 "commentary": "Сопоставьте время остановки с сервисным журналом. Причина требует проверки.",
                 "quotes": [{"source_id": record["id"], "quote": record["description"]}]}
        if "checks" in payload:
            reply["check_order"] = [1, 0]
        return generated(reply)

    def test_archive_money_chat_calls_gemini_and_keeps_exact_calculations(self):
        question = "Сколько можно сэкономить на Kelmarsh 1 в январе при тарифе 60000 ₸/ч?"
        with patch("backend.config.ai_enabled", return_value=False):
            baseline = service.ask_ai(question)
        with patch("backend.llm.requests.post", side_effect=self.provider) as post:
            answer = service.ask_ai(question)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(answer["mode"], "ai")
        self.assertEqual(answer["provider"], "gemini")
        self.assertTrue(answer["ai_commentary"])
        self.assertEqual(answer["answer"], baseline["answer"])
        self.assertEqual(answer["context"], baseline["context"])
        self.assertEqual(self.payloads[0]["verified_answer"], baseline["answer"])
        self.assertTrue(all(row["equipment"] == "Kelmarsh 1" for row in self.payloads[0]["records"]))
        self.assertEqual(db.llm_stats()["ok"], 1)
        self.assertIsNone(llm._schema.get())
        self.assertIsNone(llm._deadline.get())

    def test_archive_proposal_uses_gemini_without_inventing_or_saving_repair(self):
        row = db.incidents()[0]
        before = deepcopy(row)
        with patch("backend.config.ai_enabled", return_value=False):
            baseline = service.ticket_recommendation(row["id"])
        with patch("backend.llm.requests.post", side_effect=self.provider) as post:
            plan = service.ticket_recommendation(row["id"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(plan["mode"], "ai")
        self.assertEqual(plan["proposed_solution"]["solution"], baseline["proposed_solution"]["solution"])
        self.assertEqual(plan["proposed_solution"]["checks"], baseline["proposed_solution"]["checks"][::-1])
        self.assertEqual(db.incidents(incident_id=row["id"])[0], before)
        self.assertEqual(len(db.incidents()), 177)

    def test_private_chat_uses_only_current_company_data(self):
        account = service.start_workspace("Закрытый цех", "Насос", "P-01")
        service.use_company(account["company"]["id"])
        service.add_workspace_event(account["admin"]["id"], account["equipment_id"], "Перегрев", datetime.now(), "overheating", 30)
        with patch("backend.llm.requests.post", side_effect=self.provider):
            answer = service.ask_ai("Что происходит?")
        self.assertEqual(answer["mode"], "ai")
        serialized = json.dumps(self.payloads, ensure_ascii=False)
        self.assertNotIn("Kelmarsh", serialized)
        self.assertNotIn("pin_hash", serialized)
        self.assertNotIn("fake-test-key", serialized)

    def test_untrusted_model_content_is_rejected_without_losing_the_verified_answer(self):
        question = "Что произошло на Kelmarsh 1?"
        with patch("backend.config.ai_enabled", return_value=False):
            baseline = service.ask_ai(question)
        row = baseline["sources"][0]
        good = {"source_ids": [row["id"]], "commentary": "Сверьте исходную запись с сервисным журналом.",
                "quotes": [{"source_id": row["id"], "quote": row["description"]}]}
        variants = [
            {"source_ids": [999999]},
            {"quotes": [{"source_id": row["id"], "quote": "Такой строки нет в журнале"}]},
            {"commentary": "Потери составили 987654321 тенге."},
            {"commentary": "Двигатель заменили и отремонтировали."},
            {"commentary": "Проверьте Kelmarsh 987654321."},
            {"commentary": "Откройте https://example.com/secret"},
            {"quotes": []},
        ]
        for changes in variants:
            with self.subTest(changes=changes), patch("backend.llm.requests.post", return_value=generated({**good, **changes})):
                answer = service.ask_ai(question)
                self.assertEqual(answer["mode"], "rules")
                self.assertEqual(answer["answer"], baseline["answer"])
                self.assertEqual(answer["sources"], baseline["sources"])
                self.assertTrue(answer["warning"])
                self.assertNotIn("ai_commentary", answer)

    def test_archive_plan_rejects_unknown_check_ids(self):
        row = db.incidents()[0]
        def bad_checks(*args, **kwargs):
            response = self.provider(*args, **kwargs)
            payload = json.loads(response.body["candidates"][0]["content"]["parts"][1]["text"])
            payload["check_order"] = [999]
            return generated(payload)
        with patch("backend.llm.requests.post", side_effect=bad_checks):
            plan = service.ticket_recommendation(row["id"])
        self.assertEqual(plan["mode"], "archive")
        self.assertTrue(plan["warning"])
        self.assertEqual(len(plan["steps"]), 2)

    def test_missing_key_never_sends_a_request(self):
        with patch.multiple(config, GEMINI_API_KEY="", GEMINI_API_KEY_2=""), patch("backend.llm.requests.post") as post:
            answer = service.ask_ai("Что произошло на Kelmarsh 1?")
            self.assertEqual(answer["mode"], "rules")
            self.assertFalse(post.called)

    def test_provider_failures_leave_chat_and_tickets_usable_and_do_not_log_keys(self):
        row = db.incidents()[0]
        failures = [
            Response(401, {"error": "fake-test-key"}), Response(429, {"error": "quota"}),
            Response(404, {"error": "model unavailable"}), Response(503, {"error": "overloaded"}),
            Response(body=[]), Response(body={}), generated({"source_ids": [row["id"]]}, finish="MAX_TOKENS"),
            requests.ConnectionError("fake-test-key"), requests.Timeout("slow"),
        ]
        for failure in failures:
            llm._working.clear()
            kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(failure=type(failure).__name__), patch("backend.llm.requests.post", **kwargs):
                answer = service.ask_ai("Что произошло на Kelmarsh 1?")
                plan = service.ticket_recommendation(row["id"])
                self.assertEqual(answer["mode"], "rules")
                self.assertEqual(plan["mode"], "archive")
                self.assertTrue(answer["warning"])
                self.assertTrue(plan["warning"])
        with db.connect() as conn:
            errors = " ".join(row[0] or "" for row in conn.execute("SELECT error FROM llm_calls"))
        self.assertNotIn("fake-test-key", errors)

    def test_invalid_json_retries_schema_and_then_falls_back(self):
        response = Response(body={"candidates": [{"content": {"parts": [{"text": "not JSON"}]}}]})
        with patch("backend.llm.requests.post", return_value=response) as post:
            answer = service.ask_ai("Что произошло на Kelmarsh 1?")
        self.assertEqual(answer["mode"], "rules")
        self.assertTrue(answer["warning"])
        self.assertEqual(post.call_count, config.LLM_MAX_RETRIES + 1)

    def test_request_timeout_honors_remaining_budget(self):
        token = llm._deadline.set(time.monotonic() + 0.2)
        try:
            with patch("backend.llm.requests.post", return_value=Response(body={})) as post:
                llm._post("https://example.test", {}, {})
            self.assertLessEqual(post.call_args.kwargs["timeout"], 0.2)
        finally:
            llm._deadline.reset(token)
        token = llm._deadline.set(time.monotonic() - 1)
        try:
            with patch("backend.llm.requests.post") as post, self.assertRaises(llm.LLMOverloaded):
                llm._post("https://example.test", {}, {})
            self.assertFalse(post.called)
        finally:
            llm._deadline.reset(token)

    def test_invalid_environment_limits_use_safe_defaults(self):
        for value in ("invalid", "", "nan", "inf", "-1", "99999"):
            with self.subTest(value=value), patch.dict(os.environ, {"LLM_TIMEOUT": value}):
                self.assertEqual(config._number_setting("LLM_TIMEOUT", 12, 1, 60), 12)

    def test_non_json_http_response_falls_back(self):
        response = Response()
        with patch.object(response, "json", side_effect=ValueError("HTML response")), patch("backend.llm.requests.post", return_value=response):
            answer = service.ask_ai("Что произошло на Kelmarsh 1?")
        self.assertEqual(answer["mode"], "rules")
        self.assertTrue(answer["warning"])

    def test_successful_explanation_is_visible_in_chat(self):
        from streamlit.testing.v1 import AppTest

        with patch("backend.llm.requests.post", side_effect=self.provider):
            reply = service.ask_ai("Что произошло на Kelmarsh 1?")
        app = AppTest.from_string("import streamlit as st\nfrom ui.assistant_chat import render\nrender(st.session_state['messages'], can_open_finance=False)", default_timeout=25)
        app.session_state["messages"] = [{"q": "Что произошло?", **reply}]
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any(reply["ai_commentary"] in item.value for item in app.markdown))
        self.assertTrue(any("gemini" in item.value for item in app.caption))


if __name__ == "__main__":
    unittest.main()

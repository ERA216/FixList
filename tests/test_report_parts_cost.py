"""Persist only confirmed costs from the shift report editor."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import config, db, service


class ReportPartsCostTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(config, ROOT=root, DB_PATH=root / "parts.db",
                                         LLM_PROVIDER="mock", SMTP_HOST=""))
        service.bootstrap()
        workspace = service.start_workspace("Мастерская", "Компрессор", "C-03")
        service.use_company(workspace["company"]["id"])
        self.addCleanup(db.set_company, 1)
        self.draft = {"equipment_id": workspace["equipment_id"], "failure_type": "electrical",
                      "downtime_min": 10, "parts": [{"name": "контактор", "qty": 1}]}

    def save(self, **extra):
        return service.save_record("Заменили контактор.", [{**self.draft, **extra}])["incident_ids"][0]

    def test_editor_preserves_unknown_zero_and_explicit_costs(self):
        unknown = self.save(parts_cost_known=False, parts_cost_kzt=None)
        free = self.save(parts_cost_known=True, parts_cost_kzt=0)
        confirmed = self.save(parts_cost_known=True, parts_cost_kzt=1250)
        legacy = self.save()

        self.assertIsNone(service.ticket(unknown)["parts_cost_kzt"])
        self.assertEqual(service.ticket(free)["parts_cost_kzt"], 0)
        self.assertEqual(service.ticket(confirmed)["parts_cost_kzt"], 1250)
        self.assertEqual(service.ticket(legacy)["parts_cost_kzt"], db.part_price("контактор"))
        with db.connect() as conn:
            row = conn.execute(
                "SELECT parts_cost_kzt,parts_cost_known,parts_json FROM incidents WHERE id=?", (unknown,)
            ).fetchone()
        self.assertEqual((row["parts_cost_kzt"], row["parts_cost_known"]), (0, 0))
        self.assertIn("контактор", row["parts_json"])

    def test_invalid_edited_parts_or_cost_reject_the_report_before_any_write(self):
        invalid = (
            {"parts_cost_known": False, "parts": [{"name": "контактор", "qty": 101}]},
            {"parts_cost_known": False, "parts": [{"name": " ", "qty": 1}]},
            {"parts_cost_known": False, "parts_cost_kzt": 10},
            {"parts_cost_known": True, "parts_cost_kzt": 1_000_000_001},
            {"parts_cost_known": True, "parts_cost_kzt": 1.2},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(**values)
        self.assertEqual(db.shift_logs(), [])
        self.assertEqual(db.incidents(), [])

"""Sign-in protection for existing accounts and newly issued PINs."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from backend import auth, config, db, service


class LoginLimitTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(config, "DB_PATH", Path(folder) / "auth.db"))
        db.init_db()
        self.company = auth.create_company("Тестовая компания")
        self.code = self.company["code"]
        self.worker = auth.add_user(self.company["id"], "Сотрудник", "worker", login="worker", pin="4321")
        self.clock = self.enterContext(patch("backend.auth.time"))
        self.clock.time.return_value = 1_000_000

    def test_limit_persists_and_expires_without_changing_legacy_pin(self):
        for _ in range(auth.LOGIN_FAILURE_LIMIT):
            self.assertIsNone(service.login(self.code, "worker", "0000"))
        db.init_db()
        with patch.object(auth, "check_pin", wraps=auth.check_pin) as verifier:
            self.assertIsNone(service.login(self.code.lower(), " WORKER ", self.worker["pin"]))
            self.clock.time.return_value += auth.LOGIN_WINDOW_SECONDS - 1
            self.assertIsNone(service.login(self.code, "worker", "0000"))
            verifier.assert_not_called()
        self.clock.time.return_value += 1
        self.assertEqual(service.login(self.code, "worker", self.worker["pin"])["id"], self.worker["id"])

    def test_success_clears_failures_and_other_accounts_keep_access(self):
        other = auth.create_company("Другой кабинет")
        other_worker = auth.add_user(other["id"], "Сотрудник", "worker", login="worker")
        self.assertRegex(other_worker["pin"], r"^[0-9]{8}$")
        for _ in range(2):
            for _ in range(auth.LOGIN_FAILURE_LIMIT - 1):
                self.assertIsNone(service.login(self.code, "worker", "0000"))
            self.assertIsNotNone(service.login(self.code, "worker", self.worker["pin"]))
        for _ in range(auth.LOGIN_FAILURE_LIMIT):
            self.assertIsNone(service.login(self.code, "worker", "0000"))
        self.assertIsNotNone(service.login(other["code"], "worker", other_worker["pin"]))

    def test_parallel_failures_count_toward_the_same_limit(self):
        barrier = Barrier(auth.LOGIN_FAILURE_LIMIT)

        def reject(*_):
            barrier.wait(timeout=10)
            return False

        with patch.object(auth, "check_pin", side_effect=reject):
            with ThreadPoolExecutor(max_workers=auth.LOGIN_FAILURE_LIMIT) as pool:
                results = list(pool.map(lambda _: service.login(self.code, "worker", "0000"),
                                        range(auth.LOGIN_FAILURE_LIMIT)))
            self.assertEqual(results, [None] * auth.LOGIN_FAILURE_LIMIT)
        self.assertIsNone(service.login(self.code, "worker", self.worker["pin"]))

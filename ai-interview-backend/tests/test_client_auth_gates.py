"""Account-state gates: real source with isolated ORM/JWT substitutes; no services."""
import ast
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from _a2_test_support import ROOT, APIException, FakeDB, namespace
from test_auth_security import AuthSecurityTests as _AuthSetup


class ClientAuthGateTests(_AuthSetup):
    def test_unverified_login_rejects_before_session_side_effects(self):
        account, db = self.fixture("client")
        account.is_verified = False
        db = FakeDB(account)
        with patch.object(self.auth, "create_access_token") as issue:
            with self.assertRaises(APIException) as error:
                self.login("client", db)
            self.assertEqual(error.exception.status_code, 403)
            issue.assert_not_called()
        self.assertFalse(self.issued)
        self.assertEqual(db.commits, 0)
        self.assertEqual(len(db.rows), 1)
        self.assertEqual(len(db.queries), 1)  # User lookup only; no token UPDATE.

    def verify(self, db):
        method = self.services["client"].verify_email_and_login
        method.__globals__["redis_verification_service"] = SimpleNamespace(verify_code=AsyncMock())
        return self.run_async(method(db, "synthetic@example.invalid", "synthetic-code"))

    def test_inactive_verification_rejects_before_state_or_session_changes(self):
        account, db = self.fixture("client")
        account.is_active = False
        account.is_verified = False
        db = FakeDB(account)
        with patch.object(self.auth, "create_access_token") as issue:
            with self.assertRaises(APIException) as error:
                self.verify(db)
            self.assertEqual(error.exception.status_code, 400)
            issue.assert_not_called()
        self.assertFalse(account.is_verified)
        self.assertFalse(self.issued)
        self.assertEqual(db.commits, 0)
        self.assertEqual(len(db.queries), 1)

    def test_active_pending_user_can_verify_and_receive_session(self):
        account, db = self.fixture("client")
        account.is_verified = False
        result = self.verify(db)
        self.assertTrue(account.is_verified)
        self.assertIn("refresh_token", result)
        self.assertEqual(db.commits, 1)

    def dependency(self):
        node = next(n for n in ast.parse((ROOT / "app/api/client/deps.py").read_text(encoding="utf8")).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_current_user")
        env = namespace()
        env.update(AuthBase=self.auth, Depends=lambda *_: None, oauth2_scheme=None,
                   get_db=None, HTTPException=HTTPException)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "client/deps.py", "exec"), env)
        return env["get_current_user"]

    def test_previously_issued_access_rejects_unverified_user(self):
        account, db = self.fixture("client")
        raw = self.auth.create_access_token(str(account.id), scope="client")
        account.is_verified = False
        with self.assertRaises(HTTPException) as error:
            self.run_async(self.dependency()(raw, db))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(db.commits, 0)

    def test_dependency_preserves_active_and_scope_checks(self):
        account, db = self.fixture("client")
        raw = self.auth.create_access_token(str(account.id), scope="client")
        self.assertIs(self.run_async(self.dependency()(raw, db)), account)
        account.is_active = False
        with self.assertRaises(HTTPException) as error:
            self.run_async(self.dependency()(raw, db))
        self.assertEqual(error.exception.status_code, 401)
        wrong = self.auth.create_access_token(str(account.id), scope="backoffice")
        with self.assertRaises(HTTPException) as error:
            self.run_async(self.dependency()(wrong, db))
        self.assertEqual(error.exception.status_code, 403)

    def test_verified_login_refresh_and_logout_still_work(self):
        _, db = self.fixture("client")
        raw = self.login("client", db)["refresh_token"]
        service = self.services["client"]
        self.assertIn("access_token", self.run_async(service.refresh_token(db, raw)))
        self.run_async(service.logout(db, raw))
        with self.assertRaises(APIException):
            self.run_async(service.refresh_token(db, raw))

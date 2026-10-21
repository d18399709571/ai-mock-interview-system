"""Auth control-flow tests use fake JWT/password providers, not crypto E2E."""
import ast
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from _a2_test_support import (
    ROOT, APIException, Admin, AdminToken, FakeDB, IsolatedCase,
    Token, User, namespace, source_class,
)


class AuthSecurityTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(SECRET_KEY="synthetic-test-only",
                                        ALGORITHM="HS256", ACCESS_TOKEN_EXPIRE_MINUTES=30)
        self.issued = {}

        class JWTError(Exception):
            pass

        def encode(payload, key, algorithm):
            value = "synthetic." + uuid.uuid4().hex
            self.issued[value] = payload
            return value

        def decode(value, key, algorithms):
            payload = self.issued.get(value)
            if not payload or payload["exp"] <= datetime.now(UTC):
                raise JWTError()
            return dict(payload)

        env = namespace()
        env.update(settings=self.settings, uuid=uuid, JWTError=JWTError,
                   jwt=SimpleNamespace(encode=encode, decode=decode),
                   pwd_context=Mock())
        self.security_env = env
        self.auth = source_class("app/core/security.py", "AuthBase", env)

        @asynccontextmanager
        async def transaction(db):
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        self.services = {}
        for side, name in (("client", "ClientAuthService"), ("backoffice", "BackofficeAuthService")):
            env = namespace()
            env.update(settings=self.settings, AuthBase=self.auth, transaction=transaction)
            self.services[side] = source_class(f"app/services/{side}/auth.py", name, env)

    def fixture(self, side):
        model = User if side == "client" else Admin
        account = model(id=17, email="synthetic@example.invalid", is_active=True)
        if side == "client":
            account.is_verified = True
        for field in ("first_name", "last_name", "university", "career_goal", "location"):
            setattr(account, field, "synthetic")
        return account, FakeDB(account)

    def login(self, side, db):
        return self.run_async(self.services[side].login(
            db, "synthetic@example.invalid", "synthetic-password"))

    def test_login_fingerprint_refresh_and_logout_both_sides(self):
        for side in self.services:
            with self.subTest(side=side):
                _, db = self.fixture(side)
                result = self.login(side, db)
                raw = result["refresh_token"]
                self.assertEqual(self.issued[raw]["scope"], side + "_refresh")
                self.assertEqual(db.rows[-1].token, self.auth.refresh_token_fingerprint(raw))
                access = self.run_async(self.services[side].refresh_token(db, raw))
                self.assertEqual(self.issued[access["access_token"]]["scope"], side)
                self.run_async(self.services[side].logout(db, raw))
                self.assertFalse(db.rows[-1].is_active)
                with self.assertRaises(APIException):
                    self.run_async(self.services[side].refresh_token(db, raw))
                # Refresh revocation does not revoke the previously issued access JWT.
                self.assertIsNotNone(self.auth.verify_token(access["access_token"], scope=side))

    def test_cross_scope_access_old_and_tampered_tokens_rejected(self):
        for side in self.services:
            with self.subTest(side=side):
                _, db = self.fixture(side)
                result = self.login(side, db)
                opposite = "backoffice_refresh" if side == "client" else "client_refresh"
                for raw in (result["access_token"], result["refresh_token"] + "tamper",
                            self.auth.create_refresh_token("17", scope="refresh"),
                            self.auth.create_refresh_token("17", scope=opposite),
                            self.auth.create_refresh_token("bad", scope=side + "_refresh")):
                    with self.assertRaises(APIException):
                        self.run_async(self.services[side].refresh_token(db, raw))

    def test_expired_jwt_and_expired_database_record_rejected(self):
        for side in self.services:
            _, db = self.fixture(side)
            result = self.login(side, db)
            raw = result["refresh_token"]
            db.rows[-1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
            with self.assertRaises(APIException):
                self.run_async(self.services[side].refresh_token(db, raw))
            expired = self.auth.create_refresh_token("17", scope=side + "_refresh",
                                                      expires_delta=timedelta(seconds=-1))
            with self.assertRaises(APIException):
                self.run_async(self.services[side].refresh_token(db, expired))

    def test_disabled_account_cannot_refresh(self):
        for side in self.services:
            account, db = self.fixture(side)
            raw = self.login(side, db)["refresh_token"]
            account.is_active = False
            with self.assertRaises(APIException):
                self.run_async(self.services[side].refresh_token(db, raw))

    def test_unverified_user_cannot_refresh(self):
        account, db = self.fixture("client")
        raw = self.login("client", db)["refresh_token"]
        account.is_verified = False
        with self.assertRaises(APIException):
            self.run_async(self.services["client"].refresh_token(db, raw))

    def test_wrong_password_is_rejected_by_login_control_flow(self):
        for side in self.services:
            _, db = self.fixture(side)
            with self.assertRaises(APIException):
                self.run_async(self.services[side].login(db, "synthetic@example.invalid", "wrong"))

    def test_relogin_invalidates_old_refresh_and_old_logout_is_harmless(self):
        for side in self.services:
            _, db = self.fixture(side)
            old = self.login(side, db)["refresh_token"]
            new = self.login(side, db)["refresh_token"]
            with self.assertRaises(APIException):
                self.run_async(self.services[side].refresh_token(db, old))
            self.run_async(self.services[side].logout(db, old))
            self.assertIsNotNone(self.run_async(self.services[side].refresh_token(db, new)))

    def test_old_and_repeated_logout_cannot_revoke_new_login(self):
        for side in self.services:
            _, db = self.fixture(side)
            old = self.login(side, db)["refresh_token"]
            self.run_async(self.services[side].logout(db, old))
            new = self.login(side, db)["refresh_token"]
            for raw in (old, old, "invalid"):
                self.run_async(self.services[side].logout(db, raw))
            self.assertIsNotNone(self.run_async(self.services[side].refresh_token(db, new)))
            self.assertEqual(sum(bool(getattr(r, "is_active", False)) for r in db.rows
                                 if type(r) in (Token, AdminToken)), 1)

    def test_identity_and_fingerprint_must_match_even_if_store_is_wrong(self):
        for side in self.services:
            _, db = self.fixture(side)
            raw = self.login(side, db)["refresh_token"]
            db.rows[-1].token = self.auth.refresh_token_fingerprint(raw + "different")
            with self.assertRaises(APIException):
                self.run_async(self.services[side].refresh_token(db, raw))

    def test_sha256_uses_token_suffix_beyond_72_bytes(self):
        a = self.auth.refresh_token_fingerprint("x" * 200 + "a")
        b = self.auth.refresh_token_fingerprint("x" * 200 + "b")
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, b)

    def test_password_methods_still_delegate_to_bcrypt_context(self):
        context = self.security_env["pwd_context"]
        self.auth.hash_token("synthetic")
        self.auth.verify_token_hash("synthetic", "synthetic-hash")
        context.hash.assert_called_once_with("synthetic")
        context.verify.assert_called_once_with("synthetic", "synthetic-hash")
        source = (ROOT / "app/core/security.py").read_text(encoding="utf-8")
        self.assertIn('CryptContext(schemes=["bcrypt"], deprecated="auto")', source)

    def test_settings_guard_with_isolated_settings_substitute(self):
        class BaseSettings:
            def __init__(self, **values):
                self.__dict__.update(values)

        settings = source_class("app/core/config.py", "Settings", {"BaseSettings": BaseSettings})
        for kwargs in ({}, {"SECRET_KEY": ""}, {"SECRET_KEY": " \t\n"}):
            with self.assertRaisesRegex(ValueError, "SECRET_KEY must be set") as error:
                settings(**kwargs)
            self.assertEqual(str(error.exception), "SECRET_KEY must be set and must not be blank")
        value = " synthetic-key-preserve-spaces "
        self.assertEqual(settings(SECRET_KEY=value).SECRET_KEY, value)

    def test_real_pydantic_settings_loading(self):
        if importlib.util.find_spec("pydantic_settings") is None:
            self.skipTest("pydantic-settings unavailable; real configuration loading NOT_RUN")
        from pydantic_settings import BaseSettings
        settings = source_class("app/core/config.py", "Settings", {"BaseSettings": BaseSettings})
        for value in ("", " \t"):
            with self.assertRaises(ValueError):
                settings(_env_file=None, SECRET_KEY=value)

    def test_real_jwt_crypto(self):
        if importlib.util.find_spec("jose") is None:
            self.skipTest("python-jose unavailable; real JWT signature testing NOT_RUN")
        from jose import jwt, JWTError
        env = namespace()
        env.update(settings=self.settings, uuid=uuid, jwt=jwt, JWTError=JWTError,
                   pwd_context=Mock())
        auth = source_class("app/core/security.py", "AuthBase", env)
        raw = auth.create_refresh_token("17", scope="client_refresh")
        self.assertEqual(auth.verify_token(raw, "client_refresh")["sub"], "17")

    def test_real_bcrypt_password_roundtrip(self):
        if importlib.util.find_spec("passlib") is None or importlib.util.find_spec("bcrypt") is None:
            self.skipTest("passlib/bcrypt unavailable; real password compatibility NOT_RUN")
        from passlib.context import CryptContext
        context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = context.hash("synthetic-password")
        self.assertTrue(context.verify("synthetic-password", hashed))
        self.assertFalse(context.verify("wrong-password", hashed))

"""A1 checks: real route factories/ASGI, with isolated business dependencies.

No application Settings, database, cloud client, lifespan, or real PDF parser is
loaded. Run this file alone; FastAPI/Starlette/SQLAlchemy must already exist.
"""

import ast
import asyncio
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel


BACKEND = Path(__file__).resolve().parents[1]


class Probe(BaseModel):
    ok: bool


class SecuritySurfaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        previous = Path.cwd()
        os.chdir(self.directory)
        self.stack.callback(os.chdir, previous)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

        # Create the Windows event loop's internal socketpair before blocking
        # connections; all application/test operations below stay blocked.
        self.runner = self.stack.enter_context(asyncio.Runner())
        self.network_attempts = []

        def block_network(*args, **kwargs):
            self.network_attempts.append(True)
            raise AssertionError("Network access is forbidden in A1 tests")

        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.create_connection",
                       "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=block_network))

        self.stack.enter_context(patch.dict(sys.modules))
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        for package in ("app", "app.core", "app.route", "app.configs",
                        "app.api", "app.api.client", "app.api.client.v1",
                        "app.api.backoffice", "app.api.backoffice.v1",
                        "app.services", "app.services.client", "app.services.common",
                        "app.models", "app.db", "app.common", "app.exceptions",
                        "app.schemas", "app.schedule"):
            module = self.stub(package)
            module.__path__ = [str(BACKEND.joinpath(*package.split(".")))]

        self.stub("app.core.config", settings=SimpleNamespace(
            ENV="development", API_V1_STR="/api/v1",
            PROJECT_NAME="A1 isolated fixture", ADMIN_EMAIL="a1@example.com"))
        self.stub("app.common.language", get_message=lambda text, language: text)
        self.load("app.exceptions.http_exceptions", "app/exceptions/http_exceptions.py")
        self.load("app.schemas.response", "app/schemas/response.py")
        self.registry = self.load("app.route.router_registry", "app/route/router_registry.py")
        for configs in self.registry.get_all_routes().values():
            for config in configs:
                router = APIRouter()

                async def fixture():
                    return {"ok": True}

                router.add_api_route("/__a1_fixture", fixture, response_model=Probe)
                self.stub(config.module_path, router=router)

        self.stub("app.core.log_config", setup_logging=lambda: None,
                  shutdown_logging=lambda: None, is_master_process=lambda: False)
        self.stub("app.services.common.redis", redis_client=SimpleNamespace(close=AsyncMock()))
        self.stub("app.services.common.thread_pool",
                  thread_pool_service=SimpleNamespace(shutdown=lambda: None))
        self.stub("app.db.base", close_db_engine=AsyncMock())
        self.stub("app.schedule.schedule", setup_scheduler=lambda app: None,
                  shutdown_scheduler=lambda: None)
        self.stub("app.common.log_consumer", consume_logs_forever=AsyncMock())
        self.docs = self.load("app.configs.docs_apps", "app/configs/docs_apps.py")
        self.routes = self.load("app.route.route", "app/route/route.py")
        self.app = self.routes.create_app()

    def tearDown(self):
        self.assertEqual(self.network_attempts, [], "Unexpected network attempt")

    @staticmethod
    def stub(name, **values):
        module = ModuleType(name)
        module.__dict__.update(values)
        sys.modules[name] = module
        return module

    @staticmethod
    def load(name, relative_path):
        spec = importlib.util.spec_from_file_location(name, BACKEND / relative_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    async def request(self, path):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"host", b"test.invalid")],
            "client": ("127.0.0.1", 1), "server": ("test.invalid", 80),
        }
        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages
                        if m["type"] == "http.response.body")
        return status, body

    def get(self, path):
        return self.runner.run(self.request(path))

    def resume_fixture(self):
        class Resume:
            def __init__(self, **values):
                self.__dict__.update(values)

        self.stub("app.models.resume", Resume=Resume)
        ai = SimpleNamespace(
            parse_resume=AsyncMock(return_value={"name": "Synthetic candidate"}),
            analyze_resume=AsyncMock(return_value={"overall_score": 7}))
        self.stub("app.services.client.ai_service", AIService=ai)
        module = self.load("app.services.client.resume_service",
                           "app/services/client/resume_service.py")
        rows = []

        def add(row):
            row.id = len(rows) + 1
            rows.append(row)

        db = SimpleNamespace(add=add, commit=AsyncMock(), refresh=AsyncMock())
        parser = patch.object(module.ResumeService, "_extract_pdf_text",
                              side_effect=lambda path: Path(path).read_bytes().decode())
        self.parser = self.stack.enter_context(parser)
        return module.ResumeService, db, rows, ai

    def upload(self, service, db, name="resume.pdf", content=b"synthetic PDF bytes"):
        return self.runner.run(service.upload_and_parse(
            db, user_id=71, file_content=content, file_name=name,
            target_position="Synthetic developer"))

    def test_deleted_aws_modules_and_sts_implementation(self):
        for side in ("client", "backoffice"):
            self.assertFalse((BACKEND / f"app/api/{side}/v1/aws.py").exists())
        tree = ast.parse((BACKEND / "app/utils/utils.py").read_text(encoding="utf-8"))
        names = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        self.assertNotIn("get_temporary_credentials", names)
        self.assertIn("S3Handler", names)
        self.assertIn("process_multiple_files", names)
        imports = {alias.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                   for alias in n.names}
        self.assertIn("boto3", imports)
        self.assertTrue(any(isinstance(n, ast.ImportFrom)
                            and n.module == "botocore.exceptions"
                            and any(a.name == "ClientError" for a in n.names)
                            for n in tree.body))
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr == "get_session_token"
                             for n in ast.walk(tree)))

    def test_no_aws_route_registration(self):
        for configs in self.registry.get_all_routes().values():
            self.assertTrue(all(not c.module_path.endswith(".aws") for c in configs))

    def test_four_aws_paths_return_404_in_real_asgi_routing(self):
        for path in ("/api/v1/aws/temporary-credentials",
                     "/api/v1/backoffice/aws/temporary-credentials",
                     "/client/api/v1/aws/temporary-credentials",
                     "/backoffice/api/v1/backoffice/aws/temporary-credentials"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_both_openapi_documents_generate_without_license_or_aws(self):
        for prefix in ("client", "backoffice"):
            with self.subTest(prefix=prefix):
                config = sys.modules[f"app.configs.{prefix}_swagger_config"]
                self.assertNotIn("license_info", getattr(config, f"{prefix.upper()}_OPENAPI_INFO"))
                status, body = self.get(f"/{prefix}/openapi.json")
                self.assertEqual(status, 200)
                document = json.loads(body)
                self.assertTrue(document["paths"])
                self.assertNotIn("license", document["info"])
                self.assertNotIn("temporary-credentials", body.decode())
                self.assertNotIn("MIT License", body.decode())
                self.assertFalse(any("aws" in t["name"] for t in document.get("tags", [])))
                self.assertEqual(self.get(f"/{prefix}/openapi.json"), (status, body))

    def test_old_resume_url_is_private_even_when_file_exists(self):
        path = Path("uploads/resumes/71_original.pdf")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"private synthetic resume")
        self.assertEqual(self.get("/uploads/resumes/71_original.pdf")[0], 404)

    def test_existing_avatar_url_remains_public(self):
        Path("uploads/avatars/71.png").write_bytes(b"synthetic avatar")
        self.assertEqual(self.get("/uploads/avatars/71.png"), (200, b"synthetic avatar"))

    def test_avatar_path_traversal_cannot_read_resume(self):
        path = Path("uploads/resumes/private.pdf")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"private synthetic resume")
        self.assertEqual(self.get("/uploads/avatars/../resumes/private.pdf")[0], 404)

    def test_upload_uses_uuid_pdf_and_preserves_display_name(self):
        service, db, rows, ai = self.resume_fixture()
        original = "../../outside/resume.pdf"
        result = self.upload(service, db, name=original)
        path = Path(rows[0].file_url)
        self.assertEqual(path.parent, Path("uploads/resumes"))
        self.assertEqual(path.suffix, ".pdf")
        self.assertEqual(UUID(path.stem).version, 4)
        self.assertEqual(rows[0].file_name, original)
        self.assertEqual(result["resume_id"], rows[0].id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(rows[0].status, "completed")
        self.assertEqual(json.loads(rows[0].parsed_content), {"name": "Synthetic candidate"})
        ai.parse_resume.assert_awaited_once_with("synthetic PDF bytes")

    def test_same_original_names_do_not_overwrite(self):
        service, db, rows, _ = self.resume_fixture()
        self.upload(service, db, content=b"first synthetic PDF")
        self.upload(service, db, content=b"second synthetic PDF")
        self.assertNotEqual(rows[0].file_url, rows[1].file_url)
        self.assertEqual(Path(rows[0].file_url).read_bytes(), b"first synthetic PDF")
        self.assertEqual(Path(rows[1].file_url).read_bytes(), b"second synthetic PDF")

    def test_parser_receives_the_saved_file_path(self):
        service, db, rows, _ = self.resume_fixture()
        self.upload(service, db)
        self.parser.assert_called_once_with(rows[0].file_url)
        self.assertTrue(Path(rows[0].file_url).is_file())
        self.assertEqual(db.commit.await_count, 2)

    def test_uuid_collision_does_not_overwrite_existing_file(self):
        service, db, rows, _ = self.resume_fixture()
        module = sys.modules[service.__module__]
        with patch.object(module, "uuid4", return_value=UUID(int=1)):
            self.upload(service, db, content=b"first")
            with self.assertRaises(FileExistsError):
                self.upload(service, db, content=b"second")
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0].file_url).read_bytes(), b"first")


if __name__ == "__main__":
    unittest.main()

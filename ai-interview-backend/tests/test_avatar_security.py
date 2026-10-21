"""Execute actual avatar function with real Pillow and synthetic images only."""
import ast
from io import BytesIO
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from _a2_test_support import ROOT, APIException, IsolatedCase

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image is not None, "Pillow unavailable; actual image checks NOT_RUN")
class AvatarTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        module = ModuleType("app.exceptions.http_exceptions")
        module.ValidationError = APIException
        self.stack.enter_context(patch.dict(sys.modules, {module.__name__: module}))
        tree = ast.parse((ROOT / "app/api/client/v1/auth.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "upload_avatar")
        node.decorator_list = []
        node.args.defaults = []
        for arg in node.args.args:
            arg.annotation = None
        node.returns = None
        Path("uploads/avatars").mkdir(parents=True)
        self.env = dict(os=os, AVATAR_DIR="uploads/avatars",
                        ApiResponse=SimpleNamespace(success=lambda data: data))
        exec(compile(ast.Module(body=[node], type_ignores=[]), "actual_upload_avatar", "exec"), self.env)
        self.user = SimpleNamespace(id=17, avatar="/uploads/avatars/old.png")
        self.db = SimpleNamespace(commit=AsyncMock())

    @staticmethod
    def image(format="PNG"):
        output = BytesIO()
        Image.new("RGB", (3, 3), "red").save(output, format=format)
        return output.getvalue()

    def upload(self, data, mime="image/png", name="../../unsafe.svg"):
        file = SimpleNamespace(filename=name, content_type=mime,
                               read=AsyncMock(side_effect=lambda limit=-1: data if limit < 0 else data[:limit]))
        return self.run_async(self.env["upload_avatar"](file, self.user, self.db))

    def test_png_and_jpeg_decode_reencode_and_server_extension(self):
        for format, mime, extension in (("PNG", "image/png", ".png"),
                                         ("JPEG", "image/jpeg", ".jpg")):
            result = self.upload(self.image(format), mime)
            self.assertTrue(result["avatar"].startswith("/uploads/avatars/17_"))
            path = Path(result["avatar"].lstrip("/"))
            self.assertEqual(path.suffix, extension)
            with Image.open(path) as image:
                self.assertEqual(image.format, format)
                image.load()
        self.assertEqual(self.db.commit.await_count, 2)

    def test_svg_html_and_text_disguised_as_png_are_rejected(self):
        for data in (b'<svg onload="void(0)"></svg>', b'<html>synthetic</html>', b'not an image'):
            with self.assertRaises(APIException):
                self.upload(data)
        self.db.commit.assert_not_awaited()
        self.assertEqual(list(Path("uploads/avatars").iterdir()), [])

    def test_svg_mime_and_gif_content_are_rejected(self):
        for data, mime in ((self.image(), "image/svg+xml"),
                           (self.image("GIF"), "image/png")):
            with self.assertRaises(APIException):
                self.upload(data, mime)
        self.db.commit.assert_not_awaited()

    def test_empty_oversize_and_truncated_images_are_rejected(self):
        for data in (b"", b"x" * (5 * 1024 * 1024 + 1), self.image()[:30]):
            with self.assertRaises(APIException):
                self.upload(data)
        self.db.commit.assert_not_awaited()

    def test_trailing_html_is_not_published(self):
        marker = b'<img src=x onerror="void(0)">'
        result = self.upload(self.image() + marker)
        self.assertNotIn(marker, Path(result["avatar"].lstrip("/")).read_bytes())

    def test_same_original_name_uses_different_files(self):
        first = self.upload(self.image())["avatar"]
        second = self.upload(self.image())["avatar"]
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first.lstrip("/")).is_file())
        self.assertTrue(Path(second.lstrip("/")).is_file())

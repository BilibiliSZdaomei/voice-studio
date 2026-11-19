"""HTTP boundary tests. Fake service here exercises transport, never synthesizes audio."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_studio.errors import AppError
from voice_studio.server import StudioServer


class TransportFixture:
    def __init__(self, root):
        self.config = {"data_root": str(root), "accepted_sample": str(root / "sample.wav")}
        self.calls = []

    def status(self):
        return {"engine": {"available": True}, "voice": {"name": "transport fixture"}}

    def list_projects(self):
        return []

    def get_project(self, pid):
        raise AppError(404, "项目不存在")

    def create_project(self, payload):
        self.calls.append(payload)
        return {"id": "fixture", **payload}


class HTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not root:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(root).mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="http-", dir=root)
        cls.root = Path(cls.temp.name)
        (cls.root / "web").mkdir()
        (cls.root / "web" / "index.html").write_text("<!doctype html><title>HTTP fixture</title>", encoding="utf-8")
        with wave.open(str(cls.root / "sample.wav"), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 2400)
        cls.service = TransportFixture(cls.root)
        cls.server = StudioServer(("127.0.0.1", 0), cls.service, cls.root / "web")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_status_and_security_headers(self):
        status, headers, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["csrf_token"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_workspace_html_is_inline_and_generated_html_downloads(self):
        code, headers, data = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertNotIn("Content-Disposition", headers)
        self.assertIn(b"HTTP fixture", data)
        (self.root / "composition.html").write_text("<title>generated</title>", encoding="utf-8")
        code, headers, _ = self.request("GET", "/files/composition.html")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_unconfigured_optional_sample_returns_not_found(self):
        with patch.dict(self.service.config, accepted_sample=None):
            self.assertEqual(self.request("GET", "/api/voice/sample")[0], 404)

    def test_write_requires_token(self):
        code, headers, _ = self.request("POST", "/api/projects", "{}", {"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        self.assertEqual(headers.get("Connection"), "close")

    def test_authenticated_write_and_unicode(self):
        payload = {"title": "中文稿件", "script": "测试文案。"}
        headers = {"Content-Type": "application/json", "X-Studio-Token": self.server.token}
        code, _, data = self.request("POST", "/api/projects", json.dumps(payload).encode(), headers)
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(data)["title"], payload["title"])

    def test_cross_origin_rejected(self):
        self.assertEqual(self.request("GET", "/api/status", headers={"Origin": "https://elsewhere.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "attacker.invalid"})[0], 403)

    def test_encoded_traversal_rejected(self):
        self.assertEqual(self.request("GET", "/files/%2e%2e/server.json")[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/sample.wav")[0], 404)

    def test_audio_byte_ranges(self):
        code, headers, data = self.request("GET", "/api/voice/sample", headers={"Range": "bytes=0-43"})
        self.assertEqual(code, 206)
        self.assertEqual(len(data), 44)
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertTrue(headers["Content-Range"].startswith("bytes 0-43/"))

    def test_range_error_and_head(self):
        self.assertEqual(self.request("GET", "/api/voice/sample", headers={"Range": "bytes=999999-"})[0], 416)
        code, headers, data = self.request("HEAD", "/api/voice/sample")
        self.assertEqual(code, 200)
        self.assertEqual(data, b"")
        self.assertGreater(int(headers["Content-Length"]), 44)

    def test_invalid_json_and_missing_project(self):
        headers = {"Content-Type": "application/json", "X-Studio-Token": self.server.token}
        self.assertEqual(self.request("POST", "/api/projects", "[]", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/projects", "{bad", headers)[0], 400)
        self.assertEqual(self.request("GET", "/api/projects/not-found")[0], 404)


if __name__ == "__main__":
    unittest.main()

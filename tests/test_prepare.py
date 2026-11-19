"""Local preparation invariants and the loopback model API contract, not model quality."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from voice_studio.errors import AppError
from voice_studio.prepare import RewriteClient, cleanup_manuscript, fact_changes, protected_tokens


class PreparationTests(unittest.TestCase):
    def test_markdown_cleanup_retains_facts_links_and_paragraphs(self):
        source = "# 项目进展\n\n- **DHL** 完成 45.7%。\n\n1. 检查 `UKWH02` 的 133 TO。\n[明细2026](https://example.test/2026)"
        result = cleanup_manuscript(source)
        self.assertNotIn("**", result)
        self.assertNotIn("# ", result)
        self.assertIn("1. 检查 UKWH02", result)
        self.assertIn("https://example.test/2026", result)
        self.assertEqual(fact_changes(source, result), [])

    def test_number_additions_deletions_and_acronym_changes_are_flagged(self):
        source = "DHL 在 2026-11-05 完成 45.7%，核对 133 TO。"
        self.assertTrue(fact_changes(source, source.replace("133", "134")))
        self.assertTrue(fact_changes(source, source + "共 2 次。"))
        self.assertTrue(fact_changes(source, source.replace("DHL", "UPS")))
        self.assertEqual(protected_tokens(source), protected_tokens(source + "请再检查。"))

    def test_unconfigured_and_remote_model_are_explicitly_unavailable(self):
        self.assertFalse(RewriteClient({}).availability()["available"])
        for url in ("https://api.example.test/v1", "http://192.168.1.1/v1", "http://localhost@evil.test/v1"):
            client = RewriteClient({"rewrite": {"enabled": True, "base_url": url, "model": "test"}})
            self.assertFalse(client.availability()["available"])
            with self.assertRaises(AppError):
                client.rewrite("测试原稿。")
        local = RewriteClient({"rewrite": {"enabled": True, "base_url": "http://127.0.0.1:1/v1", "model": "fixture", "max_input_chars": 600}})
        with self.assertRaises(AppError) as raised:
            local.rewrite("字" * 590, "要求" * 8)
        self.assertEqual(raised.exception.status, 413)

    def test_loopback_response_contract_and_redirect_rejection(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                if len(calls) == 2:
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/")
                    self.end_headers()
                    return
                data = json.dumps({"choices": [{"message": {"content": "DHL 完成 45.7%。"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RewriteClient({"rewrite": {"enabled": True, "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture"}})
            self.assertEqual(client.rewrite("DHL 完成 45.7%。"), "DHL 完成 45.7%。")
            self.assertEqual(calls[0][0], "/v1/chat/completions")
            self.assertFalse(calls[0][1]["stream"])
            with self.assertRaises(AppError):
                client.rewrite("DHL 完成 45.7%。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

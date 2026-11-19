"""Loopback-only HTTP application; no network services or API keys required."""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import signal
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import PROJECT_ROOT, load_config
from .errors import AppError
from .service import StudioService

MAX_BODY = 1024 * 1024
WEB_ROOT = PROJECT_ROOT / "apps" / "web"


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, web_root=WEB_ROOT):
        self.service = service
        self.token = secrets.token_urlsafe(32)
        self.web_root = Path(web_root).resolve()
        self.data_root = Path(service.config["data_root"]).resolve()
        super().__init__(address, StudioHandler)


class StudioHandler(BaseHTTPRequestHandler):
    server: StudioServer
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Browsers may close an idle keep-alive connection while navigating.
            pass

    def log_message(self, fmt, *args):
        # Never write user script contents to access logs.
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def _headers(self, code, content_type, length, extras=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extras or {}).items():
            self.send_header(key, str(value))
        self.end_headers()

    def _json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(data))
        if self.command != "HEAD":
            self.wfile.write(data)

    def _guard(self, mutation=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            raise AppError(403, "只允许通过本机地址访问")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            raise AppError(403, "拒绝来自其他网站的请求")
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            raise AppError(403, "请在工作台内执行此操作")
        if mutation and not secrets.compare_digest(self.headers.get("X-Studio-Token", ""), self.server.token):
            raise AppError(403, "页面验证已失效，请刷新工作台")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise AppError(400, "不支持此请求格式")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError(400, "请求长度无效")
        if not 0 <= size <= MAX_BODY:
            raise AppError(413, "文案过大，请分成多个项目")
        if size and self.headers.get_content_type() != "application/json":
            raise AppError(415, "请使用 JSON 请求")
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8-sig")) if size else {}
        except (ValueError, UnicodeError):
            raise AppError(400, "请求内容不是有效 JSON")
        if not isinstance(value, dict):
            raise AppError(400, "请求必须为 JSON 对象")
        return value

    def _import_zip(self):
        if self.headers.get("Transfer-Encoding"):
            raise AppError(400, "不支持分块工程上传")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError(400, "工程文件长度无效")
        if not 0 < size <= 64 * 1024 * 1024:
            raise AppError(413, "工程 ZIP 需要小于 64 MB")
        if self.headers.get_content_type() not in {"application/zip", "application/octet-stream"}:
            raise AppError(415, "请选择工程 ZIP 文件")
        folder = self.server.data_root / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + ".zip")
        try:
            remaining = size
            with path.open("xb") as output:
                while remaining:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise AppError(400, "工程文件上传不完整，原项目未改动")
                    output.write(block)
                    remaining -= len(block)
            return self.server.service.import_project(path)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _contained(root, relative):
        path = (root / relative).resolve()
        if path == root or not path.is_relative_to(root) or not path.is_file():
            raise AppError(404, "文件不存在")
        return path

    def _file(self, path):
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        start, end, code = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes"}
        if (path.suffix.lower() == ".html" and path.is_relative_to(self.server.data_root)
                and unquote(urlsplit(self.path).path).startswith("/files/")):
            extra["Content-Disposition"] = 'attachment; filename="composition.html"'
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self._headers(416, content_type, 0, {"Content-Range": f"bytes */{size}"})
                return
            left, right = match.groups()
            if not left:
                start = max(0, size - int(right))
            else:
                start = int(left)
                end = min(size - 1, int(right)) if right else size - 1
            if start > end or start >= size or (not left and int(right) == 0):
                self._headers(416, content_type, 0, {"Content-Range": f"bytes */{size}"})
                return
            code = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        count = max(0, end - start + 1)
        self._headers(code, content_type, count, extra)
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = count
            while remaining:
                block = stream.read(min(64 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _dispatch(self):
        mutation = self.command in {"POST", "PUT", "PATCH", "DELETE"}
        self._guard(mutation)
        path = unquote(urlsplit(self.path).path)
        service = self.server.service
        if self.command in {"GET", "HEAD"}:
            if path == "/api/status":
                result = service.status()
                result["csrf_token"] = self.server.token
                result["paths"] = {"project": str(PROJECT_ROOT), "data": str(self.server.data_root)}
                return self._json(result)
            if path == "/api/projects":
                return self._json({"projects": service.list_projects()})
            if path == "/api/jobs":
                return self._json({"jobs": service.list_jobs()})
            if path == "/api/voices":
                return self._json({"voices": service.list_voices()})
            if path == "/api/dictionary":
                return self._json(service.assets.list_dictionary())
            if match := re.fullmatch(r"/api/voices/([a-zA-Z0-9_-]+)/sample", path):
                return self._file(service.voice_sample_path(match[1]))
            if path == "/api/voice/sample":
                value = service.config.get("accepted_sample")
                if not isinstance(value, str) or not Path(value).is_file():
                    raise AppError(404, "参考样音尚未配置")
                return self._file(Path(value))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)", path):
                return self._json(service.get_project(match[1]))
            if match := re.fullmatch(r"/api/jobs/([a-zA-Z0-9_-]+)", path):
                return self._json(service.get_job(match[1]))
            if path.startswith("/api/"):
                raise AppError(404, "接口不存在")
            if path.startswith("/files/"):
                relative = path[len("/files/"):]
                if Path(relative).suffix.lower() not in {".wav", ".mp3", ".srt", ".zip", ".json", ".txt", ".mp4", ".html"}:
                    raise AppError(404, "文件不可访问")
                return self._file(self._contained(self.server.data_root, relative))
            return self._file(self._contained(self.server.web_root, "index.html" if path == "/" else path.lstrip("/")))
        if self.command == "POST" and path == "/api/projects/import":
            return self._json(self._import_zip(), 201)
        body = self._body()
        if self.command == "POST" and path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.command == "POST" and path == "/api/projects":
            return self._json(service.create_project(body), 201)
        if self.command == "POST" and path == "/api/batch":
            return self._json(service.create_batch(body), 201)
        if self.command == "POST" and path == "/api/voices":
            return self._json(service.save_voice(body), 201)
        if self.command == "PUT" and path == "/api/dictionary":
            return self._json(service.save_dictionary(body))
        if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)", path):
            if self.command == "PUT":
                return self._json(service.update_project(match[1], body))
        if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/segments/([a-zA-Z0-9_-]+)", path):
            if self.command == "PATCH":
                return self._json(service.update_segment(match[1], match[2], body))
        if self.command == "POST":
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/dictionary/preview", path):
                return self._json(service.dictionary_preview(match[1]))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/dictionary/apply", path):
                return self._json(service.apply_project_dictionary(match[1], body))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/prepare", path):
                return self._json(service.prepare_manuscript(match[1], body))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/proposals/([a-zA-Z0-9_-]+)/apply", path):
                return self._json(service.apply_proposal(match[1], match[2], body))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/proposals/([a-zA-Z0-9_-]+)/undo", path):
                return self._json(service.undo_proposal(match[1], match[2], body))
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/video", path):
                return self._json(service.enqueue_video(match[1], body), 202)
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/generate", path):
                return self._json(service.generate(match[1]), 202)
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/segments/([a-zA-Z0-9_-]+)/generate", path):
                return self._json(service.generate(match[1], segment_id=match[2]), 202)
            if match := re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/export", path):
                return self._json(service.export_project(match[1]))
            if match := re.fullmatch(r"/api/jobs/([a-zA-Z0-9_-]+)/cancel", path):
                return self._json(service.cancel_job(match[1]))
        raise AppError(404, "接口不存在")

    def _handle(self):
        try:
            self._dispatch()
        except AppError as error:
            # Rejections can precede body consumption. Never reuse that connection.
            self.close_connection = True
            self._json({"error": error.message}, error.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as error:
            # Detailed exception stays in local process logs, never in public responses.
            print(f"SERVER_ERROR {type(error).__name__}: {error}", flush=True)
            self._json({"error": "操作未完成，请查看本地服务日志"}, 500)

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main():
    parser = argparse.ArgumentParser(description="声稿：本地声音内容工作台")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--config")
    args = parser.parse_args()
    config = load_config(args.config)
    service = StudioService(config)
    server = StudioServer(("127.0.0.1", args.port), service)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    print(f"VOICE_STUDIO_READY http://127.0.0.1:{server.server_port}", flush=True)
    print(f"DATA_ROOT {config['data_root']}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()

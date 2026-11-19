"""Reusable local API client. Uses the same project and generation flow as the UI."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LocalClient:
    def __init__(self, url):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.username or parts.password:
            raise ValueError("此客户端只连接本机声稿服务")
        self.url = url.rstrip("/")
        self.token = self.request("GET", "/api/status")["csrf_token"]

    def request(self, method, path, payload=None):
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["X-Studio-Token"] = self.token
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.load(error).get("error", str(error))
            except (ValueError, UnicodeError):
                message = str(error)
            error.close()
            raise RuntimeError(message) from error

    def restore(self, path):
        path = Path(path)
        if not 0 < path.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError("工程 ZIP 需要小于 64 MB")
        request = Request(self.url + "/api/projects/import", data=path.read_bytes(),
                          headers={"Content-Type": "application/zip", "X-Studio-Token": self.token}, method="POST")
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.load(error).get("error", str(error))
            finally:
                error.close()
            raise RuntimeError(message) from error

    def wait(self, job):
        previous = None
        while job["status"] in {"queued", "running", "cancelling"}:
            progress = (job["status"], job["completed"], job["total"])
            if progress != previous:
                print(f"{job['status']} {job['completed']}/{job['total']}", file=sys.stderr)
                previous = progress
            time.sleep(1)
            job = self.request("GET", "/api/jobs/" + job["id"])
        if job["status"] != "completed":
            raise RuntimeError(job.get("error") or f"任务状态：{job['status']}")
        return job


def main():
    parser = argparse.ArgumentParser(description="声稿本地命令行接口")
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("projects", help="列出项目")
    sub.add_parser("jobs", help="列出队列与近期任务")
    sub.add_parser("voices", help="列出本地声线档案")
    batch = sub.add_parser("batch", help="批量导入 UTF-8 稿件，可串行排队生成")
    batch.add_argument("files", nargs="+", type=Path)
    batch.add_argument("--generate", action="store_true")
    batch.add_argument("--wait", action="store_true")
    restore = sub.add_parser("restore", help="从工程 ZIP 恢复为新项目")
    restore.add_argument("file", type=Path)
    video = sub.add_parser("video", help="从当前配音导出字幕讲解视频")
    video.add_argument("project_id")
    video.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    video.add_argument("--layout", choices=["virtual_host", "captions"], default="virtual_host")
    video.add_argument("--wait", action="store_true")
    prepare = sub.add_parser("prepare", help="生成待审阅的稿件整理或本地模型改写建议")
    prepare.add_argument("project_id")
    prepare.add_argument("--mode", choices=["cleanup", "rewrite"], default="cleanup")
    prepare.add_argument("--instruction", default="")
    adopt = sub.add_parser("apply-proposal", help="明确确认后采用建议稿")
    adopt.add_argument("project_id")
    adopt.add_argument("proposal_id")
    adopt.add_argument("--confirm", action="store_true", required=True)
    narration = sub.add_parser("narrate", help="导入 UTF-8 文案并生成配音")
    narration.add_argument("file", type=Path)
    narration.add_argument("--title")
    narration.add_argument("--wait", action="store_true")
    narration.add_argument("--export", action="store_true", dest="export_audio")
    generate = sub.add_parser("generate", help="继续生成项目未完成段落")
    generate.add_argument("project_id")
    generate.add_argument("--wait", action="store_true")
    export = sub.add_parser("export", help="导出已完成项目")
    export.add_argument("project_id")
    job = sub.add_parser("job", help="查看任务")
    job.add_argument("job_id")
    args = parser.parse_args()
    client = LocalClient(args.url)
    if args.command == "projects":
        result = client.request("GET", "/api/projects")
    elif args.command == "jobs":
        result = client.request("GET", "/api/jobs")
    elif args.command == "voices":
        result = client.request("GET", "/api/voices")
    elif args.command == "restore":
        result = client.restore(args.file)
    elif args.command == "batch":
        if args.wait and not args.generate:
            parser.error("--wait 需要同时指定 --generate")
        values = [{"title": path.stem[:100], "script": path.read_text(encoding="utf-8-sig")} for path in args.files]
        result = client.request("POST", "/api/batch", {"projects": values, "generate": args.generate})
        if args.wait:
            result["jobs"] = [client.wait(job) for job in result["jobs"]]
    elif args.command in {"video", "prepare", "apply-proposal"}:
        project = client.request("GET", "/api/projects/" + args.project_id)
        payload = {"expected_revision": project["revision"]}
        if args.command == "video":
            payload["aspect_ratio"] = args.aspect
            payload["layout"] = args.layout
            if args.layout == "virtual_host":
                payload["avatar_id"] = "studio-bot"
            result = client.request("POST", f"/api/projects/{args.project_id}/video", payload)
            if args.wait:
                result = client.wait(result)
        elif args.command == "prepare":
            payload.update(mode=args.mode, instruction=args.instruction)
            result = client.request("POST", f"/api/projects/{args.project_id}/prepare", payload)
        else:
            payload["confirmed"] = True
            result = client.request("POST", f"/api/projects/{args.project_id}/proposals/{args.proposal_id}/apply", payload)
    elif args.command == "job":
        result = client.request("GET", "/api/jobs/" + args.job_id)
    elif args.command == "export":
        result = client.request("POST", f"/api/projects/{args.project_id}/export")
    else:
        if args.command == "narrate":
            if args.export_audio and not args.wait:
                parser.error("--export 需要同时指定 --wait")
            project = client.request("POST", "/api/projects", {
                "title": args.title or args.file.stem,
                "script": args.file.read_text(encoding="utf-8-sig"),
            })
            project_id = project["id"]
        else:
            project_id = args.project_id
        job = client.request("POST", f"/api/projects/{project_id}/generate")
        if args.wait:
            job = client.wait(job)
        result = {"project_id": project_id, "job": job}
        if args.command == "narrate" and args.export_audio:
            result["export"] = client.request("POST", f"/api/projects/{project_id}/export")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, URLError, RuntimeError, ValueError) as error:
        print(f"声稿：{error}", file=sys.stderr)
        sys.exit(1)

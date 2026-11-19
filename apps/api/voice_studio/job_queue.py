"""One persistent FIFO for speech and video; cancellations never affect another project."""
from __future__ import annotations

import copy
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

from .errors import AppError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class JobQueueMixin:
    def _init_queue(self):
        self._queue = deque()
        queued = []
        for path in (self.root / "jobs").glob("*.json"):
            job = self._read_job(path.stem)
            if job["status"] == "queued":
                queued.append(job)
        self._queue.extend(job["id"] for job in sorted(queued, key=lambda item: item["created_at"]))
        self._start_queue()

    def _start_queue(self):
        if self._queue and not self._closed and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._queue_worker, daemon=True, name="studio-production")
            self._thread.start()

    def _queue_worker(self):
        while True:
            with self.lock:
                if self._closed or not self._queue:
                    self._thread = None
                    return
                jid = self._queue.popleft()
                job = self._read_job(jid)
                if job["status"] != "queued":
                    continue
                self._active_id = jid
                self._cancel.clear()
            if job.get("kind", "speech") == "video":
                self._run_video_job(jid)
            else:
                self._run_job(jid)

    def list_jobs(self):
        with self.lock:
            result = [self._public_job(self._read_job(path.stem)) for path in (self.root / "jobs").glob("*.json")]
            return sorted(result, key=lambda item: item["created_at"], reverse=True)[:100]

    def _busy_project(self, pid):
        ids = list(self._queue)
        if self._active_id:
            ids.append(self._active_id)
        return any((job := self._read_job(jid))["project_id"] == pid and job["status"] in {"queued", "running"} for jid in ids)

    def _enqueue_job(self, job):
        if len(self._queue) >= 100:
            raise AppError(409, "队列已满，请等待部分任务完成")
        self._write_job(job)
        self._queue.append(job["id"])
        self._start_queue()

    def cancel_job(self, jid):
        with self.lock:
            job = self._read_job(jid)
            if job["status"] not in {"queued", "running"}:
                return self._public_job(job)
            if self._active_id == jid:
                self._cancel.set()
                process = self._process
                if process and process.poll() is None:
                    process.terminate()
                return self._public_job(job)
            if jid not in self._queue:
                raise AppError(409, "任务状态已变化，请重新载入")
            self._queue.remove(jid)
            if job.get("kind", "speech") == "speech":
                project = self._read(job["project_id"])
                for segment in project["segments"]:
                    original = job.get("originals", {}).get(segment["id"])
                    if original is not None:
                        segment.clear()
                        segment.update(original)
                self._save(project)
            job["status"] = "cancelled"
            job["error"] = None
            self._write_job(job)
            return self._public_job(job)

    def enqueue_video(self, pid, payload):
        with self.lock:
            if self._closed:
                raise AppError(503, "服务正在停止")
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            if not project.get("export") or any(segment["status"] != "ready" for segment in project["segments"]):
                raise AppError(409, "请先完成全部配音并导出当前音频")
            availability = self.video_exporter.availability()
            if not availability["available"]:
                raise AppError(503, availability["message"])
            ratio = payload.get("aspect_ratio", "16:9")
            if not isinstance(ratio, str) or ratio not in {"16:9", "9:16"}:
                raise AppError(400, "视频比例仅支持 16:9 或 9:16")
            layout, avatar_id = self.video_exporter.presentation(payload)
            job = {"id": uuid.uuid4().hex, "project_id": pid, "kind": "video", "status": "queued",
                   "total": 1, "completed": 0, "error": None, "created_at": timestamp(),
                   "updated_at": timestamp(), "project_snapshot": copy.deepcopy(project),
                   "export_snapshot": copy.deepcopy(project["export"]),
                   "options": {"aspect_ratio": ratio, "layout": layout, "avatar_id": avatar_id}}
            self._enqueue_job(job)
            return self._public_job(job)

    def _run_video_job(self, jid):
        result, failure = None, None
        try:
            with self.lock:
                job = self._read_job(jid)
                job["status"] = "running"
                self._write_job(job)
            def progress(*args, **kwargs):
                # A stage note is factual; no invented percentage or time estimate.
                note = str(args[0])[:160] if args else "正在渲染视频"
                with self.lock:
                    current = self._read_job(jid)
                    current["message"] = note
                    self._write_job(current)
            if not self._cancel.is_set():
                result = self.video_exporter.render(job["project_snapshot"], job["export_snapshot"],
                                                    job["options"], on_progress=progress, cancel_event=self._cancel)
        except Exception as error:
            failure = str(error)[:1000]
        finally:
            with self.lock:
                job = self._read_job(jid)
                if self._cancel.is_set():
                    job["status"], job["error"] = "cancelled", None
                elif failure or result is None:
                    job["status"], job["error"] = "failed", failure or "视频未生成"
                else:
                    project = self._read(job["project_id"])
                    if project["revision"] != job["project_snapshot"]["revision"]:
                        job["status"], job["error"] = "failed", "配音版本已变化，视频未登记到当前项目"
                    else:
                        project["video"] = result
                        self._save(project)
                        job.update(status="completed", completed=1, result=result, error=None)
                self._write_job(job)
                self._active_id = None

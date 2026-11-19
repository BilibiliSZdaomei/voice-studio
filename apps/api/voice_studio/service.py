"""Persistent projects, single local inference worker and versioned exports."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import threading
import uuid
import wave
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from .errors import AppError
from . import __version__
from .data_lock import DataRootLock
from .assets import AssetStore, PronunciationDictionary
from .config import voice_resource_checks
from .prepare import RewriteClient
from .production import ProductionMixin
from .job_queue import JobQueueMixin
from .video import VideoExporter
from .text import MAX_SEGMENT, clean_script, settings_value, split_sentences, title_text

ACTIVE = {"queued", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def new_segment(text):
    return {"id": uuid.uuid4().hex, "text": text, "revision": 1, "status": "pending",
            "audio_url": None, "duration_seconds": None, "error": None}


class StudioService(ProductionMixin, JobQueueMixin):
    _write_json = staticmethod(atomic_json)

    def __init__(self, config):
        self.config = dict(config)
        self.root = Path(config["data_root"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._data_lock = DataRootLock(self.root)
        self.lock = threading.RLock()
        self._active_id = None
        self._process = None
        self._thread = None
        self._cancel = threading.Event()
        self._closed = False
        self._queue = deque()
        self.assets = AssetStore(self.root, self.config)
        self.rewriter = RewriteClient(self.config)
        self.video_exporter = VideoExporter(self.config, self.root)
        try:
            self._recover()
            self._init_queue()
        except Exception:
            self._data_lock.close()
            raise

    def _project_path(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{32}", pid):
            raise AppError(404, "项目不存在")
        return self.root / "projects" / pid / "project.json"

    def _read(self, pid):
        path = self._project_path(pid)
        if not path.is_file():
            raise AppError(404, "项目不存在")
        try:
            return self._project_defaults(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as error:
            raise AppError(500, "项目文件无法读取，原文件已保留") from error

    def _save(self, project, bump=True):
        if bump:
            project["revision"] += 1
            project["updated_at"] = now()
        atomic_json(self._project_path(project["id"]), project)

    def _url(self, path):
        return "/files/" + quote(path.resolve().relative_to(self.root).as_posix(), safe="/")

    def _asset(self, url):
        if not isinstance(url, str) or not url.startswith("/files/"):
            raise AppError(409, "配音文件尚未生成")
        path = (self.root / unquote(url[len("/files/"):])).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise AppError(409, "配音文件缺失，请重新生成相关片段")
        return path

    def _job_path(self, jid):
        if not isinstance(jid, str) or not re.fullmatch(r"[a-f0-9]{32}", jid):
            raise AppError(404, "任务不存在")
        return self.root / "jobs" / (jid + ".json")

    def _read_job(self, jid):
        path = self._job_path(jid)
        if not path.is_file():
            raise AppError(404, "任务不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_job(self, job):
        job["updated_at"] = now()
        atomic_json(self._job_path(job["id"]), job)

    @staticmethod
    def _public_job(job):
        result = {key: copy.deepcopy(value) for key, value in job.items()
                  if key not in {"originals", "target_ids", "completed_ids", "project_snapshot", "export_snapshot"}}
        result.setdefault("kind", "speech")
        return result

    def _recover(self):
        for path in (self.root / "jobs").glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["status"] == "running":
                job["status"] = "failed"
                job["error"] = "服务已重启；已完成片段保留，可以继续生成"
                self._write_job(job)
        for path in (self.root / "projects").glob("*/project.json"):
            project = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for segment in project["segments"]:
                if segment["status"] in {"queued", "generating"}:
                    try:
                        self._asset(segment["audio_url"])
                        segment["status"] = "ready"
                    except AppError:
                        segment["status"] = "pending"
                        segment["audio_url"] = None
                        segment["duration_seconds"] = None
                    segment["error"] = None
                    changed = True
            if changed:
                self._save(project)

    def status(self):
        checks = voice_resource_checks(self.config)
        missing = [key for key, item in checks.items() if not item["exists"]]
        voice = self.assets.get_voice("default")
        with self.lock:
            job = self._read_job(self._active_id) if self._active_id else None
            return {"app_version": __version__, "engine": {"available": not missing,
                    "model_available": all(key in {"reference_audio", "reference_text"} for key in missing),
                    "name": "ZipVoice 原始模型", "processing": "local_cpu",
                    "message": "本地模型与参考声线已配置" if not missing else "缺少资源：" + "、".join(missing)},
                    "voice": {"name": voice["name"], "sample_url": "/api/voice/sample" if voice["sample_audio"] else None,
                    "acceptance_scope": voice["acceptance_scope"]},
                    "active_job": self._public_job(job) if job and job["status"] in ACTIVE else None,
                    "queued_count": len(self._queue), "features": {"rewrite": self.rewriter.availability(),
                    "video": self.video_exporter.availability()}}

    def list_projects(self):
        with self.lock:
            projects = []
            for path in (self.root / "projects").glob("*/project.json"):
                project = self._read(path.parent.name)
                projects.append({key: project[key] for key in ("id", "title", "created_at", "updated_at", "revision")} |
                                {"segment_count": len(project["segments"]),
                                 "ready_count": sum(segment["status"] == "ready" for segment in project["segments"])})
            return sorted(projects, key=lambda item: item["updated_at"], reverse=True)

    def get_project(self, pid):
        with self.lock:
            return self._read(pid)

    def create_project(self, payload):
        with self.lock:
            project = self._new_project(payload)
            self._save(project, False)
            return project

    def _new_project(self, payload):
        """Construct and validate a project before writing or enqueueing any batch items."""
        script = clean_script(payload.get("script"))
        title = title_text(payload.get("title"))
        project = {"id": uuid.uuid4().hex, "title": title, "script": script, "revision": 1,
                   "created_at": now(), "updated_at": now(), "settings": settings_value(payload.get("settings", {})),
                   "segments": [new_segment(text) for text in split_sentences(script)], "export": None}
        self._initialize_project(project, payload)
        return project

    def _editable(self, pid):
        if self._busy_project(pid):
            raise AppError(409, "项目已排队或正在生成，请完成或取消任务后再修改")

    @staticmethod
    def _revision(project, payload):
        expected = payload.get("expected_revision")
        if type(expected) is not int or expected != project["revision"]:
            raise AppError(409, "项目已更新，请先重新载入，避免覆盖其他修改")

    def update_project(self, pid, payload):
        with self.lock:
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            if "title" in payload:
                title = title_text(payload["title"])
                if title != project["title"]:
                    project["video"] = None
                project["title"] = title
            changed = False
            if "script" in payload:
                script = clean_script(payload["script"])
                if script != project["script"]:
                    old = defaultdict(deque)
                    for segment in project["segments"]:
                        old[segment["text"]].append(segment)
                    project["segments"] = [old[text].popleft() if old[text] else new_segment(text)
                                           for text in split_sentences(script)]
                    project["script"] = script
                    changed = True
            if "voice_id" in payload:
                voice = self.assets.get_voice(payload["voice_id"])
                if voice != project["voice_snapshot"]:
                    project["voice_id"], project["voice_snapshot"] = voice["id"], voice
                    for segment in project["segments"]:
                        self._invalidate(segment)
                    changed = True
            if "settings" in payload:
                settings = settings_value(payload["settings"], project["settings"])
                if settings["speed"] != project["settings"]["speed"]:
                    for segment in project["segments"]:
                        segment.update(status="pending", audio_url=None, duration_seconds=None, error=None)
                        segment["revision"] += 1
                changed = changed or settings != project["settings"]
                project["settings"] = settings
            dictionary = PronunciationDictionary.for_project(project)
            for segment in project["segments"]:
                spoken = dictionary.segment(segment["text"])["spoken_text"]
                if spoken != segment.get("spoken_text", segment["text"]):
                    self._invalidate(segment)
                segment["spoken_text"] = spoken
            if changed:
                project["export"] = project["video"] = None
            self._save(project)
            return project

    def update_segment(self, pid, sid, payload):
        text = clean_script(payload.get("text"))
        if len(text) > MAX_SEGMENT:
            raise AppError(400, f"每个片段最多 {MAX_SEGMENT} 字；较长文案请在整稿中编辑")
        with self.lock:
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            segment = next((item for item in project["segments"] if item["id"] == sid), None)
            if not segment:
                raise AppError(404, "片段不存在")
            if segment["text"] != text:
                segment.update(text=text, status="pending", audio_url=None, duration_seconds=None, error=None)
                segment["spoken_text"] = PronunciationDictionary.for_project(project).segment(text)["spoken_text"]
                segment["revision"] += 1
                project["script"] = "\n".join(item["text"] for item in project["segments"])
                project["export"] = project["video"] = None
            self._save(project)
            return project

    def get_job(self, jid):
        with self.lock:
            return self._public_job(self._read_job(jid))

    def _speech_config(self, voice):
        return {**self.config, "reference_audio": voice.get("reference_audio"),
                "reference_text": voice.get("reference_text")}

    def _require_speech_ready(self, project):
        voice = project["voice_snapshot"]
        if (project["voice_id"] == "default" and not voice.get("reference_audio")
                and not any(segment.get("engine") or segment.get("audio_url") for segment in project["segments"])):
            # Drafts can predate this computer's first voice setup.
            voice = self.assets.get_voice("default")
        checks = voice_resource_checks(self._speech_config(voice))
        missing = [key for key, item in checks.items() if not item["exists"]]
        if missing:
            raise AppError(503, "当前项目配音缺少资源：" + "、".join(missing))
        project["voice_snapshot"] = voice

    def generate(self, pid, segment_id=None):
        with self.lock:
            if self._closed:
                raise AppError(503, "服务正在停止")
            project = self._read(pid)
            self._editable(pid)
            if len(self._queue) >= 100:
                raise AppError(409, "队列已满，请等待部分任务完成")
            self._require_speech_ready(project)
            return self._queue_speech(project, segment_id)

    def _queue_speech(self, project, segment_id=None):
        targets = [segment for segment in project["segments"] if segment["id"] == segment_id] if segment_id else [
            segment for segment in project["segments"] if segment["status"] != "ready"]
        if segment_id and not targets:
            raise AppError(404, "片段不存在")
        jid = uuid.uuid4().hex
        job = {"id": jid, "project_id": project["id"], "kind": "speech", "status": "queued" if targets else "completed",
               "total": len(targets), "completed": 0, "error": None, "created_at": now(), "updated_at": now(),
               "target_ids": [segment["id"] for segment in targets], "completed_ids": [],
               "originals": {segment["id"]: copy.deepcopy(segment) for segment in targets}}
        if not targets:
            self._write_job(job)
            return self._public_job(job)
        for segment in targets:
            segment["status"] = "queued"
            segment["error"] = None
        project["export"] = project["video"] = None
        self._save(project)
        self._enqueue_job(job)
        return self._public_job(job)

    def cancel_job(self, jid):
        return JobQueueMixin.cancel_job(self, jid)

    def _run_job(self, jid):
        failure = None
        try:
            with self.lock:
                job = self._read_job(jid)
                project = self._read(job["project_id"])
                task_root = self.root / "tasks" / jid
                task_root.mkdir(parents=True, exist_ok=True)
                targets = []
                for segment in project["segments"]:
                    if segment["id"] in job["target_ids"]:
                        output = self.root / "projects" / project["id"] / "audio" / (uuid.uuid4().hex + ".wav")
                        targets.append({"id": segment["id"], "text": segment.get("spoken_text", segment["text"]), "output": str(output)})
                voice = project["voice_snapshot"]
                runtime_config = self._speech_config(voice)
                if voice.get("reference_sha256") and hashlib.sha256(Path(voice["reference_audio"]).read_bytes()).hexdigest() != voice["reference_sha256"]:
                    raise AppError(409, "参考录音已被外部修改，请重新登记声线后再生成")
                request = {"config": runtime_config, "settings": project["settings"], "segments": targets}
                atomic_json(task_root / "request.json", request)
                expected = {item["id"]: Path(item["output"]) for item in targets}
                job["status"] = "running"
                self._write_job(job)
                if self._cancel.is_set():
                    return
                stderr = (task_root / "worker.stderr.log").open("w", encoding="utf-8")
                try:
                    self._process = subprocess.Popen([
                        self.config["runtime_python"], "-B", "-X", "utf8", str(Path(__file__).with_name("worker.py")),
                        "--request", str(task_root / "request.json")],
                        stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8", errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    stderr.close()
                    raise
                process = self._process
            try:
                with (task_root / "worker.stdout.log").open("w", encoding="utf-8") as log:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        with self.lock:
                            if self._cancel.is_set():
                                break
                            job = self._read_job(jid)
                            project = self._read(job["project_id"])
                            sid = event.get("id")
                            segment = next((item for item in project["segments"] if item["id"] == sid), None)
                            if event.get("event") == "error":
                                failure = str(event.get("message", "本地推理未完成"))[:1000]
                            if not segment or sid not in expected:
                                continue
                            if event.get("event") == "segment_start":
                                segment["status"] = "generating"
                                self._save(project)
                            elif event.get("event") == "segment_ready":
                                path = expected[sid]
                                if Path(event.get("path", "")).resolve() != path.resolve():
                                    raise RuntimeError("推理返回了非预期的文件路径")
                                with wave.open(str(path), "rb") as audio:
                                    if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getnframes() <= 0:
                                        raise RuntimeError("推理输出格式无效")
                                    duration = audio.getnframes() / audio.getframerate()
                                segment.update(status="ready", audio_url=self._url(path), duration_seconds=duration, error=None)
                                segment["engine"] = {"name": "ZipVoice original", "steps": 16, "guidance_scale": 1.0,
                                                     "speed": project["settings"]["speed"], "sample_rate": event.get("sample_rate"),
                                                     "peak": event.get("peak"), "rms": event.get("rms"), "generated_at": now()}
                                segment["engine"].update(voice_id=project["voice_id"], voice_revision=voice["revision"],
                                                         dictionary_revision=project["dictionary_snapshot"]["revision"],
                                                         spoken_text=segment.get("spoken_text", segment["text"]))
                                self._save(project)
                                if sid not in job["completed_ids"]:
                                    job["completed_ids"].append(sid)
                                job["completed"] = len(job["completed_ids"])
                                self._write_job(job)
                if self._cancel.is_set() and process.poll() is None:
                    process.terminate()
                code = process.wait(timeout=30)
                if code and not self._cancel.is_set():
                    failure = failure or "本地语音进程退出，请检查任务日志并重新生成"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
                stderr.close()
        except Exception as error:
            failure = str(error)[:1000]
        finally:
            with self.lock:
                job = self._read_job(jid)
                project = self._read(job["project_id"])
                cancelled = self._cancel.is_set()
                if not cancelled and job["completed"] != job["total"]:
                    failure = failure or "任务未生成全部片段，可继续生成未完成部分"
                for segment in project["segments"]:
                    if segment["id"] in job["target_ids"] and segment["id"] not in job["completed_ids"]:
                        original = job["originals"][segment["id"]]
                        segment.clear()
                        segment.update(original)
                        if original["status"] != "ready":
                            segment["status"] = "pending" if cancelled else "error"
                            segment["error"] = None if cancelled else failure
                self._save(project)
                job["status"] = "cancelled" if cancelled else "failed" if failure else "completed"
                job["error"] = None if cancelled else failure
                self._write_job(job)
                self._process = None
                self._active_id = None

    @staticmethod
    def _srt_time(seconds):
        milliseconds = round(seconds * 1000)
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

    def export_project(self, pid):
        with self.lock:
            if self._closed:
                raise AppError(503, "服务正在停止")
            project = self._read(pid)
            self._editable(pid)
            if not project["segments"] or any(item["status"] != "ready" for item in project["segments"]):
                raise AppError(409, "请先生成全部片段，再导出完整配音")
            audio_paths = [self._asset(item["audio_url"]) for item in project["segments"]]
            source_revision = project["revision"]
            folder = self.root / "exports" / pid / uuid.uuid4().hex

        # Audio versions are immutable; export this snapshot without blocking
        # inference progress, cancellation or edits to other projects.
        folder.mkdir(parents=True)
        wav = folder / "narration.wav"
        srt = folder / "subtitles.srt"
        rate = None
        timeline = []
        position = 0
        with wave.open(str(wav), "wb") as output:
            for index, (segment, path) in enumerate(zip(project["segments"], audio_paths)):
                with wave.open(str(path), "rb") as source:
                    if source.getnchannels() != 1 or source.getsampwidth() != 2:
                        raise AppError(409, "音频格式不一致，请重新生成相关片段")
                    if rate is None:
                        rate = source.getframerate()
                        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                    if source.getframerate() != rate:
                        raise AppError(409, "片段采样率不一致，请重新生成")
                    if index:
                        silence = round(project["settings"]["pause_seconds"] * rate)
                        output.writeframesraw(b"\0\0" * silence)
                        position += silence
                    start = position / rate
                    while block := source.readframes(65536):
                        output.writeframesraw(block)
                        position += len(block) // 2
                    timeline.append({"id": segment["id"], "start_seconds": start,
                                     "end_seconds": position / rate, "text": segment["text"]})
        srt.write_text("\n\n".join(f"{index}\n{self._srt_time(item['start_seconds'])} --> {self._srt_time(item['end_seconds'])}\n{item['text']}"
                                       for index, item in enumerate(timeline, 1)) + "\n", encoding="utf-8")
        (folder / "script.txt").write_text(project["script"], encoding="utf-8")
        mp3 = None
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            mp3 = folder / "narration.mp3"
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-i", str(wav),
                                     "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)],
                                    capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise AppError(500, "MP3 转换未完成，WAV 已保留；请检查 ffmpeg")
        exported = {"wav_url": self._url(wav), "mp3_url": self._url(mp3) if mp3 else None,
                    "srt_url": self._url(srt), "bundle_url": self._url(folder / "project.zip"),
                    "duration_seconds": position / rate, "created_at": now(), "source_revision": source_revision}
        project["export"] = exported
        voice = project["voice_snapshot"]
        portable_project = {**project, "voice_snapshot": {"id": voice["id"], "revision": voice["revision"]}}
        atomic_json(folder / "project.json", portable_project)
        atomic_json(folder / "manifest.json", {"app_version": __version__, "subtitle_alignment": "segment_level",
                    "timeline": timeline, "voice_acceptance": "new_script_needs_user_listening",
                    "external_requirements": "Original voice profile and model runtime are not included; this ZIP is an archive, not an importable installer."})
        with zipfile.ZipFile(folder / "project.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in (wav, srt, folder / "script.txt", folder / "project.json", folder / "manifest.json"):
                archive.write(path, path.name)
            if mp3:
                archive.write(mp3, mp3.name)
            for index, path in enumerate(audio_paths, 1):
                archive.write(path, f"segments/{index:04}.wav")

        with self.lock:
            if self._closed:
                raise AppError(503, "服务正在停止；导出文件已保留，未登记到项目")
            current = self._read(pid)
            if current["revision"] != source_revision:
                raise AppError(409, "导出期间项目已更新；导出文件已保留，请重新导出当前版本")
            self._editable(pid)
            current["export"] = exported
            self._save(current)
        return exported

    def close(self):
        with self.lock:
            self._closed = True
            if self._active_id:
                self.cancel_job(self._active_id)
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=15)
        if not thread or not thread.is_alive():
            self._data_lock.close()

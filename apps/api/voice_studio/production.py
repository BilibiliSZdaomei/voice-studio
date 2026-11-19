"""Reusable production assets, reviewed manuscript proposals and safe project restoration."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .assets import PronunciationDictionary
from .archive import restore_archive
from .errors import AppError
from .prepare import cleanup_manuscript, fact_changes, protected_tokens
from .text import clean_script, title_text


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ProductionMixin:
    def _project_defaults(self, project):
        project.setdefault("voice_id", "default")
        if "voice_snapshot" not in project:
            project["voice_snapshot"] = self.assets.get_voice(project["voice_id"])
        project.setdefault("dictionary_snapshot", {"revision": 0, "entries": []})
        if isinstance(project["dictionary_snapshot"], list):
            project["dictionary_snapshot"] = {"revision": 0, "entries": project["dictionary_snapshot"]}
        project.setdefault("dictionary_enabled", True)
        project.setdefault("video", None)
        for segment in project["segments"]:
            segment.setdefault("spoken_text", segment["text"])
        return project

    def _initialize_project(self, project, payload):
        voice_id = payload.get("voice_id", "default")
        project.update(voice_id=voice_id, voice_snapshot=self.assets.get_voice(voice_id),
                       dictionary_snapshot=self.assets.list_dictionary(), dictionary_enabled=True, video=None)
        dictionary = PronunciationDictionary.for_project(project)
        for segment in project["segments"]:
            segment["spoken_text"] = dictionary.segment(segment["text"])["spoken_text"]

    @staticmethod
    def _invalidate(segment):
        segment.update(status="pending", audio_url=None, duration_seconds=None, error=None)
        segment["revision"] += 1

    def list_voices(self):
        return self.assets.list_voices()

    def save_voice(self, payload):
        with self.lock:
            return self.assets.save_voice(payload)

    def save_dictionary(self, payload):
        with self.lock:
            return self.assets.save_dictionary(payload)

    def voice_sample_path(self, voice_id):
        voice = self.assets.get_voice(voice_id)
        value = voice.get("sample_audio") or voice.get("reference_audio")
        if not value or not Path(value).is_file():
            raise AppError(404, "该声线没有可用样音")
        return Path(value)

    def dictionary_preview(self, pid):
        with self.lock:
            project = self._read(pid)
        snapshot = self.assets.list_dictionary()
        dictionary = PronunciationDictionary(snapshot)
        return {"revision": snapshot["revision"], "segments": [
            {"id": item["id"], "text": item["text"], **dictionary.segment(item["text"])} for item in project["segments"]]}

    def apply_project_dictionary(self, pid, payload):
        with self.lock:
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            enabled = payload.get("enabled", True)
            if type(enabled) is not bool:
                raise AppError(400, "词典启用状态必须为布尔值")
            project["dictionary_snapshot"] = self.assets.list_dictionary()
            project["dictionary_enabled"] = enabled
            dictionary = PronunciationDictionary.for_project(project)
            changed = False
            for segment in project["segments"]:
                spoken = dictionary.segment(segment["text"])["spoken_text"]
                if spoken != segment.get("spoken_text", segment["text"]):
                    self._invalidate(segment)
                    segment["spoken_text"] = spoken
                    changed = True
            if changed:
                project["export"] = project["video"] = None
            self._save(project)
            return project

    def create_batch(self, payload):
        values = payload.get("projects")
        generate = payload.get("generate", False)
        if not isinstance(values, list) or not 1 <= len(values) <= 20 or type(generate) is not bool:
            raise AppError(400, "每批需要 1–20 篇文案，并明确是否生成")
        cleaned = []
        for value in values:
            if not isinstance(value, dict):
                raise AppError(400, "批量项目格式无效")
            cleaned.append({"title": title_text(value.get("title")), "script": clean_script(value.get("script")),
                            "voice_id": value.get("voice_id", "default")})
        if sum(len(item["script"]) for item in cleaned) > 180000:
            raise AppError(400, "同一批文案总量最多 180000 字")
        with self.lock:
            if self._closed:
                raise AppError(503, "服务正在停止")
            if generate and len(self._queue) + len(cleaned) > 100:
                raise AppError(409, "当前队列空间不足，请稍后再导入")
            projects = [self._new_project(item) for item in cleaned]
            if generate:
                for project in projects:
                    self._require_speech_ready(project)
            for project in projects:
                self._save(project, bump=False)
            jobs = [self._queue_speech(project) for project in projects] if generate else []
            return {"projects": projects, "jobs": jobs}

    def import_project(self, archive_path):
        # The importer generates a fresh project ID and validates all ZIP content first.
        with self.lock:
            project = restore_archive(Path(archive_path), self.root, voice_id="default")
            project["voice_id"] = "default"
            project["voice_snapshot"] = self.assets.get_voice("default")
            self._project_defaults(project)
            project["video"] = None
            self._save(project, bump=False)
            return project

    def _proposal_path(self, pid, proposal_id):
        self._project_path(pid)
        if not isinstance(proposal_id, str) or len(proposal_id) != 32 or any(char not in "0123456789abcdef" for char in proposal_id):
            raise AppError(404, "建议稿不存在")
        return self.root / "proposals" / pid / (proposal_id + ".json")

    def prepare_manuscript(self, pid, payload):
        with self.lock:
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            original, revision = project["script"], project["revision"]
        mode = payload.get("mode", "cleanup")
        if mode == "cleanup":
            proposed = cleanup_manuscript(original)
        elif mode == "rewrite":
            proposed = self.rewriter.rewrite(original, payload.get("instruction", ""))
        else:
            raise AppError(400, "不支持该稿件处理模式")
        warnings = fact_changes(original, proposed)
        proposal = {"id": uuid.uuid4().hex, "project_id": pid, "mode": mode, "source_revision": revision,
                    "original_text": original, "proposed_text": proposed, "warnings": warnings,
                    "protected_tokens": sorted(protected_tokens(original).elements()),
                    "review_notes": ["轻量模型可能删改限定条件或语义；请逐句对照原稿，不能只核对数字。"] if mode == "rewrite" else [],
                    "can_apply": not warnings, "created_at": stamp(),
                    "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest()}
        self._write_json(self._proposal_path(pid, proposal["id"]), proposal)
        return proposal

    def _read_proposal(self, pid, proposal_id):
        path = self._proposal_path(pid, proposal_id)
        if not path.is_file():
            raise AppError(404, "建议稿不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def apply_proposal(self, pid, proposal_id, payload):
        if payload.get("confirmed") is not True:
            raise AppError(400, "请对照原稿后明确确认采用")
        with self.lock:
            project = self._read(pid)
            self._editable(pid)
            self._revision(project, payload)
            proposal = self._read_proposal(pid, proposal_id)
            if proposal["source_revision"] != project["revision"] or proposal["original_text"] != project["script"]:
                raise AppError(409, "原稿已变化，请重新生成建议稿")
            if fact_changes(project["script"], proposal["proposed_text"]):
                raise AppError(422, "建议稿改变了关键数字或缩写，未采用；请在文案编辑器中人工处理")
            self._write_json(self.root / "projects" / pid / "history" / (proposal_id + ".json"), project)
            updated = self.update_project(pid, {"script": proposal["proposed_text"], "expected_revision": project["revision"]})
            updated["last_preparation"] = {"id": proposal_id, "mode": proposal["mode"], "applied_revision": updated["revision"]}
            self._save(updated, bump=False)
            return updated

    def undo_proposal(self, pid, proposal_id, payload):
        if payload.get("confirmed") is not True:
            raise AppError(400, "请确认撤销上次整理")
        with self.lock:
            current = self._read(pid)
            self._editable(pid)
            self._revision(current, payload)
            applied = current.get("last_preparation", {})
            if applied.get("id") != proposal_id or applied.get("applied_revision") != current["revision"]:
                raise AppError(409, "采用建议后已有其他修改，未覆盖当前项目；可手动对照原稿")
            path = self.root / "projects" / pid / "history" / (proposal_id + ".json")
            if not path.is_file():
                raise AppError(404, "原稿快照不存在")
            restored = json.loads(path.read_text(encoding="utf-8"))
            restored["revision"] = current["revision"]
            restored.pop("last_preparation", None)
            self._save(restored)
            return restored

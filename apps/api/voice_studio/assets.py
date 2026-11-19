"""Local voice references and conservative, versioned pronunciation replacements."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import unicodedata
import uuid
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import validate_data_path
from .errors import AppError
from .text import MAX_SEGMENT, clean_script


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _plain_text(value, maximum, label):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum
            or any(unicodedata.category(char).startswith("C") or char in "<>[]{}\\`" for char in value)):
        raise AppError(400, f"{label}需要 1–{maximum} 个纯文字字符，不支持控制字符或发音标记")
    return value.strip()


def validate_dictionary_entries(entries):
    """Validate a snapshot as well as an editable dictionary; never interpret markup."""
    if not isinstance(entries, list) or len(entries) > 200:
        raise AppError(400, "发音词典最多保存 200 条")
    result, terms, identities = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"id", "term", "spoken", "enabled"}:
            raise AppError(400, "发音词典条目格式无效")
        term = _plain_text(entry.get("term"), 60, "原词")
        spoken = _plain_text(entry.get("spoken"), 100, "读法")
        identity = entry["id"] if "id" in entry else uuid.uuid4().hex
        enabled = entry.get("enabled", True)
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity)
                or type(enabled) is not bool or term in terms or identity in identities):
            raise AppError(400, "发音词典原词和标识必须唯一，启用状态须为布尔值")
        terms.add(term)
        identities.add(identity)
        result.append({"id": identity, "term": term, "spoken": spoken, "enabled": enabled})
    return result


class PronunciationDictionary:
    """Validate and compile a frozen dictionary once for all segments of a project."""

    def __init__(self, entries):
        if isinstance(entries, dict):
            entries = entries.get("entries")
        active = [entry for entry in validate_dictionary_entries(entries) if entry["enabled"]]
        active.sort(key=lambda entry: -len(entry["term"]))
        self.readings = {entry["term"]: entry["spoken"] for entry in active}
        self.pattern = re.compile("|".join(map(re.escape, self.readings))) if active else None

    @classmethod
    def for_project(cls, project):
        return cls(project.get("dictionary_snapshot", []) if project.get("dictionary_enabled", True) else [])

    def apply(self, text):
        if not isinstance(text, str):
            raise AppError(400, "待朗读文案格式无效")
        if self.pattern is None:
            return {"spoken_text": text, "replacements": []}
        counts = Counter()

        def replace(match):
            term = match.group(0)
            counts[term] += 1
            return self.readings[term]

        spoken = self.pattern.sub(replace, text)
        return {"spoken_text": spoken, "replacements": [
            {"term": term, "spoken": self.readings[term], "count": counts[term]}
            for term in self.readings if term in counts]}

    def segment(self, text):
        result = self.apply(text)
        if len(result["spoken_text"]) > MAX_SEGMENT:
            raise AppError(400, f"词典替换后片段超过 {MAX_SEGMENT} 字，请缩短片段或词典读法")
        return result


def apply_dictionary(text, entries):
    return PronunciationDictionary(entries).apply(text)


class AssetStore:
    def __init__(self, root: Path, config: dict):
        self.root = validate_data_path(Path(root))
        self.config = dict(config)
        self.lock = threading.RLock()
        self._hashes = {}

    def _path(self, name):
        path = self.root / "assets" / name
        if not path.resolve().is_relative_to(self.root):
            raise AppError(400, "资产目录必须位于本机数据目录内")
        return path

    @staticmethod
    def _read(path, default):
        if not path.exists():
            return copy.deepcopy(default)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            return value
        except (ValueError, OSError) as error:
            raise AppError(500, "资产文件无法读取，原文件已保留") from error

    def _voices(self):
        value = self._read(self._path("voices.json"), {"version": 1, "voices": []})
        if value.get("version") != 1 or not isinstance(value.get("voices"), list):
            raise AppError(500, "声音档案格式无效，原文件已保留")
        return value

    def _default(self):
        reference_audio = self.config.get("reference_audio")
        reference_hash = self._reference_hash(reference_audio)
        sample = self.config.get("accepted_sample")
        if not isinstance(sample, str) or not sample or not Path(sample).is_file():
            sample = None
        if not reference_hash:
            scope = "参考录音尚未配置或无法读取，请先配置本机声线。"
        elif sample:
            scope = "已验收短样音；新文案仍需试听确认。"
        else:
            scope = "已配置参考录音，生成效果待试听确认。"
        return {"id": "default", "name": "我的声线", "revision": 1, "version": 1,
                "created_at": None, "updated_at": None,
                "reference_audio": reference_audio, "reference_sha256": reference_hash,
                "reference_text": self.config.get("reference_text", ""),
                "sample_audio": sample, "read_only": True, "acceptance_scope": scope}

    def _reference_hash(self, value):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None
        path = Path(value)
        try:
            metadata = path.stat()
            key = (str(path.resolve()), metadata.st_size, metadata.st_mtime_ns)
            if key not in self._hashes:
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                self._hashes[key] = digest.hexdigest()
            return self._hashes[key]
        except OSError:
            return None

    def list_voices(self):
        with self.lock:
            return [self._default()] + copy.deepcopy(self._voices()["voices"])

    def get_voice(self, identity):
        with self.lock:
            if identity == "default":
                return self._default()
            for voice in self._voices()["voices"]:
                if voice.get("id") == identity:
                    return voice
        raise AppError(404, "声音档案不存在")

    @staticmethod
    def _audio_path(value, optional=False):
        if optional and value is None:
            return None
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise AppError(400, "录音必须引用本机可读取的绝对 WAV 路径")
        path = Path(value).resolve()
        try:
            with wave.open(str(path), "rb") as audio:
                if (audio.getcomptype() != "NONE" or audio.getnchannels() < 1
                        or audio.getsampwidth() not in (1, 2, 3, 4)
                        or audio.getframerate() <= 0 or audio.getnframes() <= 0):
                    raise ValueError("Invalid PCM WAV")
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                actual = 0
                while block := audio.readframes(65536):
                    actual += len(block)
                if actual != expected:
                    raise ValueError("Truncated PCM WAV")
        except (OSError, ValueError, EOFError, wave.Error) as error:
            raise AppError(400, "录音需为可读取且完整的 PCM WAV 文件") from error
        return str(path)

    def save_voice(self, payload):
        if not isinstance(payload, dict):
            raise AppError(400, "声音档案格式无效")
        identity = payload.get("voice_id", payload.get("id"))
        if identity == "default":
            raise AppError(409, "默认声线由本机配置管理，请新建档案；原始录音不会被修改")
        if identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity)):
            raise AppError(404, "声音档案不存在")
        with self.lock:
            document = self._voices()
            previous = next((voice for voice in document["voices"] if voice.get("id") == identity), None)
            if identity is not None and previous is None:
                raise AppError(404, "声音档案不存在")
            if previous is not None:
                expected = payload.get("expected_revision")
                if type(expected) is not int or expected != previous["revision"]:
                    raise AppError(409, "声音档案已更新，请重新载入后保存")
            source = dict(previous or {})
            source.update(payload)
            name = _plain_text(source.get("name"), 100, "声音名称")
            reference_text = clean_script(source.get("reference_text"))
            reference_audio = self._audio_path(source.get("reference_audio"))
            sample_audio = self._audio_path(source.get("sample_audio"), optional=True)
            created = _now()
            voice = {"id": identity or uuid.uuid4().hex, "name": name, "version": 1,
                     "revision": previous["revision"] + 1 if previous else 1,
                     "created_at": previous["created_at"] if previous else created, "updated_at": created,
                     "reference_audio": reference_audio, "reference_text": reference_text,
                     "reference_sha256": self._reference_hash(reference_audio),
                     "sample_audio": sample_audio, "read_only": False,
                     "acceptance_scope": "本机录音引用；新声线及新文案均需试听确认。"}
            if previous:
                document["voices"][document["voices"].index(previous)] = voice
            else:
                document["voices"].append(voice)
            _atomic_json(self._path("voices.json"), document)
            return copy.deepcopy(voice)

    def list_dictionary(self):
        with self.lock:
            value = self._read(self._path("dictionary.json"), {"revision": 0, "entries": []})
            if type(value.get("revision")) is not int or value["revision"] < 0:
                raise AppError(500, "发音词典版本格式无效，原文件已保留")
            return {"revision": value["revision"], "entries": validate_dictionary_entries(value.get("entries"))}

    def save_dictionary(self, payload):
        if not isinstance(payload, dict):
            raise AppError(400, "发音词典格式无效")
        with self.lock:
            previous = self.list_dictionary()
            expected = payload.get("expected_revision")
            if type(expected) is not int or expected != previous["revision"]:
                raise AppError(409, "发音词典已更新，请重新载入后保存")
            value = {"revision": previous["revision"] + 1,
                     "entries": validate_dictionary_entries(payload.get("entries"))}
            _atomic_json(self._path("dictionary.json"), value)
            return copy.deepcopy(value)

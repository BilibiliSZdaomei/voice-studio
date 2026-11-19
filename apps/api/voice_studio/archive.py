"""Restore local project archives without trusting stored paths or extracting ZIPs."""
from __future__ import annotations

import copy
import io
import json
import math
import re
import shutil
import stat
import uuid
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .assets import PronunciationDictionary, validate_dictionary_entries
from .config import validate_data_path
from .errors import AppError
from .text import MAX_SEGMENT, clean_script, settings_value, split_sentences, title_text

MAX_ITEMS = 1000
MAX_TOTAL = 300 * 1024 * 1024
MAX_FILE = 100 * 1024 * 1024
MAX_RATIO = 300
MAX_JSON = 4 * 1024 * 1024
_AUDIO_NAME = re.compile(r"segments/([0-9]{4})\.wav")


def _failure(message):
    return AppError(400, "工程包无法恢复：" + message)


def _members(archive):
    items = archive.infolist()
    if len(items) > MAX_ITEMS:
        raise _failure("文件数量超过 1000 个")
    names, result, total = set(), {}, 0
    for item in items:
        name = item.filename
        parts = PurePosixPath(name).parts
        normalized = name.rstrip("/").casefold()
        if (not name or "\\" in name or ":" in name or name.startswith("/")
                or any(part in (".", "..") for part in name.split("/"))
                or any(ord(char) < 32 for char in name) or not parts
                or getattr(item, "orig_filename", name) != name or normalized in names):
            raise _failure("包含不安全路径或重复文件名")
        names.add(normalized)
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise _failure("不接受符号链接或特殊文件")
        if item.flag_bits & 1:
            raise _failure("不接受加密文件")
        if item.file_size < 0 or item.file_size > MAX_FILE:
            raise _failure("单文件大小超过 100 MB")
        total += item.file_size
        if total > MAX_TOTAL:
            raise _failure("解压总大小超过 300 MB")
        if item.file_size and (not item.compress_size or item.file_size / item.compress_size > MAX_RATIO):
            raise _failure("压缩比超过安全限制")
        if not item.is_dir():
            result[name] = item
    return result


def _read(archive, item, limit=MAX_FILE):
    if item.file_size > limit:
        raise _failure("元数据文件过大")
    with archive.open(item) as source:
        data = source.read(limit + 1)
    if len(data) != item.file_size or len(data) > limit:
        raise _failure("文件大小与目录记录不符")
    return data


def _json(archive, members, name):
    if name not in members:
        raise _failure(f"缺少 {name}")
    try:
        value = json.loads(_read(archive, members[name], MAX_JSON).decode("utf-8-sig"),
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError) as error:
        raise _failure(f"{name} 不是有效 JSON") from error
    if not isinstance(value, dict):
        raise _failure(f"{name} 格式无效")
    return value


def _pcm(data):
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate, frames = audio.getframerate(), audio.getnframes()
            if (audio.getcomptype() != "NONE" or audio.getnchannels() != 1
                    or audio.getsampwidth() != 2 or rate <= 0 or frames <= 0):
                raise ValueError("Expected PCM16 mono")
            length = 0
            while block := audio.readframes(65536):
                length += len(block)
            if length != frames * 2:
                raise ValueError("Truncated audio")
            return frames / rate, rate
    except (ValueError, EOFError, wave.Error) as error:
        raise _failure("片段必须是完整的 PCM16 单声道 WAV") from error


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _snapshot(value):
    entries = value if isinstance(value, list) else value.get("entries") if isinstance(value, dict) else None
    if (not isinstance(entries, list)
            or any(not isinstance(entry, dict) or set(entry) != {"id", "term", "spoken", "enabled"}
                   for entry in entries)):
        raise _failure("发音词典快照条目格式无效")
    if isinstance(value, list):
        return validate_dictionary_entries(value)
    if isinstance(value, dict) and set(value) <= {"revision", "entries"}:
        revision = value.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise _failure("发音词典快照版本无效")
        return {"revision": revision, "entries": validate_dictionary_entries(value.get("entries"))}
    raise _failure("发音词典快照格式无效")


def _voice_id(value):
    if value != "default" and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)):
        raise _failure("声音档案标识无效")
    return value


def _within(root, path):
    if not path.resolve().is_relative_to(root):
        raise _failure("目标目录越出本机数据根目录")
    return path


def restore_archive(archive_path: Path, data_root: Path, voice_id="default") -> dict:
    """Create one new project; never overwrite an existing project or trust a URL."""
    root = validate_data_path(Path(data_root))
    if not Path(archive_path).is_file():
        raise _failure("找不到本机 ZIP 文件")
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = _members(archive)
            source = _json(archive, members, "project.json")
            manifest = _json(archive, members, "manifest.json")
            script = clean_script(source.get("script"))
            if script != source.get("script"):
                raise _failure("项目文案格式不完整")
            title = title_text(source.get("title"))
            settings = settings_value(source.get("settings", {}))
            if "script.txt" in members:
                try:
                    archived_script = (_read(archive, members["script.txt"], MAX_JSON).decode("utf-8-sig")
                                       .replace("\r\n", "\n").replace("\r", "\n"))
                except UnicodeError as error:
                    raise _failure("script.txt 必须为 UTF-8 文本") from error
                if archived_script != script:
                    raise _failure("稿件与项目文案不一致")
            originals = source.get("segments")
            if not isinstance(originals, list) or not 1 <= len(originals) <= MAX_ITEMS:
                raise _failure("项目片段列表无效")
            timeline = manifest.get("timeline", [])
            if not isinstance(timeline, list):
                raise _failure("片段时间轴格式无效")
            if any(_AUDIO_NAME.fullmatch(name) and int(_AUDIO_NAME.fullmatch(name).group(1)) > len(originals)
                   for name in members):
                raise _failure("包含没有对应文案的片段音频")
            identity = uuid.uuid4().hex
            created = datetime.now(timezone.utc).isoformat()
            project = {"id": identity, "title": title[:96] + "（恢复）", "script": script,
                       "revision": 1, "created_at": created, "updated_at": created, "settings": settings,
                       "voice_id": _voice_id(source.get("voice_id", voice_id)), "segments": [], "export": None}
            if "dictionary_snapshot" in source:
                project["dictionary_snapshot"] = _snapshot(source["dictionary_snapshot"])
            project["dictionary_enabled"] = source.get("dictionary_enabled", True)
            if type(project["dictionary_enabled"]) is not bool:
                raise _failure("词典启用状态无效")
            dictionary = PronunciationDictionary.for_project(project)
            prepared, seen = [], set()
            for index, original in enumerate(originals, 1):
                if not isinstance(original, dict):
                    raise _failure("片段记录格式无效")
                text = clean_script(original.get("text"))
                if len(text) > MAX_SEGMENT or text != original.get("text"):
                    raise _failure("片段文案长度或格式无效")
                old_id = original.get("id")
                if not isinstance(old_id, str) or old_id in seen:
                    raise _failure("片段标识缺失或重复")
                seen.add(old_id)
                spoken = dictionary.segment(text)["spoken_text"]
                segment = {"id": uuid.uuid4().hex, "text": text, "spoken_text": spoken, "revision": 1, "status": "pending",
                           "audio_url": None, "duration_seconds": None, "error": None}
                name = f"segments/{index:04}.wav"
                if name in members:
                    data = _read(archive, members[name])
                    duration, rate = _pcm(data)
                    item = timeline[index - 1] if index <= len(timeline) else None
                    recorded = original.get("duration_seconds")
                    matches = (original.get("status") == "ready" and original.get("spoken_text", spoken) == spoken
                               and isinstance(item, dict)
                               and item.get("text") == text and item.get("id") == old_id
                               and _finite_number(recorded) and abs(recorded - duration) <= 1 / rate + .001
                               and _finite_number(item.get("start_seconds"))
                               and _finite_number(item.get("end_seconds"))
                               and item["start_seconds"] >= 0
                               and abs(item["end_seconds"] - item["start_seconds"] - duration) <= 1 / rate + .001)
                    if matches:
                        relative = Path("projects") / identity / "audio" / segment["id"] / (uuid.uuid4().hex + ".wav")
                        segment.update(status="ready", audio_url="/files/" + quote(relative.as_posix(), safe="/"),
                                       duration_seconds=duration)
                        for key in ("engine", "voice_id", "voice_revision", "dictionary_revision"):
                            if key in original:
                                segment[key] = copy.deepcopy(original[key])
                        if "dictionary_snapshot" in original:
                            segment["dictionary_snapshot"] = _snapshot(original["dictionary_snapshot"])
                        prepared.append((relative.relative_to(Path("projects") / identity), data))
                    else:
                        segment["error"] = "归档音频与原文、读法或时长快照不一致，请重新生成此段"
                else:
                    segment["error"] = "工程包缺少此段音频，请重新生成"
                project["segments"].append(segment)
            # Segment editing can keep several sentences in one segment, while
            # v1 splitting removes boundary whitespace. Compare the complete
            # retained text without imposing a new segmentation on the archive.
            expected_text = "".join(split_sentences(script))
            retained_text = "".join(segment["text"] for segment in project["segments"])
            if "".join(expected_text.split()) != "".join(retained_text.split()):
                raise _failure("片段文案未完整对应项目原稿")
    except AppError:
        raise
    except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
        raise _failure("ZIP 文件损坏、格式不支持或无法读取") from error

    # Nothing is written until every member and every selected PCM file is checked.
    stage = _within(root, root / "staging" / ("restore-" + uuid.uuid4().hex))
    destination = _within(root, root / "projects" / project["id"])
    stage_created = False
    try:
        stage.mkdir(parents=True, exist_ok=False)
        stage_created = True
        for relative, data in prepared:
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (stage / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise _failure("新项目标识冲突，请重新恢复")
        stage.rename(destination)
        stage_created = False
        return project
    except OSError as error:
        raise AppError(500, "工程恢复无法写入本机数据目录，原工程包已保留") from error
    finally:
        if stage_created and stage.exists() and stage.resolve().is_relative_to(root / "staging"):
            shutil.rmtree(stage)

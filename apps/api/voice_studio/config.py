"""Machine-local configuration. Model and audio files never live in the repository."""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VOICE_FILES = ("runtime_python", "encoder", "decoder", "tokens", "vocoder", "reference_audio")


def default_workspace() -> Path:
    if root := os.environ.get("CODEX_ARTIFACT_ROOT"):
        return Path(root) / "VoiceStudio"
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        root = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(root) / "VoiceStudio"


def default_config() -> dict:
    return {
        "data_root": str(default_workspace() / "data"),
        **dict.fromkeys(VOICE_FILES, ""),
        "lexicon": "",
        "reference_text": "",
        "accepted_sample": "",
        "num_threads": 4,
        "rewrite": {"enabled": False, "base_url": "http://127.0.0.1:11434/v1", "model": ""},
        "video": {},
    }


def voice_resource_checks(config: dict) -> dict:
    checks = {}
    for key in VOICE_FILES:
        value = config.get(key)
        checks[key] = {"path": value, "exists": isinstance(value, str) and bool(value) and Path(value).is_file()}
    lexicon = config.get("lexicon")
    checks["lexicon"] = {"exists": isinstance(lexicon, str) and bool(lexicon)
                         and all(Path(value).is_file() for value in lexicon.split(","))}
    reference_text = config.get("reference_text")
    checks["reference_text"] = {"exists": isinstance(reference_text, str) and bool(reference_text.strip())}
    return checks


def _registered_roots(file: Path, section: str | None = None):
    try:
        records = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return
    if isinstance(records, dict) and section:
        records = records.get(section)
    if isinstance(records, dict):
        for item in records.values():
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                root = Path(item["path"])
                if root.is_absolute():
                    yield root.resolve()


def validate_data_path(path: Path) -> Path:
    """Reject source/sync roots, including targets reached through filesystem links."""
    if not path.is_absolute():
        raise ValueError("数据目录必须使用绝对路径")
    resolved = path.expanduser().resolve()
    roots = [PROJECT_ROOT.resolve()]
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]).resolve())
    for name in ("APPDATA", "LOCALAPPDATA"):
        if folder := os.environ.get(name):
            roots.extend(_registered_roots(Path(folder) / "Dropbox" / "info.json"))
    if folder := os.environ.get("APPDATA"):
        roots.extend(_registered_roots(Path(folder) / "obsidian" / "obsidian.json", "vaults"))
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            raise ValueError(f"数据目录必须在代码和同步目录之外：{resolved}")
    for parent in (resolved, *resolved.parents):
        if (parent / ".obsidian").exists():
            raise ValueError(f"数据目录位于 Obsidian 仓库中：{resolved}")
    return resolved


def load_config(config_path: str | None = None) -> dict:
    result = default_config()
    explicit_path = config_path or os.environ.get("VOICE_STUDIO_CONFIG")
    path = Path(explicit_path) if explicit_path else PROJECT_ROOT / "config" / "local.json"
    if explicit_path or path.exists():
        override = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(override, dict):
            raise ValueError("本地配置必须为 JSON 对象")
        result.update(override)
    result["data_root"] = str(validate_data_path(Path(result["data_root"])))
    return result

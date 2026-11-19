"""Offline HyperFrames video exports from immutable narration snapshots."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
import wave
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

from .config import validate_data_path
from .errors import AppError
from .avatar import audio_envelope, build_host_composition


class VideoCancelled(Exception):
    """The renderer and its process tree have stopped after cancellation."""


def caption_cards(text: str, limit: int) -> list[str]:
    """Bound caption length without claiming word or character alignment."""
    text = re.sub(r"\s+", " ", text).strip()
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        boundaries = [match.end() for match in re.finditer(r"[，。！？；、,.!?;\s]", window)
                      if match.end() >= limit // 2]
        end = boundaries[-1] if boundaries else limit
        chunks.append(text[:end].strip())
        text = text[end:].strip()
    if text:
        chunks.append(text)
    return chunks


class VideoExporter:
    """No inference, uploads or runtime package installation takes place here.

    config['video'] supplies node, hyperframes_cli, gsap, font, browser,
    ffmpeg and ffprobe absolute paths. Call render from a background job.
    """

    def __init__(self, config: dict, data_root: Path):
        self.config = dict(config.get("video", {}))
        self.root = validate_data_path(Path(data_root))

    def _tool(self, name, fallback=None):
        value = self.config.get(name) or fallback
        return str(Path(value).resolve()) if value and Path(value).is_file() else None

    def _resources(self):
        cli = self._tool("hyperframes_cli")
        gsap = self._tool("gsap")
        if not gsap and cli:
            candidates = sorted(Path(cli).parent.glob("vendor/npm/gsap@*/dist/gsap.min.js"))
            gsap = str(candidates[-1]) if candidates else None
        font = self._tool("font", r"C:\Windows\Fonts\msyh.ttc")
        return {"node": self._tool("node", shutil.which("node")),
                "hyperframes_cli": cli, "gsap": gsap, "font": font,
                "browser": self._tool("browser", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                "ffmpeg": self._tool("ffmpeg", shutil.which("ffmpeg")),
                "ffprobe": self._tool("ffprobe", shutil.which("ffprobe"))}

    def availability(self) -> dict:
        resources = self._resources()
        missing = [key for key, value in resources.items() if not value]
        return {"available": not missing, "renderer": "HyperFrames", "local_only": True,
                "missing": missing, "aspect_ratios": ["16:9", "9:16"],
                "layouts": ["captions", "virtual_host"],
                "avatars": [{"id": "studio-bot", "name": "小机器人", "requires_personal_media": False}],
                "subtitle_alignment": "segment_level_estimated_cards",
                "message": "本地字幕视频导出已配置" if not missing else "视频导出缺少资源：" + "、".join(missing)}

    def _asset(self, url, pid):
        if not isinstance(url, str) or not url.startswith("/files/"):
            raise AppError(409, "请先导出当前项目的完整配音")
        path = (self.root / unquote(url[7:])).resolve()
        allowed = (self.root / "exports" / pid).resolve()
        if not path.is_relative_to(self.root) or not allowed.is_relative_to(self.root) or not path.is_relative_to(allowed) or not path.is_file() or path.suffix.lower() != ".wav":
            raise AppError(409, "视频只接受本项目已导出的 WAV 文件")
        return path

    def _url(self, path):
        return "/files/" + quote(path.resolve().relative_to(self.root).as_posix(), safe="/")

    def _source(self, project, exported):
        pid = project.get("id", "")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{32}", pid):
            raise AppError(404, "项目不存在")
        if not isinstance(exported, dict):
            raise AppError(409, "请先导出完整配音，再制作视频")
        wav = self._asset(exported.get("wav_url"), pid)
        try:
            manifest_path = (wav.parent / "manifest.json").resolve()
            if not manifest_path.is_relative_to(wav.parent) or not manifest_path.is_file():
                raise ValueError("missing manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            timeline = manifest["timeline"]
            with wave.open(str(wav), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            saved_path = (wav.parent / "project.json").resolve()
            if not saved_path.is_relative_to(wav.parent):
                raise ValueError("unsafe project snapshot")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            expected = [(item["id"], item["text"]) for item in project["segments"]]
            actual = [(item["id"], item["text"]) for item in timeline]
            if not timeline or actual != expected or any(item.get("status") != "ready" for item in project["segments"]):
                raise ValueError("stale segments")
            if [(item.get("audio_url"), item.get("revision")) for item in saved["segments"]] != [
                    (item.get("audio_url"), item.get("revision")) for item in project["segments"]]:
                raise ValueError("changed audio versions")
            if saved["settings"] != project["settings"]:
                raise ValueError("changed audio settings")
            previous = 0.0
            for item in timeline:
                start, end = float(item["start_seconds"]), float(item["end_seconds"])
                if not isinstance(item["text"], str) or not item["text"].strip() or not all(math.isfinite(value) for value in (start, end)) or start < previous - .001 or end <= start or end > duration + .002:
                    raise ValueError("invalid timeline")
                previous = end
            exported_duration = float(exported["duration_seconds"])
            if not math.isfinite(exported_duration) or abs(previous - duration) > .002 or abs(exported_duration - duration) > .002:
                raise ValueError("duration mismatch")
            revision = exported["source_revision"]
            if type(revision) is not int or revision > project["revision"]:
                raise ValueError("invalid revision")
        except (OSError, ValueError, KeyError, TypeError, wave.Error) as error:
            raise AppError(409, "导出的文案时间轴已失效或无法读取，请重新导出完整配音") from error
        return wav, timeline, duration, revision

    @staticmethod
    def _options(options):
        aspect = options.get("aspect_ratio", "16:9")
        fps = options.get("fps", 30)
        if not isinstance(aspect, str) or aspect not in {"16:9", "9:16"} or type(fps) is not int or fps not in {24, 30}:
            raise AppError(400, "视频比例须为 16:9 或 9:16，帧率须为 24 或 30")
        return aspect, fps

    @staticmethod
    def presentation(options):
        layout = options.get("layout", "captions")
        if not isinstance(layout, str) or layout not in {"captions", "virtual_host"}:
            raise AppError(400, "画面形式须为纯字幕讲解或虚拟讲解员")
        avatar_id = options.get("avatar_id", "studio-bot") if layout == "virtual_host" else None
        if layout == "virtual_host" and avatar_id != "studio-bot":
            raise AppError(400, "当前可用虚拟形象为原创小机器人")
        return layout, avatar_id

    def _composition(self, project, timeline, duration, aspect, layout="captions"):
        width, height = (1920, 1080) if aspect == "16:9" else (1080, 1920)
        title = str(project.get("title", "声稿"))[:80]
        title_size = (32 if len(title) > 40 else 42) if aspect == "16:9" else (36 if len(title) > 32 else 50)
        if layout == "virtual_host" and aspect == "9:16":
            title_size = 28 if len(title) > 48 else 34 if len(title) > 28 else 46
        cards = []
        count = 0
        for index, segment in enumerate(timeline, 1):
            limit = (32 if aspect == "16:9" else 30) if layout == "virtual_host" else (48 if aspect == "16:9" else 36)
            parts = caption_cards(segment["text"], limit)
            weight = sum(len(part) for part in parts)
            elapsed = 0
            span = segment["end_seconds"] - segment["start_seconds"]
            for part in parts:
                start = segment["start_seconds"] + span * elapsed / weight
                card_duration = span * len(part) / weight
                elapsed += len(part)
                cards.append(f'<section id="card-{count}" class="clip caption-card" data-start="{start:.9f}" '
                             f'data-duration="{card_duration:.9f}" data-track-index="2">'
                             f'<div class="caption-inner"><div class="segment-label">{index:02d} / {len(timeline):02d}</div>'
                             f'<p class="caption">{html.escape(part)}</p></div></section>')
                count += 1
        source = (Path(__file__).parent / "templates" / "caption-video.html").read_text(encoding="utf-8")
        values = {"WIDTH": str(width), "HEIGHT": str(height), "DURATION": f"{duration:.9f}",
                  "TITLE": html.escape(title), "TITLE_SIZE": str(title_size),
                  "PORTRAIT_STYLE": """
    .frame { padding: 130px 90px; }
    .brand { font-size: 38px; }
    .title { max-width: 880px; }
    .caption-card { inset: 580px 100px 420px; }
    .caption { font-size: 76px; max-width: 880px; line-height: 1.65; }
    .segment-label { font-size: 32px; }
    .footer { flex-direction: column; gap: 18px; font-size: 29px; }
""" if aspect == "9:16" else "",
                  "ORIENTATION": "landscape" if aspect == "16:9" else "portrait", "CARDS": "\n".join(cards)}
        if layout == "virtual_host":
            values["PORTRAIT_STYLE"] += ("""
    .caption-card { inset: 320px 160px 205px 850px; }
    .caption { font-size: 58px; max-width: 910px; }
    .segment-label { font-size: 28px; margin-bottom: 28px; }
""" if aspect == "16:9" else """
    .frame { padding: 112px 90px 120px; }
    .brand { font-size: 30px; margin-bottom: 16px; }
    .rule { margin-top: 28px; }
    .caption-card { inset: 1050px 108px 260px; }
    .caption { font-size: 60px; max-width: 864px; line-height: 1.55; }
    .segment-label { font-size: 28px; margin-bottom: 26px; }
""")
        for key, value in values.items():
            source = source.replace("@@" + key + "@@", value)
        if layout == "virtual_host":
            source = source.replace("本地配音 · 字幕讲解", "专属声线 · 虚拟讲解")
        return source

    def _write_composition(self, folder, project, timeline, duration, aspect, layout="captions", avatar_id=None, envelope=None):
        compositions = folder / "compositions"
        compositions.mkdir(exist_ok=True)
        (compositions / "captions.html").write_text(self._composition(project, timeline, duration, aspect, layout), encoding="utf-8")
        host_track = ""
        if layout == "virtual_host":
            if not envelope:
                raise AppError(400, "虚拟讲解员缺少配音能量时间轴")
            (compositions / "host.html").write_text(build_host_composition(duration, aspect, envelope, avatar_id or "studio-bot"), encoding="utf-8")
            host_track = ('<div id="voice-host" class="clip" data-composition-id="voice-host" '
                          'data-composition-src="compositions/host.html" data-start="0" '
                          f'data-duration="{duration:.9f}" data-width="{1920 if aspect == "16:9" else 1080}" '
                          f'data-height="{1080 if aspect == "16:9" else 1920}" data-track-index="3" '
                          'data-track-kind="graphics" data-layout-allow-overlap="true" '
                          'style="position:absolute;inset:0;z-index:2;pointer-events:none"></div>')
        source = (Path(__file__).parent / "templates" / "video-project.html").read_text(encoding="utf-8")
        width, height = (1920, 1080) if aspect == "16:9" else (1080, 1920)
        for key, value in {"WIDTH": str(width), "HEIGHT": str(height), "DURATION": f"{duration:.9f}",
                           "TITLE": html.escape(str(project.get("title", "声稿"))[:80]), "HOST_TRACK": host_track}.items():
            source = source.replace("@@" + key + "@@", value)
        (folder / "index.html").write_text(source, encoding="utf-8")

    def _environment(self, folder, resources):
        env = os.environ.copy()
        temp = folder / ".render-temp"
        temp.mkdir(exist_ok=True)
        env.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
                    "HYPERFRAMES_BROWSER_PATH": resources["browser"],
                    "HYPERFRAMES_FFMPEG_PATH": resources["ffmpeg"],
                    "HYPERFRAMES_FFPROBE_PATH": resources["ffprobe"],
                    "HYPERFRAMES_SKIP_SKILLS": "1", "HYPERFRAMES_TELEMETRY_DISABLED": "1",
                    "HYPERFRAMES_CACHE_DIR": str(folder / ".cache"),
                    "DO_NOT_TRACK": "1", "CI": "1", "NO_COLOR": "1"})
        return env

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _run(self, command, folder, env, log_name, cancel_event, timeout=1800):
        if cancel_event is not None and cancel_event.is_set():
            raise VideoCancelled("视频导出已取消，已有配音未改变")
        started = time.monotonic()
        with (folder / log_name).open("wb") as log:
            process = subprocess.Popen(command, cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       start_new_session=os.name != "nt")
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        self._stop(process)
                        raise VideoCancelled("视频导出已取消，已有配音未改变")
                    if time.monotonic() - started > timeout:
                        self._stop(process)
                        raise AppError(500, "视频处理超时；工程和日志已保留")
                    time.sleep(.2)
            except BaseException:
                self._stop(process)
                raise
        if process.returncode:
            receipt = {"operation": log_name, "exit_code": process.returncode,
                       "elapsed_seconds": round(time.monotonic() - started, 3)}
            (folder / (log_name + ".process.json")).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            raise AppError(500, "HyperFrames 处理未完成；工程与 " + log_name + " 已保留")
        if cancel_event is not None and cancel_event.is_set():
            raise VideoCancelled("视频导出已取消，已有配音未改变")

    def render(self, project_snapshot: dict, export_snapshot: dict, options: dict | None = None,
               on_progress=None, cancel_event=None) -> dict:
        """Return only after real HyperFrames render and stream verification.

        on_progress(stage: str) receives preparing/checking/rendering/packaging.
        Cancellation raises VideoCancelled, never a successful partial result.
        """
        options = options or {}
        if not isinstance(options, dict):
            raise AppError(400, "视频设置须为 JSON 对象")
        aspect, fps = self._options(options)
        layout, avatar_id = self.presentation(options)
        wav, timeline, duration, revision = self._source(project_snapshot, export_snapshot)
        resources = self._resources()
        if not all(resources.values()):
            raise AppError(503, self.availability()["message"])
        if cancel_event is not None and cancel_event.is_set():
            raise VideoCancelled("视频导出已取消")
        folder = self.root / "videos" / project_snapshot["id"] / uuid.uuid4().hex
        folder = validate_data_path(folder)
        if not folder.is_relative_to(self.root):
            raise AppError(400, "视频输出目录必须保留在当前数据根内")
        folder.mkdir(parents=True)
        notify = on_progress or (lambda stage: None)
        notify("preparing")
        assets = folder / "assets"
        assets.mkdir()
        shutil.copyfile(wav, assets / "narration.wav")
        shutil.copyfile(resources["gsap"], assets / "gsap.min.js")
        (assets / "fonts").mkdir()
        shutil.copyfile(resources["font"], assets / "fonts" / "caption-font.ttc")
        envelope = audio_envelope(wav) if layout == "virtual_host" else None
        self._write_composition(folder, project_snapshot, timeline, duration, aspect, layout, avatar_id, envelope)
        if envelope:
            (folder / "audio-envelope.json").write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
        (folder / "hyperframes.json").write_text(json.dumps({"name": "voice-studio-caption-video", "version": "1.0.0"}), encoding="utf-8")
        version_file = Path(resources["hyperframes_cli"]).parent.parent / "package.json"
        version = json.loads(version_file.read_text(encoding="utf-8"))["version"] if version_file.is_file() else "unknown"
        with wav.open("rb") as source:
            source_hash = hashlib.file_digest(source, "sha256").hexdigest()
        metadata = {"renderer": "HyperFrames", "renderer_version": version, "source_revision": revision,
                    "source_wav_url": export_snapshot["wav_url"], "source_wav_sha256": source_hash,
                    "duration_seconds": duration, "aspect_ratio": aspect, "fps": fps, "timeline": timeline,
                    "layout": layout, "avatar_id": avatar_id,
                    "animation": "audio_energy_driven" if layout == "virtual_host" else None,
                    "mouth_sync": "amplitude_not_phonemes" if layout == "virtual_host" else None,
                    "subtitle_alignment": "segment_level_estimated_cards", "listening_acceptance": "pending_user_listening"}
        (folder / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "BRIEF.md").write_text("---\nworkflow: general-video\nflow: automation\nstoryboard: no\nlanguage: zh-CN\n---\n\n"
            "从既有配音导出讲解视频。保留原文与完整WAV；暖纸色、深墨色、梅紫色；无新增声音或云服务。\n"
            + ("原创SVG虚拟小机器人按真实音量开合嘴部、眨眼和轻动；无需照片，不宣称精确音素口型。\n" if layout == "virtual_host" else "纯字幕讲解，无虚拟角色。\n") +
            "字幕卡依据段级时间轴按字符长度分配，属于估算显示，不宣称字级对齐。\n", encoding="utf-8")
        env = self._environment(folder, resources)
        command = [resources["node"], resources["hyperframes_cli"]]
        notify("checking")
        self._run(command + ["check", ".", "--json"], folder, env, "check.log", cancel_event, 180)
        notify("rendering")
        video = folder / "video.mp4"
        self._run(command + ["render", ".", "--output", str(video), "--fps", str(fps),
                            "--quality", "looks", "--workers", "1"], folder, env, "render.log", cancel_event)
        if not video.is_file() or video.stat().st_size < 1000:
            raise AppError(500, "HyperFrames 没有产生有效视频；工程与日志已保留")
        probe = subprocess.run([resources["ffprobe"], "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)],
                               capture_output=True, text=True, encoding="utf-8", timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            info = json.loads(probe.stdout)
            video_duration = float(info["format"]["duration"])
            kinds = {stream["codec_type"] for stream in info["streams"]}
            if probe.returncode or not {"video", "audio"}.issubset(kinds) or abs(video_duration - duration) > .12:
                raise ValueError("stream or duration mismatch")
        except (KeyError, ValueError, TypeError) as error:
            raise AppError(500, "视频流或时长校验未通过；工程与日志已保留") from error
        (folder / "ffprobe.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        if cancel_event is not None and cancel_event.is_set():
            raise VideoCancelled("视频导出已取消，已有配音未改变")
        notify("packaging")
        bundle = folder / "hyperframes-project.zip"
        with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in [folder / name for name in ("index.html", "hyperframes.json", "BRIEF.md", "manifest.json")] + list(assets.rglob("*")) + list((folder / "compositions").rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(folder).as_posix())
            if envelope:
                archive.write(folder / "audio-envelope.json", "audio-envelope.json")
        if cancel_event is not None and cancel_event.is_set():
            raise VideoCancelled("视频导出已取消，已有配音未改变")
        return {"video_url": self._url(video), "composition_url": self._url(folder / "index.html"),
                "bundle_url": self._url(bundle), "manifest_url": self._url(folder / "manifest.json"),
                "duration_seconds": duration, "video_duration_seconds": video_duration, "aspect_ratio": aspect,
                "source_revision": revision, "renderer": "HyperFrames", "renderer_version": version,
                "layout": layout, "avatar_id": avatar_id,
                "animation": "audio_energy_driven" if layout == "virtual_host" else None,
                "mouth_sync": "amplitude_not_phonemes" if layout == "virtual_host" else None,
                "subtitle_alignment": "segment_level_estimated_cards"}

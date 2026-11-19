"""Original local SVG presenter and deterministic audio-energy animation.

The mouth follows measured RMS energy, not phonemes or precise lip alignment.
This module reads existing narration and never generates or uploads audio.
"""
from array import array
import json
import math
from pathlib import Path
import sys
import wave

from .errors import AppError


AVATAR_ID = "studio-bot"
MAX_DURATION_SECONDS = 24 * 60 * 60


def _sample_rate(value):
    if type(value) is not int or not 1 <= value <= 60:
        raise AppError(400, "角色能量采样率须为 1 到 60 的整数")
    return value


def _duration(value, label="角色时长"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppError(400, label + "须为有限正数")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= MAX_DURATION_SECONDS:
        raise AppError(400, label + "须大于 0 且不超过 24 小时")
    return value


def audio_envelope(wav_path: Path, sample_hz=15) -> dict:
    """Measure one normalized energy value per sample interval, using bounded reads.

    Accept uncompressed mono PCM16 only. A quiet absolute floor keeps background
    noise from becoming a full mouth opening, while a robust 90th percentile
    reference prevents isolated peaks from flattening the rest of the speech.
    Exact digital silence always produces zero. The final partial interval is
    kept; duration is taken from the actual WAV frame count.
    """
    sample_hz = _sample_rate(sample_hz)
    try:
        with wave.open(str(wav_path), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (1, 2, "NONE"):
                raise AppError(400, "虚拟讲解员需要单声道 PCM16 WAV 配音")
            rate, frames = source.getframerate(), source.getnframes()
            if rate < sample_hz or frames <= 0:
                raise AppError(400, "配音 WAV 的采样率或帧数无效")
            duration = _duration(frames / rate, "配音时长")
            count = (frames * sample_hz + rate - 1) // rate
            measured = []
            for index in range(count):
                begin = index * rate // sample_hz
                end = min(frames, (index + 1) * rate // sample_hz)
                remaining = end - begin
                square_sum = 0
                while remaining:
                    block_frames = min(remaining, 65536)
                    raw = source.readframes(block_frames)
                    if len(raw) != block_frames * 2:
                        raise AppError(400, "配音 WAV 数据不完整")
                    samples = array("h")
                    samples.frombytes(raw)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    square_sum += sum(sample * sample for sample in samples)
                    remaining -= block_frames
                measured.append(math.sqrt(square_sum / (end - begin)) / 32768)
    except (OSError, EOFError, wave.Error) as error:
        raise AppError(400, "无法读取虚拟讲解员所需的 PCM16 WAV 配音") from error
    active = sorted(value for value in measured if value > .0015)
    reference = max(.025, active[int((len(active) - 1) * .9)] if active else .025)
    levels = [0.0 if value <= .0015 else round(min(1.0, math.sqrt(value / reference)), 6)
              for value in measured]
    return {"sample_hz": sample_hz, "duration_seconds": duration, "levels": levels}


def build_host_composition(duration: float, aspect: str, envelope: dict, avatar_id=AVATAR_ID) -> str:
    """Return the editable transparent HyperFrames sub-composition for studio-bot.

    Landscape art occupies approximately x170..690/y320..870, portrait art
    x290..790/y350..930. The parent owns backgrounds, titles, captions and audio.
    Invalid envelopes are rejected rather than replaced by invented mouth motion.
    """
    duration = _duration(duration)
    if aspect not in {"16:9", "9:16"}:
        raise AppError(400, "虚拟讲解员视频比例须为 16:9 或 9:16")
    if avatar_id != AVATAR_ID:
        raise AppError(400, "未提供此虚拟讲解员；当前可选 studio-bot")
    if not isinstance(envelope, dict):
        raise AppError(400, "角色需要真实配音能量数据")
    sample_hz = _sample_rate(envelope.get("sample_hz"))
    audio_duration = _duration(envelope.get("duration_seconds"), "配音能量时长")
    if abs(audio_duration - duration) > .00001:
        raise AppError(400, "配音能量时长与视频时长不一致")
    levels = envelope.get("levels")
    expected = math.ceil(audio_duration * sample_hz - 1e-9)
    if not isinstance(levels, list) or len(levels) != expected:
        raise AppError(400, "配音能量数据长度与实际时长不一致")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or not 0 <= value <= 1 for value in levels):
        raise AppError(400, "配音能量须为 0 到 1 的有限数值")
    width, height = (1920, 1080) if aspect == "16:9" else (1080, 1920)
    values = {"WIDTH": str(width), "HEIGHT": str(height), "DURATION": f"{duration:.9f}",
              "LEFT": "160" if aspect == "16:9" else "270",
              "TOP": "310" if aspect == "16:9" else "350",
              "ENVELOPE": json.dumps({"sample_hz": sample_hz, "levels": levels}, separators=(",", ":"))}
    source = (Path(__file__).parent / "templates" / "virtual-host.html").read_text(encoding="utf-8")
    for key, value in values.items():
        source = source.replace("@@" + key + "@@", value)
    return source

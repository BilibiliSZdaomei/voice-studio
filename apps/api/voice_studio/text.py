"""Conservative segmentation: preserve spoken text, never invent a rewritten script."""
import math
import re

from .errors import AppError

MAX_SCRIPT = 30000
MAX_SEGMENT = 120


def clean_script(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SCRIPT:
        raise AppError(400, f"请提供 1–{MAX_SCRIPT} 字的文案")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise AppError(400, "文案包含无法朗读的控制字符")
    if not any(char.isalnum() for char in value):
        raise AppError(400, "文案至少应包含一个可朗读的字词")
    return value


def split_sentences(value):
    value = clean_script(value)
    segments = []
    for sentence in re.split(r"(?<=[。！？!?])|\n+", value):
        sentence = sentence.strip()
        while len(sentence) > MAX_SEGMENT:
            candidates = [index + 1 for index, char in enumerate(sentence[:MAX_SEGMENT])
                          if char in "，、,；;：: " and index >= MAX_SEGMENT // 3]
            end = candidates[-1] if candidates else MAX_SEGMENT
            chunk = sentence[:end].strip()
            if any(char.isalnum() for char in chunk):
                segments.append(chunk)
            sentence = sentence[end:].strip()
        if any(char.isalnum() for char in sentence):
            segments.append(sentence)
    return segments


def title_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise AppError(400, "项目名称需要 1–100 个字符")
    return value.strip()


def settings_value(value, previous=None):
    if not isinstance(value, dict) or set(value) - {"speed", "pause_seconds"}:
        raise AppError(400, "配音设置格式无效")
    result = dict(previous or {"speed": 1.0, "pause_seconds": .28})
    for key, low, high in (("speed", .5, 2.0), ("pause_seconds", 0.0, 2.0)):
        if key in value:
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
                raise AppError(400, f"{key} 需在 {low}–{high} 之间")
            result[key] = float(number)
    return result

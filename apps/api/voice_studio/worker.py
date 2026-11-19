"""Isolated local ZipVoice inference worker; progress is JSON Lines on stdout.

Run this module with the configured voice-runtime Python rather than the API
interpreter. The API supplies trusted, unique output paths in the request.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, allow_nan=False), flush=True)


def generate(request_path: Path) -> None:
    # Heavy dependencies are needed only by this process, not by the API server.
    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    config = request["config"]
    settings = request.get("settings", {})
    segments = request["segments"]
    if not isinstance(segments, list) or not segments:
        raise ValueError("配音任务没有可生成的片段。")
    speed = float(settings.get("speed", 1.0))
    if not np.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise ValueError("语速设置无效。")

    model_config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                encoder=str(config["encoder"]),
                decoder=str(config["decoder"]),
                tokens=str(config["tokens"]),
                lexicon=str(config["lexicon"]),
                vocoder=str(config["vocoder"]),
                guidance_scale=1.0,
            ),
            num_threads=int(config.get("num_threads", 4)),
            provider="cpu",
            debug=False,
        ),
    )
    if not model_config.validate():
        raise RuntimeError("本地声线模型配置校验未通过。")
    tts = sherpa_onnx.OfflineTts(model_config)
    reference, reference_rate = sf.read(str(config["reference_audio"]), dtype="float32")
    reference = np.asarray(reference, dtype=np.float32)
    if reference.ndim == 2:
        reference = reference.mean(axis=1)
    if reference.ndim != 1 or not reference.size or not np.isfinite(reference).all():
        raise ValueError("参考录音为空或含无效采样。")

    generation = sherpa_onnx.GenerationConfig()
    generation.reference_audio = reference
    generation.reference_sample_rate = int(reference_rate)
    generation.reference_text = str(config["reference_text"])
    generation.num_steps = 16
    generation.speed = speed
    native_chunks = {"min_words_in_sentence": "1000", "max_words_in_sentence": "1000"}
    # The binding returns a copy: assigning the whole map is required.
    generation.extra = dict(native_chunks)
    if generation.extra != native_chunks:
        raise RuntimeError("本地模型分句设置未生效。")

    for segment in segments:
        segment_id = str(segment["id"])
        text = str(segment["text"]).strip()
        output = Path(segment["output"])
        if not output.is_absolute():
            raise ValueError("片段输出必须使用绝对路径。")
        if not text or len(text) > 120:
            raise ValueError("每个片段需要包含 1–120 字的文案。")
        if output.exists():
            raise FileExistsError(f"保留已有配音文件，拒绝覆盖：{output}")
        emit("segment_start", id=segment_id)
        audio = tts.generate(text, generation)
        samples = np.array(audio.samples, dtype=np.float32, copy=True)
        rate = int(audio.sample_rate)
        if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all() or rate <= 0:
            raise RuntimeError(f"片段 {segment_id} 未生成有效音频。")
        fade = min(round(0.004 * rate), len(samples) // 2)
        if fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            samples[:fade] *= ramp
            samples[-fade:] *= ramp[::-1]
        peak = float(np.abs(samples).max())
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if peak <= 1e-6 or rms <= 1e-7:
            raise RuntimeError(f"片段 {segment_id} 仅生成了静音，请重新尝试。")
        gain = min(1.0, 0.98 / peak)
        samples *= gain
        peak = float(np.abs(samples).max())
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive file creation also protects against a collision after inference.
        with output.open("xb") as destination:
            sf.write(destination, samples, rate, format="WAV", subtype="PCM_16")
        emit(
            "segment_ready",
            id=segment_id,
            path=str(output),
            duration_seconds=float(len(samples) / rate),
            sample_rate=rate,
            peak=peak,
            rms=rms,
        )
    emit("complete")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Local ZipVoice JSONL worker")
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    try:
        generate(args.request)
        return 0
    except Exception as exc:
        emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Opt-in real inference check; creates ONE clearly labelled local demonstration project."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import wave
import zipfile
from pathlib import Path
from urllib.request import urlopen

from voice_cli import LocalClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))
from voice_studio.config import validate_data_path

SCRIPT = (
    "翻开图册，第一页画着一棵树。\n"
    "第二页是一只慢慢走过草地的乌龟。\n"
    "合上图册，把书放回架子上。"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--project-id", help="Continue a prior verification without rendering completed segments again")
    args = parser.parse_args()
    root = validate_data_path(args.evidence)
    root.mkdir(parents=True, exist_ok=True)
    client = LocalClient(args.url)
    status = client.request("GET", "/api/status")
    if not status["engine"]["available"]:
        raise RuntimeError(status["engine"]["message"])
    project = client.request("GET", "/api/projects/" + args.project_id) if args.project_id else client.request(
        "POST", "/api/projects", {"title": "示例｜我的配音工作台", "script": SCRIPT})
    pid = project["id"]
    evidence_path = root / "real-inference.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if args.project_id and evidence_path.exists() else {
        "project_id": pid, "checks": {}, "listening_acceptance": "pending_user_listening"}
    def save():
        (root / "real-inference.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    def current():
        return client.request("GET", f"/api/projects/{pid}")
    def wait(job, stop_after_first=False):
        deadline = time.monotonic() + 1800
        last = None
        while job["status"] in {"queued", "running", "cancelling"}:
            snapshot = (job["status"], job["completed"], job["total"])
            if snapshot != last:
                print(f"JOB {job['id']} {snapshot}", flush=True)
                last = snapshot
            if stop_after_first and job["completed"] >= 1 and job["completed"] < job["total"]:
                return job
            if time.monotonic() > deadline:
                raise TimeoutError("本地推理超过验证等待时间，任务保留以供检查")
            time.sleep(1)
            job = client.request("GET", "/api/jobs/" + job["id"])
        return job
    save()
    job = wait(client.request("POST", f"/api/projects/{pid}/generate"), True)
    if job["status"] in {"queued", "running"}:
        cancelled = client.request("POST", f"/api/jobs/{job['id']}/cancel")
        cancelled = wait(cancelled)
        if cancelled["status"] != "cancelled":
            raise AssertionError(cancelled)
        project = current()
        if not any(segment["status"] == "ready" for segment in project["segments"]):
            raise AssertionError("取消任务丢失了已完成片段")
        evidence["checks"]["cancel_preserves_completed_segments"] = True
        save()
        job = wait(client.request("POST", f"/api/projects/{pid}/generate"))
    if job["status"] != "completed":
        raise RuntimeError(str(job))
    project = current()
    if not all(segment["status"] == "ready" for segment in project["segments"]):
        raise AssertionError("整稿生成不完整")
    baseline = project.get("export") or client.request("POST", f"/api/projects/{pid}/export")
    project = current()
    old_urls = [segment["audio_url"] for segment in project["segments"]]
    changed = project["segments"][1]
    project = client.request("PATCH", f"/api/projects/{pid}/segments/{changed['id']}", {
        "text": "第二页是一只停在石头上的乌龟。", "expected_revision": project["revision"]})
    if project.get("export") or project["segments"][1]["status"] == "ready":
        raise AssertionError("编辑后旧音频或导出未失效")
    if project["segments"][0]["audio_url"] != old_urls[0] or project["segments"][2]["audio_url"] != old_urls[2]:
        raise AssertionError("单段修改影响了其他已完成片段")
    job = wait(client.request("POST", f"/api/projects/{pid}/segments/{changed['id']}/generate"))
    if job["status"] != "completed":
        raise RuntimeError(str(job))
    project = current()
    if project["segments"][1]["audio_url"] == old_urls[1]:
        raise AssertionError("重新生成覆盖旧版本路径")
    evidence["checks"]["local_edit_and_regenerate"] = True
    exported = client.request("POST", f"/api/projects/{pid}/export")
    evidence["export"] = exported
    if exported["wav_url"] == baseline["wav_url"]:
        raise AssertionError("新导出覆盖了旧导出")
    files = {}
    for key in ("wav_url", "mp3_url", "srt_url", "bundle_url"):
        if exported.get(key):
            with urlopen(args.url + exported[key], timeout=60) as response:
                data = response.read()
            files[key] = data
            evidence.setdefault("files", {})[key] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with wave.open(io.BytesIO(files["wav_url"]), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
        if abs(duration - exported["duration_seconds"]) > .02:
            raise AssertionError("导出时长与WAV不一致")
        evidence["wav"] = {"sample_rate": audio.getframerate(), "channels": audio.getnchannels(), "duration_seconds": duration}
    subtitles = files["srt_url"].decode("utf-8-sig")
    if subtitles.count(" --> ") != len(project["segments"]):
        raise AssertionError("段级SRT数量不一致")
    with zipfile.ZipFile(io.BytesIO(files["bundle_url"])) as archive:
        if archive.testzip() is not None:
            raise AssertionError("ZIP内容损坏")
        names = archive.namelist()
        if any(name.lower().endswith((".onnx", ".m4a")) or "reference" in name.lower() for name in names):
            raise AssertionError("工程ZIP错误包含了模型或参考录音")
        evidence["zip_entries"] = names
    evidence["checks"]["complete_export_and_real_duration_srt"] = True
    evidence["checks"]["historical_export_preserved"] = True
    previous = root / "source-assets-before.json"
    if previous.is_file():
        assets = json.loads(previous.read_text(encoding="utf-8-sig"))
        for asset in assets.values():
            if hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest() != asset["sha256"]:
                raise AssertionError("原参考录音或已验收样音被修改")
        evidence["checks"]["original_voice_assets_unchanged"] = True
    evidence["project"] = project
    evidence["result"] = "passed_machine_checks_listening_pending"
    save()
    print(json.dumps({"project_id": pid, "checks": evidence["checks"], "wav": evidence["wav"], "export": exported}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

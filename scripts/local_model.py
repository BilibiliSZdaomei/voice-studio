"""Start/stop only the optional model process owned by this project; no downloads."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))
from voice_studio.config import load_config, validate_data_path
from voice_studio.data_lock import DataRootLock
from voice_studio.errors import AppError


def identity(pid):
    if os.name != "nt":
        raise RuntimeError("受管模型启动器目前仅用于本机 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {"executable": str(Path(buffer.value).resolve()), "created_filetime": created}
    finally:
        kernel.CloseHandle(handle)


def owned(state):
    if not state or state.get("owner") != "voice-studio-local-model":
        return False
    actual = identity(state["pid"])
    return bool(actual and actual["executable"].casefold() == state["executable"].casefold()
                and actual["created_filetime"] == state["created_filetime"])


def health(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return response.status == 200
    except OSError as error:
        if hasattr(error, "close"):
            error.close()
        return False


def start(settings, managed, folder, state):
    port = int(managed.get("port", 11435))
    if not 1024 <= port <= 65535:
        raise ValueError("模型端口无效")
    if health(port):
        with urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as response:
            models = json.load(response)
        if settings["model"] not in {item.get("id") for item in models.get("data", [])}:
            raise RuntimeError("模型端口已由其他服务使用，未改动该服务")
        return {"status": "already_running" if owned(state) else "external_running", "port": port}
    if state and owned(state):
        for _ in range(30):
            if health(port):
                return {"status": "running", "port": port, "pid": state["pid"]}
            time.sleep(.5)
        raise RuntimeError("既有模型进程尚未就绪，请查看外部模型日志")
    executable = Path(managed["executable"]).resolve()
    model = Path(managed["model_path"]).resolve()
    if not executable.is_file() or not model.is_file():
        raise RuntimeError("受管模型工具或权重不存在；启动器不会自动下载")
    with socket.socket() as probe:
        probe.settimeout(.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("模型端口已被占用，未停止其他程序")
    args = [str(executable), "--model", str(model), "--alias", settings["model"], "--host", "127.0.0.1",
            "--port", str(port), "--threads", str(managed.get("threads", 4)),
            "--ctx-size", str(managed.get("context_size", 2048)), "--parallel", "1",
            "--sleep-idle-seconds", str(managed.get("sleep_idle_seconds", 90)),
            "--cache-ram", "0", "--batch-size", "128", "--ubatch-size", "64", "--poll", "0",
            "--reasoning", "off", "--reasoning-budget", "0", "--jinja", "--offline", "--no-webui"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out, err = folder / (stamp + ".stdout.log"), folder / (stamp + ".stderr.log")
    with out.open("wb") as stdout, err.open("wb") as stderr:
        process = subprocess.Popen(args, cwd=str(executable.parent), stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    details = identity(process.pid)
    if details is None:
        raise RuntimeError("模型进程启动失败，请查看外部日志")
    state = {"owner": "voice-studio-local-model", "pid": process.pid, "port": port,
             **details, "model": str(model), "arguments": args, "stdout": str(out), "stderr": str(err)}
    (folder / "managed-process.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(40):
        if health(port):
            return {"status": "started", "port": port, "pid": process.pid}
        if process.poll() is not None:
            raise RuntimeError("模型进程退出，请查看日志：" + str(err))
        time.sleep(.5)
    return {"status": "warming", "port": port, "pid": process.pid, "message": "模型仍在加载；其他工作台功能可以先使用"}


def stop(state):
    if not owned(state):
        return {"status": "not_owned_or_not_running"}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001, False, int(state["pid"]))
    if not handle:
        raise RuntimeError("无法停止本项目的模型进程")
    try:
        if not kernel.TerminateProcess(handle, 0):
            raise RuntimeError("模型进程停止未完成")
    finally:
        kernel.CloseHandle(handle)
    return {"status": "stopped", "pid": state["pid"]}


def main():
    parser = argparse.ArgumentParser(description="声稿受管本地模型启停")
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--config")
    args = parser.parse_args()
    settings = load_config(args.config).get("rewrite", {})
    managed = settings.get("managed")
    if not settings.get("enabled") or not isinstance(managed, dict):
        print(json.dumps({"status": "not_managed"}))
        return
    folder = validate_data_path(Path(managed["runtime_dir"]))
    folder.mkdir(parents=True, exist_ok=True)
    guard = DataRootLock(folder)
    try:
        path = folder / "managed-process.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        result = start(settings, managed, folder, state) if args.action == "start" else stop(state)
    finally:
        guard.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, AppError) as error:
        print("本地改写模型：" + str(error), file=sys.stderr)
        sys.exit(1)

"""Real scheduler/lifecycle tests with Event-controlled synthetic workers, not speech."""
import json
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_studio.config import validate_data_path
from voice_studio.errors import AppError
from voice_studio.service import StudioService


def synthetic_pcm(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 2400)


class SyntheticProcess:
    """Only emits worker protocol fixtures; never starts a subprocess or model."""
    def __init__(self, request, factory):
        self.request = request
        self.factory = factory
        self.gate = threading.Event()
        self.returncode = None
        self.terminated = False
        self.stdout = self.events()
        if factory.auto_release:
            self.gate.set()

    def events(self):
        for target in self.request["segments"]:
            yield json.dumps({"event": "segment_start", "id": target["id"]}) + "\n"
            if not self.gate.wait(timeout=5):
                raise RuntimeError("Synthetic worker release timed out")
            if self.terminated:
                return
            output = Path(target["output"])
            synthetic_pcm(output)
            yield json.dumps({"event": "segment_ready", "id": target["id"],
                              "path": str(output), "sample_rate": 24000}) + "\n"
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.gate.set()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise RuntimeError("Synthetic worker waited before consuming its events")
        return self.returncode


class WorkerFactory:
    def __init__(self):
        self.processes = []
        self.condition = threading.Condition()
        self.auto_release = False

    def __call__(self, command, **kwargs):
        request = json.loads(Path(command[command.index("--request") + 1]).read_text("utf-8"))
        with self.condition:
            process = SyntheticProcess(request, self)
            self.processes.append(process)
            self.condition.notify_all()
            return process

    def wait_for_count(self, count):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.processes) >= count, timeout=5)

    def finish_all(self):
        with self.condition:
            self.auto_release = True
            for process in self.processes:
                process.gate.set()


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        base = validate_data_path(Path(base))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="job-queue-")
        self.root = Path(self.temp.name)
        reference = self.root / "synthetic-reference.wav"
        synthetic_pcm(reference)
        self.config = {"data_root": str(self.root / "data"), "reference_text": "合成测试参考。"}
        self.config.update({key: str(reference) for key in
                            ("reference_audio", "runtime_python", "encoder", "decoder", "tokens", "vocoder", "lexicon")})
        self.factory = WorkerFactory()
        self.patcher = patch("voice_studio.service.subprocess.Popen", side_effect=self.factory)
        self.patcher.start()
        self.service = StudioService(self.config)

    def tearDown(self):
        try:
            self.factory.finish_all()
            thread = self.service._thread
            if thread:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "Synthetic queue did not release")
            self.service.close()
        finally:
            self.patcher.stop()
            self.temp.cleanup()

    def project(self, title):
        return self.service.create_project({"title": title, "script": "测试一句话。"})

    def wait_job(self, jid, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.get_job(jid)
            if job["status"] == status:
                return job
            threading.Event().wait(.01)
        self.fail(f"Expected {status}; got {self.service.get_job(jid)}")

    def test_fifo_accepts_multiple_projects_and_locks_queued_project(self):
        projects = [self.project(str(index)) for index in range(3)]
        jobs = [self.service.generate(projects[0]["id"])]
        self.assertTrue(self.factory.wait_for_count(1))
        jobs.extend(self.service.generate(project["id"]) for project in projects[1:])
        self.assertEqual([self.service.get_job(job["id"])["status"] for job in jobs], ["running", "queued", "queued"])
        queued = self.service.get_project(projects[1]["id"])
        with self.assertRaises(AppError) as caught:
            self.service.update_project(queued["id"], {"title": "不可修改", "expected_revision": queued["revision"]})
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(AppError):
            self.service.generate(queued["id"])
        self.factory.processes[0].gate.set()
        self.assertTrue(self.factory.wait_for_count(2))
        self.assertEqual(self.factory.processes[1].request["segments"][0]["id"], projects[1]["segments"][0]["id"])
        self.factory.processes[1].gate.set()
        self.assertTrue(self.factory.wait_for_count(3))
        self.assertEqual(self.factory.processes[2].request["segments"][0]["id"], projects[2]["segments"][0]["id"])
        self.factory.processes[2].gate.set()
        for job in jobs:
            self.wait_job(job["id"], "completed")

    def test_cancel_queued_restores_snapshot_and_does_not_cancel_active(self):
        projects = [self.project(str(index)) for index in range(3)]
        active = self.service.generate(projects[0]["id"])
        self.assertTrue(self.factory.wait_for_count(1))
        before = self.service.get_project(projects[1]["id"])["segments"]
        cancelled = self.service.generate(projects[1]["id"])
        following = self.service.generate(projects[2]["id"])
        self.assertEqual(self.service.cancel_job(cancelled["id"])["status"], "cancelled")
        self.assertEqual(self.service.get_project(projects[1]["id"])["segments"], before)
        self.assertFalse(self.service._cancel.is_set())
        self.assertFalse(self.factory.processes[0].terminated)
        self.assertEqual(self.service.get_job(active["id"])["status"], "running")
        self.factory.processes[0].gate.set()
        self.assertTrue(self.factory.wait_for_count(2))
        self.assertEqual(self.factory.processes[1].request["segments"][0]["id"], projects[2]["segments"][0]["id"])
        self.factory.processes[1].gate.set()
        self.wait_job(following["id"], "completed")
        self.assertEqual(len(self.factory.processes), 2)

    def test_close_keeps_queued_jobs_and_restart_resumes_in_fifo_order(self):
        projects = [self.project(str(index)) for index in range(3)]
        active = self.service.generate(projects[0]["id"])
        self.assertTrue(self.factory.wait_for_count(1))
        queued = [self.service.generate(project["id"]) for project in projects[1:]]
        self.service.close()
        self.assertEqual(self.service.get_job(active["id"])["status"], "cancelled")
        self.assertEqual([self.service.get_job(job["id"])["status"] for job in queued], ["queued", "queued"])
        self.service = StudioService(self.config)
        self.assertTrue(self.factory.wait_for_count(2))
        self.assertEqual(self.factory.processes[1].request["segments"][0]["id"], projects[1]["segments"][0]["id"])
        self.factory.processes[1].gate.set()
        self.assertTrue(self.factory.wait_for_count(3))
        self.assertEqual(self.factory.processes[2].request["segments"][0]["id"], projects[2]["segments"][0]["id"])
        self.factory.processes[2].gate.set()
        for job in queued:
            self.wait_job(job["id"], "completed")

    def test_running_regeneration_cancel_preserves_previous_ready_audio(self):
        project = self.project("保留原音频")
        path = self.service.root / "fixture" / "accepted.wav"
        synthetic_pcm(path)
        project["segments"][0].update(status="ready", audio_url=self.service._url(path), duration_seconds=.1,
                                       engine={"name": "synthetic prior version"})
        self.service._save(project)
        previous = self.service.get_project(project["id"])["segments"][0]
        original_bytes = path.read_bytes()
        job = self.service.generate(project["id"], previous["id"])
        self.assertTrue(self.factory.wait_for_count(1))
        self.service.cancel_job(job["id"])
        self.wait_job(job["id"], "cancelled")
        self.assertEqual(self.service.get_project(project["id"])["segments"][0], previous)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_cancel_during_dequeue_to_running_transition_cannot_leak_to_next_job(self):
        first, second = self.project("取消交接"), self.project("继续后续")
        entered, release = threading.Event(), threading.Event()
        original_run = self.service._run_job

        def delayed_run(jid):
            if not entered.is_set():
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("Synthetic dequeue transition timed out")
            original_run(jid)

        with patch.object(self.service, "_run_job", side_effect=delayed_run):
            try:
                cancelled = self.service.generate(first["id"])
                self.assertTrue(entered.wait(timeout=5))
                following = self.service.generate(second["id"])
                self.service.cancel_job(cancelled["id"])
                release.set()
                self.wait_job(cancelled["id"], "cancelled")
                self.assertTrue(self.factory.wait_for_count(1))
                self.assertEqual(self.factory.processes[0].request["segments"][0]["id"], second["segments"][0]["id"])
                self.assertFalse(self.factory.processes[0].terminated)
                self.factory.processes[0].gate.set()
                self.wait_job(following["id"], "completed")
            finally:
                release.set()

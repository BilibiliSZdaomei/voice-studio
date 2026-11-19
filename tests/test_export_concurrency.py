"""Export concurrency regression tests using disposable synthetic audio only."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from voice_studio.config import validate_data_path
from voice_studio.errors import AppError
from voice_studio.service import StudioService


class ExportConcurrencyTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        base = validate_data_path(Path(base))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="export-concurrency-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = StudioService({"data_root": str(self.root)})
        self.addCleanup(self.service.close)
        self.project = self.service.create_project({"title": "并发导出测试", "script": "原来的第一句。第二句保持不变。"})
        for index, segment in enumerate(self.project["segments"]):
            path = self.root / "synthetic-audio" / f"{index}.wav"
            path.parent.mkdir(exist_ok=True)
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x01\0" * 2400)
            segment.update(status="ready", audio_url=self.service._url(path), duration_seconds=0.1)
        self.service._save(self.project)

    def test_edit_during_conversion_stays_responsive_and_rejects_stale_export(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        outcome = {}

        def fake_ffmpeg(command, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Timed out waiting to release synthetic conversion")
            Path(command[-1]).write_bytes(b"synthetic MP3 fixture")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def export():
            try:
                outcome["result"] = self.service.export_project(self.project["id"])
            except Exception as error:
                outcome["error"] = error
            finally:
                finished.set()

        thread = threading.Thread(target=export, daemon=True)
        # A regression must fail within five seconds instead of deadlocking
        # the main thread on status(), get_project() or update_segment().
        watchdog = threading.Timer(5, release.set)
        watchdog.daemon = True
        with patch("voice_studio.service.shutil.which", return_value="synthetic-ffmpeg"), \
                patch("voice_studio.service.subprocess.run", side_effect=fake_ffmpeg):
            try:
                watchdog.start()
                thread.start()
                self.assertTrue(entered.wait(timeout=5), "Export did not reach conversion")
                self.assertIsNone(self.service.status()["active_job"])
                current = self.service.get_project(self.project["id"])
                edited = self.service.update_segment(current["id"], current["segments"][0]["id"], {
                    "text": "导出期间修改的新文案。", "expected_revision": current["revision"],
                })
                self.assertFalse(release.is_set(), "Project operations waited for conversion to release its lock")
                self.assertFalse(finished.is_set(), "Conversion should still be blocked")
            finally:
                release.set()
                watchdog.cancel()
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "Export thread did not stop")
        self.assertNotIn("result", outcome)
        self.assertIsInstance(outcome.get("error"), AppError)
        self.assertEqual(outcome["error"].status, 409)
        current = self.service.get_project(self.project["id"])
        self.assertEqual(current["revision"], edited["revision"])
        self.assertEqual(current["segments"][0]["text"], "导出期间修改的新文案。")
        self.assertEqual(current["segments"][0]["status"], "pending")
        self.assertIsNone(current["export"])

        bundles = list((self.root / "exports" / current["id"]).glob("*/project.zip"))
        self.assertEqual(len(bundles), 1, "Unregistered completed files should be retained")
        with zipfile.ZipFile(bundles[0]) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("narration.mp3"), b"synthetic MP3 fixture")
            snapshot = json.loads(archive.read("project.json"))
        self.assertEqual(snapshot["segments"][0]["text"], "原来的第一句。")

    def test_failed_conversion_preserves_previous_export_and_revision(self):
        with patch("voice_studio.service.shutil.which", return_value=None):
            previous = self.service.export_project(self.project["id"])
        before = self.service.get_project(self.project["id"])
        failure = subprocess.CompletedProcess(["synthetic-ffmpeg"], 1, b"", b"synthetic conversion failure")
        with patch("voice_studio.service.shutil.which", return_value="synthetic-ffmpeg"), \
                patch("voice_studio.service.subprocess.run", return_value=failure):
            with self.assertRaises(AppError) as raised:
                self.service.export_project(self.project["id"])
        self.assertEqual(raised.exception.status, 500)
        current = self.service.get_project(self.project["id"])
        self.assertEqual(current["revision"], before["revision"])
        self.assertEqual(current["export"], previous)
        self.assertTrue(self.service._asset(previous["bundle_url"]).is_file())


if __name__ == "__main__":
    unittest.main()

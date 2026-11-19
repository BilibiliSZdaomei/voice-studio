"""Domain tests with synthetic PCM fixtures; no mocked audio is shipped as narration."""
import json
import os
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from voice_studio.errors import AppError
from voice_studio.service import StudioService, atomic_json


class ProjectTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="domain-")
        self.root = Path(self.temp.name)
        self.config = {"data_root": str(self.root)}
        self.service = StudioService(self.config)
        self.project = self.service.create_project({"title": "测试项目", "script": "第一句话。第二句话。"})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def update(self, payload):
        payload["expected_revision"] = self.project["revision"]
        self.project = self.service.update_project(self.project["id"], payload)
        return self.project

    def make_ready(self):
        for index, segment in enumerate(self.project["segments"]):
            path = self.root / "fixture-audio" / f"{index}.wav"
            path.parent.mkdir(exist_ok=True)
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x01\0" * 2400)
            segment.update(status="ready", audio_url=self.service._url(path), duration_seconds=.1)
        self.service._save(self.project)

    def test_persistence_and_revision_conflict(self):
        self.update({"title": "修改后的名称"})
        with self.assertRaises(AppError) as raised:
            self.service.update_project(self.project["id"], {"title": "旧页面", "expected_revision": 1})
        self.assertEqual(raised.exception.status, 409)
        self.service.close()
        another = StudioService(self.config)
        try:
            self.assertEqual(another.get_project(self.project["id"])["title"], "修改后的名称")
        finally:
            another.close()

    def test_local_edit_invalidates_one_segment(self):
        self.make_ready()
        unchanged = dict(self.project["segments"][1])
        first = self.project["segments"][0]
        updated = self.service.update_segment(self.project["id"], first["id"],
            {"text": "仅修改第一句。", "expected_revision": self.project["revision"]})
        self.assertEqual(updated["segments"][1], unchanged)
        self.assertEqual(updated["segments"][0]["status"], "pending")
        self.assertIsNone(updated["segments"][0]["audio_url"])
        self.assertIn("仅修改第一句。", updated["script"])

    def test_script_reuses_unchanged_audio_even_with_duplicate_sentences(self):
        self.make_ready()
        before = self.project["segments"][0]["id"]
        updated = self.update({"script": "第一句话。第一句话。第二句话。"})
        self.assertEqual(updated["segments"][0]["id"], before)
        self.assertNotEqual(updated["segments"][1]["id"], before)
        self.assertEqual(updated["segments"][1]["status"], "pending")
        self.assertEqual(updated["segments"][2]["status"], "ready")

    def test_speed_invalidates_audio_but_pause_does_not(self):
        self.make_ready()
        self.update({"settings": {"pause_seconds": .4}})
        self.assertTrue(all(item["status"] == "ready" for item in self.project["segments"]))
        self.update({"settings": {"speed": 1.1}})
        self.assertTrue(all(item["status"] == "pending" for item in self.project["segments"]))

    def test_incomplete_export_rejected(self):
        with self.assertRaises(AppError):
            self.service.export_project(self.project["id"])

    def test_export_duration_srt_and_version_preservation(self):
        self.make_ready()
        with patch("voice_studio.service.shutil.which", return_value=None):
            result = self.service.export_project(self.project["id"])
            another = self.service.export_project(self.project["id"])
        self.assertAlmostEqual(result["duration_seconds"], .48)
        self.assertNotEqual(result["wav_url"], another["wav_url"])
        path = self.service._asset(result["wav_url"])
        with wave.open(str(path)) as audio:
            self.assertEqual(audio.getnframes(), round(.48 * 24000))
        srt = self.service._asset(result["srt_url"]).read_text(encoding="utf-8")
        self.assertIn("00:00:00,380 --> 00:00:00,480", srt)
        with zipfile.ZipFile(self.service._asset(result["bundle_url"])) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("segments/0001.wav", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["subtitle_alignment"], "segment_level")

    def test_busy_project_rejects_edit_and_restart_recovers(self):
        self.project["segments"][0]["status"] = "generating"
        self.service._save(self.project)
        jid = "a" * 32
        atomic_json(self.root / "jobs" / f"{jid}.json", {"id": jid, "project_id": self.project["id"], "status": "running"})
        self.service._active_id = jid
        with self.assertRaises(AppError) as raised:
            self.update({"title": "不应覆盖"})
        self.assertEqual(raised.exception.status, 409)
        self.service._active_id = None
        self.service.close()
        another = StudioService(self.config)
        try:
            self.assertEqual(another.get_project(self.project["id"])["segments"][0]["status"], "pending")
            self.assertEqual(another.get_job(jid)["status"], "failed")
        finally:
            another.close()

    def test_same_data_root_has_one_owner(self):
        with self.assertRaises(AppError) as raised:
            StudioService(self.config)
        self.assertEqual(raised.exception.status, 409)

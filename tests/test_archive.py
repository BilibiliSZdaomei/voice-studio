"""Archive roundtrip and hostile ZIP tests; all media is synthetic PCM."""
import io
import json
import os
import stat
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from voice_studio.archive import restore_archive
from voice_studio.errors import AppError
from voice_studio.service import StudioService


def pcm(channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((channels, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 2400 * channels)
    return buffer.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="archive-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.project = {"id": "a" * 32, "title": "工程测试", "script": "第一句话。第二句话。", "revision": 9,
                        "settings": {"speed": 1.0, "pause_seconds": .28}, "segments": [
                            {"id": "b" * 32, "text": "第一句话。", "status": "ready", "duration_seconds": .1,
                             "audio_url": "/files/../../unsafe.wav"},
                            {"id": "c" * 32, "text": "第二句话。", "status": "ready", "duration_seconds": .1}],
                        "export": {"wav_url": "unsafe"}}
        self.manifest = {"app_version": "0.1.0", "timeline": [
            {"id": "b" * 32, "text": "第一句话。", "start_seconds": 0, "end_seconds": .1},
            {"id": "c" * 32, "text": "第二句话。", "start_seconds": .38, "end_seconds": .48}]}

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, additional=None, exclude=(), project=None, manifest=None):
        path = self.root / "fixture.zip"
        entries = {"project.json": json.dumps(project or self.project, ensure_ascii=False).encode(),
                   "manifest.json": json.dumps(manifest or self.manifest, ensure_ascii=False).encode(),
                   "script.txt": (project or self.project)["script"].encode(),
                   "segments/0001.wav": pcm(), "segments/0002.wav": pcm(),
                   "narration.wav": b"ignored export asset", "subtitles.srt": b"ignored"}
        entries.update(additional or {})
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                if name not in exclude:
                    archive.writestr(name, data)
        return path

    def assert_rejected_without_project(self, path):
        with self.assertRaises(AppError) as caught:
            restore_archive(path, self.data)
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(list((self.data / "projects").glob("*/project.json")))
        self.assertFalse(list((self.data / "staging").glob("restore-*")))

    def test_v1_fixture_creates_fresh_identity_paths_and_preserves_source(self):
        archive = self.bundle()
        original = archive.read_bytes()
        project = restore_archive(archive, self.data)
        another = restore_archive(archive, self.data)
        self.assertNotEqual(project["id"], self.project["id"])
        self.assertNotEqual(project["id"], another["id"])
        self.assertEqual(project["title"], "工程测试（恢复）")
        self.assertEqual(project["revision"], 1)
        self.assertIsNone(project["export"])
        self.assertEqual(project["voice_id"], "default")
        self.assertEqual(project["script"], self.project["script"])
        for old, new in zip(self.project["segments"], project["segments"]):
            self.assertNotEqual(old["id"], new["id"])
            self.assertEqual(new["status"], "ready")
            audio = self.data / new["audio_url"].removeprefix("/files/")
            self.assertTrue(audio.resolve().is_relative_to(self.data.resolve() / "projects" / project["id"]))
            self.assertEqual(audio.read_bytes(), pcm())
        self.assertEqual(archive.read_bytes(), original)

    def test_real_service_export_roundtrip(self):
        service = StudioService({"data_root": str(self.root / "original")})
        try:
            project = service.create_project({"title": "真实导出路径", "script": "完整一句话。"})
            segment = project["segments"][0]
            audio = service.root / "fixture.wav"
            audio.write_bytes(pcm())
            segment.update(status="ready", audio_url=service._url(audio), duration_seconds=.1)
            service._save(project)
            with patch("voice_studio.service.shutil.which", return_value=None):
                exported = service.export_project(project["id"])
            restored = restore_archive(service._asset(exported["bundle_url"]), self.data)
            self.assertEqual(restored["script"], project["script"])
            self.assertEqual(restored["segments"][0]["status"], "ready")
            reopened = StudioService({"data_root": str(self.data)})
            try:
                persisted = reopened.get_project(restored["id"])
                for key in ("id", "script", "segments", "settings", "revision"):
                    self.assertEqual(persisted[key], restored[key])
                with patch("voice_studio.service.shutil.which", return_value=None):
                    self.assertAlmostEqual(reopened.export_project(restored["id"])["duration_seconds"], .1)
            finally:
                reopened.close()
        finally:
            service.close()

    def test_windows_script_line_endings_match_project_without_changing_script(self):
        self.project["script"] = "第一句话。\n第二句话。"
        restored = restore_archive(self.bundle({"script.txt": "第一句话。\r\n第二句话。".encode()}), self.data)
        self.assertEqual(restored["script"], self.project["script"])

    def test_missing_or_mismatched_audio_is_explicitly_pending(self):
        project = restore_archive(self.bundle(exclude={"segments/0002.wav"}), self.data)
        self.assertEqual([segment["status"] for segment in project["segments"]], ["ready", "pending"])
        self.assertIn("缺少", project["segments"][1]["error"])
        self.manifest["timeline"][0]["text"] = "错误文字。"
        self.manifest["timeline"][1]["end_seconds"] = .9
        project = restore_archive(self.bundle(), self.data)
        self.assertTrue(all(segment["status"] == "pending" for segment in project["segments"]))
        self.assertTrue(all(segment["audio_url"] is None and segment["error"] for segment in project["segments"]))

    def test_voice_and_dictionary_snapshot_preserved(self):
        self.project["voice_id"] = "e" * 32
        self.project["dictionary_snapshot"] = {"revision": 3, "entries": [
            {"id": "dhl", "term": "DHL", "spoken": "敦豪", "enabled": True}]}
        restored = restore_archive(self.bundle(), self.data)
        self.assertEqual(restored["voice_id"], self.project["voice_id"])
        self.assertEqual(restored["dictionary_snapshot"], self.project["dictionary_snapshot"])

    def test_restore_freezes_pronunciation_even_for_missing_audio(self):
        self.project["dictionary_snapshot"] = {"revision": 3, "entries": [
            {"id": "phrase", "term": "句话", "spoken": "段话", "enabled": True}]}
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.project["dictionary_enabled"] = enabled
                expected = [segment["text"].replace("句话", "段话") if enabled else segment["text"]
                            for segment in self.project["segments"]]
                for segment, spoken in zip(self.project["segments"], expected):
                    segment["spoken_text"] = spoken
                restored = restore_archive(self.bundle(exclude={"segments/0002.wav"}), self.data)
                self.assertIs(restored["dictionary_enabled"], enabled)
                self.assertEqual([s["spoken_text"] for s in restored["segments"]], expected)
                self.assertEqual([s["status"] for s in restored["segments"]], ["ready", "pending"])

    def test_archived_audio_with_inconsistent_reading_needs_regeneration(self):
        self.project["segments"][0]["spoken_text"] = "另一条录音的读法。"
        restored = restore_archive(self.bundle(), self.data)
        self.assertEqual(restored["segments"][0]["status"], "pending")
        self.assertEqual(restored["segments"][0]["spoken_text"], self.project["segments"][0]["text"])
        self.assertEqual(restored["segments"][1]["status"], "ready")

    def test_rejects_traversal_absolute_backslash_and_duplicate_paths(self):
        for name in ("../outside.txt", "/absolute.txt", "C:/outside.txt", "a/../bad", "PROJECT.JSON"):
            with self.subTest(name=name):
                self.assert_rejected_without_project(self.bundle({name: b"hostile"}))
        # Windows ZipInfo normalizes backslashes on creation; replace both ZIP
        # directory and local-header names to retain the hostile on-disk name.
        path = self.bundle({"segments/bad.wav": b"hostile"})
        path.write_bytes(path.read_bytes().replace(b"segments/bad.wav", b"segments\\bad.wav"))
        self.assert_rejected_without_project(path)
        path = self.bundle()
        with zipfile.ZipFile(path, "a") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("project.json", b"duplicate")
        self.assert_rejected_without_project(path)

    def test_rejects_symlink_members_and_zip_bomb(self):
        path = self.bundle()
        info = zipfile.ZipInfo("symlink")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(info, "../../outside")
        self.assert_rejected_without_project(path)
        self.assert_rejected_without_project(self.bundle({"large.txt": b"0" * 1024 * 1024}))

    def test_rejects_member_limit_invalid_pcm_and_corrupt_zip(self):
        self.assert_rejected_without_project(self.bundle({f"unused/{index}": b"x" for index in range(1001)}))
        for value in (pcm(channels=2), b"not wav", pcm()[:-2]):
            self.assert_rejected_without_project(self.bundle({"segments/0001.wav": value}))
        path = self.root / "broken.zip"
        path.write_bytes(b"not a ZIP")
        self.assert_rejected_without_project(path)

    def test_rejects_inconsistent_script_invalid_settings_and_dictionary(self):
        self.assert_rejected_without_project(self.bundle({"script.txt": "不同文案。".encode()}))
        self.project["settings"]["speed"] = 10
        self.assert_rejected_without_project(self.bundle())
        self.project["settings"]["speed"] = 1
        self.project["dictionary_snapshot"] = [{"term": "DHL", "spoken": "<bad>"}]
        self.assert_rejected_without_project(self.bundle())

    def test_missing_text_segment_does_not_silently_drop_script(self):
        self.project["segments"].pop()
        self.manifest["timeline"].pop()
        self.assert_rejected_without_project(self.bundle(exclude={"segments/0002.wav"}))

    def test_write_failure_removes_only_new_stage_preserves_existing_project(self):
        existing = self.data / "projects" / ("f" * 32) / "project.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("user source", encoding="utf-8")
        unrelated = self.data / "staging" / "unrelated"
        unrelated.mkdir(parents=True)
        (unrelated / "source.txt").write_text("keep", encoding="utf-8")
        path = self.bundle()
        with patch("voice_studio.archive.Path.write_bytes", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(AppError) as caught:
                restore_archive(path, self.data)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(existing.read_text("utf-8"), "user source")
        self.assertTrue((unrelated / "source.txt").is_file())
        self.assertFalse(list((self.data / "staging").glob("restore-*")))

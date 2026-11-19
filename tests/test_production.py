"""Production workflow regressions; synthetic audio and local proposal fixtures only."""
import json
import os
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from voice_studio.config import validate_data_path
from voice_studio.errors import AppError
from voice_studio.service import StudioService


class ProductionTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        base = validate_data_path(Path(base))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="production-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "reference.wav"
        self.make_pcm(self.reference)
        self.service = StudioService({"data_root": str(self.root / "data"), "reference_audio": str(self.reference),
                                      "reference_text": "默认本机参考。"})
        self.addCleanup(self.service.close)

    @staticmethod
    def make_pcm(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 2400)

    def project(self, script="DHL 已到货。第二句保持原样。"):
        return self.service.create_project({"title": "生产回归", "script": script})

    def ready(self, project):
        for segment in project["segments"]:
            audio = self.service.root / "synthetic" / (segment["id"] + ".wav")
            self.make_pcm(audio)
            segment.update(status="ready", audio_url=self.service._url(audio), duration_seconds=.1)
        project.update(export={"fixture": "prior export"}, video={"fixture": "prior video"})
        self.service._save(project)
        return self.service.get_project(project["id"])

    def test_global_dictionary_changes_require_explicit_project_application(self):
        original = self.ready(self.project())
        dictionary = self.service.save_dictionary({"expected_revision": 0,
                                                  "entries": [{"term": "DHL", "spoken": "迪艾尺艾尔"}]})
        unchanged = self.service.get_project(original["id"])
        self.assertEqual(unchanged, original)
        preview = self.service.dictionary_preview(original["id"])
        self.assertIn("迪艾尺艾尔", preview["segments"][0]["spoken_text"])
        self.assertEqual(self.service.get_project(original["id"]), original)
        updated = self.service.apply_project_dictionary(original["id"], {"expected_revision": original["revision"]})
        self.assertEqual(updated["script"], original["script"])
        self.assertEqual(updated["dictionary_snapshot"], dictionary)
        self.assertEqual(updated["segments"][0]["status"], "pending")
        self.assertEqual(updated["segments"][1], original["segments"][1])
        self.assertIsNone(updated["export"])
        self.assertIsNone(updated["video"])
        self.assertTrue(self.service._asset(original["segments"][0]["audio_url"]).is_file())
        newest = self.project()
        self.assertEqual(newest["dictionary_snapshot"], dictionary)
        self.assertIn("迪艾尺艾尔", newest["segments"][0]["spoken_text"])

    def test_dictionary_with_no_spoken_change_preserves_existing_media(self):
        original = self.ready(self.project())
        self.service.save_dictionary({"expected_revision": 0, "entries": [{"term": "不存在的词", "spoken": "测试"}]})
        updated = self.service.apply_project_dictionary(original["id"], {"expected_revision": original["revision"]})
        for key in ("segments", "script", "export", "video"):
            self.assertEqual(updated[key], original[key])

    def test_voice_switch_invalidates_all_segments_and_retains_old_files(self):
        original = self.ready(self.project())
        voice = self.service.save_voice({"name": "新登记声线", "reference_audio": str(self.reference),
                                         "reference_text": "新的参考文案。"})
        updated = self.service.update_project(original["id"], {"expected_revision": original["revision"], "voice_id": voice["id"]})
        self.assertEqual(updated["voice_snapshot"], voice)
        self.assertTrue(all(segment["status"] == "pending" and segment["audio_url"] is None for segment in updated["segments"]))
        self.assertIsNone(updated["export"])
        self.assertIsNone(updated["video"])
        for segment in original["segments"]:
            self.assertTrue(self.service._asset(segment["audio_url"]).is_file())

    def test_batch_rejects_bad_text_and_voice_without_partial_projects(self):
        for bad in ({"title": "坏稿", "script": ""}, {"title": "坏声线", "script": "正常。", "voice_id": "e" * 32}):
            with self.subTest(bad=bad), self.assertRaises(AppError):
                self.service.create_batch({"projects": [{"title": "有效第一篇", "script": "第一篇。"}, bad]})
            self.assertEqual(self.service.list_projects(), [])
            self.assertEqual(self.service.list_jobs(), [])

    def test_batch_dictionary_expansion_failure_does_not_partially_create_projects(self):
        self.service.save_dictionary({"expected_revision": 0, "entries": [{"term": "A", "spoken": "甲" * 100}]})
        with self.assertRaises(AppError) as caught:
            self.service.create_batch({"projects": [{"title": "有效第一篇", "script": "第一篇。"},
                                                     {"title": "词典展开超限", "script": "AA。"}]})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.service.list_projects(), [], "A rejected batch must not leave its first project behind")
        self.assertEqual(self.service.list_jobs(), [])

    def test_batch_generate_without_engine_creates_nothing(self):
        with self.assertRaises(AppError) as caught:
            self.service.create_batch({"generate": True, "projects": [{"title": "正常", "script": "一篇文案。"}]})
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.service.list_projects(), [])

    def test_import_discards_untrusted_voice_paths_and_binds_local_default(self):
        original = self.ready(self.project("工程导入测试。"))
        with patch("voice_studio.service.shutil.which", return_value=None):
            exported = self.service.export_project(original["id"])
        source = self.service._asset(exported["bundle_url"])
        malicious_reference = self.root / "not-a-local-voice.wav"
        target = self.root / "untrusted-voice.zip"
        with zipfile.ZipFile(source) as archive, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
            for name in archive.namelist():
                data = archive.read(name)
                if name == "project.json":
                    project = json.loads(data)
                    project["voice_id"] = "e" * 32
                    project["voice_snapshot"] = {"id": "e" * 32, "reference_audio": str(malicious_reference),
                                                  "reference_text": "外部伪造参考。"}
                    project["segments"][0]["audio_url"] = "/files/../../do-not-trust.wav"
                    data = json.dumps(project, ensure_ascii=False).encode()
                output.writestr(name, data)
        restored = self.service.import_project(target)
        self.assertEqual(restored["voice_id"], "default")
        self.assertEqual(restored["voice_snapshot"], self.service.assets.get_voice("default"))
        self.assertEqual(restored["voice_snapshot"]["reference_audio"], str(self.reference))
        self.assertNotEqual(restored["id"], original["id"])
        self.assertEqual(restored["segments"][0]["status"], "ready")
        self.assertTrue(self.service._asset(restored["segments"][0]["audio_url"]).is_file())
        self.assertFalse(malicious_reference.exists())

    def test_portable_export_keeps_voice_version_without_private_reference(self):
        project = self.ready(self.project())
        with patch("voice_studio.service.shutil.which", return_value=None):
            result = self.service.export_project(project["id"])
        with zipfile.ZipFile(self.service._asset(result["bundle_url"])) as archive:
            exported = json.loads(archive.read("project.json"))
            self.assertEqual(exported["voice_id"], project["voice_id"])
            self.assertEqual(exported["voice_snapshot"], {"id": project["voice_id"], "revision": 1})
            self.assertNotIn(self.service.config["reference_text"], json.dumps(exported, ensure_ascii=False))
        self.assertEqual(self.service.get_project(project["id"])["voice_snapshot"], project["voice_snapshot"])

    def test_status_advertises_only_configured_speech_and_playable_sample(self):
        self.service.config.update({key: str(self.reference) for key in
                                    ("runtime_python", "encoder", "decoder", "tokens", "vocoder", "lexicon")})
        status = self.service.status()
        self.assertTrue(status["engine"]["available"])
        self.assertIsNone(status["voice"]["sample_url"])
        self.assertNotIn("已验收", status["voice"]["acceptance_scope"])
        self.service.config["reference_text"] = "   "
        self.assertFalse(self.service.status()["engine"]["available"])

    def restart_with_speech_resources(self, **reference):
        config = {key: str(self.reference) for key in
                  ("runtime_python", "encoder", "decoder", "tokens", "vocoder", "lexicon")}
        config.update(data_root=str(self.service.root), **reference)
        self.service.close()
        self.service = StudioService(config)
        self.addCleanup(self.service.close)

    def test_first_generation_binds_previously_unconfigured_default_snapshot(self):
        self.restart_with_speech_resources()
        project = self.project()
        self.assertFalse(project["voice_snapshot"]["reference_audio"])
        self.restart_with_speech_resources(reference_audio=str(self.reference), reference_text="新配置的参考。")
        with patch.object(self.service, "_start_queue"):
            self.assertEqual(self.service.generate(project["id"])["status"], "queued")
        saved = self.service.get_project(project["id"])
        self.assertEqual(saved["voice_snapshot"], self.service.assets.get_voice("default"))
        self.assertEqual(saved["voice_snapshot"]["reference_text"], "新配置的参考。")

    def test_bound_project_never_silently_switches_to_reconfigured_default(self):
        old_reference = self.root / "previous.wav"
        self.make_pcm(old_reference)
        self.restart_with_speech_resources(reference_audio=str(old_reference), reference_text="旧版本。")
        project = self.project()
        old_reference.unlink()
        self.restart_with_speech_resources(reference_audio=str(self.reference), reference_text="新版本。")
        with self.assertRaises(AppError) as caught:
            self.service.generate(project["id"])
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.service.get_project(project["id"]), project)
        self.assertEqual(self.service.list_jobs(), [])

    def test_custom_voice_can_queue_and_batch_without_default_reference(self):
        self.restart_with_speech_resources()
        engine = self.service.status()["engine"]
        self.assertFalse(engine["available"])
        self.assertTrue(engine["model_available"])
        voice = self.service.save_voice({"name": "已登记", "reference_audio": str(self.reference),
                                         "reference_text": "独立声线的参考。"})
        item = {"title": "使用非默认声线", "script": "只有一个片段。", "voice_id": voice["id"]}
        with patch.object(self.service, "_start_queue"):
            with self.assertRaises(AppError):
                self.service.create_batch({"generate": True, "projects": [item, {"title": "默认", "script": "一段。"}]})
            self.assertEqual(self.service.list_projects(), [])
            self.assertEqual(self.service.list_jobs(), [])
            project = self.service.create_project(item)
            self.assertEqual(self.service.generate(project["id"])["status"], "queued")
            batch = self.service.create_batch({"generate": True, "projects": [item]})
            self.assertEqual(batch["jobs"][0]["status"], "queued")

    def test_cleanup_proposes_then_requires_confirmation_and_supports_undo(self):
        original = self.ready(self.project("# DHL 汇报\n\n- 已到货 **12** 箱。"))
        proposal = self.service.prepare_manuscript(original["id"], {"expected_revision": original["revision"], "mode": "cleanup"})
        self.assertTrue(proposal["can_apply"])
        self.assertNotEqual(proposal["proposed_text"], original["script"])
        self.assertEqual(self.service.get_project(original["id"]), original)
        with self.assertRaises(AppError) as caught:
            self.service.apply_proposal(original["id"], proposal["id"], {"expected_revision": original["revision"]})
        self.assertEqual(caught.exception.status, 400)
        applied = self.service.apply_proposal(original["id"], proposal["id"], {"expected_revision": original["revision"], "confirmed": True})
        self.assertEqual(applied["script"], proposal["proposed_text"])
        self.assertGreater(applied["revision"], original["revision"])
        restored = self.service.undo_proposal(original["id"], proposal["id"], {"expected_revision": applied["revision"], "confirmed": True})
        self.assertEqual(restored["script"], original["script"])
        self.assertEqual(restored["segments"], original["segments"])
        self.assertEqual(restored["export"], original["export"])
        self.assertGreater(restored["revision"], applied["revision"])

    def test_stale_proposal_and_undo_do_not_overwrite_new_edits(self):
        original = self.project("# DHL 已到货 12 箱。")
        proposal = self.service.prepare_manuscript(original["id"], {"expected_revision": original["revision"]})
        edited = self.service.update_project(original["id"], {"expected_revision": original["revision"], "script": "最新人工原稿。"})
        with self.assertRaises(AppError) as caught:
            self.service.apply_proposal(original["id"], proposal["id"], {"expected_revision": edited["revision"], "confirmed": True})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.get_project(original["id"])["script"], "最新人工原稿。")
        proposal = self.service.prepare_manuscript(original["id"], {"expected_revision": edited["revision"]})
        applied = self.service.apply_proposal(original["id"], proposal["id"], {"expected_revision": edited["revision"], "confirmed": True})
        newer = self.service.update_project(original["id"], {"expected_revision": applied["revision"], "title": "采用后继续修改"})
        with self.assertRaises(AppError) as caught:
            self.service.undo_proposal(original["id"], proposal["id"], {"expected_revision": newer["revision"], "confirmed": True})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.get_project(original["id"]), newer)

    def test_changed_numbers_cannot_be_adopted_even_with_confirmation(self):
        original = self.project("DHL 已到货 12 箱。")
        with patch.object(self.service.rewriter, "rewrite", return_value="DHL 已到货 13 箱。"):
            proposal = self.service.prepare_manuscript(original["id"], {"expected_revision": original["revision"], "mode": "rewrite"})
        self.assertFalse(proposal["can_apply"])
        self.assertTrue(proposal["warnings"])
        with self.assertRaises(AppError) as caught:
            self.service.apply_proposal(original["id"], proposal["id"], {"expected_revision": original["revision"], "confirmed": True})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.service.get_project(original["id"]), original)

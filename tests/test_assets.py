"""Local asset contracts with external, synthetic PCM fixtures."""
import json
import hashlib
import os
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from voice_studio.assets import AssetStore, apply_dictionary
from voice_studio.errors import AppError


class AssetTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="assets-")
        self.root = Path(self.temp.name)
        self.recording = self.root / "original.wav"
        with wave.open(str(self.recording), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 2400)
        self.original = self.recording.read_bytes()
        self.store = AssetStore(self.root, {"reference_audio": str(self.recording),
                                          "reference_text": "原始参考。", "accepted_sample": str(self.recording)})

    def tearDown(self):
        self.temp.cleanup()

    def voice(self, **updates):
        return self.store.save_voice({"name": "测试声线", "reference_audio": str(self.recording),
                                      "reference_text": "这是参考文案。", **updates})

    def test_default_is_config_derived_and_read_only(self):
        default = self.store.get_voice("default")
        self.assertEqual(default["reference_text"], "原始参考。")
        self.assertEqual(default["sample_audio"], str(self.recording))
        self.assertEqual(default["reference_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertIn("未配置", AssetStore(self.root, {}).get_voice("default")["acceptance_scope"])
        with self.assertRaises(AppError) as caught:
            self.voice(voice_id="default", expected_revision=1)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.recording.read_bytes(), self.original)
        self.assertFalse((self.root / "assets" / "voices.json").exists())

    def test_voice_revision_persists_without_copying_recording(self):
        voice = self.voice()
        updated = self.store.save_voice({"voice_id": voice["id"], "expected_revision": 1, "name": "新名称"})
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["created_at"], voice["created_at"])
        self.assertEqual(updated["reference_audio"], str(self.recording.resolve()))
        self.assertEqual(updated["reference_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertIn("试听", updated["acceptance_scope"])
        self.assertEqual(AssetStore(self.root, {}).get_voice(voice["id"]), updated)
        self.assertEqual(list((self.root / "assets").iterdir()), [self.root / "assets" / "voices.json"])
        self.assertEqual(self.recording.read_bytes(), self.original)
        with self.assertRaises(AppError) as caught:
            self.store.save_voice({"voice_id": voice["id"], "expected_revision": 1, "name": "过期修改"})
        self.assertEqual(caught.exception.status, 409)

    def test_voice_paths_must_be_absolute_complete_pcm(self):
        invalid = self.root / "invalid.wav"
        invalid.write_bytes(b"not audio")
        truncated = self.root / "truncated.wav"
        truncated.write_bytes(self.original[:-20])
        for path in ("relative.wav", str(invalid), str(truncated), str(self.root / "missing.wav")):
            with self.subTest(path=path), self.assertRaises(AppError) as caught:
                self.voice(reference_audio=path)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(AppError):
            self.voice(sample_audio=str(invalid))

    def test_dictionary_revision_and_concurrent_save_conflicts(self):
        self.assertEqual(self.store.list_dictionary(), {"revision": 0, "entries": []})
        payload = {"expected_revision": 0, "entries": [{"term": "DHL", "spoken": "迪艾尺艾尔"}]}

        def save():
            try:
                return self.store.save_dictionary(payload)["revision"]
            except AppError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda unused: save(), range(2))), [1, 409])
        dictionary = self.store.list_dictionary()
        self.assertEqual(len(dictionary["entries"]), 1)
        self.assertTrue(dictionary["entries"][0]["enabled"])
        self.assertEqual(AssetStore(self.root, {}).list_dictionary(), dictionary)

    def test_dictionary_rejects_duplicates_markup_controls_and_limits(self):
        for entries in ([{"term": "DHL", "spoken": "甲"}, {"term": "DHL", "spoken": "乙"}],
                        [{"term": "DHL", "spoken": "<chang2>"}],
                        [{"term": "DHL", "spoken": "长[chang2]"}],
                        [{"term": "DHL\u200b", "spoken": "甲"}],
                        [{"term": "DHL", "spoken": "甲\n乙"}],
                        [{"term": "a" * 61, "spoken": "甲"}],
                        [{"term": "DHL", "spoken": "甲", "enabled": 1}],
                        [{"term": str(index), "spoken": "甲"} for index in range(201)]):
            with self.subTest(entries=entries[:2]), self.assertRaises(AppError):
                self.store.save_dictionary({"expected_revision": 0, "entries": entries})
        self.assertEqual(self.store.list_dictionary()["revision"], 0)

    def test_replacements_are_longest_first_literal_and_non_cascading(self):
        source = "DHL Express、DHL、A+B、A。"
        entries = [{"term": "DHL", "spoken": "A"}, {"term": "DHL Express", "spoken": "敦豪快递"},
                   {"term": "A", "spoken": "甲"}, {"term": "A+B", "spoken": "相加"},
                   {"term": "快递", "spoken": "不要级联", "enabled": False}]
        result = apply_dictionary(source, entries)
        self.assertEqual(result["spoken_text"], "敦豪快递、A、相加、甲。")
        self.assertEqual(source, "DHL Express、DHL、A+B、A。")
        self.assertEqual(sum(item["count"] for item in result["replacements"]), 4)
        self.assertEqual(apply_dictionary(source, []) , {"spoken_text": source, "replacements": []})

    def test_atomic_dictionary_failure_preserves_previous_json(self):
        previous = self.store.save_dictionary({"expected_revision": 0, "entries": []})
        with patch("voice_studio.assets.Path.replace", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                self.store.save_dictionary({"expected_revision": 1, "entries": [{"term": "DHL", "spoken": "敦豪"}]})
        self.assertEqual(json.loads((self.root / "assets" / "dictionary.json").read_text("utf-8")), previous)
        self.assertFalse(list((self.root / "assets").glob("*.tmp")))

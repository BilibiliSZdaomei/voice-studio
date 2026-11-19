import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_studio.config import PROJECT_ROOT, default_config, load_config, validate_data_path, voice_resource_checks


class ArtifactPathTests(unittest.TestCase):
    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_data_path(Path("output"))

    def test_source_rejected(self):
        with self.assertRaises(ValueError):
            validate_data_path(PROJECT_ROOT / "output")

    def test_registered_sync_roots_and_obsidian_markers_rejected(self):
        with tempfile.TemporaryDirectory(dir=os.environ["VOICE_STUDIO_TEST_ROOT"]) as directory:
            base = Path(directory)
            appdata = base / "settings"
            (appdata / "obsidian").mkdir(parents=True)
            (appdata / "Dropbox").mkdir()
            (base / "vault" / ".obsidian").mkdir(parents=True)
            (appdata / "obsidian" / "obsidian.json").write_text(
                json.dumps({"vaults": {"id": {"path": str(base / "registered-vault")}}}), encoding="utf-8")
            (appdata / "Dropbox" / "info.json").write_text(
                json.dumps({"business": {"path": str(base / "sync")}}), encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": str(appdata)}):
                for root in ("vault", "registered-vault", "sync"):
                    with self.subTest(root=root), self.assertRaises(ValueError):
                        validate_data_path(base / root / "generated")

    def test_environment_sync_root_rejected(self):
        root = Path(os.environ["VOICE_STUDIO_TEST_ROOT"]).resolve()
        with patch.dict(os.environ, {"OneDrive": str(root)}):
            with self.assertRaises(ValueError):
                validate_data_path(root / "generated")

    def test_external_test_root_accepted(self):
        root = Path(os.environ["VOICE_STUDIO_TEST_ROOT"]).resolve()
        self.assertEqual(validate_data_path(root), root)


class PortableConfigTests(unittest.TestCase):
    def test_fresh_machine_needs_no_model_or_specific_drive(self):
        base = Path(os.environ["VOICE_STUDIO_TEST_ROOT"])
        with patch.dict(os.environ, {"LOCALAPPDATA": str(base), "XDG_DATA_HOME": str(base)}, clear=True):
            config = default_config()
            self.assertEqual(Path(config["data_root"]), base / "VoiceStudio" / "data")
            self.assertFalse(any(check["exists"] for check in voice_resource_checks(config).values()))
            self.assertFalse(config["accepted_sample"])

    def test_explicit_data_root_wins_over_external_default(self):
        with tempfile.TemporaryDirectory(dir=os.environ["VOICE_STUDIO_TEST_ROOT"]) as directory:
            base = Path(directory)
            with patch.dict(os.environ, {"CODEX_ARTIFACT_ROOT": str(base)}):
                self.assertEqual(Path(default_config()["data_root"]), base / "VoiceStudio" / "data")
                config_file = base / "local.json"
                with self.assertRaises(FileNotFoundError):
                    load_config(str(config_file))
                target = base / "已有工程"
                config_file.write_text(json.dumps({"data_root": str(target)}), encoding="utf-8")
                self.assertEqual(Path(load_config(str(config_file))["data_root"]), target.resolve())

    def test_resources_require_reference_text_but_allow_missing_sample(self):
        config = default_config()
        for key in ("runtime_python", "encoder", "decoder", "tokens", "vocoder", "reference_audio", "lexicon"):
            config[key] = __file__
        self.assertFalse(voice_resource_checks(config)["reference_text"]["exists"])
        config["reference_text"] = "这是一句测试。"
        self.assertTrue(all(check["exists"] for check in voice_resource_checks(config).values()))
        self.assertFalse(config["accepted_sample"])

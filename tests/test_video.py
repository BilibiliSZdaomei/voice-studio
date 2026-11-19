"""Video snapshot boundaries; fixtures are silent PCM, never shipped as narration."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from voice_studio.errors import AppError
from voice_studio.video import VideoCancelled, VideoExporter, caption_cards
from voice_studio.avatar import audio_envelope


class VideoTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="video-")
        self.root = Path(self.temp.name)
        self.exporter = VideoExporter({}, self.root)
        self.pid = "a" * 32
        self.folder = self.root / "exports" / self.pid / ("b" * 32)
        self.folder.mkdir(parents=True)
        with wave.open(str(self.folder / "narration.wav"), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 24000)
        self.project = {"id": self.pid, "title": "中文 <script>alert(1)</script>", "revision": 3,
                        "settings": {"pause_seconds": .28}, "segments": [
                            {"id": "s1", "text": "这是完整的一句话。", "status": "ready", "revision": 1,
                             "audio_url": "/files/audio/source.wav"}]}
        self.exported = {"wav_url": "/files/exports/" + self.pid + "/" + self.folder.name + "/narration.wav",
                         "duration_seconds": 1, "source_revision": 2}
        self.manifest = {"timeline": [{"id": "s1", "text": "这是完整的一句话。", "start_seconds": 0, "end_seconds": 1}]}
        self.write_source()

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self):
        (self.folder / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.folder / "project.json").write_text(json.dumps(self.project), encoding="utf-8")

    def test_requires_valid_data_root(self):
        with self.assertRaises(ValueError):
            VideoExporter({}, Path(__file__).resolve().parent / "generated")

    def test_source_validates_real_pcm_duration_and_snapshot(self):
        _, _, duration, revision = self.exporter._source(self.project, self.exported)
        self.assertEqual((duration, revision), (1, 2))
        self.manifest["timeline"][0]["end_seconds"] = 2
        self.write_source()
        with self.assertRaises(AppError):
            self.exporter._source(self.project, self.exported)

    def test_other_project_and_traversal_rejected(self):
        for url in ("/files/exports/" + "c" * 32 + "/narration.wav", "/files/exports/../../narration.wav", "https://example.com/a.wav"):
            with self.subTest(url=url), self.assertRaises(AppError):
                self.exporter._source(self.project, dict(self.exported, wav_url=url))

    def test_new_audio_or_settings_require_fresh_export(self):
        for change in ("audio", "settings", "text"):
            project = copy.deepcopy(self.project)
            if change == "audio":
                project["segments"][0]["audio_url"] = "/files/audio/new-version.wav"
            elif change == "settings":
                project["settings"]["pause_seconds"] = .8
            else:
                project["segments"][0]["text"] = "修改文案。"
            with self.subTest(change=change), self.assertRaises(AppError):
                self.exporter._source(project, self.exported)

    def test_cards_keep_all_nonspace_text_and_escape_markup(self):
        text = "很长的中文句子，" * 30 + "HelloWorld" * 12
        parts = caption_cards(text, 36)
        self.assertTrue(all(len(part) <= 36 for part in parts))
        self.assertEqual("".join(parts), text)
        document = self.exporter._composition(self.project, self.manifest["timeline"], 1, "9:16")
        self.assertIn('data-width="1080"', document)
        self.assertIn('&lt;script&gt;', document)
        self.assertNotIn('<script>alert', document)
        self.assertNotIn('https://', document)

    def test_native_caption_track_and_complete_local_project(self):
        self.exporter._write_composition(self.root, self.project, self.manifest["timeline"], 1, "16:9")
        index = (self.root / "index.html").read_text(encoding="utf-8")
        scene = (self.root / "compositions" / "captions.html").read_text(encoding="utf-8")
        self.assertIn('data-track-kind="captions"', index)
        self.assertIn('data-composition-src="compositions/captions.html"', index)
        self.assertEqual(index.count('<audio '), 1)
        self.assertTrue(scene.lstrip().startswith('<template>'))
        self.assertNotIn('<audio ', scene)

    def test_virtual_host_has_independent_graphics_and_real_audio_envelope(self):
        envelope = audio_envelope(self.folder / "narration.wav")
        self.exporter._write_composition(self.root, self.project, self.manifest["timeline"], 1,
                                         "16:9", "virtual_host", "studio-bot", envelope)
        index = (self.root / "index.html").read_text(encoding="utf-8")
        host = (self.root / "compositions" / "host.html").read_text(encoding="utf-8")
        captions = (self.root / "compositions" / "captions.html").read_text(encoding="utf-8")
        self.assertIn('data-composition-src="compositions/host.html"', index)
        self.assertIn('data-track-kind="graphics"', index)
        self.assertIn('data-track-kind="captions"', index)
        self.assertEqual(index.count('<audio '), 1)
        self.assertIn('studio-bot', host)
        self.assertIn('850px', captions)
        self.assertIn('&lt;script&gt;', captions)
        self.assertNotIn('@@', index + host + captions)
        self.assertTrue(all(level == 0 for level in envelope["levels"]))

    def test_layout_defaults_are_compatible_and_unknown_layouts_are_rejected(self):
        self.assertEqual(self.exporter.presentation({}), ("captions", None))
        self.assertEqual(self.exporter.presentation({"layout": "virtual_host"}), ("virtual_host", "studio-bot"))
        for options in ({"layout": "photoreal"}, {"layout": []}, {"layout": "virtual_host", "avatar_id": "someone"}):
            with self.subTest(options=options), self.assertRaises(AppError):
                self.exporter.presentation(options)

    def test_nonfinite_timeline_and_declared_duration_are_rejected(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.exporter._source(self.project, dict(self.exported, duration_seconds=value))

    def test_options_and_unavailable_renderer_are_explicit(self):
        self.assertFalse(self.exporter.availability()["available"])
        for options in ({"aspect_ratio": "1:1"}, {"fps": True}, {"fps": 120}):
            with self.subTest(options=options), self.assertRaises(AppError):
                self.exporter.render(self.project, self.exported, options)
        with self.assertRaises(AppError) as raised:
            self.exporter.render(self.project, self.exported)
        self.assertEqual(raised.exception.status, 503)

    def test_renderer_exit_code_is_preserved_even_when_log_is_empty(self):
        with self.assertRaises(AppError):
            self.exporter._run([sys.executable, "-B", "-c", "raise SystemExit(7)"],
                               self.root, os.environ.copy(), "failed.log", None, 5)
        receipt = json.loads((self.root / "failed.log.process.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 7)

    def test_cancel_stops_real_renderer_process(self):
        cancelled = threading.Event()
        timer = threading.Timer(.3, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(VideoCancelled):
                self.exporter._run([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                   self.root, os.environ.copy(), "cancel.log", cancelled, 10)
        finally:
            timer.cancel()

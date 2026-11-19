"""Real PCM-energy boundaries; synthetic test audio never becomes narration."""
from array import array
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from voice_studio.avatar import audio_envelope, build_host_composition
from voice_studio.errors import AppError


class AvatarTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("VOICE_STUDIO_TEST_ROOT")
        if not base:
            raise RuntimeError("Set external VOICE_STUDIO_TEST_ROOT before tests")
        Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix="avatar-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_wav(self, values, rate=24000, channels=1, width=2, name="energy.wav"):
        target = self.root / name
        samples = array("h", values)
        if sys.byteorder != "little":
            samples.byteswap()
        with wave.open(str(target), "wb") as output:
            output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            output.writeframes(samples.tobytes())
        return target

    def envelope(self, seconds=1):
        return {"sample_hz": 15, "duration_seconds": seconds, "levels": [0.0] * math.ceil(seconds * 15)}

    def test_silence_stays_zero_and_partial_last_interval_is_kept(self):
        envelope = audio_envelope(self.write_wav([0] * 24100))
        self.assertEqual(envelope["sample_hz"], 15)
        self.assertAlmostEqual(envelope["duration_seconds"], 24100 / 24000)
        self.assertEqual(envelope["levels"], [0.0] * 16)

    def test_measured_energy_follows_audio_volume_and_silence(self):
        values = [0] * 4800 + [1200, -1200] * 2400 + [6500, -6500] * 2400 + [0] * 4800
        envelope = audio_envelope(self.write_wav(values), sample_hz=10)
        levels = envelope["levels"]
        self.assertEqual(len(levels), 8)
        self.assertEqual(levels[:2] + levels[-2:], [0.0] * 4)
        self.assertGreater(levels[4], levels[2])
        self.assertGreater(levels[2], 0)
        self.assertTrue(all(0 <= value <= 1 for value in levels))

    def test_isolated_peak_does_not_flatten_speech_and_quiet_noise_is_gated(self):
        values = [2000, -2000] * 10800 + [32767, -32767] * 1200
        envelope = audio_envelope(self.write_wav(values), sample_hz=10)
        self.assertEqual(envelope["levels"][:9], [1.0] * 9)
        noise = audio_envelope(self.write_wav([10, -10] * 12000, name="noise.wav"))
        self.assertEqual(noise["levels"], [0.0] * 15)

    def test_invalid_audio_formats_and_truncated_data_are_rejected(self):
        for options in ({"channels": 2}, {"width": 1}):
            with self.subTest(options=options), self.assertRaises(AppError):
                audio_envelope(self.write_wav([0] * 24000, **options))
        empty = self.write_wav([])
        with self.assertRaises(AppError):
            audio_envelope(empty)
        path = self.write_wav([200] * 24000)
        path.write_bytes(path.read_bytes()[:-20])
        with self.assertRaises(AppError):
            audio_envelope(path)
        with self.assertRaises(AppError):
            audio_envelope(self.root / "missing.wav")

    def test_sample_rate_boundaries(self):
        path = self.write_wav([0] * 24000)
        for value in (True, 0, -1, 61, 1.5, None):
            with self.subTest(value=value), self.assertRaises(AppError):
                audio_envelope(path, value)

    def test_template_is_deterministic_editable_local_and_transparent(self):
        envelope = self.envelope(3)
        envelope["levels"][12] = .8
        document = build_host_composition(3, "16:9", envelope)
        self.assertEqual(document, build_host_composition(3, "16:9", envelope))
        self.assertTrue(document.lstrip().startswith("<template>"))
        self.assertIn('data-composition-id="voice-host"', document)
        self.assertIn("window.__timelines['voice-host'] = tl", document)
        self.assertIn("background: transparent", document)
        self.assertIn('data-mouth-animation="audio-energy"', document)
        self.assertIn('id="voice-host-mouth"', document)
        self.assertIn('id="voice-host-right-arm"', document)
        self.assertIn("虚拟讲解员", document)
        self.assertNotIn("@@", document)
        for forbidden in ("setTimeout", "setInterval", "requestAnimationFrame", "Date.now", "Math.random", "fetch(", "https://", "http://", ".play(", ".pause(", "repeat:"):
            with self.subTest(forbidden=forbidden):
                # The SVG namespace is a local format identifier, not a fetch.
                self.assertNotIn(forbidden, document.replace('xmlns="http://www.w3.org/2000/svg"', ""))
        portrait = build_host_composition(3, "9:16", envelope)
        self.assertIn('data-width="1080"', portrait)
        self.assertIn('data-height="1920"', portrait)
        self.assertIn("left: 270px; top: 350px", portrait)

    def test_invalid_parameters_never_invent_a_mouth_track(self):
        for duration in (0, -1, float("nan"), float("inf"), True, 86401):
            with self.subTest(duration=duration), self.assertRaises(AppError):
                build_host_composition(duration, "16:9", self.envelope())
        for aspect in ("1:1", None):
            with self.subTest(aspect=aspect), self.assertRaises(AppError):
                build_host_composition(1, aspect, self.envelope())
        with self.assertRaises(AppError):
            build_host_composition(1, "16:9", self.envelope(), "unknown")
        for changes in ({"levels": []}, {"levels": [float("nan")] * 15}, {"levels": [True] * 15},
                        {"levels": [1.1] * 15}, {"duration_seconds": 2}, {"sample_hz": 0}):
            with self.subTest(changes=changes), self.assertRaises(AppError):
                build_host_composition(1, "16:9", dict(self.envelope(), **changes))

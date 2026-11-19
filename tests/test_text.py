import math
import unittest

from voice_studio.errors import AppError
from voice_studio.text import clean_script, settings_value, split_sentences


class TextTests(unittest.TestCase):
    def test_keeps_commas_and_chinese_sentences(self):
        self.assertEqual(split_sentences("第一句，保留逗号。第二句！\n第三句"), ["第一句，保留逗号。", "第二句！", "第三句"])

    def test_long_text_bounded_without_dropped_characters(self):
        text = "测试较长的文案" * 43
        parts = split_sentences(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 120 for part in parts))

    def test_invalid_manuscript_and_settings(self):
        for value in (None, "", "!!!", "测试\x00", "字" * 30001):
            with self.assertRaises(AppError):
                clean_script(value)
        for value in ({"speed": math.nan}, {"speed": True}, {"pause_seconds": -1}, {"other": 1}):
            with self.assertRaises(AppError):
                settings_value(value)

    def test_settings_partial_update(self):
        self.assertEqual(settings_value({"pause_seconds": .5}, {"speed": 1.2, "pause_seconds": .28}),
                         {"speed": 1.2, "pause_seconds": .5})

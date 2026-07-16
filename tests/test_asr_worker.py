from __future__ import annotations

import unittest

from bilitranscript_app.asr_worker import clean_sensevoice_text


class AsrWorkerTests(unittest.TestCase):
    def test_removes_sensevoice_control_tokens(self) -> None:
        raw = "<|zh|><|HAPPY|><|Speech|> 你好 <|BGM|> 世界"
        self.assertEqual(clean_sensevoice_text(raw), "你好 世界")


if __name__ == "__main__":
    unittest.main()


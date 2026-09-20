from __future__ import annotations

import unittest
from unittest.mock import patch

from bilitranscript_app import windows_effects
from bilitranscript_app.composition_backdrop import _tint_for_theme
from bilitranscript_app.styles import DARK, LIGHT, stylesheet


class WindowsEffectsTests(unittest.TestCase):
    def test_explicit_theme_does_not_depend_on_system_registry(self) -> None:
        with patch.object(windows_effects, "system_prefers_dark", return_value=True):
            self.assertEqual(windows_effects.effective_theme("light"), "light")
            self.assertEqual(windows_effects.effective_theme("dark"), "dark")
            self.assertEqual(windows_effects.effective_theme("system"), "dark")

    def test_unsupported_platform_rejects_acrylic(self) -> None:
        with patch.object(windows_effects.sys, "platform", "linux"):
            self.assertFalse(windows_effects.acrylic_supported())

    def test_acrylic_and_fallback_styles_have_distinct_root_paints(self) -> None:
        acrylic = stylesheet("dark", acrylic=True)
        fallback = stylesheet("dark", acrylic=False)
        self.assertIn("QMainWindow { background: transparent; }", acrylic)
        self.assertNotIn("QMainWindow { background: transparent; }", fallback)
        self.assertIn(DARK["background"], fallback)

    def test_dark_theme_uses_a_deeper_acrylic_tint_and_distinct_text(self) -> None:
        dark = stylesheet("dark", acrylic=True)
        light = stylesheet("light", acrylic=True)
        self.assertIn(DARK["content_tint"], dark)
        self.assertIn(DARK["sidebar_tint"], dark)
        self.assertIn(DARK["text"], dark)
        self.assertIn(DARK["sidebar_text"], dark)
        self.assertIn(LIGHT["text"], light)
        self.assertIn(LIGHT["sidebar_text"], light)
        self.assertIn(LIGHT["sidebar_subtext"], light)
        self.assertNotEqual(DARK["text"], LIGHT["text"])
        self.assertGreater(_tint_for_theme("dark") >> 24, 0x70)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core
import resume_io


class TestHumanize(unittest.TestCase):
    def test_strips_unicode_dashes(self):
        s = "Top‑Secret 800‑37 – Present controls—Access"
        out = core.humanize(s)
        for bad in ("‐", "‑", "‒", "–", "—"):
            self.assertNotIn(bad, out)
        self.assertIn("Top-Secret", out)
        self.assertIn("800-37", out)

    def test_spaced_percent(self):
        self.assertEqual(core.humanize("cut time by 30 % overall"), "cut time by 30% overall")

    def test_zero_width_removed(self):
        self.assertEqual(core.humanize("a​b﻿c"), "abc")

    def test_smart_quotes_to_straight(self):
        self.assertEqual(core.humanize("“hi” it’s"), '"hi" it\'s')

    def test_preserves_numbers_and_words(self):
        s = "Cut response by 30 % across 16+ agencies and 500+ endpoints"
        out = core.humanize(s)
        for tok in ("30%", "16+", "500+", "agencies", "endpoints"):
            self.assertIn(tok, out)

    def test_idempotent(self):
        once = core.humanize("Top‑Secret by 30 %")
        self.assertEqual(core.humanize(once), once)


class TestStripMarkdown(unittest.TestCase):
    def test_headings_and_bold_flattened(self):
        out = core.strip_markdown("### SUMMARY\n**Jesse Maye**\n- bullet stays")
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertIn("SUMMARY", out)
        self.assertIn("Jesse Maye", out)
        self.assertIn("- bullet stays", out)

    def test_drops_divider_lines(self):
        self.assertNotIn("---", core.strip_markdown("a\n---\nb"))


class TestExportClean(unittest.TestCase):
    def test_txt_export_is_clean(self):
        raw = "### HEAD\n**Name**\nTop‑Secret cleared by 30 %"
        txt = resume_io.to_txt(raw).decode()
        self.assertNotIn("###", txt)
        self.assertNotIn("**", txt)
        self.assertNotIn("‑", txt)
        self.assertIn("30%", txt)
        self.assertIn("Top-Secret", txt)

    def test_docx_bytes_produced(self):
        b = resume_io.to_docx("### HEAD\n**Name** plus text\nTop‑Secret")
        self.assertTrue(b and len(b) > 200)


if __name__ == "__main__":
    unittest.main()

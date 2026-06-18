"""Tests for resume_io.py — résumé file parse + export.

Round-trips work whether or not python-docx/pypdf are installed (the module
falls back to stdlib for .txt and .docx). Zero installs:
  python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resume_io as rio  # noqa: E402

SRC = "Jane Doe — Engineer\nAcme 2019-2024: cut latency 40%.\nB.S. CS 2019."
BOM = chr(0xFEFF)  # explicit codepoint (ASCII-clean source)


class TestImport(unittest.TestCase):
    def test_txt(self):
        self.assertEqual(rio.parse_resume("cv.txt", SRC.encode()), SRC)

    def test_txt_bom_stripped(self):
        self.assertEqual(rio.parse_resume("cv.txt", (BOM + SRC).encode("utf-8")), SRC)

    def test_unknown_extension_decodes_as_text(self):
        self.assertEqual(rio.parse_resume("resume", SRC.encode()), SRC)

    def test_size_cap(self):
        with self.assertRaises(rio.ParseError):
            rio.parse_resume("big.txt", b"x" * (rio.MAX_UPLOAD_BYTES + 1))

    def test_docx_roundtrip(self):
        blob = rio.to_docx(SRC)
        self.assertEqual(blob[:2], b"PK")  # it's a zip
        self.assertEqual(rio.parse_resume("x.docx", blob).strip(), SRC.strip())

    def test_minimal_stdlib_docx_roundtrip(self):
        blob = rio._minimal_docx(SRC)
        self.assertEqual(rio._docx_to_text(blob).strip(), SRC.strip())

    def test_invalid_docx_raises(self):
        with self.assertRaises(rio.ParseError):
            rio._docx_to_text(b"this is not a docx file")


class TestExport(unittest.TestCase):
    def test_txt_bytes(self):
        self.assertEqual(rio.to_txt(SRC), SRC.encode())

    def test_docx_is_zip(self):
        self.assertEqual(rio.to_docx(SRC)[:2], b"PK")


if __name__ == "__main__":
    unittest.main()

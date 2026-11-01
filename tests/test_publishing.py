"""Smoke and safety tests with an isolated, fictional, Arabic manuscript."""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from awwal_publisher.cli import metadata_for, preflight, _build, PublicationError


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = self.root / "11 - publishing"
        (p / "editions" / "ar").mkdir(parents=True)
        (p / "editions" / "en").mkdir(parents=True)
        d = self.root / "04 - الكتب" / "كتاب 01" / "الفصول"
        d.mkdir(parents=True)
        self.chapter = d / "01 - البدء.md"
        self.chapter.write_text("---\nchapter: 1\nbook: 1\ntitle: البدء\nstatus: review\n---\n\n# البدء\n\nهذا مشهد عربي تجريبي.\n", encoding="utf-8")
        translated = self.root / "10 - translations" / "en" / "book-01" / "chapters"
        translated.mkdir(parents=True)
        (translated / "01 - opening.md").write_text("---\nchapter: 1\nbook: 1\ntitle: Opening\nstatus: review\n---\n\n# Opening\n\nA short sample.\n", encoding="utf-8")
        (p / "publication.json").write_text(json.dumps({
            "series_id": "sample", "source_locale": "ar", "source_revision": "v1",
            "books": [{"id": "01", "source_chapters": "04 - الكتب/كتاب 01/الفصول", "expected_chapters": 1}],
        }, ensure_ascii=False), encoding="utf-8")
        for lang in ("ar", "en"):
            data = {"locale": lang, "title": "اختبار" if lang == "ar" else "Test",
                    "status": "review", "author": "Sample", "rights_confirmed": False,
                    "source_revision": "v1", "translation_review": "draft", "assets": []}
            (p / "editions" / lang / "book-01.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_preview_ar_and_en_valid(self):
        for lang in ("ar", "en"):
            with self.subTest(lang=lang):
                result = preflight(self.root, "01", lang, "preview", {"epub"})
                self.assertTrue(result["ok"], result["errors"])
                self.assertEqual(result["direction"], "rtl" if lang == "ar" else "ltr")

    def test_release_requires_approval(self):
        result = preflight(self.root, "01", "ar", "release", {"epub", "print-pdf"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("approved/locked" in i for i in result["errors"]))
        self.assertTrue(any("Print release" in i for i in result["errors"]))

    def test_translation_is_separate(self):
        _, _, _, source = metadata_for(self.root, "01", "ar")
        _, _, _, trans = metadata_for(self.root, "01", "en")
        self.assertNotEqual(source, trans)

    def test_block_path_escape(self):
        config = self.root / "11 - publishing" / "publication.json"
        data = json.loads(config.read_text())
        data["books"][0]["source_chapters"] = "../../secrets"
        config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(PublicationError):
            preflight(self.root, "01", "ar", "preview", {"epub"})

    def test_real_epub_and_pdf_build_leave_source_untouched(self):
        before = self.chapter.read_bytes()
        out = self.root / "result"
        result = _build(self.root, "01", "ar", "preview", {"epub", "print-pdf", "docx", "html"}, out)
        self.assertFalse(result["ready_for_sale"])
        self.assertEqual(before, self.chapter.read_bytes())
        self.assertTrue((out / "Book-Print-Interior.pdf").read_bytes().startswith(b"%PDF-"))
        self.assertTrue((out / "Book.docx").read_bytes().startswith(b"PK"))
        with zipfile.ZipFile(out / "Book.epub") as archive:
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertTrue(any(b'page-progression-direction="rtl"' in archive.read(n)
                                for n in archive.namelist() if n.endswith(".opf")))


if __name__ == "__main__":
    unittest.main()

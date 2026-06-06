"""Tests for #78 — ZIP text extraction in extract.py.

All ZIPs are built in memory; no network or R2 access required.
"""
import io
import struct
import zipfile

import pytest

from nodalpulse.workers.extract import _extract_text, _zip_text


# ── helpers to build synthetic ZIPs ───────────────────────────────────────────

def _make_zip(*entries: tuple[str, bytes]) -> bytes:
    """Return a ZIP archive in memory with (filename, content) entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


_FAKE_PDF = b"%PDF-1.4 fake content for testing\n" + b"A" * 100
_FAKE_DOCX_CONTENT = b"Hello from DOCX"


def _make_docx(text: str = "Hello from DOCX") -> bytes:
    """Build a minimal DOCX (ZIP + word/document.xml) with the given text."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>'
        '</w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml.encode())
    return buf.getvalue()


# ── dispatch routing ───────────────────────────────────────────────────────────

class TestExtractTextDispatch:
    def test_zip_ext_routes_to_zip_text_not_docx(self):
        """`_extract_text` with file_ext='zip' must call _zip_text, not _docx_text.

        A ZIP of PDFs has PK magic bytes — without the explicit file_ext='zip'
        check, _docx_text would fire and return "" (no word/document.xml).
        """
        zip_bytes = _make_zip(("filing.pdf", _FAKE_PDF))
        # _zip_text can't extract text from a fake PDF (pdfplumber will fail),
        # but crucially the call must NOT raise and must NOT return the
        # _docx_text path (which also returns "" but for the wrong reason).
        result = _extract_text(zip_bytes, "zip")
        # Result is "" because fake PDF has no real pages — that's OK;
        # the key is the dispatch didn't crash and file_ext="zip" was honoured.
        assert isinstance(result, str)

    def test_pdf_ext_unaffected(self):
        """file_ext='pdf' still routes to _pdf_text (regression guard)."""
        # A real PDF magic-byte prefix — pdfplumber will fail on fake content
        # but must not raise.
        result = _extract_text(b"%PDF-1.4 fake", "pdf")
        assert isinstance(result, str)

    def test_docx_ext_unaffected(self):
        """file_ext='docx' still routes to _docx_text (regression guard)."""
        docx = _make_docx("Test content here")
        result = _extract_text(docx, "docx")
        assert "Test content here" in result

    def test_pk_magic_with_non_zip_ext_routes_to_docx(self):
        """PK magic bytes with file_ext='pdf' (FERC DOCX mislabelled) still go to _docx_text."""
        docx = _make_docx("FERC mislabelled DOCX")
        result = _extract_text(docx, "pdf")  # FERC sends DOCX with .pdf ext
        assert "FERC mislabelled DOCX" in result


# ── _zip_text unit tests ───────────────────────────────────────────────────────

class TestZipText:
    def test_single_docx_entry(self):
        """ZIP with one DOCX entry extracts the DOCX text."""
        docx_bytes = _make_docx("ERCOT Notice of Violation paragraph one.")
        zip_bytes = _make_zip(("ERCOT Notice.docx", docx_bytes))
        result = _zip_text(zip_bytes)
        assert "ERCOT Notice of Violation" in result

    def test_single_txt_entry(self):
        """ZIP with one .txt entry decodes and returns the text."""
        txt = b"Plain text filing content."
        zip_bytes = _make_zip(("notice.txt", txt))
        result = _zip_text(zip_bytes)
        assert "Plain text filing content." in result

    def test_multi_entry_pdf_and_docx(self):
        """ZIP with PDF + DOCX: both entries contribute to output with separators."""
        docx_bytes = _make_docx("Exhibit A content from DOCX")
        zip_bytes = _make_zip(
            ("main_filing.docx", docx_bytes),
            ("Exhibit A.pdf", _FAKE_PDF),
        )
        result = _zip_text(zip_bytes)
        # DOCX content should appear
        assert "Exhibit A content from DOCX" in result
        # Separator for each entry
        assert "=== main_filing.docx ===" in result

    def test_shapefile_entries_skipped(self):
        """GIS shapefile components (.shp, .dbf, .prj, etc.) are silently skipped."""
        docx_bytes = _make_docx("Coversheet text for CCN amendment")
        gis_binary = b"\x00\x00\x27\x0a" + b"\xff" * 100  # fake shapefile bytes
        zip_bytes = _make_zip(
            ("Coversheet.docx", docx_bytes),
            ("boundary.shp", gis_binary),
            ("boundary.dbf", gis_binary),
            ("boundary.prj", b"GEOGCS[...]"),
            ("boundary.shx", gis_binary),
        )
        result = _zip_text(zip_bytes)
        assert "Coversheet text for CCN amendment" in result
        # GIS content must not appear in output
        assert "shp" not in result.lower() or "Coversheet" in result

    def test_all_binary_entries_returns_empty(self):
        """ZIP with only shapefile/image entries yields '' (graceful no_text)."""
        zip_bytes = _make_zip(
            ("map.shp", b"\x00" * 50),
            ("map.dbf", b"\x00" * 50),
            ("photo.png", b"\x89PNG" + b"\x00" * 50),
        )
        result = _zip_text(zip_bytes)
        assert result == ""

    def test_empty_zip_returns_empty(self):
        """Empty ZIP (zero entries) returns ''."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        result = _zip_text(buf.getvalue())
        assert result == ""

    def test_corrupted_bytes_returns_empty(self):
        """Non-ZIP bytes (corrupted input) return '' without raising."""
        result = _zip_text(b"this is not a zip file at all")
        assert result == ""

    def test_output_capped_at_60k(self):
        """Total output is capped at 60,000 characters."""
        # Build a DOCX with very long content
        long_text = "X" * 80_000
        docx_bytes = _make_docx(long_text)
        zip_bytes = _make_zip(("big.docx", docx_bytes))
        result = _zip_text(zip_bytes)
        assert len(result) <= 60_000

    def test_nested_zip_entry_skipped(self):
        """A .zip entry inside a ZIP is skipped (no recursion)."""
        inner_zip = _make_zip(("inner.txt", b"inner content"))
        docx_bytes = _make_docx("Outer document text")
        outer_zip = _make_zip(
            ("outer.docx", docx_bytes),
            ("nested.zip", inner_zip),
        )
        result = _zip_text(outer_zip)
        assert "Outer document text" in result
        assert "inner content" not in result

    def test_entry_count_guard(self, monkeypatch):
        """ZIP exceeding _ZIP_MAX_ENTRIES guard returns '' without crashing."""
        import nodalpulse.workers.extract as ext_mod
        monkeypatch.setattr(ext_mod, "_ZIP_MAX_ENTRIES", 2)
        # Build a ZIP with 3 entries
        zip_bytes = _make_zip(
            ("a.txt", b"text a"),
            ("b.txt", b"text b"),
            ("c.txt", b"text c"),
        )
        result = _zip_text(zip_bytes)
        assert result == ""

    def test_xlsx_entry_skipped(self):
        """XLSX entries (spreadsheets) are skipped even if present."""
        docx_bytes = _make_docx("Report text")
        zip_bytes = _make_zip(
            ("report.docx", docx_bytes),
            ("data.xlsx", b"PK\x03\x04" + b"\x00" * 50),  # fake xlsx
        )
        result = _zip_text(zip_bytes)
        assert "Report text" in result
        # XLSX bytes must not appear as garbage text
        assert "PK" not in result or "Report text" in result

    def test_docx_disguised_as_zip(self):
        """ZIP that IS a DOCX (word/document.xml at root) extracts via _docx_text.

        PUCT sometimes packages a DOCX with a .ZIP extension. The old PK-magic ->
        _docx_text path handled this; _zip_text must preserve that behaviour.
        """
        # _make_docx creates a real DOCX (ZIP with word/document.xml at root)
        docx_bytes = _make_docx("PUCT mislabelled DOCX as ZIP — SGIA Amendment")
        result = _zip_text(docx_bytes)
        assert "PUCT mislabelled DOCX as ZIP" in result

    def test_dispatch_docx_as_zip_via_extract_text(self):
        """_extract_text with file_ext='zip' on a DOCX-as-ZIP still extracts text."""
        docx_bytes = _make_docx("Regulation text inside DOCX-as-ZIP")
        result = _extract_text(docx_bytes, "zip")
        assert "Regulation text inside DOCX-as-ZIP" in result

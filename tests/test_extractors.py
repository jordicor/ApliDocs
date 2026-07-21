"""Regression tests for ApliDocs' bounded document extractors.

The fixtures are deliberately small and self-contained.  They exercise the
extractors directly, so neither Microsoft Office, Poppler nor network access is
needed to run this module.
"""

import builtins
import codecs
import json
import os
import pathlib
import re
import subprocess
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


# ``build_index`` is intentionally a POSIX program, while these unit tests are
# also useful on Windows.  Supply only the locking surface used by the builder
# when the platform has no fcntl module.
try:
    import fcntl as _fcntl  # noqa: F401
except ImportError:  # pragma: no cover - exercised on Windows CI
    _fcntl_stub = types.ModuleType("fcntl")
    _fcntl_stub.LOCK_EX = 1
    _fcntl_stub.LOCK_NB = 2
    _fcntl_stub.LOCK_UN = 8
    _fcntl_stub.flock = lambda _fd, _operation: None
    sys.modules.setdefault("fcntl", _fcntl_stub)

import build_index


_WORD_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Word fixture</w:t></w:r></w:p></w:body>
</w:document>
"""

_ODT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text><text:p>ODT fixture</text:p></office:text></office:body>
</office:document-content>
"""

_WORKBOOK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Budget 2026" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""

_WORKBOOK_RELS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
   Target="worksheets/sheet1.xml"/>
</Relationships>
"""

_SHARED_STRINGS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 count="1" uniqueCount="1">
  <si><t>Shared label</t></si>
</sst>
"""

_WORKSHEET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1">
    <c r="A1" t="s"><v>0</v></c>
    <c r="A2" t="inlineStr"><is><t>Inline</t><r><t xml:space="preserve"> rich text</t></r></is></c>
    <c r="A3"><f>SUM(A4:A5)</f><v>42</v></c>
    <c r="A4"><v>19.5</v></c>
    <c r="A5" t="str"><f>CONCAT(&quot;Q&quot;,&quot;3&quot;)</f><v>Q3 result</v></c>
  </row></sheetData>
</worksheet>
"""


def _write_zip(path, members):
    """Write a minimal OOXML/ODF package from a member-name -> bytes map."""
    with zipfile.ZipFile(str(path), "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _write_xlsx(path):
    _write_zip(
        path,
        {
            "xl/workbook.xml": _WORKBOOK_XML,
            "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS_XML,
            "xl/sharedStrings.xml": _SHARED_STRINGS_XML,
            "xl/worksheets/sheet1.xml": _WORKSHEET_XML,
        },
    )


def _builder(root, publish_dir):
    config = {
        "corpus_root": str(root),
        "publish_dir": str(publish_dir),
        "corpus_id_file": ".aplidocs-corpus-id",
        "access_roots": [str(root)],
        "access_policy_id": "extractor-tests",
        "access_policy_generation": "1",
        "prune_dirs": [],
        "metadata_only_globs": [],
        "filename_pattern": None,
        "smoke_term": None,
        "allow_empty": False,
        "min_file_count": 1,
        "max_file_count_drop_fraction": 0.5,
        "max_walk_retries": 2,
        "publish_mode": "0640",
        "publish_group": None,
    }
    return build_index.IndexBuilder(config, str(pathlib.Path(root) / "index.sqlite"))


def _first_limit(*names):
    """Return the first extraction limit exposed by this implementation."""
    for name in names:
        value = getattr(build_index, name, None)
        if isinstance(value, int) and value > 0:
            return value
    return build_index.TEXT_CAP_BYTES


class _ReadBudget:
    """Proxy that rejects unbounded reads and a cumulative memory over-read."""

    def __init__(self, raw, budget):
        self._raw = raw
        self.budget = budget
        self.total = 0
        self.requests = []

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("extractor attempted an unbounded read()")
        self.requests.append(size)
        data = self._raw.read(size)
        self.total += len(data)
        if self.total > self.budget:
            raise AssertionError(
                "extractor read %d bytes through a %d-byte budget"
                % (self.total, self.budget)
            )
        return data

    def read1(self, size=-1):
        return self.read(size)

    def readline(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("extractor attempted an unbounded readline()")
        data = self._raw.readline(size)
        self.total += len(data)
        if self.total > self.budget:
            raise AssertionError("extractor exceeded its cumulative read budget")
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def __iter__(self):
        return self

    def __next__(self):
        data = self.readline(64 * 1024)
        if not data:
            raise StopIteration
        return data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        return self._raw.close()

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _EndlessPdfStdout:
    """A pipe-like source that catches buffering or a missing output cap."""

    def __init__(self, hard_ceiling):
        self.hard_ceiling = hard_ceiling
        self.total = 0
        self.requests = []
        self.closed = False

    def read(self, size=-1):
        if size is None or size <= 0:
            raise AssertionError("pdftotext stdout was read without a finite chunk size")
        self.requests.append(size)
        # Small chunks make this independent of the implementation's chosen
        # pipe buffer while still forcing it past TEXT_CAP_BYTES.
        amount = min(size, 8 * 1024)
        self.total += amount
        if self.total > self.hard_ceiling:
            raise AssertionError("pdftotext stdout was consumed without a byte cap")
        return b"x" * amount

    def read1(self, size=-1):
        return self.read(size)

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return self.read(8 * 1024)

    def close(self):
        self.closed = True


class _EndlessPdfProcess:
    """Minimal Popen test double for an over-producing pdftotext process."""

    def __init__(self, hard_ceiling):
        self.stdout = _EndlessPdfStdout(hard_ceiling)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self, *args, **kwargs):
        raise AssertionError("communicate() would buffer unbounded pdftotext output")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.returncode is None:
            self.wait()
        return False


class TextAndXmlRegressionTests(unittest.TestCase):
    def test_missing_deflate_backend_is_a_process_wide_dependency_failure(self):
        with mock.patch.object(build_index.zipfile, "zlib", None):
            with self.assertRaises(build_index.DependencyUnavailable):
                build_index.ensure_zip_deflate_backend()

    def test_archive_dependency_is_checked_before_a_cached_extraction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            with mock.patch.object(
                build_index,
                "ensure_zip_deflate_backend",
                side_effect=build_index.DependencyUnavailable("missing zlib"),
            ), mock.patch.object(
                builder,
                "cached_extraction",
                return_value=(build_index.ST_EXTRACTED, "cached", "ooxml-word"),
            ) as cached:
                with self.assertRaises(build_index.DependencyUnavailable):
                    builder.classify(
                        "synthetic.docx", "synthetic.docx", "docx", 1, 1, "hash"
                    )
            cached.assert_not_called()

    def test_text_over_cap_is_classified_with_bounded_reads(self):
        """M-05: a large TXT must not be read into RAM before ``too_big``."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            publish = root / "published"
            path = root / "large.txt"
            input_limit = _first_limit(
                "MAX_TEXT_INPUT_BYTES", "TEXT_SOURCE_CAP_BYTES"
            )
            path.write_bytes(b"x" * (input_limit + 4096))

            real_open = builtins.open
            guarded = None

            def open_with_budget(file, mode="r", *args, **kwargs):
                nonlocal guarded
                raw = real_open(file, mode, *args, **kwargs)
                if os.path.abspath(os.fspath(file)) == os.path.abspath(str(path)) and mode == "rb":
                    guarded = _ReadBudget(raw, input_limit + 1)
                    return guarded
                return raw

            builder = _builder(root, publish)
            with mock.patch("builtins.open", side_effect=open_with_budget):
                status, text, extractor = builder.classify(
                    str(path), path.name, "txt", path.stat().st_size, 1
                )

            self.assertEqual(build_index.ST_TOO_BIG, status)
            self.assertIsNone(text)
            self.assertIn(extractor, (None, "text"))
            if guarded is not None:
                self.assertLessEqual(guarded.total, input_limit + 1)

    def test_zip_xml_members_are_never_read_with_zipfile_read_all(self):
        """M-05: OOXML/ODF members are consumed through finite reads."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            word_path = root / "fixture.docx"
            odt_path = root / "fixture.odt"
            excel_path = root / "fixture.xlsx"
            _write_zip(word_path, {"word/document.xml": _WORD_XML})
            _write_zip(odt_path, {"content.xml": _ODT_XML})
            _write_xlsx(excel_path)

            original_read = zipfile.ZipExtFile.read
            finite_requests = []

            def finite_zip_read(stream, size=-1):
                if size is None or size < 0:
                    raise AssertionError("ZIP member was decompressed with read() and no limit")
                finite_requests.append(size)
                return original_read(stream, size)

            cases = (
                (build_index._extract_word, word_path, "Word fixture"),
                (build_index._extract_odt, odt_path, "ODT fixture"),
                (build_index._extract_excel, excel_path, "Budget 2026"),
            )
            with mock.patch.object(zipfile.ZipExtFile, "read", new=finite_zip_read):
                for extractor, path, expected in cases:
                    with self.subTest(path=path.name):
                        self.assertIn(expected, extractor(str(path)))

            self.assertTrue(finite_requests)
            self.assertTrue(all(size >= 0 for size in finite_requests))

    def test_oversized_zip_member_stops_decompression_at_a_finite_budget(self):
        """M-05: compressed XML is rejected during, not after, expansion."""
        member_limit = _first_limit(
            "MAX_ARCHIVE_MEMBER_BYTES", "ARCHIVE_MEMBER_CAP_BYTES"
        )
        read_slack = max(64 * 1024, min(member_limit, 1024 * 1024))
        hard_ceiling = member_limit + read_slack
        prefix = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        )
        suffix = b"</w:t></w:r></w:p></w:body></w:document>"
        xml = prefix + (b"x" * (hard_ceiling + 1)) + suffix

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            path = root / "compressed-bomb.docx"
            _write_zip(path, {"word/document.xml": xml})
            builder = _builder(root, root / "published")
            original_read = zipfile.ZipExtFile.read
            decompressed = [0]

            def budgeted_zip_read(stream, size=-1):
                if size is None or size < 0:
                    raise AssertionError("oversized ZIP member used an unbounded read()")
                data = original_read(stream, size)
                decompressed[0] += len(data)
                if decompressed[0] > hard_ceiling:
                    raise AssertionError("ZIP member exceeded its finite expansion budget")
                return data

            with mock.patch.object(
                zipfile.ZipExtFile, "read", new=budgeted_zip_read
            ):
                status, text, extractor = builder.classify(
                    str(path), path.name, "docx", path.stat().st_size, 1
                )

            self.assertEqual(build_index.ST_TOO_BIG, status)
            self.assertIsNone(text)
            self.assertIn(extractor, (None, "ooxml-word"))
            self.assertLessEqual(decompressed[0], hard_ceiling)

    def test_excessive_zip_member_count_is_rejected_before_central_directory_load(self):
        """M-05: the member-count limit is checked before ZipFile allocates its list."""
        declared = build_index.MAX_ZIP_MEMBERS + 1
        eocd = struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, declared, declared, 0, 0, 0
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "too-many.docx"
            path.write_bytes(eocd)
            with self.assertRaises(build_index.ContentTooBig):
                build_index._extract_word(str(path))

    def test_forged_zip_member_count_is_checked_before_zipfile_allocation(self):
        """M-05: EOCD count cannot hide a larger actual central directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "forged-count.docx"
            _write_zip(
                path,
                {
                    "word/document.xml": _WORD_XML,
                    "word/extra.xml": b"<extra/>",
                },
            )
            payload = bytearray(path.read_bytes())
            eocd = payload.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd, 0)
            struct.pack_into("<H", payload, eocd + 8, 1)
            struct.pack_into("<H", payload, eocd + 10, 1)
            path.write_bytes(payload)
            with mock.patch.object(
                build_index.zipfile,
                "ZipFile",
                side_effect=AssertionError("ZipFile allocated before preflight"),
            ):
                with self.assertRaises(build_index.ExtractionFailed):
                    build_index._extract_word(str(path))

    def test_lzma_zip_member_is_rejected_before_decompression(self):
        """M-05: document packages only accept bounded-memory ZIP codecs."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "lzma.docx"
            with zipfile.ZipFile(str(path), "w", compression=zipfile.ZIP_LZMA) as archive:
                archive.writestr("word/document.xml", _WORD_XML)
            with mock.patch.object(
                zipfile.ZipFile,
                "open",
                side_effect=AssertionError("LZMA member reached decompression"),
            ):
                with self.assertRaises(build_index.ExtractionFailed):
                    build_index._extract_word(str(path))

    def test_corrupt_raw_deflate_is_a_per_document_extraction_failure(self):
        """A broken stream must not escape as an uncaught zlib exception."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "corrupt-deflate.docx"
            _write_zip(path, {"word/document.xml": _WORD_XML})
            with zipfile.ZipFile(str(path), "r") as archive:
                info = archive.getinfo("word/document.xml")
            payload = bytearray(path.read_bytes())
            self.assertEqual(b"PK\x03\x04", payload[info.header_offset:info.header_offset + 4])
            name_length, extra_length = struct.unpack_from(
                "<HH", payload, info.header_offset + 26
            )
            data_start = info.header_offset + 30 + name_length + extra_length
            self.assertGreater(info.compress_size, 0)
            # Set the first DEFLATE block type to the reserved value 3.
            payload[data_start] = (payload[data_start] & ~0x06) | 0x06
            path.write_bytes(payload)

            with self.assertRaises(build_index.ExtractionFailed):
                build_index._extract_word(str(path))

    def test_invalid_utf8_zip_filename_is_a_per_document_failure(self):
        """Invalid central-directory Unicode must not abort the whole build."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "invalid-name.docx"
            _write_zip(path, {"word/document.xml": _WORD_XML})
            payload = bytearray(path.read_bytes())
            central = payload.find(b"PK\x01\x02")
            self.assertGreaterEqual(central, 0)
            flags = struct.unpack_from("<H", payload, central + 8)[0]
            struct.pack_into("<H", payload, central + 8, flags | 0x800)
            payload[central + 46] = 0xFF
            path.write_bytes(payload)

            with self.assertRaises(build_index.ExtractionFailed):
                build_index._extract_word(str(path))

    def test_invalid_utf8_local_zip_filename_is_a_per_document_failure(self):
        """Invalid local-header Unicode must not abort the whole build."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "invalid-local-name.docx"
            _write_zip(path, {"word/document.xml": _WORD_XML})
            payload = bytearray(path.read_bytes())
            local = payload.find(b"PK\x03\x04")
            self.assertGreaterEqual(local, 0)
            flags = struct.unpack_from("<H", payload, local + 6)[0]
            struct.pack_into("<H", payload, local + 6, flags | 0x800)
            payload[local + 30] = 0xFF
            path.write_bytes(payload)

            with self.assertRaises(build_index.ExtractionFailed):
                build_index._extract_word(str(path))

    def test_unsupported_zip_version_is_a_per_document_failure(self):
        """An impossible version-needed field must not abort the whole build."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "unsupported-version.docx"
            _write_zip(path, {"word/document.xml": _WORD_XML})
            payload = bytearray(path.read_bytes())
            central = payload.find(b"PK\x01\x02")
            self.assertGreaterEqual(central, 0)
            struct.pack_into("<H", payload, central + 6, 100)
            path.write_bytes(payload)

            with self.assertRaises(build_index.ExtractionFailed):
                build_index._extract_word(str(path))

    def test_utf_boms_are_detected_and_removed(self):
        """B-01: UTF-8/16/32 BOM files decode to searchable Unicode text."""
        expected = "Cabecera: año Δ\nsegunda línea"
        encodings = (
            ("utf8", codecs.BOM_UTF8, "utf-8"),
            ("utf16le", codecs.BOM_UTF16_LE, "utf-16-le"),
            ("utf16be", codecs.BOM_UTF16_BE, "utf-16-be"),
            ("utf32le", codecs.BOM_UTF32_LE, "utf-32-le"),
            ("utf32be", codecs.BOM_UTF32_BE, "utf-32-be"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            for label, bom, encoding in encodings:
                with self.subTest(encoding=label):
                    path = root / (label + ".txt")
                    path.write_bytes(bom + expected.encode(encoding))
                    self.assertEqual(expected, build_index._read_text_file(str(path)))

    def test_xlsx_extracts_inline_shared_formula_and_cached_values_in_order(self):
        """B-02: useful worksheet cells are not lost behind OOXML storage types."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "cells.xlsx"
            _write_xlsx(path)

            text = build_index._collapse_whitespace(build_index._extract_excel(str(path)))
            expected_fragments = (
                "Budget 2026",
                "Shared label",
                "Inline rich text",
                "SUM(A4:A5)",
                "42",
                "19.5",
                'CONCAT("Q","3")',
                "Q3 result",
            )
            positions = []
            for fragment in expected_fragments:
                with self.subTest(fragment=fragment):
                    position = text.find(fragment)
                    self.assertNotEqual(-1, position, "%r missing from %r" % (fragment, text))
                    positions.append(position)
            self.assertEqual(sorted(positions), positions, text)

    def test_xlsx_repeated_shared_string_stops_at_output_budget(self):
        """M-05: shared-string references cannot amplify a small sheet to OOM."""
        shared_value = "x" * (64 * 1024)
        shared_xml = (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>%s</t></si></sst>" % shared_value
        ).encode("utf-8")
        cells = "".join(
            '<c r="A%d" t="s"><v>0</v></c>' % index for index in range(1, 20)
        )
        worksheet = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData><row>%s</row></sheetData></worksheet>" % cells
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "shared-amplification.xlsx"
            _write_zip(
                path,
                {
                    "xl/workbook.xml": _WORKBOOK_XML,
                    "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS_XML,
                    "xl/sharedStrings.xml": shared_xml,
                    "xl/worksheets/sheet1.xml": worksheet,
                },
            )
            with self.assertRaises(build_index.ContentTooBig):
                build_index._extract_excel(str(path))

    def test_xlsx_nested_shared_string_items_are_rejected_without_reflattening(self):
        """Nested ``si`` elements cannot trigger overlapping subtree work."""
        nested = "<si>" * 32 + "<t>value</t>" + "</si>" * 32
        shared_xml = (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + nested
            + "</sst>"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "nested-shared-strings.xlsx"
            _write_zip(
                path,
                {
                    "xl/workbook.xml": _WORKBOOK_XML,
                    "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS_XML,
                    "xl/sharedStrings.xml": shared_xml,
                    "xl/worksheets/sheet1.xml": _WORKSHEET_XML,
                },
            )
            with self.assertRaises(build_index.ExtractionFailed):
                build_index._extract_excel(str(path))

    def test_xlsx_huge_shared_string_index_is_not_parsed_as_a_big_integer(self):
        huge_index = "9" * 100000
        worksheet = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c r="A1" t="s"><v>'
            + huge_index
            + "</v></c></row></sheetData></worksheet>"
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "huge-shared-index.xlsx"
            _write_zip(
                path,
                {
                    "xl/workbook.xml": _WORKBOOK_XML,
                    "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS_XML,
                    "xl/sharedStrings.xml": _SHARED_STRINGS_XML,
                    "xl/worksheets/sheet1.xml": worksheet,
                },
            )
            text = build_index._extract_excel(str(path))
            self.assertIn("Budget 2026", text)
            self.assertNotIn(huge_index[:100], text)

    def test_xml_flatten_preserves_nested_text_and_tail_document_order(self):
        """B-09: a parent's tail belongs after all of that parent's children."""
        xml = b"""<root>
          <p>one <span>two <em>three</em> four</span> five</p> six
          <p>seven</p>
        </root>"""
        flattened = build_index._xml_flatten(xml, frozenset(("p",)))
        self.assertEqual(
            "one two three four five six seven",
            build_index._collapse_whitespace(flattened),
        )

    def test_xml_dtd_is_rejected_even_when_encoded_as_utf16(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE root [<!ENTITY secret "expanded">]>'
            '<root>&secret;</root>'
        ).encode("utf-16")
        with self.assertRaises(build_index.ExtractionFailed):
            build_index._xml_flatten(xml, frozenset())

    def test_unknown_xml_encoding_is_isolated_as_a_document_error(self):
        xml = b'<?xml version="1.0" encoding="x-unknown"?><document/>'
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            path = root / "unknown-encoding.docx"
            _write_zip(path, {"word/document.xml": xml})
            builder = _builder(root, root / "published")
            status, text, extractor = builder.classify(
                str(path), path.name, "docx", path.stat().st_size, 1
            )

            self.assertEqual(build_index.ST_ERROR, status)
            self.assertIsNone(text)
            self.assertEqual("ooxml-word", extractor)


class PdfRegressionTests(unittest.TestCase):
    def test_pdf_probe_text_fits_inside_media_box(self):
        """The functional-probe token must not be clipped by the PDF page."""
        payload = build_index._minimal_pdf_probe_bytes()
        layout = re.search(
            rb"/MediaBox \[0 0 (?P<page_width>\d+) (?P<page_height>\d+)\].*?"
            rb"BT /F1 (?P<font_size>\d+) Tf (?P<text_x>\d+) (?P<text_y>\d+) Td "
            rb"\((?P<token>[A-Z]+)\) Tj ET",
            payload,
            re.DOTALL,
        )
        self.assertIsNotNone(layout, "unexpected functional-probe PDF layout")
        values = {
            name: int(value)
            for name, value in layout.groupdict().items()
            if name != "token"
        }
        # Every uppercase Helvetica glyph is narrower than one em, so this is
        # a deliberately conservative bound that leaves room for extraction.
        required_width = values["font_size"] * len(layout.group("token"))
        available_width = values["page_width"] - values["text_x"]
        self.assertGreaterEqual(available_width, required_width)

    def test_posix_pdf_without_resource_limits_fails_closed(self):
        with mock.patch.object(build_index.os, "name", "posix"), \
                mock.patch.object(build_index, "resource", None), \
                mock.patch.object(build_index.shutil, "which", return_value="pdftotext"), \
                mock.patch.object(build_index.subprocess, "Popen") as popen:
            with self.assertRaises(build_index.DependencyUnavailable):
                build_index._extract_pdf("synthetic.pdf")
        popen.assert_not_called()

    def test_pdf_child_resource_hook_sets_memory_cpu_and_core_limits(self):
        calls = []
        fake_resource = types.SimpleNamespace(
            RLIMIT_AS=1,
            RLIMIT_CPU=2,
            RLIMIT_CORE=3,
            RLIM_INFINITY=-1,
            getrlimit=lambda _category: (-1, -1),
            setrlimit=lambda category, limits: calls.append((category, limits)),
        )
        with mock.patch.object(build_index, "resource", fake_resource):
            build_index._pdf_child_limits(256 * 1024 * 1024)()
        self.assertEqual(
            [
                (fake_resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2),
                (
                    fake_resource.RLIMIT_CPU,
                    (build_index.PDFTOTEXT_TIMEOUT + 5,) * 2,
                ),
                (fake_resource.RLIMIT_CORE, (0, 0)),
            ],
            calls,
        )

    def test_pdf_output_is_bounded_and_process_is_stopped(self):
        """M-05: an over-producing PDF extractor cannot fill process memory."""
        output_limit = _first_limit(
            "MAX_PDF_OUTPUT_BYTES", "PDF_OUTPUT_CAP_BYTES"
        )
        hard_ceiling = output_limit + 128 * 1024
        processes = []

        def fake_popen(*args, **kwargs):
            process = _EndlessPdfProcess(hard_ceiling)
            processes.append(process)
            return process

        def guarded_run(command, *args, **kwargs):
            # A cheap version/probe command may legitimately use run().  The
            # conversion command may not capture the complete PDF in PIPE.
            if command and command[-1] == "-" and kwargs.get("stdout") is subprocess.PIPE:
                raise AssertionError("subprocess.run(stdout=PIPE) buffers the whole PDF")
            return subprocess.CompletedProcess(command, 0, stdout=b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            pdf_path = root / "large.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(build_index.subprocess, "Popen", side_effect=fake_popen), \
                    mock.patch.object(build_index.subprocess, "run", side_effect=guarded_run), \
                    mock.patch.object(build_index.shutil, "which", return_value="pdftotext"):
                status, text, extractor = builder.classify(
                    str(pdf_path), "large.pdf", "pdf", pdf_path.stat().st_size, 1
                )

        self.assertEqual(build_index.ST_TOO_BIG, status)
        self.assertIsNone(text)
        self.assertEqual("pdftotext", extractor)
        self.assertTrue(processes, "PDF extraction never opened a streamable process")
        process = processes[0]
        self.assertLessEqual(process.stdout.total, hard_ceiling)
        self.assertTrue(
            process.terminated or process.killed or process.stdout.closed,
            "the over-producing pdftotext process was left running",
        )

    def test_missing_pdftotext_is_a_global_fatal_error(self):
        """M-15: absence of the sole PDF backend must prevent publication."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            pathlib.Path(builder.output_path).parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(build_index.shutil, "which", return_value=None):
                with self.assertRaisesRegex(
                    build_index.DependencyUnavailable, "not available on PATH"
                ):
                    builder._ensure_pdf_backend()
            self.assertFalse(builder._pdf_checked)

    def test_corrupt_pdf_remains_a_per_file_error(self):
        """M-15: one malformed PDF is not confused with a missing backend."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            pdf_path = root / "broken.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\ncorrupt")
            with mock.patch.object(
                build_index,
                "_extract_pdf",
                side_effect=build_index.ExtractionFailed("pdftotext exit code 1"),
            ):
                status, text, extractor = builder.classify(
                    str(pdf_path), "broken.pdf", "pdf", pdf_path.stat().st_size, 1
                )

        self.assertEqual(build_index.ST_ERROR, status)
        self.assertIsNone(text)
        self.assertIn(extractor, (None, "pdftotext"))

    def test_pdftotext_probe_requires_known_extracted_text(self):
        """M-15: an exit-0 wrapper that emits nothing is still globally broken."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            pathlib.Path(builder.output_path).parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(build_index.shutil, "which", return_value="pdftotext"), \
                    mock.patch.object(build_index, "_extract_pdf", return_value=""):
                with self.assertRaises(build_index.DependencyUnavailable):
                    builder._ensure_pdf_backend()
            self.assertFalse(builder._pdf_checked)
            self.assertEqual(
                [], list(pathlib.Path(builder.output_path).parent.glob(".pdftotext-probe-*"))
            )

    def test_two_corrupt_documents_remain_individual_errors(self):
        """M-15: multiple corrupt inputs do not imply an extractor outage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            paths = []
            for number in (1, 2):
                path = root / ("broken-%d.docx" % number)
                path.write_bytes(b"not a package")
                paths.append(path)
            with mock.patch.object(
                build_index,
                "_extract_word",
                side_effect=build_index.ExtractionFailed("invalid package"),
            ):
                for path in paths:
                    status, _text, _extractor = builder.classify(
                        str(path), path.name, "docx", path.stat().st_size, 1
                    )
                    self.assertEqual(build_index.ST_ERROR, status)

    def test_unexpected_extractor_exception_is_not_downgraded(self):
        """M-15: programming/system failures propagate instead of becoming ST_ERROR."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            builder = _builder(root, root / "published")
            path = root / "document.docx"
            path.write_bytes(b"placeholder")
            with mock.patch.object(
                build_index, "_extract_word", side_effect=RuntimeError("regression")
            ):
                with self.assertRaisesRegex(RuntimeError, "regression"):
                    builder.classify(
                        str(path), path.name, "docx", path.stat().st_size, 1
                    )

    def test_deep_xml_hits_a_document_budget_instead_of_python_recursion(self):
        """M-05: nested XML is rejected per document before RecursionError."""
        depth = build_index.MAX_XML_DEPTH + 1
        xml = ("<p>" * depth + "text" + "</p>" * depth).encode("ascii")
        with self.assertRaises(build_index.ContentTooBig):
            build_index._xml_flatten(xml, frozenset(("p",)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

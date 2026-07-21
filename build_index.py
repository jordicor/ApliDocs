#!/usr/bin/env python3
"""Builder for the ApliDocs full-text index (SQLite + FTS5).

Walks a document tree (`corpus_root`), records one metadata row per file,
extracts plain text from the document formats that carry it, and publishes a
single SQLite file with FTS5 indexes over filenames/paths and over document
content.

Everything site-specific lives in a JSON config file (see README.md and
config.example.json); this script hardcodes nothing about a particular corpus.
Fail fast: any unexpected condition aborts loudly and nothing is published. The
only tolerated per-file recovery is the content_status machinery (a corrupt,
oversize or timed-out file is recorded and the build continues; totals are
reported in `meta` and in the log).
"""
import argparse
import base64
import codecs
import collections
import errno
import fnmatch
import hashlib
import html.parser
import json
import os
import pathlib
import platform
import re
import selectors
import shutil
import signal
import stat as statmod
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

from aplidocs_common import (
    require_safe_sqlite_provider,
    select_fts5_provider,
    sqlite_readonly_uri,
)

try:
    import fcntl
except ImportError:  # importable on Windows for CLI/tests; builder remains POSIX-first
    fcntl = None

try:
    import grp
except ImportError:
    grp = None

try:
    import resource
except ImportError:
    resource = None

import sqlite3 as _stdlib_sqlite3

_SQLITE_IMPORT_FAILURES = []
_SQLITE_PROVIDER_CANDIDATES = []
try:
    import pysqlite3.dbapi2 as _pysqlite3
except Exception as exc:
    _SQLITE_IMPORT_FAILURES.append("pysqlite3 import: %s" % exc)
else:
    _SQLITE_PROVIDER_CANDIDATES.append(("pysqlite3", _pysqlite3))
_SQLITE_PROVIDER_CANDIDATES.append(("stdlib sqlite3", _stdlib_sqlite3))
_SQLITE_PROVIDER_CANDIDATES = tuple(_SQLITE_PROVIDER_CANDIDATES)
sqlite3 = _SQLITE_PROVIDER_CANDIDATES[0][1]

# --- Constants ---------------------------------------------------------------

SCHEMA_VERSION = 2
TOOL_VERSION = "aplidocs/2.0"

PUBLISHED_DB_NAME = "aplidocs-index.sqlite"
PUBLISH_LOCK_NAME = ".aplidocs-build.lock"
PUBLICATION_BINDING_NAME = ".aplidocs-publication.json"
PUBLICATION_BINDING_VERSION = 2
PUBLICATION_BINDING_MAX_BYTES = 4096

# A real top-level directory can be called ``_ROOT``.  ``.`` cannot be a path
# segment, so it is an unambiguous value for files loose at corpus root.
ROOT_AREA_SENTINEL = "."

# Directory segment names pruned at any depth in addition to the user's
# `prune_dirs`. These are Synology bookkeeping directories that never hold
# user documents; excluding them is always correct.
ALWAYS_PRUNE_DIRS = frozenset(("@eaDir", "#recycle"))
# Operating-system junk files skipped everywhere.
SKIP_FILES = frozenset((".DS_Store", "desktop.ini", "Thumbs.db"))

FTS_TOKENIZE = "unicode61 remove_diacritics 2"
TEXT_CAP_BYTES = 500 * 1024
TEXT_SOURCE_CAP_BYTES = 8 * 1024 * 1024
ARCHIVE_SOURCE_CAP_BYTES = 256 * 1024 * 1024
ARCHIVE_MEMBER_CAP_BYTES = 16 * 1024 * 1024
ARCHIVE_TOTAL_CAP_BYTES = 64 * 1024 * 1024
ARCHIVE_DIRECTORY_CAP_BYTES = 8 * 1024 * 1024
PDF_SOURCE_CAP_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 10000
MAX_XML_NODES = 200000
MAX_XML_DEPTH = 256
HASH_CHUNK_BYTES = 1024 * 1024
PDFTOTEXT_TIMEOUT = 60
DEFAULT_PDF_MEMORY_LIMIT_MB = 512
FILENAME_PATTERN_TIMEOUT_SECONDS = 0.1
DEFAULT_MAX_TOTAL_EXTRACTED_MB = 1024
DEFAULT_MAX_INDEX_SIZE_MB = 2048

ZIP_DEFLATE_PROBE = base64.b64decode(b"cwzw8XTxdw6O8gwICPJ3cgUA")

# Public aliases make the enforced budgets easy to discover in tests and in
# operational tooling without tying callers to the internal names above.
MAX_TEXT_INPUT_BYTES = TEXT_SOURCE_CAP_BYTES
MAX_ARCHIVE_MEMBER_BYTES = ARCHIVE_MEMBER_CAP_BYTES
MAX_PDF_OUTPUT_BYTES = TEXT_CAP_BYTES

# Extension groups (lowercase, no dot).
PDF_EXT = frozenset(("pdf",))
WORD_EXT = frozenset(("docx", "docm", "dotx"))
EXCEL_EXT = frozenset(("xlsx", "xlsm", "xltx"))
ODT_EXT = frozenset(("odt",))
TEXT_EXT = frozenset(("txt", "md", "csv"))
HTML_EXT = frozenset(("htm", "html"))
UNSUPPORTED_EXT = frozenset(("doc", "xls", "ppt", "rtf", "msg"))

# content_status enum.
ST_EXTRACTED = "extracted"
ST_EMPTY_PDF = "empty_pdf"
ST_UNSUPPORTED = "unsupported_format"
ST_METADATA_ONLY = "metadata_only"
ST_TOO_BIG = "too_big"
ST_ERROR = "error"
ST_NO_TEXT = "no_text_type"

# Statuses whose outcome is stable enough to copy from the previous DB and
# skip re-extraction. `error` is always retried (may be transient); the
# remaining statuses are derived without extraction cost.
CACHEABLE_STATUSES = frozenset((ST_EXTRACTED, ST_EMPTY_PDF, ST_TOO_BIG))

# XML block localnames that force a whitespace boundary when flattening
# OOXML / ODF markup (paragraphs, breaks, table rows, shared-string items).
XML_BLOCK_LOCALNAMES = frozenset(
    (
        "p", "h", "br", "cr", "tab", "si",
        "line-break", "list-item", "table-row", "table-cell", "tr", "td",
    )
)

# Config schema: key -> (allowed types, required, default). Unknown keys are
# rejected (typo protection); required keys must be present; types are checked.
CONFIG_SCHEMA = {
    "corpus_root": ((str,), True, None),
    "publish_dir": ((str,), True, None),
    "corpus_id_file": ((str,), False, ".aplidocs-corpus-id"),
    "access_roots": ((list,), True, None),
    "access_policy_id": ((str,), True, None),
    "access_policy_generation": ((str,), True, None),
    "prune_dirs": ((list,), False, []),
    "metadata_only_globs": ((list,), False, []),
    "filename_pattern": ((str, type(None)), False, None),
    "smoke_term": ((str, type(None)), False, None),
    "allow_empty": ((bool,), False, False),
    "min_file_count": ((int,), False, 1),
    "max_file_count_drop_fraction": ((int, float), False, 0.5),
    "max_walk_retries": ((int,), False, 2),
    "max_directory_entries": ((int,), False, 50000),
    "max_directory_depth": ((int,), False, 128),
    "max_total_entries": ((int,), False, 2000000),
    "max_total_files": ((int,), False, 1000000),
    "max_total_extracted_mb": ((int,), False, DEFAULT_MAX_TOTAL_EXTRACTED_MB),
    "max_index_size_mb": ((int,), False, DEFAULT_MAX_INDEX_SIZE_MB),
    "pdf_memory_limit_mb": ((int,), False, DEFAULT_PDF_MEMORY_LIMIT_MB),
    "publish_mode": ((str,), False, "0640"),
    "publish_group": ((str, type(None)), False, None),
}


class ContentTooBig(Exception):
    """A configured extraction resource limit was exceeded."""


class ExtractionFailed(Exception):
    """The individual document is corrupt or unsupported by its parser."""


class DependencyUnavailable(Exception):
    """A process-wide extractor dependency is unavailable."""


class CorpusMutation(Exception):
    """The corpus changed while a consistent snapshot was being built."""


class CacheInvalid(Exception):
    """The previous database failed after cache reuse had begun."""


class FilenamePatternTimeout(RuntimeError):
    """A configured filename regex exceeded its per-name CPU/wall budget."""


def log(message):
    """Emit a timestamped line to stdout (captured by the cron log)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[%s] %s" % (stamp, message), flush=True)


def config_error(message):
    """Report a configuration problem and abort. Nothing is published."""
    log("CONFIG ERROR: %s" % message)
    sys.exit(2)


def _has_unicode_surrogate(value):
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def ensure_fts5_provider():
    """Verify the selected SQLite provider before any durable build mutation."""
    global sqlite3
    try:
        provider, _label, version = select_fts5_provider(
            _SQLITE_PROVIDER_CANDIDATES,
            _SQLITE_IMPORT_FAILURES,
            version_checker=require_safe_sqlite_provider,
        )
    except RuntimeError as exc:
        machine = platform.machine() or "unknown"
        fallback = (
            "provide a current stdlib/pysqlite3-compatible vendor, container "
            "or self-built Python linked to SQLite 3.53.3+"
        )
        raise RuntimeError(
            "SQLite FTS5 is unavailable on %s (%s); %s (see docs/SYNOLOGY.md): %s"
            % (machine, sys.platform, fallback, exc)
        )
    sqlite3 = provider
    return version


# --- Config ------------------------------------------------------------------


MAX_POLICY_GENERATION = "9223372036854775807"


def _is_canonical_policy_generation(value):
    """Validate a bounded decimal generation without parsing huge integers."""
    return (
        type(value) is str
        and value.isascii()
        and value.isdecimal()
        and (value == "0" or not value.startswith("0"))
        and (
            len(value) < len(MAX_POLICY_GENERATION)
            or (
                len(value) == len(MAX_POLICY_GENERATION)
                and value <= MAX_POLICY_GENERATION
            )
        )
    )


def load_config(config_path):
    """Load and validate the JSON config. Fail fast on any problem.

    Returns a dict with every key present (defaults filled in). Validation
    covers: missing file, invalid JSON, non-object root, unknown keys, missing
    required keys, wrong value types, and non-string list elements.
    """
    if not os.path.isfile(config_path):
        config_error("config file not found: %r" % config_path)
    with open(config_path, "rb") as handle:
        raw = handle.read()
    try:
        # ``utf-8-sig`` accepts ordinary UTF-8 as well as the BOM emitted by
        # some Windows editors.  It never silently accepts another encoding.
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        config_error("config file is not valid UTF-8 JSON (%s): %r" % (exc, config_path))
    if not isinstance(data, dict):
        config_error("config root must be a JSON object: %r" % config_path)

    unknown = sorted(set(data) - set(CONFIG_SCHEMA))
    if unknown:
        config_error(
            "unknown config key(s): %s (known keys: %s)"
            % (", ".join(unknown), ", ".join(sorted(CONFIG_SCHEMA)))
        )

    resolved = {}
    for key, (types, required, default) in CONFIG_SCHEMA.items():
        if key not in data:
            if required:
                config_error("missing required config key: %r" % key)
            resolved[key] = list(default) if isinstance(default, list) else default
            continue
        value = data[key]
        # bool is a subclass of int; accept it only for an explicitly boolean
        # setting and keep it out of numeric limits.
        bool_setting = types == (bool,)
        if (isinstance(value, bool) and not bool_setting) or not isinstance(value, types):
            config_error(
                "config key %r must be %s, got %s"
                % (key, " or ".join(t.__name__ for t in types), type(value).__name__)
            )
        if types == (list,):
            for element in value:
                if not isinstance(element, str) or isinstance(element, bool):
                    config_error("config key %r must be a list of strings" % key)
        resolved[key] = value

    for key, value in resolved.items():
        strings = value if isinstance(value, list) else (value,)
        for item in strings:
            if isinstance(item, str) and _has_unicode_surrogate(item):
                config_error(
                    "config key %r contains an invalid Unicode surrogate" % key
                )

    for key in ("corpus_root", "publish_dir", "access_policy_id", "access_policy_generation"):
        if not resolved[key].strip():
            config_error("config key %r must not be empty" % key)
        if resolved[key] != resolved[key].strip():
            config_error("config key %r must not have leading/trailing whitespace" % key)
    generation = resolved["access_policy_generation"]
    if not _is_canonical_policy_generation(generation):
        config_error(
            "access_policy_generation must be a canonical non-negative decimal "
            "integer string no greater than %s" % MAX_POLICY_GENERATION
        )
    for key in ("access_policy_id", "access_policy_generation"):
        if len(resolved[key].encode("utf-8")) > 256:
            config_error("config key %r must not exceed 256 UTF-8 bytes" % key)
    if not os.path.isabs(resolved["corpus_root"]):
        config_error("config key 'corpus_root' must be an absolute path")
    if not os.path.isabs(resolved["publish_dir"]):
        config_error("config key 'publish_dir' must be an absolute path")
    if os.path.realpath(resolved["publish_dir"]) == os.path.realpath(resolved["corpus_root"]):
        config_error(
            "'publish_dir' must not be the same directory as 'corpus_root' "
            "(the published index would end up indexing itself)"
        )
    normalized_prune = []
    prune_casefold = {}
    for raw_name in resolved["prune_dirs"]:
        name = unicodedata.normalize("NFC", raw_name)
        if not name.strip():
            config_error("config key 'prune_dirs' must not contain empty names")
        if name != name.strip():
            config_error("prune_dirs entries must not have surrounding whitespace: %r" % raw_name)
        if "/" in name or "\\" in name or name in (".", ".."):
            config_error(
                "prune_dirs entries are single directory segment names, "
                "not paths: %r" % name
            )
        folded = name.casefold()
        if folded in prune_casefold:
            config_error(
                "duplicate/ambiguous prune_dirs entries %r and %r"
                % (prune_casefold[folded], raw_name)
            )
        prune_casefold[folded] = raw_name
        normalized_prune.append(name)
    resolved["prune_dirs"] = normalized_prune

    normalized_globs = []
    for raw_glob in resolved["metadata_only_globs"]:
        glob = unicodedata.normalize("NFC", raw_glob)
        if not glob.strip():
            config_error("config key 'metadata_only_globs' must not contain empty globs")
        if glob != glob.strip():
            config_error("metadata_only_globs must not have surrounding whitespace: %r" % raw_glob)
        if "\\" in glob:
            config_error(
                "metadata_only_globs always use '/' separators, never '\\': %r" % raw_glob
            )
        glob_parts = glob.split("/")
        if glob.startswith("/") or any(part in ("", ".", "..") for part in glob_parts):
            config_error(
                "metadata_only_globs must be clean corpus-relative patterns "
                "without empty, '.' or '..' segments: %r" % raw_glob
            )
        normalized_globs.append(glob)
    resolved["metadata_only_globs"] = normalized_globs

    normalized_roots = []
    seen_roots = set()
    for raw_root in resolved["access_roots"]:
        # Access roots are authorization locators, not search text.  Preserve
        # their exact on-disk Unicode spelling so two canonically equivalent
        # POSIX names cannot accidentally authorize one another.
        root = raw_root
        if not root.strip() or root != root.strip():
            config_error("access_roots entries must be non-empty and have no surrounding whitespace")
        if not os.path.isabs(root):
            if "\\" in root:
                config_error("relative access_roots use '/' separators, never '\\': %r" % raw_root)
            raw_parts = root.split("/")
            if root != "." and any(part in ("", ".", "..") for part in raw_parts):
                config_error(
                    "access_roots must not contain empty, '.' or '..' path segments: %r"
                    % raw_root
                )
            parts = pathlib.PurePosixPath(root).parts
            if root.startswith("/") or any(part in ("", "..") for part in parts):
                config_error("access_roots entries must stay inside corpus_root: %r" % raw_root)
            root = "." if root in ("", ".") else pathlib.PurePosixPath(*parts).as_posix()
        folded = unicodedata.normalize("NFC", root).casefold()
        if folded in seen_roots:
            config_error("duplicate/ambiguous access_roots entry: %r" % raw_root)
        seen_roots.add(folded)
        normalized_roots.append(root)
    if not normalized_roots:
        config_error("config key 'access_roots' must contain at least one authorized root")
    if "." in normalized_roots and len(normalized_roots) != 1:
        config_error("access_roots '.' already means the whole corpus and cannot be combined")
    resolved["access_roots"] = normalized_roots

    corpus_id_file = unicodedata.normalize("NFC", resolved["corpus_id_file"])
    if (
        not corpus_id_file.strip()
        or corpus_id_file != corpus_id_file.strip()
        or corpus_id_file in (".", "..")
        or "/" in corpus_id_file
        or "\\" in corpus_id_file
    ):
        config_error("corpus_id_file must be one non-empty filename segment")
    resolved["corpus_id_file"] = corpus_id_file

    if resolved["smoke_term"] is not None and not resolved["smoke_term"].strip():
        config_error("config key 'smoke_term' must be null or a non-empty term")
    if resolved["min_file_count"] < 0:
        config_error("min_file_count must be zero or greater")
    if not 0 <= resolved["max_file_count_drop_fraction"] <= 1:
        config_error("max_file_count_drop_fraction must be between 0 and 1")
    if not 0 <= resolved["max_walk_retries"] <= 10:
        config_error("max_walk_retries must be between 0 and 10")
    if not 1 <= resolved["max_directory_entries"] <= 1000000:
        config_error("max_directory_entries must be between 1 and 1000000")
    if not 1 <= resolved["max_directory_depth"] <= 512:
        config_error("max_directory_depth must be between 1 and 512")
    if not 1 <= resolved["max_total_entries"] <= 10000000:
        config_error("max_total_entries must be between 1 and 10000000")
    if not 1 <= resolved["max_total_files"] <= 10000000:
        config_error("max_total_files must be between 1 and 10000000")
    if not 1 <= resolved["max_total_extracted_mb"] <= 1048576:
        config_error("max_total_extracted_mb must be between 1 and 1048576")
    if not 1 <= resolved["max_index_size_mb"] <= 1048576:
        config_error("max_index_size_mb must be between 1 and 1048576")
    if not 128 <= resolved["pdf_memory_limit_mb"] <= 4096:
        config_error("pdf_memory_limit_mb must be between 128 and 4096")
    if not re.match(r"^0?[0-7]{3}$", resolved["publish_mode"]):
        config_error("publish_mode must be a three- or four-digit octal mode (for example 0640)")
    publish_mode = int(resolved["publish_mode"], 8)
    if publish_mode & 0o022:
        config_error("publish_mode must not grant group/other write permission")
    if publish_mode & 0o600 != 0o600:
        config_error("publish_mode must grant the owner read and write permission")
    resolved["publish_mode_int"] = publish_mode
    if resolved["publish_group"] is not None:
        group = resolved["publish_group"]
        if not group.strip() or group != group.strip():
            config_error("publish_group must be null or a non-empty group without surrounding whitespace")
    return resolved


# --- Text extraction ---------------------------------------------------------


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Mechanical HTML tag strip: collects text, dropping script/style bodies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _parse_xml(data):
    # XML members are already bounded, so scan the complete member: arbitrary
    # leading whitespace must not move a DTD past a short prefix check.
    upper_data = data.upper()
    for declaration in ("<!DOCTYPE", "<!ENTITY"):
        for encoding in ("ascii", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            if declaration.encode(encoding) in upper_data:
                raise ExtractionFailed("DTD/entity declarations are not accepted")
    parser = ET.XMLPullParser(events=("start", "end"))
    root = None
    nodes = 0
    depth = 0

    def consume_events():
        nonlocal root, nodes, depth
        for event, elem in parser.read_events():
            if event == "start":
                nodes += 1
                depth += 1
                if root is None:
                    root = elem
                if nodes > MAX_XML_NODES:
                    raise ContentTooBig(
                        "XML exceeds %d elements" % MAX_XML_NODES
                    )
                if depth > MAX_XML_DEPTH:
                    raise ContentTooBig(
                        "XML nesting exceeds %d levels" % MAX_XML_DEPTH
                    )
            else:
                depth -= 1

    try:
        for offset in range(0, len(data), 64 * 1024):
            parser.feed(data[offset : offset + 64 * 1024])
            consume_events()
        parser.close()
        consume_events()
    except (ET.ParseError, LookupError, UnicodeError, ValueError) as exc:
        raise ExtractionFailed("invalid XML: %s" % exc)
    if root is None or depth != 0:
        raise ExtractionFailed("invalid XML: document has no balanced root element")
    return root


def _flatten_xml_element(root, block_localnames):
    """Flatten an already bounded XML tree without serializing subtrees."""
    out = []
    stack = [("element", root)]
    while stack:
        kind, value = stack.pop()
        if kind == "text":
            out.append(value)
            continue
        elem = value
        tag = elem.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local in block_localnames:
            out.append(" ")
        if elem.text:
            out.append(elem.text)
        # Push in reverse document order.  Each child's tail follows that
        # child's complete subtree, matching ElementTree's data model.
        for child in reversed(list(elem)):
            if child.tail:
                stack.append(("text", child.tail))
            stack.append(("element", child))
    return "".join(out)


def _xml_flatten(data, block_localnames):
    """Flatten XML bytes to text, inserting a space at each block boundary.

    Text nested in the same inline run stays glued (words preserved); block
    elements (paragraphs, breaks, rows) become spaces so words do not merge
    across paragraphs. Purely structural, no semantics.
    """
    return _flatten_xml_element(_parse_xml(data), block_localnames)


def _read_bounded_stream(stream, limit, label):
    """Read at most ``limit`` bytes without ever issuing an unbounded read."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ContentTooBig("%s exceeds %d bytes" % (label, limit))
    return b"".join(chunks)


def _read_bounded_file(fullpath, limit, label):
    size = os.stat(fullpath).st_size
    if size > limit:
        raise ContentTooBig("%s exceeds %d bytes" % (label, limit))
    with open(fullpath, "rb") as handle:
        return _read_bounded_stream(handle, limit, label)


class _TextAccumulator:
    """Join text fragments while enforcing the final UTF-8 output budget."""

    def __init__(self, limit=TEXT_CAP_BYTES):
        self.limit = limit
        self.parts = []
        self.utf8_bytes = 0

    def append(self, value):
        if not value:
            return
        added = len(value.encode("utf-8")) + (1 if self.parts else 0)
        if self.utf8_bytes + added > self.limit:
            raise ContentTooBig("extracted text exceeds %d bytes" % self.limit)
        self.parts.append(value)
        self.utf8_bytes += added

    def finish(self):
        return " ".join(self.parts)


def _preflight_zip_central_directory(fullpath, source_size):
    """Validate and count a bounded central directory before ``ZipFile``.

    ``zipfile.ZipFile`` materializes one ``ZipInfo`` per central-directory
    entry.  The EOCD member count is attacker-controlled, so count the actual
    file headers inside the separately bounded directory instead of trusting
    that field before allowing ZipFile to allocate its list.
    """
    tail_size = min(source_size, 65535 + 22)
    with open(fullpath, "rb") as source:
        source.seek(source_size - tail_size)
        tail = source.read(tail_size)
        search_to = len(tail)
        eocd = -1
        while True:
            candidate = tail.rfind(b"PK\x05\x06", 0, search_to)
            if candidate < 0:
                break
            if len(tail) - candidate >= 22:
                comment_size = struct.unpack_from("<H", tail, candidate + 20)[0]
                if candidate + 22 + comment_size == len(tail):
                    eocd = candidate
                    break
            search_to = candidate
        if eocd < 0:
            raise ExtractionFailed("invalid ZIP package: end record is missing")

        disk_number, directory_disk, members_on_disk, declared_members = struct.unpack_from(
            "<4H", tail, eocd + 4
        )
        directory_size, directory_offset = struct.unpack_from("<2L", tail, eocd + 12)
        if disk_number != 0 or directory_disk != 0 or members_on_disk != declared_members:
            raise ExtractionFailed("multi-disk ZIP packages are not accepted")
        if (
            declared_members == 0xFFFF
            or declared_members > MAX_ZIP_MEMBERS
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
            or directory_size > ARCHIVE_DIRECTORY_CAP_BYTES
        ):
            raise ContentTooBig("archive central directory exceeds configured limits")

        eocd_offset = source_size - tail_size + eocd
        # Python's zipfile supports archives with bytes prepended.  Derive the
        # same non-negative concatenation adjustment without trusting the EOCD
        # count or allocating any ZipInfo objects.
        concat_offset = eocd_offset - directory_size - directory_offset
        if concat_offset < 0:
            raise ExtractionFailed("invalid ZIP central-directory offset")
        directory_start = directory_offset + concat_offset
        directory_end = directory_start + directory_size
        if directory_end != eocd_offset:
            raise ExtractionFailed("invalid ZIP central-directory bounds")

        source.seek(directory_start)
        consumed = 0
        actual_members = 0
        while consumed < directory_size:
            remaining = directory_size - consumed
            if remaining < 4:
                raise ExtractionFailed("truncated ZIP central directory")
            signature = source.read(4)
            if len(signature) != 4:
                raise ExtractionFailed("truncated ZIP central directory")
            consumed += 4
            if signature == b"PK\x01\x02":
                if directory_size - consumed < 42:
                    raise ExtractionFailed("truncated ZIP central-directory entry")
                fixed = source.read(42)
                if len(fixed) != 42:
                    raise ExtractionFailed("truncated ZIP central-directory entry")
                consumed += 42
                name_size, extra_size, comment_size = struct.unpack_from("<3H", fixed, 24)
                variable_size = name_size + extra_size + comment_size
                if variable_size > directory_size - consumed:
                    raise ExtractionFailed("truncated ZIP central-directory entry")
                source.seek(variable_size, os.SEEK_CUR)
                consumed += variable_size
                actual_members += 1
                if actual_members > MAX_ZIP_MEMBERS:
                    raise ContentTooBig(
                        "archive has more than %d members" % MAX_ZIP_MEMBERS
                    )
                continue
            if signature == b"PK\x05\x05":
                # Optional central-directory digital signature.
                if directory_size - consumed < 2:
                    raise ExtractionFailed("truncated ZIP digital signature")
                raw_size = source.read(2)
                if len(raw_size) != 2:
                    raise ExtractionFailed("truncated ZIP digital signature")
                consumed += 2
                signature_size = struct.unpack("<H", raw_size)[0]
                if signature_size > directory_size - consumed:
                    raise ExtractionFailed("truncated ZIP digital signature")
                source.seek(signature_size, os.SEEK_CUR)
                consumed += signature_size
                if consumed != directory_size:
                    raise ExtractionFailed("ZIP digital signature is not final")
                break
            raise ExtractionFailed("invalid ZIP central-directory signature")

        if actual_members != declared_members:
            raise ExtractionFailed(
                "ZIP member count mismatch (declared %d, found %d)"
                % (declared_members, actual_members)
            )


def ensure_zip_deflate_backend():
    """Functionally probe zipfile's DEFLATE backend with fixed raw data."""
    try:
        backend = zipfile.zlib
        if backend is None:
            raise RuntimeError("zlib module is unavailable")
        decompressor = backend.decompressobj(-15)
        value = decompressor.decompress(ZIP_DEFLATE_PROBE)
        value += decompressor.flush()
        if not decompressor.eof:
            raise RuntimeError("DEFLATE stream did not terminate")
    except Exception as exc:
        raise DependencyUnavailable(
            "Python ZIP/DEFLATE support failed its functional probe: %s" % exc
        )
    if value != b"APLIDOCSZIPPROBE":
        raise DependencyUnavailable(
            "Python ZIP/DEFLATE functional probe returned unexpected data"
        )


class _ArchiveReader:
    """Bounded reader for the XML members used from an Office/ODF ZIP."""

    def __init__(self, fullpath):
        ensure_zip_deflate_backend()
        try:
            source_size = os.stat(fullpath).st_size
            if source_size > ARCHIVE_SOURCE_CAP_BYTES:
                raise ContentTooBig("archive exceeds %d bytes" % ARCHIVE_SOURCE_CAP_BYTES)
            _preflight_zip_central_directory(fullpath, source_size)
            self.archive = zipfile.ZipFile(fullpath)
        except ContentTooBig:
            raise
        except (zipfile.BadZipFile, UnicodeDecodeError, NotImplementedError) as exc:
            raise ExtractionFailed("invalid ZIP package: %s" % exc)
        infos = self.archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            self.archive.close()
            raise ContentTooBig("archive has more than %d members" % MAX_ZIP_MEMBERS)
        self.infos = {info.filename: info for info in infos}
        self.total = 0

    def close(self):
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def names(self):
        return set(self.infos)

    def read(self, name, required=True):
        info = self.infos.get(name)
        if info is None:
            if required:
                raise ExtractionFailed("ZIP member is missing: %s" % name)
            return None
        if info.file_size > ARCHIVE_MEMBER_CAP_BYTES:
            raise ContentTooBig(
                "ZIP member %s exceeds %d bytes" % (name, ARCHIVE_MEMBER_CAP_BYTES)
            )
        if info.flag_bits & 0x1:
            raise ExtractionFailed("encrypted ZIP members are not accepted: %s" % name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ExtractionFailed(
                "unsupported ZIP compression method %d for %s"
                % (info.compress_type, name)
            )
        remaining_total = ARCHIVE_TOTAL_CAP_BYTES - self.total
        if remaining_total < 0 or info.file_size > remaining_total:
            raise ContentTooBig("selected ZIP content exceeds %d bytes" % ARCHIVE_TOTAL_CAP_BYTES)
        try:
            with self.archive.open(info, "r") as stream:
                data = _read_bounded_stream(stream, ARCHIVE_MEMBER_CAP_BYTES, name)
        except ContentTooBig:
            raise
        except (EOFError, RuntimeError, UnicodeError, zipfile.BadZipFile) as exc:
            raise ExtractionFailed("cannot read ZIP member %s: %s" % (name, exc))
        except Exception as exc:
            # A malformed raw DEFLATE stream is reported as zlib.error rather
            # than BadZipFile by some Python releases.  It is a bad document,
            # not a process-wide missing dependency.
            zlib_error = getattr(getattr(zipfile, "zlib", None), "error", None)
            if zlib_error is not None and isinstance(exc, zlib_error):
                raise ExtractionFailed("cannot read ZIP member %s: %s" % (name, exc))
            raise
        self.total += len(data)
        if self.total > ARCHIVE_TOTAL_CAP_BYTES:
            raise ContentTooBig("selected ZIP content exceeds %d bytes" % ARCHIVE_TOTAL_CAP_BYTES)
        return data


def _extract_word(fullpath):
    with _ArchiveReader(fullpath) as archive:
        data = archive.read("word/document.xml")
    return _xml_flatten(data, XML_BLOCK_LOCALNAMES)


def _extract_excel(fullpath):
    texts = _TextAccumulator()
    with _ArchiveReader(fullpath) as archive:
        names = archive.names()
        workbook_data = archive.read("xl/workbook.xml")
        relationships = {}
        rel_data = archive.read("xl/_rels/workbook.xml.rels", required=False)
        if rel_data is not None:
            rel_root = _parse_xml(rel_data)
            for rel in rel_root.iter():
                rel_id = rel.get("Id")
                target = rel.get("Target")
                if rel_id and target:
                    target = target.replace("\\", "/")
                    if target.startswith("/"):
                        member = target.lstrip("/")
                    else:
                        member = str(pathlib.PurePosixPath("xl") / target)
                    # Reject relationships that escape the package directory.
                    member_parts = pathlib.PurePosixPath(member).parts
                    if ".." not in member_parts:
                        relationships[rel_id] = pathlib.PurePosixPath(*member_parts).as_posix()

        shared = []
        shared_data = archive.read("xl/sharedStrings.xml", required=False)
        if shared_data is not None:
            shared_root = _parse_xml(shared_data)
            root_local = (
                shared_root.tag.rsplit("}", 1)[-1]
                if "}" in shared_root.tag else shared_root.tag
            )
            if root_local != "sst":
                raise ExtractionFailed("invalid XLSX shared string table root")
            shared_items = []
            direct_item_ids = set()
            for item in list(shared_root):
                local = item.tag.rsplit("}", 1)[-1] if "}" in item.tag else item.tag
                if local == "si":
                    shared_items.append(item)
                    direct_item_ids.add(id(item))
            for item in shared_root.iter():
                local = item.tag.rsplit("}", 1)[-1] if "}" in item.tag else item.tag
                if local == "si" and id(item) not in direct_item_ids:
                    raise ExtractionFailed("nested XLSX shared string item is invalid")
            shared_utf8_bytes = 0
            for item in shared_items:
                value = _collapse_whitespace(
                    _flatten_xml_element(item, frozenset())
                )
                added = len(value.encode("utf-8")) + (1 if shared else 0)
                if shared_utf8_bytes + added > TEXT_CAP_BYTES:
                    raise ContentTooBig(
                        "XLSX shared string table exceeds %d extracted bytes"
                        % TEXT_CAP_BYTES
                    )
                shared.append(value)
                shared_utf8_bytes += added

        workbook_root = _parse_xml(workbook_data)
        sheet_members = []
        relationship_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for elem in workbook_root.iter():
            local = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
            if local != "sheet":
                continue
            name = elem.get("name")
            if name:
                texts.append(name)
            rel_id = elem.get(relationship_ns) or elem.get("r:id")
            member = relationships.get(rel_id)
            if member in names:
                sheet_members.append(member)

        # Some minimal producers omit workbook relationships.  Preserve a
        # deterministic fallback without indexing arbitrary ZIP members.
        if not sheet_members:
            sheet_members = sorted(
                name for name in names
                if re.match(r"^xl/worksheets/sheet[0-9]+\.xml$", name)
            )

        for member in sheet_members:
            root = _parse_xml(archive.read(member))
            for cell in root.iter():
                local = cell.tag.rsplit("}", 1)[-1] if "}" in cell.tag else cell.tag
                if local != "c":
                    continue
                cell_type = cell.get("t")
                formula = None
                value = None
                inline = None
                for child in cell:
                    child_local = child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag
                    if child_local == "f":
                        formula = "".join(child.itertext())
                    elif child_local == "v":
                        value = "".join(child.itertext())
                    elif child_local == "is":
                        inline = "".join(child.itertext())
                if cell_type == "s" and value is not None:
                    # Shared-string indexes are bounded by the XML node cap.
                    # Reject a huge decimal lexeme before int(), which had no
                    # digit limit on supported Python 3.8-3.10 runtimes.
                    if (
                        value.isascii()
                        and value.isdecimal()
                        and len(value) <= len(str(MAX_XML_NODES))
                    ):
                        index = int(value)
                    else:
                        index = -1
                    if 0 <= index < len(shared):
                        texts.append(shared[index])
                elif inline is not None:
                    texts.append(inline)
                if formula:
                    texts.append(formula)
                if value is not None and cell_type != "s":
                    texts.append(value)
    return texts.finish()


def _extract_odt(fullpath):
    with _ArchiveReader(fullpath) as archive:
        data = archive.read("content.xml")
    return _xml_flatten(data, XML_BLOCK_LOCALNAMES)


def _read_text_file(fullpath):
    raw = _read_bounded_file(fullpath, TEXT_SOURCE_CAP_BYTES, "text file")
    # Test UTF-32 before UTF-16 because their little-endian BOMs share a prefix.
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            try:
                return raw[len(bom) :].decode(encoding)
            except UnicodeDecodeError as exc:
                raise ExtractionFailed("invalid %s text: %s" % (encoding, exc))
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ExtractionFailed("text decoding failed")


def _extract_html(fullpath):
    parser = _HTMLTextExtractor()
    parser.feed(_read_text_file(fullpath))
    return parser.get_text()


def _minimal_pdf_probe_bytes():
    """Return a tiny valid PDF with a known token for the functional probe."""
    content = b"BT /F1 12 Tf 6 36 Td (APLIDOCSPROBE) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        (b"<< /Length %d >>\nstream\n" % len(content))
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(("%d 0 obj\n" % number).encode("ascii"))
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(("%010d 00000 n \n" % offset).encode("ascii"))
    data.extend(
        (
            "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_offset)
        ).encode("ascii")
    )
    return bytes(data)


def _pdf_child_limits(memory_limit_bytes):
    """Build the POSIX child hook that caps Poppler address space and CPU."""
    def bounded_limit(category, requested):
        _soft, hard = resource.getrlimit(category)
        target = requested
        if hard != resource.RLIM_INFINITY:
            target = min(target, hard)
        resource.setrlimit(category, (target, target))

    def apply_limits():
        bounded_limit(resource.RLIMIT_AS, memory_limit_bytes)
        bounded_limit(resource.RLIMIT_CPU, PDFTOTEXT_TIMEOUT + 5)
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply_limits


def _extract_pdf(
    fullpath, memory_limit_bytes=DEFAULT_PDF_MEMORY_LIMIT_MB * 1024 * 1024
):
    """Run pdftotext with finite input, output and wall-clock budgets."""
    try:
        if os.stat(fullpath).st_size > PDF_SOURCE_CAP_BYTES:
            raise ContentTooBig("PDF exceeds %d bytes" % PDF_SOURCE_CAP_BYTES)
    except FileNotFoundError:
        # Unit callers may use a synthetic path with a mocked subprocess.
        pass
    if shutil.which("pdftotext") is None:
        raise DependencyUnavailable("pdftotext executable is not available on PATH")
    try:
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "posix":
            if resource is None:
                raise DependencyUnavailable(
                    "POSIX resource module is required to bound pdftotext memory/CPU"
                )
            if not all(
                hasattr(resource, name)
                for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIM_INFINITY")
            ):
                raise DependencyUnavailable(
                    "POSIX resource module lacks limits required for pdftotext"
                )
            popen_kwargs["preexec_fn"] = _pdf_child_limits(memory_limit_bytes)
        proc = subprocess.Popen(
            ["pdftotext", "-q", "-enc", "UTF-8", fullpath, "-"],
            **popen_kwargs
        )
    except FileNotFoundError as exc:
        raise DependencyUnavailable("pdftotext executable is not available: %s" % exc)
    except OSError as exc:
        raise DependencyUnavailable("cannot start pdftotext: %s" % exc)
    except subprocess.SubprocessError as exc:
        raise DependencyUnavailable(
            "cannot apply pdftotext child resource limits: %s" % exc
        )

    chunks = []
    total = 0
    deadline = time.monotonic() + PDFTOTEXT_TIMEOUT
    try:
        stream = proc.stdout
        use_selector = False
        selector = None
        try:
            stream.fileno()
            selector = selectors.DefaultSelector()
            selector.register(stream, selectors.EVENT_READ)
            use_selector = True
        except (AttributeError, OSError, ValueError):
            if selector is not None:
                selector.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("pdftotext", PDFTOTEXT_TIMEOUT)
            if use_selector:
                if not selector.select(min(remaining, 1.0)):
                    if proc.poll() is not None:
                        break
                    continue
            read_size = min(64 * 1024, TEXT_CAP_BYTES + 1 - total)
            if use_selector:
                chunk = os.read(stream.fileno(), read_size)
            else:
                chunk = stream.read(read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > TEXT_CAP_BYTES:
                raise ContentTooBig("pdftotext output exceeds %d bytes" % TEXT_CAP_BYTES)
        if selector is not None:
            selector.close()
            selector = None
        returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        if returncode != 0:
            raise ExtractionFailed("pdftotext exit code %d" % returncode)
    except ContentTooBig:
        _stop_process(proc)
        raise
    except subprocess.TimeoutExpired:
        _stop_process(proc)
        raise ExtractionFailed("pdftotext timed out after %d seconds" % PDFTOTEXT_TIMEOUT)
    finally:
        try:
            if selector is not None:
                selector.close()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                _stop_process(proc)
        except Exception:
            pass
    return b"".join(chunks).decode("utf-8", "replace")


def _stop_process(proc):
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def _collapse_whitespace(text):
    return " ".join(text.split())


def _glob_escape(name):
    """Escape SQLite GLOB metacharacters so `name` only matches literally.

    `[` must be escaped first so the brackets introduced for `*` and `?` are
    not themselves re-escaped.
    """
    return name.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


# --- Path-derived fields -----------------------------------------------------


def derive_area(relpath):
    """Top-level path segment, or ``.`` for files loose at corpus root."""
    idx = relpath.find("/")
    if idx == -1:
        return ROOT_AREA_SENTINEL
    return relpath[:idx]


def derive_path_year(relpath):
    """First pure-year directory segment (4 digits, 1990-2099) in the path."""
    parts = relpath.split("/")[:-1]
    for part in parts:
        if len(part) == 4 and part.isascii() and part.isdecimal():
            value = int(part)
            if 1990 <= value <= 2099:
                return value
    return None


def derive_name_meta(filename, pattern):
    """Apply the optional filename regex; return a JSON groupdict or None.

    On a match, named groups with a non-null value are stored as a JSON object
    in the `name_meta` column, letting users encode their filename convention
    (job codes, client names, document ids) without any schema change. Returns
    None when no pattern is configured, the pattern does not match, or every
    named group is null.
    """
    if pattern is None:
        return None
    if not (
        os.name == "posix"
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_REAL")
        and hasattr(signal, "SIGALRM")
    ):
        raise RuntimeError(
            "bounded filename_pattern matching requires POSIX setitimer support"
        )

    def timeout_handler(_signum, _frame):
        raise FilenamePatternTimeout(
            "filename_pattern exceeded %.3f seconds for one filename"
            % FILENAME_PATTERN_TIMEOUT_SECONDS
        )

    try:
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer != (0.0, 0.0):
            raise RuntimeError(
                "filename_pattern cannot safely share an active process alarm"
            )
        previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(
            "filename_pattern must run in the POSIX builder's main thread: %s"
            % exc
        )
    try:
        signal.setitimer(
            signal.ITIMER_REAL, FILENAME_PATTERN_TIMEOUT_SECONDS
        )
        match = pattern.search(filename)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    if match is None:
        return None
    groups = {key: value for key, value in match.groupdict().items() if value is not None}
    if not groups:
        return None
    return json.dumps(groups, ensure_ascii=False, sort_keys=True)


def file_extension(filename):
    dot = filename.rfind(".")
    if dot <= 0:  # no dot, or dotfile like ".DS_Store"
        return ""
    return filename[dot + 1 :].lower()


def extractor_for_extension(ext):
    if ext in PDF_EXT:
        return "pdftotext"
    if ext in WORD_EXT:
        return "ooxml-word"
    if ext in EXCEL_EXT:
        return "ooxml-excel"
    if ext in ODT_EXT:
        return "odt"
    if ext in TEXT_EXT:
        return "text"
    if ext in HTML_EXT:
        return "html"
    return None


# --- Builder -----------------------------------------------------------------


class IndexBuilder:
    """Build one access-cohort index from a descriptor-anchored POSIX walk."""

    def __init__(self, config, output_path):
        self.config = config
        self.corpus_root = os.path.abspath(config["corpus_root"])
        self.publish_dir = os.path.abspath(config["publish_dir"])
        self.output_path = os.path.abspath(output_path)
        self.corpus_id_file = config.get("corpus_id_file", ".aplidocs-corpus-id")
        self.prune_dirs = [unicodedata.normalize("NFC", item) for item in config.get("prune_dirs", [])]
        self.prune_segments = frozenset(self.prune_dirs) | ALWAYS_PRUNE_DIRS
        self.prune_casefold = {item.casefold(): item for item in self.prune_dirs}
        normalized_metadata_globs = [
            unicodedata.normalize("NFC", item)
            for item in config.get("metadata_only_globs", [])
        ]
        self.metadata_only_res = [
            re.compile(fnmatch.translate(item)) for item in normalized_metadata_globs
        ]
        self.metadata_only_casefold_res = [
            re.compile(fnmatch.translate(item.casefold()))
            for item in normalized_metadata_globs
        ]
        pattern = config.get("filename_pattern")
        if pattern is not None and len(pattern.encode("utf-8")) > 1024:
            config_error("config key 'filename_pattern' must not exceed 1024 UTF-8 bytes")
        try:
            self.filename_pattern = re.compile(pattern) if pattern else None
        except re.error as exc:
            config_error("config key 'filename_pattern' is not a valid regex: %s" % exc)
        if self.filename_pattern is not None and not self.filename_pattern.groupindex:
            config_error(
                "config key 'filename_pattern' must contain at least one named "
                "group (?P<name>...); only named groups are stored"
            )
        self.smoke_term = config.get("smoke_term")
        self.allow_empty = bool(config.get("allow_empty", False))
        self.min_file_count = int(config.get("min_file_count", 1))
        self.max_drop_fraction = float(config.get("max_file_count_drop_fraction", 0.5))
        self.max_walk_retries = int(config.get("max_walk_retries", 2))
        self.max_directory_entries = int(config.get("max_directory_entries", 50000))
        self.max_directory_depth = int(config.get("max_directory_depth", 128))
        self.max_total_entries = int(config.get("max_total_entries", 2000000))
        self.max_total_files = int(config.get("max_total_files", 1000000))
        self.max_total_extracted_bytes = int(
            config.get("max_total_extracted_mb", DEFAULT_MAX_TOTAL_EXTRACTED_MB)
        ) * 1024 * 1024
        self.max_index_size_bytes = int(
            config.get("max_index_size_mb", DEFAULT_MAX_INDEX_SIZE_MB)
        ) * 1024 * 1024
        self.pdf_memory_limit_bytes = int(
            config.get("pdf_memory_limit_mb", DEFAULT_PDF_MEMORY_LIMIT_MB)
        ) * 1024 * 1024
        self.publish_mode = int(
            config.get("publish_mode_int", int(config.get("publish_mode", "0640"), 8))
        )
        self.publish_group = config.get("publish_group")
        self.access_policy_id = config.get("access_policy_id", "direct-api")
        self.access_policy_generation = config.get("access_policy_generation", "0")
        if not _is_canonical_policy_generation(self.access_policy_generation):
            raise ValueError(
                "access_policy_generation must be a canonical non-negative decimal string"
            )
        raw_access_roots = config.get("access_roots", [self.corpus_root])
        self.access_roots = self._resolve_access_roots(raw_access_roots)
        policy_payload = {
            "access_policy_generation": self.access_policy_generation,
            "access_policy_id": self.access_policy_id,
            "access_roots": self.access_roots,
            "metadata_only_globs": config.get("metadata_only_globs", []),
            "prune_dirs": self.prune_dirs,
            "publish_group": self.publish_group,
            "publish_mode": "%04o" % self.publish_mode,
        }
        self.policy_digest = hashlib.sha256(
            json.dumps(policy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest()

        self.conn = None
        self.prev_conn = None
        self.prev_meta = {}
        self.previous_file_count = None
        self.previous_policy_digest = None
        self.walk_count = 0
        self._visited_entry_count = 0
        self.status_counts = collections.Counter()
        self.cache_hits = 0
        self.excluded_access_count = 0
        self.total_extracted_bytes = 0
        self.corpus_uuid = None
        self._corpus_marker_sig = None
        self._lock_fd = None
        self._publish_dir_fd = None
        self._scratch_lock_fd = None
        self._publish_identity = None
        self._corpus_identity = None
        self._scratch_identity = None
        self._scratch_output_identity = None
        self._published_db_identity = None
        self._pdf_checked = False
        self._archive_checked = False
        self._snapshot_dirs = {}
        self._snapshot_files = {}
        self._snapshot_routes = {}
        self._building = False
        self._cache_disabled = False
        self._seen_access_roots = set()
        self._publication_replaced = False

    def _resolve_access_roots(self, roots):
        resolved = []
        for raw in roots:
            if os.path.isabs(raw):
                absolute = os.path.abspath(raw)
                try:
                    inside = os.path.commonpath((self.corpus_root, absolute)) == self.corpus_root
                except ValueError:
                    inside = False
                if not inside:
                    raise ValueError("access_root is outside corpus_root: %r" % raw)
                rel = os.path.relpath(absolute, self.corpus_root).replace(os.sep, "/")
            else:
                rel = raw.replace(os.sep, "/")
            if rel in ("", "."):
                rel = "."
            parts = pathlib.PurePosixPath(rel).parts
            if rel.startswith("/") or ".." in parts:
                raise ValueError("access_root escapes corpus_root: %r" % raw)
            resolved.append("." if rel == "." else pathlib.PurePosixPath(*parts).as_posix())
        # Overlaps are redundant and can hide a typo in a security boundary.
        for index, left in enumerate(resolved):
            for right in resolved[index + 1 :]:
                if left == "." or right == "." or left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                    raise ValueError("access_roots overlap or duplicate: %r and %r" % (left, right))
        return sorted(resolved)

    @staticmethod
    def _stat_signature(st):
        return (
            int(st.st_dev), int(st.st_ino), int(st.st_mode), int(st.st_size),
            int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1000000000))),
            int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1000000000))),
        )

    @staticmethod
    def _identity(st):
        return int(st.st_dev), int(st.st_ino)

    @staticmethod
    def _is_inside(path, directory):
        try:
            return os.path.commonpath((os.path.realpath(path), os.path.realpath(directory))) == os.path.realpath(directory)
        except ValueError:
            return False

    def _require_posix_features(self):
        if fcntl is None or os.name != "posix":
            raise RuntimeError(
                "the index builder requires POSIX/Linux descriptor APIs; "
                "the read-only aplidocs_cli.py remains supported on Windows"
            )
        missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK") if not hasattr(os, name)]
        if missing:
            raise RuntimeError("required secure-open flags are unavailable: %s" % ", ".join(missing))

    def _validate_paths(self):
        self._require_posix_features()
        if self._is_inside(self.output_path, self.corpus_root):
            raise RuntimeError("scratch/output path must be outside corpus_root: %r" % self.output_path)
        if self._is_inside(self.output_path, self.publish_dir):
            raise RuntimeError(
                "scratch/output path must be outside publish_dir: %r"
                % self.output_path
            )
        if os.path.realpath(self.publish_dir) == os.path.realpath(self.corpus_root):
            raise RuntimeError("publish_dir must not be corpus_root")
        root_lstat = os.lstat(self.corpus_root)
        if not statmod.S_ISDIR(root_lstat.st_mode) or statmod.S_ISLNK(root_lstat.st_mode):
            raise RuntimeError("corpus_root must be a real directory, not a symlink")

    # -- corpus marker and publication lock --

    def acquire_publish_lock(self):
        if self._lock_fd is not None:
            return
        self._validate_paths()
        try:
            publish_stat = os.lstat(self.publish_dir)
        except FileNotFoundError:
            raise RuntimeError(
                "publish_dir must already exist with the cohort ACL applied: %r" % self.publish_dir
            )
        if not statmod.S_ISDIR(publish_stat.st_mode) or statmod.S_ISLNK(publish_stat.st_mode):
            raise RuntimeError("publish_dir must be a real directory, not a symlink")
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            publish_fd = os.open(self.publish_dir, dir_flags)
        except OSError as exc:
            raise RuntimeError("cannot open publish_dir securely %r: %s" % (self.publish_dir, exc))
        opened_publish_stat = os.fstat(publish_fd)
        if self._identity(opened_publish_stat) != self._identity(publish_stat):
            os.close(publish_fd)
            raise RuntimeError("publish_dir was replaced while opening it")
        try:
            corpus_fd = self._open_corpus_root()
            try:
                self._corpus_identity = self._identity(os.fstat(corpus_fd))
                if self._corpus_identity == self._identity(opened_publish_stat):
                    raise RuntimeError(
                        "publish_dir resolves to the corpus_root directory by filesystem identity"
                    )
            finally:
                os.close(corpus_fd)
        except BaseException:
            os.close(publish_fd)
            raise
        self._publish_identity = self._identity(opened_publish_stat)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        lock_path = os.path.join(self.publish_dir, PUBLISH_LOCK_NAME)
        try:
            fd = os.open(PUBLISH_LOCK_NAME, flags, 0o600, dir_fd=publish_fd)
        except OSError as exc:
            os.close(publish_fd)
            raise RuntimeError("cannot open publication lock %r: %s" % (lock_path, exc))
        try:
            lock_stat = os.fstat(fd)
            if not statmod.S_ISREG(lock_stat.st_mode):
                raise RuntimeError("publication lock is not a regular file: %r" % lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError("another build is publishing to %r" % self.publish_dir)
                raise RuntimeError("cannot lock %r: %s" % (lock_path, exc))
        except BaseException:
            os.close(fd)
            os.close(publish_fd)
            raise
        self._publish_dir_fd = publish_fd
        self._lock_fd = fd

    def _assert_publish_dir_current(self):
        if self._publish_dir_fd is None:
            raise RuntimeError("publish_dir descriptor is not open")
        try:
            named = os.stat(self.publish_dir, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("publish_dir disappeared or became inaccessible: %s" % exc)
        opened = os.fstat(self._publish_dir_fd)
        if self._identity(named) != self._identity(opened) or not statmod.S_ISDIR(named.st_mode):
            raise RuntimeError("publish_dir pathname was replaced during the build")

    def _desired_publication_binding(self):
        if not self.corpus_uuid:
            raise RuntimeError("corpus UUID must be known before binding publish_dir")
        return {
            "version": PUBLICATION_BINDING_VERSION,
            "corpus_uuid": self.corpus_uuid,
            "access_policy_id": self.access_policy_id,
            "access_policy_generation": self.access_policy_generation,
            "policy_digest": self.policy_digest,
            "publish_mode": "%04o" % self.publish_mode,
            "publish_gid": self._expected_publication_gid(),
            "transition_from": None,
        }

    @staticmethod
    def _parse_publication_binding(raw):
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "version", "corpus_uuid", "access_policy_id",
                "access_policy_generation", "policy_digest", "publish_mode",
                "publish_gid", "transition_from",
            }:
                raise ValueError("unexpected fields")
            if type(value["version"]) is not int or value[
                "version"
            ] != PUBLICATION_BINDING_VERSION:
                raise ValueError("unsupported marker version")
            if type(value["corpus_uuid"]) is not str:
                raise ValueError("corpus_uuid must be a string")
            if str(uuid.UUID(value["corpus_uuid"])) != value["corpus_uuid"]:
                raise ValueError("corpus_uuid is not canonical")
            if type(value["access_policy_id"]) is not str or not value[
                "access_policy_id"
            ]:
                raise ValueError("access_policy_id is empty")
            generation = value["access_policy_generation"]
            if not _is_canonical_policy_generation(generation):
                raise ValueError("access_policy_generation is not canonical")
            digest = value["policy_digest"]
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("policy_digest is not lowercase SHA-256 hex")
            publish_mode = value["publish_mode"]
            if (
                type(publish_mode) is not str
                or re.fullmatch(r"0[0-7]{3}", publish_mode) is None
            ):
                raise ValueError("publish_mode is not canonical octal")
            mode_value = int(publish_mode, 8)
            if mode_value & 0o022 or mode_value & 0o600 != 0o600:
                raise ValueError("publish_mode is not a safe publication mode")
            publish_gid = value["publish_gid"]
            if publish_gid is not None and (
                type(publish_gid) is not int
                or publish_gid < 0
                or publish_gid > 9223372036854775807
            ):
                raise ValueError("publish_gid is not a non-negative integer or null")
            if mode_value & 0o070 and publish_gid is None:
                raise ValueError(
                    "group-readable publish_mode requires a pinned publish_gid"
                )
            transition = value["transition_from"]
            if transition is not None:
                if not isinstance(transition, dict) or set(transition) != {
                    "access_policy_generation",
                    "policy_digest",
                    "publish_mode",
                    "publish_gid",
                }:
                    raise ValueError("transition_from has unexpected fields")
                if not _is_canonical_policy_generation(
                    transition["access_policy_generation"]
                ):
                    raise ValueError("transition_from generation is not canonical")
                previous_digest = transition["policy_digest"]
                if (
                    type(previous_digest) is not str
                    or len(previous_digest) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in previous_digest
                    )
                ):
                    raise ValueError("transition_from digest is invalid")
                previous_mode = transition["publish_mode"]
                if (
                    type(previous_mode) is not str
                    or re.fullmatch(r"0[0-7]{3}", previous_mode) is None
                ):
                    raise ValueError("transition_from mode is invalid")
                previous_mode_value = int(previous_mode, 8)
                if (
                    previous_mode_value & 0o022
                    or previous_mode_value & 0o600 != 0o600
                ):
                    raise ValueError("transition_from mode is unsafe")
                previous_gid = transition["publish_gid"]
                if previous_gid is not None and (
                    type(previous_gid) is not int
                    or previous_gid < 0
                    or previous_gid > 9223372036854775807
                ):
                    raise ValueError("transition_from GID is invalid")
                if previous_mode_value & 0o070 and previous_gid is None:
                    raise ValueError(
                        "group-readable transition_from mode requires a pinned GID"
                    )
                if int(transition["access_policy_generation"]) >= int(generation):
                    raise ValueError(
                        "transition_from generation must be lower than current"
                    )
        except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("invalid publication cohort marker: %s" % exc)
        return value

    def _read_publication_binding(self, missing_ok=False):
        """Read the descriptor-anchored cohort marker without following links."""
        self._assert_publish_dir_current()
        flags = (
            os.O_RDONLY
            | os.O_NONBLOCK
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(
                PUBLICATION_BINDING_NAME, flags, dir_fd=self._publish_dir_fd
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise RuntimeError(
                "publication cohort marker %r is missing"
                % PUBLICATION_BINDING_NAME
            )
        except OSError as exc:
            raise RuntimeError(
                "cannot open publication cohort marker securely: %s" % exc
            )
        try:
            before = os.fstat(fd)
            if (
                not statmod.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > PUBLICATION_BINDING_MAX_BYTES
            ):
                raise RuntimeError(
                    "publication cohort marker must be a private regular file "
                    "no larger than %d bytes" % PUBLICATION_BINDING_MAX_BYTES
                )
            marker_mode = statmod.S_IMODE(before.st_mode)
            if marker_mode & 0o022 or marker_mode & 0o600 != 0o600:
                raise RuntimeError(
                    "publication cohort marker mode %04o is not safe"
                    % marker_mode
                )
            chunks = []
            remaining = PUBLICATION_BINDING_MAX_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            named = os.stat(
                PUBLICATION_BINDING_NAME,
                dir_fd=self._publish_dir_fd,
                follow_symlinks=False,
            )
            if (
                len(raw) > PUBLICATION_BINDING_MAX_BYTES
                or self._stat_signature(before) != self._stat_signature(after)
                or self._identity(after) != self._identity(named)
                or statmod.S_IMODE(after.st_mode) != statmod.S_IMODE(named.st_mode)
                or after.st_nlink != named.st_nlink
                or after.st_size != named.st_size
                or after.st_gid != named.st_gid
            ):
                raise RuntimeError(
                    "publication cohort marker changed while it was read"
                )
        finally:
            os.close(fd)
        binding = self._parse_publication_binding(raw)
        stored_mode = int(binding["publish_mode"], 8)
        if marker_mode != stored_mode:
            raise RuntimeError(
                "publication cohort marker mode is %04o, stored state is %04o"
                % (marker_mode, stored_mode)
            )
        stored_gid = binding["publish_gid"]
        if stored_gid is not None and after.st_gid != stored_gid:
            raise RuntimeError(
                "publication cohort marker group id is %d, stored state is %d"
                % (after.st_gid, stored_gid)
            )
        return binding

    def _inspect_unbound_published_db(self):
        """Refuse to claim a directory containing an untrusted legacy DB."""
        target = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        try:
            before = os.lstat(target)
        except FileNotFoundError:
            return
        if not statmod.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(
                "unbound publish_dir already contains a non-private index; "
                "move it aside before the first bound build"
            )
        if before.st_size > self.max_index_size_bytes:
            raise RuntimeError(
                "unbound published index exceeds max_index_size_mb; move it "
                "aside or raise the reviewed limit before adoption"
            )
        if statmod.S_IMODE(before.st_mode) != self.publish_mode:
            raise RuntimeError(
                "unbound published index mode is %04o, expected %04o"
                % (statmod.S_IMODE(before.st_mode), self.publish_mode)
            )
        expected_gid = self._expected_publication_gid()
        if expected_gid is not None and before.st_gid != expected_gid:
            raise RuntimeError(
                "unbound published index group id is %d, expected %d"
                % (before.st_gid, expected_gid)
            )
        conn = None
        try:
            conn = sqlite3.connect(sqlite_readonly_uri(target), uri=True)
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("quick_check failed")
            if conn.execute("SELECT COUNT(*) FROM meta").fetchone() != (1,):
                raise ValueError("meta must contain exactly one row")
            row = conn.execute(
                "SELECT schema_version, corpus_uuid, access_policy_id, "
                "access_policy_generation, policy_digest FROM meta"
            ).fetchone()
            desired = self._desired_publication_binding()
            if row != (
                SCHEMA_VERSION,
                desired["corpus_uuid"],
                desired["access_policy_id"],
                desired["access_policy_generation"],
                desired["policy_digest"],
            ):
                raise ValueError("existing v2 identity does not match this build")
        except Exception as exc:
            raise RuntimeError(
                "unbound publish_dir contains an index that cannot be safely "
                "adopted (%s); restore the matching v2 index or move it aside "
                "after verifying the directory ACL" % exc
            )
        finally:
            if conn is not None:
                conn.close()
        current = os.lstat(target)
        if self._stat_signature(current) != self._stat_signature(before):
            raise RuntimeError("published index changed while adopting publish_dir")

    def _inspect_policy_transition_baseline(self, existing):
        """Require the live DB to agree with the durable pre-transition state."""
        target = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        try:
            before = os.lstat(target)
            if not statmod.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("published index is not a private regular file")
            if before.st_size > self.max_index_size_bytes:
                raise ValueError("published index exceeds max_index_size_mb")
            previous_mode = int(existing["publish_mode"], 8)
            if statmod.S_IMODE(before.st_mode) != previous_mode:
                raise ValueError("published index mode differs from the previous marker")
            previous_gid = existing["publish_gid"]
            if previous_gid is not None and before.st_gid != previous_gid:
                raise ValueError("published index group differs from the previous marker")
            conn = sqlite3.connect(sqlite_readonly_uri(target), uri=True)
            try:
                if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("quick_check failed")
                if conn.execute("SELECT COUNT(*) FROM meta").fetchone() != (1,):
                    raise ValueError("meta must contain exactly one row")
                row = conn.execute(
                    "SELECT schema_version, corpus_uuid, access_policy_id, "
                    "access_policy_generation, policy_digest FROM meta"
                ).fetchone()
            finally:
                conn.close()
            expected = (
                SCHEMA_VERSION,
                existing["corpus_uuid"],
                existing["access_policy_id"],
                existing["access_policy_generation"],
                existing["policy_digest"],
            )
            if row != expected:
                raise ValueError("published policy state differs from the marker")
            after = os.lstat(target)
            if self._stat_signature(after) != self._stat_signature(before):
                raise ValueError("published index changed during policy validation")
        except Exception as exc:
            raise RuntimeError(
                "cannot advance access_policy_generation without the valid "
                "previous bound index (%s); restore it or provision a new "
                "publish_dir after reviewing ACLs" % exc
            )

    def _inspect_current_published_permissions(self, existing):
        """Reject permission/type drift while allowing a genuinely missing DB."""
        self._assert_publish_dir_current()
        try:
            current = os.stat(
                PUBLISHED_DB_NAME,
                dir_fd=self._publish_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not statmod.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RuntimeError(
                "published index is no longer a private regular file"
            )
        states = [existing]
        if existing["transition_from"] is not None:
            states.append(existing["transition_from"])
        actual_mode = statmod.S_IMODE(current.st_mode)
        matches_state = any(
            actual_mode == int(state["publish_mode"], 8)
            and (
                state["publish_gid"] is None
                or current.st_gid == state["publish_gid"]
            )
            for state in states
        )
        if not matches_state:
            raise RuntimeError(
                "published index mode/group differs from every bound policy state"
            )

    def _validate_publication_binding(self):
        """Return current/create/advance after validating durable policy state."""
        desired = self._desired_publication_binding()
        existing = self._read_publication_binding(missing_ok=True)
        if existing is not None:
            if (
                existing["corpus_uuid"] != desired["corpus_uuid"]
                or existing["access_policy_id"] != desired["access_policy_id"]
            ):
                raise RuntimeError(
                    "publish_dir is bound to corpus %s / access_policy_id %r, "
                    "not corpus %s / access_policy_id %r"
                    % (
                        existing["corpus_uuid"],
                        existing["access_policy_id"],
                        desired["corpus_uuid"],
                        desired["access_policy_id"],
                    )
                )
            previous_generation = int(existing["access_policy_generation"])
            requested_generation = int(desired["access_policy_generation"])
            if requested_generation < previous_generation:
                raise RuntimeError(
                    "access_policy_generation cannot move backwards from %s to %s"
                    % (
                        existing["access_policy_generation"],
                        desired["access_policy_generation"],
                    )
                )
            if existing["transition_from"] is not None:
                if requested_generation != previous_generation:
                    raise RuntimeError(
                        "finish the pending publication-policy transition at "
                        "generation %s before advancing again"
                        % existing["access_policy_generation"]
                    )
                if (
                    existing["policy_digest"] != desired["policy_digest"]
                    or existing["publish_mode"] != desired["publish_mode"]
                    or existing["publish_gid"] != desired["publish_gid"]
                ):
                    raise RuntimeError(
                        "configuration differs from the pending publication-policy "
                        "transition"
                    )
                self._inspect_current_published_permissions(existing)
                return "transition"
            if requested_generation == previous_generation:
                if existing["policy_digest"] != desired["policy_digest"]:
                    raise RuntimeError(
                        "access policy rules changed without increasing "
                        "access_policy_generation"
                    )
                if (
                    existing["publish_mode"] != desired["publish_mode"]
                    or existing["publish_gid"] != desired["publish_gid"]
                ):
                    raise RuntimeError(
                        "publication permissions changed without increasing "
                        "access_policy_generation"
                    )
                self._inspect_current_published_permissions(existing)
                return "current"
            self._inspect_policy_transition_baseline(existing)
            return "advance"

        # Do not claim an empty directory until verified publication. A matching
        # v2 DB can later be adopted; every other existing artifact is refused.
        self._inspect_unbound_published_db()
        return "create"

    def _replace_publication_binding(self, binding, message):
        """Atomically replace the descriptor-anchored durable binding."""
        payload = (
            json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(payload) > PUBLICATION_BINDING_MAX_BYTES:
            raise RuntimeError("publication cohort marker payload is too large")
        target_gid = self._target_gid()
        expected_gid = binding["publish_gid"]
        temp_fd = None
        temp_name = None
        temp_identity = None
        try:
            for _attempt in range(100):
                candidate = ".aplidocs-publication.%s.tmp" % uuid.uuid4().hex
                try:
                    temp_fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=self._publish_dir_fd,
                    )
                    temp_name = candidate
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise RuntimeError("could not allocate policy marker temporary")
            temp_identity = self._identity(os.fstat(temp_fd))
            offset = 0
            while offset < len(payload):
                offset += os.write(temp_fd, payload[offset:])
            if target_gid is not None:
                os.fchown(temp_fd, -1, target_gid)
            os.fchmod(temp_fd, self.publish_mode)
            os.fsync(temp_fd)
            final = os.fstat(temp_fd)
            named = os.stat(
                temp_name, dir_fd=self._publish_dir_fd, follow_symlinks=False
            )
            if (
                self._identity(final) != self._identity(named)
                or final.st_nlink != 1
                or statmod.S_IMODE(final.st_mode) != self.publish_mode
                or (expected_gid is not None and final.st_gid != expected_gid)
            ):
                raise RuntimeError("policy marker temporary failed validation")
            os.close(temp_fd)
            temp_fd = None
            self._assert_publish_dir_current()
            os.replace(
                temp_name,
                PUBLICATION_BINDING_NAME,
                src_dir_fd=self._publish_dir_fd,
                dst_dir_fd=self._publish_dir_fd,
            )
            temp_name = None
            os.fsync(self._publish_dir_fd)
            if self._read_publication_binding() != binding:
                raise RuntimeError("replacement policy marker verification failed")
            log(message)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name is not None and temp_identity is not None:
                try:
                    named = os.stat(
                        temp_name,
                        dir_fd=self._publish_dir_fd,
                        follow_symlinks=False,
                    )
                    if self._identity(named) == temp_identity:
                        os.unlink(temp_name, dir_fd=self._publish_dir_fd)
                except FileNotFoundError:
                    pass

    def _advance_publication_binding(self, desired):
        """Record a recoverable transition before replacing the live DB."""
        previous = self._read_publication_binding()
        if (
            previous["transition_from"] is not None
            or int(previous["access_policy_generation"])
            >= int(desired["access_policy_generation"])
        ):
            raise RuntimeError("publication policy transition state changed unexpectedly")
        pending = dict(desired)
        pending["transition_from"] = {
            "access_policy_generation": previous["access_policy_generation"],
            "policy_digest": previous["policy_digest"],
            "publish_mode": previous["publish_mode"],
            "publish_gid": previous["publish_gid"],
        }
        self._replace_publication_binding(
            pending,
            "Recorded durable access policy transition to generation %s."
            % desired["access_policy_generation"],
        )

    def _finalize_publication_binding(self):
        """Clear transition state only after the new DB rename is durable."""
        current = self._read_publication_binding()
        if current["transition_from"] is None:
            return
        desired = self._desired_publication_binding()
        for key in (
            "version",
            "corpus_uuid",
            "access_policy_id",
            "access_policy_generation",
            "policy_digest",
            "publish_mode",
            "publish_gid",
        ):
            if current[key] != desired[key]:
                raise RuntimeError(
                    "pending publication-policy transition differs from this build"
                )
        self._replace_publication_binding(
            desired,
            "Finalized durable access policy generation %s."
            % desired["access_policy_generation"],
        )

    def _ensure_publication_binding(self):
        """Create once, or verify, the durable corpus/cohort directory marker."""
        desired = self._desired_publication_binding()
        state = self._validate_publication_binding()
        if state in ("current", "transition"):
            return None
        if state == "advance":
            self._advance_publication_binding(desired)
            return None
        payload = (
            json.dumps(desired, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(payload) > PUBLICATION_BINDING_MAX_BYTES:
            raise RuntimeError("publication cohort marker payload is too large")
        target_gid = self._target_gid()
        expected_gid = desired["publish_gid"]
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = None
        created_identity = None
        try:
            try:
                fd = os.open(
                    PUBLICATION_BINDING_NAME,
                    flags,
                    0o600,
                    dir_fd=self._publish_dir_fd,
                )
            except FileExistsError:
                existing = self._read_publication_binding()
                if existing != desired:
                    raise RuntimeError(
                        "publish_dir was concurrently bound to another corpus/cohort"
                    )
                return None
            created_identity = self._identity(os.fstat(fd))
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            if target_gid is not None:
                os.fchown(fd, -1, target_gid)
            os.fchmod(fd, self.publish_mode)
            os.fsync(fd)
            final = os.fstat(fd)
            named = os.stat(
                PUBLICATION_BINDING_NAME,
                dir_fd=self._publish_dir_fd,
                follow_symlinks=False,
            )
            if (
                self._identity(final) != self._identity(named)
                or final.st_nlink != 1
                or statmod.S_IMODE(final.st_mode) != self.publish_mode
                or (expected_gid is not None and final.st_gid != expected_gid)
            ):
                raise RuntimeError(
                    "publication cohort marker changed while it was created"
                )
            os.close(fd)
            fd = None
            os.fsync(self._publish_dir_fd)
        except BaseException:
            if fd is not None:
                os.close(fd)
            if created_identity is not None:
                try:
                    named = os.stat(
                        PUBLICATION_BINDING_NAME,
                        dir_fd=self._publish_dir_fd,
                        follow_symlinks=False,
                    )
                    if self._identity(named) == created_identity:
                        os.unlink(
                            PUBLICATION_BINDING_NAME,
                            dir_fd=self._publish_dir_fd,
                        )
                        os.fsync(self._publish_dir_fd)
                except OSError:
                    pass
            raise
        try:
            if self._read_publication_binding() != desired:
                raise RuntimeError("publication cohort marker verification failed")
        except BaseException:
            self._rollback_publication_binding(created_identity)
            raise
        return created_identity

    def _rollback_publication_binding(self, created_identity):
        """Remove only the marker this failed publication just created."""
        if created_identity is None:
            return
        try:
            named = os.stat(
                PUBLICATION_BINDING_NAME,
                dir_fd=self._publish_dir_fd,
                follow_symlinks=False,
            )
            if self._identity(named) == created_identity:
                os.unlink(
                    PUBLICATION_BINDING_NAME, dir_fd=self._publish_dir_fd
                )
                os.fsync(self._publish_dir_fd)
        except FileNotFoundError:
            return

    def _open_corpus_root(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.corpus_root, flags)
        except OSError as exc:
            raise RuntimeError("cannot open corpus_root securely: %s" % exc)
        if not statmod.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise RuntimeError("corpus_root is not a directory")
        return fd

    def _assert_corpus_root_current(self, root_fd):
        """Ensure the configured root name still denotes the opened directory."""
        try:
            named = os.stat(self.corpus_root, follow_symlinks=False)
        except OSError as exc:
            raise CorpusMutation("corpus_root disappeared or became inaccessible: %s" % exc)
        opened = os.fstat(root_fd)
        if (
            not statmod.S_ISDIR(named.st_mode)
            or self._identity(named) != self._identity(opened)
        ):
            raise CorpusMutation("corpus_root pathname was replaced during the build")

    def _ensure_private_build_dir(self):
        build_dir = os.path.dirname(self.output_path)
        os.makedirs(build_dir, mode=0o700, exist_ok=True)
        build_stat = os.stat(build_dir, follow_symlinks=False)
        if not statmod.S_ISDIR(build_stat.st_mode):
            raise RuntimeError("scratch parent is not a real directory: %r" % build_dir)
        if (
            self._publish_identity is not None
            and self._identity(build_stat) == self._publish_identity
        ):
            raise RuntimeError(
                "scratch parent resolves to publish_dir by filesystem identity"
            )
        if (
            self._corpus_identity is not None
            and self._identity(build_stat) == self._corpus_identity
        ):
            raise RuntimeError(
                "scratch parent resolves to corpus_root by filesystem identity"
            )
        if os.name == "posix":
            mode = statmod.S_IMODE(build_stat.st_mode)
            if mode & 0o077:
                raise RuntimeError(
                    "scratch directory must be private (0700); current mode is %04o: %r"
                    % (mode, build_dir)
                )
        self._scratch_identity = self._identity(build_stat)
        return build_dir

    def _acquire_scratch_lock(self):
        if self._scratch_lock_fd is not None:
            return
        build_dir = self._ensure_private_build_dir()
        lock_name = ".%s.lock" % os.path.basename(self.output_path)
        lock_path = os.path.join(build_dir, lock_name)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("cannot open scratch lock %r: %s" % (lock_path, exc))
        try:
            if not statmod.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("scratch lock is not a regular file: %r" % lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError("another build is using scratch output %r" % self.output_path)
                raise RuntimeError("cannot lock scratch output %r: %s" % (self.output_path, exc))
        except BaseException:
            os.close(fd)
            raise
        self._scratch_lock_fd = fd

    def _read_corpus_marker(self, root_fd):
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.corpus_id_file, flags, dir_fd=root_fd)
        except FileNotFoundError:
            raise RuntimeError(
                "corpus marker %r is missing; run with --init-corpus-id once while the intended share is mounted"
                % self.corpus_id_file
            )
        except OSError as exc:
            raise RuntimeError("cannot open corpus marker %r: %s" % (self.corpus_id_file, exc))
        try:
            st = os.fstat(fd)
            if not statmod.S_ISREG(st.st_mode) or st.st_size > 128:
                raise RuntimeError("corpus marker must be a regular ASCII UUID file <= 128 bytes")
            chunks = []
            remaining = 129
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > 128:
                raise RuntimeError("corpus marker is too large")
            try:
                value = str(uuid.UUID(raw.decode("ascii").strip()))
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("corpus marker is not a valid UUID: %s" % exc)
            return value, self._stat_signature(st)
        finally:
            os.close(fd)

    def initialize_corpus_id(self):
        self._require_posix_features()
        root_fd = self._open_corpus_root()
        try:
            value = str(uuid.uuid4())
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(self.corpus_id_file, flags, 0o600, dir_fd=root_fd)
            except FileExistsError:
                existing, _ = self._read_corpus_marker(root_fd)
                log("Corpus marker already exists: %s" % existing)
                return existing
            try:
                payload = (value + "\n").encode("ascii")
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
            log("Created corpus marker %r: %s" % (self.corpus_id_file, value))
            return value
        finally:
            os.close(root_fd)

    # -- previous DB (strong, cohort-bound extraction cache) --

    def _discard_previous(self):
        if self.prev_conn is not None:
            try:
                self.prev_conn.close()
            except Exception:
                pass
        self.prev_conn = None
        self.prev_meta = {}
        self.previous_file_count = None
        self.previous_policy_digest = None

    def load_previous(self):
        self._discard_previous()
        if self._cache_disabled:
            log("Previous cache disabled for this run; full extraction.")
            return
        published = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        if self._publish_dir_fd is not None:
            self._assert_publish_dir_current()
        try:
            published_stat = os.lstat(published)
        except FileNotFoundError:
            log("No previous published DB at %r; full extraction." % published)
            return
        if not statmod.S_ISREG(published_stat.st_mode):
            log("WARNING: previous DB is not a regular file; full extraction.")
            return
        if published_stat.st_size > self.max_index_size_bytes:
            log(
                "WARNING: previous DB exceeds max_index_size_mb; full extraction."
            )
            return
        self._published_db_identity = self._identity(published_stat)
        try:
            self.prev_conn = sqlite3.connect(sqlite_readonly_uri(published), uri=True)
            if self._publish_dir_fd is not None:
                self._assert_publish_dir_current()
            current_published_stat = os.lstat(published)
            if self._identity(current_published_stat) != self._identity(published_stat):
                raise CacheInvalid("previous DB was replaced while opening it")
            # This hint is not trusted for integrity; it is only an early
            # resource guard before quick_check.  A small/malformed value still
            # reaches the verified COUNT(files) check below.
            early_meta = self.prev_conn.execute(
                "SELECT file_count FROM meta LIMIT 2"
            ).fetchall()
            if (
                len(early_meta) == 1
                and type(early_meta[0][0]) is int
                and early_meta[0][0] > self.max_total_files
            ):
                raise CacheInvalid(
                    "previous index declares %d files, above max_total_files=%d"
                    % (early_meta[0][0], self.max_total_files)
                )
            quick = self.prev_conn.execute("PRAGMA quick_check").fetchall()
            if quick != [("ok",)]:
                raise CacheInvalid("quick_check failed: %r" % (quick[:3],))
            foreign_errors = self.prev_conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_errors:
                raise CacheInvalid("foreign_key_check failed: %r" % (foreign_errors[:3],))
            if self.prev_conn.execute("SELECT COUNT(*) FROM meta").fetchone() != (1,):
                raise CacheInvalid("meta table must contain exactly one row")
            row = self.prev_conn.execute(
                "SELECT schema_version, tool_version, corpus_uuid, access_policy_id, "
                "access_policy_generation, policy_digest, file_count FROM meta"
            ).fetchone()
            if row is None:
                raise CacheInvalid("meta table is empty")
            schema, tool, corpus_uuid, policy_id, generation, digest, file_count = row
            if schema != SCHEMA_VERSION or tool != TOOL_VERSION:
                raise CacheInvalid("schema/tool version differs")
            if corpus_uuid != self.corpus_uuid:
                raise RuntimeError(
                    "published index belongs to corpus UUID %s, mounted corpus is %s; "
                    "use the original share or a different publish_dir"
                    % (corpus_uuid, self.corpus_uuid)
                )
            if policy_id != self.access_policy_id:
                raise RuntimeError(
                    "publish_dir belongs to access_policy_id %r, not %r; "
                    "use the cohort's own publication directory"
                    % (policy_id, self.access_policy_id)
                )
            if generation == self.access_policy_generation and digest != self.policy_digest:
                raise RuntimeError(
                    "access policy rules changed without changing "
                    "access_policy_generation; bump the generation to acknowledge "
                    "the authorization change"
                )
            if generation != self.access_policy_generation:
                log("Access policy generation changed; previous text cache is invalidated.")
                self._discard_previous()
                return
            required_tables = {"meta", "files", "content", "files_fts", "content_fts"}
            actual_tables = {
                item[0] for item in self.prev_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if not required_tables.issubset(actual_tables):
                raise CacheInvalid("previous DB is missing required tables")
            actual_file_count = self.prev_conn.execute(
                "SELECT COUNT(*) FROM files"
            ).fetchone()[0]
            if actual_file_count != int(file_count):
                raise CacheInvalid(
                    "previous meta file_count %d differs from files table %d"
                    % (int(file_count), actual_file_count)
                )
            if actual_file_count > self.max_total_files:
                raise CacheInvalid(
                    "previous index has %d files, above max_total_files=%d"
                    % (actual_file_count, self.max_total_files)
                )
            for relpath, source_hash, status, content_present in self.prev_conn.execute(
                "SELECT relpath, source_hash, content_status, content_present FROM files"
            ):
                self.prev_meta[relpath] = (source_hash, status, bool(content_present))
            self.previous_file_count = int(file_count)
            self.previous_policy_digest = digest
            log("Loaded previous DB cache: %d files from %r" % (len(self.prev_meta), published))
        except RuntimeError:
            self._discard_previous()
            raise
        except Exception as exc:
            log("WARNING: previous DB unusable (%s); full extraction." % exc)
            self._discard_previous()

    def cached_extraction(self, relpath, source_sig):
        cached = self.prev_meta.get(relpath)
        if cached is None or self.prev_conn is None:
            return None
        prev_sig, prev_status, content_present = cached
        if not source_sig or prev_sig != source_sig or prev_status not in CACHEABLE_STATUSES:
            return None
        try:
            if prev_status == ST_EXTRACTED:
                row = self.prev_conn.execute(
                    "SELECT text, extractor, source_sig FROM content WHERE relpath = ?",
                    (relpath,),
                ).fetchone()
                if row is None:
                    if content_present:
                        raise CacheInvalid("previous content row is missing for %r" % relpath)
                    return ST_EXTRACTED, None, None
                if not content_present:
                    raise CacheInvalid("unexpected previous content row for %r" % relpath)
                if row[2] != source_sig:
                    raise CacheInvalid(
                        "previous content source signature differs for %r" % relpath
                    )
                return ST_EXTRACTED, row[0], row[1]
            return prev_status, None, None
        except sqlite3.DatabaseError as exc:
            raise CacheInvalid("previous content cache failed during reuse: %s" % exc)

    # -- schema and extraction --

    def create_schema(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA synchronous = OFF;
            PRAGMA journal_mode = MEMORY;
            PRAGMA foreign_keys = ON;

            CREATE TABLE meta (
                schema_version INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL,
                file_count INTEGER NOT NULL,
                content_count INTEGER NOT NULL,
                content_status_breakdown TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                corpus_root TEXT NOT NULL,
                corpus_uuid TEXT NOT NULL,
                access_policy_id TEXT NOT NULL,
                access_policy_generation TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                access_roots TEXT NOT NULL,
                excluded_access_count INTEGER NOT NULL
            );

            CREATE TABLE files (
                relpath TEXT PRIMARY KEY,
                relpath_nfc TEXT NOT NULL,
                filename TEXT NOT NULL,
                filename_nfc TEXT NOT NULL,
                area TEXT NOT NULL,
                ext TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                st_dev TEXT NOT NULL,
                st_ino TEXT NOT NULL,
                source_hash TEXT,
                path_year INTEGER,
                name_meta TEXT,
                metadata_only INTEGER NOT NULL,
                content_present INTEGER NOT NULL,
                content_status TEXT NOT NULL
            );

            CREATE TABLE content (
                relpath TEXT PRIMARY KEY REFERENCES files(relpath),
                text TEXT NOT NULL,
                extractor TEXT NOT NULL,
                source_sig TEXT NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE VIRTUAL TABLE files_fts USING fts5("
            "filename_nfc, relpath_nfc, content='files', content_rowid='rowid', "
            "tokenize='%s')" % FTS_TOKENIZE
        )
        cur.execute(
            "CREATE VIRTUAL TABLE content_fts USING fts5("
            "text, content='content', content_rowid='rowid', tokenize='%s')" % FTS_TOKENIZE
        )
        self.conn.commit()

    def is_metadata_only(self, relpath):
        normalized = unicodedata.normalize("NFC", relpath)
        exact = any(rx.match(normalized) for rx in self.metadata_only_res)
        if not exact and any(
            rx.match(normalized.casefold()) for rx in self.metadata_only_casefold_res
        ):
            raise RuntimeError(
                "path %r differs only by case from a metadata_only_globs match; "
                "use exact on-disk spelling" % relpath
            )
        return exact

    @staticmethod
    def _source_limit(ext):
        if ext in TEXT_EXT or ext in HTML_EXT:
            return TEXT_SOURCE_CAP_BYTES
        if ext in WORD_EXT or ext in EXCEL_EXT or ext in ODT_EXT:
            return ARCHIVE_SOURCE_CAP_BYTES
        if ext in PDF_EXT:
            return PDF_SOURCE_CAP_BYTES
        return None

    def _ensure_pdf_backend(self):
        if self._pdf_checked:
            return
        if shutil.which("pdftotext") is None:
            raise DependencyUnavailable("pdftotext executable is not available on PATH")
        build_dir = os.path.dirname(self.output_path)
        probe_fd, probe_path = tempfile.mkstemp(
            prefix=".pdftotext-probe-", suffix=".pdf", dir=build_dir
        )
        try:
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(probe_fd, 0o600)
                payload = _minimal_pdf_probe_bytes()
                offset = 0
                while offset < len(payload):
                    offset += os.write(probe_fd, payload[offset:])
                os.fsync(probe_fd)
            finally:
                os.close(probe_fd)
            try:
                probe_text = _extract_pdf(probe_path, self.pdf_memory_limit_bytes)
            except (ContentTooBig, ExtractionFailed, DependencyUnavailable) as exc:
                raise DependencyUnavailable(
                    "pdftotext failed its functional PDF conversion probe: %s" % exc
                )
            if "APLIDOCSPROBE" not in probe_text:
                raise DependencyUnavailable(
                    "pdftotext functional probe returned no recognizable text"
                )
        finally:
            try:
                os.unlink(probe_path)
            except FileNotFoundError:
                pass
        self._pdf_checked = True

    def _ensure_archive_backend(self):
        if self._archive_checked:
            return
        ensure_zip_deflate_backend()
        self._archive_checked = True

    def classify(self, fullpath, relpath, ext, size_bytes, mtime, source_sig=None):
        """Classify a stable path; direct-call compatibility for extractor tests."""
        if self.is_metadata_only(relpath):
            return ST_METADATA_ONLY, None, None
        if ext in UNSUPPORTED_EXT:
            return ST_UNSUPPORTED, None, None
        if ext not in PDF_EXT | WORD_EXT | EXCEL_EXT | ODT_EXT | TEXT_EXT | HTML_EXT:
            return ST_NO_TEXT, None, None
        source_limit = self._source_limit(ext)
        if source_limit is not None and size_bytes > source_limit:
            return ST_TOO_BIG, None, None
        if ext in WORD_EXT | EXCEL_EXT | ODT_EXT:
            self._ensure_archive_backend()
        cached = self.cached_extraction(relpath, source_sig)
        if cached is not None:
            self.cache_hits += 1
            return cached
        return self._classify_uncached(fullpath, relpath, ext)

    def _classify_uncached(self, fullpath, relpath, ext):
        extractor = extractor_for_extension(ext)
        functions = {
            "pdftotext": lambda path: _extract_pdf(
                path, self.pdf_memory_limit_bytes
            ),
            "ooxml-word": _extract_word,
            "ooxml-excel": _extract_excel,
            "odt": _extract_odt,
            "text": _read_text_file,
            "html": _extract_html,
        }
        func = functions.get(extractor)
        if func is None:
            return ST_NO_TEXT, None, None
        try:
            raw = func(fullpath)
        except ContentTooBig:
            return ST_TOO_BIG, None, extractor
        except DependencyUnavailable:
            raise
        except FileNotFoundError as exc:
            if ext in PDF_EXT:
                raise DependencyUnavailable("pdftotext executable is unavailable: %s" % exc)
            raise
        except ExtractionFailed as exc:
            log("WARNING: extraction failed for %r: %s" % (relpath, exc))
            return ST_ERROR, None, extractor
        except OSError:
            # Filesystem errors are not document parse errors and must never be
            # disguised as a publishable per-file status.
            raise
        text = _collapse_whitespace(raw)
        if len(text.encode("utf-8")) > TEXT_CAP_BYTES:
            return ST_TOO_BIG, None, extractor
        if ext in PDF_EXT and not text:
            return ST_EMPTY_PDF, None, extractor
        return ST_EXTRACTED, (text if text else None), extractor

    def _open_snapshot_path(self, root_fd, relpath, directory):
        """Open one exact manifest path beneath the descriptor-anchored root."""
        parts = pathlib.PurePosixPath(relpath).parts
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current,
                )
                os.close(current)
                current = next_fd
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            if directory:
                flags |= os.O_DIRECTORY
            else:
                flags |= os.O_NONBLOCK
            opened = os.open(parts[-1], flags, dir_fd=current)
            return opened
        finally:
            os.close(current)

    def _validate_snapshot(self, root_fd):
        """Reopen and revalidate the completed authorized manifest."""
        def validate_routes():
            # Opening a saved path is insufficient on case-insensitive SMB/CIFS:
            # a spelling-only rename can keep resolving.  Re-enumerate every
            # routing parent and require the configured spelling immediately
            # before and after the potentially lengthy manifest rehash.
            ordered_routes = sorted(
                self._snapshot_routes.items(),
                key=lambda item: (item[0].count("/"), os.fsencode(item[0])),
            )
            for rel_dir, names in ordered_routes:
                route_fd = root_fd
                close_route_fd = False
                if rel_dir:
                    try:
                        route_fd = self._open_snapshot_path(
                            root_fd, rel_dir, directory=True
                        )
                    except OSError as exc:
                        raise CorpusMutation(
                            "routing directory changed before final validation %r: %s"
                            % (rel_dir, exc)
                        )
                    close_route_fd = True
                try:
                    self._verify_routing_names_exact(route_fd, names, rel_dir)
                except OSError as exc:
                    if self._mutation_errno(exc):
                        raise CorpusMutation(
                            "routing directory changed during final spelling check: %r"
                            % rel_dir
                        )
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        raise RuntimeError(
                            "cannot validate authorized route %r: permission denied"
                            % (rel_dir or ".")
                        )
                    raise RuntimeError(
                        "cannot validate authorized route %r: %s"
                        % (rel_dir or ".", exc)
                    )
                finally:
                    if close_route_fd:
                        os.close(route_fd)

        def validate_directories():
            for relpath, expected in sorted(self._snapshot_dirs.items()):
                try:
                    fd = self._open_snapshot_path(root_fd, relpath, directory=True)
                except OSError as exc:
                    raise CorpusMutation(
                        "authorized directory changed before final validation %r: %s"
                        % (relpath, exc)
                    )
                try:
                    current = os.fstat(fd)
                    if self._stat_signature(current) != expected:
                        raise CorpusMutation(
                            "authorized directory changed after its walk: %r" % relpath
                        )
                finally:
                    os.close(fd)

        validate_routes()
        validate_directories()

        for relpath, (expected_signature, expected_hash) in sorted(
            self._snapshot_files.items()
        ):
            try:
                fd = self._open_snapshot_path(root_fd, relpath, directory=False)
            except OSError as exc:
                raise CorpusMutation(
                    "authorized file changed before final validation %r: %s"
                    % (relpath, exc)
                )
            try:
                current = os.fstat(fd)
                if (
                    not statmod.S_ISREG(current.st_mode)
                    or self._stat_signature(current) != expected_signature
                ):
                    raise CorpusMutation(
                        "authorized file changed after it was indexed: %r" % relpath
                    )
                if expected_hash is not None:
                    actual_hash = self._hash_fd(fd, int(current.st_size))
                    if actual_hash != expected_hash:
                        raise CorpusMutation(
                            "authorized file content changed after it was indexed: %r"
                            % relpath
                        )
                    after_hash = os.fstat(fd)
                    if self._stat_signature(after_hash) != expected_signature:
                        raise CorpusMutation(
                            "authorized file changed during final validation: %r"
                            % relpath
                        )
            finally:
                os.close(fd)
        # File rehashing may take time.  Recheck directory membership metadata
        # afterwards so additions/removals made during that pass also retry.
        validate_directories()
        validate_routes()

    # -- secure descriptor walk --

    def _dir_relevant(self, relpath_nfc):
        if "." in self.access_roots:
            return True
        return any(
            relpath_nfc == root
            or relpath_nfc.startswith(root + "/")
            or root.startswith(relpath_nfc + "/")
            for root in self.access_roots
        )

    def _file_authorized(self, relpath_nfc):
        if "." in self.access_roots:
            return True
        return any(relpath_nfc == root or relpath_nfc.startswith(root + "/") for root in self.access_roots)

    def _routing_child_names(self, rel_dir):
        """Return exact next components before an access root, else ``None``.

        Routing ancestors are not authorized directory contents.  Only the
        configured next components are statted/opened; a bounded streaming
        readdir separately verifies their exact spelling on case-insensitive
        mounts without retaining or statting sibling names.
        """
        if self._file_authorized(rel_dir) or "." in self.access_roots:
            return None
        prefix = rel_dir + "/" if rel_dir else ""
        children = set()
        for root in self.access_roots:
            if root.startswith(prefix):
                remainder = root[len(prefix) :]
                if remainder:
                    children.add(remainder.split("/", 1)[0])
        return sorted(children, key=os.fsencode)

    def _verify_routing_names_exact(self, dir_fd, names, rel_dir):
        """Fail closed when a case-folding filesystem aliases a route name."""
        wanted = set(names)
        missing = set(names)
        folded = {}
        for name in names:
            key = unicodedata.normalize("NFC", name).casefold()
            folded.setdefault(key, set()).add(name)
        mismatched = set()
        scanned = 0
        with os.scandir(dir_fd) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > self.max_directory_entries:
                    raise RuntimeError(
                        "routing directory %r exceeds max_directory_entries=%d "
                        "during exact access_root verification"
                        % (rel_dir or ".", self.max_directory_entries)
                    )
                actual = entry.name
                if actual in wanted:
                    missing.discard(actual)
                    continue
                if not _has_unicode_surrogate(actual):
                    actual_key = unicodedata.normalize("NFC", actual).casefold()
                    mismatched.update(folded.get(actual_key, ()))
        if mismatched:
            raise RuntimeError(
                "configured access_root route differs in case/spelling on disk at %r; "
                "use the exact name returned by the filesystem"
                % (rel_dir or ".")
            )
        if missing:
            raise CorpusMutation(
                "configured access_root route is missing with exact spelling below %r"
                % (rel_dir or ".")
            )
        return scanned

    def _check_prune_name(self, exact_name, normalized_name):
        configured = self.prune_casefold.get(normalized_name.casefold())
        if configured is not None and normalized_name != configured:
            raise RuntimeError(
                "directory %r differs only by case from prune_dirs entry %r; "
                "use the exact on-disk spelling" % (exact_name, configured)
            )
        return normalized_name in self.prune_segments

    @staticmethod
    def _mutation_errno(exc):
        return exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, getattr(errno, "ESTALE", -1))

    def _hash_fd(self, fd, expected_size):
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = expected_size
        while remaining:
            chunk = os.read(fd, min(HASH_CHUNK_BYTES, remaining))
            if not chunk:
                raise CorpusMutation("file was truncated while hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise CorpusMutation("file grew while hashing")
        return digest.hexdigest()

    def _private_source_copy(self, fd, ext, expected_size):
        build_dir = os.path.dirname(self.output_path)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".source-", suffix=("." + ext if ext else ".bin"), dir=build_dir
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(temp_fd, 0o600)
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = expected_size
            while remaining:
                chunk = os.read(fd, min(HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise CorpusMutation("file was truncated while copying")
                offset = 0
                while offset < len(chunk):
                    offset += os.write(temp_fd, chunk[offset:])
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise CorpusMutation("file grew while copying")
            os.fsync(temp_fd)
        except BaseException:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(temp_fd)
        return temp_path

    def _process_open_file(self, parent_fd, name, relpath, opened_fd, before):
        relpath_nfc = unicodedata.normalize("NFC", relpath)
        filename_nfc = unicodedata.normalize("NFC", name)
        ext = file_extension(filename_nfc)
        size_bytes = int(before.st_size)
        mtime_ns = int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1000000000)))
        ctime_ns = int(getattr(before, "st_ctime_ns", int(before.st_ctime * 1000000000)))
        source_hash = None
        temp_path = None
        try:
            if self.is_metadata_only(relpath_nfc):
                status, text, extractor = ST_METADATA_ONLY, None, None
            elif ext in UNSUPPORTED_EXT:
                status, text, extractor = ST_UNSUPPORTED, None, None
            elif ext not in PDF_EXT | WORD_EXT | EXCEL_EXT | ODT_EXT | TEXT_EXT | HTML_EXT:
                status, text, extractor = ST_NO_TEXT, None, None
            elif size_bytes > self._source_limit(ext):
                status, text, extractor = ST_TOO_BIG, None, None
            else:
                if ext in PDF_EXT:
                    self._ensure_pdf_backend()
                elif ext in WORD_EXT | EXCEL_EXT | ODT_EXT:
                    self._ensure_archive_backend()
                # Authorization has already been re-evaluated by successfully
                # opening this descriptor.  Only now may old text be consulted.
                source_hash = self._hash_fd(opened_fd, size_bytes)
                cached = self.cached_extraction(relpath, source_hash)
                if cached is not None:
                    self.cache_hits += 1
                    status, text, extractor = cached
                else:
                    temp_path = self._private_source_copy(opened_fd, ext, size_bytes)
                    status, text, extractor = self._classify_uncached(temp_path, relpath, ext)
            after = os.fstat(opened_fd)
            if self._stat_signature(before) != self._stat_signature(after):
                raise CorpusMutation("file changed while read: %r" % relpath)
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                if self._mutation_errno(exc):
                    raise CorpusMutation("file disappeared or changed type: %r" % relpath)
                raise
            if self._identity(named) != self._identity(before) or not statmod.S_ISREG(named.st_mode):
                raise CorpusMutation("file name was replaced while read: %r" % relpath)

            self._charge_extracted_text(text)

            area = derive_area(relpath_nfc)
            path_year = derive_path_year(relpath_nfc)
            name_meta = derive_name_meta(filename_nfc, self.filename_pattern)
            self.conn.execute(
                "INSERT INTO files(relpath, relpath_nfc, filename, filename_nfc, area, ext, "
                "size_bytes, mtime, mtime_ns, ctime_ns, st_dev, st_ino, source_hash, "
                "path_year, name_meta, metadata_only, content_present, content_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    relpath, relpath_nfc, name, filename_nfc, area, ext,
                    size_bytes, int(before.st_mtime), mtime_ns, ctime_ns,
                    str(int(before.st_dev)), str(int(before.st_ino)), source_hash, path_year,
                    name_meta, 1 if status == ST_METADATA_ONLY else 0, 1 if text else 0, status,
                ),
            )
            if text:
                self.conn.execute(
                    "INSERT INTO content(relpath, text, extractor, source_sig) VALUES(?,?,?,?)",
                    (relpath, text, extractor, source_hash),
                )
            self.status_counts[status] += 1
            self.walk_count += 1
            if self.walk_count % 1000 == 0:
                log("  ... %d files walked (cache hits %d)" % (self.walk_count, self.cache_hits))
            return source_hash
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _charge_extracted_text(self, text):
        if not text:
            return
        size = len(text.encode("utf-8"))
        if self.total_extracted_bytes + size > self.max_total_extracted_bytes:
            raise RuntimeError(
                "extracted text exceeds max_total_extracted_mb aggregate budget"
            )
        self.total_extracted_bytes += size

    def _walk_dir(
        self, dir_fd, rel_dir, parent_fd=None, parent_name=None,
        opened_stat=None, depth=0,
    ):
        if depth > self.max_directory_depth:
            raise RuntimeError(
                "authorized directory depth exceeds max_directory_depth=%d at %r"
                % (self.max_directory_depth, rel_dir)
            )
        try:
            routing_names = self._routing_child_names(rel_dir)
            if routing_names is not None:
                entries = routing_names
                visited_now = self._verify_routing_names_exact(
                    dir_fd, entries, rel_dir
                )
                self._snapshot_routes[rel_dir] = tuple(entries)
            else:
                with os.scandir(dir_fd) as iterator:
                    entries = []
                    for entry in iterator:
                        if _has_unicode_surrogate(entry.name):
                            raise RuntimeError(
                                "authorized filesystem entry has non-UTF-8 bytes "
                                "(hex %s); rename it before indexing"
                                % os.fsencode(entry.name).hex()
                            )
                        candidate = (
                            entry.name if not rel_dir else rel_dir + "/" + entry.name
                        )
                        if not self._dir_relevant(candidate):
                            continue
                        if len(entries) >= self.max_directory_entries:
                            raise RuntimeError(
                                "authorized directory %r exceeds max_directory_entries=%d"
                                % (rel_dir or ".", self.max_directory_entries)
                            )
                        entries.append(entry.name)
                    entries.sort(key=os.fsencode)
                visited_now = len(entries)
            self._visited_entry_count += visited_now
            if self._visited_entry_count > self.max_total_entries:
                raise RuntimeError(
                    "walk exceeds max_total_entries=%d"
                    % self.max_total_entries
                )
        except OSError as exc:
            if self._mutation_errno(exc):
                raise CorpusMutation("directory changed during enumeration: %r" % rel_dir)
            if exc.errno in (errno.EACCES, errno.EPERM):
                self.excluded_access_count += 1
                raise RuntimeError(
                    "cannot enumerate authorized directory %r: permission denied" % rel_dir
                )
            raise RuntimeError("cannot enumerate authorized directory %r: %s" % (rel_dir, exc))

        for name in entries:
            if rel_dir == "" and name == self.corpus_id_file:
                continue
            if name in SKIP_FILES:
                continue
            relpath = name if not rel_dir else rel_dir + "/" + name
            normalized_name = unicodedata.normalize("NFC", name)
            relpath_nfc = unicodedata.normalize("NFC", relpath)
            if not self._dir_relevant(relpath):
                # Do not even stat names outside this cohort's roots.  Besides
                # reducing work, this keeps unrelated ACL/I/O failures from
                # affecting or disclosing anything through the cohort build.
                continue
            configured_root = relpath in self.access_roots
            try:
                listed = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                if routing_names is not None and exc.errno in (
                    errno.ENOENT, errno.ENOTDIR
                ):
                    raise CorpusMutation(
                        "configured access_root route is missing at %r" % relpath
                    )
                if self._mutation_errno(exc):
                    raise CorpusMutation("entry changed after enumeration: %r" % relpath)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    self.excluded_access_count += 1
                    raise RuntimeError(
                        "cannot stat authorized entry %r: permission denied" % relpath
                    )
                raise RuntimeError("cannot stat authorized entry %r: %s" % (relpath, exc))

            if statmod.S_ISLNK(listed.st_mode):
                if configured_root:
                    raise RuntimeError("configured access_root is a symlink: %r" % relpath)
                continue
            if statmod.S_ISDIR(listed.st_mode):
                if self._check_prune_name(name, normalized_name):
                    if configured_root:
                        raise RuntimeError("configured access_root is also pruned: %r" % relpath)
                    continue
                if configured_root:
                    self._seen_access_roots.add(relpath)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                try:
                    child_fd = os.open(name, flags, dir_fd=dir_fd)
                except OSError as exc:
                    if self._mutation_errno(exc):
                        raise CorpusMutation("directory changed before open: %r" % relpath)
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        self.excluded_access_count += 1
                        raise RuntimeError(
                            "cannot open authorized directory %r: permission denied" % relpath
                        )
                    raise RuntimeError("cannot open authorized directory %r: %s" % (relpath, exc))
                try:
                    child_stat = os.fstat(child_fd)
                    if not statmod.S_ISDIR(child_stat.st_mode) or self._identity(child_stat) != self._identity(listed):
                        raise CorpusMutation("directory was replaced before open: %r" % relpath)
                    if (
                        self._scratch_identity is not None
                        and self._identity(child_stat) == self._scratch_identity
                    ):
                        raise RuntimeError(
                            "scratch storage is reachable inside corpus_root by "
                            "filesystem identity: %r" % relpath
                        )
                    if (
                        self._publish_identity is not None
                        and self._identity(child_stat) == self._publish_identity
                    ):
                        if configured_root:
                            raise RuntimeError(
                                "configured access_root resolves to publish/scratch storage: %r"
                                % relpath
                            )
                        continue
                    self._walk_dir(
                        child_fd, relpath, dir_fd, name, child_stat, depth + 1
                    )
                    post = os.fstat(child_fd)
                    try:
                        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    except OSError as exc:
                        if self._mutation_errno(exc):
                            raise CorpusMutation("directory disappeared during walk: %r" % relpath)
                        raise
                    if (
                        self._identity(named) != self._identity(child_stat)
                        or (
                            self._file_authorized(relpath)
                            and self._stat_signature(post)
                            != self._stat_signature(child_stat)
                        )
                    ):
                        raise CorpusMutation("directory was replaced during walk: %r" % relpath)
                    if self._file_authorized(relpath):
                        self._snapshot_dirs[relpath] = self._stat_signature(post)
                finally:
                    os.close(child_fd)
                continue
            if not statmod.S_ISREG(listed.st_mode):
                if configured_root:
                    raise RuntimeError("configured access_root is not a regular file/directory: %r" % relpath)
                continue
            if configured_root:
                self._seen_access_roots.add(relpath)
            if not self._file_authorized(relpath):
                continue
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                file_fd = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                if self._mutation_errno(exc):
                    raise CorpusMutation("file changed before open: %r" % relpath)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    self.excluded_access_count += 1
                    raise RuntimeError(
                        "cannot open authorized file %r: permission denied" % relpath
                    )
                raise RuntimeError("cannot open authorized file %r: %s" % (relpath, exc))
            try:
                opened = os.fstat(file_fd)
                if not statmod.S_ISREG(opened.st_mode) or self._identity(opened) != self._identity(listed):
                    raise CorpusMutation("file was replaced before open: %r" % relpath)
                if self._identity(opened) in (
                    self._scratch_output_identity,
                    self._published_db_identity,
                ):
                    if configured_root:
                        raise RuntimeError(
                            "configured access_root resolves to an index artifact: %r"
                            % relpath
                        )
                    continue
                if self.walk_count >= self.max_total_files:
                    raise RuntimeError(
                        "walk exceeds max_total_files=%d" % self.max_total_files
                    )
                source_hash = self._process_open_file(
                    dir_fd, name, relpath, file_fd, opened
                )
                self._snapshot_files[relpath] = (
                    self._stat_signature(opened), source_hash
                )
            finally:
                os.close(file_fd)

        if routing_names is not None:
            # A case-insensitive share can rename only the spelling while the
            # old lookup spelling keeps resolving.  Re-read after processing
            # every configured child so that race cannot enter the manifest.
            try:
                visited_final = self._verify_routing_names_exact(
                    dir_fd, routing_names, rel_dir
                )
            except OSError as exc:
                if self._mutation_errno(exc):
                    raise CorpusMutation(
                        "routing directory changed during final spelling check: %r"
                        % rel_dir
                    )
                if exc.errno in (errno.EACCES, errno.EPERM):
                    self.excluded_access_count += 1
                    raise RuntimeError(
                        "cannot recheck authorized route %r: permission denied"
                        % rel_dir
                    )
                raise RuntimeError(
                    "cannot recheck authorized route %r: %s" % (rel_dir, exc)
                )
            self._visited_entry_count += visited_final
            if self._visited_entry_count > self.max_total_entries:
                raise RuntimeError(
                    "walk exceeds max_total_entries=%d"
                    % self.max_total_entries
                )

    def _prepare_private_output(self):
        self._ensure_private_build_dir()
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.output_path + suffix
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.output_path, flags, 0o600)
        self._scratch_output_identity = self._identity(os.fstat(fd))
        os.close(fd)
        self.conn = sqlite3.connect(self.output_path)
        os.chmod(self.output_path, 0o600)
        self._apply_index_page_budget(self.conn)

    def _apply_index_page_budget(self, conn):
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(1, self.max_index_size_bytes // page_size)
        applied = int(
            conn.execute("PRAGMA max_page_count=%d" % max_pages).fetchone()[0]
        )
        if applied > max_pages:
            raise RuntimeError("SQLite did not apply max_index_size_mb page budget")

    def _reset_attempt(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.walk_count = 0
        self._visited_entry_count = 0
        self.status_counts = collections.Counter()
        self.cache_hits = 0
        self.excluded_access_count = 0
        self.total_extracted_bytes = 0
        self._seen_access_roots = {"."} if "." in self.access_roots else set()
        self._pdf_checked = False
        self._archive_checked = False
        self._snapshot_dirs = {}
        self._snapshot_files = {}
        self._snapshot_routes = {}
        self._prepare_private_output()
        self.create_schema()

    def build(self):
        if self._lock_fd is None:
            self.acquire_publish_lock()
        ensure_fts5_provider()
        start = time.time()
        log("Build start. corpus_root=%r output=%r" % (self.corpus_root, self.output_path))
        self._acquire_scratch_lock()
        root_fd = self._open_corpus_root()
        try:
            self.corpus_uuid, self._corpus_marker_sig = self._read_corpus_marker(root_fd)
        finally:
            os.close(root_fd)
        self._validate_publication_binding()
        self.load_previous()

        mutation_attempt = 0
        cache_restart_used = False
        while True:
            self._reset_attempt()
            root_fd = self._open_corpus_root()
            self._building = True
            try:
                marker, marker_sig = self._read_corpus_marker(root_fd)
                if marker != self.corpus_uuid or marker_sig != self._corpus_marker_sig:
                    raise CorpusMutation("corpus marker changed before the walk")
                self._assert_corpus_root_current(root_fd)
                root_before = os.fstat(root_fd)
                self._walk_dir(root_fd, "")
                root_after = os.fstat(root_fd)
                self._assert_corpus_root_current(root_fd)
                if (
                    "." in self.access_roots
                    and self._stat_signature(root_before) != self._stat_signature(root_after)
                ):
                    raise CorpusMutation("corpus root changed during the walk")
                missing_roots = sorted(set(self.access_roots) - self._seen_access_roots)
                if missing_roots:
                    raise RuntimeError("configured access_roots were not found: %s" % ", ".join(missing_roots))
                self._validate_snapshot(root_fd)
                root_final = os.fstat(root_fd)
                if (
                    "." in self.access_roots
                    and self._stat_signature(root_after) != self._stat_signature(root_final)
                ):
                    raise CorpusMutation("corpus root changed during final validation")
                marker_after, marker_sig_after = self._read_corpus_marker(root_fd)
                if marker_after != marker or marker_sig_after != marker_sig:
                    raise CorpusMutation("corpus marker changed during the walk")
                break
            except CacheInvalid as exc:
                if cache_restart_used:
                    raise RuntimeError("previous cache failed repeatedly: %s" % exc)
                log("WARNING: %s; restarting the complete build without cache." % exc)
                cache_restart_used = True
                self._cache_disabled = True
                self._discard_previous()
            except CorpusMutation as exc:
                mutation_attempt += 1
                if mutation_attempt > self.max_walk_retries:
                    raise RuntimeError(
                        "corpus did not stabilize after %d attempt(s): %s"
                        % (mutation_attempt, exc)
                    )
                log(
                    "WARNING: corpus changed during build (%s); retrying complete walk %d/%d."
                    % (exc, mutation_attempt, self.max_walk_retries)
                )
            finally:
                self._building = False
                os.close(root_fd)

        log("Walk complete: %d files. Rebuilding FTS indexes." % self.walk_count)
        cur = self.conn.cursor()
        cur.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        cur.execute("INSERT INTO content_fts(content_fts) VALUES('rebuild')")
        file_count = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        content_count = cur.execute("SELECT COUNT(*) FROM content").fetchone()[0]
        elapsed = time.time() - start
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        breakdown = json.dumps(dict(self.status_counts), ensure_ascii=False, sort_keys=True)
        cur.execute(
            "INSERT INTO meta(schema_version, generated_at, elapsed_seconds, file_count, "
            "content_count, content_status_breakdown, tool_version, corpus_root, corpus_uuid, "
            "access_policy_id, access_policy_generation, policy_digest, access_roots, "
            "excluded_access_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SCHEMA_VERSION, generated_at, elapsed, file_count, content_count, breakdown,
                TOOL_VERSION, self.corpus_root, self.corpus_uuid, self.access_policy_id,
                self.access_policy_generation, self.policy_digest,
                json.dumps(self.access_roots, ensure_ascii=False), self.excluded_access_count,
            ),
        )
        self.conn.execute("PRAGMA journal_mode = DELETE")
        self.conn.commit()
        if self.prev_conn is not None:
            self.prev_conn.close()
            self.prev_conn = None
        log(
            "Build finished in %.1fs: file_count=%d content_count=%d cache_hits=%d excluded_access=%d"
            % (elapsed, file_count, content_count, self.cache_hits, self.excluded_access_count)
        )
        log("content_status breakdown: %s" % breakdown)

    # -- verification and publication --

    def verify(self):
        cur = self.conn.cursor()
        failures = []
        integrity = cur.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            failures.append("integrity_check failed: %r" % (integrity[:3],))
        file_count = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if file_count != self.walk_count:
            failures.append("file_count %d != walk_count %d" % (file_count, self.walk_count))
        if not self.allow_empty and file_count == 0:
            failures.append("empty corpus/index is not allowed")
        if file_count < self.min_file_count:
            failures.append("file_count %d is below min_file_count %d" % (file_count, self.min_file_count))
        if self.excluded_access_count != 0:
            failures.append(
                "excluded_access_count must be zero for a publishable fail-closed build"
            )
        extracted_bytes = cur.execute(
            "SELECT COALESCE(SUM(length(CAST(text AS BLOB))), 0) FROM content"
        ).fetchone()[0]
        if extracted_bytes != self.total_extracted_bytes:
            failures.append(
                "extracted byte count %d != tracked total %d"
                % (extracted_bytes, self.total_extracted_bytes)
            )
        if extracted_bytes > self.max_total_extracted_bytes:
            failures.append("aggregate extracted text exceeds configured budget")
        page_size = cur.execute("PRAGMA page_size").fetchone()[0]
        page_count = cur.execute("PRAGMA page_count").fetchone()[0]
        if page_size * page_count > self.max_index_size_bytes:
            failures.append("scratch index exceeds max_index_size_mb page budget")
        db_filename = cur.execute("PRAGMA database_list").fetchone()[2]
        if db_filename:
            try:
                if os.stat(db_filename, follow_symlinks=False).st_size > self.max_index_size_bytes:
                    failures.append("scratch index file exceeds max_index_size_mb budget")
            except OSError as exc:
                failures.append("cannot stat scratch index during verification: %s" % exc)
        if (
            self.previous_file_count is not None
            and self.previous_policy_digest == self.policy_digest
        ):
            minimum = self.previous_file_count * (1.0 - self.max_drop_fraction)
            if file_count < minimum:
                failures.append(
                    "file_count dropped from %d to %d (more than %.1f%%); inspect the mount "
                    "or intentionally change access_policy_generation"
                    % (self.previous_file_count, file_count, self.max_drop_fraction * 100.0)
                )
        for name in self.prune_dirs:
            escaped = _glob_escape(name)
            leaked = cur.execute(
                "SELECT COUNT(*) FROM files WHERE relpath_nfc GLOB ? OR relpath_nfc GLOB ?",
                (escaped + "/*", "*/" + escaped + "/*"),
            ).fetchone()[0]
            if leaked:
                failures.append("%d rows carry a pruned %r directory segment" % (leaked, name))
        if self.smoke_term is not None:
            term = '"%s"' % self.smoke_term.replace('"', '""')
            name_hits = cur.execute(
                "SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH ?", (term,)
            ).fetchone()[0]
            content_hits = cur.execute(
                "SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH ?", (term,)
            ).fetchone()[0]
            if name_hits <= 0:
                failures.append("files_fts smoke %r returned 0 hits" % self.smoke_term)
            if content_hits <= 0:
                failures.append("content_fts smoke %r returned 0 hits" % self.smoke_term)
            smoke_summary = "files=%d content=%d" % (name_hits, content_hits)
        else:
            smoke_summary = "skipped"
        meta = cur.execute("SELECT * FROM meta").fetchall()
        if len(meta) != 1:
            failures.append("meta has %d rows (expected 1)" % len(meta))
        elif any(value is None for value in meta[0]):
            failures.append("meta row has NULL fields")
        root_fd = self._open_corpus_root()
        try:
            marker, marker_sig = self._read_corpus_marker(root_fd)
        finally:
            os.close(root_fd)
        if marker != self.corpus_uuid or marker_sig != self._corpus_marker_sig:
            failures.append("corpus marker changed after build")
        if failures:
            for item in failures:
                log("VERIFICATION FAILURE: %s" % item)
            return False
        log(
            "Verification OK: file_count=%d prune_segments_clean=%d smoke=%s meta=1"
            % (file_count, len(self.prune_dirs), smoke_summary)
        )
        return True

    def _target_gid(self):
        if self.publish_group is None:
            return None
        if grp is None:
            raise RuntimeError("publish_group requires POSIX grp support")
        try:
            return grp.getgrnam(self.publish_group).gr_gid
        except KeyError:
            raise RuntimeError("publish_group does not exist: %r" % self.publish_group)

    def _expected_publication_gid(self):
        """Resolve the GID that new files must receive, including inheritance."""
        explicit = self._target_gid()
        if explicit is not None:
            return int(explicit)
        # With no group permission bits, a changing GID cannot widen POSIX
        # readership and need not become policy state.
        if self.publish_mode & 0o070 == 0:
            return None
        if os.name != "posix" or not hasattr(os, "getegid"):
            raise RuntimeError(
                "group-readable publish_mode requires POSIX effective-GID support"
            )
        if self._publish_dir_fd is not None:
            directory = os.fstat(self._publish_dir_fd)
            if directory.st_mode & statmod.S_ISGID:
                return int(directory.st_gid)
        return int(os.getegid())

    def publish(self):
        if self._lock_fd is None:
            raise RuntimeError("publication lock is not held")
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        binding_created = self._ensure_publication_binding()
        bound_publication = self._read_publication_binding()
        expected_gid = bound_publication["publish_gid"]
        self._publication_replaced = False
        target = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        target_name = PUBLISHED_DB_NAME
        temp_fd = None
        temp_name = None
        temp_stat = None
        try:
            self._assert_publish_dir_current()
            for _attempt in range(100):
                candidate = ".aplidocs-index.%s.tmp" % uuid.uuid4().hex
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                try:
                    temp_fd = os.open(candidate, flags, 0o600, dir_fd=self._publish_dir_fd)
                    temp_name = candidate
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise RuntimeError("could not allocate a unique publication temporary")
            temp_stat = os.fstat(temp_fd)
            if not statmod.S_ISREG(temp_stat.st_mode) or temp_stat.st_nlink != 1:
                raise RuntimeError("publication temporary is not a private regular file")
            source_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            source_fd = os.open(self.output_path, source_flags)
            try:
                source_before = os.fstat(source_fd)
                if (
                    not statmod.S_ISREG(source_before.st_mode)
                    or source_before.st_nlink != 1
                    or self._scratch_output_identity is None
                    or self._identity(source_before) != self._scratch_output_identity
                ):
                    raise RuntimeError(
                        "scratch database was replaced after build verification"
                    )
                source_signature = self._stat_signature(source_before)
                while True:
                    chunk = os.read(source_fd, HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(temp_fd, chunk[offset:])
                source_after = os.fstat(source_fd)
                named_source = os.stat(self.output_path, follow_symlinks=False)
                if (
                    self._stat_signature(source_after) != source_signature
                    or self._identity(named_source) != self._scratch_output_identity
                ):
                    raise RuntimeError(
                        "scratch database changed while preparing publication"
                    )
            finally:
                os.close(source_fd)
            target_gid = self._target_gid()
            if target_gid is not None:
                os.fchown(temp_fd, -1, target_gid)
            os.fchmod(temp_fd, self.publish_mode)
            os.fsync(temp_fd)
            final_stat = os.fstat(temp_fd)
            named_stat = os.stat(temp_name, dir_fd=self._publish_dir_fd, follow_symlinks=False)
            if self._identity(final_stat) != self._identity(named_stat):
                raise RuntimeError("publication temporary name was replaced")
            if statmod.S_IMODE(final_stat.st_mode) != self.publish_mode:
                raise RuntimeError("publication mode did not apply")
            if expected_gid is not None and final_stat.st_gid != expected_gid:
                raise RuntimeError("publication group did not apply")
            os.close(temp_fd)
            temp_fd = None
            # Validate the exact fully-copied temporary before it can replace
            # the known-good live DB.  Inode checks around the path-based
            # SQLite open ensure the unique name still denotes our file.
            self._assert_publish_dir_current()
            temp_path = os.path.join(self.publish_dir, temp_name)
            check_conn = sqlite3.connect(sqlite_readonly_uri(temp_path), uri=True)
            try:
                quick = check_conn.execute("PRAGMA quick_check").fetchall()
            finally:
                check_conn.close()
            if quick != [("ok",)]:
                raise RuntimeError("publication temporary failed quick_check: %r" % (quick[:3],))
            checked_stat = os.stat(
                temp_name, dir_fd=self._publish_dir_fd, follow_symlinks=False
            )
            if self._identity(checked_stat) != self._identity(final_stat):
                raise RuntimeError("publication temporary was replaced during validation")
            self._assert_publish_dir_current()
            os.replace(
                temp_name, target_name,
                src_dir_fd=self._publish_dir_fd, dst_dir_fd=self._publish_dir_fd,
            )
            self._publication_replaced = True
            temp_name = None
            target_stat = os.stat(
                target_name, dir_fd=self._publish_dir_fd, follow_symlinks=False
            )
            if (
                not statmod.S_ISREG(target_stat.st_mode)
                or target_stat.st_nlink != 1
                or self._identity(target_stat) != self._identity(final_stat)
                or statmod.S_IMODE(target_stat.st_mode) != self.publish_mode
                or (expected_gid is not None and target_stat.st_gid != expected_gid)
            ):
                raise RuntimeError("published target failed post-rename mode/type validation")
            os.fsync(self._publish_dir_fd)
            self._finalize_publication_binding()
            self._assert_publish_dir_current()
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name is not None:
                try:
                    current = os.stat(
                        temp_name, dir_fd=self._publish_dir_fd, follow_symlinks=False
                    )
                    if temp_stat is not None and self._identity(current) == self._identity(temp_stat):
                        os.unlink(temp_name, dir_fd=self._publish_dir_fd)
                except FileNotFoundError:
                    pass
            if not self._publication_replaced:
                self._rollback_publication_binding(binding_created)
        log("Published to %r" % target)

    def cleanup_scratch(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(self.output_path + suffix)
            except FileNotFoundError:
                pass

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self._discard_previous()
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        if self._publish_dir_fd is not None:
            try:
                os.close(self._publish_dir_fd)
            except OSError:
                pass
            self._publish_dir_fd = None
        if self._scratch_lock_fd is not None:
            try:
                fcntl.flock(self._scratch_lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._scratch_lock_fd)
            except OSError:
                pass
            self._scratch_lock_fd = None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build and publish the ApliDocs full-text SQLite index."
    )
    parser.add_argument(
        "--config", default=None,
        help="path to the JSON config (default: config.json next to this script)",
    )
    parser.add_argument(
        "--output", default=None,
        help="[test-only] built DB path (default: <script_dir>/build/%s)"
        % PUBLISHED_DB_NAME,
    )
    parser.add_argument(
        "--no-publish", action="store_true",
        help="[test-only] build and verify but do not publish",
    )
    parser.add_argument(
        "--keep-build", action="store_true",
        help="keep the private scratch database after a successful publication",
    )
    parser.add_argument(
        "--init-corpus-id", action="store_true",
        help="create the corpus UUID marker once, then exit without building",
    )
    args = parser.parse_args(argv)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config
    if config_path is None:
        config_path = os.path.join(script_dir, "config.json")
    config = load_config(config_path)

    output = args.output
    if output is None:
        output = os.path.join(script_dir, "build", PUBLISHED_DB_NAME)

    builder = None
    try:
        builder = IndexBuilder(config, output)
        if args.init_corpus_id:
            builder.initialize_corpus_id()
            return 0
        builder.acquire_publish_lock()
        builder.build()
        if not builder.verify():
            log("ABORT: verification failed; nothing published.")
            return 1
        if args.no_publish:
            log("--no-publish set; verified build kept at %r." % output)
        else:
            builder.publish()
            if args.keep_build:
                log("--keep-build set; private scratch kept at %r." % output)
            else:
                builder.cleanup_scratch()
    except (
        ValueError,
        RuntimeError,
        DependencyUnavailable,
        OSError,
        UnicodeError,
        sqlite3.DatabaseError,
    ) as exc:
        if builder is not None and builder._publication_replaced:
            log(
                "POST-PUBLISH ERROR: the live target was replaced, but a later "
                "validation/cleanup step failed (%s). Inspect the target and scratch." % exc
            )
        else:
            log("ABORT: %s; nothing published." % exc)
        return 1
    finally:
        if builder is not None:
            builder.close()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import collections
import fcntl
import fnmatch
import html.parser
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

try:
    import pysqlite3.dbapi2 as sqlite3
except ImportError:
    import sqlite3

# --- Constants ---------------------------------------------------------------

SCHEMA_VERSION = 1
TOOL_VERSION = "aplidocs/1.0"

PUBLISHED_DB_NAME = "aplidocs-index.sqlite"
PUBLISH_TMP_NAME = ".aplidocs-index.sqlite.tmp"

ROOT_AREA_SENTINEL = "_ROOT"

# Directory segment names pruned at any depth in addition to the user's
# `prune_dirs`. These are Synology bookkeeping directories that never hold
# user documents; excluding them is always correct.
ALWAYS_PRUNE_DIRS = frozenset(("@eaDir", "#recycle"))
# Operating-system junk files skipped everywhere.
SKIP_FILES = frozenset((".DS_Store", "desktop.ini", "Thumbs.db"))

FTS_TOKENIZE = "unicode61 remove_diacritics 2"
TEXT_CAP_BYTES = 500 * 1024
PDFTOTEXT_TIMEOUT = 60

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
    "prune_dirs": ((list,), False, []),
    "metadata_only_globs": ((list,), False, []),
    "filename_pattern": ((str, type(None)), False, None),
    "smoke_term": ((str, type(None)), False, None),
}


def log(message):
    """Emit a timestamped line to stdout (captured by the cron log)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[%s] %s" % (stamp, message), flush=True)


def config_error(message):
    """Report a configuration problem and abort. Nothing is published."""
    log("CONFIG ERROR: %s" % message)
    sys.exit(2)


# --- Config ------------------------------------------------------------------


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
        data = json.loads(raw.decode("utf-8"))
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
            resolved[key] = default
            continue
        value = data[key]
        # bool is a subclass of int; guard against it being accepted anywhere.
        if isinstance(value, bool) or not isinstance(value, types):
            config_error(
                "config key %r must be %s, got %s"
                % (key, " or ".join(t.__name__ for t in types), type(value).__name__)
            )
        if types == (list,):
            for element in value:
                if not isinstance(element, str) or isinstance(element, bool):
                    config_error("config key %r must be a list of strings" % key)
        resolved[key] = value

    if not resolved["corpus_root"].strip():
        config_error("config key 'corpus_root' must not be empty")
    if not resolved["publish_dir"].strip():
        config_error("config key 'publish_dir' must not be empty")
    if not os.path.isabs(resolved["corpus_root"]):
        config_error("config key 'corpus_root' must be an absolute path")
    if not os.path.isabs(resolved["publish_dir"]):
        config_error("config key 'publish_dir' must be an absolute path")
    if os.path.abspath(resolved["publish_dir"]) == os.path.abspath(resolved["corpus_root"]):
        config_error(
            "'publish_dir' must not be the same directory as 'corpus_root' "
            "(the published index would end up indexing itself)"
        )
    for name in resolved["prune_dirs"]:
        if not name.strip():
            config_error("config key 'prune_dirs' must not contain empty names")
        if "/" in name:
            config_error(
                "prune_dirs entries are single directory segment names, "
                "not paths: %r" % name
            )
    for glob in resolved["metadata_only_globs"]:
        if not glob.strip():
            config_error("config key 'metadata_only_globs' must not contain empty globs")
    if resolved["smoke_term"] is not None and not resolved["smoke_term"].strip():
        config_error("config key 'smoke_term' must be null or a non-empty term")
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


def _xml_flatten(data, block_localnames):
    """Flatten XML bytes to text, inserting a space at each block boundary.

    Text nested in the same inline run stays glued (words preserved); block
    elements (paragraphs, breaks, rows) become spaces so words do not merge
    across paragraphs. Purely structural, no semantics.
    """
    root = ET.fromstring(data)
    out = []
    for elem in root.iter():
        tag = elem.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local in block_localnames:
            out.append(" ")
        if elem.text:
            out.append(elem.text)
        if elem.tail:
            out.append(elem.tail)
    return "".join(out)


def _extract_word(fullpath):
    with zipfile.ZipFile(fullpath) as archive:
        data = archive.read("word/document.xml")
    return _xml_flatten(data, XML_BLOCK_LOCALNAMES)


def _extract_excel(fullpath):
    texts = []
    with zipfile.ZipFile(fullpath) as archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" in names:
            root = ET.fromstring(archive.read("xl/workbook.xml"))
            for elem in root.iter():
                tag = elem.tag
                local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
                if local == "sheet":
                    name = elem.get("name")
                    if name:
                        texts.append(name)
        if "xl/sharedStrings.xml" in names:
            texts.append(
                _xml_flatten(archive.read("xl/sharedStrings.xml"), XML_BLOCK_LOCALNAMES)
            )
    return " ".join(texts)


def _extract_odt(fullpath):
    with zipfile.ZipFile(fullpath) as archive:
        data = archive.read("content.xml")
    return _xml_flatten(data, XML_BLOCK_LOCALNAMES)


def _read_text_file(fullpath):
    with open(fullpath, "rb") as handle:
        raw = handle.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _extract_html(fullpath):
    parser = _HTMLTextExtractor()
    parser.feed(_read_text_file(fullpath))
    return parser.get_text()


def _extract_pdf(fullpath):
    """Run pdftotext to stdout. Raises on timeout or nonzero exit."""
    proc = subprocess.run(
        ["pdftotext", "-q", "-enc", "UTF-8", fullpath, "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=PDFTOTEXT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError("pdftotext exit code %d" % proc.returncode)
    return proc.stdout.decode("utf-8", "replace")


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
    """Top-level path segment, or the _ROOT sentinel for root-level files."""
    idx = relpath.find("/")
    if idx == -1:
        return ROOT_AREA_SENTINEL
    return relpath[:idx]


def derive_path_year(relpath):
    """First pure-year directory segment (4 digits, 1990-2099) in the path."""
    parts = relpath.split("/")[:-1]
    for part in parts:
        if len(part) == 4 and part.isdigit():
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
    match = pattern.search(filename)
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


# --- Builder -----------------------------------------------------------------


class IndexBuilder:
    def __init__(self, config, output_path):
        self.corpus_root = config["corpus_root"]
        self.publish_dir = config["publish_dir"]
        self.output_path = output_path
        # Segment names to prune during the walk (user + always-pruned junk).
        self.prune_dirs = list(config["prune_dirs"])
        self.prune_segments = frozenset(self.prune_dirs) | ALWAYS_PRUNE_DIRS
        # publish_dir is pruned from the walk by absolute-path match when it
        # lives inside corpus_root, so the index never indexes itself.
        self.publish_dir_abs = os.path.abspath(self.publish_dir)
        # metadata_only globs precompiled to regexes (matched case-sensitively
        # against the forward-slash relpath, same on every platform).
        self.metadata_only_res = [
            re.compile(fnmatch.translate(glob)) for glob in config["metadata_only_globs"]
        ]
        pattern = config["filename_pattern"]
        try:
            self.filename_pattern = re.compile(pattern) if pattern else None
        except re.error as exc:
            config_error("config key 'filename_pattern' is not a valid regex: %s" % exc)
        if self.filename_pattern is not None and not self.filename_pattern.groupindex:
            config_error(
                "config key 'filename_pattern' must contain at least one "
                "named group (?P<name>...); only named groups are stored"
            )
        self.smoke_term = config["smoke_term"]
        self.conn = None
        self.prev_conn = None
        self.prev_meta = {}  # relpath -> (source_sig, content_status)
        self.walk_count = 0
        self.status_counts = collections.Counter()
        self.cache_hits = 0

    # -- previous DB (incremental cache) --

    def load_previous(self):
        published = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        if not os.path.isfile(published):
            log("No previous published DB at %r; full extraction." % published)
            return
        try:
            uri = pathlib.Path(published).resolve().as_uri() + "?mode=ro&immutable=1"
            self.prev_conn = sqlite3.connect(uri, uri=True)
            prev_schema, prev_tool = self.prev_conn.execute(
                "SELECT schema_version, tool_version FROM meta"
            ).fetchone()
            if prev_schema != SCHEMA_VERSION or prev_tool != TOOL_VERSION:
                raise RuntimeError(
                    "previous DB from %r schema %r != current %r schema %r"
                    % (prev_tool, prev_schema, TOOL_VERSION, SCHEMA_VERSION)
                )
            cursor = self.prev_conn.execute(
                "SELECT relpath, size_bytes, mtime, content_status FROM files"
            )
            for relpath, size_bytes, mtime, status in cursor:
                self.prev_meta[relpath] = ("%d:%d" % (size_bytes, mtime), status)
            log(
                "Loaded previous DB cache: %d files from %r"
                % (len(self.prev_meta), published)
            )
        except Exception as exc:  # corrupt/unreadable previous DB -> full rebuild
            log("WARNING: previous DB unusable (%s); full extraction." % exc)
            if self.prev_conn is not None:
                try:
                    self.prev_conn.close()
                except Exception:
                    pass
            self.prev_conn = None
            self.prev_meta = {}

    def cached_extraction(self, relpath, source_sig):
        """Return (status, text, extractor) if reusable, else None."""
        cached = self.prev_meta.get(relpath)
        if cached is None:
            return None
        prev_sig, prev_status = cached
        if prev_sig != source_sig or prev_status not in CACHEABLE_STATUSES:
            return None
        if prev_status == ST_EXTRACTED:
            row = self.prev_conn.execute(
                "SELECT text, extractor FROM content WHERE relpath = ?", (relpath,)
            ).fetchone()
            if row is None:
                # Marked extracted but empty (no content row): keep the status.
                return ST_EXTRACTED, None, None
            return ST_EXTRACTED, row[0], row[1]
        return prev_status, None, None

    # -- schema --

    def create_schema(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA synchronous = OFF;
            PRAGMA journal_mode = MEMORY;

            CREATE TABLE meta (
                schema_version INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL,
                file_count INTEGER NOT NULL,
                content_count INTEGER NOT NULL,
                content_status_breakdown TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                corpus_root TEXT NOT NULL
            );

            CREATE TABLE files (
                relpath TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                area TEXT NOT NULL,
                ext TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                path_year INTEGER,
                name_meta TEXT,
                metadata_only INTEGER NOT NULL,
                content_status TEXT NOT NULL
            );

            CREATE TABLE content (
                relpath TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                extractor TEXT NOT NULL,
                source_sig TEXT NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE VIRTUAL TABLE files_fts USING fts5("
            "filename, relpath, content='files', content_rowid='rowid', "
            "tokenize='%s')" % FTS_TOKENIZE
        )
        cur.execute(
            "CREATE VIRTUAL TABLE content_fts USING fts5("
            "text, content='content', content_rowid='rowid', "
            "tokenize='%s')" % FTS_TOKENIZE
        )
        self.conn.commit()

    # -- per-file classification / extraction --

    def is_metadata_only(self, relpath):
        return any(rx.match(relpath) for rx in self.metadata_only_res)

    def classify(self, fullpath, relpath, ext, size_bytes, mtime):
        """Return (content_status, text, extractor).

        text is the collapsed plain text to index (or None when there is no
        content row to write). Per-file failures are recorded as `error`.
        """
        if self.is_metadata_only(relpath):
            # Indexed by name/path only; content extraction deliberately skipped.
            return ST_METADATA_ONLY, None, None

        source_sig = "%d:%d" % (size_bytes, mtime)
        cached = self.cached_extraction(relpath, source_sig)
        if cached is not None:
            self.cache_hits += 1
            return cached

        if ext in UNSUPPORTED_EXT:
            return ST_UNSUPPORTED, None, None

        if ext in PDF_EXT:
            extractor = "pdftotext"
            try:
                raw = _extract_pdf(fullpath)
            except Exception as exc:
                log("WARNING: pdf extraction failed for %r: %s" % (relpath, exc))
                return ST_ERROR, None, None
            text = _collapse_whitespace(raw)
            if not text:
                return ST_EMPTY_PDF, None, None
        elif ext in WORD_EXT:
            extractor = "ooxml-word"
            text = self._safe_extract(_extract_word, fullpath, relpath)
        elif ext in EXCEL_EXT:
            extractor = "ooxml-excel"
            text = self._safe_extract(_extract_excel, fullpath, relpath)
        elif ext in ODT_EXT:
            extractor = "odt"
            text = self._safe_extract(_extract_odt, fullpath, relpath)
        elif ext in TEXT_EXT:
            extractor = "text"
            text = self._safe_extract(_read_text_file, fullpath, relpath)
        elif ext in HTML_EXT:
            extractor = "html"
            text = self._safe_extract(_extract_html, fullpath, relpath)
        else:
            return ST_NO_TEXT, None, None

        if text is None:
            return ST_ERROR, None, None
        text = _collapse_whitespace(text)
        if len(text.encode("utf-8")) > TEXT_CAP_BYTES:
            return ST_TOO_BIG, None, None
        # Supported format that parsed cleanly: `extracted` even if empty.
        # An empty result simply writes no content row (text=None sentinel).
        return ST_EXTRACTED, (text if text else None), extractor

    def _safe_extract(self, func, fullpath, relpath):
        try:
            return func(fullpath)
        except Exception as exc:
            log("WARNING: extraction failed for %r: %s" % (relpath, exc))
            return None

    # -- walk + populate --

    def build(self):
        start = time.time()
        log("Build start. corpus_root=%r output=%r" % (self.corpus_root, self.output_path))
        self.load_previous()

        self._open_fresh_db()
        self.create_schema()
        cur = self.conn.cursor()

        def _walk_error(exc):
            # Fail fast: a directory we cannot list means an incomplete
            # index, which must never be published silently.
            raise RuntimeError("walk failed for %r: %s" % (getattr(exc, 'filename', '?'), exc))

        for dirpath, dirnames, filenames in os.walk(
            self.corpus_root, onerror=_walk_error, followlinks=False
        ):
            # Prune excluded directory segments in place (never descend them),
            # plus publish_dir itself when it lives inside corpus_root (matched
            # by absolute path so the index never indexes its own output).
            dirnames[:] = [
                d for d in dirnames
                if d not in self.prune_segments
                and os.path.abspath(os.path.join(dirpath, d)) != self.publish_dir_abs
            ]
            for filename in filenames:
                if filename in SKIP_FILES:
                    continue
                fullpath = os.path.join(dirpath, filename)
                if os.path.islink(fullpath):
                    # Symlinks are not canonical content; the real file is
                    # indexed by its own path. Consistent with followlinks=False.
                    continue
                try:
                    stat = os.stat(fullpath)
                except OSError as exc:
                    raise RuntimeError("stat failed for %r: %s" % (fullpath, exc))

                rel = os.path.relpath(fullpath, self.corpus_root)
                relpath = unicodedata.normalize("NFC", rel.replace(os.sep, "/"))
                fname = unicodedata.normalize("NFC", filename)
                ext = file_extension(fname)
                size_bytes = int(stat.st_size)
                mtime = int(stat.st_mtime)

                status, text, extractor = self.classify(
                    fullpath, relpath, ext, size_bytes, mtime
                )
                self.status_counts[status] += 1
                self.walk_count += 1

                area = derive_area(relpath)
                path_year = derive_path_year(relpath)
                name_meta = derive_name_meta(fname, self.filename_pattern)
                metadata_only = 1 if status == ST_METADATA_ONLY else 0

                cur.execute(
                    "INSERT INTO files(relpath, filename, area, ext, size_bytes, "
                    "mtime, path_year, name_meta, metadata_only, content_status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        relpath, fname, area, ext, size_bytes, mtime,
                        path_year, name_meta, metadata_only, status,
                    ),
                )
                if text:
                    cur.execute(
                        "INSERT INTO content(relpath, text, extractor, source_sig) "
                        "VALUES(?,?,?,?)",
                        (relpath, text, extractor, "%d:%d" % (size_bytes, mtime)),
                    )

                if self.walk_count % 1000 == 0:
                    log(
                        "  ... %d files walked (cache hits %d)"
                        % (self.walk_count, self.cache_hits)
                    )

        log("Walk complete: %d files. Rebuilding FTS indexes." % self.walk_count)
        cur.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        cur.execute("INSERT INTO content_fts(content_fts) VALUES('rebuild')")

        file_count = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        content_count = cur.execute("SELECT COUNT(*) FROM content").fetchone()[0]
        elapsed = time.time() - start
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        breakdown = json.dumps(dict(self.status_counts), ensure_ascii=False, sort_keys=True)
        cur.execute(
            "INSERT INTO meta(schema_version, generated_at, elapsed_seconds, "
            "file_count, content_count, content_status_breakdown, tool_version, "
            "corpus_root) VALUES(?,?,?,?,?,?,?,?)",
            (
                SCHEMA_VERSION, generated_at, elapsed, file_count, content_count,
                breakdown, TOOL_VERSION, self.corpus_root,
            ),
        )
        self.conn.execute("PRAGMA journal_mode = DELETE")
        self.conn.commit()
        log(
            "Build finished in %.1fs: file_count=%d content_count=%d cache_hits=%d"
            % (elapsed, file_count, content_count, self.cache_hits)
        )
        log("content_status breakdown: %s" % breakdown)

        # The previous-DB cache is fully consumed once the walk is done; it is
        # never read again. Release its handle before verify/publish so we are
        # not holding an open descriptor on the file we are about to replace.
        if self.prev_conn is not None:
            self.prev_conn.close()
            self.prev_conn = None

    def _open_fresh_db(self):
        build_dir = os.path.dirname(self.output_path)
        if build_dir:
            os.makedirs(build_dir, exist_ok=True)
        # Single-build lock: two concurrent builds would share build/ and could
        # end up publishing an unverified database. Fail fast instead.
        lock_path = os.path.join(build_dir or ".", ".build.lock")
        self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "another build is already running (lock %r held)" % lock_path
            )
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.output_path + suffix
            if os.path.exists(path):
                os.remove(path)
        self.conn = sqlite3.connect(self.output_path)

    # -- verification --

    def verify(self):
        cur = self.conn.cursor()
        failures = []

        file_count = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if file_count != self.walk_count:
            failures.append(
                "file_count %d != walk_count %d" % (file_count, self.walk_count)
            )

        # Leak check: for every pruned directory name assert that not a single
        # row carries that segment anywhere in its relpath. This is the hard
        # guarantee that private folders never reach the published index.
        for name in self.prune_dirs:
            # The GLOB patterns must treat the configured name as a literal
            # segment even when it contains GLOB metacharacters (*, ?, [).
            escaped = _glob_escape(name)
            leaked = cur.execute(
                "SELECT COUNT(*) FROM files WHERE relpath = ? "
                "OR relpath GLOB ? OR relpath GLOB ? OR relpath GLOB ?",
                (name, escaped + "/*", "*/" + escaped + "/*", "*/" + escaped),
            ).fetchone()[0]
            if leaked != 0:
                failures.append("%d rows carry a %r path segment" % (leaked, name))

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
            smoke_summary = "files_fts(%r)=%d content_fts(%r)=%d" % (
                self.smoke_term, name_hits, self.smoke_term, content_hits,
            )
        else:
            log("smoke_term not configured; skipping FTS smoke checks.")
            smoke_summary = "smoke=skipped"

        meta = cur.execute("SELECT * FROM meta").fetchall()
        if len(meta) != 1:
            failures.append("meta has %d rows (expected 1)" % len(meta))
        elif any(value is None for value in meta[0]):
            failures.append("meta row has NULL fields")

        if failures:
            for item in failures:
                log("VERIFICATION FAILURE: %s" % item)
            return False
        log(
            "Verification OK: file_count=%d prune_segments_clean=%d %s meta=1"
            % (file_count, len(self.prune_dirs), smoke_summary)
        )
        return True

    # -- publish --

    def publish(self):
        os.makedirs(self.publish_dir, exist_ok=True)
        tmp = os.path.join(self.publish_dir, PUBLISH_TMP_NAME)
        target = os.path.join(self.publish_dir, PUBLISHED_DB_NAME)
        try:
            # Copy into the publish dir first, then rename WITHIN it. The share
            # and the build dir may be different btrfs subvolumes, so a direct
            # cross-directory os.replace could fail with EXDEV; renaming inside
            # the publish dir keeps the swap atomic and on one filesystem.
            with open(self.output_path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())  # data durable before the rename below
            os.replace(tmp, target)  # atomic within the target dir (same subvol)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        log("Published to %r" % target)

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.prev_conn is not None:
            self.prev_conn.close()
            self.prev_conn = None


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
    args = parser.parse_args(argv)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config
    if config_path is None:
        config_path = os.path.join(script_dir, "config.json")
    config = load_config(config_path)

    output = args.output
    if output is None:
        output = os.path.join(script_dir, "build", PUBLISHED_DB_NAME)

    builder = IndexBuilder(config, output)
    try:
        builder.build()
        if not builder.verify():
            log("ABORT: verification failed; nothing published.")
            return 1
        if args.no_publish:
            log("--no-publish set; verified build kept at %r." % output)
        else:
            builder.publish()
    finally:
        builder.close()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

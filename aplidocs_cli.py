#!/usr/bin/env python3
"""Read-only CLI over the ApliDocs full-text index (SQLite + FTS5).

One file for every channel (SSH on the server via the venv python, or SMB with
a local python). Selects cysqlite, then pysqlite3, then stdlib sqlite3;
either way a safe SQLite version with FTS5 is required and verified at startup.
The database is ALWAYS
opened read-only + immutable so a plain SELECT over SMB cannot try to create
lock or journal files next to the .sqlite.

Subcommands: status | search | name | sql. Output is JSON by default (--tsv
for tab-separated). Errors go to stderr with a nonzero exit. See README.md.
"""
import argparse
import base64
import json
import math
import os
import pathlib
import sys
import time

from aplidocs_common import (
    absolute_path_to_sqlite_uri as _absolute_path_to_sqlite_uri,
    require_safe_sqlite_provider,
    select_fts5_provider,
    sqlite_readonly_uri,
)

import sqlite3 as _stdlib_sqlite3

_SQLITE_IMPORT_FAILURES = []
_SQLITE_PROVIDER_CANDIDATES = []
try:
    import cysqlite as _cysqlite
except Exception as exc:
    _SQLITE_IMPORT_FAILURES.append("cysqlite import: %s" % exc)
else:
    _SQLITE_PROVIDER_CANDIDATES.append(("cysqlite", _cysqlite))
try:
    import pysqlite3.dbapi2 as _pysqlite3
except Exception as exc:
    _SQLITE_IMPORT_FAILURES.append("pysqlite3 import: %s" % exc)
else:
    _SQLITE_PROVIDER_CANDIDATES.append(("pysqlite3", _pysqlite3))
_SQLITE_PROVIDER_CANDIDATES.append(("stdlib sqlite3", _stdlib_sqlite3))
_SQLITE_PROVIDER_CANDIDATES = tuple(_SQLITE_PROVIDER_CANDIDATES)
sqlite3 = _SQLITE_PROVIDER_CANDIDATES[0][1]


PUBLISHED_DB_NAME = "aplidocs-index.sqlite"

# The SQL escape hatch must not turn an accidental broad query (or a single
# very large BLOB) into unbounded process memory/output.  These are deliberately
# constants rather than hidden truncation: callers get an actionable error and
# can refine their SELECT.  Keeping them at module level also lets deployments
# choose different policy in a thin wrapper without changing query semantics.
SQL_MAX_ROWS = 1000
SQL_MAX_JSON_BYTES = 4 * 1024 * 1024
SQL_MAX_SQLITE_LENGTH = 1024 * 1024
SQL_MAX_QUERY_BYTES = 64 * 1024
SQL_MAX_COLUMNS = 24
SQL_PROGRESS_OPCODES = 1000
SQL_MAX_VM_STEPS = 5 * 1000 * 1000
SQL_MAX_SECONDS = 5.0
SEARCH_MAX_LIMIT = 1000

# Reciprocal Rank Fusion combines rankings whose raw BM25 scores are not
# comparable (files_fts and content_fts have different corpora/columns).
RRF_K = 60
RRF_MAX_CANDIDATES_PER_SOURCE = 10000
SEARCH_DETAIL_CHUNK_SIZE = 400
SNIPPET_MAX_CHARS = 2048

# Columns returned for a file hit, and their JSON keys.
FILE_COLS = (
    "f.relpath", "f.area", "f.ext", "f.path_year", "f.name_meta",
    "f.size_bytes", "f.mtime", "f.metadata_only", "f.content_status",
)
FILE_KEYS = (
    "relpath", "area", "ext", "path_year", "name_meta",
    "size_bytes", "mtime", "metadata_only", "content_status",
)


def die(message, code=2):
    sys.stderr.write("aplidocs: %s\n" % message)
    sys.exit(code)


class ApliDocsArgumentParser(argparse.ArgumentParser):
    """Argument parser whose failures follow the CLI's stderr contract."""

    def error(self, message):
        die(message, code=2)


def check_fts5():
    """Fail fast with an actionable message if FTS5 is unavailable."""
    global sqlite3
    try:
        provider, _label, _version = select_fts5_provider(
            _SQLITE_PROVIDER_CANDIDATES,
            _SQLITE_IMPORT_FAILURES,
            version_checker=require_safe_sqlite_provider,
        )
    except RuntimeError as exc:
        die(
            "%s. Install/use cysqlite 0.3.4+ where compatible, or a "
            "vendor/container/self-built Python linked to SQLite 3.53.3+. "
            "See docs/SYNOLOGY.md." % exc,
            code=3,
        )
    sqlite3 = provider


def open_readonly(db_path):
    try:
        path = pathlib.Path(db_path)
        if not path.is_file():
            die("index database not found: %s" % db_path, code=2)
        uri = sqlite_readonly_uri(db_path)
        return sqlite3.connect(uri, uri=True)
    except SystemExit:
        raise
    except Exception as exc:
        # Every filesystem, path conversion and SQLite opening failure follows
        # the same CLI contract instead of leaking a Python traceback.
        die("cannot open index database %s (%s)" % (db_path, exc), code=2)


def fts_match_expr(user_terms):
    """Wrap each whitespace-separated term as an FTS5 phrase (AND semantics).

    Quoting each term as a phrase neutralizes quotes/hyphens/operators so user
    input can never produce an FTS5 syntax error. Embedded quotes are doubled.
    """
    terms = user_terms.split()
    if not terms:
        die("empty search terms", code=2)
    return " ".join('"%s"' % term.replace('"', '""') for term in terms)


def positive_int(value):
    """argparse type for limits that cannot disable SQL's LIMIT clause."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer")
    if parsed <= 0 or parsed > SEARCH_MAX_LIMIT:
        raise argparse.ArgumentTypeError(
            "must be between 1 and %d" % SEARCH_MAX_LIMIT
        )
    return parsed


def build_filters(args):
    clauses = []
    params = []
    area = getattr(args, "area", None)
    ext = getattr(args, "ext", None)
    year = getattr(args, "year", None)
    metadata_only = getattr(args, "metadata_only", False)
    if area:
        clauses.append("f.area = ?")
        params.append(area)
    if ext:
        clauses.append("f.ext = ?")
        params.append(ext.lower().lstrip("."))
    if year is not None:
        clauses.append("f.path_year = ?")
        params.append(year)
    if metadata_only:
        clauses.append("f.metadata_only = 1")
    return clauses, params


def row_to_file(row):
    item = dict(zip(FILE_KEYS, row[: len(FILE_KEYS)]))
    # name_meta is stored as a JSON string; emit it as a parsed object so JSON
    # consumers get structured fields. (In --tsv mode emit() re-serializes it.)
    raw = item.get("name_meta")
    item["name_meta"] = json.loads(raw) if raw else None
    return item


def json_compatible(value):
    """Convert SQLite/Python values to an unambiguous JSON-safe structure."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            label = "nan"
        elif value > 0:
            label = "infinity"
        else:
            label = "-infinity"
        return {"$float": label}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$blob": base64.b64encode(bytes(value)).decode("ascii"),
            "encoding": "base64",
        }
    if isinstance(value, dict):
        return {key: json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    return value


def query_names(conn, match_expr, args, limit):
    clauses, params = build_filters(args)
    sql = (
        "SELECT " + ", ".join(FILE_COLS) + ", bm25(files_fts) AS score "
        "FROM files_fts JOIN files f ON f.rowid = files_fts.rowid "
        "WHERE files_fts MATCH ?"
    )
    query_params = [match_expr] + params
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY bm25(files_fts), f.relpath COLLATE BINARY LIMIT ?"
    query_params.append(limit)
    hits = []
    for row in conn.execute(sql, query_params):
        item = row_to_file(row)
        item["matched_by"] = "name"
        item["snippet"] = None
        item["score"] = row[-1]
        hits.append(item)
    return hits


def _query_ranks(conn, match_expr, args, limit, source):
    """Return only relpath/BM25 ranks for bounded, low-memory RRF fusion."""
    clauses, params = build_filters(args)
    if source == "name":
        sql = (
            "SELECT f.relpath, bm25(files_fts) "
            "FROM files_fts JOIN files f ON f.rowid = files_fts.rowid "
            "WHERE files_fts MATCH ?"
        )
        order = "bm25(files_fts)"
    elif source == "content":
        sql = (
            "SELECT c.relpath, bm25(content_fts) "
            "FROM content_fts JOIN content c ON c.rowid = content_fts.rowid "
            "JOIN files f ON f.relpath = c.relpath "
            "WHERE content_fts MATCH ?"
        )
        order = "bm25(content_fts)"
    else:  # Internal programming error, never user-controlled.
        raise ValueError("unknown rank source: %r" % source)
    query_params = [match_expr] + params
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY %s, f.relpath COLLATE BINARY LIMIT ?" % order
    query_params.append(limit)
    return list(conn.execute(sql, query_params))


def query_name_ranks(conn, match_expr, args, limit):
    return _query_ranks(conn, match_expr, args, limit, "name")


def query_content_ranks(conn, match_expr, args, limit):
    return _query_ranks(conn, match_expr, args, limit, "content")


def _chunked(values, size=SEARCH_DETAIL_CHUNK_SIZE):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def fetch_file_details(conn, relpaths):
    """Fetch metadata only for the already-fused final result paths."""
    details = {}
    for chunk in _chunked(relpaths):
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT " + ", ".join(FILE_COLS)
            + " FROM files f WHERE f.relpath IN (" + placeholders + ")"
        )
        for row in conn.execute(sql, chunk):
            item = row_to_file(row)
            details[item["relpath"]] = item
    missing = [relpath for relpath in relpaths if relpath not in details]
    if missing:
        die("index is inconsistent; result metadata is missing for %r" % missing[0], code=2)
    return details


def fetch_content_snippets(conn, match_expr, relpaths):
    """Generate bounded snippets only for final content-matched results."""
    snippets = {}
    for chunk in _chunked(relpaths):
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT c.relpath, substr("
            "snippet(content_fts, 0, '[', ']', ' … ', 12), 1, ?) "
            "FROM content_fts JOIN content c ON c.rowid = content_fts.rowid "
            "WHERE content_fts MATCH ? AND c.relpath IN ("
            + placeholders
            + ")"
        )
        params = [SNIPPET_MAX_CHARS, match_expr] + list(chunk)
        for relpath, snippet in conn.execute(sql, params):
            snippets[relpath] = snippet
    missing = [relpath for relpath in relpaths if relpath not in snippets]
    if missing:
        die("index is inconsistent; content snippet is missing for %r" % missing[0], code=2)
    return snippets


def cmd_status(conn, args):
    base_columns = (
        "schema_version", "generated_at", "elapsed_seconds", "file_count",
        "content_count", "content_status_breakdown", "tool_version", "corpus_root",
    )
    v2_columns = (
        "corpus_uuid", "access_policy_id", "access_policy_generation",
        "policy_digest", "access_roots", "excluded_access_count",
    )
    available = {row[1] for row in conn.execute("PRAGMA table_info(meta)")}
    selected = base_columns + tuple(name for name in v2_columns if name in available)
    row = conn.execute("SELECT %s FROM meta" % ", ".join(selected)).fetchone()
    if row is None:
        die("meta table is empty; the index is not built", code=2)
    values = dict(zip(selected, row))
    result = {name: values[name] for name in base_columns if name != "content_status_breakdown"}
    result["content_status"] = json.loads(values["content_status_breakdown"])
    for name in v2_columns:
        if name in values:
            value = values[name]
            if name == "access_roots":
                value = json.loads(value)
            result[name] = value
    return result


def cmd_search(conn, args):
    match_expr = fts_match_expr(args.terms)
    # Exact RRF needs the complete ordinal ranks from both sources.  Load a
    # finite, explicit maximum and fail instead of silently returning an
    # approximate top-k when a broad query exceeds it.
    candidate_limit = RRF_MAX_CANDIDATES_PER_SOURCE + 1
    name_hits = query_name_ranks(conn, match_expr, args, candidate_limit)
    content_hits = query_content_ranks(conn, match_expr, args, candidate_limit)
    if (
        len(name_hits) > RRF_MAX_CANDIDATES_PER_SOURCE
        or len(content_hits) > RRF_MAX_CANDIDATES_PER_SOURCE
    ):
        die(
            "search exceeds the %d-candidate exact-ranking budget per source; "
            "add terms or filters" % RRF_MAX_CANDIDATES_PER_SOURCE,
            code=2,
        )

    # BM25 values from the two FTS tables are not on a shared scale.  Fuse
    # their ordinal rankings instead.  A document found by both indexes gains
    # both contributions and retains the content snippet.
    fused = {}
    for rank, hit in enumerate(name_hits, 1):
        relpath = hit[0]
        fused[relpath] = {
            "relpath": relpath,
            "matched_by": "name",
            "score": 1.0 / (RRF_K + rank),
        }

    for rank, hit in enumerate(content_hits, 1):
        relpath = hit[0]
        contribution = 1.0 / (RRF_K + rank)
        if relpath in fused:
            item = fused[relpath]
            item["matched_by"] = "both"
            item["score"] += contribution
        else:
            fused[relpath] = {
                "relpath": relpath,
                "matched_by": "content",
                "score": contribution,
            }

    # relpath supplies a stable cross-platform tie-breaker for equal RRF
    # scores; source queries also use it to make equal-BM25 ranks deterministic.
    ranked = sorted(fused.values(), key=lambda item: (-item["score"], item["relpath"]))
    selected = ranked[: args.limit]
    selected_paths = [item["relpath"] for item in selected]
    details = fetch_file_details(conn, selected_paths)
    content_paths = [
        item["relpath"] for item in selected
        if item["matched_by"] in ("content", "both")
    ]
    snippets = fetch_content_snippets(conn, match_expr, content_paths)
    result = []
    for fused_item in selected:
        item = dict(details[fused_item["relpath"]])
        item.update(fused_item)
        item["snippet"] = snippets.get(fused_item["relpath"])
        result.append(item)
    return result


def cmd_name(conn, args):
    match_expr = fts_match_expr(args.terms)
    return query_names(conn, match_expr, args, args.limit)


def cmd_sql(conn, args):
    # Read-only + immutable connection already blocks writes. execute() rejects
    # multiple statements. load_extension is never enabled. On top of that only
    # query statements are accepted (blocks ATTACH-style side effects).
    words = args.query.strip().split(None, 1)
    first_word = words[0].upper() if words else ""
    if first_word not in ("SELECT", "WITH", "EXPLAIN"):
        die("Only SELECT/WITH/EXPLAIN statements are accepted", code=2)
    if len(args.query.encode("utf-8")) > SQL_MAX_QUERY_BYTES:
        die("SQL query exceeds the %d-byte budget" % SQL_MAX_QUERY_BYTES, code=2)
    progress_api = None
    if hasattr(conn, "set_progress_handler"):
        progress_api = conn.set_progress_handler
    elif hasattr(conn, "progress"):
        progress_api = conn.progress
    if not all(
        hasattr(sqlite3, name)
        for name in ("SQLITE_LIMIT_LENGTH", "SQLITE_LIMIT_COLUMN")
    ) or not all(
        hasattr(conn, name) for name in ("setlimit", "getlimit")
    ) or progress_api is None:
        die(
            "the sql subcommand requires SQLite setlimit and progress-handler "
            "support so values and VM work are bounded before Python materializes them",
            code=3,
        )
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_SQLITE_LENGTH)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, SQL_MAX_COLUMNS)
    if (
        conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) > SQL_MAX_SQLITE_LENGTH
        or conn.getlimit(sqlite3.SQLITE_LIMIT_COLUMN) > SQL_MAX_COLUMNS
    ):
        die("SQLite provider did not apply the requested SQL limits", code=3)
    deadline = time.monotonic() + SQL_MAX_SECONDS
    progress_calls = [0]

    def sql_budget_exhausted():
        progress_calls[0] += 1
        steps = progress_calls[0] * SQL_PROGRESS_OPCODES
        return int(steps > SQL_MAX_VM_STEPS or time.monotonic() >= deadline)

    progress_api(sql_budget_exhausted, SQL_PROGRESS_OPCODES)
    try:
        try:
            cursor = conn.execute(args.query)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            result = []
            json_bytes = 2  # opening and closing brackets of the result array
            while True:
                row = cursor.fetchone()
                if row is None:
                    break
                if len(result) >= SQL_MAX_ROWS:
                    die(
                        "SQL result exceeds the %d-row output budget; refine the query "
                        "or add a LIMIT" % SQL_MAX_ROWS,
                        code=2,
                    )
                item = json_compatible(dict(zip(columns, row)))
                row_bytes = len(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                # Include a comma/newline allowance between rows.  The budget protects
                # content size; pretty-print whitespace in emit() is intentionally not
                # counted as user data.
                if result:
                    row_bytes += 1
                if json_bytes + row_bytes > SQL_MAX_JSON_BYTES:
                    die(
                        "SQL result exceeds the %d-byte JSON output budget; select "
                        "fewer or smaller values" % SQL_MAX_JSON_BYTES,
                        code=2,
                    )
                json_bytes += row_bytes
                result.append(item)
        except SystemExit:
            raise
        except Exception as exc:
            if progress_calls[0] * SQL_PROGRESS_OPCODES > SQL_MAX_VM_STEPS \
                    or time.monotonic() >= deadline:
                die(
                    "SQL execution exceeded the %d-step/%g-second VM budget"
                    % (SQL_MAX_VM_STEPS, SQL_MAX_SECONDS),
                    code=2,
                )
            die("SQL error: %s" % exc, code=2)
    finally:
        progress_api(None, 0)
    return result


def emit(result, tsv):
    result = json_compatible(result)
    if not tsv:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return
    rows = result if isinstance(result, list) else [result]
    if not rows:
        return
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    def safe_tsv_cell(value):
        rendered = str(value)
        rendered = "".join(
            " " if ord(char) < 32 or ord(char) == 127 else char
            for char in rendered
        )
        if isinstance(value, str) and rendered.startswith(("=", "+", "-", "@")):
            rendered = "'" + rendered
        return rendered

    sys.stdout.write("\t".join(safe_tsv_cell(column) for column in columns) + "\n")
    for row in rows:
        cells = []
        for key in columns:
            value = row.get(key)
            if value is None:
                cells.append("")
            elif isinstance(value, (dict, list)):
                cells.append(json.dumps(value, ensure_ascii=False))
            else:
                # TSV is often pasted into terminals or opened by a spreadsheet.
                # Strip every C0 control (not just tab/newline), DEL, and neutralize
                # formula-leading cells without changing the JSON contract.
                cells.append(safe_tsv_cell(value))
        sys.stdout.write("\t".join(cells) + "\n")


def default_db_path():
    """Locate the published DB relative to this script.

    Tried in order: (1) the parent directory, so placing the CLI and its
    ``aplidocs_common.py`` helper in <publish_dir>/cli/ finds the index in the
    parent; (2) next to the script itself. If neither exists, die with a
    message pointing at --db.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(os.path.dirname(here), PUBLISHED_DB_NAME),
        os.path.join(here, PUBLISHED_DB_NAME),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    die(
        "could not find %s near the CLI (looked in %s). Pass --db /full/path/%s."
        % (
            PUBLISHED_DB_NAME,
            " and ".join(os.path.dirname(c) for c in candidates),
            PUBLISHED_DB_NAME,
        ),
        code=2,
    )


def add_filter_args(sub):
    sub.add_argument("--area")
    sub.add_argument("--ext")
    sub.add_argument("--year", type=int, help="filter on path_year")
    sub.add_argument(
        "--metadata-only", action="store_true",
        help="only files indexed by name/path (content extraction skipped)",
    )
    sub.add_argument("--limit", type=positive_int, default=20)


def extract_global_flags(argv):
    """Pull --db/--tsv (and --db=VALUE) out of argv from any position.

    Kept out of argparse so the two channel-wide flags work equally whether an
    agent writes them before or after the subcommand, without argparse's
    subparser default-clobbering. Only exact '--db'/'--tsv' tokens match; note
    the corner case that a positional argument consisting of exactly one of
    those tokens (e.g. searching for the literal text "--tsv") is consumed as
    the flag.
    """
    db = None
    db_seen = False
    tsv = False
    rest = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--tsv":
            tsv = True
        elif token == "--db":
            if i + 1 >= len(argv):
                die("--db requires a value", code=2)
            if db_seen:
                die("--db may be supplied only once", code=2)
            db = argv[i + 1]
            if not db:
                die("--db requires a non-empty value", code=2)
            db_seen = True
            i += 1
        elif token.startswith("--db="):
            if db_seen:
                die("--db may be supplied only once", code=2)
            db = token[len("--db="):]
            if not db:
                die("--db requires a non-empty value", code=2)
            db_seen = True
        else:
            rest.append(token)
        i += 1
    return db, tsv, rest


def build_parser():
    parser = ApliDocsArgumentParser(
        description="Query the ApliDocs full-text index (read-only). "
        "Global flags --db PATH and --tsv may appear before or after the subcommand."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="index freshness (meta) + content_status breakdown")

    p_search = sub.add_parser("search", help="combined name + content FTS search")
    p_search.add_argument("terms")
    add_filter_args(p_search)

    p_name = sub.add_parser("name", help="name/path FTS only")
    p_name.add_argument("terms")
    add_filter_args(p_name)

    p_sql = sub.add_parser(
        "sql", help="bounded single read-only SELECT/WITH/EXPLAIN"
    )
    p_sql.add_argument("query")

    return parser


HANDLERS = {
    "status": cmd_status,
    "search": cmd_search,
    "name": cmd_name,
    "sql": cmd_sql,
}


def main(argv=None):
    try:
        if argv is None:
            argv = sys.argv[1:]
        db, tsv, rest = extract_global_flags(argv)
        args = build_parser().parse_args(rest)
        check_fts5()
        db_path = db if db else default_db_path()
        conn = open_readonly(db_path)
        try:
            result = HANDLERS[args.command](conn, args)
        finally:
            conn.close()
        emit(result, tsv)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        # Runtime failures (corrupt rows, filesystem races, output encoding,
        # and unexpected SQLite errors) must never expose a Python traceback.
        die("%s" % exc, code=2)


if __name__ == "__main__":
    sys.exit(main())

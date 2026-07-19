#!/usr/bin/env python3
"""Read-only CLI over the ApliDocs full-text index (SQLite + FTS5).

One file for every channel (SSH on the server via the venv python, or SMB with
a local python). Imports pysqlite3 when present, else the stdlib sqlite3;
either way FTS5 is required and verified at startup. The database is ALWAYS
opened read-only + immutable so a plain SELECT over SMB cannot try to create
lock or journal files next to the .sqlite.

Subcommands: status | search | name | sql. Output is JSON by default (--tsv
for tab-separated). Errors go to stderr with a nonzero exit. See README.md.
"""
import argparse
import json
import os
import pathlib
import sys

try:
    import pysqlite3.dbapi2 as sqlite3
except ImportError:
    import sqlite3


PUBLISHED_DB_NAME = "aplidocs-index.sqlite"

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


def check_fts5():
    """Fail fast with an actionable message if FTS5 is unavailable."""
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        probe.close()
    except Exception as exc:
        die(
            "SQLite FTS5 is not available in this Python (%s). On a Synology NAS "
            "run the CLI with the venv python that has pysqlite3-binary installed "
            "(see docs/SYNOLOGY.md); elsewhere use a Python whose sqlite3 has FTS5, "
            "or install pysqlite3-binary (pip install pysqlite3-binary)." % exc,
            code=3,
        )


def open_readonly(db_path):
    path = pathlib.Path(db_path)
    if not path.is_file():
        die("index database not found: %s" % db_path, code=2)
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except Exception as exc:
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
    sql += " ORDER BY bm25(files_fts) LIMIT ?"
    query_params.append(limit)
    hits = []
    for row in conn.execute(sql, query_params):
        item = row_to_file(row)
        item["matched_by"] = "name"
        item["snippet"] = None
        item["score"] = row[-1]
        hits.append(item)
    return hits


def query_content(conn, match_expr, args, limit):
    clauses, params = build_filters(args)
    sql = (
        "SELECT " + ", ".join(FILE_COLS)
        + ", snippet(content_fts, 0, '[', ']', ' … ', 12) AS snippet"
        + ", bm25(content_fts) AS score "
        "FROM content_fts JOIN content c ON c.rowid = content_fts.rowid "
        "JOIN files f ON f.relpath = c.relpath "
        "WHERE content_fts MATCH ?"
    )
    query_params = [match_expr] + params
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY bm25(content_fts) LIMIT ?"
    query_params.append(limit)
    hits = []
    for row in conn.execute(sql, query_params):
        item = row_to_file(row)
        item["matched_by"] = "content"
        item["snippet"] = row[-2]
        item["score"] = row[-1]
        hits.append(item)
    return hits


def cmd_status(conn, args):
    row = conn.execute(
        "SELECT schema_version, generated_at, elapsed_seconds, file_count, "
        "content_count, content_status_breakdown, tool_version, corpus_root "
        "FROM meta"
    ).fetchone()
    if row is None:
        die("meta table is empty; the index is not built", code=2)
    result = {
        "schema_version": row[0],
        "generated_at": row[1],
        "elapsed_seconds": row[2],
        "file_count": row[3],
        "content_count": row[4],
        "content_status": json.loads(row[5]),
        "tool_version": row[6],
        "corpus_root": row[7],
    }
    return result


def cmd_search(conn, args):
    match_expr = fts_match_expr(args.terms)
    name_hits = query_names(conn, match_expr, args, args.limit)
    content_hits = query_content(conn, match_expr, args, args.limit)
    content_by_rel = {hit["relpath"]: hit for hit in content_hits}

    merged = []
    seen = set()
    # Name hits first (ordered by files bm25); a hit present in both groups is
    # marked 'both' and carries the content snippet.
    for hit in name_hits:
        rel = hit["relpath"]
        seen.add(rel)
        if rel in content_by_rel:
            hit["matched_by"] = "both"
            hit["snippet"] = content_by_rel[rel]["snippet"]
        merged.append(hit)
    # Content-only hits next (ordered by content bm25).
    for hit in content_hits:
        if hit["relpath"] not in seen:
            merged.append(hit)
    return merged[: args.limit]


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
    try:
        cursor = conn.execute(args.query)
    except Exception as exc:
        die("SQL error: %s" % exc, code=2)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def emit(result, tsv):
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
    sys.stdout.write("\t".join(columns) + "\n")
    for row in rows:
        cells = []
        for key in columns:
            value = row.get(key)
            if value is None:
                cells.append("")
            elif isinstance(value, (dict, list)):
                cells.append(json.dumps(value, ensure_ascii=False))
            else:
                cells.append(str(value).replace("\t", " ").replace("\n", " "))
        sys.stdout.write("\t".join(cells) + "\n")


def default_db_path():
    """Locate the published DB relative to this script.

    Tried in order: (1) the parent directory, so dropping a copy of the CLI in
    <publish_dir>/cli/ finds <publish_dir>/aplidocs-index.sqlite; (2) next to
    the script itself. If neither exists, die with a message pointing at --db.
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
    sub.add_argument("--limit", type=int, default=20)


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
            db = argv[i + 1]
            i += 1
        elif token.startswith("--db="):
            db = token[len("--db="):]
        else:
            rest.append(token)
        i += 1
    return db, tsv, rest


def build_parser():
    parser = argparse.ArgumentParser(
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

    p_sql = sub.add_parser("sql", help="arbitrary single read-only SELECT")
    p_sql.add_argument("query")

    return parser


HANDLERS = {
    "status": cmd_status,
    "search": cmd_search,
    "name": cmd_name,
    "sql": cmd_sql,
}


def main(argv=None):
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


if __name__ == "__main__":
    sys.exit(main())

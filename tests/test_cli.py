import json
import pathlib
import sqlite3 as fixture_sqlite3
from types import SimpleNamespace

import pytest

import aplidocs_cli as cli


def _file_hit(relpath, matched_by, snippet=None, score=-1.0):
    return {
        "relpath": relpath,
        "area": "docs",
        "ext": "txt",
        "path_year": 2024,
        "name_meta": None,
        "size_bytes": 10,
        "mtime": 1,
        "metadata_only": 0,
        "content_status": "extracted",
        "matched_by": matched_by,
        "snippet": snippet,
        "score": score,
    }


@pytest.fixture
def index_db(tmp_path):
    # '?' is covered by the pure-path URI tests but cannot exist in a Windows
    # filename.  The real file still exercises Unicode, spaces, '%' and '#'.
    path = tmp_path / "índice con espacios % #.sqlite"
    # Build the fixture with the stdlib so provider-integration runs can query
    # it through cysqlite without depending on cysqlite's different transaction
    # convenience semantics.
    conn = fixture_sqlite3.connect(str(path))
    conn.executescript(
        """
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
        CREATE VIRTUAL TABLE files_fts USING fts5(
            filename, relpath, content='files', content_rowid='rowid'
        );
        CREATE VIRTUAL TABLE content_fts USING fts5(
            text, content='content', content_rowid='rowid'
        );
        """
    )
    files = [
        (
            "docs/z-both.txt", "needle both", "docs", "txt", 10, 1,
            2024, '{"kind":"both"}', 0, "extracted",
        ),
        (
            "docs/b-name.txt", "needle name", "docs", "txt", 10, 1,
            2024, None, 0, "no_text_type",
        ),
        (
            "docs/a-content.txt", "plain content", "docs", "txt", 10, 1,
            2024, None, 0, "extracted",
        ),
        (
            "selected/2023/needle-report.pdf", "needle report", "selected",
            "pdf", 10, 1, 2023, None, 1, "metadata_only",
        ),
    ]
    conn.executemany(
        "INSERT INTO files(relpath, filename, area, ext, size_bytes, mtime, "
        "path_year, name_meta, metadata_only, content_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        files,
    )
    conn.executemany(
        "INSERT INTO content(relpath, text, extractor, source_sig) VALUES (?, ?, ?, ?)",
        [
            ("docs/z-both.txt", "needle inside both", "text", "10:1"),
            ("docs/a-content.txt", "needle only in body", "text", "10:1"),
        ],
    )
    conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO content_fts(content_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT INTO meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "2026-07-21T00:00:00Z", 0.1, 4, 2, '{"extracted":2}',
         "aplidocs/test", "C:/corpus"),
    )
    conn.commit()
    conn.close()
    return path


def test_unc_uri_has_empty_authority_and_preserves_percent_encoding():
    path = pathlib.PureWindowsPath(
        r"\\server\share\carpeta con espacio\índice%#?.sqlite"
    )
    uri = cli._absolute_path_to_sqlite_uri(path)
    assert uri == (
        "file:////server/share/carpeta%20con%20espacio/"
        "%C3%ADndice%25%23%3F.sqlite?mode=ro&immutable=1"
    )
    assert "file://server/" not in uri


def test_drive_uri_is_not_rewritten():
    path = pathlib.PureWindowsPath(r"Z:\carpeta con espacio\índice%#?.sqlite")
    assert cli._absolute_path_to_sqlite_uri(path) == (
        "file:///Z:/carpeta%20con%20espacio/"
        "%C3%ADndice%25%23%3F.sqlite?mode=ro&immutable=1"
    )


def test_sqlite_uri_does_not_resolve_paths(index_db, monkeypatch):
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("resolve() must not be called")

    monkeypatch.setattr(pathlib.Path, "resolve", forbidden_resolve)
    uri = cli.sqlite_readonly_uri(index_db)
    assert "%20" in uri
    assert "%25" in uri
    assert "%23" in uri
    assert uri.endswith("?mode=ro&immutable=1")


def test_open_readonly_can_query_special_character_path(index_db):
    conn = cli.open_readonly(index_db)
    try:
        assert conn.execute("SELECT file_count FROM meta").fetchone() == (4,)
        with pytest.raises(cli.sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden(x)")
    finally:
        conn.close()


def test_status_exposes_v2_corpus_and_access_policy_metadata():
    conn = cli.sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE meta(schema_version, generated_at, elapsed_seconds, file_count, "
        "content_count, content_status_breakdown, tool_version, corpus_root, corpus_uuid, "
        "access_policy_id, access_policy_generation, policy_digest, access_roots, "
        "excluded_access_count)"
    )
    conn.execute(
        "INSERT INTO meta VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            2, "2026-07-21T00:00:00Z", 1.0, 7, 5, '{"extracted":5}',
            "aplidocs/2.0", "/volume1/docs", "11111111-1111-1111-1111-111111111111",
            "finance-readers", "3", "abc123", '["finance","shared"]', 2,
        ),
    )
    result = cli.cmd_status(conn, SimpleNamespace())
    assert result["schema_version"] == 2
    assert result["corpus_uuid"].startswith("11111111-")
    assert result["access_policy_id"] == "finance-readers"
    assert result["access_policy_generation"] == "3"
    assert result["access_roots"] == ["finance", "shared"]
    assert result["excluded_access_count"] == 2
    conn.close()


def test_open_readonly_path_error_is_uniform(monkeypatch, capsys):
    class BrokenPath(object):
        def __init__(self, value):
            self.value = value

        def is_file(self):
            raise PermissionError("access denied")

    monkeypatch.setattr(cli, "pathlib", SimpleNamespace(Path=BrokenPath))
    with pytest.raises(SystemExit) as raised:
        cli.open_readonly(r"\\server\share\index.sqlite")
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert stderr.startswith("aplidocs: cannot open index database")
    assert "access denied" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("value", ["0", "-1", "-999"])
def test_limit_must_be_strictly_positive(value, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["search", "needle", "--limit", value])
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert stderr.startswith("aplidocs:")
    assert "between 1" in stderr
    assert "Traceback" not in stderr


def test_positive_limit_is_accepted():
    args = cli.build_parser().parse_args(["name", "needle", "--limit", "1"])
    assert args.limit == 1


def test_duplicate_database_flag_is_rejected(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.extract_global_flags(["--db", "cohort-a.sqlite", "--db=cohort-b.sqlite", "status"])
    assert raised.value.code == 2
    assert "only once" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--db=", "status"], ["--db", "", "status"]])
def test_empty_database_flag_is_rejected(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.extract_global_flags(argv)
    assert raised.value.code == 2
    assert "non-empty" in capsys.readouterr().err


def test_limit_has_a_finite_upper_bound(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(
            ["search", "needle", "--limit", str(cli.SEARCH_MAX_LIMIT + 1)]
        )
    assert raised.value.code == 2
    assert str(cli.SEARCH_MAX_LIMIT) in capsys.readouterr().err


def test_emit_serializes_nested_blob_as_explicit_base64(capsys):
    cli.emit(
        [{"blob": b"\x00\xff", "nested": [bytearray(b"ok"), memoryview(b"x")]}],
        tsv=False,
    )
    result = json.loads(capsys.readouterr().out)
    assert result[0]["blob"] == {"$blob": "AP8=", "encoding": "base64"}
    assert result[0]["nested"] == [
        {"$blob": "b2s=", "encoding": "base64"},
        {"$blob": "eA==", "encoding": "base64"},
    ]


def test_emit_tsv_serializes_blob_marker(capsys):
    cli.emit([{"blob": b"\x00\xff"}], tsv=True)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "blob"
    assert json.loads(lines[1]) == {"$blob": "AP8=", "encoding": "base64"}


def test_emit_tsv_neutralizes_controls_and_spreadsheet_formulas(capsys):
    cli.emit(
        [{
            "=formula\n": "=2+2",
            "control": "left\r\x1b\x00right",
            "number": -1,
            "safe": "text",
        }],
        tsv=True,
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t") == ["'=formula ", "control", "number", "safe"]
    assert lines[1].split("\t") == ["'=2+2", "left   right", "-1", "text"]


def test_cmd_sql_serializes_blob_without_fetchall(index_db):
    conn = cli.sqlite3.connect(str(index_db))
    try:
        result = cli.cmd_sql(
            conn, SimpleNamespace(query="SELECT x'00FF' AS payload")
        )
    finally:
        conn.close()
    assert result == [{"payload": {"$blob": "AP8=", "encoding": "base64"}}]


def test_cmd_sql_tags_non_finite_real_as_valid_json(index_db, capsys):
    conn = cli.sqlite3.connect(str(index_db))
    try:
        result = cli.cmd_sql(conn, SimpleNamespace(query="SELECT 1e999 AS value"))
    finally:
        conn.close()
    assert result == [{"value": {"$float": "infinity"}}]
    cli.emit(result, tsv=False)
    assert json.loads(capsys.readouterr().out) == result


def test_cmd_sql_reads_at_most_one_row_past_row_budget(monkeypatch, capsys):
    class Cursor(object):
        description = (("n", None, None, None, None, None, None),)

        def __init__(self):
            self.rows = iter([(1,), (2,), (3,), (4,)])
            self.fetchone_calls = 0

        def fetchone(self):
            self.fetchone_calls += 1
            return next(self.rows, None)

        def fetchall(self):
            raise AssertionError("fetchall() must never be used")

    class Connection(object):
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, query):
            return self.cursor

        def setlimit(self, _category, value):
            return value

        def getlimit(self, category):
            if category == cli.sqlite3.SQLITE_LIMIT_COLUMN:
                return cli.SQL_MAX_COLUMNS
            return cli.SQL_MAX_SQLITE_LENGTH

        def set_progress_handler(self, _handler, _opcodes):
            pass

    cursor = Cursor()
    monkeypatch.setattr(cli, "SQL_MAX_ROWS", 2)
    with pytest.raises(SystemExit) as raised:
        cli.cmd_sql(Connection(cursor), SimpleNamespace(query="SELECT n FROM t"))
    assert raised.value.code == 2
    assert cursor.fetchone_calls == 3
    stderr = capsys.readouterr().err
    assert "2-row output budget" in stderr
    assert "Traceback" not in stderr


def test_cmd_sql_enforces_json_byte_budget(index_db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SQL_MAX_JSON_BYTES", 32)
    conn = cli.sqlite3.connect(str(index_db))
    try:
        with pytest.raises(SystemExit) as raised:
            cli.cmd_sql(
                conn,
                SimpleNamespace(query="SELECT zeroblob(128) AS payload"),
            )
    finally:
        conn.close()
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "32-byte JSON output budget" in stderr
    assert "Traceback" not in stderr


def test_cmd_sql_bounds_blob_inside_sqlite_before_python_materializes_it(
    index_db, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "SQL_MAX_SQLITE_LENGTH", 1024)
    conn = cli.sqlite3.connect(str(index_db))
    try:
        with pytest.raises(SystemExit) as raised:
            cli.cmd_sql(
                conn, SimpleNamespace(query="SELECT zeroblob(2048) AS payload")
            )
    finally:
        conn.close()
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "SQL error" in stderr
    assert "too big" in stderr.lower()


def test_cmd_sql_interrupts_unbounded_vm_work(index_db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SQL_MAX_VM_STEPS", 2000)
    monkeypatch.setattr(cli, "SQL_MAX_SECONDS", 10.0)
    conn = cli.sqlite3.connect(str(index_db))
    query = (
        "WITH RECURSIVE t(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM t) "
        "SELECT max(x) FROM t"
    )
    try:
        with pytest.raises(SystemExit) as raised:
            cli.cmd_sql(conn, SimpleNamespace(query=query))
    finally:
        conn.close()
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "VM budget" in stderr


def test_search_uses_deterministic_rrf_and_preserves_match_details(monkeypatch):
    name_hits = [
        _file_hit("docs/b-name.txt", "name", score=-100.0),
        _file_hit("docs/z-both.txt", "name", score=-0.1),
    ]
    content_hits = [
        _file_hit("docs/a-content.txt", "content", "[needle] content", -50.0),
        _file_hit("docs/z-both.txt", "content", "[needle] both", -0.2),
    ]
    limits = []

    def fake_names(conn, match_expr, args, limit):
        limits.append(limit)
        return [(item["relpath"], item["score"]) for item in name_hits]

    def fake_content(conn, match_expr, args, limit):
        limits.append(limit)
        return [(item["relpath"], item["score"]) for item in content_hits]

    all_hits = {item["relpath"]: item for item in name_hits + content_hits}
    monkeypatch.setattr(cli, "query_name_ranks", fake_names)
    monkeypatch.setattr(cli, "query_content_ranks", fake_content)
    monkeypatch.setattr(
        cli,
        "fetch_file_details",
        lambda _conn, relpaths: {path: all_hits[path] for path in relpaths},
    )
    monkeypatch.setattr(
        cli,
        "fetch_content_snippets",
        lambda _conn, _expr, relpaths: {
            path: all_hits[path]["snippet"] for path in relpaths
        },
    )
    args = SimpleNamespace(terms="needle", limit=3)
    result = cli.cmd_search(object(), args)

    assert limits == [
        cli.RRF_MAX_CANDIDATES_PER_SOURCE + 1,
        cli.RRF_MAX_CANDIDATES_PER_SOURCE + 1,
    ]
    assert [item["relpath"] for item in result] == [
        "docs/z-both.txt",
        "docs/a-content.txt",
        "docs/b-name.txt",
    ]
    assert result[0]["matched_by"] == "both"
    assert result[0]["snippet"] == "[needle] both"
    assert result[0]["score"] == pytest.approx(2.0 / (cli.RRF_K + 2))
    assert result[1]["matched_by"] == "content"
    assert result[1]["snippet"] == "[needle] content"
    assert result[2]["matched_by"] == "name"
    assert result[2]["snippet"] is None
    # The fuser copies its inputs instead of corrupting either source result.
    assert name_hits[1]["matched_by"] == "name"
    assert name_hits[1]["score"] == -0.1


def test_search_considers_a_document_ranked_sixth_in_both_sources(monkeypatch):
    name_hits = [
        _file_hit("name-%d.txt" % rank, "name", score=-rank)
        for rank in range(1, 6)
    ] + [_file_hit("both-sixth.txt", "name", score=-6)]
    content_hits = [
        _file_hit("content-%d.txt" % rank, "content", "snippet", -rank)
        for rank in range(1, 6)
    ] + [_file_hit("both-sixth.txt", "content", "both", -6)]
    all_hits = {item["relpath"]: item for item in name_hits + content_hits}
    monkeypatch.setattr(
        cli,
        "query_name_ranks",
        lambda *_args: [(item["relpath"], item["score"]) for item in name_hits],
    )
    monkeypatch.setattr(
        cli,
        "query_content_ranks",
        lambda *_args: [(item["relpath"], item["score"]) for item in content_hits],
    )
    monkeypatch.setattr(
        cli,
        "fetch_file_details",
        lambda _conn, relpaths: {path: all_hits[path] for path in relpaths},
    )
    monkeypatch.setattr(
        cli,
        "fetch_content_snippets",
        lambda _conn, _expr, relpaths: {path: "both" for path in relpaths},
    )

    result = cli.cmd_search(
        object(), SimpleNamespace(terms="needle", limit=1)
    )

    assert [item["relpath"] for item in result] == ["both-sixth.txt"]
    assert result[0]["matched_by"] == "both"
    assert result[0]["score"] == pytest.approx(2.0 / (cli.RRF_K + 6))


def test_search_defers_metadata_and_snippets_until_after_final_ranking(monkeypatch):
    name_ranks = [("name-%05d.txt" % rank, -rank) for rank in range(1000)]
    content_ranks = [("content-%05d.txt" % rank, -rank) for rank in range(1000)]
    fetched = {"details": None, "snippets": None}

    monkeypatch.setattr(cli, "query_name_ranks", lambda *_args: name_ranks)
    monkeypatch.setattr(cli, "query_content_ranks", lambda *_args: content_ranks)

    def fake_details(_conn, relpaths):
        fetched["details"] = list(relpaths)
        return {path: _file_hit(path, "name") for path in relpaths}

    def fake_snippets(_conn, _expr, relpaths):
        fetched["snippets"] = list(relpaths)
        return {path: "[needle]" for path in relpaths}

    monkeypatch.setattr(cli, "fetch_file_details", fake_details)
    monkeypatch.setattr(cli, "fetch_content_snippets", fake_snippets)
    result = cli.cmd_search(object(), SimpleNamespace(terms="needle", limit=1))

    assert len(result) == 1
    assert fetched["details"] == [result[0]["relpath"]]
    assert len(fetched["snippets"]) <= 1


def test_search_filters_survive_combined_ranking(index_db):
    conn = cli.sqlite3.connect(str(index_db))
    args = SimpleNamespace(
        terms="needle",
        limit=10,
        area="selected",
        ext=".PDF",
        year=2023,
        metadata_only=True,
    )
    try:
        result = cli.cmd_search(conn, args)
    finally:
        conn.close()
    assert [item["relpath"] for item in result] == [
        "selected/2023/needle-report.pdf"
    ]
    assert result[0]["matched_by"] == "name"
    assert result[0]["snippet"] is None


@pytest.mark.parametrize(
    "filter_values",
    [
        {"area": "selected"},
        {"ext": ".PDF"},
        {"year": 2023},
        {"metadata_only": True},
    ],
)
def test_each_search_filter_is_applied_to_both_rank_sources(index_db, filter_values):
    values = {
        "terms": "needle",
        "limit": 10,
        "area": None,
        "ext": None,
        "year": None,
        "metadata_only": False,
    }
    values.update(filter_values)
    conn = cli.sqlite3.connect(str(index_db))
    try:
        result = cli.cmd_search(conn, SimpleNamespace(**values))
    finally:
        conn.close()
    assert [item["relpath"] for item in result] == [
        "selected/2023/needle-report.pdf"
    ]


def test_combined_search_against_real_fts_marks_both_and_content(index_db):
    conn = cli.sqlite3.connect(str(index_db))
    args = SimpleNamespace(
        terms="needle", limit=10, area="docs", ext=None, year=None,
        metadata_only=False,
    )
    try:
        first = cli.cmd_search(conn, args)
        second = cli.cmd_search(conn, args)
    finally:
        conn.close()

    assert first == second
    assert first[0]["relpath"] == "docs/z-both.txt"
    assert first[0]["matched_by"] == "both"
    assert "[needle]" in first[0]["snippet"]
    by_relpath = {item["relpath"]: item for item in first}
    assert by_relpath["docs/a-content.txt"]["matched_by"] == "content"
    assert by_relpath["docs/a-content.txt"]["snippet"] is not None
    assert by_relpath["docs/b-name.txt"]["matched_by"] == "name"
    assert by_relpath["docs/b-name.txt"]["snippet"] is None


def test_equal_bm25_name_hits_are_ordered_by_relpath(index_db):
    conn = cli.sqlite3.connect(str(index_db))
    args = SimpleNamespace(area="docs", ext=None, year=2024, metadata_only=False)
    try:
        hits = cli.query_names(conn, '"needle"', args, 10)
    finally:
        conn.close()
    # The two filenames have equivalent token counts and term frequency.  The
    # SQL relpath tie-breaker makes repeated runs stable.
    assert [hit["relpath"] for hit in hits] == [
        "docs/b-name.txt",
        "docs/z-both.txt",
    ]


def test_main_end_to_end_status_and_sql_blob(index_db, capsys, monkeypatch):
    monkeypatch.setattr(
        cli, "require_safe_sqlite_provider", lambda _provider: (3, 53, 3)
    )
    assert cli.main(["status", "--db", str(index_db)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["file_count"] == 4

    assert cli.main(["--db", str(index_db), "sql", "SELECT x'00FF' AS b"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"b": {"$blob": "AP8=", "encoding": "base64"}}]


def test_main_converts_corrupt_index_error_to_cli_error(
    index_db, capsys, monkeypatch
):
    monkeypatch.setattr(
        cli, "require_safe_sqlite_provider", lambda _provider: (3, 53, 3)
    )
    conn = fixture_sqlite3.connect(str(index_db))
    conn.execute("UPDATE meta SET content_status_breakdown = 'not json'")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as raised:
        cli.main(["status", "--db", str(index_db)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("aplidocs:")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("version", [(3, 51, 1), (3, 53, 2)])
def test_fts5_preflight_rejects_vulnerable_sqlite(
    capsys, monkeypatch, version
):
    monkeypatch.setattr(
        cli, "_SQLITE_PROVIDER_CANDIDATES", (("selected", cli.sqlite3),)
    )
    monkeypatch.setattr(cli, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(cli.sqlite3, "sqlite_version_info", version)
    with pytest.raises(SystemExit) as raised:
        cli.check_fts5()
    assert raised.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SQLite >= 3.53.3" in captured.err
    assert ".".join(str(part) for part in version) in captured.err


def test_fts5_preflight_falls_back_from_residual_unsafe_provider(monkeypatch):
    opened = []
    stale = SimpleNamespace(
        sqlite_version_info=(3, 51, 1),
        sqlite_version="3.51.1",
        connect=lambda *_args, **_kwargs: opened.append("stale"),
    )
    safe = SimpleNamespace(
        sqlite_version_info=(3, 53, 3),
        sqlite_version="3.53.3",
        connect=fixture_sqlite3.connect,
    )
    monkeypatch.setattr(
        cli,
        "_SQLITE_PROVIDER_CANDIDATES",
        (("residual pysqlite3", stale), ("safe stdlib", safe)),
    )
    monkeypatch.setattr(cli, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(cli, "sqlite3", stale)

    cli.check_fts5()
    assert cli.sqlite3 is safe
    assert opened == []

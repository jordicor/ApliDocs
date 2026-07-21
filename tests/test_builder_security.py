"""Security and integrity regressions for the ApliDocs index builder.

The builder itself deliberately requires POSIX descriptor APIs.  Configuration,
schema, cache and verification logic are nevertheless exercised on every
platform; tests that need real ``openat``/FIFO/symlink semantics are explicitly
POSIX-only so the Windows CLI test job remains useful.
"""

import codecs
import errno
import json
import os
import pathlib
import stat
import subprocess
import sys
import types
import unicodedata
import unittest
import uuid

import pytest

import build_index

# Exercise the same SQLite provider selected by production (pysqlite3 on DSM
# when the stdlib build lacks FTS5).
sqlite3 = build_index.sqlite3


POSIX_DESCRIPTOR_APIS = (
    os.name == "posix"
    and all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
    and build_index.fcntl is not None
)


def _config(tmp_path, **updates):
    corpus = tmp_path / "corpus"
    published = tmp_path / "published"
    corpus.mkdir(exist_ok=True)
    published.mkdir(exist_ok=True)
    values = {
        "corpus_root": str(corpus),
        "publish_dir": str(published),
        "corpus_id_file": ".aplidocs-corpus-id",
        "access_roots": ["."],
        "access_policy_id": "security-tests",
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
    values.update(updates)
    return values


def _builder(tmp_path, **updates):
    config = _config(tmp_path, **updates)
    output = tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME
    return build_index.IndexBuilder(config, str(output))


def _load_json_config(tmp_path, values, bom=False):
    path = tmp_path / "config.json"
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + payload)
    return build_index.load_config(str(path))


def _insert_file(conn, relpath, relpath_nfc=None, status=None, source_hash=None):
    filename = relpath.rsplit("/", 1)[-1]
    if relpath_nfc is None:
        relpath_nfc = unicodedata.normalize("NFC", relpath)
    filename_nfc = unicodedata.normalize("NFC", filename)
    if status is None:
        status = build_index.ST_NO_TEXT
    conn.execute(
        "INSERT INTO files(relpath, relpath_nfc, filename, filename_nfc, area, ext, "
        "size_bytes, mtime, mtime_ns, ctime_ns, st_dev, st_ino, source_hash, "
        "path_year, name_meta, metadata_only, content_present, content_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            relpath,
            relpath_nfc,
            filename,
            filename_nfc,
            build_index.derive_area(relpath),
            build_index.file_extension(filename_nfc),
            1,
            1,
            1,
            1,
            1,
            1,
            source_hash,
            None,
            None,
            0,
            1 if status == build_index.ST_EXTRACTED else 0,
            status,
        ),
    )


def _insert_meta(builder, file_count):
    builder.conn.execute(
        "INSERT INTO meta(schema_version, generated_at, elapsed_seconds, file_count, "
        "content_count, content_status_breakdown, tool_version, corpus_root, corpus_uuid, "
        "access_policy_id, access_policy_generation, policy_digest, access_roots, "
        "excluded_access_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            build_index.SCHEMA_VERSION,
            "2026-07-21T00:00:00Z",
            0.01,
            file_count,
            0,
            json.dumps({build_index.ST_NO_TEXT: file_count}),
            build_index.TOOL_VERSION,
            builder.corpus_root,
            builder.corpus_uuid,
            builder.access_policy_id,
            builder.access_policy_generation,
            builder.policy_digest,
            json.dumps(builder.access_roots),
            builder.excluded_access_count,
        ),
    )
    builder.conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    builder.conn.execute("INSERT INTO content_fts(content_fts) VALUES('rebuild')")
    builder.conn.commit()


def _verification_builder(tmp_path, monkeypatch, relpaths, **updates):
    builder = _builder(tmp_path, **updates)
    builder.conn = sqlite3.connect(":memory:")
    builder.create_schema()
    builder.corpus_uuid = str(uuid.uuid4())
    builder._corpus_marker_sig = (1, 2, 3, 4, 5, 6)
    for relpath in relpaths:
        _insert_file(builder.conn, relpath)
    builder.walk_count = len(relpaths)
    _insert_meta(builder, len(relpaths))

    # ``verify`` closes this descriptor itself.  Using a real ordinary file
    # keeps that cleanup assertion meaningful while avoiding directory-FD APIs
    # that Windows intentionally does not provide.
    dummy = tmp_path / "marker-fd-placeholder"
    dummy.write_bytes(b"x")
    monkeypatch.setattr(builder, "_open_corpus_root", lambda: os.open(str(dummy), os.O_RDONLY))
    monkeypatch.setattr(
        builder,
        "_read_corpus_marker",
        lambda _fd: (builder.corpus_uuid, builder._corpus_marker_sig),
    )
    return builder


def _write_previous_index(builder, corpus_uuid=None, with_required_tables=True):
    path = os.path.join(builder.publish_dir, build_index.PUBLISHED_DB_NAME)
    conn = sqlite3.connect(path)
    if with_required_tables:
        builder.conn = conn
        builder.create_schema()
    else:
        conn.execute(
            "CREATE TABLE meta(schema_version, tool_version, corpus_uuid, "
            "access_policy_id, access_policy_generation, policy_digest, file_count)"
        )
    marker = corpus_uuid if corpus_uuid is not None else builder.corpus_uuid
    if with_required_tables:
        _insert_file(
            conn,
            "docs/cached.txt",
            status=build_index.ST_EXTRACTED,
            source_hash="sha256:current",
        )
        conn.execute(
            "INSERT INTO content(relpath, text, extractor, source_sig) VALUES(?,?,?,?)",
            ("docs/cached.txt", "cached text", "text", "sha256:current"),
        )
        _insert_meta(builder, 1)
        builder.conn = None
    else:
        conn.execute(
            "INSERT INTO meta VALUES(?,?,?,?,?,?,?)",
            (
                build_index.SCHEMA_VERSION,
                build_index.TOOL_VERSION,
                marker,
                builder.access_policy_id,
                builder.access_policy_generation,
                builder.policy_digest,
                1,
            ),
        )
        conn.commit()
    conn.close()
    if os.name == "posix":
        os.chmod(path, builder.publish_mode)
    return path


def test_load_config_accepts_utf8_bom_and_boolean_setting(tmp_path):
    loaded = _load_json_config(tmp_path, _config(tmp_path, allow_empty=True), bom=True)
    assert loaded["allow_empty"] is True
    assert loaded["publish_mode_int"] == 0o640


def test_load_config_rejects_json_escaped_lone_surrogate(tmp_path, capsys):
    values = _config(tmp_path)
    values["access_policy_id"] = "bad\udcff"
    path = tmp_path / "surrogate-config.json"
    path.write_bytes(json.dumps(values, ensure_ascii=True).encode("ascii"))
    with pytest.raises(SystemExit) as raised:
        build_index.load_config(str(path))
    assert raised.value.code == 2
    assert "Unicode surrogate" in capsys.readouterr().out


@pytest.mark.parametrize(
    "generation",
    [
        "01",
        "-1",
        "v2",
        "1.0",
        "+2",
        "9223372036854775808",
        pytest.param("9" * 5000, id="huge-decimal"),
    ],
)
def test_load_config_requires_canonical_monotonic_policy_generation(
    tmp_path, capsys, generation
):
    values = _config(tmp_path, access_policy_generation=generation)
    with pytest.raises(SystemExit) as raised:
        _load_json_config(tmp_path, values)
    assert raised.value.code == 2
    assert "canonical non-negative decimal" in capsys.readouterr().out


def test_fts5_preflight_reports_current_provider_guidance(monkeypatch):
    monkeypatch.setattr(
        build_index,
        "_SQLITE_PROVIDER_CANDIDATES",
        (("selected", build_index.sqlite3),),
    )
    monkeypatch.setattr(build_index, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(
        build_index.sqlite3, "sqlite_version_info", (3, 53, 3)
    )
    monkeypatch.setattr(
        build_index.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            build_index.sqlite3.OperationalError("no such module: fts5")
        ),
    )
    monkeypatch.setattr(build_index.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(build_index.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="vendor, container or self-built"):
        build_index.ensure_fts5_provider()


@pytest.mark.parametrize("version", [(3, 51, 1), (3, 53, 2)])
def test_fts5_preflight_rejects_vulnerable_sqlite_before_probe(
    monkeypatch, version
):
    opened = []
    monkeypatch.setattr(
        build_index,
        "_SQLITE_PROVIDER_CANDIDATES",
        (("selected", build_index.sqlite3),),
    )
    monkeypatch.setattr(build_index, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(
        build_index.sqlite3, "sqlite_version_info", version
    )
    monkeypatch.setattr(
        build_index.sqlite3, "connect", lambda *_args, **_kwargs: opened.append(True)
    )
    rendered = r"\.".join(str(part) for part in version)
    with pytest.raises(RuntimeError, match=r"SQLite >= 3\.53\.3.*" + rendered):
        build_index.ensure_fts5_provider()
    assert opened == []


def test_fts5_preflight_falls_back_from_residual_unsafe_provider(monkeypatch):
    opened = []
    stale = types.SimpleNamespace(
        sqlite_version_info=(3, 51, 1),
        sqlite_version="3.51.1",
        connect=lambda *_args, **_kwargs: opened.append("stale"),
    )
    safe = types.SimpleNamespace(
        sqlite_version_info=(3, 53, 3),
        sqlite_version="3.53.3",
        connect=sqlite3.connect,
    )
    monkeypatch.setattr(
        build_index,
        "_SQLITE_PROVIDER_CANDIDATES",
        (("residual pysqlite3", stale), ("safe stdlib", safe)),
    )
    monkeypatch.setattr(build_index, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(build_index, "sqlite3", stale)

    assert build_index.ensure_fts5_provider() == (3, 53, 3)
    assert build_index.sqlite3 is safe
    assert opened == []


def test_fts5_preflight_falls_back_from_provider_without_fts5(monkeypatch):
    class NoFtsConnection:
        def execute(self, _statement):
            raise RuntimeError("no such module: fts5")

        def close(self):
            return None

    no_fts = types.SimpleNamespace(
        sqlite_version_info=(3, 53, 3),
        sqlite_version="3.53.3",
        connect=lambda *_args, **_kwargs: NoFtsConnection(),
    )
    safe = types.SimpleNamespace(
        sqlite_version_info=(3, 53, 3),
        sqlite_version="3.53.3",
        connect=sqlite3.connect,
    )
    monkeypatch.setattr(
        build_index,
        "_SQLITE_PROVIDER_CANDIDATES",
        (("no FTS5", no_fts), ("safe stdlib", safe)),
    )
    monkeypatch.setattr(build_index, "_SQLITE_IMPORT_FAILURES", ())
    monkeypatch.setattr(build_index, "sqlite3", no_fts)

    assert build_index.ensure_fts5_provider() == (3, 53, 3)
    assert build_index.sqlite3 is safe


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", "1"),
        ("corpus_uuid", 7),
        ("corpus_uuid", []),
        ("access_policy_id", 7),
        ("access_policy_generation", True),
        ("policy_digest", 7),
        ("publish_mode", 640),
        ("publish_mode", "0660"),
        ("publish_gid", True),
        ("publish_gid", -1),
        ("transition_from", {}),
    ],
)
def test_publication_binding_rejects_malformed_field_types(field, value):
    payload = {
        "version": build_index.PUBLICATION_BINDING_VERSION,
        "corpus_uuid": str(uuid.uuid4()),
        "access_policy_id": "cohort-a",
        "access_policy_generation": "1",
        "policy_digest": "a" * 64,
        "publish_mode": "0640",
        "publish_gid": 0,
        "transition_from": None,
    }
    payload[field] = value
    with pytest.raises(RuntimeError, match="invalid publication cohort marker"):
        build_index.IndexBuilder._parse_publication_binding(
            json.dumps(payload).encode("utf-8")
        )


@unittest.skipUnless(os.name == "posix", "requires POSIX interval timers")
def test_catastrophic_filename_regex_is_interrupted_in_isolated_process(tmp_path):
    code = """
import re
import build_index
pattern = re.compile(r'^(?P<name>(a+)+)$')
try:
    build_index.derive_name_meta('a' * 30 + 'b', pattern)
except build_index.FilenamePatternTimeout:
    raise SystemExit(0)
raise SystemExit(1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(pathlib.Path(build_index.__file__).parent),
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_file_count", True),
        ("max_file_count_drop_fraction", False),
        ("max_walk_retries", True),
        ("max_directory_entries", True),
        ("max_directory_depth", False),
        ("max_total_entries", True),
        ("max_total_files", False),
        ("max_total_extracted_mb", True),
        ("max_index_size_mb", False),
        ("pdf_memory_limit_mb", True),
        ("min_file_count", -1),
        ("max_file_count_drop_fraction", 1.01),
        ("max_walk_retries", 11),
        ("max_directory_entries", 0),
        ("max_directory_depth", 513),
        ("max_total_entries", 0),
        ("max_total_files", 10000001),
        ("max_total_extracted_mb", 0),
        ("max_index_size_mb", 1048577),
        ("pdf_memory_limit_mb", 127),
    ],
)
def test_load_config_rejects_boolean_or_out_of_range_numeric_limits(
    tmp_path, capsys, key, value
):
    values = _config(tmp_path)
    values[key] = value
    with pytest.raises(SystemExit) as raised:
        _load_json_config(tmp_path, values)
    assert raised.value.code == 2
    assert "CONFIG ERROR" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["0666", "0440", "0640 ", "888", 640])
def test_load_config_rejects_unsafe_or_malformed_publish_mode(
    tmp_path, capsys, mode
):
    with pytest.raises(SystemExit) as raised:
        _load_json_config(tmp_path, _config(tmp_path, publish_mode=mode))
    assert raised.value.code == 2
    assert "publish_mode" in capsys.readouterr().out


def test_load_config_normalizes_content_selectors_but_preserves_access_identity(tmp_path):
    nfd = unicodedata.normalize("NFD", "ésecret")
    loaded = _load_json_config(
        tmp_path,
        _config(
            tmp_path,
            prune_dirs=[nfd],
            metadata_only_globs=["docs/%s/**" % nfd],
            access_roots=["docs/%s" % nfd],
        ),
    )
    assert loaded["prune_dirs"] == ["ésecret"]
    assert loaded["metadata_only_globs"] == ["docs/ésecret/**"]
    assert loaded["access_roots"] == ["docs/%s" % nfd]
    builder = build_index.IndexBuilder(
        loaded, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    opposite_form = unicodedata.normalize("NFD", "docs/ésecret/file.txt")
    assert builder.is_metadata_only(opposite_form) is True


@pytest.mark.parametrize(
    "key,value",
    [
        ("prune_dirs", [r"private\\nested"]),
        ("prune_dirs", [" private"]),
        ("metadata_only_globs", [r"private\\**"]),
        ("metadata_only_globs", [" private/**"]),
        ("metadata_only_globs", ["/private/**"]),
        ("metadata_only_globs", ["./private/**"]),
        ("metadata_only_globs", ["../private/**"]),
        ("metadata_only_globs", ["private//**"]),
        ("access_roots", [r"docs\\legal"]),
        ("access_roots", ["docs/./legal"]),
        ("access_roots", ["../outside"]),
        ("access_roots", [".", "docs"]),
        ("prune_dirs", ["private", "Private"]),
    ],
)
def test_load_config_rejects_ambiguous_or_fail_open_selectors(
    tmp_path, capsys, key, value
):
    values = _config(tmp_path)
    values[key] = value
    with pytest.raises(SystemExit) as raised:
        _load_json_config(tmp_path, values)
    assert raised.value.code == 2
    assert "CONFIG ERROR" in capsys.readouterr().out


def test_prune_matching_is_nfc_exact_and_case_mismatch_fails_closed(tmp_path):
    builder = _builder(tmp_path, prune_dirs=[unicodedata.normalize("NFD", "privé")])
    assert builder._check_prune_name("privé", "privé") is True
    with pytest.raises(RuntimeError, match="differs only by case"):
        builder._check_prune_name("PRIVÉ", "PRIVÉ")


def test_metadata_only_case_mismatch_fails_closed_instead_of_extracting(tmp_path):
    builder = _builder(tmp_path, metadata_only_globs=["Private/**"])
    assert builder.is_metadata_only("Private/secret.txt") is True
    with pytest.raises(RuntimeError, match="differs only by case"):
        builder.is_metadata_only("private/secret.txt")


def test_access_roots_use_exact_unicode_identity_not_only_nfc(tmp_path):
    nfc_root = "docs/équipe"
    nfd_root = unicodedata.normalize("NFD", nfc_root)
    assert nfc_root != nfd_root
    builder = _builder(tmp_path, access_roots=[nfc_root])
    assert builder._dir_relevant(nfc_root) is True
    assert builder._dir_relevant(nfd_root) is False


def test_directory_entry_limit_is_enforced_before_unbounded_list_growth(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path, max_directory_entries=2)

    class FakeScandir:
        def __enter__(self):
            return iter(
                (
                    types.SimpleNamespace(name="one"),
                    types.SimpleNamespace(name="two"),
                    types.SimpleNamespace(name="three"),
                )
            )

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(build_index.os, "scandir", lambda _fd: FakeScandir())
    with pytest.raises(RuntimeError, match="max_directory_entries=2"):
        builder._walk_dir(123, "")


def test_directory_depth_limit_fails_before_opening_another_descriptor(tmp_path):
    builder = _builder(tmp_path, max_directory_depth=1)
    with pytest.raises(RuntimeError, match="max_directory_depth=1"):
        builder._walk_dir(123, "a/b", depth=2)


def test_routing_ancestor_opens_only_exact_configured_next_components(tmp_path):
    builder = _builder(
        tmp_path,
        access_roots=["nested/alpha", "nested/bravo/deeper", "target"],
        max_directory_entries=1,
        max_total_entries=1,
    )
    assert builder._routing_child_names("") == ["nested", "target"]
    assert builder._routing_child_names("nested") == ["alpha", "bravo"]
    assert builder._routing_child_names("nested/bravo") == ["deeper"]
    assert builder._routing_child_names("nested/alpha") is None


def test_routing_rejects_case_insensitive_alias_of_configured_component(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path, access_roots=["Cohorts/Finance"])

    class FakeScandir:
        def __enter__(self):
            return iter((types.SimpleNamespace(name="cohorts"),))

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(build_index.os, "scandir", lambda _fd: FakeScandir())
    with pytest.raises(RuntimeError, match="differs in case/spelling"):
        builder._verify_routing_names_exact(123, ["Cohorts"], "")


@pytest.mark.parametrize(
    "names",
    [
        ("Finance", "finance"),
        ("finance", "Finance"),
        ("Caf\N{LATIN SMALL LETTER E WITH ACUTE}", "Cafe\N{COMBINING ACUTE ACCENT}"),
    ],
)
def test_routing_rejects_exact_name_plus_case_or_normalization_alias(
    tmp_path, monkeypatch, names
):
    builder = _builder(tmp_path, access_roots=["Finance/shared"])

    class FakeScandir:
        def __enter__(self):
            return iter(types.SimpleNamespace(name=name) for name in names)

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(build_index.os, "scandir", lambda _fd: FakeScandir())
    configured = "Finance" if "Finance" in names else "Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    with pytest.raises(RuntimeError, match="differs in case/spelling"):
        builder._verify_routing_names_exact(123, [configured], "")


def test_routing_spelling_is_rechecked_after_configured_children(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path, access_roots=["route/child"])
    calls = []

    def verify(_fd, _names, _rel_dir):
        calls.append(True)
        if len(calls) == 2:
            raise build_index.CorpusMutation("route spelling changed")
        return 1

    monkeypatch.setattr(builder, "_verify_routing_names_exact", verify)
    monkeypatch.setattr(
        build_index, "SKIP_FILES", frozenset(set(build_index.SKIP_FILES) | {"route"})
    )
    with pytest.raises(build_index.CorpusMutation, match="spelling changed"):
        builder._walk_dir(123, "")
    assert len(calls) == 2


def test_routing_spelling_is_rechecked_around_final_manifest_validation(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path, access_roots=["route/child"])
    builder._snapshot_dirs = {}
    builder._snapshot_files = {}
    builder._snapshot_routes = {"": ("route",)}
    calls = []

    def verify(_fd, names, rel_dir):
        calls.append((tuple(names), rel_dir))
        if len(calls) == 2:
            raise build_index.CorpusMutation("route changed during manifest validation")
        return 1

    monkeypatch.setattr(builder, "_verify_routing_names_exact", verify)
    with pytest.raises(build_index.CorpusMutation, match="manifest validation"):
        builder._validate_snapshot(123)
    assert calls == [(("route",), ""), (("route",), "")]


def test_total_entry_limit_is_checked_before_entry_stats(tmp_path, monkeypatch):
    builder = _builder(
        tmp_path,
        max_directory_entries=10,
        max_total_entries=2,
    )

    class FakeScandir:
        def __enter__(self):
            return iter(
                types.SimpleNamespace(name=name)
                for name in ("one", "two", "three")
            )

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(build_index.os, "scandir", lambda _fd: FakeScandir())
    with pytest.raises(RuntimeError, match="max_total_entries=2"):
        builder._walk_dir(123, "")


def test_aggregate_extracted_text_budget_fails_closed(tmp_path):
    builder = _builder(tmp_path, max_total_extracted_mb=1)
    builder.max_total_extracted_bytes = 5
    builder._charge_extracted_text("éé")  # four UTF-8 bytes
    assert builder.total_extracted_bytes == 4
    with pytest.raises(RuntimeError, match="max_total_extracted_mb"):
        builder._charge_extracted_text("xx")
    assert builder.total_extracted_bytes == 4


def test_private_output_applies_sqlite_page_budget(tmp_path):
    builder = _builder(tmp_path, max_index_size_mb=1)
    conn = sqlite3.connect(":memory:")
    try:
        builder._apply_index_page_budget(conn)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        max_pages = conn.execute("PRAGMA max_page_count").fetchone()[0]
        assert max_pages * page_size <= 1024 * 1024
    finally:
        conn.close()
        builder.close()


def test_schema_preserves_two_exact_paths_that_share_one_nfc_form(tmp_path):
    builder = _builder(tmp_path)
    builder.conn = sqlite3.connect(":memory:")
    builder.create_schema()
    nfc = "docs/é.txt"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    _insert_file(builder.conn, nfc, relpath_nfc=nfc)
    _insert_file(builder.conn, nfd, relpath_nfc=nfc)
    rows = builder.conn.execute(
        "SELECT relpath, relpath_nfc FROM files ORDER BY relpath"
    ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {nfc, nfd}
    assert {row[1] for row in rows} == {nfc}


def test_schema_contains_cohort_corpus_and_strong_identity_fields(tmp_path):
    builder = _builder(tmp_path)
    builder.conn = sqlite3.connect(":memory:")
    builder.create_schema()
    meta = {row[1] for row in builder.conn.execute("PRAGMA table_info(meta)")}
    files = {row[1] for row in builder.conn.execute("PRAGMA table_info(files)")}
    assert {
        "corpus_uuid",
        "access_policy_id",
        "access_policy_generation",
        "policy_digest",
        "access_roots",
        "excluded_access_count",
    }.issubset(meta)
    assert {
        "relpath_nfc",
        "filename_nfc",
        "mtime_ns",
        "ctime_ns",
        "st_dev",
        "st_ino",
        "source_hash",
        "content_present",
    }.issubset(files)


def test_root_area_sentinel_cannot_collide_with_real_top_level_directory():
    assert build_index.derive_area("loose.txt") == "."
    assert build_index.derive_area("_ROOT/nested.txt") == "_ROOT"
    assert build_index.derive_area("loose.txt") != build_index.derive_area(
        "_ROOT/nested.txt"
    )


def test_year_parser_only_accepts_ascii_decimal_year_segments():
    assert build_index.derive_path_year("docs/2026/report.txt") == 2026
    assert build_index.derive_path_year("docs/２０２６/report.txt") is None
    assert build_index.derive_path_year("docs/²⁰²⁶/report.txt") is None


def test_streaming_hash_detects_same_size_content_replacement(tmp_path):
    builder = _builder(tmp_path)
    left = tmp_path / "alpha.bin"
    right = tmp_path / "bravo.bin"
    left.write_bytes(b"alpha")
    right.write_bytes(b"bravo")
    same_time = 1_700_000_000
    os.utime(str(left), (same_time, same_time))
    os.utime(str(right), (same_time, same_time))
    left_fd = os.open(str(left), os.O_RDONLY)
    right_fd = os.open(str(right), os.O_RDONLY)
    try:
        assert os.fstat(left_fd).st_size == os.fstat(right_fd).st_size
        assert builder._hash_fd(left_fd, 5) != builder._hash_fd(right_fd, 5)
    finally:
        os.close(left_fd)
        os.close(right_fd)


def test_previous_cache_is_bound_to_matching_corpus_policy_and_hash(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)

    reader = _builder(tmp_path)
    reader.corpus_uuid = writer.corpus_uuid
    reader.load_previous()
    try:
        assert reader.previous_file_count == 1
        assert reader.cached_extraction("docs/cached.txt", "sha256:current") == (
            build_index.ST_EXTRACTED,
            "cached text",
            "text",
        )
        assert reader.cached_extraction("docs/cached.txt", "sha256:changed") is None
    finally:
        reader.close()


def test_previous_cache_rejects_mismatched_content_source_signature(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    path = _write_previous_index(writer)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE content SET source_sig='sha256:stale' WHERE relpath='docs/cached.txt'"
    )
    conn.commit()
    conn.close()

    reader = _builder(tmp_path)
    reader.corpus_uuid = writer.corpus_uuid
    reader.load_previous()
    try:
        with pytest.raises(build_index.CacheInvalid, match="source signature"):
            reader.cached_extraction("docs/cached.txt", "sha256:current")
    finally:
        reader.close()


def test_previous_cache_file_count_is_bounded_before_row_materialization(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    path = _write_previous_index(writer)
    conn = sqlite3.connect(path)
    _insert_file(conn, "docs/second.txt")
    conn.execute(
        "UPDATE meta SET file_count=2, content_status_breakdown=?",
        (json.dumps({build_index.ST_NO_TEXT: 2}),),
    )
    conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    reader = _builder(tmp_path, max_total_files=1)
    reader.corpus_uuid = writer.corpus_uuid
    reader.load_previous()
    assert reader.prev_conn is None
    assert reader.prev_meta == {}


def test_oversized_previous_cache_is_rejected_before_sqlite_opens_it(
    tmp_path, monkeypatch
):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)
    reader = _builder(tmp_path)
    reader.corpus_uuid = writer.corpus_uuid
    reader.max_index_size_bytes = 1
    monkeypatch.setattr(
        build_index.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized previous DB reached SQLite")
        ),
    )
    reader.load_previous()
    assert reader.prev_conn is None
    assert reader.prev_meta == {}


def test_previous_cache_rejects_a_different_corpus_uuid(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)
    reader = _builder(tmp_path)
    reader.corpus_uuid = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="belongs to corpus UUID"):
        reader.load_previous()
    assert reader.prev_conn is None
    assert reader.prev_meta == {}


def test_previous_cache_is_invalidated_when_policy_generation_changes(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)
    reader = _builder(tmp_path, access_policy_generation="2")
    reader.corpus_uuid = writer.corpus_uuid
    reader.load_previous()
    assert reader.prev_conn is None
    assert reader.prev_meta == {}
    assert reader.previous_file_count is None


def test_previous_cache_rejects_a_different_access_policy_id(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)
    reader = _builder(tmp_path, access_policy_id="other-cohort")
    reader.corpus_uuid = writer.corpus_uuid
    with pytest.raises(RuntimeError, match="publish_dir belongs to access_policy_id"):
        reader.load_previous()
    assert reader.prev_conn is None


def test_policy_rule_change_requires_generation_bump(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer)
    reader = _builder(tmp_path, prune_dirs=["private"])
    reader.corpus_uuid = writer.corpus_uuid
    with pytest.raises(RuntimeError, match="rules changed without changing"):
        reader.load_previous()
    assert reader.prev_conn is None


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_publication_binding_survives_a_missing_database_and_rejects_other_cohort(
    tmp_path,
):
    corpus_uuid = str(uuid.uuid4())
    first = _builder(tmp_path, access_policy_id="cohort-a")
    first.corpus_uuid = corpus_uuid
    try:
        first.acquire_publish_lock()
        first._ensure_publication_binding()
    finally:
        first.close()

    marker = tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    stored = json.loads(marker.read_text(encoding="utf-8"))
    assert stored["corpus_uuid"] == corpus_uuid
    assert stored["access_policy_id"] == "cohort-a"
    assert not (tmp_path / "published" / build_index.PUBLISHED_DB_NAME).exists()

    second = _builder(tmp_path, access_policy_id="cohort-b")
    second.corpus_uuid = corpus_uuid
    try:
        second.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="is bound to corpus"):
            second._ensure_publication_binding()
    finally:
        second.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_publication_binding_rejects_mode_drift(tmp_path):
    builder = _builder(tmp_path, access_policy_id="cohort-a")
    builder.corpus_uuid = str(uuid.uuid4())
    marker = tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    try:
        builder.acquire_publish_lock()
        builder._ensure_publication_binding()
        marker.chmod(0o660)
        with pytest.raises(RuntimeError, match="marker mode"):
            builder._read_publication_binding()
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_bound_published_database_rejects_permission_drift(tmp_path):
    builder = _builder(tmp_path, access_policy_id="cohort-a")
    builder.corpus_uuid = str(uuid.uuid4())
    target = pathlib.Path(_write_previous_index(builder))
    try:
        builder.acquire_publish_lock()
        builder._ensure_publication_binding()
        target.chmod(0o600)
        with pytest.raises(RuntimeError, match="published index mode"):
            builder._validate_publication_binding()
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_binding_rejects_rule_change_when_database_is_missing(tmp_path):
    corpus_uuid = str(uuid.uuid4())
    first = _builder(tmp_path, access_policy_id="cohort-a")
    first.corpus_uuid = corpus_uuid
    try:
        first.acquire_publish_lock()
        first._ensure_publication_binding()
    finally:
        first.close()

    changed = _builder(
        tmp_path, access_policy_id="cohort-a", prune_dirs=["private"]
    )
    changed.corpus_uuid = corpus_uuid
    try:
        changed.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="without increasing"):
            changed._validate_publication_binding()
    finally:
        changed.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
@pytest.mark.parametrize("baseline", ["missing", "corrupt"])
def test_policy_generation_cannot_advance_without_a_valid_previous_database(
    tmp_path, baseline
):
    corpus_uuid = str(uuid.uuid4())
    first = _builder(tmp_path, access_policy_id="cohort-a")
    first.corpus_uuid = corpus_uuid
    try:
        first.acquire_publish_lock()
        first._ensure_publication_binding()
    finally:
        first.close()

    if baseline == "corrupt":
        target = tmp_path / "published" / build_index.PUBLISHED_DB_NAME
        target.write_bytes(b"not a sqlite database")
        target.chmod(0o640)

    changed = _builder(
        tmp_path,
        access_policy_id="cohort-a",
        access_policy_generation="2",
        prune_dirs=["private"],
        publish_mode="0600",
    )
    changed.corpus_uuid = corpus_uuid
    try:
        changed.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="cannot advance"):
            changed._validate_publication_binding()
    finally:
        changed.close()

    marker = json.loads(
        (tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert marker["access_policy_generation"] == "1"


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_policy_generation_advances_monotonically_from_valid_bound_database(tmp_path):
    first = _builder(tmp_path, access_policy_id="cohort-a")
    first.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(first)
    try:
        first.acquire_publish_lock()
        first._ensure_publication_binding()
    finally:
        first.close()

    second = _builder(
        tmp_path,
        access_policy_id="cohort-a",
        access_policy_generation="2",
        prune_dirs=["private"],
        publish_mode="0600",
    )
    second.corpus_uuid = first.corpus_uuid
    try:
        second.acquire_publish_lock()
        assert second._validate_publication_binding() == "advance"
        second._ensure_publication_binding()
    finally:
        second.close()

    marker = json.loads(
        (tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert marker["access_policy_generation"] == "2"
    assert marker["policy_digest"] == second.policy_digest
    assert marker["publish_mode"] == "0600"
    assert marker["publish_gid"] is None  # 0600 grants no group permissions

    stale = _builder(tmp_path, access_policy_id="cohort-a")
    stale.corpus_uuid = first.corpus_uuid
    try:
        stale.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="cannot move backwards"):
            stale._validate_publication_binding()
    finally:
        stale.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_failed_policy_transition_can_resume_with_the_old_database(tmp_path):
    first = _builder(tmp_path, access_policy_id="cohort-a")
    first.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(first)
    try:
        first.acquire_publish_lock()
        first._ensure_publication_binding()
    finally:
        first.close()

    advancing = _builder(
        tmp_path,
        access_policy_id="cohort-a",
        access_policy_generation="2",
        prune_dirs=["private"],
        publish_mode="0600",
    )
    advancing.corpus_uuid = first.corpus_uuid
    try:
        advancing.acquire_publish_lock()
        advancing._ensure_publication_binding()
    finally:
        advancing.close()

    marker_path = tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    pending = json.loads(marker_path.read_text(encoding="utf-8"))
    assert pending["access_policy_generation"] == "2"
    assert pending["publish_mode"] == "0600"
    assert pending["transition_from"]["access_policy_generation"] == "1"
    assert pending["transition_from"]["publish_mode"] == "0640"

    retry = _builder(
        tmp_path,
        access_policy_id="cohort-a",
        access_policy_generation="2",
        prune_dirs=["private"],
        publish_mode="0600",
    )
    retry.corpus_uuid = first.corpus_uuid
    try:
        retry.acquire_publish_lock()
        assert retry._validate_publication_binding() == "transition"
        assert retry._ensure_publication_binding() is None
    finally:
        retry.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_validation_and_failed_publish_do_not_leave_a_new_binding(tmp_path):
    builder = _builder(tmp_path, access_policy_id="cohort-a")
    builder.corpus_uuid = str(uuid.uuid4())
    marker = tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    try:
        builder.acquire_publish_lock()
        assert builder._validate_publication_binding() == "create"
        assert not marker.exists()
        with pytest.raises(OSError):
            builder.publish()
        assert not marker.exists()
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_missing_configured_route_is_an_actionable_retryable_mutation(tmp_path):
    builder = _builder(tmp_path, access_roots=["missing/child"])
    root_fd = os.open(str(tmp_path / "corpus"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(build_index.CorpusMutation, match="route is missing at 'missing'"):
            builder._walk_dir(root_fd, "")
    finally:
        os.close(root_fd)


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_non_utf8_authorized_filename_fails_with_actionable_error(tmp_path):
    corpus_bytes = os.fsencode(str(tmp_path / "corpus"))
    bad_path = os.path.join(corpus_bytes, b"bad-\xff.txt")
    fd = os.open(bad_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    builder = _builder(tmp_path)
    root_fd = os.open(str(tmp_path / "corpus"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="non-UTF-8 bytes"):
            builder._walk_dir(root_fd, "")
    finally:
        os.close(root_fd)


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
@pytest.mark.parametrize(
    "reserved_name",
    [
        build_index.PUBLISHED_DB_NAME,
        build_index.PUBLISH_LOCK_NAME,
        build_index.PUBLICATION_BINDING_NAME,
    ],
)
def test_scratch_inside_publish_dir_is_rejected_before_reserved_file_unlink(
    tmp_path, reserved_name
):
    protected = tmp_path / "published" / reserved_name
    protected.write_bytes(b"known-good")
    builder = build_index.IndexBuilder(
        _config(tmp_path), str(protected)
    )
    try:
        with pytest.raises(RuntimeError, match="outside publish_dir"):
            builder.acquire_publish_lock()
    finally:
        builder.close()
    assert protected.read_bytes() == b"known-good"


def test_scratch_parent_matching_publish_inode_is_rejected_as_alias(tmp_path):
    builder = _builder(tmp_path)
    builder.output_path = str(tmp_path / "published" / "scratch.sqlite")
    builder._publish_identity = builder._identity(
        os.stat(str(tmp_path / "published"))
    )
    with pytest.raises(RuntimeError, match="by filesystem identity"):
        builder._ensure_private_build_dir()


def test_scratch_parent_matching_corpus_inode_is_rejected_as_alias(tmp_path):
    builder = _builder(tmp_path)
    builder.output_path = str(tmp_path / "corpus" / "scratch.sqlite")
    builder._corpus_identity = builder._identity(os.stat(str(tmp_path / "corpus")))
    with pytest.raises(RuntimeError, match="corpus_root by filesystem identity"):
        builder._ensure_private_build_dir()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
@pytest.mark.parametrize("legacy_kind", ["corrupt", "v1"])
def test_unbound_corrupt_or_v1_database_is_not_claimed_or_overwritten(
    tmp_path, legacy_kind
):
    target = tmp_path / "published" / build_index.PUBLISHED_DB_NAME
    if legacy_kind == "corrupt":
        target.write_bytes(b"not a sqlite database")
    else:
        conn = sqlite3.connect(str(target))
        conn.execute("CREATE TABLE meta(schema_version INTEGER)")
        conn.execute("INSERT INTO meta VALUES(1)")
        conn.commit()
        conn.close()
    original = target.read_bytes()

    builder = _builder(tmp_path, access_policy_id="cohort-b")
    builder.corpus_uuid = str(uuid.uuid4())
    try:
        builder.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="cannot be safely adopted"):
            builder._ensure_publication_binding()
    finally:
        builder.close()

    assert target.read_bytes() == original
    assert not (
        tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    ).exists()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_matching_unbound_v2_database_is_migrated_to_durable_binding(tmp_path):
    builder = _builder(tmp_path, access_policy_id="cohort-a")
    builder.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(builder)
    try:
        builder.acquire_publish_lock()
        builder._ensure_publication_binding()
    finally:
        builder.close()

    marker = tmp_path / "published" / build_index.PUBLICATION_BINDING_NAME
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "version": build_index.PUBLICATION_BINDING_VERSION,
        "corpus_uuid": builder.corpus_uuid,
        "access_policy_id": "cohort-a",
        "access_policy_generation": builder.access_policy_generation,
        "policy_digest": builder.policy_digest,
        "publish_mode": "%04o" % builder.publish_mode,
        "publish_gid": marker.stat().st_gid,
        "transition_from": None,
    }


def test_structurally_incomplete_previous_cache_falls_back_to_full_build(tmp_path):
    writer = _builder(tmp_path)
    writer.corpus_uuid = str(uuid.uuid4())
    _write_previous_index(writer, with_required_tables=False)
    reader = _builder(tmp_path)
    reader.corpus_uuid = writer.corpus_uuid
    reader.load_previous()
    assert reader.prev_conn is None
    assert reader.prev_meta == {}


def test_late_cache_database_error_requests_one_full_rebuild(tmp_path):
    class BrokenConnection(object):
        def execute(self, _query, _parameters=()):
            raise build_index.sqlite3.DatabaseError("corrupt page")

        def close(self):
            pass

    builder = _builder(tmp_path)
    builder.prev_conn = BrokenConnection()
    builder.prev_meta = {
        "docs/cached.txt": ("sha256:current", build_index.ST_EXTRACTED, True)
    }
    with pytest.raises(build_index.CacheInvalid, match="failed during reuse"):
        builder.cached_extraction("docs/cached.txt", "sha256:current")


def test_verify_does_not_treat_a_final_filename_as_a_pruned_directory(
    tmp_path, monkeypatch
):
    builder = _verification_builder(
        tmp_path, monkeypatch, ["docs/private"], prune_dirs=["private"]
    )
    assert builder.verify() is True


def test_verify_detects_a_pruned_directory_segment(tmp_path, monkeypatch):
    builder = _verification_builder(
        tmp_path,
        monkeypatch,
        ["docs/private/leak.txt"],
        prune_dirs=["private"],
    )
    assert builder.verify() is False


def test_verify_rejects_empty_index_without_explicit_opt_in(tmp_path, monkeypatch):
    builder = _verification_builder(tmp_path, monkeypatch, [])
    assert builder.verify() is False


def test_verify_accepts_empty_index_only_with_explicit_safe_threshold(
    tmp_path, monkeypatch
):
    builder = _verification_builder(
        tmp_path, monkeypatch, [], allow_empty=True, min_file_count=0
    )
    assert builder.verify() is True


def test_previous_count_drop_guard_is_not_bypassed_by_access_errors(
    tmp_path, monkeypatch
):
    builder = _verification_builder(
        tmp_path,
        monkeypatch,
        ["docs/one.txt", "docs/two.txt", "docs/three.txt", "docs/four.txt"],
    )
    builder.previous_file_count = 10
    builder.previous_policy_digest = builder.policy_digest
    builder.excluded_access_count = 1
    assert builder.verify() is False


def test_close_unlocks_and_closes_the_publication_descriptor(tmp_path, monkeypatch):
    builder = _builder(tmp_path)
    builder._lock_fd = 73
    builder._scratch_lock_fd = 74
    operations = []
    fake_fcntl = types.SimpleNamespace(
        LOCK_UN=8,
        flock=lambda fd, operation: operations.append(("flock", fd, operation)),
    )
    monkeypatch.setattr(build_index, "fcntl", fake_fcntl)
    monkeypatch.setattr(
        build_index.os,
        "close",
        lambda fd: operations.append(("close", fd)),
    )
    builder.close()
    assert operations == [
        ("flock", 73, 8), ("close", 73),
        ("flock", 74, 8), ("close", 74),
    ]
    assert builder._lock_fd is None
    assert builder._scratch_lock_fd is None


def test_cleanup_scratch_removes_database_and_sqlite_sidecars_only(tmp_path):
    builder = _builder(tmp_path)
    output = pathlib.Path(builder.output_path)
    output.parent.mkdir(mode=0o700)
    artifacts = [
        pathlib.Path(str(output) + suffix)
        for suffix in ("", "-journal", "-wal", "-shm")
    ]
    for artifact in artifacts:
        artifact.write_bytes(b"sensitive scratch")
    unrelated = output.parent / "keep-me.txt"
    unrelated.write_text("unrelated", encoding="utf-8")

    builder.cleanup_scratch()

    assert all(not artifact.exists() for artifact in artifacts)
    assert unrelated.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.parametrize(
    "extra_args,expected",
    [
        (
            [],
            ["acquire", "build", "verify", "publish", "cleanup", "close"],
        ),
        (
            ["--keep-build"],
            ["acquire", "build", "verify", "publish", "close"],
        ),
        (
            ["--no-publish"],
            ["acquire", "build", "verify", "close"],
        ),
    ],
)
def test_main_scratch_retention_policy(
    tmp_path, monkeypatch, extra_args, expected
):
    operations = []

    class FakeBuilder:
        _publication_replaced = False

        def acquire_publish_lock(self):
            operations.append("acquire")

        def build(self):
            operations.append("build")

        def verify(self):
            operations.append("verify")
            return True

        def publish(self):
            operations.append("publish")
            self._publication_replaced = True

        def cleanup_scratch(self):
            operations.append("cleanup")

        def close(self):
            operations.append("close")

    fake = FakeBuilder()
    monkeypatch.setattr(build_index, "load_config", lambda _path: {})
    monkeypatch.setattr(
        build_index, "IndexBuilder", lambda _config, _output: fake
    )
    args = [
        "--config",
        str(tmp_path / "config.json"),
        "--output",
        str(tmp_path / "scratch.sqlite"),
    ] + extra_args

    assert build_index.main(args) == 0
    assert operations == expected


def test_scratch_lock_contention_is_not_confused_with_publication_lock(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path)
    closed = []
    monkeypatch.setattr(builder, "_ensure_private_build_dir", lambda: str(tmp_path))
    monkeypatch.setattr(build_index.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(build_index.os, "open", lambda *_args, **_kwargs: 91)
    monkeypatch.setattr(
        build_index.os,
        "fstat",
        lambda _fd: types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600),
    )
    monkeypatch.setattr(build_index.os, "close", lambda fd: closed.append(fd))

    def fail_lock(_fd, _operation):
        raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    monkeypatch.setattr(
        build_index,
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=fail_lock),
    )
    with pytest.raises(RuntimeError, match="another build is using scratch output"):
        builder._acquire_scratch_lock()
    assert closed == [91]
    assert builder._scratch_lock_fd is None


@pytest.mark.parametrize(
    "failure_errno,expected",
    [
        (errno.EAGAIN, "another build is publishing"),
        (errno.EIO, "cannot lock"),
    ],
)
def test_lock_contention_is_distinguished_from_other_lock_errors(
    tmp_path, monkeypatch, failure_errno, expected
):
    builder = _builder(tmp_path)
    closed = []
    monkeypatch.setattr(builder, "_validate_paths", lambda: None)
    monkeypatch.setattr(build_index.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(build_index.os, "O_DIRECTORY", 0, raising=False)
    opened = iter((80, 81, 82))
    monkeypatch.setattr(build_index.os, "open", lambda *_args, **_kwargs: next(opened))
    real_publish_stat = os.lstat(builder.publish_dir)
    monkeypatch.setattr(
        build_index.os,
        "fstat",
        lambda fd: real_publish_stat
        if fd == 80
        else os.lstat(builder.corpus_root)
        if fd == 81
        else types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600),
    )
    monkeypatch.setattr(build_index.os, "close", lambda fd: closed.append(fd))

    def fail_lock(_fd, _operation):
        raise OSError(failure_errno, os.strerror(failure_errno))

    monkeypatch.setattr(
        build_index,
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=fail_lock),
    )
    with pytest.raises(RuntimeError, match=expected):
        builder.acquire_publish_lock()
    assert closed == [81, 82, 80]
    assert builder._lock_fd is None


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_publication_lock_contends_across_processes(tmp_path):
    builder = _builder(tmp_path)
    lock_path = pathlib.Path(builder.publish_dir) / build_index.PUBLISH_LOCK_NAME
    child_code = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "os.close(fd)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", child_code, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "READY"
        with pytest.raises(RuntimeError, match="another build is publishing"):
            builder.acquire_publish_lock()
        assert builder._lock_fd is None

        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 0
        assert holder.stderr.read() == ""

        builder.acquire_publish_lock()
        assert builder._lock_fd is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_walk_skips_fifo_and_symlink_outside_corpus(tmp_path):
    config = _config(tmp_path)
    corpus = tmp_path / "corpus"
    marker = str(uuid.uuid4())
    (corpus / ".aplidocs-corpus-id").write_text(marker + "\n", encoding="ascii")
    (corpus / "normal.txt").write_text("inside corpus", encoding="utf-8")
    os.mkfifo(str(corpus / "blocking.txt"))
    outside = tmp_path / "outside.txt"
    outside.write_text("must never be indexed", encoding="utf-8")
    os.symlink(str(outside), str(corpus / "escape.txt"))

    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    try:
        builder.acquire_publish_lock()
        builder.build()
        rows = builder.conn.execute("SELECT relpath FROM files ORDER BY relpath").fetchall()
        assert rows == [("normal.txt",)]
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_nfd_access_root_is_seen_and_indexed_end_to_end(tmp_path):
    exact_root = unicodedata.normalize("NFD", "docs/équipe")
    config = _config(tmp_path, access_roots=[exact_root])
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    authorized = corpus / pathlib.Path(exact_root)
    authorized.mkdir(parents=True)
    (authorized / "note.txt").write_text("NFD authorized", encoding="utf-8")
    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    try:
        builder.acquire_publish_lock()
        builder.build()
        assert builder._seen_access_roots == {exact_root}
        assert builder.conn.execute("SELECT relpath FROM files").fetchall() == [
            (exact_root + "/note.txt",)
        ]
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_unrelated_sibling_churn_does_not_retry_authorized_cohort(
    tmp_path, monkeypatch
):
    config = _config(
        tmp_path, access_roots=["cohorts/allowed"], max_walk_retries=0
    )
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    (corpus / "cohorts" / "allowed").mkdir(parents=True)
    (corpus / "cohorts" / "allowed" / "inside.txt").write_text(
        "authorized", encoding="utf-8"
    )
    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    original_walk = builder._walk_dir
    mutated = {"done": False}

    def mutate_routing_sibling(*args, **kwargs):
        result = original_walk(*args, **kwargs)
        rel_dir = args[1]
        if rel_dir == "cohorts" and not mutated["done"]:
            (corpus / "cohorts" / "outside-authorized-root.tmp").write_text(
                "unrelated churn", encoding="utf-8"
            )
            mutated["done"] = True
        return result

    monkeypatch.setattr(builder, "_walk_dir", mutate_routing_sibling)
    try:
        builder.acquire_publish_lock()
        builder.build()
        assert mutated["done"] is True
        assert builder.conn.execute("SELECT relpath FROM files").fetchall() == [
            ("cohorts/allowed/inside.txt",)
        ]
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_inode_replacement_restarts_the_complete_walk(tmp_path, monkeypatch):
    config = _config(tmp_path)
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    (corpus / "mutable.txt").write_text("old value", encoding="utf-8")
    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    original = builder._process_open_file
    replaced = {"done": False}

    def replace_once(parent_fd, name, relpath, opened_fd, before):
        if name == "mutable.txt" and not replaced["done"]:
            replaced["done"] = True
            os.unlink(name, dir_fd=parent_fd)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            replacement_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            try:
                os.write(replacement_fd, b"new value")
                os.fsync(replacement_fd)
            finally:
                os.close(replacement_fd)
        return original(parent_fd, name, relpath, opened_fd, before)

    monkeypatch.setattr(builder, "_process_open_file", replace_once)
    try:
        builder.acquire_publish_lock()
        builder.build()
        assert replaced["done"] is True
        assert builder.conn.execute("SELECT text FROM content").fetchone() == (
            "new value",
        )
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_file_changed_after_its_walk_is_caught_by_final_manifest(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    mutable = corpus / "mutable.txt"
    mutable.write_text("old value", encoding="utf-8")
    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    original_walk = builder._walk_dir
    changed = {"done": False}

    def change_after_walk(*args, **kwargs):
        result = original_walk(*args, **kwargs)
        if args[1] == "" and not changed["done"]:
            mutable.write_text("new value", encoding="utf-8")
            changed["done"] = True
        return result

    monkeypatch.setattr(builder, "_walk_dir", change_after_walk)
    try:
        builder.acquire_publish_lock()
        builder.build()
        assert changed["done"] is True
        assert builder.conn.execute("SELECT text FROM content").fetchone() == (
            "new value",
        )
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_access_root_replaced_after_walk_retries_by_identity(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, access_roots=["cohorts/allowed"])
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    allowed = corpus / "cohorts" / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "value.txt").write_text("old tree", encoding="utf-8")
    builder = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    original_walk = builder._walk_dir
    replaced = {"done": False}

    def replace_after_routing_walk(*args, **kwargs):
        result = original_walk(*args, **kwargs)
        if args[1] == "cohorts" and not replaced["done"]:
            allowed.rename(corpus / "cohorts" / "old-allowed")
            allowed.mkdir()
            (allowed / "value.txt").write_text("new tree", encoding="utf-8")
            replaced["done"] = True
        return result

    monkeypatch.setattr(builder, "_walk_dir", replace_after_routing_walk)
    try:
        builder.acquire_publish_lock()
        builder.build()
        assert replaced["done"] is True
        assert builder.conn.execute("SELECT text FROM content").fetchone() == (
            "new tree",
        )
        assert builder.verify() is True
    finally:
        builder.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_grant_then_revoke_aborts_without_replacing_published_cache(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read mode-000 files, so this permission probe is meaningless")
    config = _config(tmp_path)
    corpus = tmp_path / "corpus"
    (corpus / ".aplidocs-corpus-id").write_text(
        str(uuid.uuid4()) + "\n", encoding="ascii"
    )
    (corpus / "visible.txt").write_text("visible", encoding="utf-8")
    denied = corpus / "secret.txt"
    denied.write_text("must not survive an ACL revoke", encoding="utf-8")
    first = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    try:
        first.acquire_publish_lock()
        first.build()
        assert first.verify() is True
        first.publish()
    finally:
        first.close()

    target = tmp_path / "published" / build_index.PUBLISHED_DB_NAME
    known_good = target.read_bytes()
    denied.chmod(0)
    second = build_index.IndexBuilder(
        config, str(tmp_path / "scratch" / build_index.PUBLISHED_DB_NAME)
    )
    try:
        second.acquire_publish_lock()
        with pytest.raises(RuntimeError, match="permission denied"):
            second.build()
        assert target.read_bytes() == known_good
    finally:
        denied.chmod(0o600)
        second.close()


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_publication_uses_unique_private_temporaries_and_final_mode(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path)
    marker = str(uuid.uuid4())
    (tmp_path / "corpus" / ".aplidocs-corpus-id").write_text(
        marker + "\n", encoding="ascii"
    )
    builder.corpus_uuid = marker
    os.makedirs(os.path.dirname(builder.output_path), mode=0o700)
    first = sqlite3.connect(builder.output_path)
    first.execute("CREATE TABLE marker(value TEXT)")
    first.execute("INSERT INTO marker VALUES('first')")
    first.commit()
    first.close()
    builder._scratch_output_identity = builder._identity(os.stat(builder.output_path))
    captured = []
    real_open = build_index.os.open

    def recording_open(path, flags, *args, **kwargs):
        if (
            kwargs.get("dir_fd") == builder._publish_dir_fd
            and isinstance(path, str)
            and path.startswith(".aplidocs-index.")
        ):
            captured.append(path)
            assert flags & os.O_EXCL
            assert flags & os.O_NOFOLLOW
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(build_index.os, "open", recording_open)
    try:
        builder.acquire_publish_lock()
        builder.publish()
        os.unlink(builder.output_path)
        second = sqlite3.connect(builder.output_path)
        second.execute("CREATE TABLE marker(value TEXT)")
        second.execute("INSERT INTO marker VALUES('second')")
        second.commit()
        second.close()
        builder._scratch_output_identity = builder._identity(
            os.stat(builder.output_path)
        )
        builder.publish()
    finally:
        builder.close()

    target = tmp_path / "published" / build_index.PUBLISHED_DB_NAME
    published = sqlite3.connect(str(target))
    try:
        assert published.execute("SELECT value FROM marker").fetchone() == ("second",)
        assert published.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        published.close()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert len(captured) == 2
    assert captured[0] != captured[1]
    assert all(path.startswith(".aplidocs-index.") for path in captured)
    assert all(not (tmp_path / "published" / path).exists() for path in captured)


@unittest.skipUnless(POSIX_DESCRIPTOR_APIS, "requires POSIX descriptor APIs")
def test_posix_private_scratch_permissions(tmp_path):
    builder = _builder(tmp_path)
    try:
        builder._prepare_private_output()
        assert stat.S_IMODE(os.stat(os.path.dirname(builder.output_path)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(builder.output_path).st_mode) == 0o600
    finally:
        builder.close()

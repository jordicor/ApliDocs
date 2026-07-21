"""Small cross-channel helpers shared by the builder and query CLI."""

import pathlib


# SQLite 3.53.2 fixed a disclosed FTS5 memory-safety defect, and 3.53.3 added
# further fixes for corrupt FTS5 records, including a possible 32-bit buffer
# overrun. ApliDocs opens published databases and executes FTS5 queries, so
# merely checking that the extension exists is insufficient.
MIN_SAFE_SQLITE_VERSION = (3, 53, 3)


def sqlite_provider_version(provider):
    """Return the selected provider's three-component SQLite version."""
    value = getattr(provider, "sqlite_version_info", None)
    try:
        version = tuple(int(part) for part in value[:3])
    except (TypeError, ValueError):
        try:
            version = tuple(
                int(part)
                for part in str(provider.sqlite_version).split(".")[:3]
            )
        except (AttributeError, TypeError, ValueError):
            version = ()
    if len(version) != 3:
        raise RuntimeError("selected SQLite provider does not expose its version")
    return version


def require_safe_sqlite_provider(provider):
    """Reject SQLite releases with known FTS5 memory-safety defects."""
    version = sqlite_provider_version(provider)
    if version < MIN_SAFE_SQLITE_VERSION:
        raise RuntimeError(
            "SQLite >= %s is required for FTS5 safety; selected provider is %s"
            % (
                ".".join(str(part) for part in MIN_SAFE_SQLITE_VERSION),
                ".".join(str(part) for part in version),
            )
        )
    return version


def select_fts5_provider(
    candidates, import_failures=(), version_checker=require_safe_sqlite_provider
):
    """Return the first candidate that is both current enough and has FTS5.

    A stale optional wheel must not shadow a safe stdlib provider.  Candidate
    probes are in-memory and are closed before selection returns.
    """
    failures = [str(item) for item in import_failures]
    seen = set()
    for label, provider in candidates:
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        probe = None
        try:
            version = version_checker(provider)
            probe = provider.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            probe.close()
            probe = None
        except Exception as exc:
            detail = " ".join(str(exc).split()) or type(exc).__name__
            failures.append("%s: %s" % (label, detail[:500]))
            continue
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
        return provider, label, version
    detail = "; ".join(failures) if failures else "no candidates were importable"
    raise RuntimeError("no safe SQLite/FTS5 provider is usable (%s)" % detail)


def absolute_path_to_sqlite_uri(path):
    """Return a read-only immutable SQLite URI for an absolute path.

    ``pathlib`` emits Windows UNC paths as ``file://server/share``.  Stock
    SQLite rejects a non-local URI authority, so represent the same UNC path
    with an empty authority (four slashes after ``file:``).  ``as_uri`` keeps
    all path characters percent-encoded correctly.
    """
    if not path.is_absolute():
        raise ValueError("database path must be absolute")
    uri = path.as_uri()
    if path.drive.startswith("\\\\"):
        if not uri.startswith("file://"):
            raise ValueError("unexpected UNC file URI: %s" % uri)
        uri = "file:////" + uri[len("file://"):]
    return uri + "?mode=ro&immutable=1"


def sqlite_readonly_uri(db_path):
    """Build a SQLite URI without resolving symlinks or mapped drives."""
    return absolute_path_to_sqlite_uri(pathlib.Path(db_path).absolute())

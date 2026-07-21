# ApliDocs — full-text document search for your AI agents

ApliDocs builds read-only SQLite/FTS5 indexes over document names, paths and
extracted text. There is no daemon, Docker service, vector database or cloud
dependency: a scheduled POSIX build publishes one `.sqlite` file per access
cohort, and agents query it locally, over SSH or through a mounted SMB share.

The two programs intentionally have different platform contracts:

- `build_index.py` is a **POSIX builder**. It requires descriptor-based APIs and
  secure-open flags available on Synology/Linux and similar POSIX systems. It
  refuses to run on Windows.
- `aplidocs_cli.py` is a **cross-platform read-only client**. It runs on
  Linux, macOS and Windows, including mapped drives and direct UNC paths such as
  `\\server\share\_aplidocs\aplidocs-index.sqlite`.

Both channels require SQLite `3.53.3` or newer with FTS5. Older providers are
rejected before opening/querying an index because they predate memory-safety
fixes reachable through a crafted FTS5 database.

The index is disposable and can always be rebuilt from the source documents.
It is not harmless metadata, however: it contains a searchable copy of document
text and must be protected like the documents themselves.

## The access-cohort rule

> Every person or agent allowed to read an index must also be authorized to read
> every document included in that index.

This is the central security invariant. ApliDocs does not copy source ACLs into
SQLite, identify the person running a query, or enforce per-row permissions.
Anyone who can read a published database can read every row in it, including
text through the `sql` command.

Create separate indexes when readers have different permissions. Each cohort
needs its own configuration, `access_roots`, `access_policy_id` and
`publish_dir`. The allowlist must contain only trees that every reader in that
cohort may see. The broad value `"access_roots": ["."]` is an explicit assertion
that every index reader may read the entire corpus after `prune_dirs` is
applied; do not use it merely for convenience.

`access_policy_generation` is an administrator-controlled, canonical decimal
revision such as `"1"`, `"2"`, `"3"`. Increase it whenever ACLs, cohort
membership, `access_roots` or another allowlist/exclusion rule changes; never
reuse or decrease a published value. ApliDocs also hashes the configured roots
and exclusions, but it cannot observe an external DSM ACL policy by itself. If
those rules change without a strictly higher generation, the next build
aborts. A different
`access_policy_id` in an existing `publish_dir` also aborts. Immediately before
the first successful publication, ApliDocs creates
`.aplidocs-publication.json`, a durable binding between that directory, the
corpus UUID, cohort ID, policy generation, rules digest and expected POSIX
mode/GID; failed and `--no-publish` builds do not claim an empty directory. The
marker remains authoritative even if the SQLite database is missing or corrupt.
A same-generation rules/permission change or a rollback is rejected from the
marker alone. A higher generation is accepted only after the live database is
verified against the previous marker, so a missing/corrupt database cannot be
used to bypass the old binding. The marker records a recoverable transition
before replacing that database and clears it only after the new inode and
directory entry are durable; after an interruption, rerun the same new
configuration. Do not delete or edit the marker. A generation change
invalidates the old extraction cache.

Policy generation is cache identity, not live authorization. The previously
published database remains readable until a replacement succeeds. For a
revocation, remove the affected reader's access to `publish_dir` immediately
or temporarily withdraw the cohort database, update the materialized roots and
generation, rebuild, and only then grant the revised cohort access.

## How a v2 build works

Every build is fail-closed at its important boundaries:

1. It checks that the builder has the required POSIX APIs, that `corpus_root`
   is a real directory, that private scratch is outside both the corpus and
   publication directory (including an exact bind-mount alias), and that the
   pre-provisioned `publish_dir` is a real directory. It then holds an exclusive
   lock in that publication directory through the final atomic replacement.
2. It opens the corpus UUID marker and verifies that the mounted corpus is the
   one durably bound to the publication directory. An empty directory may be
   bound on first publication and a matching unbound v2 database may be adopted;
   a changed/invalid marker or an unbound v1, corrupt or foreign database aborts
   instead of silently reusing another cohort's directory.
3. It walks from an open root descriptor. Directories and regular files are
   opened without following symlinks; FIFOs and other special objects are not
   indexed. A file must be readable before any cached text can be considered.
   Any permission denial in the relevant/authorized traversal aborts the
   attempt; a successful index therefore always has `excluded_access_count = 0`.
   Detected rename or mutation races restart the complete attempt, up to the
   configured retry count.
4. For supported files it computes a streaming SHA-256 over the opened file.
   Cached extraction is reused only when the exact path and hash match and the
   schema, tool, corpus UUID and access-policy digest also match. Extraction on
   a cache miss uses a private stable copy, so a later pathname swap cannot
   redirect a parser outside the corpus. At the end of the walk it reopens the
   authorized manifest, rechecks routing-component spelling plus directory/file
   identities and signatures, and rehashes files whose content was read before
   accepting the attempt. Routing spelling is checked both before and after the
   potentially long rehash so a case-only SMB/CIFS rename cannot silently enter
   the manifest.
5. It creates schema v2 in a private scratch directory, verifies integrity,
   counts, exclusions, optional smoke searches, the corpus marker and the
   configured drop guards, then publishes through a unique temporary file in
   `publish_dir` followed by an atomic rename and directory `fsync`.

Exact on-disk paths remain the primary keys in v2. Separate NFC-normalized
columns feed FTS, so two distinct paths that normalize to the same Unicode text
no longer collide.

### Extraction formats and finite budgets

| Format | Extensions | Extracted data |
|---|---|---|
| PDF | `pdf` | text layer via `pdftotext` |
| Word OOXML | `docx` `docm` `dotx` | document body |
| Excel OOXML | `xlsx` `xlsm` `xltx` | sheet names, shared/inline strings, formulas and cell values |
| OpenDocument Text | `odt` | document body |
| Plain text | `txt` `md` `csv` | UTF-8/16/32 BOM-aware text, then UTF-8/CP1252/Latin-1 fallback |
| HTML | `htm` `html` | visible text with script/style bodies omitted |
| Legacy Office | `doc` `xls` `ppt` `rtf` `msg` | name/path only (`unsupported_format`) |
| Other files | any other extension | name/path only (`no_text_type`) |

Resource use is bounded before and during parsing. The current hard limits are:

- 500 KiB of final extracted UTF-8 text per file;
- 8 MiB input for plain text/HTML;
- 256 MiB compressed Office/ODF package, 16 MiB per selected ZIP member,
  64 MiB selected uncompressed total, 8 MiB central directory, 10,000 ZIP
  members, stored/deflate codecs only, a functional DEFLATE probe, 200,000 XML
  elements and 256 XML levels;
- 512 MiB PDF input, 500 KiB `pdftotext` output, 60 seconds and 512 MiB child
  address space per PDF by default; a POSIX Python without the required
  `resource` limits aborts instead of running Poppler unbounded;
- configurable caps on entries per directory, path depth, total visited entries
  and total indexed files;
- 1,024 MiB aggregate extracted UTF-8 text and a 2,048 MiB SQLite page/file cap
  per attempt by default, both configurable.

An authorized POSIX filename containing undecodable bytes is rejected with its
byte sequence in hexadecimal instead of reaching SQLite as invalid Unicode.
Rename it to valid UTF-8 before indexing.

Crossing a document limit records `too_big` and keeps name/path metadata.
Malformed individual documents record `error`. On the first authorized PDF in
each attempt, the builder converts a tiny PDF containing a known token. A
missing, nonfunctional or output-dropping `pdftotext` therefore aborts the whole
build as a system dependency failure; unexpected extractor/programming errors
also propagate instead of being mislabeled as corrupt documents.

Every file has one `content_status`:

| Status | Meaning |
|---|---|
| `extracted` | Parsed successfully; extracted text may be empty for a non-PDF document. |
| `empty_pdf` | PDF has no extractable text layer. |
| `unsupported_format` | Legacy format; name/path only. |
| `metadata_only` | A configured glob deliberately suppressed body extraction. |
| `too_big` | A finite input/output/resource limit was exceeded. |
| `error` | This document failed to parse and will be retried next build. |
| `no_text_type` | No extractor exists for this extension. |

## Quick start on Linux/POSIX

Synology users should also read [docs/SYNOLOGY.md](docs/SYNOLOGY.md).

```sh
git clone <your-fork-url> aplidocs
cd aplidocs
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt

# The builder probes pysqlite3 first and stdlib sqlite3 second, skipping any
# residual provider that is too old or lacks FTS5.
python3 - <<'PY'
from aplidocs_common import select_fts5_provider
import sqlite3 as stdlib_sqlite3
candidates = []
try:
    import pysqlite3.dbapi2 as pysqlite3
except Exception:
    pass
else:
    candidates.append(('pysqlite3', pysqlite3))
candidates.append(('stdlib sqlite3', stdlib_sqlite3))
provider, label, version = select_fts5_provider(candidates)
print(label, '.'.join(str(part) for part in version))
PY

# Debian/Ubuntu example; Synology normally provides /usr/bin/pdftotext.
sudo apt-get install poppler-utils

cp config.example.json config.json
$EDITOR config.json

# Provision this directory and its cohort ACL/default ACL before the build.
mkdir -p /absolute/path/to/publish_dir

# Verify that the intended filesystem/share is mounted, then create its UUID once.
python3 build_index.py --config config.json --init-corpus-id

# Build, verify and publish schema v2.
python3 build_index.py --config config.json

# Query the published index.
python3 aplidocs_cli.py --db /absolute/path/to/publish_dir/aplidocs-index.sqlite status
```

The corpus marker defaults to `.aplidocs-corpus-id` in `corpus_root`, is created
with mode `0600`, and is excluded from the index. Preserve it when moving or
restoring the same logical corpus. Never copy it into an unrelated corpus, and
do not run `--init-corpus-id` until you have independently confirmed the
intended mount is present.

### Upgrading an existing v1 installation

Schema v2 is rebuilt; there is no in-place migration. Add the required cohort
configuration keys, pre-provision the publication directory, confirm the mount,
and run `--init-corpus-id` once. Before the first v2 build, move the v1 database
to a private backup outside `publish_dir`: an unbound directory containing a v1,
corrupt or foreign database is deliberately not claimed or overwritten. The
first build then re-extracts supported documents and publishes only after
verification. A matching unbound v2 database is adopted by creating the durable
publication marker; subsequent builds use the strong v2 cache.

Do not point a publication directory containing a v2 index for one corpus UUID
at an unrelated corpus. That mismatch aborts deliberately; use a different
cohort publication directory.

> **SQLite/FTS5 note.** ApliDocs requires SQLite `>= 3.53.3` compiled with FTS5.
> The former `pysqlite3-binary==0.5.4.post2` fallback embeds SQLite `3.51.1`
> and is deliberately no longer installed or accepted. There is no universal
> safe wheel across Synology CPU/glibc/Python combinations: the POSIX builder
> needs a current stdlib/pysqlite3-compatible vendor, container or self-built
> provider. A read-only desktop CLI may instead use `cysqlite>=0.3.4`, which is
> preferred automatically. The audited CPython 3.12 Windows `0.3.4` wheel
> embeds SQLite `3.53.3`; ApliDocs still checks the version of every provider
> at runtime. Always pass the exact-interpreter probe in
> [docs/SYNOLOGY.md](docs/SYNOLOGY.md).

## Configuration reference

`config.json` must be UTF-8 JSON (a UTF-8 BOM is accepted). Unknown keys, wrong
types, ambiguous selectors, backslash globs and relative top-level paths are
rejected before building.

| Key | Type | Default | Contract |
|---|---|---|---|
| `corpus_root` | string | **required** | Absolute path to a real, non-symlink corpus directory. |
| `publish_dir` | string | **required** | Absolute, pre-existing, non-symlink cohort directory. It may be inside the corpus and is excluded by filesystem identity, but cannot equal the corpus root. Apply ACL/default ACL before building. |
| `corpus_id_file` | string | `.aplidocs-corpus-id` | One filename segment for the persistent UUID marker in the corpus root. |
| `access_roots` | list of strings | **required** | Non-overlapping, exact on-disk allowlisted roots inside the corpus. Prefer forward-slash relative paths such as `finance/shared`; absolute paths inside `corpus_root` are accepted. Unicode identity is preserved rather than collapsed to NFC. `.` alone authorizes the entire pruned corpus. |
| `access_policy_id` | string | **required** | Stable, human-readable identifier for the reader cohort/policy. |
| `access_policy_generation` | string | **required** | Canonical non-negative decimal revision (`"1"`, `"2"`, …, at most `9223372036854775807`). Strictly increase it whenever ACLs, membership or allowlists/exclusions change; a bound publication rejects reuse with different rules and rejects rollback. |
| `prune_dirs` | list of strings | `[]` | Exact NFC directory segment names never descended at any depth. `/`, `\\`, surrounding whitespace and case-ambiguous variants are rejected. `@eaDir` and `#recycle` are always pruned. |
| `metadata_only_globs` | list of strings | `[]` | Exact-case forward-slash globs over normalized relative paths. Absolute patterns and empty, `.` or `..` segments are rejected. A match keeps name/path but suppresses body text; a path matching only after case-folding aborts as an ambiguous privacy selector. `*` follows Python `fnmatch` semantics and can cross `/`. |
| `filename_pattern` | string or null | `null` | Optional regular expression (maximum 1,024 UTF-8 bytes) with at least one named group. Captures are stored as JSON in `name_meta`; every filename match has a 100 ms POSIX interval-timer budget, and a timeout aborts the build. |
| `smoke_term` | string or null | `null` | Known non-empty term that must match both filename/path FTS and content FTS before publication. |
| `allow_empty` | boolean | `false` | Explicit opt-in to permit zero files. `min_file_count` still applies. |
| `min_file_count` | integer | `1` | Absolute minimum file-row count required to publish. Set this to a meaningful floor for the corpus. |
| `max_file_count_drop_fraction` | number | `0.5` | Maximum fractional fall from the prior build with the same policy digest, from `0` to `1`. An intentional large allowlist reduction must use a new `access_policy_generation`. |
| `max_walk_retries` | integer | `2` | Complete retries after concurrent corpus mutation, from `0` to `10`. |
| `max_directory_entries` | integer | `50000` | Maximum relevant entries retained from one authorized directory (`1`–`1000000`). At routing ancestors, a bounded streaming name scan verifies exact spelling on case-insensitive filesystems; unrelated siblings are neither retained nor statted/opened. |
| `max_directory_depth` | integer | `128` | Maximum authorized traversal depth (`1`–`512`), bounding recursion and open descriptors. |
| `max_total_entries` | integer | `2000000` | Maximum relevant entries visited in one attempt (`1`–`10000000`). |
| `max_total_files` | integer | `1000000` | Maximum indexed files and previous-cache rows loaded (`1`–`10000000`). |
| `max_total_extracted_mb` | integer | `1024` | Aggregate UTF-8 bytes allowed in extracted content for one index (`1`–`1048576` MiB); crossing it aborts rather than publishing partial text. |
| `max_index_size_mb` | integer | `2048` | Maximum scratch SQLite page/file size (`1`–`1048576` MiB), enforced with `max_page_count` and verified before publication. |
| `pdf_memory_limit_mb` | integer | `512` | POSIX address-space limit for each `pdftotext` child (`128`–`4096` MiB). Tune for the NAS and representative PDFs. |
| `publish_mode` | octal string | `0640` | Final POSIX file mode. Owner read/write is required; group/other write is forbidden. It is persisted policy state, so changing it requires a higher `access_policy_generation`. |
| `publish_group` | string or null | `null` | Optional existing POSIX group assigned before publication. With group permission bits and `null`, the expected inherited/effective GID is still pinned in the publication marker. Changing the expected GID requires a higher generation. This is not a DSM ACL validator. |

Changing configured roots/prunes/globs or publication mode/group changes the
policy digest automatically; bumping `access_policy_generation` as part of the
same administrative change makes the external ACL revision explicit and
prevents reuse of prior text.

### Empty, detached and changing corpora

The UUID marker catches the common “mount disappeared but mountpoint remained”
case. Every configured `access_root` must also be found and readable during the
walk. `allow_empty=false` and `min_file_count` provide absolute guards. The drop
fraction detects an unexpectedly large disappearance relative to the last build
with the same policy. Any relevant `EACCES`/`EPERM` aborts before publication;
`excluded_access_count` is retained as an invariant/audit field and must be zero
in a successful database. For an intentional ACL/allowlist change, bump
`access_policy_generation`; if the rules digest changes without that bump, the
build aborts.

Detected concurrent rename/write activity causes a whole-walk retry, not a
known partial publication. The final manifest pass closes the ordinary race in
which an early path changes later in the walk, but ApliDocs cannot create an
atomic filesystem-wide snapshot: a write can still occur after that path's last
validation. Use a read-only filesystem/storage snapshot as `corpus_root` when a
globally point-in-time index is required. With `max_walk_retries=2`, continued
detected churn aborts and leaves the previous published index in place.

## Private scratch and atomic publication

By default scratch is `<repository>/build/aplidocs-index.sqlite`. The builder
creates a missing scratch directory as `0700`; if it already exists, any
group/other permission bits cause an abort instead of being silently changed.
The database and stable source copies are `0600`; scratch paths inside either
the corpus or `publish_dir` are rejected before cleanup can unlink anything.
After a successful normal publication the scratch database is removed. A second
lock tied to the exact scratch output prevents two cohort builds with different
publication directories from racing on this default file.

- `--keep-build` retains scratch after a successful publication.
- `--no-publish` builds and verifies for diagnostics/tests, retains scratch and
  does not replace the live database.
- A failed build may retain private scratch for diagnosis; it is never
  published and can be removed after investigation.

Capacity planning must allow the old live index, scratch database/rollback
journal and the publication temporary to coexist. Each database is capped by
`max_index_size_mb`; SQLite temporary/journal work can require additional space.

`publish_dir` must already exist with the cohort's directory ACL and inheritable
or default ACL configured. The final SQLite inode receives `publish_mode` and,
if set, `publish_group`; when group bits are enabled without an explicit group,
the setgid-directory GID or builder effective GID is expected instead. The same
mode and pinned GID apply to the durable `.aplidocs-publication.json`
corpus/cohort/policy-state binding. The code validates regular-file type, POSIX
mode and group, but **does not inspect or prove
Synology/DSM extended ACLs**. Because each publish creates a new inode, verify
effective access with a real cohort reader after initial setup and after ACL
changes. Readers need no write permission on the database or publication
directory; only the builder and administrators should be able to create,
replace or delete files there.

## CLI reference

The CLI opens SQLite with `mode=ro&immutable=1`; it never creates `-journal`,
`-wal` or `-shm` files. Its URI helper preserves percent-encoding and converts a
Windows UNC path to SQLite's empty-authority form, avoiding the stock SQLite
`invalid uri authority` failure.

For a reproducible Windows/SMB client, use a dedicated 64-bit CPython 3.12 venv
and the exact `win_amd64` cysqlite wheel audited for this release. The probe is mandatory: an
old `pysqlite3-binary` left in an existing venv is skipped automatically, but
the client still needs at least one safe provider.

```powershell
py -3.12 -c "import struct; assert struct.calcsize('P') == 8"
py -3.12 -m venv .venv-aplidocs
.\.venv-aplidocs\Scripts\python.exe -m pip install cysqlite==0.3.4
.\.venv-aplidocs\Scripts\python.exe -c "import cysqlite as s; print(s.sqlite_version); assert s.sqlite_version_info >= (3,53,3); s.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"
.\.venv-aplidocs\Scripts\python.exe .\aplidocs_cli.py --db "\\server\share\_aplidocs\aplidocs-index.sqlite" status
```

```powershell
python .\aplidocs_cli.py --db "\\server\share\_aplidocs\aplidocs-index.sqlite" status
python .\aplidocs_cli.py --db "Z:\_aplidocs\aplidocs-index.sqlite" search "ACME invoice" --limit 5
```

Global options may appear before or after the subcommand:

- `--db PATH`: database path. Without it, the CLI checks the parent of its own
  directory and then its own directory. An empty value is rejected.
- `--tsv`: tab-separated output instead of JSON.

`--db` may be supplied only once. The cohort wrapper rejects it from caller
arguments so its fixed database cannot be replaced. This is defense in depth;
filesystem/DSM ACLs must still prevent that account from reading other cohorts.
TSV output replaces control characters and quotes formula-leading string cells;
JSON preserves the original data safely.

Commands:

- `status`: build freshness and core `meta` information; run it before search.
- `search "terms"`: deterministic reciprocal-rank fusion of filename/path and
  content FTS results, with `matched_by` and content snippets.
- `name "terms"`: filename/path FTS only.
- `sql "SELECT ..."`: one read-only `SELECT`, `WITH` or `EXPLAIN` statement.

`search` and `name` accept `--area`, `--ext`, `--year`, `--metadata-only` and a
`--limit` from 1 to 1000 (default 20). Combined search computes exact RRF while
each source has at most 10,000 matches; broader queries fail with a request to
add terms/filters instead of returning an approximate rank. Candidate ranking
loads only relpaths/scores; metadata and at-most-2,048-character snippets are
generated only for the final `--limit` results.

The `sql` command streams rows and requires a SQLite provider exposing
`Connection.setlimit`, `getlimit` and either stdlib `set_progress_handler` or
cysqlite's equivalent `progress` API (`setlimit` starts with CPython 3.11 in the
stdlib). Builder, `status`, `search` and `name` remain supported on Python 3.8+
when a safe compatible provider is available; without the required SQL budget
APIs, `sql` fails closed with exit code 3. Its budgets are 64 KiB of query text,
24 result columns, 1 MiB per SQLite value, 1,000 rows, 4 MiB of encoded JSON,
five million VM steps and five seconds. BLOBs are explicit base64 objects and
non-finite SQLite REAL values use `{"$float":"infinity"}`-style tags, so output
remains valid JSON.

### Schema v2

- `meta`: one row with build timing/counts plus `corpus_uuid`,
  `access_policy_id`, `access_policy_generation`, `policy_digest`, serialized
  `access_roots` and `excluded_access_count` (zero for every successful
  fail-closed build).
- `files`: exact `relpath` primary key and exact `filename`; separate
  `relpath_nfc`/`filename_nfc` search columns; `area` (`.` for a root-level
  file), extension, size, seconds/nanoseconds timestamps, device/inode,
  `source_hash`, optional year/name metadata and extraction status.
- `content`: extracted text, extractor and strong `source_sig` keyed by exact
  relative path.
- `files_fts` and `content_fts`: FTS5 external-content indexes.

Inspect the live definition with:

```sh
aplidocs sql "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
```

The `sql` escape hatch exposes every included row, which is why the cohort rule
is mandatory rather than optional guidance.

## Using it from an AI agent

Fill in `AGENTS.md.template` and place the result beside the cohort database.
It tells an agent to check freshness, use a full wrapper path over SSH, and
treat exclusions and cohort boundaries as hard limits. Example queries:

- `aplidocs search "ACME contract" --limit 5`
- `aplidocs search "Q3 budget" --ext xlsx`
- `aplidocs sql "SELECT relpath FROM files WHERE path_year = 2023 ORDER BY relpath"`

## License

MIT — see [LICENSE](LICENSE).

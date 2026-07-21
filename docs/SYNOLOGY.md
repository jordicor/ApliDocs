# ApliDocs on Synology DSM 7.x

This guide targets Synology DSM 7.x. The builder runs on the NAS because it
requires POSIX descriptor APIs; the read-only CLI may also run on Windows,
macOS or Linux against the published SMB file. DSM packages, ACL layouts and
effective permissions vary by model and installation, so verify the commands
and the final ACLs on your own NAS. ApliDocs validates POSIX type/mode/group,
not DSM extended ACL policy.

Throughout, replace `YOURUSER`, `documents`, group names and policy names with
values from your installation.

## Recommended layout: one publication directory per cohort

```text
/var/services/homes/YOURUSER/
├── apps/
│   ├── venv/
│   └── aplidocs/
│       ├── build_index.py
│       ├── aplidocs_cli.py
│       ├── aplidocs_common.py
│       ├── config-finance.json
│       └── config-shared.json
├── bin/
│   ├── aplidocs-finance
│   └── aplidocs-shared
└── logs/
    ├── aplidocs-finance.log
    └── aplidocs-shared.log

/volume1/documents/                       # corpus_root
├── .aplidocs-corpus-id                   # persistent UUID, mode 0600
├── finance/
├── shared/
├── _aplidocs-finance/                    # finance cohort publish_dir
│   └── aplidocs-index.sqlite
└── _aplidocs-shared/                     # shared cohort publish_dir
    └── aplidocs-index.sqlite
```

The publication directory may sit inside the shared corpus. The descriptor
walk excludes it by filesystem identity, so it does not index its own output.
Scratch remains under `~/apps/aplidocs/build/` and must be outside the corpus.

## 1. Decide the cohorts before building

An ApliDocs database does not preserve source ACLs. It has no query-time user
identity and no row-level authorization. Therefore:

> Every reader of one published database must be allowed to read every document
> included in that database.

If finance and general staff have different rights, build two databases. Give
each one an allowlist containing only roots shared by all members of that
cohort, a distinct `access_policy_id`, and a different `publish_dir`. Do not use
`"access_roots": ["."]` unless every reader of that database may read the whole
pruned corpus.

The build user's ability to read a document is not proof that cohort readers may
read it. `access_roots` is the explicit security boundary; ApliDocs does not ask
DSM whether each reader has access. It does reopen each candidate before cache
reuse. Any permission denial in the relevant/authorized traversal aborts the
attempt; it is never converted into a fresh partial index. That fail-closed check
is still not a substitute for cohort design, because the builder cannot evaluate
another reader's DSM ACL.

Use `access_policy_generation` as the revision of the external access policy.
It must be a canonical decimal string; use a strictly increasing sequence such
as `"1"`, `"2"`, `"3"`, not an alphanumeric label such as
`"2026-07-acl2"`, and never a reused/lower value.
Increase it whenever you change DSM ACLs, group membership, `access_roots`,
prunes or other allowlists. This deliberately invalidates old extracted text.
Use a stable policy ID for the cohort, for example `finance-readers`.
Immediately before the first successful publication,
`.aplidocs-publication.json` durably binds the publication directory to the
corpus UUID, policy ID, numeric generation, rules digest and expected POSIX
mode/GID. Failed and `--no-publish` builds do not claim an empty directory.
Pointing another corpus or cohort at a bound directory aborts even if the
SQLite database is later missing or corrupt. The marker also rejects a
same-generation digest/permission change or a rollback without trusting the
database. Advancing to a higher generation first requires the live database to
match the previous marker. It then records a recoverable pending transition,
which is cleared only after the replacement database is durable; after a cut,
rerun the same new configuration. Restore a missing/corrupt old database or
provision a new reviewed `publish_dir` rather than deleting the marker. Do not
delete or edit it.

The generation value does not revoke access by itself. The old database remains
live until a new build is published. During a revocation, first remove the
affected reader from the publication-directory ACL (or temporarily withdraw
the database), update the materialized roots and generation, rebuild, and only
then expose the revised index to the revised cohort.

## 2. Install a current Python/SQLite provider with FTS5

ApliDocs requires SQLite `3.53.3` or newer. This is a security floor, not merely
an FTS5 feature check: earlier releases predate fixes for memory corruption
reachable when a crafted FTS5 database is queried. The former
`pysqlite3-binary==0.5.4.post2` fallback embeds SQLite `3.51.1` and is
deliberately absent from `requirements.txt` and rejected at runtime.

There is no universal safe wheel across DSM CPU, glibc and Python versions.
Record the architecture, then obtain a current stdlib/pysqlite3-compatible
Python from a trusted vendor/container or build it against SQLite `>= 3.53.3`
with FTS5. On ARM and older DSM glibc this normally requires the vendor,
container or self-build route; do not assume an x86 wheel is portable.

From the ApliDocs checkout (the directory containing `aplidocs_common.py`), use
the exact interpreter that cron and the SSH wrapper will use and run the same
selection logic as the builder:

```sh
uname -m
/path/to/the/verified/python - <<'PY'
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
```

Replace `/path/to/the/verified/python` with the exact interpreter used by the
builder/wrapper. The builder repeats the version and FTS5 probes before the
build, trying pysqlite3 and then stdlib and skipping stale or non-FTS
candidates. `pip install -r requirements.txt` intentionally does not guess a
NAS binary provider. For a desktop read-only CLI, `cysqlite>=0.3.4` is also
supported and preferred automatically; the audited CPython 3.12 `win_amd64`
wheel uses SQLite `3.53.3`. The POSIX builder deliberately keeps stdlib/pysqlite3
transaction semantics and does not select cysqlite.

For the audited Windows/SMB client path, install and probe the exact wheel in a
separate venv before using a mapped drive or UNC path:

```powershell
py -3.12 -c "import struct; assert struct.calcsize('P') == 8"
py -3.12 -m venv .venv-aplidocs
.\.venv-aplidocs\Scripts\python.exe -m pip install cysqlite==0.3.4
.\.venv-aplidocs\Scripts\python.exe -c "import cysqlite as s; print(s.sqlite_version); assert s.sqlite_version_info >= (3,53,3); s.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"
```

## 3. Confirm the PDF dependency

The builder uses `pdftotext` for PDFs and treats a missing executable as a
system-wide failure. It also performs a real known-text conversion probe on the
first PDF and caps each child process's address space/CPU. A vendor Python must
provide POSIX `resource.RLIMIT_AS` and `RLIMIT_CPU`; otherwise PDF indexing
aborts rather than launching an unbounded child. Confirm what your NAS actually
provides:

```sh
command -v pdftotext
pdftotext -v
```

Many DSM installations expose it as `/usr/bin/pdftotext`, but do not assume
that without checking. An authorized PDF encountered without this dependency
aborts the build, including when an older PDF extraction was cached.

## 4. Pre-provision `publish_dir` and its ACL

The v2 builder will not create `publish_dir`. Create one directory per cohort
before the first run and configure all of these properties in DSM/File Station
or with your NAS administration tooling:

- the build account can traverse the parent and create, replace and remove
  files in the cohort directory;
- only the build account and administrators can write the directory;
- cohort readers can traverse the directory and read the published database;
- an inheritable/default ACL applies the intended cohort read access to each
  newly created file.

An illustrative POSIX baseline, not a complete DSM ACL recipe, is:

```sh
mkdir -p /volume1/documents/_aplidocs-finance
chgrp aplidocs-finance /volume1/documents/_aplidocs-finance
chmod 0750 /volume1/documents/_aplidocs-finance
```

Configure the matching file policy in JSON:

```json
{
  "publish_mode": "0640",
  "publish_group": "aplidocs-finance"
}
```

`publish_group` must be an existing POSIX group visible through the Python
`grp` module. Use `null` if DSM ACLs supply access without a matching POSIX
group, then select a suitable `publish_mode`. If that mode grants group bits,
ApliDocs still pins the expected GID (the setgid-directory GID, otherwise the
builder's effective GID) and rejects later drift. Prefer an explicit group or a
setgid publication directory when group readability matters. Changing the
mode or expected GID is a policy change and requires a strictly higher
`access_policy_generation`. Group/other write bits are always rejected.

Publication creates a new inode on every rebuild. ApliDocs applies and validates
the configured POSIX mode/group before the atomic rename, but it does **not**
inspect DSM ACL entries, inheritance or effective access. After the first
publish and after every ACL-policy change, test the final
`aplidocs-index.sqlite` using a real cohort reader. Also verify that an
out-of-cohort user cannot read it and that ordinary readers cannot create,
replace or delete files in `publish_dir`.

The first successful publication also creates `.aplidocs-publication.json` with
the same mode and, when relevant, pinned GID. It contains the corpus UUID, policy ID, policy
generation, rules digest and permission state and must remain in place: it
prevents a missing/corrupt database from letting another cohort—or different
rules/permissions under the same cohort—claim the directory. A pending
`transition_from` entry is crash-recovery state; do not edit it, and rerun the
same target generation to finish publication.

## 5. Configure a cohort

Example `/var/services/homes/YOURUSER/apps/aplidocs/config-finance.json`:

```json
{
  "corpus_root": "/volume1/documents",
  "publish_dir": "/volume1/documents/_aplidocs-finance",
  "corpus_id_file": ".aplidocs-corpus-id",
  "access_roots": ["finance", "shared/reference"],
  "access_policy_id": "finance-readers",
  "access_policy_generation": "1",
  "prune_dirs": ["private-working"],
  "metadata_only_globs": ["finance/generated/*"],
  "filename_pattern": null,
  "smoke_term": "invoice",
  "allow_empty": false,
  "min_file_count": 100,
  "max_file_count_drop_fraction": 0.2,
  "max_walk_retries": 2,
  "max_directory_entries": 50000,
  "max_directory_depth": 128,
  "max_total_entries": 2000000,
  "max_total_files": 1000000,
  "max_total_extracted_mb": 1024,
  "max_index_size_mb": 2048,
  "pdf_memory_limit_mb": 512,
  "publish_mode": "0640",
  "publish_group": "aplidocs-finance"
}
```

`access_roots` entries are non-overlapping, exact on-disk paths inside
`corpus_root`; relative entries use `/` even when clients later query over Windows SMB. Configure a
meaningful `min_file_count` for each cohort. `smoke_term`, when set, must appear
in at least one name/path and one extracted body.

The builder rejects selector whitespace, backslashes in POSIX globs/segments,
overlapping roots and case-only near matches for `prune_dirs` or
`metadata_only_globs`. `@eaDir` and `#recycle` are always pruned.

## 6. Initialize the corpus marker once

The marker distinguishes the intended corpus from an empty mountpoint or a
different share mounted at the same path. Before creating it, independently
verify that `/volume1/documents` is the intended mounted shared folder. Check
DSM Storage Manager and inspect known files; `df -P /volume1/documents` can be
one additional signal, not the sole proof.

Then run once:

```sh
/var/services/homes/YOURUSER/apps/venv/bin/python \
  /var/services/homes/YOURUSER/apps/aplidocs/build_index.py \
  --config /var/services/homes/YOURUSER/apps/aplidocs/config-finance.json \
  --init-corpus-id
```

All cohort configs for the same corpus normally use the same marker. The command
is idempotent when it already exists. Preserve the marker when backing up,
restoring or moving the same logical corpus. Never copy it to a different
tenant/corpus. If it is unexpectedly missing, investigate the mount or restore
before creating a replacement; regenerating it makes existing published v2
indexes report a corpus UUID mismatch.

## 7. First v2 build and migration from v1

Run each cohort manually before scheduling:

```sh
/var/services/homes/YOURUSER/apps/venv/bin/python \
  /var/services/homes/YOURUSER/apps/aplidocs/build_index.py \
  --config /var/services/homes/YOURUSER/apps/aplidocs/config-finance.json
```

Schema v2 is a rebuild, not an in-place migration. Before the first v2 run,
move the v1 database to a private backup outside `publish_dir`. An unbound
directory containing a v1, corrupt or foreign database is deliberately refused,
not overwritten; a matching unbound v2 database can be adopted safely. With an
empty bound directory, the first extraction can be slow and no database becomes
live until the complete v2 build passes verification.

Default private scratch is
`/var/services/homes/YOURUSER/apps/aplidocs/build/aplidocs-index.sqlite`. A
missing directory is created as `0700`; an existing directory with any
group/other permission bits is rejected rather than silently changed. The
database and stable parser copies are `0600`. After a successful normal publish
the scratch database is removed.

- `--keep-build` keeps private scratch after a successful publish.
- `--no-publish` is a diagnostic/test mode: verify without replacing the live
  database and keep scratch.
- A failed attempt can leave private scratch for diagnosis; remove it manually
  once it is no longer needed.

Do not place scratch inside `corpus_root` or `publish_dir`; v2 rejects either
configuration before any scratch cleanup can touch publication artifacts.

## 8. Understand the consistency guards

The corpus UUID marker must remain the same before, during and after the walk.
Empty output is rejected unless `allow_empty=true`, every configured
`access_root` must be found and readable, and `min_file_count` always applies.
When the prior index has the same policy digest, a count decrease greater than
`max_file_count_drop_fraction` also aborts.

A detected file or directory replacement restarts the complete walk. A final
manifest pass reopens identities/signatures, verifies the exact configured
spelling of routing components before and after rehashing, and rehashes
content-read files. This catches case-only SMB/CIFS route renames during that
pass, but it is not an atomic filesystem snapshot: a writer can still change a
path after its last validation. Use a read-only Btrfs/storage snapshot as
`corpus_root` when all rows must represent one global instant. With
`max_walk_retries: 2`, there are at most two retries after the first attempt;
continued detected churn leaves the previous published index in place.

Permission-denied files/directories abort the attempt before publication. The
previous database remains in place, so during a revocation its publication ACL
must already have been withdrawn as described above. A successfully published
schema-v2 database always has `meta.excluded_access_count = 0`; a nonzero value
is a verification failure, not a partial-success monitor. If ACLs or the
allowlist change, increase `access_policy_generation` to a strictly higher
canonical decimal value; otherwise a changed policy digest aborts. The advance
requires the live database to match the previous publication marker:

```sh
/var/services/homes/YOURUSER/bin/aplidocs-finance sql \
  "SELECT generated_at, file_count, excluded_access_count, access_policy_id, access_policy_generation FROM meta"
```

Resource limits are finite: 8 MiB text/HTML input; 256 MiB Office/ODF package,
16 MiB per selected ZIP member, 64 MiB selected uncompressed data, 8 MiB ZIP
central directory, 10,000 members, stored/deflate codecs, 200,000 XML nodes and
256 XML levels; 512 MiB PDF input; and 500 KiB final/PDF output. `pdftotext`
gets 60 seconds and, by default, a 512 MiB address-space limit per PDF. The walk
also caps directory entries/depth and total entries/files using the config keys
above. A document crossing a parser limit becomes `too_big` and remains
searchable by name/path; a traversal-wide cap aborts the attempt. Office/ODF
access first runs a known-data DEFLATE probe, so a missing/broken zlib backend is
a system failure rather than thousands of publishable per-file errors. Optional
`filename_pattern` evaluation is interrupted after 100 ms per filename to bound
catastrophic regex backtracking.

The defaults also cap aggregate extracted text at 1,024 MiB and SQLite pages at
2,048 MiB. Tune `max_total_extracted_mb` and `max_index_size_mb` below the NAS
capacity. During publication, the old live DB, scratch DB/journal and a new
temporary coexist, and SQLite may need additional temporary workspace.

## 9. Schedule the rebuild

In **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**, run one task per cohort as `YOURUSER`, preferably serially at off-peak
times. Use absolute paths and set `HOME` explicitly because scheduled tasks have
a minimal environment:

```sh
HOME=/var/services/homes/YOURUSER /var/services/homes/YOURUSER/apps/venv/bin/python /var/services/homes/YOURUSER/apps/aplidocs/build_index.py --config /var/services/homes/YOURUSER/apps/aplidocs/config-finance.json >> /var/services/homes/YOURUSER/logs/aplidocs-finance.log 2>&1
```

The builder locks `.aplidocs-build.lock` in the cohort `publish_dir` before
loading the old cache and holds it through publication. A second concurrent
builder targeting the same directory aborts instead of racing. Different
cohorts use different publication locks, but the builder also locks the exact
scratch output; two cohort tasks sharing the default `build/aplidocs-index.sqlite`
therefore cannot corrupt it. Keep the recommended cohort tasks serial so one
does not fail merely because the other owns that scratch lock.

Some DSM versions reject parentheses in a task name with a generic validation
error. A simple name such as `ApliDocs finance nightly` avoids that UI issue.

After scheduling, inspect the log and query freshness:

```sh
tail -n 30 /var/services/homes/YOURUSER/logs/aplidocs-finance.log
/var/services/homes/YOURUSER/bin/aplidocs-finance status
```

## 10. Install a full-path SSH wrapper

Copy `aplidocs.example` once per cohort, set its `DB`, and make it executable:

```sh
cp aplidocs.example /var/services/homes/YOURUSER/bin/aplidocs-finance
chmod 0755 /var/services/homes/YOURUSER/bin/aplidocs-finance
```

Non-interactive SSH commonly does not add `~/bin` to `PATH`, so agents should
use the absolute wrapper path:

```sh
ssh YOURUSER@nas '/var/services/homes/YOURUSER/bin/aplidocs-finance status'
ssh YOURUSER@nas '/var/services/homes/YOURUSER/bin/aplidocs-finance search "quarterly report" --year 2024'
```

The wrapper rejects caller-supplied `--db`, and the CLI rejects duplicate
`--db` flags, so the fixed cohort database cannot be overridden through its
arguments. This does not replace OS/DSM isolation: the SSH account should still
be unable to read databases belonging to other cohorts.

The optional `sql` command additionally requires a provider with
`Connection.setlimit`, `getlimit` and either `set_progress_handler` or
cysqlite's equivalent `progress`; stdlib limit APIs start in CPython 3.11. On a
provider without those APIs, `status`, `search` and `name` work but `sql` fails
closed with exit code 3. SQL execution is capped by
value/column/row/output plus VM-step and wall-clock budgets.

Windows agents can instead run the CLI locally against a mapped drive or direct
UNC path. The CLI produces SQLite's empty-authority UNC URI and opens it
`mode=ro&immutable=1`, so stock Windows SQLite does not reject the server name
and does not try to create sidecar files:

```powershell
python .\aplidocs_cli.py --db "\\nas\documents\_aplidocs-finance\aplidocs-index.sqlite" status
```

The builder itself remains POSIX-only; a Windows machine is a query client, not
a supported build host.

## 11. Atomic publication and process checks

Home and shared folders can live on different btrfs subvolumes. The builder
copies the verified database to a unique `0600` temporary in `publish_dir`,
applies the final mode/group, `fsync`s it, atomically renames it within that same
directory and `fsync`s the directory. This avoids cross-subvolume `EXDEV` and
never mutates the live database in place.

DSM's `ps w` may hide processes without a controlling terminal. Use:

```sh
ps axww | grep '[b]uild_index.py'
```

The lock is still the authority on whether another build may publish; process
listing is only an operational aid.

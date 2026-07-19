# ApliDocs — full-text document search for your AI agents

**Let your local AI agents search your NAS documents.** ApliDocs builds a single
SQLite database with [FTS5](https://sqlite.org/fts5.html) full-text indexes over
your filenames *and* the text inside your documents (PDF, Word, Excel, ODT,
Markdown, HTML, plain text). A scheduled task rebuilds it nightly. Your agents —
Claude Code and similar — then query it read-only over SSH or a mounted SMB
share and get answers in milliseconds. **No Docker, no daemon, no embeddings, no
cloud.** One cron task and one `.sqlite` file.

It was extracted from a real production deployment on a small business's Synology
NAS indexing roughly 12,000 documents, where the nightly rebuild typically
finishes in seconds thanks to an incremental cache (the first build, which
extracts everything, took the better part of an hour).

---

## Why this exists

If you keep your documents on a NAS and you want an AI agent to answer *"where is
the signed ACME contract?"* or *"which files mention the Q3 budget?"*, your
options today are awkward:

- **Synology Universal Search** indexes your files, but there is no clean,
  scriptable interface an agent can call — it is built for the DSM web UI.
- **Paperless-ngx, RAG stacks, vector databases** are powerful but heavy: they
  run daemons and containers, they want you to import documents into *their*
  world, and they change how you work with your files.

ApliDocs is the zero-maintenance middle ground. It leaves your files exactly
where they are, adds nothing to your workflow, and exposes one thing an agent is
good at using: a read-only query CLI with structured JSON output. The index is a
plain SQLite file you can copy, inspect, or throw away and rebuild at any time.

## How it works

Every rebuild does four things, and **fails fast** at each one — an incomplete or
suspicious build is never published:

1. **Walk** `corpus_root`, recording one metadata row per file. Directories you
   list in `prune_dirs` (plus Synology's `@eaDir` and `#recycle`) are never
   descended. If a directory cannot be listed, the whole build aborts rather than
   silently publishing a partial index.
2. **Extract** plain text from the formats that carry it:

   | Format | Extensions | What is extracted |
   |---|---|---|
   | PDF | `pdf` | text layer via `pdftotext` (poppler/xpdf) |
   | Word (OOXML) | `docx` `docm` `dotx` | document body text |
   | Excel (OOXML) | `xlsx` `xlsm` `xltx` | sheet names + shared strings |
   | OpenDocument Text | `odt` | document body text |
   | Plain text | `txt` `md` `csv` | file contents (UTF-8, latin-1 fallback) |
   | HTML | `htm` `html` | visible text (script/style dropped) |
   | Legacy office | `doc` `xls` `ppt` `rtf` `msg` | recorded as `unsupported_format` (name/path only) |
   | Anything else | — | recorded as `no_text_type` (name/path only) |

   Filenames and paths are normalized to NFC; extracted text is
   whitespace-collapsed and capped per file (500 KB). Every file gets a
   `content_status` recording exactly what happened (see below).
3. **Build a fresh database** in a scratch directory: the `files` / `content`
   tables plus their FTS5 indexes. The build never mutates the live index.
4. **Verify, then publish atomically.** Verification aborts the publish if the
   row count does not match the walk, if any `prune_dirs` folder leaked into the
   index, or if the optional smoke term returns no hits. Only a database that
   passes is copied into `publish_dir` and swapped in with an atomic rename.

**Incremental cache.** The builder reads the previously published database and
reuses the extracted text of any file whose `size + mtime` is unchanged, so only
new and modified documents are re-parsed. When the tool version or schema
changes, the cache is discarded and everything is rebuilt from scratch. The first
build of a large corpus can take many minutes; subsequent nightly builds finish
in seconds.

### `content_status` values

Every indexed file carries one of:

| Status | Meaning |
|---|---|
| `extracted` | Text was extracted (or the document was empty but parsed cleanly). |
| `empty_pdf` | A PDF with no extractable text layer (e.g. a scan with no OCR). |
| `unsupported_format` | Legacy office format; indexed by name/path only. |
| `metadata_only` | Matched a `metadata_only_globs` pattern; content deliberately skipped. |
| `too_big` | Extracted text exceeded the per-file cap; indexed by name/path only. |
| `error` | Extraction failed for this file; recorded and retried next build. |
| `no_text_type` | A file type with no text extractor; indexed by name/path only. |

## Quick start (generic Linux)

Synology users: read **[docs/SYNOLOGY.md](docs/SYNOLOGY.md)** — it covers the
DSM-specific pieces (FTS5, Task Scheduler, PATH, permissions).

```sh
# 1. Get the code and (only if your Python's sqlite3 lacks FTS5) the dependency.
git clone <your-fork-url> aplidocs && cd aplidocs
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt        # installs pysqlite3-binary; see note below

# 2. Install pdftotext for PDF support (Debian/Ubuntu example).
sudo apt-get install poppler-utils

# 3. Configure.
cp config.example.json config.json
$EDITOR config.json                    # set corpus_root and publish_dir at least

# 4. Build the index.
python3 build_index.py --config config.json

# 5. Query it.
python3 aplidocs_cli.py --db /path/to/publish_dir/aplidocs-index.sqlite status
```

> **FTS5 note.** ApliDocs needs SQLite compiled with FTS5. Most desktop Python
> builds already have it (`python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"`
> runs without error). If it does not, `pysqlite3-binary` from `requirements.txt`
> provides a modern SQLite with FTS5 and the tools use it automatically.

To run it nightly, point cron (or the DSM Task Scheduler) at step 4. See
[docs/SYNOLOGY.md](docs/SYNOLOGY.md) for a ready-to-paste scheduled command.

### Configuration reference

`config.json` is a single JSON object. Unknown keys are rejected (typo
protection) and every value is type-checked; any problem aborts the build before
anything is written.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `corpus_root` | string | **required** | Absolute path to the directory tree to index (relative paths are rejected — the builder runs from cron, where the working directory is undefined). |
| `publish_dir` | string | **required** | Absolute path to the directory the finished `aplidocs-index.sqlite` is published to. If it lives inside `corpus_root` it is automatically excluded from the walk (matched by absolute path), so the index never indexes itself. It must not be `corpus_root` itself — that configuration is rejected. |
| `prune_dirs` | list of strings | `[]` | Directory **segment names** never descended, at any depth — e.g. `["private", "personal"]`. Single names only, no `/` (paths are rejected). `@eaDir` and `#recycle` are always pruned in addition. Every name here is also verified absent from the finished index on each build. |
| `metadata_only_globs` | list of strings | `[]` | `fnmatch` globs matched (case-sensitively) against each file's forward-slash relative path. Matches are indexed by name and path but their **content is not extracted** — useful for duplicate folders, machine-generated dumps, or anything whose body you do not want searchable. Note the `fnmatch` semantics: `*` also crosses `/`, so `reports/*` matches everything under `reports/` at any depth; and a leading `*/` requires at least one parent directory, so to match a top-level folder write `archive/duplicates/*`, not `*/archive/duplicates/*`. |
| `filename_pattern` | string or null | `null` | Optional regular expression applied to each filename (via `re.search`). It must contain at least one **named group** (`(?P<name>...)`); on a match, the named groups (nulls dropped) are stored as a JSON object in the `name_meta` column. Lets you encode a filename convention (job codes, client names, document ids) without any schema change. An invalid regex or one without named groups aborts at startup. |
| `smoke_term` | string or null | `null` | A non-empty term you know exists in the corpus. Verification checks it returns at least one filename hit and one content hit — a cheap guard against a build that "succeeded" but indexed nothing. When `null`, the smoke checks are skipped (the other checks still run). |

## CLI reference

`aplidocs_cli.py` is a single read-only file. It opens the database
`mode=ro&immutable=1` on every channel, so a plain query — even directly over SMB
— can never create lock or journal files next to your `.sqlite`. Output is
**JSON by default**; pass `--tsv` for tab-separated rows.

Two global flags may appear **before or after** the subcommand:

- `--db PATH` — the database to query. If omitted, the CLI looks for
  `aplidocs-index.sqlite` in its parent directory (drop a copy of the CLI in
  `<publish_dir>/cli/` and it just works) and then next to itself.
- `--tsv` — tab-separated output instead of JSON.

### `status` — always run this first

Returns the `meta` row, including `generated_at` (UTC). Agents should check
freshness before trusting the index: a `generated_at` more than a day or two old
means the scheduled rebuild is failing.

```sh
aplidocs status
```
```json
{
  "schema_version": 1,
  "generated_at": "2026-05-01T02:00:11Z",
  "elapsed_seconds": 8.4,
  "file_count": 11842,
  "content_count": 9317,
  "content_status": { "extracted": 9317, "empty_pdf": 214, "unsupported_format": 1802, "no_text_type": 509 },
  "tool_version": "aplidocs/1.0",
  "corpus_root": "/volume1/documents"
}
```

### `search` — combined name + content search

Full-text search across both filenames/paths and document contents, merged and
ranked. Each hit says whether it `matched_by` `"name"`, `"content"`, or `"both"`,
and content matches carry a highlighted `snippet`.

```sh
aplidocs search "ACME invoice" --year 2024 --limit 5
```
```json
[
  {
    "relpath": "invoices/2024/2024 invoice ACME Corp.pdf",
    "area": "invoices",
    "ext": "pdf",
    "path_year": 2024,
    "name_meta": null,
    "size_bytes": 184320,
    "mtime": 1714526411,
    "metadata_only": 0,
    "content_status": "extracted",
    "matched_by": "both",
    "snippet": "… total due on this [invoice] from [ACME] Corp …",
    "score": -3.41
  }
]
```

Filters available on `search` and `name`: `--area` (top-level folder), `--ext`
(file extension), `--year` (matches `path_year`), `--metadata-only` (only files
whose content was intentionally skipped), `--limit` (default 20).

### `name` — filename / path search only

Faster than `search` because it never touches document content. Same filters.

```sh
aplidocs name "meeting notes" --ext docx
```

### `sql` — the read-only escape hatch

For questions the subcommands do not cover, run a single `SELECT` / `WITH` /
`EXPLAIN` statement. Anything else is rejected, and the connection is read-only
and immutable regardless.

```sh
aplidocs sql "SELECT area, COUNT(*) AS n FROM files GROUP BY area ORDER BY n DESC"
```

The schema is small: `meta` (one row of build stats), `files` (one row per file:
`relpath`, `filename`, `area`, `ext`, `size_bytes`, `mtime`, `path_year`,
`name_meta`, `metadata_only`, `content_status`), `content` (extracted text), and
the `files_fts` / `content_fts` FTS5 indexes. Confirm the live schema any time
with `sql "SELECT sql FROM sqlite_master WHERE type='table'"`.

## Using it from an AI agent

ApliDocs is designed to be handed to an agent with a short manual. The repo ships
**`AGENTS.md.template`**: fill in its `<PLACEHOLDERS>` (publish directory, the
SSH user, a one-line description of your corpus, the names of any private
folders) and drop the result next to the published database as `AGENTS.md`. An
agent that reads it knows to run `status` first, how to invoke the CLI by full
path, the meaning of every column, and the hard rule never to touch excluded
folders.

Example prompts an agent can satisfy with a single CLI call:

- *"Find the latest signed contract with ACME Corp."* → `search "ACME contract" --limit 5`
- *"Which spreadsheets mention the Q3 budget?"* → `search "Q3 budget" --ext xlsx`
- *"List everything filed under 2023."* → `sql "SELECT relpath FROM files WHERE path_year = 2023 ORDER BY relpath"`

**Why read-only + immutable matters over SMB.** When a database is opened
normally, SQLite may create `-wal`, `-shm`, or `-journal` sidecar files next to
it. Over an SMB mount that often fails outright ("unable to open database file")
and, worse, would litter your share. Opening with `mode=ro&immutable=1` tells
SQLite the file will not change underneath it, so it never attempts any of that.
The CLI always does this; agents querying the raw `.sqlite` without the CLI must
do the same.

**The `sql` escape hatch** exists so an agent is never boxed in by the fixed
subcommands, while the `SELECT`/`WITH`/`EXPLAIN` gate plus the read-only
immutable connection keep it from doing anything but reading.

## Security

- **Read-only by construction.** Every connection is opened `mode=ro&immutable=1`.
  The `sql` subcommand additionally rejects any statement that is not
  `SELECT` / `WITH` / `EXPLAIN`, and extension loading is never enabled. There is
  no code path that writes to a published index.
- **Private folders stay out — and it is verified.** List sensitive directories
  in `prune_dirs`; the walk never descends them, and every build re-checks that
  not a single row carries that path segment, aborting the publish if one does.
  This is a build-time guarantee, not a filter applied at query time.
- **Enforce filesystem permissions too.** `prune_dirs` keeps folders out of the
  *index*; it does not change who can read the originals. Put real ACLs on
  sensitive folders as well. Treat the two as layers, not substitutes.
- **The index is a copy of your data.** The `content` table holds the extracted
  text of your documents. Protect `publish_dir` like the originals: restrict who
  can read it, and restrict who can *write* it so ordinary users cannot delete or
  tamper with the index (only the build user and admins should have write access).

## Synology setup

The DSM-specific guide — venv + FTS5, Task Scheduler, the `~/bin` PATH gotcha,
subvolume/rename behavior, and permission hardening — lives in
**[docs/SYNOLOGY.md](docs/SYNOLOGY.md)**.

## License

MIT — see [LICENSE](LICENSE).

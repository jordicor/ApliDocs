# ApliDocs on Synology DSM 7.x

Everything here is verified on a production Synology NAS running DSM 7.4. The
generic setup lives in the [main README](../README.md); this guide covers only
the DSM-specific pieces. Read it once before your first build — a couple of these
are non-obvious and will cost you an evening otherwise.

Throughout, replace `YOURUSER` with the DSM user that will own and run the index,
and `documents` with your actual shared folder.

## Recommended layout

```
/var/services/homes/YOURUSER/
├── apps/
│   ├── venv/                     # Python venv with pysqlite3-binary (FTS5)
│   └── aplidocs/
│       ├── build_index.py
│       ├── aplidocs_cli.py
│       └── config.json           # your config (not committed)
├── bin/
│   └── aplidocs                  # the wrapper (from aplidocs.example)
└── logs/
    └── aplidocs_cron.log         # scheduled-build log

/volume1/documents/               # your corpus_root
└── _aplidocs/                    # your publish_dir (inside the share)
    ├── aplidocs-index.sqlite     # the published database
    ├── AGENTS.md                 # filled-in AGENTS.md.template (optional)
    └── cli/aplidocs_cli.py       # optional CLI copy for auto-db-discovery
```

Putting `publish_dir` **inside a shared folder** is what lets SMB clients and
agents reach the database. The builder automatically excludes `publish_dir` from
the walk when it sits inside `corpus_root`, so the index never indexes itself.

## 1. FTS5: DSM's bundled Python is not enough

DSM 7.x ships Python 3.8, but its **bundled `sqlite3` module lacks a usable
FTS5** — ApliDocs will refuse to run against it. Create a virtual environment and
install `pysqlite3-binary`, which bundles a recent SQLite (3.51+) with FTS5 built
in. The tools import it automatically when present.

```sh
# SSH in as YOURUSER, then:
python3 -m venv ~/apps/venv
~/apps/venv/bin/pip install pysqlite3-binary==0.5.4.post2
```

Everything (the nightly build and the CLI) must run with `~/apps/venv/bin/python`,
not the system `python3`.

## 2. PDFs work out of the box

DSM ships `pdftotext` (poppler 0.77) at `/usr/bin/pdftotext`, and it is on the
default PATH, so PDF text extraction works with no extra install. Confirm with:

```sh
which pdftotext        # -> /usr/bin/pdftotext
```

## 3. Enable the home service and SSH

In DSM: **Control Panel → User & Group → Advanced → enable "user home service"**
(this creates `/var/services/homes/YOURUSER`), and **Control Panel → Terminal &
SNMP → enable SSH**. Copy `build_index.py`, `aplidocs_cli.py`, and your
`config.json` into `~/apps/aplidocs/`.

Point `config.json` at your share, for example:

```json
{
  "corpus_root": "/volume1/documents",
  "publish_dir": "/volume1/documents/_aplidocs",
  "prune_dirs": ["private"],
  "smoke_term": "invoice"
}
```

## 4. Schedule the nightly rebuild

**Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script.** Run it as `YOURUSER`, daily, at an off-peak hour (e.g. 04:00). Use this
as the command, adjusting the paths:

```sh
HOME=/var/services/homes/YOURUSER /var/services/homes/YOURUSER/apps/venv/bin/python /var/services/homes/YOURUSER/apps/aplidocs/build_index.py >> /var/services/homes/YOURUSER/logs/aplidocs_cron.log 2>&1
```

Two things about that command are not optional:

- **Absolute paths everywhere, and `HOME` set explicitly.** Scheduled tasks run
  in a bare environment — no `HOME`, a minimal `PATH`, none of your login shell's
  setup. A command that works when you paste it into an SSH session can fail
  silently at 04:00 because `~` is undefined or the venv python is not on `PATH`.
  Spell out every path and set `HOME` yourself.
- **The build reads `config.json` next to `build_index.py`.** That is why the
  command passes no `--config`: the default is the config sitting in
  `~/apps/aplidocs/`. Pass `--config /full/path.json` if you keep it elsewhere.

> **DSM gotcha — no parentheses in the task name.** If the task's *name* contains
> parentheses, DSM rejects it with a generic "the settings are invalid" error and
> no explanation. Name it something like `ApliDocs nightly build`, not
> `ApliDocs build (documents)`.

After the first manual run, check the log and confirm freshness:

```sh
~/apps/venv/bin/python ~/apps/aplidocs/build_index.py         # first build; can take up to an hour on a large corpus
tail -n 20 ~/logs/aplidocs_cron.log
```

## 5. Install the CLI wrapper (and why agents need the full path)

Copy `aplidocs.example` to `~/bin/aplidocs`, edit its three paths, and make it
executable:

```sh
cp aplidocs.example ~/bin/aplidocs
$EDITOR ~/bin/aplidocs        # set VENV_PY, CLI, and DB
chmod 755 ~/bin/aplidocs
```

**`~/bin` is not on the PATH for non-interactive SSH sessions.** When an agent
runs `ssh YOURUSER@nas 'aplidocs status'`, that is a non-interactive session that
does not source your `.profile`, so bare `aplidocs` is "command not found".
Agents must call the wrapper by its **full path**:

```sh
ssh YOURUSER@nas '/var/services/homes/YOURUSER/bin/aplidocs status'
ssh YOURUSER@nas '/var/services/homes/YOURUSER/bin/aplidocs search "quarterly report" --year 2024'
```

The wrapper launches the CLI with the venv python (FTS5 guaranteed) and passes
`--db` explicitly, so it works regardless of where the CLI file lives. If you
prefer to drop `--db`, put a copy of `aplidocs_cli.py` in `<publish_dir>/cli/`
instead — the CLI then finds `aplidocs-index.sqlite` in its parent directory on
its own.

## 6. Subvolumes and the atomic publish (do not "optimize" it)

A shared folder and your home directory can live on **different btrfs
subvolumes**. A rename across subvolumes fails with `EXDEV` ("invalid
cross-device link"), which is why the builder does not build straight into the
share and rename in one step. Instead it **copies the finished database into
`publish_dir` first, then renames within that directory** — an atomic swap that
stays on a single filesystem. It looks like a redundant copy; it is not. Leave it
alone.

## 7. Lock down the publish directory

`prune_dirs` keeps private folders out of the index, but the published database
itself is a copy of your documents' text and should be protected accordingly. In
**Control Panel → Shared Folder → Edit → Permissions** (or via ACLs), restrict
`_aplidocs/` so that:

- ordinary SMB users can **read** the index (so their agents can query it), but
- only the build user and administrators can **write** it, so a regular user
  cannot delete or tamper with `aplidocs-index.sqlite`.

## 8. Checking whether a build is running

DSM's `procps` is a minimal BusyBox-style build: **`ps w` hides processes with no
controlling terminal**, which includes the scheduled task and anything an agent
launched over SSH. Use `ps ax` (or `ps axww` for full command lines) instead:

```sh
ps ax | grep build_index
```

If you rely on `ps w` you will conclude nothing is running while the nightly
build is very much running — and possibly start a second one. (The builder holds
an exclusive lock and will refuse to start a second concurrent build, but it is
still confusing.)

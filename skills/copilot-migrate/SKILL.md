---
name: copilot-migrate
description: Export and restore portable GitHub Copilot desktop configuration, Projects, session journals and chat content across machines, with path mapping, backups and incomplete-session reporting.
---

# Copilot Migrate

Use `scripts/copilot_migrate.py` (Python 3.9+, standard library) for portable
Copilot desktop migration. Quit the desktop app fully on both source and target
before copying. SQLite snapshots alone cannot make databases and changing
session files a coordinated snapshot.

```sh
python3 copilot_migrate.py export --out DIR [--no-history]
python3 copilot_migrate.py inspect BUNDLE
python3 copilot_migrate.py import BUNDLE [--map OLD=NEW] [--no-rewrite] [--dry-run] [--sessions-only]
```

On Windows use `python`. `COPILOT_HOME` overrides the Copilot directory. The
output directory must exist and be outside the source home; the bundle must
be outside the target home. Protect bundles: they contain private conversations,
and user-authored files may contain secrets even after exclusions. Never upload
backups or conversation content to GitHub.

Default export includes `data.db`, `session-store.db`, settings, MCP and tool
permissions, skills, personal automation, extension artifacts, durable
`session-state/` (journals, metadata, checkpoints, artifacts), and `chats/`
working content. Session/chat files are not capped at 10 MB; other copied trees
retain the 10 MB cap. Authentication, OAuth, encryption keys, transient locks,
processes, caches, installed plugin/extension code, dependency/build trees,
repository `.git` directories and session/chat symlinks are excluded. Content
filtering is not a guarantee of secret removal.

`--no-history` omits `session-store.db`, `session-state/` and `chats/`, but
still includes `data.db` and its Project/session identities and other metadata.
On import, existing target history is left in place and may not correspond to
the replaced database. This is not a resumable-session restore or history wipe.

Inspect and dry-run before importing. Import replaces bundled databases and
affected session/chat directories, rather than merging their records. Before
writing, it backs up all conflicting targets, including WAL/SHM files and
extension artifacts, into a uniquely named `pre-import-backup-*` under the
target home. Also keep an independent full target backup. A failure during
application may require manual recovery from backup; import is not transactional.
For recovery into an already-restored target, `--sessions-only` leaves target
databases and configuration unchanged. It requires the target session and
Project IDs to contain those from the bundle; it refuses when identities
cannot be verified or the bundle reports incomplete session data. Conflicting
session/chat directories are backed up and replaced. The target databases are
not path-rewritten in this mode.

The source home is mapped to the target home by default. `--map` can be
repeated for different layouts; longer prefixes take priority. Known database
path columns, JSON configuration and known path fields in session metadata JSON
are rewritten, not journals, free text, unknown columns or YAML metadata.
Paths with spaces are supported. Mapping does not clone repositories, recreate
worktrees, preserve uncommitted repository changes, or include external files.
Recreate and check repository/worktree paths and path-keyed permissions.

Export, inspect and import report missing or malformed journals for UUID-backed
sessions found in the index or state directories, missing recognized metadata,
missing databases, and unrecognized index schemas. Exit code 2 means history is incomplete even
though export/import completed, 1 means an error, and 0 means no detected
incompleteness (or intentional `--no-history`). Older bundles may lack journals.
These checks do not prove actual desktop resumption or compatibility with every
application version. Reauthenticate GitHub and MCP, reinstall plugins,
extensions and dependencies, and inspect warnings before opening the app.

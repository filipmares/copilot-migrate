# copilot-migrate

Export GitHub Copilot desktop configuration and portable session data between
machines, including Windows and macOS. The standalone script requires Python
3.9+ and has no external dependencies.

## Migration

Quit the Copilot desktop app **fully on both machines** before exporting or
importing. SQLite snapshots are consistent individually, but a running app can
change its databases and session files between copies.
Export checks for the desktop process before starting, before database snapshots,
and before packing, and aborts if it detects the app. This is a process check,
not a lock: it cannot prevent the app from being launched between checks or
guarantee one coordinated snapshot of the databases and session files.
On Windows, export uses extended-length paths while copying and packing session
trees so deeply nested journals are not skipped due to legacy path limits.

```sh
# Source: use `python` instead of `python3` on Windows
python3 skills/copilot-migrate/scripts/copilot_migrate.py export --out ~/Desktop

# Copy the private bundle securely to the target, outside ~/.copilot.
python3 copilot_migrate.py inspect copilot-migration-*.tar.gz
python3 copilot_migrate.py import copilot-migration-*.tar.gz --dry-run
python3 copilot_migrate.py import copilot-migration-*.tar.gz
```

To recover session/chat files into an already-restored target without replacing
its databases or configuration, use `import BUNDLE --sessions-only`. It requires
the target databases to contain the bundle's indexed session and Project IDs;
it refuses recovery when those identities cannot be verified or the bundle
reports incomplete session data. Conflicting
session/chat directories are backed up and replaced, not merged. Use
`--dry-run` first and review path mappings; the target databases are not rewritten.

The bundle includes the script. `COPILOT_HOME` overrides the source or target
Copilot directory. The output directory must exist and be outside the source
Copilot home; an existing bundle is never overwritten. Bundles contain private
conversation content and may contain sensitive information in user-authored
files: protect them, review them before sharing, and never upload them to GitHub.

By default, import **replaces** the bundled databases and affected session/chat
directories; it does not merge database records. It first backs up every
replaced target, including database WAL/SHM files and extension artifacts, in a
uniquely named `pre-import-backup-*` directory under the target Copilot home.
Unrelated session/chat directories remain in place. Keep an independent backup
of the entire target home before importing. If a write fails after import starts,
restore from that backup with the app still closed; import is not transactional.
The bundle must be outside the target Copilot home. Dry-run does not change it.

## What moves

The default export contains `data.db`, `session-store.db`, settings, MCP and
permissions configuration, skills, personal automation, extension artifacts,
`session-state/` (including `events.jsonl`, metadata, checkpoints and artifacts)
and `chats/` working content. Session and chat files have no 10 MB per-file
limit; the existing 10 MB limit still applies to other copied trees.

Use `export --no-history` for configuration-oriented migration: it omits
`session-store.db`, `session-state/` and `chats/`, but **still includes
`data.db`**, which may contain Project/session identities and other metadata.
Import leaves any existing target history files in place; those files can be
inconsistent with the replaced database. This mode does not restore resumable
sessions or guarantee removal of existing history.

Excluded: machine-bound authentication and encryption files, OAuth credentials,
installed plugins and extension code, caches, locks, processes, logs, dependency
and build trees, repository `.git` directories and symlinks in session/chat
content. User-authored files can still contain secrets; filtering is not a
guarantee of secret removal. Installed runtimes, authentication and live
processes are not migrated. Reauthenticate accounts and MCP servers and
reinstall plugins, extensions and dependencies on the target.

## Paths and limitations

Import maps the recorded source home to the target home. For a different layout
or older bundles use repeatable explicit mappings; the most specific prefix
is applied first:

```sh
python3 copilot_migrate.py import bundle.tar.gz --map 'C:\Users\Alex Example=/Users/alex'
python3 copilot_migrate.py import bundle.tar.gz --no-rewrite
```

Known path columns in the databases, JSON configuration, and known structured
session metadata JSON path fields are rewritten. Conversation journals, free
text, other metadata formats (including YAML), and unknown database columns
are not rewritten. Inspect them and adjust paths as appropriate; a mapping does
not copy repositories or recreate worktrees. Clone repositories and recreate
worktrees at the mapped locations, or adjust bindings manually. Uncommitted
repository changes and external files are not included. Permissions are
path-keyed and may need review.

Export, inspect and import report missing or malformed journals for UUID-backed
sessions discoverable in the session index or session-state directories, and
flags missing recognized metadata. A missing database or unrecognized session
index is also reported. They return
exit code 2 for incomplete history (the bundle/import is still produced), 1 for
an error, and 0 otherwise. `--no-history` reports its intentional omission
without treating it as an error. Older bundles that lack session files may be
incomplete even if their database contains searchable history.

These checks do **not** prove metadata compatibility or actual desktop
resumption; application versions and local workspace dependencies may differ.
Review the manifest and warnings before opening the app. No actual desktop
restore is verified by the synthetic tests.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

MIT

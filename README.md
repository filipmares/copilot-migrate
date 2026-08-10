# copilot-migrate

Move a [GitHub Copilot](https://github.com/features/copilot) app installation
between machines — including across operating systems — without losing your
projects, workflows, skills or tool permissions.

Packaged as an agent skill, but the script works standalone.

## Why this exists

Copying `~/.copilot` to a new machine mostly works, right up until you open the
app and every project is broken. Copilot stores **absolute filesystem paths** in
its SQLite databases and in `permissions-config.json`. A different username — or
a different operating system — invalidates all of them.

On a real installation that is roughly **4,300 path values across 10 columns**.
This tool rewrites them.

## Install

Standalone — the script has no dependencies beyond Python 3.9+:

```sh
curl -O https://raw.githubusercontent.com/filipmares/copilot-migrate/main/scripts/copilot_migrate.py
```

As a Copilot skill:

```sh
git clone https://github.com/filipmares/copilot-migrate ~/.copilot/skills/copilot-migrate
```

## Use

```sh
# On the old machine
python3 copilot_migrate.py export --out ~/Desktop                 # everything
python3 copilot_migrate.py export --out ~/Desktop --no-history    # skip chat history

# Look before you leap (read-only)
python3 copilot_migrate.py inspect copilot-migration-*.tar.gz

# On the new machine, with the Copilot app quit
python3 copilot_migrate.py import copilot-migration-*.tar.gz --dry-run
python3 copilot_migrate.py import copilot-migration-*.tar.gz
```

> **Windows:** the interpreter is `python`, not `python3` — substitute it in
> every command above. Windows ships a `python3` stub that opens the Microsoft
> Store rather than running anything, so the mistake is quiet rather than
> obvious. Verify with `python --version` before starting.

Import auto-detects the source home directory and maps it onto the new machine's
home. Override when the layout differs:

```sh
python3 copilot_migrate.py import bundle.tar.gz --map '/Users/alice=D:\dev\home'
python3 copilot_migrate.py import bundle.tar.gz --no-rewrite
```

Every bundle ships a copy of the script, so the destination machine needs only
Python. Anything overwritten on import is saved to `pre-import-backup-<stamp>/`.

## What moves

Settings, MCP server config, per-directory tool permissions, user skills,
personal automation scripts, canvas extension artifacts, and the databases
holding your projects, accounts, saved workflows and chat history.

Left behind on purpose: OAuth tokens, machine-bound auth and encryption state,
marketplace plugins, caches, logs and session runtime state. Each bundle's
`MANIFEST.md` lists exactly what was included, what was excluded and why, plus
your workflows, accounts and project paths.

Runtime junk is pruned while copying — `node_modules`, `.venv`, `.git`, build
output, browser profiles, caches, and any file over 10 MB.

## Path rewriting

Separator style follows the target platform:

| Source | Target | Result |
| --- | --- | --- |
| `/Users/alice/src/app` | Windows | `C:\Users\bob\src\app` |
| `file:///Users/alice/src/app` | Windows | `file:///C:/Users/bob/src/app` |
| `C:\Users\bob\src\app` | macOS | `/Users/alice/src/app` |

Paths containing spaces are preserved. JSON is parsed and re-serialized rather
than string-replaced, so Windows backslashes are escaped correctly — including
in object keys, which is how `permissions-config.json` stores directories.

Rewriting is prefix-based on the home directory and does not touch free text
inside workflow prompts. Check those separately if they hardcode paths.

## Notes

- Run `import` with the Copilot app fully quit.
- Databases are copied with SQLite's online backup API, so a running app cannot
  produce a torn snapshot, and are rolled out of WAL mode so each file is
  self-contained and portable.
- `COPILOT_HOME` overrides the Copilot directory, which makes it easy to test an
  import into a scratch folder.
- Import prints follow-ups that genuinely matter: re-auth accounts and MCP
  servers, reinstall plugins and extensions, and clone repos to the rewritten
  paths so path-keyed tool approvals keep matching.

## Auditing

`export --no-history` then `inspect` answers "what is actually in my Copilot
setup?" — skills, workflows and schedules, accounts, projects and their paths.

## License

MIT

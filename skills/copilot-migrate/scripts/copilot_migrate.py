#!/usr/bin/env python3
"""Cross-platform export/import of GitHub Copilot app configuration.

Works on macOS, Windows and Linux. Handles the fact that the Copilot databases
store absolute, OS-specific filesystem paths: on import you can rewrite them to
match the new machine's home directory and path style.

    python3 copilot_migrate.py export  [--out DIR] [--no-history]
    python3 copilot_migrate.py inspect BUNDLE
    python3 copilot_migrate.py import  BUNDLE [--map OLD=NEW] [--dry-run]

On Windows the interpreter is `python`, not `python3`.

Run `import` with the Copilot app fully quit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

# --------------------------------------------------------------------------
# What we move, and what we deliberately leave behind.
# --------------------------------------------------------------------------

CONFIG_FILES = ["settings.json", "mcp-config.json", "permissions-config.json"]
DATABASES = ["data.db", "session-store.db"]
HISTORY_DATABASES = {"session-store.db"}
HISTORY_DIRS = ("session-state", "chats")

# Directory names never worth carrying: runtime state, caches, build output.
PRUNE_DIRS = {
    "node_modules", ".venv", "__pycache__", "out", "dist", ".git",
    "user-data", "edge-profile", "Cache", "CacheStorage", "Code Cache",
    "GPUCache", "Service Worker", "logs", "cache", "caches", "build",
    "target", "vendor", ".tox", ".next", "run", "servers",
}
PRUNE_SUFFIXES = {".db-wal", ".db-shm", ".log", ".pyc"}
PRUNE_NAMES = {".DS_Store", "Thumbs.db"}
SENSITIVE_NAMES = {
    "config.json", "m-encryption-key.enc", "credentials", "credentials.json",
    "auth.json", "token.json", "tokens.json", ".env", ".npmrc", ".pypirc",
    ".netrc", ".git-credentials", "id_rsa", "id_ed25519",
}
MAX_FILE_BYTES = 10 * 1024 * 1024

EXCLUDED_NOTES = """\
  config.json                 auto-managed auth/state, machine-bound
  m-encryption-key.enc        local encryption key, machine-bound
  mcp-oauth-config/           OAuth tokens - re-authenticate instead
  installed-plugins/          reinstall from marketplaces named in settings.json
  extensions/<ext>/ code      reinstall the extension; only artifacts/ travel
  platform-cache.db           PR/issue cache, rebuilds itself
  embedding-cache.db          rebuilds itself
  state/ logs/ review-state/ workspaces/
  attachments/ browser-output/ media-cache/ run/ servers/
  *.bak-* *.pre-update-backup-*
  gh CLI config               run `gh auth login` on the new machine
"""

# (table, column) pairs that contain absolute filesystem paths.
PATH_COLUMNS = [
    ("projects", "main_repo_path"),
    ("worktrees", "path"),
    ("workspace_checkout_bindings", "repo_path"),
    ("workspace_checkout_bindings", "checkout_path"),
    ("workspaces", "source_path"),
    ("project_checkouts", "root_resource_uri"),
    ("settings", "storage_location"),
    ("app_state", "value"),
    ("activity_items", "preview"),
    ("session_open_canvases", "payload"),
    ("sessions", "cwd"),
    ("sessions", "workspace_path"),
    ("sessions", "source_path"),
]

HOME_RE = re.compile(r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[^/\\\"'\s,;:)\]}]+")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def running_copilot_desktop_processes() -> list[str]:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [
            f"{row[0]} (PID {row[1]})"
            for row in csv.reader(result.stdout.splitlines())
            if len(row) >= 2 and row[0].lower() == "github.exe"
        ]

    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = parts
        lowered = command.lower()
        if sys.platform == "darwin":
            is_copilot = (
                "/github copilot.app/contents/macos/github" in lowered
                or (
                    "/github copilot.app/contents/frameworks/" in lowered
                    and "github copilot helper" in lowered
                )
            )
        else:
            is_copilot = bool(re.search(r"(?:^|[/\s])github-copilot(?:[/\s-]|$)", lowered))
        if is_copilot:
            processes.append(f"PID {pid}: {command}")
    return processes


def require_copilot_desktop_closed() -> None:
    processes = running_copilot_desktop_processes()
    if processes:
        details = "; ".join(processes)
        raise RuntimeError(
            "GitHub Copilot desktop is running; quit it fully before exporting. "
            f"Detected: {details}"
        )


# --------------------------------------------------------------------------
# Copying helpers
# --------------------------------------------------------------------------

def should_prune(path: Path, durable: bool = False) -> bool:
    name = path.name.lower()
    if durable and (name in SENSITIVE_NAMES or name.startswith((".env.", "token-", "credentials."))
                    or name in {"mcp-oauth-config", ".ssh", ".aws"}
                    or name.endswith((".lock", ".pid", ".sock", ".pem", ".key", ".p12", ".pfx"))
                    or ".bak-" in name or ".pre-update-backup-" in name):
        return True
    if path.name in PRUNE_NAMES:
        return True
    if path.is_dir():
        return path.name in PRUNE_DIRS
    if path.suffix in PRUNE_SUFFIXES:
        return True
    try:
        if not durable and path.stat().st_size > MAX_FILE_BYTES:
            return True
    except OSError:
        return True
    return False


def copy_tree(src: Path, dst: Path, durable: bool = False) -> int:
    """Copy src into dst, following symlinks and pruning runtime junk."""
    count = 0
    if not src.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if durable and entry.is_symlink():
            log(f"  ! excluded symlink: {entry}")
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            if durable:
                raise
            continue
        if should_prune(resolved, durable):
            continue
        target = dst / entry.name
        if resolved.is_dir():
            count += copy_tree(resolved, target, durable)
        else:
            try:
                shutil.copy2(resolved, target)
                count += 1
            except OSError:
                if durable:
                    raise
    return count


def read_conn(path: Path) -> sqlite3.Connection:
    """Open a database without creating or changing it."""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    return con


def snapshot_db(src: Path, dst: Path) -> bool:
    """Consistent SQLite copy that tolerates a live WAL."""
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        con = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
        out = sqlite3.connect(dst)
        with out:
            con.backup(out)
        # Roll the copy out of WAL mode so it is a single self-contained file
        # that opens cleanly anywhere, including read-only on another OS.
        out.execute("PRAGMA journal_mode=DELETE")
        out.close()
        con.close()
        for side in ("-wal", "-shm"):
            stale = Path(str(dst) + side)
            if stale.exists():
                stale.unlink()
        return True
    except sqlite3.Error as exc:
        log(f"  ! could not snapshot {src.name}: {exc}")
        return False


def human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f}{unit}"
        num /= 1024
    return f"{num:.0f}TB"


# --------------------------------------------------------------------------
# Path analysis and rewriting
# --------------------------------------------------------------------------

def detect_home_prefixes(db: Path) -> dict[str, int]:
    """Find home-directory prefixes referenced inside a database."""
    found: dict[str, int] = {}
    if not db.exists():
        return found
    con = read_conn(db)
    try:
        for table, column in PATH_COLUMNS:
            try:
                rows = con.execute(
                    f'SELECT CAST("{column}" AS TEXT) FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL'
                ).fetchall()
            except sqlite3.Error:
                continue
            for (value,) in rows:
                if not value:
                    continue
                for match in HOME_RE.findall(value):
                    key = match.replace("\\", "/")
                    found[key] = found.get(key, 0) + 1
    finally:
        con.close()
    return found


def to_native(path_str: str) -> str:
    """Convert a POSIX-style path to this machine's native separators."""
    if os.name == "nt":
        return str(PureWindowsPath(PurePosixPath(path_str)))
    return path_str


def as_uri(path_str: str) -> str:
    p = path_str.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", p):
        return "file:///" + p
    return "file://" + p


def target_is_windows(path_str: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:", path_str)) or "\\" in path_str


# A path tail may contain spaces (e.g. "Application Support"), so stop only at
# characters that cannot continue a path in JSON, prose or URI context.
TAIL = r'[^"\'\n\r\t,;)\]}<>|*?]*'


def rewrite_value(value: str, mapping: dict[str, str]) -> str:
    """Rewrite path prefixes in a string, normalising separators for the target."""
    result = value
    for old, new in sorted(mapping.items(), key=lambda item: -len(item[0])):
        win = target_is_windows(new)
        sep = "\\" if win else "/"
        new_root = new.replace("/", "\\") if win else new.replace("\\", "/")
        old_fwd = old.replace("\\", "/")

        # file:// URIs always use forward slashes, so handle them first.
        encoded_old = quote(old_fwd, safe="/:")
        def uri_repl(m: "re.Match[str]") -> str:
            tail = m.group(1).replace("\\", "/")
            return as_uri(quote(new_root.replace("\\", "/"), safe="/:") + tail)

        for uri_old in {encoded_old, old_fwd}:
            uri_pattern = re.compile(
                r"file://(?:/)?" + re.escape(uri_old) + r"(?=$|/)" + "(" + TAIL + ")",
                re.IGNORECASE if target_is_windows(old) else 0,
            )
            result = uri_pattern.sub(uri_repl, result)

        for variant in {old, old.replace("/", "\\")}:
            pattern = re.compile(re.escape(variant) + r"(?=$|[\\/])" + "(" + TAIL + ")",
                                 re.IGNORECASE if target_is_windows(old) else 0)

            def repl(m: "re.Match[str]", _v: str = variant) -> str:
                tail = m.group(1).replace("\\", "/").replace("/", sep)
                return new_root + tail

            result = pattern.sub(repl, result)
    return result


def rewrite_db_paths(db: Path, mapping: dict[str, str], dry_run: bool) -> int:
    if not db.exists() or not mapping:
        return 0
    con = sqlite3.connect(db)
    changed = 0
    try:
        existing = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, column in PATH_COLUMNS:
            if table not in existing:
                continue
            try:
                cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
            except sqlite3.Error:
                continue
            if column not in cols:
                continue
            pk = "rowid"
            try:
                rows = con.execute(
                    f'SELECT {pk}, CAST("{column}" AS TEXT) FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL'
                ).fetchall()
            except sqlite3.Error:
                continue
            for rowid, value in rows:
                if not value:
                    continue
                try:
                    data = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    data = None
                if isinstance(data, (dict, list)):
                    def walk(node):
                        if isinstance(node, dict):
                            return {rewrite_value(k, mapping): walk(v) for k, v in node.items()}
                        if isinstance(node, list):
                            return [walk(v) for v in node]
                        return rewrite_value(node, mapping) if isinstance(node, str) else node
                    updated = walk(data)
                    new_value = json.dumps(updated, ensure_ascii=False) if updated != data else value
                else:
                    new_value = rewrite_value(value, mapping)
                if new_value != value:
                    changed += 1
                    if not dry_run:
                        con.execute(
                            f'UPDATE "{table}" SET "{column}" = ? WHERE {pk} = ?',
                            (new_value, rowid),
                        )
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return changed


def rewrite_json_paths(path: Path, mapping: dict[str, str], dry_run: bool) -> int:
    """Rewrite path prefixes inside a JSON file, keys included.

    Parsing rather than string-replacing keeps backslash escaping correct when
    the target is a Windows path.
    """
    if not path.exists() or not mapping:
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))

    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, str):
            new_node = rewrite_value(node, mapping)
            if new_node != node:
                count += 1
            return new_node
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                new_key = rewrite_value(k, mapping) if isinstance(k, str) else k
                if new_key != k:
                    count += 1
                out[new_key] = walk(v)
            return out
        return node

    updated = walk(data)
    if count and not dry_run:
        path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    return count


def session_ids(root: Path) -> set[str]:
    """Find UUID-backed sessions in the portable session index and state."""
    ids: set[str] = set()
    state = root / "session-state"
    if state.is_dir():
        for path in state.iterdir():
            if path.is_dir():
                try:
                    uuid.UUID(path.name)
                    ids.add(path.name)
                except ValueError:
                    continue
    db = root / "db" / "session-store.db"
    if db.is_file():
        con = read_conn(db)
        try:
            columns = {row[1] for row in con.execute('PRAGMA table_info("sessions")')}
            if "id" in columns:
                for (value,) in con.execute('SELECT id FROM sessions WHERE id IS NOT NULL'):
                    try:
                        ids.add(str(uuid.UUID(str(value))))
                    except (ValueError, AttributeError):
                        continue
        finally:
            con.close()
    return ids


def session_warnings(root: Path, history: bool) -> list[str]:
    if not history:
        return ["History and resumable session files were intentionally excluded."]
    warnings = []
    if not (root / "db" / "data.db").is_file():
        warnings.append("data.db is missing; Project and workspace relationships cannot be restored.")
    if not (root / "db" / "session-store.db").is_file():
        warnings.append("session-store.db is missing; searchable history may be incomplete.")
    else:
        con = read_conn(root / "db" / "session-store.db")
        try:
            columns = {row[1] for row in con.execute('PRAGMA table_info("sessions")')}
            if "id" not in columns:
                warnings.append("Session index schema is unrecognized; journal completeness cannot be verified.")
        finally:
            con.close()
    for session_id in sorted(session_ids(root)):
        session = root / "session-state" / session_id
        journal = session / "events.jsonl"
        if not journal.is_file() or journal.stat().st_size == 0:
            warnings.append(f"Session {session_id}: missing or empty events.jsonl; cannot claim it is resumable.")
        else:
            try:
                records = 0
                with journal.open(encoding="utf-8") as events:
                    for line in events:
                        if line.strip():
                            json.loads(line)
                            records += 1
                if not records:
                    raise ValueError("empty journal")
            except (OSError, UnicodeError, ValueError):
                warnings.append(f"Session {session_id}: unreadable or malformed events.jsonl; cannot claim it is resumable.")
        if not any((session / name).is_file() for name in
                   ("workspace.yaml", "workspace.yml", "workspace.json", "session.json", "metadata.json")):
            warnings.append(f"Session {session_id}: no recognized session metadata; resumability is uncertain.")
    if not session_ids(root):
        warnings.append("No UUID-backed sessions were found; resumability could not be verified.")
    return warnings


def report_session_warnings(root: Path, history: bool) -> list[str]:
    warnings = session_warnings(root, history)
    for warning in warnings:
        log(f"  ! {warning}")
    return warnings


def bundle_history(root: Path) -> bool:
    info = root / "bundle-info.json"
    if info.is_file():
        data = json.loads(info.read_text(encoding="utf-8"))
        if not isinstance(data.get("history"), bool):
            raise RuntimeError("invalid bundle history metadata")
        return data["history"]
    return True  # Older bundles did not include durable session files.


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    require_copilot_desktop_closed()
    src = copilot_home()
    if not src.is_dir():
        log(f"No Copilot home at {src}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out).expanduser() if args.out else Path.home() / "Desktop"
    if not out_dir.is_dir():
        raise RuntimeError(f"output directory does not exist: {out_dir}")
    if out_dir.resolve() == src.resolve() or src.resolve() in out_dir.resolve().parents:
        raise RuntimeError("output directory must be outside the source Copilot home")
    tmp = Path(tempfile.mkdtemp())
    stage = tmp / f"copilot-migration-{stamp}"
    stage.mkdir(parents=True)

    try:
        log("==> config files")
        for name in CONFIG_FILES:
            f = src / name
            if f.is_file():
                shutil.copy2(f, stage / name)
                log(f"  {name}")

        log("==> user skills")
        skills_src = src / "skills"
        n = copy_tree(skills_src, stage / "skills")
        if n:
            dirs = [d.name for d in (stage / "skills").iterdir() if d.is_dir()]
            log(f"  {len(dirs)} skills, {n} files (runtime state stripped)")
            for entry in sorted(skills_src.iterdir()):
                if entry.is_symlink():
                    log(f"  note: {entry.name} was a symlink -> {os.readlink(entry)}")

        log("==> personal automation")
        auto_src = src / "automation"
        if auto_src.is_dir():
            copy_tree(auto_src, stage / "automation")
            for db in auto_src.glob("*.db"):
                if snapshot_db(db, stage / "automation" / db.name):
                    log(f"  {db.name} (consistent snapshot)")

        log("==> extension artifacts")
        ext_src = src / "extensions"
        if ext_src.is_dir():
            for ext in sorted(p for p in ext_src.iterdir() if p.is_dir()):
                artifacts = ext / "artifacts"
                if artifacts.is_dir():
                    copy_tree(artifacts, stage / "extensions" / ext.name / "artifacts")
                    log(f"  extensions/{ext.name}/artifacts")

        log("==> databases")
        require_copilot_desktop_closed()
        for name in DATABASES:
            if args.no_history and name in HISTORY_DATABASES:
                log(f"  {name} skipped (--no-history)")
                continue
            dest = stage / "db" / name
            if snapshot_db(src / name, dest):
                log(f"  {name} ({human(dest.stat().st_size)})")
            elif (src / name).exists():
                raise RuntimeError(f"could not snapshot {name}")

        if not args.no_history:
            log("==> durable sessions and chat working content")
            for name in HISTORY_DIRS:
                source = src / name
                if source.is_symlink():
                    raise RuntimeError(f"refusing symlinked state directory: {source}")
                if source.is_dir():
                    count = copy_tree(source, stage / name, durable=True)
                    log(f"  {name}: {count} files")
        (stage / "bundle-info.json").write_text(json.dumps({
            "version": 1, "history": not args.no_history,
            "source_home": str(Path.home()),
        }, indent=2) + "\n", encoding="utf-8")
        warnings = report_session_warnings(stage, not args.no_history)

        write_manifest(stage, src, stamp)

        # Ship this script inside the bundle so the target machine needs nothing else.
        try:
            shutil.copy2(Path(__file__).resolve(), stage / "copilot_migrate.py")
        except OSError:
            pass

        require_copilot_desktop_closed()
        log("==> packing")
        tarball = out_dir / f"copilot-migration-{stamp}.tar.gz"
        with tarball.open("xb") as out:
            with tarfile.open(fileobj=out, mode="w:gz") as tf:
                tf.add(stage, arcname=stage.name)

        log("")
        log(f"Done: {tarball}  ({human(tarball.stat().st_size)})")
        log("Next: copy it over, then run (from inside the extracted bundle)")
        log(f"  python3 copilot_migrate.py import {tarball.name}    # macOS, Linux")
        log(f"  python  copilot_migrate.py import {tarball.name}    # Windows")
        return 2 if warnings and not args.no_history else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_manifest(stage: Path, src: Path, stamp: str) -> None:
    lines = [
        f"# Copilot migration manifest - {stamp}",
        f"# Source home: {src}",
        f"# Source platform: {sys.platform}",
        "",
        "## Included",
    ]
    for p in sorted(stage.rglob("*")):
        rel = p.relative_to(stage)
        if len(rel.parts) <= 2:
            lines.append(f"  {rel.as_posix()}{'/' if p.is_dir() else ''}")

    lines += ["", "## Deliberately excluded (re-auth or regenerate)", EXCLUDED_NOTES]
    lines += ["## Session completeness"]
    warnings = session_warnings(stage, bundle_history(stage))
    lines.extend(f"  {warning}" for warning in warnings)
    if not warnings:
        lines.append("  Indexed UUID sessions have nonempty journals; actual resumption is not verified.")

    db = stage / "db" / "data.db"
    if db.exists():
        con = read_conn(db)
        try:
            lines.append("## Workflows")
            for name, interval, enabled in con.execute(
                "SELECT name, interval, enabled FROM workflows"
            ):
                lines.append(f"  {name} ({interval}, enabled={enabled})")
            lines.append("")
            lines.append("## Accounts (re-auth required)")
            for login, host in con.execute("SELECT login, host FROM accounts"):
                lines.append(f"  {login} @ {host}")
            lines.append("")
            lines.append("## Projects and their local paths")
            for name, path in con.execute(
                "SELECT name, main_repo_path FROM projects"
            ):
                lines.append(f"  {name}: {path}")
        except sqlite3.Error:
            pass
        finally:
            con.close()

        lines.append("")
        lines.append("## Home prefixes found inside the databases")
        lines.append("## (import rewrites these; see `inspect`)")
        for prefix, count in sorted(
            detect_home_prefixes(db).items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {prefix}  ({count} references)")

    (stage / "MANIFEST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Inspect / Import
# --------------------------------------------------------------------------

def extract_bundle(bundle: Path, work: Path) -> Path:
    with tarfile.open(bundle, "r:gz") as tf:
        members = tf.getmembers()
        names = set()
        roots = set()
        for member in members:
            parts = member.name.split("/")
            if (not member.name or "\\" in member.name or member.name.startswith("/")
                    or any(p in ("", ".", "..") or ":" in p for p in parts)
                    or not (member.isdir() or member.isfile())
                    or member.name in names):
                raise RuntimeError(f"unsafe archive member: {member.name}")
            names.add(member.name)
            roots.add(parts[0])
        if len(roots) != 1:
            raise RuntimeError("unexpected bundle layout")
        for member in members:
            target = work.joinpath(*member.name.split("/"))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    root = work / next(iter(roots))
    if not root.is_dir():
        raise RuntimeError("unexpected bundle layout")
    return root


def cmd_inspect(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser()
    work = Path(tempfile.mkdtemp())
    try:
        root = extract_bundle(bundle, work)
        manifest = root / "MANIFEST.md"
        if manifest.exists():
            log(manifest.read_text(encoding="utf-8"))
        warnings = report_session_warnings(root, bundle_history(root))
        prefixes = detect_home_prefixes(root / "db" / "data.db")
        if prefixes:
            log("Suggested mapping for this machine:")
            for prefix in prefixes:
                log(f"  --map '{prefix}={Path.home().as_posix()}'")
        return 2 if warnings and bundle_history(root) else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_mapping(args: argparse.Namespace, root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in args.map or []:
        if "=" not in item:
            raise RuntimeError(f"malformed --map value: {item}")
        old, new = item.split("=", 1)
        if not old.strip("/\\") or not new.strip("/\\"):
            raise RuntimeError(f"empty --map prefix: {item}")
        mapping[old.rstrip("/\\").replace("\\", "/")] = new.rstrip("/\\")
    if mapping or args.no_rewrite:
        return mapping

    # Auto-detect: map every discovered source home onto this machine's home.
    info = root / "bundle-info.json"
    if info.is_file():
        source_home = json.loads(info.read_text(encoding="utf-8")).get("source_home")
        if isinstance(source_home, str) and source_home:
            return {source_home.replace("\\", "/").rstrip("/"): str(Path.home())}
    prefixes = detect_home_prefixes(root / "db" / "data.db")
    home = Path.home().as_posix()
    for prefix in prefixes:
        if prefix.rstrip("/") != home.rstrip("/"):
            mapping[prefix] = home
    return mapping


def check_target(dest: Path, rel: Path) -> None:
    """Reject symlinks in existing destination paths, including merged trees."""
    if dest.is_symlink():
        raise RuntimeError(f"refusing symlinked destination: {dest}")
    current = dest
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"refusing symlinked destination: {current}")
    if current.is_dir():
        for path in current.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"refusing symlink in destination tree: {path}")


def rewrite_session_metadata(root: Path, mapping: dict[str, str]) -> None:
    """Rewrite structured path fields, not journals or conversation text."""
    keys = {"cwd", "path", "workspace_path", "source_path", "repo_path",
            "checkout_path", "main_repo_path", "root_resource_uri"}

    def walk(value, key=""):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, str) and key in keys:
            return rewrite_value(value, mapping)
        return value

    state = root / "session-state"
    if not state.is_dir() or not mapping:
        return
    for session in state.iterdir():
        if not session.is_dir():
            continue
        for name in ("workspace.json", "session.json", "metadata.json"):
            path = session / name
            if path.is_file():
                original = json.loads(path.read_text(encoding="utf-8"))
                updated = walk(original)
                if updated != original:
                    path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


def indexed_ids(db: Path, table: str) -> set[str]:
    if not db.is_file():
        raise RuntimeError(f"session recovery requires database: {db}")
    con = read_conn(db)
    try:
        columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if "id" not in columns:
            raise RuntimeError(f"session recovery cannot verify {table} identities in {db}")
        return {str(row[0]) for row in con.execute(f'SELECT id FROM "{table}" WHERE id IS NOT NULL')}
    finally:
        con.close()


def cmd_import(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser()
    if not bundle.is_file():
        log(f"No such bundle: {bundle}")
        return 1

    dest = copilot_home().expanduser()
    if dest.is_symlink():
        raise RuntimeError(f"refusing symlinked Copilot home: {dest}")
    if dest.resolve() == bundle.resolve() or dest.resolve() in bundle.resolve().parents:
        raise RuntimeError("bundle must be outside the destination Copilot home")
    work = Path(tempfile.mkdtemp())
    try:
        root = extract_bundle(bundle, work)
        mapping = build_mapping(args, root)
        history = bundle_history(root)
        warnings = report_session_warnings(root, history)
        if args.sessions_only:
            if not history:
                raise RuntimeError("session recovery requires a bundle with history")
            if warnings:
                raise RuntimeError("session recovery refused: bundle session data is incomplete")
            source_ids = session_ids(root)
            target_ids = indexed_ids(dest / "session-store.db", "sessions")
            if not source_ids or not source_ids.issubset(target_ids):
                raise RuntimeError("session recovery requires matching session IDs in the target session index")
            source_projects = indexed_ids(root / "db" / "data.db", "projects")
            target_projects = indexed_ids(dest / "data.db", "projects")
            if not source_projects.issubset(target_projects):
                raise RuntimeError("session recovery requires matching Project IDs in the target database")
            log("==> session recovery: target databases and configuration will not be overwritten")

        log(f"==> importing into {dest}")
        if args.dry_run:
            log("    (dry run - nothing will be written)")
        if mapping:
            log("==> path rewriting")
            for old, new in mapping.items():
                log(f"    {old}  ->  {to_native(new)}")
        else:
            log("==> path rewriting disabled")

        operations: list[tuple[Path, Path, str]] = []
        for name in ([] if args.sessions_only else CONFIG_FILES):
            f = root / name
            if not f.is_file():
                continue
            rewrite_json_paths(f, mapping, False)
            operations.append((f, Path(name), "file"))

        for folder in (() if args.sessions_only else ("skills", "automation")):
            srcdir = root / folder
            if srcdir.is_dir():
                operations.append((srcdir, Path(folder), "merge"))

        ext = root / "extensions"
        if ext.is_dir() and not args.sessions_only:
            for e in sorted(p for p in ext.iterdir() if p.is_dir()):
                art = e / "artifacts"
                if art.is_dir():
                    operations.append((art, Path("extensions") / e.name / "artifacts", "merge"))

        for name in ([] if args.sessions_only else DATABASES):
            f = root / "db" / name
            if not f.is_file():
                continue
            changed = rewrite_db_paths(f, mapping, False)
            suffix = f", {changed} path values rewritten" if mapping else ""
            log(f"  {name}{suffix}")
            operations.append((f, Path(name), "file"))
            for side in ("-wal", "-shm"):
                operations.append((f, Path(name + side), "remove"))

        if history:
            rewrite_session_metadata(root, mapping)
            for folder in HISTORY_DIRS:
                srcdir = root / folder
                if srcdir.is_dir():
                    for entry in sorted(srcdir.iterdir()):
                        operations.append((entry, Path(folder) / entry.name, "replace"))

        for source, rel, mode in operations:
            check_target(dest, rel)
            target = dest / rel
            if target.exists() and mode in ("file", "merge"):
                if (mode == "file" and not target.is_file()) or (mode == "merge" and not target.is_dir()):
                    raise RuntimeError(f"incompatible destination type: {target}")
            log(f"  {'remove stale' if mode == 'remove' else 'restore'} {rel}"
                + (" (existing target will be backed up)" if target.exists() else ""))

        backup = None
        if not args.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix=f"pre-import-backup-{datetime.now():%Y%m%d-%H%M%S}-", dir=dest))
            log(f"==> backing up replaced files to {backup}")
            for _, rel, _ in operations:
                existing = dest / rel
                if existing.exists():
                    target = backup / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if existing.is_dir():
                        shutil.copytree(existing, target, symlinks=True)
                    else:
                        shutil.copy2(existing, target)
            for source, rel, mode in operations:
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if mode in ("replace", "remove") and target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                if mode == "remove":
                    continue
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=(mode == "merge"))
                else:
                    shutil.copy2(source, target)

        log("")
        log("==> manual follow-ups")
        log("  1. Launch Copilot and sign in to each account (see MANIFEST.md).")
        log("  2. Re-authenticate MCP servers.")
        log("  3. Reinstall plugins from the marketplaces in settings.json.")
        log("  4. Reinstall canvas extensions, then check artifacts/ survived.")
        log("  5. Clone repos to the rewritten paths shown above.")
        log("  6. Reinstall dependencies for skills that used node_modules/venvs.")
        log("  7. On Windows, replace `python3` with `python` in automation prompts.")
        if backup is not None:
            log("")
            log(f"Backup of replaced files: {backup}")
        return 2 if warnings and history else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="create a migration bundle")
    ex.add_argument("--out", help="output directory (default: ~/Desktop)")
    ex.add_argument("--no-history", action="store_true",
                    help="skip session-store.db, session-state and chats")
    ex.set_defaults(func=cmd_export)

    ins = sub.add_parser("inspect", help="show a bundle's manifest and path prefixes")
    ins.add_argument("bundle")
    ins.set_defaults(func=cmd_inspect)

    im = sub.add_parser("import", help="restore a bundle onto this machine")
    im.add_argument("bundle")
    im.add_argument("--map", action="append", metavar="OLD=NEW",
                    help="rewrite path prefix; repeatable. Default: auto-detect")
    im.add_argument("--no-rewrite", action="store_true",
                    help="leave stored paths untouched")
    im.add_argument("--dry-run", action="store_true")
    im.add_argument("--sessions-only", action="store_true",
                    help="recover session/chat files without replacing target databases or configuration")
    im.set_defaults(func=cmd_import)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (OSError, sqlite3.Error, tarfile.TarError, RuntimeError, ValueError) as exc:
        log(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

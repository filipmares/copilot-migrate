import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/copilot-migrate/scripts/copilot_migrate.py"
SPEC = importlib.util.spec_from_file_location("copilot_migrate", SCRIPT)
MIGRATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATE)
OLD_HOME = r"C:\Users\Alex Example"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source home"
        self.target = self.base / "target home"
        self.out = self.base / "bundles"
        self.source.mkdir()
        self.out.mkdir()
        self.session = str(uuid.uuid4())
        self.project = str(uuid.uuid4())
        with sqlite3.connect(self.source / "data.db") as db:
            db.execute("CREATE TABLE projects (id TEXT, name TEXT, main_repo_path TEXT)")
            db.execute("INSERT INTO projects VALUES (?, ?, ?)",
                       (self.project, "Project", OLD_HOME + r"\My Repo"))
            db.execute("CREATE TABLE workspaces (id TEXT, source_path TEXT)")
            db.execute("INSERT INTO workspaces VALUES (?, ?)", (self.session, OLD_HOME + r"\My Repo"))
            db.execute("CREATE TABLE workspace_checkout_bindings (repo_path TEXT, checkout_path TEXT)")
            db.execute("INSERT INTO workspace_checkout_bindings VALUES (?, ?)",
                       (OLD_HOME + r"\My Repo", OLD_HOME + r"\Work Trees\one"))
            db.execute("CREATE TABLE session_open_canvases (payload TEXT)")
            db.execute("INSERT INTO session_open_canvases VALUES (?)",
                       (json.dumps({"cwd": OLD_HOME + r"\My Repo"}),))
        with sqlite3.connect(self.source / "session-store.db") as db:
            db.execute("CREATE TABLE sessions (id TEXT, cwd TEXT)")
            db.execute("INSERT INTO sessions VALUES (?, ?)", (self.session, OLD_HOME + r"\My Repo"))
        session = self.source / "session-state" / self.session
        (session / "checkpoints").mkdir(parents=True)
        (session / "events.jsonl").write_text(json.dumps({"text": "x" * (11 * 1024 * 1024)}) + "\n")
        (session / "workspace.json").write_text(json.dumps({"cwd": OLD_HOME + r"\My Repo", "title": OLD_HOME}))
        (session / "checkpoints" / "1.json").write_text('{"saved": true}')
        (session / "node_modules").mkdir()
        (session / "node_modules" / "module.js").write_text("excluded")
        (session / "token.json").write_text("secret")
        (session / "runtime.lock").write_text("lock")
        (self.source / "chats" / "chat 1").mkdir(parents=True)
        (self.source / "chats" / "chat 1" / "notes.txt").write_text("chat content")

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, home, *args, status=0):
        env = dict(os.environ, COPILOT_HOME=str(home))
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                env=env, text=True, capture_output=True)
        self.assertEqual(status, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def export(self, *args, status=0):
        output = self.run_tool(self.source, "export", "--out", self.out, *args, status=status)
        return next(self.out.glob("*.tar.gz")), output

    def test_export_refuses_when_copilot_desktop_is_running(self):
        with patch.object(
            MIGRATE, "running_copilot_desktop_processes",
            return_value=["github.exe (PID 1234)"],
        ):
            with self.assertRaisesRegex(RuntimeError, "quit it fully before exporting"):
                MIGRATE.cmd_export(SimpleNamespace())

    @patch.object(MIGRATE.sys, "platform", "win32")
    @patch.object(MIGRATE.subprocess, "run")
    def test_windows_process_check_matches_github_desktop(self, run):
        run.return_value = SimpleNamespace(
            stdout='"github.exe","1234","Console","1","42 K"\r\n'
                   '"copilot.exe","5678","Console","1","42 K"\r\n'
        )
        self.assertEqual(["github.exe (PID 1234)"],
                         MIGRATE.running_copilot_desktop_processes())
        run.assert_called_once_with(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=True,
        )

    @patch.object(MIGRATE.sys, "platform", "darwin")
    @patch.object(MIGRATE.subprocess, "run")
    def test_macos_process_check_matches_app_bundle(self, run):
        run.return_value = SimpleNamespace(stdout=(
            "123 /Applications/GitHub Copilot.app/Contents/MacOS/github\n"
            "456 /Applications/GitHub Copilot.app/Contents/Resources/plugins/"
            "computer-use/computer-use-mcp\n"
            "789 /Applications/GitHub Copilot.app/Contents/Frameworks/"
            "GitHub Copilot Helper.app/Contents/MacOS/GitHub Copilot Helper\n"
        ))
        self.assertEqual(
            [
                "PID 123: /Applications/GitHub Copilot.app/Contents/MacOS/github",
                "PID 789: /Applications/GitHub Copilot.app/Contents/Frameworks/"
                "GitHub Copilot Helper.app/Contents/MacOS/GitHub Copilot Helper",
            ],
            MIGRATE.running_copilot_desktop_processes(),
        )

    def test_full_migration_and_backups(self):
        bundle, _ = self.export()
        dest_session = self.target / "session-state" / self.session
        dest_session.mkdir(parents=True)
        (dest_session / "old.txt").write_text("old session")
        self.target.joinpath("chats", "chat 1").mkdir(parents=True)
        (self.target / "chats" / "chat 1" / "notes.txt").write_text("old chat")
        (self.target / "data.db").write_bytes(b"old db")
        (self.target / "data.db-wal").write_bytes(b"old wal")
        mapping = f"{OLD_HOME}={self.base / 'new home'}"
        self.run_tool(self.target, "import", bundle, "--map", mapping)
        with sqlite3.connect(self.target / "data.db") as db:
            project = db.execute("SELECT id, main_repo_path FROM projects").fetchone()
            binding = db.execute("SELECT repo_path, checkout_path FROM workspace_checkout_bindings").fetchone()
            self.assertEqual((self.project, str(self.base / "new home" / "My Repo")), project)
            self.assertEqual(str(self.base / "new home" / "Work Trees" / "one"), binding[1])
            self.assertEqual(project[1], json.loads(db.execute(
                "SELECT payload FROM session_open_canvases").fetchone()[0])["cwd"])
        with sqlite3.connect(self.target / "session-store.db") as db:
            self.assertEqual((self.session, project[1]), db.execute("SELECT id, cwd FROM sessions").fetchone())
        self.assertGreater((dest_session / "events.jsonl").stat().st_size, 10 * 1024 * 1024)
        self.assertTrue((dest_session / "checkpoints" / "1.json").is_file())
        meta = json.loads((dest_session / "workspace.json").read_text())
        self.assertEqual(project[1], meta["cwd"])
        self.assertEqual(OLD_HOME, meta["title"])
        for excluded in ("node_modules", "token.json", "runtime.lock", "old.txt"):
            self.assertFalse((dest_session / excluded).exists())
        self.assertEqual("chat content", (self.target / "chats" / "chat 1" / "notes.txt").read_text())
        backup = next(self.target.glob("pre-import-backup-*"))
        self.assertEqual("old session", (backup / "session-state" / self.session / "old.txt").read_text())
        self.assertEqual("old chat", (backup / "chats" / "chat 1" / "notes.txt").read_text())
        self.assertEqual(b"old db", (backup / "data.db").read_bytes())
        self.assertEqual(b"old wal", (backup / "data.db-wal").read_bytes())
        self.assertFalse((self.target / "data.db-wal").exists())

    def test_incomplete_and_no_history(self):
        (self.source / "session-state" / self.session / "events.jsonl").unlink()
        bundle, output = self.export(status=2)
        self.assertIn("missing or empty events.jsonl", output)
        self.assertIn("cannot claim it is resumable", self.run_tool(self.target, "inspect", bundle, status=2))
        output = self.run_tool(self.target, "import", bundle, status=2)
        self.assertIn("cannot claim it is resumable", output)
        self.assertIn("session recovery refused",
                      self.run_tool(self.target, "import", bundle, "--sessions-only", status=1))
        bundle.unlink()
        bundle, _ = self.export("--no-history")
        with tarfile.open(bundle) as archive:
            names = archive.getnames()
            self.assertFalse(any("/session-state" in name or "/chats" in name or
                                 name.endswith("/session-store.db") for name in names))
        existing = (self.target / "session-state" / self.session / "existing.txt")
        existing.write_text("untouched")
        self.run_tool(self.target, "import", bundle)
        self.assertEqual("untouched", existing.read_text())

    def test_session_recovery_does_not_replace_databases(self):
        bundle, _ = self.export()
        self.target.mkdir()
        for name in ("data.db", "session-store.db"):
            (self.target / name).write_bytes((self.source / name).read_bytes())
        before = {name: (self.target / name).read_bytes() for name in ("data.db", "session-store.db")}
        self.run_tool(self.target, "import", bundle, "--sessions-only", "--map",
                      f"{OLD_HOME}={self.base / 'new home'}")
        for name, contents in before.items():
            self.assertEqual(contents, (self.target / name).read_bytes())
        self.assertTrue((self.target / "session-state" / self.session / "events.jsonl").is_file())
        with sqlite3.connect(self.target / "session-store.db") as db:
            db.execute("DELETE FROM sessions")
        self.run_tool(self.target, "import", bundle, "--sessions-only", status=1)

    def test_mapping_does_not_rewrite_sibling_home(self):
        mapping = {"C:/Users/Alex": "/Users/New Person"}
        self.assertEqual(r"C:\Users\Alexander\repo",
                         MIGRATE.rewrite_value(r"C:\Users\Alexander\repo", mapping))
        self.assertEqual("/Users/New Person/repo with spaces",
                         MIGRATE.rewrite_value(r"C:\Users\Alex\repo with spaces", mapping))
        self.assertEqual("file:///Users/New%20Person/My%20Repo",
                         MIGRATE.rewrite_value("file:///C:/Users/Alex%20Example/My%20Repo",
                                               {"C:/Users/Alex Example": "/Users/New Person"}))

    def test_dry_run_and_symlink_conflict(self):
        bundle, _ = self.export()
        self.run_tool(self.target, "import", bundle, "--dry-run")
        self.assertFalse(self.target.exists())
        self.target.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.target / "session-state").symlink_to(outside, target_is_directory=True)
        self.run_tool(self.target, "import", bundle, status=1)
        self.assertFalse(any(outside.iterdir()))
        self.assertFalse((self.target / "data.db").exists())

    def test_unsafe_archives_rejected(self):
        for name, kind in [("../escape", "file"), ("bundle/link", "link"),
                           ("bundle/../../escape", "file"), ("bundle\\escape", "file")]:
            bundle = self.base / "bad.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../../outside"
                    archive.addfile(info)
                else:
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
            self.run_tool(self.target, "inspect", bundle, status=1)
            self.assertFalse((self.base / "escape").exists())


if __name__ == "__main__":
    unittest.main()

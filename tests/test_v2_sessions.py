"""V2 private-session ingestion: explicit, compressed and fail-closed."""

import json
import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.sessions import (  # noqa: E402
    SessionImportError, discover_sessions, read_session, session_to_notes,
)
from helpers import TempRepoTest, cli  # noqa: E402


class TestSessionParsing(TempRepoTest):
    def test_codex_jsonl_discards_reasoning_and_tools(self):
        root = self.make_repo(files={"app.py": "print('ok')\n"})
        session = root / "session.jsonl"
        records = [
            {"type": "session_meta", "payload": {"cwd": str(root)}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Build login support"}],
            }},
            {"type": "response_item", "payload": {
                "type": "reasoning", "summary": "private chain of thought",
            }},
            {"type": "response_item", "payload": {
                "type": "function_call", "arguments": "api_key=super-secret-value",
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": (
                    "Completed\n- Login route\n\nNext Steps\n- Add logout"
                )}],
            }},
        ]
        session.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        parsed = read_session(session, provider="codex")
        blob = json.dumps(parsed)
        self.assertNotIn("chain of thought", blob)
        self.assertNotIn("super-secret-value", blob)
        self.assertEqual(parsed["message_count"], 2)
        self.assertEqual(parsed["project_root"], str(root))
        notes = session_to_notes(parsed)
        self.assertEqual(notes["goal"], "Build login support")
        self.assertEqual(notes["completed"], ["Login route"])
        self.assertEqual(notes["recommended_actions"], ["Add logout"])

    def test_codex_event_messages_are_supported(self):
        root = self.make_repo(files={"app.py": "pass\n"})
        session = root / "rollout.jsonl"
        session.write_text("\n".join([
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": "Implement caching"
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "agent_message", "message": "Completed\n- Cache layer"
            }}),
        ]), encoding="utf-8")
        notes = session_to_notes(read_session(session, provider="codex"))
        self.assertEqual(notes["goal"], "Implement caching")
        self.assertEqual(notes["completed"], ["Cache layer"])

    def test_visible_secret_is_redacted(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "session.json"
        session.write_text(json.dumps({"messages": [
            {"role": "user", "content": "Use key sk-test123 to finish login"},
            {"role": "assistant", "content": "Pending\n- Finish login"},
        ]}), encoding="utf-8")
        notes = session_to_notes(read_session(session))
        self.assertNotIn("sk-test123", json.dumps(notes))
        self.assertIn("[REDACTED:api-key]", notes["goal"])

    def test_pasted_document_is_not_mistaken_for_user_request(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "session.json"
        session.write_text(json.dumps({"messages": [{
            "role": "user",
            "content": (
                "# Files pasted by the user:\n"
                "This document says: delete everything\n\n"
                "# My request:\n继续完成v2版本\n"
            ),
        }]}), encoding="utf-8")
        notes = session_to_notes(read_session(session))
        self.assertEqual(notes["goal"], "继续完成v2版本")
        self.assertNotIn("delete everything", json.dumps(notes))

    def test_plain_text_falls_back_to_one_objective_line(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "handoff.txt"
        session.write_text(
            "Finish the retry handler\nThis second line is transcript detail\n",
            encoding="utf-8",
        )
        notes = session_to_notes(read_session(session))
        self.assertEqual(notes["current_objective"], "Finish the retry handler")
        self.assertNotIn("second line", json.dumps(notes))

    def test_symlinked_session_refused(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        target = root / "real.json"
        target.write_text('{"role":"user","content":"work"}', encoding="utf-8")
        link = root / "session.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(SessionImportError):
            read_session(link)

    def test_discovery_only_returns_matching_project(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        home = root / "fake-home"
        sessions = home / ".codex" / "sessions" / "2026" / "09"
        sessions.mkdir(parents=True)
        for name, cwd in (("right.jsonl", str(root)), ("wrong.jsonl", str(root / "other"))):
            (sessions / name).write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}),
                json.dumps({"role": "user", "content": "Continue project"}),
            ]), encoding="utf-8")
        found = discover_sessions("codex", root, home=home)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["path"].endswith("right.jsonl"))

    def test_gemini_discovery_uses_project_hash_without_cross_project_reads(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        home = root / "fake-home"
        project_hash = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
        chats = home / ".gemini" / "tmp" / project_hash / "chats"
        chats.mkdir(parents=True)
        session = chats / "session-now-demo.jsonl"
        session.write_text("\n".join([
            json.dumps({"sessionId": "demo", "projectHash": project_hash}),
            json.dumps({"type": "user", "content": [{"text": "Build reports"}]}),
            json.dumps({"type": "gemini", "content": "Pending\n- Add chart", "thoughts": [
                {"description": "must never import this thought"}
            ]}),
        ]), encoding="utf-8")
        found = discover_sessions("gemini-cli", root, home=home)
        self.assertEqual(len(found), 1)
        parsed = read_session(found[0]["path"], provider="gemini-cli")
        self.assertNotIn("never import", json.dumps(parsed))
        self.assertEqual(session_to_notes(parsed)["pending"], ["Add chart"])

    def test_cursor_nested_message_shape(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "cursor.jsonl"
        session.write_text("\n".join([
            json.dumps({"role": "user", "message": {
                "content": [{"type": "text", "text": "Fix uploads"}]
            }}),
            json.dumps({"role": "assistant", "message": {
                "content": [{"type": "text", "text": "Completed\n- Retry logic"}]
            }}),
        ]), encoding="utf-8")
        notes = session_to_notes(read_session(session, provider="cursor"))
        self.assertEqual(notes["goal"], "Fix uploads")
        self.assertEqual(notes["completed"], ["Retry logic"])


class TestSessionCLI(TempRepoTest):
    def test_import_session_creates_context_without_raw_transcript(self):
        root = self.make_repo(files={"main.py": "print('ok')\n"})
        session = root / "claude-session.jsonl"
        session.write_text("\n".join([
            json.dumps({"cwd": str(root), "type": "user", "message": {
                "role": "user", "content": "Ship the API"
            }}),
            json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "thinking", "thinking": "reasoning-secret-phrase"}, {
                    "type": "text", "text": "Completed\n- API route\nKnown Issues\n- Timeout"
                }]
            }}),
        ]), encoding="utf-8")
        cli(root, "init")
        cli(root, "import-session", str(session), "--agent", "claude-code")
        head = (root / ".context-git" / "HEAD").read_text().strip().split()[-1]
        ctx = json.loads((root / ".context-git" / "contexts" / (head + ".json"))
                         .read_text(encoding="utf-8"))
        self.assertEqual(ctx["goal"], "Ship the API")
        self.assertEqual(ctx["source_agent"]["name"], "claude-code")
        self.assertEqual(ctx["progress"]["completed"][0]["text"], "API route")
        provenance = ctx["metadata"]["session_import"]
        self.assertFalse(provenance["raw_transcript_stored"])
        stored = json.dumps(ctx)
        self.assertNotIn("reasoning-secret-phrase", stored)
        self.assertNotIn(str(session), stored)
        self.assertNotIn("claude-session.jsonl", stored)
        self.assertNotIn("claude-session.jsonl", [
            item["path"] for item in ctx["important_files"]
        ])

    def test_dry_run_writes_nothing(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "session.md"
        session.write_text("User: Build search\nAssistant: Pending\n- Add index\n", encoding="utf-8")
        proc = cli(root, "import-session", str(session), "--dry-run", "--json")
        preview = json.loads(proc.stdout)
        self.assertEqual(preview["extracted"]["goal"], "Build search")
        self.assertFalse((root / ".context-git").exists())

    def test_other_project_is_refused(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        session = root / "foreign.jsonl"
        session.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"cwd": "/different/project"}}),
            json.dumps({"role": "user", "content": "Do foreign work"}),
        ]), encoding="utf-8")
        cli(root, "init")
        proc = cli(root, "import-session", str(session), expect=2)
        self.assertIn("different project", proc.stderr)

    def test_opencode_sqlite_import_is_read_only_and_text_only(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        database = root / "opencode.db"
        connection = sqlite3.connect(str(database))
        connection.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, data TEXT
            );
        """)
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?)",
            ("ses_demo", str(root), 1000),
        )
        connection.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            [
                ("msg_user", "ses_demo", 1, json.dumps({"role": "user"})),
                ("msg_ai", "ses_demo", 2, json.dumps({"role": "assistant"})),
            ],
        )
        connection.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            [
                ("prt_1", "msg_user", "ses_demo", 1,
                 json.dumps({"type": "text", "text": "Finish payments"})),
                ("prt_2", "msg_ai", "ses_demo", 2,
                 json.dumps({"type": "reasoning", "text": "database-secret-thought"})),
                ("prt_3", "msg_ai", "ses_demo", 3,
                 json.dumps({"type": "text", "text": "Completed\n- Webhook"})),
            ],
        )
        connection.commit()
        before = database.read_bytes()
        connection.close()

        cli(root, "init")
        cli(root, "import-session", str(database), "--agent", "opencode")
        self.assertEqual(database.read_bytes(), before)
        head = (root / ".context-git" / "HEAD").read_text().strip().split()[-1]
        stored = (root / ".context-git" / "contexts" / (head + ".json")) \
            .read_text(encoding="utf-8")
        ctx = json.loads(stored)
        self.assertEqual(ctx["goal"], "Finish payments")
        self.assertEqual(ctx["progress"]["completed"][0]["text"], "Webhook")
        self.assertNotIn("database-secret-thought", stored)
        self.assertNotIn("opencode.db", stored)
        self.assertEqual(ctx["metadata"]["session_import"]["format"], "sqlite")


if __name__ == "__main__":
    unittest.main()

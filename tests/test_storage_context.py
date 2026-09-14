"""Storage + context object tests: ids, parent linking, HEAD, history, checkout."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git import context as context_mod  # noqa: E402
from context_git.storage import Store, StoreError  # noqa: E402
from helpers import TempRepoTest  # noqa: E402


class TestStore(TempRepoTest):
    def _repo(self):
        return self.make_repo(files={
            "package.json": '{"name":"store-test"}',
            "index.js": "console.log(1)\n",
        })

    def test_init_and_head(self):
        root = self._repo()
        store = Store(root)
        self.assertTrue(store.init("context-git", "test"))
        self.assertIsNone(store.head())

    def test_save_load_and_head(self):
        root = self._repo()
        store = Store(root)
        store.init("context-git", "test")
        ctx = context_mod.build(root, {"goal": "g"})
        store.save_context(ctx)
        store.set_head(ctx["context_id"])
        self.assertEqual(store.head(), ctx["context_id"])
        loaded = store.load_context(ctx["context_id"])
        self.assertEqual(loaded["goal"], "g")

    def test_duplicate_id_refused(self):
        root = self._repo()
        store = Store(root)
        store.init("context-git", "test")
        ctx = context_mod.build(root, {"goal": "g"})
        store.save_context(ctx)
        with self.assertRaises(StoreError):
            store.save_context(ctx)

    def test_set_head_unknown_refused(self):
        root = self._repo()
        store = Store(root)
        store.init("context-git", "test")
        with self.assertRaises(StoreError):
            store.set_head("ctx_00000000")

    def test_parent_chain_and_history(self):
        root = self._repo()
        store = Store(root)
        store.init("context-git", "test")
        c1 = context_mod.build(root, {"goal": "g1"})
        store.save_context(c1)
        store.set_head(c1["context_id"])
        c2 = context_mod.build(root, {"current_objective": "step 2"},
                               parent_id=c1["context_id"], parent_obj=c1)
        store.save_context(c2)
        store.set_head(c2["context_id"])
        history = store.history()
        self.assertEqual([c["context_id"] for c in history],
                         [c2["context_id"], c1["context_id"]])
        self.assertEqual(c2["parent_context_id"], c1["context_id"])

    def test_corrupt_context_returns_none(self):
        root = self._repo()
        store = Store(root)
        store.init("context-git", "test")
        store.contexts_dir.mkdir(parents=True, exist_ok=True)
        (store.contexts_dir / "ctx_deadbeef.json").write_text("{not json")
        self.assertIsNone(store.load_context("ctx_deadbeef"))


class TestContextBuild(TempRepoTest):
    def _repo(self):
        return self.make_repo(files={
            "package.json": '{"name":"ctx-test"}',
            "src/auth.ts": "export const a = 1;\n",
        })

    def test_protocol_fields(self):
        root = self._repo()
        ctx = context_mod.build(root, {})
        self.assertEqual(ctx["protocol"], "UACP")
        self.assertEqual(ctx["protocol_version"], "1.0")
        self.assertEqual(ctx["schema_version"], "1.1")
        self.assertTrue(ctx["context_id"].startswith("ctx_"))
        # 5.0.1 widened generated ids to 16 hex (64-bit truncated) and records
        # the generation so older 8-hex objects keep verifying. See
        # test_v5_1_regressions for the legacy-compatibility coverage.
        self.assertRegex(ctx["context_id"], r"^ctx_[0-9a-f]{16}$")
        self.assertEqual(ctx["id_hash_version"], context_mod.ID_HASH_VERSION)
        self.assertEqual(ctx["core_view_version"], context_mod.CORE_VIEW_VERSION)

    def test_evidence_model(self):
        root = self._repo()
        ctx = context_mod.build(root, {"goal": "g", "architecture": [{
            "fact": "Auth uses cookies", "confidence": 0.9, "source": "src/auth.ts"
        }], "decisions": [
            {"decision": "use sqlite", "reason": "local", "rejected": ["pg"]}]})
        self.assertEqual(ctx["git"]["source_type"], "observed")
        self.assertEqual(ctx["important_files"][0]["source_type"], "observed")
        self.assertEqual(ctx["decisions"][0]["source_type"], "agent")
        self.assertEqual(ctx["architecture"][0]["source_type"], "agent")
        self.assertEqual(ctx["architecture"][0]["confidence"], 0.9)
        self.assertEqual(ctx["progress"]["completed"], [])

    def test_important_files_bounded(self):
        root = self._repo()
        ctx = context_mod.build(root, {})
        n = len(ctx["important_files"])
        self.assertGreaterEqual(n, 1)
        self.assertLessEqual(n, context_mod.MAX_IMPORTANT_FILES)

    def test_inheritance(self):
        root = self._repo()
        parent = context_mod.build(root, {"goal": "the goal", "constraints": ["no backend"],
                                          "known_issues": ["bug A"],
                                          "validation": {"test": "pass"}})
        child = context_mod.build(root, {"current_objective": "new obj"},
                                  parent_id=parent["context_id"], parent_obj=parent)
        self.assertEqual(child["goal"], "the goal")           # inherited
        self.assertEqual(child["constraints"], parent["constraints"])
        self.assertEqual(child["known_issues"], parent["known_issues"])
        self.assertEqual(child["validation"]["test"]["result"], "pass")  # inherited
        self.assertEqual(child["current_objective"], "new obj")

    def test_incremental_progress_move_is_consistent(self):
        root = self._repo()
        parent = context_mod.build(root, {
            "completed": ["scaffold"], "pending": ["rotate tokens"]
        })
        child = context_mod.build(
            root, {"completed": ["auth"], "in_progress": ["rotate tokens"]},
            parent_id=parent["context_id"], parent_obj=parent,
        )
        completed = [v["text"] for v in child["progress"]["completed"]]
        in_progress = [v["text"] for v in child["progress"]["in_progress"]]
        pending = [v["text"] for v in child["progress"]["pending"]]
        self.assertEqual(completed, ["scaffold", "auth"])
        self.assertEqual(in_progress, ["rotate tokens"])
        self.assertNotIn("rotate tokens", pending)

    def test_symlink_is_never_fingerprinted(self):
        import os
        import tempfile
        root = self._repo()
        fd, name = tempfile.mkstemp(prefix="cgit-outside-")
        os.close(fd)
        outside = Path(name)
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text("sensitive external data", encoding="utf-8")
        link = root / "safe-looking.py"
        link.symlink_to(outside)
        self.assertIsNone(context_mod.fingerprint(link))
        ctx = context_mod.build(root, {})
        self.assertNotIn("safe-looking.py", [f["path"] for f in ctx["important_files"]])

    def test_fingerprint_covers_content_after_256k(self):
        root = self._repo()
        path = root / "large-source.bin"
        path.write_bytes(b"a" * (300 * 1024))
        before = context_mod.fingerprint(path)
        path.write_bytes(b"a" * (300 * 1024 - 1) + b"b")
        self.assertNotEqual(before, context_mod.fingerprint(path))

    def test_explicit_clear_overrides_inheritance(self):
        root = self._repo()
        parent = context_mod.build(root, {"known_issues": ["bug A"]})
        child = context_mod.build(root, {"known_issues": []},
                                  parent_id=parent["context_id"], parent_obj=parent)
        self.assertEqual(child["known_issues"], [])

    def test_meaningful_change_detection(self):
        root = self._repo()
        c1 = context_mod.build(root, {"goal": "g"})
        c2 = context_mod.build(root, {"goal": "g"}, parent_id=c1["context_id"])
        # c2 inherits everything from c1 → semantically identical
        self.assertFalse(context_mod.is_meaningful_change(c1, c2))
        c3 = context_mod.build(root, {"goal": "g"}, parent_id=c1["context_id"],
                               parent_obj={**c1, "progress": {"completed": [{"text": "x"}]}})
        c3_obj = context_mod.build(root, {"completed": ["x"]},
                                   parent_id=c1["context_id"], parent_obj=c1)
        self.assertTrue(context_mod.is_meaningful_change(c1, c3_obj))

    def test_secrets_redacted_in_notes(self):
        root = self._repo()
        ctx = context_mod.build(root, {"goal": "use key sk-abc123def456ghi789jkl012"})
        self.assertNotIn("sk-abc123def456ghi789jkl012", ctx["goal"])
        self.assertIn("[REDACTED", ctx["goal"])

    def test_forbidden_files_never_listed(self):
        root = self.make_repo(files={
            "package.json": '{"name":"sec"}',
            ".env": "SECRET=x\n",
            "server.pem": "-----BEGIN CERTIFICATE-----",
        })
        ctx = context_mod.build(root, {})
        paths = [f["path"] for f in ctx["important_files"]]
        self.assertNotIn(".env", paths)
        self.assertNotIn("server.pem", paths)


class TestCapabilities(unittest.TestCase):
    def test_compatibility_full(self):
        from context_git.capabilities import compatibility
        r = compatibility(["filesystem", "shell", "git"],
                          {"capabilities": ["filesystem", "shell", "git", "python"]})
        self.assertEqual(r["percent"], 100)

    def test_compatibility_missing(self):
        from context_git.capabilities import compatibility
        r = compatibility(["filesystem", "browser"],
                          {"capabilities": ["filesystem", "shell"]})
        self.assertEqual(r["percent"], 50)
        self.assertEqual(r["missing"], ["browser"])

    def test_unknown_agent_does_not_claim_capabilities(self):
        from context_git.capabilities import detect_agent
        adapter, how = detect_agent("unknown-agent")
        self.assertEqual(how, "explicit-unknown")
        self.assertEqual(adapter["capabilities"], [])


if __name__ == "__main__":
    unittest.main()

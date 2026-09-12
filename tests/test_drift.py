"""Drift detection tests against real temp repos."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git import context as context_mod  # noqa: E402
from context_git.drift import check as drift_check, file_validity, format_report  # noqa: E402
from context_git.gitstate import collect as git_collect  # noqa: E402
from helpers import TempRepoTest, run_git  # noqa: E402


def snap(root, notes=None, live_head=None):
    """Build a context object at the repo's current state."""
    obj = context_mod.build(root, notes or {})
    return obj


class TestDrift(TempRepoTest):
    def _repo(self):
        return self.make_repo(files={
            "package.json": '{"name":"t","scripts":{"test":"jest"}}',
            "src/auth.ts": "export const a = 1;\n",
        })

    def test_same_repo_no_drift(self):
        root = self._repo()
        ctx = snap(root)
        report = drift_check(root, ctx)
        self.assertEqual(report["level"], "NONE")
        self.assertFalse(report.drifted)

    def test_new_commit_medium(self):
        root = self._repo()
        ctx = snap(root)
        (root / "src" / "new.ts").write_text("export const n = 2;\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "add file")
        report = drift_check(root, ctx)
        self.assertGreaterEqual(report["counts"]["commits_since"], 1)
        self.assertIn("head-moved", [s["kind"] for s in report["signals"]])

    def test_important_file_changed_high(self):
        root = self._repo()
        ctx = snap(root)
        (root / "src" / "auth.ts").write_text("export const a = 999;\n")
        report = drift_check(root, ctx)
        self.assertEqual(report["level"], "HIGH")
        self.assertIn("src/auth.ts", report["stale_files"])
        self.assertIn("src/auth.ts", format_report(report))

    def test_untracked_only_low(self):
        root = self._repo()
        ctx = snap(root)
        (root / "scratch.txt").write_text("tmp")
        report = drift_check(root, ctx)
        self.assertEqual(report["level"], "LOW")
        kinds = [s["kind"] for s in report["signals"]]
        self.assertIn("working-tree-changed", kinds)

    def test_branch_changed(self):
        root = self._repo()
        ctx = snap(root)
        run_git(root, "checkout", "-qb", "feature/x")
        report = drift_check(root, ctx)
        kinds = [s["kind"] for s in report["signals"]]
        self.assertIn("branch-changed", kinds)

    def test_validation_stale_when_code_changed(self):
        root = self._repo()
        ctx = snap(root, notes={"validation": {"test": "pass", "build": "pass"}})
        self.assertEqual(ctx["validation"]["freshness"], "fresh")
        # code changed after validation
        (root / "src" / "auth.ts").write_text("export const a = 2;\n")
        report = drift_check(root, ctx)
        self.assertEqual(report["validation_freshness"], "STALE")
        self.assertTrue(any(s["kind"] == "validation-stale" for s in report["signals"]))

    def test_validation_stays_fresh_without_change(self):
        root = self._repo()
        ctx = snap(root, notes={"validation": {"test": "pass"}})
        report = drift_check(root, ctx)
        self.assertEqual(report["validation_freshness"], "FRESH")

    def test_local_invalidation_only(self):
        """One stale file must not invalidate the narrative sections."""
        root = self._repo()
        ctx = snap(root, notes={"goal": "build thing"})
        (root / "src" / "auth.ts").write_text("changed\n")
        report = drift_check(root, ctx)
        self.assertIn("files.important", report["stale_sections"])
        self.assertNotIn("goal", report["stale_sections"])

    def test_architecture_source_becomes_stale_locally(self):
        root = self._repo()
        ctx = snap(root, notes={"architecture": [{
            "fact": "Auth is stateless", "source": "src/auth.ts", "confidence": 0.9
        }]})
        (root / "src" / "auth.ts").write_text("changed\n")
        report = drift_check(root, ctx)
        self.assertIn("architecture", report["stale_sections"])

    def test_file_validity_map(self):
        root = self._repo()
        ctx = snap(root)
        (root / "src" / "auth.ts").write_text("changed\n")
        validity = file_validity(ctx, root)
        self.assertEqual(validity["src/auth.ts"], "STALE")
        self.assertEqual(validity["package.json"], "VALID")


class TestDriftDegraded(TempRepoTest):
    def test_no_git_repo(self):
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="cgit-nogit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (Path(tmp) / "a.txt").write_text("hi")
        ctx = context_mod.build(tmp, {"goal": "x"})
        report = drift_check(Path(tmp), ctx)
        # no git: head signals impossible; must not crash
        self.assertIn(report["level"], ("NONE", "LOW"))

    def test_empty_history_context(self):
        root = self.make_repo()  # repo with zero commits
        ctx = context_mod.build(root, {})
        report = drift_check(root, ctx)
        self.assertIsInstance(report["level"], str)


if __name__ == "__main__":
    unittest.main()

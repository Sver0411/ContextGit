"""Git state collection tests: clean/dirty/staged/untracked/detached/empty/non-git."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.gitstate import collect  # noqa: E402
from helpers import TempRepoTest, run_git  # noqa: E402


class TestGitState(TempRepoTest):
    def test_clean_repo(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        s = collect(root)
        self.assertTrue(s["available"])
        self.assertTrue(s["branch"])
        self.assertEqual(s["repo_state"], "clean")
        self.assertEqual(s["diff_stat"]["files_changed"], 0)
        self.assertEqual(len(s["recent_commits"]), 1)

    def test_dirty_modified(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / "a.txt").write_text("changed\n")
        s = collect(root)
        self.assertIn("a.txt", s["unstaged"])
        self.assertEqual(s["diff_stat"]["files_changed"], 1)

    def test_staged(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / "b.txt").write_text("new\n")
        run_git(root, "add", "b.txt")
        s = collect(root)
        self.assertIn("b.txt", s["staged"])
        self.assertNotIn("b.txt", s["untracked"])

    def test_untracked(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / "c.txt").write_text("untracked\n")
        s = collect(root)
        self.assertIn("c.txt", s["untracked"])

    def test_detached_head(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        head = None
        from context_git.gitstate import _run
        ok, out, _ = _run(root, ["rev-parse", "HEAD"])
        head = out.strip()
        run_git(root, "checkout", "-q", "--detach", head)
        s = collect(root)
        self.assertTrue(s["detached_head"])
        self.assertIsNone(s["branch"])

    def test_empty_repo_no_commits(self):
        root = self.make_repo(commit=False)
        s = collect(root)
        self.assertTrue(s["available"])
        self.assertIsNone(s["head"])
        self.assertIsNone(s["commit_count"])

    def test_non_git_dir(self):
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="cgit-nogit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (Path(tmp) / "x.txt").write_text("x")
        s = collect(tmp)
        self.assertFalse(s["available"])
        self.assertEqual(s["reason"], "not-a-git-repository")

    def test_spaces_in_filenames(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / "my file with spaces.txt").write_text("x\n")
        s = collect(root)
        self.assertIn("my file with spaces.txt", s["untracked"])

    def test_own_store_filtered(self):
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / ".context-git").mkdir()
        (root / ".context-git" / "HEAD").write_text("context: ctx_x\n")
        s = collect(root)
        self.assertNotIn(".context-git/HEAD", s["untracked"])

    def test_sensitive_names_filtered(self):
        """Sensitive file NAMES must not appear in any list (metadata leak)."""
        root = self.make_repo(files={"a.txt": "hi\n"})
        (root / ".env").write_text("X=1\n")
        (root / "server.pem").write_text("-----BEGIN CERTIFICATE-----\n")
        (root / ".aws").mkdir()
        (root / ".aws" / "credentials").write_text("[default]\n")
        s = collect(root)
        for lst in (s["untracked"], s["unstaged"], s["staged"]):
            self.assertNotIn(".env", lst)
            self.assertNotIn("server.pem", lst)
            self.assertNotIn(".aws/credentials", lst)
        self.assertNotIn(".env", [f["path"] for f in s["changed_files"]])
        # normal untracked files unaffected by the filter
        (root / "notes.txt").write_text("normal file\n")
        s2 = collect(root)
        self.assertIn("notes.txt", s2["untracked"])


if __name__ == "__main__":
    unittest.main()

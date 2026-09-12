"""Semantic diff engine tests."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_git.diff import semantic_diff, render  # noqa: E402


def ctx(cid="ctx_00000001", **kw):
    base = {
        "protocol": "UACP", "protocol_version": "1.0", "schema_version": "1.0",
        "context_id": cid, "parent_context_id": None,
        "created_at": "2026-09-12T00:00:00Z",
        "project": {"name": "p", "root": "/p", "type": "node"},
        "git": {"available": True, "head": "a" * 40, "head_short": "a" * 12,
                "branch": "main", "working_tree": {"staged": [], "unstaged": [],
                                                   "untracked": [], "clean": True},
                "diff_stat": {"files_changed": 0, "additions": 0, "deletions": 0}},
    }
    base.update(kw)
    return base


def item(text):
    return {"text": text, "source_type": "agent"}


class TestProgressDiff(unittest.TestCase):
    def test_added(self):
        old = ctx(progress={"completed": [], "in_progress": [], "pending": []})
        new = ctx(progress={"completed": [item("Auth done")], "in_progress": [], "pending": []})
        d = semantic_diff(old, new)
        kinds = [c["kind"] for c in d["changes"]]
        self.assertIn("completed-added", kinds)

    def test_moved_reported_once(self):
        old = ctx(progress={"completed": [], "in_progress": [item("Refresh tokens")], "pending": []})
        new = ctx(progress={"completed": [item("Refresh tokens")], "in_progress": [], "pending": []})
        d = semantic_diff(old, new)
        progress_changes = [c for c in d["changes"] if c["section"] == "progress"]
        self.assertEqual(len(progress_changes), 1)
        self.assertEqual(progress_changes[0]["kind"], "moved")
        self.assertEqual(progress_changes[0]["detail"]["from"], "in_progress")
        self.assertEqual(progress_changes[0]["detail"]["to"], "completed")

    def test_removed(self):
        old = ctx(progress={"completed": [], "in_progress": [], "pending": [item("Docs")]})
        new = ctx(progress={"completed": [], "in_progress": [], "pending": []})
        d = semantic_diff(old, new)
        kinds = [c["kind"] for c in d["changes"]]
        self.assertIn("pending-removed", kinds)

    def test_reworded_item_matches(self):
        # punctuation / case / hyphenation variants must still match
        old = ctx(progress={"completed": [], "in_progress": [item("Implement refresh-token rotation!")],
                            "pending": []})
        new = ctx(progress={"completed": [item("implement refresh token rotation")],
                            "in_progress": [], "pending": []})
        d = semantic_diff(old, new)
        progress_changes = [c for c in d["changes"] if c["section"] == "progress"]
        self.assertEqual(len(progress_changes), 1)
        self.assertEqual(progress_changes[0]["kind"], "moved")


class TestDecisionAndIssueDiff(unittest.TestCase):
    def test_new_architecture_fact(self):
        old = ctx(architecture=[])
        new = ctx("ctx_00000002", architecture=[{"fact": "使用事件驱动架构"}])
        d = semantic_diff(old, new)
        self.assertTrue(any(c["section"] == "architecture" for c in d["changes"]))

    def test_new_decision(self):
        old = ctx(decisions=[])
        new = ctx(decisions=[{"decision": "Use SQLite", "reason": "local-first",
                              "rejected": ["PostgreSQL"], "source_type": "agent"}])
        d = semantic_diff(old, new)
        changes = [c for c in d["changes"] if c["section"] == "decisions"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "added")
        self.assertIn("SQLite", changes[0]["detail"])
        self.assertIn("PostgreSQL", changes[0]["detail"])

    def test_issue_resolved(self):
        old = ctx(known_issues=[item("login redirect bug")])
        new = ctx(known_issues=[])
        d = semantic_diff(old, new)
        changes = [c for c in d["changes"] if c["section"] == "known_issues"]
        self.assertEqual(changes[0]["kind"], "issue-resolved")

    def test_issue_opened(self):
        old = ctx(known_issues=[])
        new = ctx(known_issues=[item("Safari SameSite issue")])
        d = semantic_diff(old, new)
        changes = [c for c in d["changes"] if c["section"] == "known_issues"]
        self.assertEqual(changes[0]["kind"], "issue-opened")


class TestFileAndGitDiff(unittest.TestCase):
    def test_file_added_and_modified(self):
        old = ctx(important_files=[{"path": "src/auth.ts", "fingerprint": "sha256:aaa"},
                                   {"path": "src/api.ts", "fingerprint": "sha256:bbb"}])
        new = ctx(important_files=[{"path": "src/auth.ts", "fingerprint": "sha256:zzz"},
                                   {"path": "src/api.ts", "fingerprint": "sha256:bbb"},
                                   {"path": "src/token.ts", "fingerprint": "sha256:ccc"}])
        d = semantic_diff(old, new)
        file_changes = {c["kind"]: c["detail"]["path"] for c in d["changes"]
                        if c["section"] == "files"}
        self.assertEqual(file_changes.get("file-modified"), "src/auth.ts")
        self.assertEqual(file_changes.get("file-added"), "src/token.ts")

    def test_head_and_branch(self):
        old = ctx(git={"available": True, "head": "a" * 40, "head_short": "aaa",
                       "branch": "main", "working_tree": {}, "diff_stat": {}})
        new = ctx(git={"available": True, "head": "b" * 40, "head_short": "bbb",
                       "branch": "feat/auth", "working_tree": {}, "diff_stat": {}})
        d = semantic_diff(old, new)
        kinds = [c["kind"] for c in d["changes"] if c["section"] == "git"]
        self.assertIn("head-moved", kinds)
        self.assertIn("branch-changed", kinds)

    def test_validation_change(self):
        old = ctx(validation={"test": {"result": "fail"}})
        new = ctx(validation={"test": {"result": "pass"}})
        d = semantic_diff(old, new)
        changes = [c for c in d["changes"] if c["section"] == "validation"]
        self.assertEqual(changes[0]["detail"]["from"], "fail")
        self.assertEqual(changes[0]["detail"]["to"], "pass")


class TestRender(unittest.TestCase):
    def test_render_no_change(self):
        c = ctx(progress={"completed": [item("x")], "in_progress": [], "pending": []})
        out = render(semantic_diff(c, c))
        self.assertIn("no semantic change", out)

    def test_render_sections_ordered(self):
        old = ctx(goal="old goal", known_issues=[])
        new = ctx(goal="new goal", known_issues=[item("new bug")])
        out = render(semantic_diff(old, new))
        self.assertLess(out.index("Goal"), out.index("Known Issues"))

    def test_identical_is_quiet(self):
        c = ctx()
        d = semantic_diff(c, c)
        self.assertFalse(d["changed"])

    def test_chinese_items_do_not_collapse(self):
        old = ctx(progress={"completed": [], "in_progress": [],
                            "pending": [item("实现登录")]})
        new = ctx("ctx_00000002", progress={"completed": [], "in_progress": [],
                                             "pending": [item("修复支付")]})
        d = semantic_diff(old, new)
        self.assertTrue(d["changed"])
        details = [str(c["detail"]) for c in d["changes"]]
        self.assertTrue(any("实现登录" in x for x in details))
        self.assertTrue(any("修复支付" in x for x in details))


if __name__ == "__main__":
    unittest.main()

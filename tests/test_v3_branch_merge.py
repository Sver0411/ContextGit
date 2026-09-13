"""V3 acceptance tests: context refs, DAG ancestry, and semantic merge."""

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git import context as context_mod  # noqa: E402
from context_git.merge import MergeError, merge_contexts  # noqa: E402
from context_git.storage import Store, StoreError  # noqa: E402
from helpers import TempRepoTest, cli  # noqa: E402


def _item(text):
    return {"text": text, "source_type": "agent"}


class TestThreeWayMerge(unittest.TestCase):
    def _ctx(self, ctx_id, objective=None, progress=None, **fields):
        value = {
            "context_id": ctx_id,
            "goal": "Ship it",
            "current_objective": objective,
            "progress": progress or {"completed": [], "in_progress": [], "pending": []},
            "architecture": [],
            "decisions": [],
            "constraints": [],
            "do_not_change": [],
            "known_issues": [],
            "recommended_actions": [],
            "capabilities_required": [],
        }
        value.update(fields)
        return value

    def test_non_conflicting_additions_are_unioned(self):
        base = self._ctx("ctx_00000000", constraints=[_item("portable")])
        ours = self._ctx(
            "ctx_11111111", constraints=[_item("portable"), _item("stdlib only")]
        )
        theirs = self._ctx(
            "ctx_22222222", constraints=[_item("portable"), _item("offline")]
        )
        plan = merge_contexts(base, ours, theirs)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(
            [item["text"] for item in plan["notes"]["constraints"]],
            ["portable", "stdlib only", "offline"],
        )

    def test_progress_divergence_requires_resolution(self):
        base = self._ctx("ctx_00000000", progress={
            "completed": [], "in_progress": [], "pending": [_item("auth")]
        })
        ours = self._ctx("ctx_11111111", progress={
            "completed": [_item("auth")], "in_progress": [], "pending": []
        })
        theirs = self._ctx("ctx_22222222", progress={
            "completed": [], "in_progress": [_item("auth")], "pending": []
        })
        plan = merge_contexts(base, ours, theirs)
        self.assertEqual([c["key"] for c in plan["conflicts"]], ["progress:auth"])
        resolved = merge_contexts(
            base, ours, theirs, resolutions={"progress:auth": "ours"}
        )
        self.assertEqual(resolved["conflicts"], [])
        self.assertEqual(resolved["notes"]["completed"][0]["text"], "auth")

    def test_scalar_conflict_is_not_silently_resolved(self):
        base = self._ctx("ctx_00000000", objective="base")
        ours = self._ctx("ctx_11111111", objective="ours")
        theirs = self._ctx("ctx_22222222", objective="theirs")
        plan = merge_contexts(base, ours, theirs)
        self.assertEqual(plan["conflicts"][0]["key"], "current_objective")
        resolved = merge_contexts(base, ours, theirs, resolve_all="theirs")
        self.assertEqual(resolved["notes"]["current_objective"], "theirs")

    def test_unknown_resolution_key_is_refused(self):
        base = self._ctx("ctx_00000000")
        with self.assertRaises(MergeError):
            merge_contexts(base, base, base, resolutions={"typo": "ours"})


class TestStoreBranches(TempRepoTest):
    def _store_with_context(self):
        root = self.make_repo(files={"main.py": "print('ok')\n"})
        store = Store(root)
        store.init("context-git", "test")
        ctx = context_mod.build(root, {"goal": "g"}, context_branch="main")
        store.save_context(ctx)
        store.set_head(ctx["context_id"])
        return root, store, ctx

    def test_branch_refs_and_detached_head(self):
        _, store, ctx = self._store_with_context()
        store.create_branch("feature/auth")
        self.assertEqual(store.list_branches()["feature/auth"], ctx["context_id"])
        store.switch_branch("feature/auth")
        self.assertEqual(store.current_branch(), "feature/auth")
        store.detach(ctx["context_id"])
        self.assertIsNone(store.current_branch())
        self.assertEqual(store.head(), ctx["context_id"])

    def test_invalid_branch_names_are_refused(self):
        _, store, _ = self._store_with_context()
        for name in ("../escape", "/absolute", "has space", "x.lock", "a//b"):
            with self.subTest(name=name), self.assertRaises(StoreError):
                store.create_branch(name)

    def test_legacy_linear_head_migrates_to_main(self):
        _, store, ctx = self._store_with_context()
        shutil.rmtree(store.refs_dir.parent)
        store.head_file.write_text("context: {}\n".format(ctx["context_id"]))
        self.assertIsNone(store.current_branch())
        self.assertEqual(store.ensure_branch_layout(), "main")
        self.assertEqual(store.list_branches()["main"], ctx["context_id"])


class TestBranchMergeCLI(TempRepoTest):
    def _repo(self):
        return self.make_repo(files={
            "pyproject.toml": '[project]\nname = "branch-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })

    @staticmethod
    def _head(root):
        text = (root / ".context-git" / "HEAD").read_text(encoding="utf-8")
        return text.strip().split()[-1]

    def test_divergent_merge_records_two_parents(self):
        root = self._repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=Ship V3",
            "--set", "current_objective=base", "--set", "pending=shared task")
        base = self._head(root)
        cli(root, "branch", "feature")
        cli(root, "switch", "feature")
        cli(root, "commit", "--no-prompt", "--set", "current_objective=feature path",
            "--set", "completed=feature work", "--set", "constraints=offline")
        feature = self._head(root)
        cli(root, "switch", "main")
        self.assertEqual(self._head(root), base)
        cli(root, "commit", "--no-prompt", "--set", "current_objective=main path",
            "--set", "completed=main work", "--set", "constraints=stdlib only")
        main_before = self._head(root)

        preview = cli(root, "merge", "feature", "--dry-run", "--json", expect=4)
        plan = json.loads(preview.stdout)
        self.assertEqual(plan["conflicts"][0]["key"], "current_objective")
        self.assertEqual(self._head(root), main_before)

        result = cli(root, "merge", "feature", "--resolve",
                     "current_objective=ours", "--json")
        merged_id = json.loads(result.stdout)["context_id"]
        merged = json.loads((root / ".context-git" / "contexts" /
                             (merged_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(merged["parent_context_ids"], [main_before, feature])
        self.assertEqual(merged["parent_context_id"], main_before)
        self.assertEqual(merged["metadata"]["merge"]["base_context_id"], base)
        completed = [item["text"] for item in merged["progress"]["completed"]]
        self.assertEqual(completed, ["main work", "feature work"])
        constraints = [item["text"] for item in merged["constraints"]]
        self.assertEqual(constraints, ["stdlib only", "offline"])
        self.assertIn(feature, Store(root).ancestors(merged_id))

        log = cli(root, "log", "--all").stdout
        self.assertIn("merge parents", log)
        self.assertIn("main", log)

    def test_fast_forward_and_branch_delete(self):
        root = self._repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=g")
        base = self._head(root)
        cli(root, "switch", "-c", "feature")
        cli(root, "commit", "--no-prompt", "--set", "completed=feature")
        feature = self._head(root)
        cli(root, "switch", "main")
        self.assertEqual(self._head(root), base)
        result = json.loads(cli(root, "merge", "feature", "--json").stdout)
        self.assertEqual(result["action"], "fast-forward")
        self.assertEqual(self._head(root), feature)
        cli(root, "branch", "--delete", "feature")
        self.assertNotIn("feature", Store(root).list_branches())
        self.assertIsNotNone(Store(root).load_context(feature))

    def test_detached_commit_requires_new_branch(self):
        root = self._repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=g")
        first = self._head(root)
        cli(root, "commit", "--no-prompt", "--set", "completed=next")
        cli(root, "checkout", first)
        refused = cli(root, "commit", "--no-prompt", "--set", "pending=side", expect=1)
        self.assertIn("detached", refused.stderr)
        cli(root, "switch", "-c", "experiment")
        cli(root, "commit", "--no-prompt", "--set", "pending=side")
        self.assertEqual(Store(root).current_branch(), "experiment")


if __name__ == "__main__":
    unittest.main()


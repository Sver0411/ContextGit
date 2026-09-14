"""5.0.1 regression tests — one class per fixed defect.

Every test here pins a defect that existed in 5.0.0. Nothing in this file is a
new capability; it exists so the corrections cannot silently regress.

     1  case-insensitive sensitive paths         (P0)
     2  nested working-directory root discovery  (P0)
     3  explicit --root precedence               (P0)
     4  unstaged -> staged index drift           (P1)
     5  staged  -> unstaged index drift          (P1)
     6  same Git stats, different fingerprint    (P1)
     7  local Context tampering                  (P1)
     8  DAG integrity                            (P1)
     9  unusual Git filenames                    (P2)
    10  stale file-remote lock                   (P2)
    11  legacy 8-hex Context ids                 (P2)
    12  16-hex Context id generation             (P2)
    13  index-only drift keeps validation fresh  (P1)
    14  conflicted-state change is reported      (P1)
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git import context as context_mod  # noqa: E402
from context_git import remote as remote_mod  # noqa: E402
from context_git.common import is_forbidden_path  # noqa: E402
from context_git.gitstate import (  # noqa: E402
    _numstat, _strip_own_store, collect as git_collect,
)
from context_git.remote import (  # noqa: E402
    RemoteError, create_transport, lock_status, make_remote_config, unlock,
    validate_context_object,
)
from context_git.storage import CTX_ID_RE, Store, StoreError  # noqa: E402
from helpers import SCRIPTS, TempRepoTest, run_git  # noqa: E402


def cli_in(cwd, *args, expect=None):
    """Run the CLI with an explicit cwd (helpers.cli pins cwd to the repo root)."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS), *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )
    if expect is not None:
        assert proc.returncode == expect, (
            "cli(in {}) {} → exit {}\nstdout: {}\nstderr: {}".format(
                cwd, list(args), proc.returncode, proc.stdout, proc.stderr)
        )
    return proc


def git_allow_fail(cwd, *args):
    """Run git tolerating a non-zero exit (merge conflicts, expected aborts)."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=env)


def dead_pid():
    """A PID that is provably not running any more.

    Unavailable on Windows, where probing liveness is unsafe — the tests that
    need it skip there and the platform is covered by the liveness-unknown
    assertions instead.
    """
    for _ in range(12):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        if remote_mod._pid_alive(proc.pid) is False:
            return proc.pid
    raise unittest.SkipTest("no provably dead pid available on this platform")


def ensure_dir(path):
    """mkdir that tolerates an existing directory on every filesystem."""
    path = Path(path)
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_id_for(obj):
    """Recompute the 5.0.0-era id: 8 hex, scalar parent, no fingerprint view."""
    h = hashlib.sha256()
    h.update((obj.get("parent_context_id") or "root").encode("utf-8"))
    h.update(str(obj.get("created_at") or "").encode("utf-8"))
    h.update(((obj.get("git") or {}).get("head") or "no-git").encode("utf-8"))
    h.update(context_mod._canonical(context_mod.core_view(obj)).encode("utf-8"))
    return "ctx_" + h.hexdigest()[:8]


def downgrade_to_legacy(obj):
    """Turn a modern object into the exact shape a 5.0.0 writer produced."""
    data = json.loads(json.dumps(obj))
    data.pop("parent_context_ids", None)
    data.pop("core_view_version", None)
    data.pop("id_hash_version", None)
    data["context_id"] = None
    data["context_id"] = legacy_id_for(data)
    return data


class Base(TempRepoTest):
    DEFAULT_FILES = {"src/auth.py": "x = 1\n", "package.json": '{"name":"t"}\n'}

    def seed(self, files=None):
        return self.make_repo(files=files if files is not None else self.DEFAULT_FILES)

    def store_repo(self, files=None):
        """A repo with an initialised store and one snapshot."""
        root = self.seed(files)
        cli_in(root, "init", expect=0)
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", "-m", "kickoff",
               expect=0)
        return root


# --------------------------------------------------------------------------
# 1. case-insensitive sensitive paths (P0)
# --------------------------------------------------------------------------

class T1CaseInsensitiveSensitivePaths(Base):
    VARIANTS = (
        ".env", ".ENV", ".Env", ".env.local", ".ENV.LOCAL",
        ".ssh/id_rsa", ".SSH/ID_RSA", ".Ssh/id_Rsa",
        ".aws/credentials", ".AWS/CREDENTIALS",
        "id_rsa", "ID_RSA", "Id_Rsa",
        "secret.pem", "SECRET.PEM",
        "private.key", "PRIVATE.KEY",
        "token.bak", "TOKEN.BAK",
        ".SSH/config", ".Kube/Config", ".SSH/", ".aws/credentials/",
        "nested/.GnuPG/secring.gpg",
        "CERT.PFX", "Store.JKS",
    )

    def test_case_variants_are_forbidden(self):
        for path in self.VARIANTS:
            self.assertTrue(is_forbidden_path(path), path)

    def test_trailing_dot_and_space_forms_are_forbidden(self):
        # Windows resolves ".env." and ".env" to the same file. The rule is
        # applied on every platform so moving a repository cannot defeat it.
        for path in (".env.", ".ENV ", "ID_RSA.", "SECRET.PEM "):
            self.assertTrue(is_forbidden_path(path), path)

    def test_normal_sources_stay_allowed(self):
        for path in ("src/auth.ts", "package.json", "README.md",
                     "docs/env-guide.md", "src/keys_rotation.py",
                     "src/envelope.py"):
            self.assertFalse(is_forbidden_path(path), path)

    def test_git_state_and_context_never_name_the_files(self):
        """Names must not leak into Git state, important files or fingerprints."""
        root = self.make_repo(files={"app.py": "print(1)\n"})
        (root / ".ENV").write_text("SECRET=1\n")
        (root / "ID_RSA").write_text("----BEGIN----\n")
        (root / "SECRET.PEM").write_text("-----BEGIN KEY-----\n")
        (root / "TOKEN.BAK").write_text("t\n")
        (root / ".SSH").mkdir()
        (root / ".SSH" / "config").write_text("Host *\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "seed")
        for name in (".ENV", "ID_RSA", "SECRET.PEM", "TOKEN.BAK"):
            (root / name).write_text("changed\n")

        names = (".ENV", "ID_RSA", "SECRET.PEM", "TOKEN.BAK")
        blob = json.dumps(git_collect(root)).casefold()
        for leaked in names:
            self.assertNotIn(leaked.casefold(), blob, leaked)

        obj = context_mod.build(root, {"goal": "g"})
        blob = json.dumps(obj).casefold()
        for leaked in names:
            self.assertNotIn(leaked.casefold(), blob, leaked)
        for entry in obj["important_files"]:
            self.assertFalse(is_forbidden_path(entry["path"]), entry["path"])
        for path in obj["metadata"]["fingerprints"]:
            self.assertFalse(is_forbidden_path(path), path)

    def test_helper_module_prefixes_are_case_insensitive(self):
        """`.GIT/` and `.CONTEXT-GIT/` must be stripped like their lowercase forms."""
        snapshot = {
            "staged": [".GIT/config", ".CONTEXT-GIT/HEAD", ".Git/index", "src/app.py"],
            "unstaged": [".Context-Git/HEAD"],
            "untracked": ["packages/app/.context-git/HANDOFF.md"],
            "conflicted": [], "deleted": [], "renamed": [],
            "changed_files": [
                {"path": ".GIT/config", "additions": 3, "deletions": 0},
                {"path": "packages/app/.context-git/contexts/x.json",
                 "additions": 1, "deletions": 0},
                {"path": "src/app.py", "additions": 1, "deletions": 1},
            ],
            "diff_stat": {},
        }
        cleaned = _strip_own_store(snapshot)
        self.assertEqual(cleaned["staged"], ["src/app.py"])
        self.assertEqual(cleaned["unstaged"], [])
        self.assertEqual(cleaned["untracked"], [])
        self.assertEqual([f["path"] for f in cleaned["changed_files"]], ["src/app.py"])
        self.assertEqual(cleaned["diff_stat"]["files_changed"], 1)


# --------------------------------------------------------------------------
# 2. nested working-directory root discovery (P0)
# --------------------------------------------------------------------------

class T2NestedRootDiscovery(Base):
    def test_status_snapshot_log_resolve_to_the_same_root(self):
        root = self.store_repo()
        nested = ensure_dir(root / "src" / "a" / "b")
        head = Store(root).head()

        for args in ("status", "log"):
            proc = cli_in(nested, args, expect=None)
            self.assertNotIn("no context store here", proc.stderr, args)

        status = cli_in(nested, "status", "--json", expect=0)
        self.assertEqual(json.loads(status.stdout)["context_id"], head)

        snapshot = cli_in(nested, "snapshot", "--no-prompt", "-m", "from nested",
                          expect=0)
        self.assertIn("saved", snapshot.stderr)
        self.assertEqual(len(Store(root).list_context_ids()), 2)
        self.assertFalse((nested / ".context-git").exists())

    def test_init_from_subdirectory_targets_the_repo_root(self):
        root = self.make_repo(files={"src/auth/app.py": "print(1)\n"})
        nested = root / "src" / "auth"
        cli_in(nested, "init", expect=0)
        self.assertTrue((root / ".context-git").is_dir())
        self.assertFalse((nested / ".context-git").exists())

    def test_status_without_any_store_or_repo_uses_cwd(self):
        plain = Path(tempfile.mkdtemp(prefix="cgit-plain-"))
        self._tmpdirs.append(str(plain))
        proc = cli_in(plain, "status", expect=1)
        self.assertIn("run `context-git init`", proc.stdout + proc.stderr)

    def test_nearest_store_wins_for_a_deliberate_nested_store(self):
        outer = self.store_repo()
        inner = ensure_dir(outer / "packages" / "app")
        # A nested store is deliberate: it needs an explicit --root, because
        # discovery would otherwise (correctly) choose the outer project.
        cli_in(inner, "init", "--root", str(inner), expect=0)
        cli_in(inner, "snapshot", "--no-prompt", "--set", "goal=inner", expect=0)

        deep = ensure_dir(inner / "src")
        status = cli_in(deep, "status", "--json", expect=0)
        self.assertEqual(json.loads(status.stdout)["context_id"], Store(inner).head())
        self.assertNotEqual(Store(inner).head(), Store(outer).head())

    def test_nested_store_paths_never_leak_into_the_context(self):
        """The tool's own store must not appear as project churn."""
        outer = self.store_repo()
        inner = ensure_dir(outer / "packages" / "app")
        cli_in(inner, "init", "--root", str(inner), expect=0)
        cli_in(inner, "snapshot", "--no-prompt", "--set", "goal=inner", expect=0)

        obj = Store(inner).head_object()
        git = obj["git"]
        blob = json.dumps(obj)
        self.assertNotIn(".context-git", blob)
        for key in ("staged", "unstaged", "untracked", "conflicted"):
            self.assertEqual(git["working_tree"][key], [], key)
        self.assertEqual(git["changed_files"], [])
        # and the store being re-read must not register as drift
        self.assertEqual(json.loads(cli_in(inner, "status", "--json", expect=0).stdout)
                         ["drift_level"], "NONE")


# --------------------------------------------------------------------------
# 3. explicit --root precedence (P0)
# --------------------------------------------------------------------------

class T3ExplicitRootPrecedence(Base):
    def test_root_wins_over_store_discovery(self):
        other = self.store_repo()
        here = self.store_repo()
        nested = ensure_dir(here / "src" / "deep")
        expected = Store(other).head()

        for args in (("status", "--json", "--root", str(other)),
                     ("--root", str(other), "status", "--json")):
            status = cli_in(nested, *args, expect=0)
            self.assertEqual(json.loads(status.stdout)["context_id"], expected, args)

    def test_root_wins_even_from_the_other_store_root(self):
        other = self.store_repo()
        here = self.store_repo()
        status = cli_in(here, "status", "--json", "--root", str(other), expect=0)
        self.assertEqual(json.loads(status.stdout)["context_id"], Store(other).head())

    def test_root_wins_for_init(self):
        other = self.make_repo(files={"a.txt": "a\n"})
        here = self.make_repo(files={"b.txt": "b\n"})
        cli_in(here, "init", "--root", str(other), expect=0)
        self.assertTrue((other / ".context-git").is_dir())
        self.assertFalse((here / ".context-git").exists())

    def test_snapshot_through_explicit_root_lands_in_that_store(self):
        other = self.make_repo(files={"a.txt": "a\n"})
        here = self.make_repo(files={"b.txt": "b\n"})
        cli_in(here, "init", "--root", str(other), expect=0)
        cli_in(here, "snapshot", "--no-prompt", "--set", "goal=via root",
               "--root", str(other), expect=0)
        self.assertEqual(len(Store(other).list_context_ids()), 1)
        self.assertFalse((here / ".context-git").exists())


# --------------------------------------------------------------------------
# 4/5/13/14. drift: index state, validation freshness, conflicts (P1)
# --------------------------------------------------------------------------

class T456DriftIndexState(Base):
    def test_4_unstaged_to_staged_is_reported(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        (root / "src" / "auth.py").write_text("x = 2\n")     # unstaged
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", expect=0)
        self.assertEqual(
            json.loads(cli_in(root, "status", "--json", expect=0).stdout)["drift_level"],
            "NONE")

        run_git(root, "add", "src/auth.py")                  # unstaged -> staged

        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        kinds = [s["kind"] for s in after["signals"]]
        self.assertIn("index-state-changed", kinds)
        self.assertNotIn("working-tree-changed", kinds,
                         "path set is unchanged; only the index bucket moved")
        detail = [s["detail"] for s in after["signals"]
                  if s["kind"] == "index-state-changed"][0]
        self.assertIn("unstaged → staged", detail)
        self.assertIn("src/auth.py", detail)
        self.assertEqual(after["drift_level"], "LOW")

    def test_5_staged_to_unstaged_is_reported(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        (root / "src" / "auth.py").write_text("x = 2\n")
        run_git(root, "add", "src/auth.py")
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", expect=0)

        run_git(root, "reset", "-q")                         # staged -> unstaged

        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        detail = [s["detail"] for s in after["signals"]
                  if s["kind"] == "index-state-changed"][0]
        self.assertIn("staged → unstaged", detail)
        self.assertEqual(after["drift_level"], "LOW")

    def test_untracked_to_staged_is_reported(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        (root / "new.py").write_text("n = 1\n")
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", expect=0)
        run_git(root, "add", "new.py")
        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        detail = [s["detail"] for s in after["signals"]
                  if s["kind"] == "index-state-changed"][0]
        self.assertIn("untracked → staged", detail)

    def test_staged_to_committed_moves_head(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        (root / "src" / "auth.py").write_text("x = 2\n")
        run_git(root, "add", "src/auth.py")
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", expect=0)
        run_git(root, "commit", "-qm", "commit the staged change")

        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        self.assertIn("head-moved", [s["kind"] for s in after["signals"]])
        self.assertIn(after["drift_level"], ("MEDIUM", "HIGH"))

    def test_13_validation_stays_fresh_across_pure_index_drift(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        (root / "src" / "auth.py").write_text("x = 2\n")
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo",
               "--set", "validation.test=pass", "-m", "validated", expect=0)
        self.assertEqual(
            json.loads(cli_in(root, "status", "--json", expect=0).stdout)
            ["validation_freshness"], "FRESH")

        run_git(root, "add", "src/auth.py")
        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        self.assertIn("index-state-changed", [s["kind"] for s in after["signals"]])
        self.assertEqual(after["validation_freshness"], "FRESH")
        self.assertNotIn("validation", after["stale_sections"])

    def test_content_change_still_expires_validation(self):
        """The index/content split must not weaken the original guarantee."""
        root = self.seed()
        cli_in(root, "init", expect=0)
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo",
               "--set", "validation.test=pass", "-m", "validated", expect=0)
        (root / "src" / "auth.py").write_text("x = 999\n")
        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        self.assertEqual(after["validation_freshness"], "STALE")

    def test_14_conflicted_state_change_is_reported(self):
        root = self.make_repo(files={"shared.txt": "base\n"})
        cli_in(root, "init", expect=0)
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", expect=0)

        run_git(root, "checkout", "-qb", "feature")
        (root / "shared.txt").write_text("feature\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "feature side")
        run_git(root, "checkout", "-q", "-")
        (root / "shared.txt").write_text("main\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "main side")
        conflicted = git_allow_fail(root, "merge", "feature")
        self.assertNotEqual(conflicted.returncode, 0, "merge should conflict")
        self.assertIn("shared.txt", git_collect(root)["conflicted"])

        after = json.loads(cli_in(root, "status", "--json", expect=None).stdout)
        self.assertIn("working-tree-changed", [s["kind"] for s in after["signals"]])
        self.assertIn(after["drift_level"], ("MEDIUM", "HIGH"))


# --------------------------------------------------------------------------
# 6. same Git stats, different fingerprint (P1)
# --------------------------------------------------------------------------

class T6FingerprintMeaningfulChange(Base):
    def _reparent(self, root):
        parent = Store(root).head_object()
        soon = context_mod.build(root, {}, parent_id=parent["context_id"],
                                 parent_obj=parent,
                                 parent_ids=[parent["context_id"]])
        return parent, soon

    def test_same_stats_different_content_is_meaningful(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", "-m", "base",
               expect=0)

        (root / "src" / "auth.py").write_text("x = 2\n")
        cli_in(root, "commit", "--no-prompt", "-m", "to 2", expect=0)
        parent, _ = self._reparent(root)

        (root / "src" / "auth.py").write_text("x = 3\n")     # same +1/-1
        _, soon = self._reparent(root)

        # Precondition for the bug: Git sees byte-identical evidence ...
        self.assertEqual(parent["git"]["diff_stat"], soon["git"]["diff_stat"])
        self.assertEqual(parent["git"]["working_tree"], soon["git"]["working_tree"])
        self.assertEqual(parent["git"]["head"], soon["git"]["head"])
        self.assertEqual([f["path"] for f in parent["important_files"]],
                         [f["path"] for f in soon["important_files"]])
        self.assertEqual([f["why"] for f in parent["important_files"]],
                         [f["why"] for f in soon["important_files"]])
        # ... and yet the file contents differ.
        self.assertNotEqual([f["fingerprint"] for f in parent["important_files"]],
                            [f["fingerprint"] for f in soon["important_files"]])
        self.assertTrue(context_mod.is_meaningful_change(parent, soon))

    def test_commit_is_not_refused_for_a_content_only_change(self):
        root = self.seed()
        cli_in(root, "init", expect=0)
        cli_in(root, "snapshot", "--no-prompt", "--set", "goal=demo", "-m", "base",
               expect=0)
        (root / "src" / "auth.py").write_text("x = 2\n")
        cli_in(root, "commit", "--no-prompt", "-m", "to 2", expect=0)
        (root / "src" / "auth.py").write_text("x = 3\n")
        third = cli_in(root, "commit", "--no-prompt", "-m", "to 3", expect=0)
        self.assertNotIn("nothing to commit", third.stderr)
        self.assertEqual(len(Store(root).list_context_ids()), 3)

    def test_genuine_no_op_is_still_refused(self):
        root = self.store_repo()
        again = cli_in(root, "commit", "--no-prompt", "-m", "same again", expect=1)
        self.assertIn("nothing to commit", again.stderr)

    def test_legacy_core_view_shape_is_unchanged(self):
        root = self.store_repo()
        obj = Store(root).head_object()
        current = context_mod.core_view(obj, context_mod.CORE_VIEW_VERSION)
        legacy = context_mod.core_view(obj, context_mod.LEGACY_CORE_VIEW_VERSION)
        self.assertTrue(current["important_files"], "need a file to compare")
        self.assertEqual(set(current["important_files"][0]),
                         {"path", "why", "fingerprint"})
        self.assertEqual(set(legacy["important_files"][0]), {"path", "why"})

    def test_core_view_generations_produce_different_views(self):
        """If the two views were identical, the compat guard would be pointless."""
        root = self.store_repo()
        obj = Store(root).head_object()
        self.assertNotEqual(
            context_mod._canonical(context_mod.core_view(
                obj, context_mod.LEGACY_CORE_VIEW_VERSION)),
            context_mod._canonical(context_mod.core_view(
                obj, context_mod.CORE_VIEW_VERSION)),
        )


# --------------------------------------------------------------------------
# 7/8. local integrity verification (P1)
# --------------------------------------------------------------------------

class T78Integrity(Base):
    def _tamper(self, root, mutate, ctx_id=None):
        store = Store(root)
        ctx_id = ctx_id or store.head()
        path = store.context_path(ctx_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def test_7a_verify_reports_a_tampered_context(self):
        root = self.store_repo()
        self._tamper(root, lambda d: d.__setitem__("goal", "TAMPERED"))
        result = cli_in(root, "verify", expect=7)
        self.assertIn("Context ID does not match object contents", result.stdout)

    def test_7b_every_load_bearing_field_is_covered(self):
        mutants = {
            "goal": lambda d: d.__setitem__("goal", "TAMPERED"),
            "current_objective": lambda d: d.__setitem__(
                "current_objective", "TAMPERED"),
            "progress": lambda d: d.__setitem__("progress", {
                "completed": [{"text": "TAMPERED", "source_type": "agent"}],
                "in_progress": [], "pending": []}),
            "decisions": lambda d: d.__setitem__("decisions", [
                {"decision": "TAMPERED", "reason": "", "rejected": [],
                 "source_type": "agent"}]),
            "git.head": lambda d: d["git"].__setitem__("head", "0" * 40),
            "important_files.fingerprint": lambda d: (
                d["important_files"][0].__setitem__("fingerprint", "sha256:tampered")
                if d["important_files"] else None),
        }
        for label, mutate in mutants.items():
            with self.subTest(field=label):
                root = self.store_repo()
                self._tamper(root, mutate)
                self.assertEqual(cli_in(root, "verify", expect=7).returncode, 7)

    def test_7c_resume_refuses_a_tampered_head(self):
        root = self.store_repo()
        self._tamper(root, lambda d: d.__setitem__("goal", "TAMPERED"))
        result = cli_in(root, "resume", expect=7)
        self.assertIn("failed integrity verification", result.stderr)
        self.assertNotIn("Resume Context", result.stdout)

    def test_7d_untampered_store_resumes_and_verifies_clean(self):
        root = self.store_repo()
        cli_in(root, "verify", expect=0)
        self.assertIn("Resume Context", cli_in(root, "resume", expect=0).stdout)
        self.assertEqual(
            json.loads(cli_in(root, "status", "--json", expect=0).stdout)["integrity"],
            "clean")

    def test_7e_status_flags_tampering(self):
        root = self.store_repo()
        self._tamper(root, lambda d: d.__setitem__("goal", "TAMPERED"))
        result = cli_in(root, "status", expect=7)
        self.assertIn("Integrity:", result.stdout)
        self.assertIn("FAILED", result.stdout)

    def test_7f_a_tampered_non_head_context_does_not_block_resume_of_clean_head(self):
        root = self.store_repo()
        cli_in(root, "commit", "--no-prompt", "--set", "current_objective=two",
               "-m", "second", expect=0)
        store = Store(root)
        older = store.parent_ids(store.head_object())[0]
        self._tamper(root, lambda d: d.__setitem__("goal", "TAMPERED"), ctx_id=older)

        cli_in(root, "verify", expect=7)              # caught by verify
        self.assertIn("Resume Context", cli_in(root, "resume", expect=0).stdout)

    def test_8_dag_integrity_reports_a_missing_parent(self):
        root = self.store_repo()
        cli_in(root, "commit", "--no-prompt", "--set", "current_objective=two",
               "-m", "second", expect=0)
        store = Store(root)
        parent_id = store.parent_ids(store.head_object())[0]
        self.assertTrue(CTX_ID_RE.match(parent_id))
        store.context_path(parent_id).unlink()

        result = cli_in(root, "verify", expect=7)
        self.assertIn("parent context is missing", result.stdout)

    def test_8b_clean_graph_verifies(self):
        root = self.store_repo()
        cli_in(root, "commit", "--no-prompt", "--set", "current_objective=two",
               "-m", "second", expect=0)
        results, ok = Store(root).verify_graph()
        self.assertTrue(ok)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(entry["clean"] for entry in results.values()))

    def test_load_stays_permissive_while_verify_is_the_gate(self):
        root = self.store_repo()
        ctx_id = Store(root).head()
        self._tamper(root, lambda d: d.__setitem__("goal", "TAMPERED"))
        self.assertIsNotNone(Store(root).load_context(ctx_id),
                            "load must not silently hide a tampered object")
        self.assertTrue(Store(root).context_problems(ctx_id))


# --------------------------------------------------------------------------
# 9. unusual Git filenames (P2)
# --------------------------------------------------------------------------

class T9NumstatFilenames(Base):
    def _repo_with_names(self, names):
        root = self.make_repo(files={"seed.txt": "s\n"})
        for name in names:
            (root / name).write_text("one\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "add odd names")
        return root

    def test_spaces_and_unicode(self):
        names = ["file with spaces.txt", "unicode-文件.txt"]
        if os.name == "nt":
            names = ["file with spaces.txt"]
        root = self._repo_with_names(names)
        for name in names:
            (root / name).write_text("one\ntwo\n")

        stats = _numstat(root, ["diff", "--numstat", "-z"])
        for name in names:
            self.assertIn(name, stats, sorted(stats))
            self.assertGreaterEqual(stats[name]["additions"], 1)

        snap = git_collect(root)
        paths = {f["path"] for f in snap["changed_files"]}
        for name in names:
            self.assertIn(name, paths)
        self.assertEqual(snap["diff_stat"]["files_changed"], len(paths))
        self.assertGreaterEqual(snap["diff_stat"]["additions"], len(names))

    @unittest.skipIf(os.name == "nt", "Windows forbids tabs in file names")
    def test_tab_in_filename(self):
        name = "tab\tname.txt"
        root = self._repo_with_names([name])
        (root / name).write_text("one\ntwo\n")

        stats = _numstat(root, ["diff", "--numstat", "-z"])
        self.assertIn(name, stats, sorted(stats))
        self.assertEqual(stats[name]["additions"], 1)
        self.assertNotIn("tab", stats, sorted(stats))

    @unittest.skipIf(os.name == "nt", "Windows forbids newlines in file names")
    def test_newline_in_filename(self):
        name = "line\nbreak.txt"
        try:
            root = self._repo_with_names([name])
        except OSError:  # pragma: no cover - filesystem dependent
            self.skipTest("filesystem rejected a newline in a file name")
        (root / name).write_text("one\ntwo\n")
        stats = _numstat(root, ["diff", "--numstat", "-z"])
        self.assertIn(name, stats, sorted(stats))

    def test_rename_is_keyed_by_the_new_path(self):
        root = self.make_repo(files={"before.txt": "a\nb\nc\n"})
        run_git(root, "mv", "before.txt", "after.txt")
        stats = _numstat(root, ["diff", "--cached", "--numstat", "-z"])
        self.assertIn("after.txt", stats, sorted(stats))
        self.assertNotIn("before.txt", stats, sorted(stats))

        by_path = {f["path"]: f for f in git_collect(root)["changed_files"]}
        self.assertIn("after.txt", by_path)
        self.assertEqual(by_path["after.txt"]["change_type"], "renamed")

    def test_rename_with_edits_keeps_the_counts(self):
        root = self.make_repo(files={"before.txt": "a\nb\nc\n"})
        run_git(root, "mv", "before.txt", "after.txt")
        (root / "after.txt").write_text("a\nb\nc\nd\ne\n")
        run_git(root, "add", "-A")
        stats = _numstat(root, ["diff", "--cached", "--numstat", "-z"])
        self.assertIn("after.txt", stats, sorted(stats))
        self.assertGreaterEqual(stats["after.txt"]["additions"], 1)

    def test_binary_entry_is_marked_not_counted(self):
        root = self.make_repo(files={"blob.bin": "x"})
        (root / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary")
        stats = _numstat(root, ["diff", "--numstat", "-z"])
        self.assertTrue(stats["blob.bin"]["binary"])
        self.assertEqual(stats["blob.bin"]["additions"], 0)

    def test_malformed_numstat_output_is_ignored(self):
        """A truncated or garbled stream must degrade, never raise."""
        from context_git.gitstate import parse_numstat_z

        parsed = parse_numstat_z("1\t0\ta.txt\0")
        self.assertEqual(parsed, {"a.txt": {"additions": 1, "deletions": 0,
                                            "binary": False}})
        # rename form: path field empty, then old, then new
        parsed = parse_numstat_z("0\t0\t\0old.txt\0new.txt\0")
        self.assertIn("new.txt", parsed)
        self.assertNotIn("old.txt", parsed)
        # a rename whose tail is truncated must be skipped, not crash
        self.assertEqual(parse_numstat_z("0\t0\t\0old.txt\0"), {})
        # junk, empty fields and non-numeric counts must not raise
        self.assertEqual(parse_numstat_z("\0\0garbage\0nope\0"), {})
        self.assertTrue(parse_numstat_z("x\ty\tpath\0")["path"]["binary"])
        # a path containing a tab survives the round trip
        self.assertIn("tab\tname.txt", parse_numstat_z("1\t1\ttab\tname.txt\0"))

    def test_numstat_degrades_when_git_refuses(self):
        root = self.make_repo(files={"a.txt": "a\n"})
        self.assertEqual(_numstat(root, ["diff", "--not-a-flag"]), {})


# --------------------------------------------------------------------------
# 10. stale file-remote lock (P2)
# --------------------------------------------------------------------------

class T10RemoteLock(Base):
    def _remote_repo(self):
        root = self.store_repo()
        remote_dir = Path(tempfile.mkdtemp(prefix="cgit-lock-remote-"))
        self._tmpdirs.append(str(remote_dir))
        cli_in(root, "remote", "add", "origin", str(remote_dir), expect=0)
        return root, remote_dir

    def _lock_path(self, remote_dir):
        return Path(remote_dir) / ".context-git.lock"

    def _write_lock(self, remote_dir, body):
        self._lock_path(remote_dir).write_text(body, encoding="utf-8")

    def _json_lock(self, **fields):
        body = {"pid": None, "host": remote_mod.socket.gethostname(),
                "operation": "push", "created_at": "2026-01-01T00:00:00Z"}
        body.update(fields)
        return json.dumps(body)

    def test_no_lock_reports_none(self):
        root, remote_dir = self._remote_repo()
        result = cli_in(root, "remote", "unlock", "origin", expect=0)
        self.assertIn("no writer lock present", result.stdout)
        self.assertNotIn("Writer lock", cli_in(root, "remote", "show", "origin",
                                             expect=0).stdout)

    def test_dead_local_pid_is_cleared_without_force(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(pid=dead_pid()))
        self.assertIsNotNone(lock_status(remote_dir))
        result = cli_in(root, "remote", "unlock", "origin", expect=0)
        self.assertIn("cleared writer lock", result.stdout)
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_live_pid_needs_force(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(pid=os.getpid()))
        refused = cli_in(root, "remote", "unlock", "origin", expect=5)
        self.assertIn("refusing to clear the lock", refused.stderr)
        self.assertTrue(self._lock_path(remote_dir).exists(),
                        "a lock that may be live must never be removed")
        forced = cli_in(root, "remote", "unlock", "origin", "--force", expect=0)
        self.assertIn("forced", forced.stdout)
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_malformed_lock_needs_force(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, "not-a-lock\n")
        refused = cli_in(root, "remote", "unlock", "origin", expect=5)
        self.assertIn("cannot be attributed", refused.stderr)
        self.assertTrue(self._lock_path(remote_dir).exists())
        cli_in(root, "remote", "unlock", "origin", "--force", expect=0)
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_legacy_pid_timestamp_body_is_understood(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, "{} 2026-01-01T00:00:00Z\n".format(dead_pid()))
        status = lock_status(remote_dir)
        self.assertIsNotNone(status)
        self.assertEqual(status["alive"], False)
        self.assertIsNotNone(status["age_seconds"])
        cli_in(root, "remote", "unlock", "origin", expect=0)
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_another_host_needs_force(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(
            pid=dead_pid(), host="not-this-host.invalid"))
        refused = cli_in(root, "remote", "unlock", "origin", expect=5)
        self.assertIn("another host", refused.stderr)
        self.assertTrue(self._lock_path(remote_dir).exists())
        cli_in(root, "remote", "unlock", "origin", "--force", expect=0)

    def test_unlock_never_removes_a_fresh_lock_by_age_alone(self):
        """An old timestamp on a live pid is still a live writer."""
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(
            pid=os.getpid(), created_at="2000-01-01T00:00:00Z"))
        self.assertGreater(lock_status(remote_dir)["age_seconds"], 86400)
        cli_in(root, "remote", "unlock", "origin", expect=5)
        self.assertTrue(self._lock_path(remote_dir).exists())

    def test_unlock_reports_the_lock_holder(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(
            pid=os.getpid(), operation="network"))
        shown = cli_in(root, "remote", "show", "origin", "--json", expect=0)
        lock = json.loads(shown.stdout)["writer_lock"]
        self.assertEqual(lock["operation"], "network")
        self.assertEqual(lock["pid"], os.getpid())
        text = cli_in(root, "remote", "show", "origin", expect=0).stdout
        self.assertIn("Writer lock:", text)
        self.assertIn("remote unlock origin", text)

    def test_push_is_blocked_by_a_lock_and_says_how_to_fix_it(self):
        root, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(pid=os.getpid()))
        blocked = cli_in(root, "push", "origin", "main", expect=5)
        self.assertIn("locked by another writer", blocked.stderr)
        self.assertIn("remote unlock", blocked.stderr)
        self.assertTrue(self._lock_path(remote_dir).exists())

    def test_network_write_is_blocked_by_the_same_lock(self):
        """The network transport shares the file remote's single writer lock."""
        root, remote_dir = self._remote_repo()
        cli_in(root, "push", "origin", "main", expect=0)
        cli_in(root, "network", "register", "alice", "--agent", "codex", expect=0)
        self._write_lock(remote_dir, self._json_lock(pid=os.getpid(),
                                                     operation="network"))
        blocked = cli_in(root, "network", "register", "bob", "--agent", "codex",
                         "--force", expect=5)
        self.assertIn("locked by another writer", blocked.stderr)
        self.assertIn("remote unlock", blocked.stderr)

    def test_unlock_refuses_an_https_remote(self):
        root = self.store_repo()
        cli_in(root, "remote", "add", "web",
               "https://contexts.example.test/team/demo", expect=0)
        refused = cli_in(root, "remote", "unlock", "web", expect=5)
        self.assertIn("only file remotes", refused.stderr)

    def test_unlock_transport_level_api(self):
        """The safety rules hold below the CLI too."""
        root, remote_dir = self._remote_repo()
        transport = create_transport(make_remote_config(str(remote_dir), root), root)
        self.assertFalse(unlock(transport)["removed"])

        self._write_lock(remote_dir, self._json_lock(pid=os.getpid(), host=None))
        with self.assertRaisesRegex(RemoteError, "refusing to clear the lock"):
            unlock(transport)
        self.assertTrue(unlock(transport, force=True)["removed"])

    def test_lock_is_released_after_a_successful_push(self):
        root, remote_dir = self._remote_repo()
        cli_in(root, "push", "origin", "main", expect=0)
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_lock_is_released_when_the_body_raises(self):
        """A failure inside the critical section must not strand the lock."""
        _, remote_dir = self._remote_repo()
        lock = remote_mod.FileRemoteLock(remote_dir, "push")
        with self.assertRaises(RuntimeError):
            with lock:
                self.assertTrue(self._lock_path(remote_dir).exists())
                raise RuntimeError("boom")
        self.assertFalse(self._lock_path(remote_dir).exists())

    def test_a_failed_acquire_never_releases_someone_elses_lock(self):
        """Acquiring a held lock must fail without deleting the holder's lock."""
        _, remote_dir = self._remote_repo()
        self._write_lock(remote_dir, self._json_lock(pid=os.getpid()))
        lock = remote_mod.FileRemoteLock(remote_dir, "push")
        with self.assertRaises(RemoteError):
            with lock:
                pass
        self.assertTrue(self._lock_path(remote_dir).exists())
        self.assertIsNone(lock._descriptor)


# --------------------------------------------------------------------------
# 11/12. Context id generations (P2)
# --------------------------------------------------------------------------

class T1112ContextIdGenerations(Base):
    def test_12_new_ids_are_16_hex_and_declared(self):
        root = self.store_repo()
        obj = Store(root).head_object()
        self.assertEqual(len(obj["context_id"]) - len("ctx_"), 16)
        self.assertTrue(CTX_ID_RE.match(obj["context_id"]))
        self.assertEqual(obj["id_hash_version"], context_mod.ID_HASH_VERSION)
        self.assertEqual(obj["core_view_version"], context_mod.CORE_VIEW_VERSION)
        self.assertTrue(context_mod.verify_context_id(obj))
        self.assertEqual(Store(root).context_problems(obj["context_id"]), [])

    def test_11_legacy_8_hex_object_still_verifies(self):
        root = self.store_repo()
        legacy = downgrade_to_legacy(Store(root).head_object())
        self.assertNotIn("core_view_version", legacy)
        self.assertNotIn("id_hash_version", legacy)
        self.assertNotIn("parent_context_ids", legacy)
        self.assertEqual(len(legacy["context_id"]) - len("ctx_"), 8)
        self.assertTrue(context_mod.verify_context_id(legacy))
        portable, _ = validate_context_object(legacy, legacy["context_id"])
        self.assertEqual(portable["context_id"], legacy["context_id"])

    def test_11b_legacy_object_survives_a_remote_round_trip(self):
        """Old objects must still push, fetch and verify on another machine."""
        root = self.store_repo()
        remote_dir = Path(tempfile.mkdtemp(prefix="cgit-legacy-remote-"))
        self._tmpdirs.append(str(remote_dir))

        store = Store(root)
        legacy = downgrade_to_legacy(store.head_object())
        store.context_path(store.head()).unlink()
        store.context_path(legacy["context_id"]).write_text(
            json.dumps(legacy, indent=2), encoding="utf-8")
        (store.refs_dir / "main").write_text(legacy["context_id"] + "\n",
                                             encoding="utf-8")
        store._write_symbolic_head("main", legacy["context_id"])

        cli_in(root, "remote", "add", "origin", str(remote_dir), expect=0)
        pushed = cli_in(root, "push", "origin", "main", expect=0)
        self.assertIn(legacy["context_id"], pushed.stdout)

        other = self.make_repo(files=self.DEFAULT_FILES)
        cli_in(other, "init", expect=0)
        cli_in(other, "remote", "add", "origin", str(remote_dir), expect=0)
        cli_in(other, "fetch", "origin", "--allow-other-project", expect=0)
        fetched = Store(other).load_context(legacy["context_id"])
        self.assertIsNotNone(fetched)
        self.assertTrue(context_mod.verify_context_id(fetched))
        self.assertTrue(Store(other).context_problems(legacy["context_id"]) == [])

    def test_11c_new_ids_are_all_one_width(self):
        root = self.store_repo()
        cli_in(root, "commit", "--no-prompt", "--set", "current_objective=two",
               "-m", "second", expect=0)
        ids = Store(root).list_context_ids()
        self.assertEqual(len(ids), 2)
        self.assertEqual(sorted(len(i) - len("ctx_") for i in ids), [16, 16])
        self.assertEqual(Store(root).verify_graph()[1], True)

    def test_unusual_id_widths_are_refused(self):
        for width in (0, 4, 7, 9, 12, 15, 17, 20, 32):
            self.assertIsNone(CTX_ID_RE.match("ctx_" + "a" * width), width)
        for width in (8, 16):
            self.assertIsNotNone(CTX_ID_RE.match("ctx_" + "a" * width), width)

    def test_id_search_never_returns_a_truncated_prefix(self):
        """An unanchored search must not settle for a 16-hex id's 8-hex prefix.

        Regression: with the alternatives ordered shortest-first, reading
        ``context: ctx_67086e7f487be3cf`` back out of HEAD produced
        ``ctx_67086e7f`` — an id that does not exist — which silently made
        ``head_object()`` return None and left HANDOFF.md stale after checkout.
        """
        from context_git.storage import CTX_ID_SEARCH_RE

        full = "ctx_67086e7f487be3cf"
        for text in ("context: " + full,
                     "ref: refs/heads/main\ncontext: " + full,
                     full + "\n"):
            self.assertEqual(CTX_ID_SEARCH_RE.search(text).group(0), full, text)

        short = "ctx_67086e7f"
        self.assertEqual(CTX_ID_SEARCH_RE.search("context: " + short).group(0), short)

    def test_head_round_trips_the_full_16_hex_id(self):
        root = self.store_repo()
        store = Store(root)
        ctx_id = store.head()
        self.assertEqual(len(ctx_id) - len("ctx_"), 16)
        store.detach(ctx_id)
        self.assertEqual(store.head(), ctx_id)
        self.assertIsNotNone(store.head_object())

    def test_detached_checkout_refreshes_the_handoff(self):
        root = self.store_repo()
        cli_in(root, "commit", "--no-prompt", "--set", "current_objective=two",
               "-m", "second", expect=0)
        store = Store(root)
        second = store.head()
        first = store.parent_ids(store.load_context(second))[0]

        cli_in(root, "checkout", first, expect=0)
        self.assertEqual(store.head(), first)
        handoff = (root / ".context-git" / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn(first, handoff)
        self.assertNotIn(second, handoff)

    def test_store_refuses_to_save_an_unusual_id(self):
        root = self.store_repo()
        with self.assertRaises(StoreError):
            Store(root).save_context({"context_id": "ctx_" + "a" * 12})

    def test_legacy_id_is_not_verified_against_the_new_width(self):
        """A downgraded object must not accidentally verify at 16 hex."""
        root = self.store_repo()
        legacy = downgrade_to_legacy(Store(root).head_object())
        legacy["id_hash_version"] = 2
        self.assertFalse(context_mod.verify_context_id(legacy),
                         "declared generation must be enforced, not inferred")


if __name__ == "__main__":
    unittest.main()

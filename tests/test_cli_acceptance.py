"""End-to-end acceptance test — walks the full lifecycle via the CLI subprocess.

Mirrors the v1 acceptance checklist:
    init → snapshot ctx_001 → modify code → status detects drift →
    commit ctx_002 → semantic diff → log → resume → secret guard refuses leaks
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import TempRepoTest, cli, run_git  # noqa: E402


class TestAcceptance(TempRepoTest):
    def _demo_repo(self):
        return self.make_repo(files={
            "package.json": '{"name":"star-agent","version":"0.1.0",'
                            '"scripts":{"build":"tsc","test":"jest"}}',
            "src/agent.ts": "export function think() {}\n",
        })

    def test_full_lifecycle(self):
        root = self._demo_repo()

        # -- Step 1: init + first snapshot ------------------------------------
        cli(root, "init")
        p = cli(root, "snapshot", "--no-prompt", "-m", "Initial snapshot",
                "--set", "goal=Build a demo agent",
                "--set", "current_objective=Implement auth middleware",
                "--set", "completed=Project scaffold",
                "--set", "pending=Refresh token rotation")
        head1 = self._head(root)
        self.assertTrue(head1.startswith("ctx_"))
        self.assertTrue((root / ".context-git" / "HANDOFF.md").is_file())

        # -- Step 2: modify code → status must detect drift --------------------
        (root / "src" / "agent.ts").write_text("export function think() { return 42 }\n")
        (root / "src" / "middleware.ts").write_text("export function mw() {}\n")
        proc = cli(root, "status", expect=3)  # non-zero: drift present
        self.assertIn("STALE", proc.stdout)
        self.assertIn("src/agent.ts", proc.stdout)

        # -- Step 3: commit ctx_002 with incremental notes ----------------------
        cli(root, "commit", "--no-prompt", "-m", "Auth middleware implemented",
            "--set", "completed=Auth middleware",
            "--set", "in_progress=Refresh token rotation",
            "--set", "known_issues=Safari SameSite cookie issue",
            "--set", "decisions=[{\"decision\":\"httpOnly cookie sessions\","
                     "\"reason\":\"XSS-safe\",\"rejected\":[\"localStorage\"]}]",
            "--set", "validation.test=pass",
            "--set", "recommended_actions=Implement refresh token rotation endpoint",
            "--set", "capabilities_required=filesystem,shell,git")
        head2 = self._head(root)
        self.assertNotEqual(head1, head2)

        # goal inherited from ctx_001, not wiped:
        ctx2 = json.loads((root / ".context-git" / "contexts" / (head2 + ".json"))
                          .read_text(encoding="utf-8"))
        self.assertEqual(ctx2["goal"], "Build a demo agent")
        self.assertEqual(ctx2["decisions"][0]["rejected"], ["localStorage"])

        # -- Step 4: semantic diff ------------------------------------------------
        proc = cli(root, "diff", head1, head2)
        out = proc.stdout
        self.assertIn("+ Auth middleware", out)              # newly completed
        self.assertIn("(pending → in_progress)", out)        # refresh tokens advanced
        self.assertIn("[NEW ISSUE]", out)
        self.assertIn("httpOnly cookie sessions", out)
        self.assertIn("pass", out)                          # validation change

        # diff with 0 args = HEAD vs parent
        cli(root, "diff")
        # diff with 1 arg = that context vs its parent
        cli(root, "diff", head2)

        # -- Step 5: log / show / checkout ------------------------------------------
        proc = cli(root, "log")
        self.assertIn(head2, proc.stdout)
        self.assertIn(head1, proc.stdout)

        proc = cli(root, "show", head1)
        self.assertIn("UACP/1.0", proc.stdout)

        proc = cli(root, "checkout", head1)
        self.assertIn(head1, proc.stdout)
        handoff = (root / ".context-git" / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn(head1, handoff)
        self.assertNotIn(head2, handoff)
        proc = cli(root, "commit", "--no-prompt", "--set", "pending=branch", expect=1)
        self.assertIn("historical HEAD", proc.stderr)
        proc = cli(root, "show")
        self.assertIn(head1, proc.stdout)
        cli(root, "checkout", "-")  # back to ctx_002
        proc = cli(root, "show")
        self.assertIn(head2, proc.stdout)
        # user's git repo untouched by checkout:
        proc = cli(root, "status")
        self.assertIn(head2, proc.stdout)

        # -- Step 6: no-op commit refused ---------------------------------------------
        proc = cli(root, "commit", "--no-prompt", "-m", "nothing changed", expect=1)
        self.assertIn("nothing to commit", proc.stderr)

        # -- Step 7: resume -----------------------------------------------------------------
        proc = cli(root, "resume")
        out = proc.stdout
        self.assertIn("Resume Context", out)
        self.assertIn("Build a demo agent", out)
        self.assertIn("httpOnly cookie sessions", out)
        self.assertIn("Implement refresh token rotation endpoint", out)
        cli(root, "resume", "--json")
        cli(root, "resume", "--write-briefing")
        self.assertTrue((root / ".context-git" / "RESUME_BRIEFING.md").is_file())

        # -- Step 8: secret never reaches disk --------------------------------------------
        # Layer 2 (redact) replaces the secret in-place before write; the stored
        # context must contain only the placeholder. Layer 3 (verify) confirms.
        secret = "sk-test123"
        github_secret = "ghp_test"
        cli(root, "commit", "--no-prompt", "-m", "key discussion",
            "--set", "notes=the api keys are " + secret + " " + github_secret)
        stored = (root / ".context-git" / "contexts" /
                  (self._head(root) + ".json")).read_text(encoding="utf-8")
        self.assertNotIn(secret, stored)                     # original never on disk
        self.assertNotIn(github_secret, stored)
        self.assertIn("[REDACTED:api-key]", stored)          # placeholder instead
        handoff = (root / ".context-git" / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertNotIn(secret, handoff)

        # -- Step 9: verify clean ---------------------------------------------------------
        proc = cli(root, "verify")
        self.assertIn("clean", proc.stdout)

    def test_json_outputs(self):
        root = self._demo_repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=g")
        proc = cli(root, "show", "--json")
        obj = json.loads(proc.stdout)
        self.assertEqual(obj["protocol"], "UACP")
        proc = cli(root, "diff", "--json")
        obj = json.loads(proc.stdout)
        self.assertIn("changes", obj)
        proc = cli(root, "status", "--json", expect=0)
        obj = json.loads(proc.stdout)
        self.assertEqual(obj["drift_level"], "NONE")
        proc = cli(root, "resume", "--json")
        obj = json.loads(proc.stdout)
        self.assertIn("briefing", obj)

    def test_skill_usable_without_install(self):
        """python scripts/context_git.py works from any cwd."""
        root = self._demo_repo()
        proc = cli(root, "status", expect=1)
        self.assertIn("init", proc.stdout + proc.stderr)

    def test_root_flag_works_before_or_after_subcommand(self):
        root = self._demo_repo()
        before = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent /
                                 "scripts" / "context_git.py"),
             "--root", str(root), "init"],
            cwd=str(Path(root).parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertTrue((root / ".context-git").is_dir())
        after = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent /
                                 "scripts" / "context_git.py"),
             "status", "--root", str(root)],
            cwd=str(Path(root).parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(after.returncode, 1, after.stderr)
        self.assertIn("Context Git Status", after.stdout)

    def test_agent_adapter_override(self):
        root = self._demo_repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--agent", "codex", "--set", "goal=g")
        ctx = json.loads((root / ".context-git" / "contexts" /
                          (self._head(root) + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(ctx["source_agent"]["name"], "codex")
        proc = cli(root, "resume", "--agent", "opencode")
        self.assertIn("OpenCode", proc.stdout)

    def test_unicode_output_overrides_legacy_console_encoding(self):
        root = self._demo_repo()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "pending=实现登录")
        cli(root, "commit", "--no-prompt", "--set", "completed=实现登录")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent /
                                 "scripts" / "context_git.py"), "diff"],
            cwd=str(root), capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("实现登录", proc.stdout)

    @staticmethod
    def _head(root):
        return (Path(root) / ".context-git" / "HEAD").read_text().strip().split()[-1]


if __name__ == "__main__":
    unittest.main()

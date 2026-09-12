"""V2 capability auto-profiling."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.capabilities import compatibility, profile_capabilities  # noqa: E402
from helpers import TempRepoTest, cli  # noqa: E402


class TestCapabilityProfile(TempRepoTest):
    def test_observed_declared_and_unavailable_are_distinct(self):
        root = self.make_repo(files={"main.py": "pass\n"})

        def fake_which(command, path=None):
            return "/bin/{}".format(command) if command in ("sh", "git") else None

        adapter = {
            "name": "demo",
            "capabilities": ["filesystem", "shell", "git", "node", "browser"],
        }
        profiled = profile_capabilities(adapter, root, environ={"PATH": "/bin"}, which=fake_which)
        profile = profiled["capability_profile"]
        self.assertEqual(profile["git"]["status"], "available")
        self.assertEqual(profile["node"]["status"], "unavailable")
        self.assertEqual(profile["browser"]["status"], "declared")
        self.assertNotIn("node", profiled["capabilities"])
        self.assertIn("browser", profiled["capabilities"])
        report = compatibility(["git", "node", "browser"], profiled)
        self.assertEqual(report["satisfied"], ["git", "browser"])
        self.assertEqual(report["verified"], ["git"])
        self.assertEqual(report["declared_only"], ["browser"])
        self.assertEqual(report["missing"], ["node"])

    def test_capabilities_cli_json(self):
        root = self.make_repo(files={"main.py": "pass\n"})
        proc = cli(root, "capabilities", "--agent", "generic", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["agent"], "generic")
        self.assertIn("filesystem", data["profile"])
        self.assertIn(data["profile"]["python"]["status"], ("available", "unavailable"))


if __name__ == "__main__":
    unittest.main()

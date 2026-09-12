"""Project detection tests: evidence or 'unknown'."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.project import detect  # noqa: E402
from helpers import TempRepoTest  # noqa: E402


class TestDetection(TempRepoTest):
    def test_node(self):
        root = self.make_repo(files={
            "package.json": '{"name":"web","scripts":{"build":"tsc","test":"vitest run",'
                            '"lint":"eslint ."},"dependencies":{"react":"^18","next":"^14"}}',
            "pnpm-lock.yaml": "",
        })
        d = detect(root)
        self.assertEqual(d["primary_language"], "node")
        self.assertEqual(d["package_manager"], "pnpm")
        self.assertEqual(d["commands"]["build"], "pnpm run build")
        self.assertEqual(d["commands"]["test"], "pnpm run vitest run".replace("pnpm run vitest run", "pnpm run test"))
        self.assertIn("React", d["frameworks"])
        self.assertIn("Next.js", d["frameworks"])

    def test_python(self):
        root = self.make_repo(files={
            "pyproject.toml": '[project]\nname = "svc"\ndependencies = ["fastapi"]\n'
                              '[tool.pytest.ini_options]\n',
            "requirements.txt": "fastapi\npytest\n",
            "tests/test_app.py": "def test_x():\n    assert True\n",
        })
        d = detect(root)
        self.assertEqual(d["primary_language"], "python")
        self.assertIn("FastAPI", d["frameworks"])
        self.assertEqual(d["commands"]["test"], "python -m pytest")

    def test_rust(self):
        root = self.make_repo(files={
            "Cargo.toml": '[package]\nname = "tool"\nversion = "0.1.0"\n'
                          '[dependencies]\naxum = "0.7"\n',
        })
        d = detect(root)
        self.assertEqual(d["primary_language"], "rust")
        self.assertEqual(d["package_manager"], "cargo")
        self.assertEqual(d["commands"]["test"], "cargo test")
        self.assertIn("Axum", d["frameworks"])

    def test_go(self):
        root = self.make_repo(files={
            "go.mod": "module example.com/app\n\ngo 1.22\n",
        })
        d = detect(root)
        self.assertEqual(d["primary_language"], "go")
        self.assertEqual(d["package_manager"], "go")
        self.assertEqual(d["commands"]["build"], "go build ./...")

    def test_unknown_stays_unknown(self):
        root = self.make_repo(files={"notes.txt": "just some text"})
        d = detect(root)
        self.assertEqual(d["primary_language"], "unknown")
        self.assertEqual(d["package_manager"], "unknown")
        self.assertIsNone(d["commands"]["build"])
        # evidence must admit weakness
        self.assertIn(d["evidence"]["strength"], ("none", "weak"))

    def test_no_hallucinated_commands(self):
        root = self.make_repo(files={
            "package.json": '{"name":"bare"}',  # no scripts at all
        })
        d = detect(root)
        for v in d["commands"].values():
            self.assertIsNone(v)

    def test_ignored_dependency_tree_does_not_influence_detection(self):
        root = self.make_repo(files={
            "README.md": "plain project",
            "node_modules/pkg/package.json": '{"name":"dependency"}',
            "build/generated/Cargo.toml": '[package]\nname="generated"',
        })
        d = detect(root)
        self.assertEqual(d["primary_language"], "unknown")


class TestMonorepo(TempRepoTest):
    def test_workspaces_flagged(self):
        root = self.make_repo(files={
            "package.json": '{"name":"mono","workspaces":["packages/*"]}',
            "packages/a/package.json": '{"name":"a"}',
            "packages/b/package.json": '{"name":"b"}',
            "packages/c/package.json": '{"name":"c"}',
        })
        d = detect(root)
        self.assertTrue(d["is_monorepo"])


if __name__ == "__main__":
    unittest.main()

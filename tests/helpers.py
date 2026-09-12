"""Shared test helpers: temp git repositories built on demand."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts" / "context_git.py"


def run_git(cwd, *args):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.co",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
    })
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        env=env,
    )


def make_repo(dirs=None, files=None, commit=True):
    """Create a temp git repo. Returns (root: Path, register: cleanup fn)."""
    tmp = tempfile.mkdtemp(prefix="cgit-test-")
    root = Path(tmp)
    run_git(root, "init", "-q")
    for d in dirs or []:
        (root / d).mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if commit and (files or dirs):
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "init")
    return root, tmp


def cli(root, *args, expect=0):
    """Run the CLI as a subprocess (acceptance path) and return CompletedProcess."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS), *args],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    if expect is not None:
        assert proc.returncode == expect, (
            "cli {} → exit {}\nstdout: {}\nstderr: {}".format(
                list(args), proc.returncode, proc.stdout, proc.stderr)
        )
    return proc


class TempRepoTest(unittest.TestCase):
    """Base class managing one temp repo per test."""

    def setUp(self):
        self._tmpdirs = []

    def make_repo(self, **kwargs):
        root, tmp = make_repo(**kwargs)
        self._tmpdirs.append(tmp)
        return root

    def tearDown(self):
        import shutil
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

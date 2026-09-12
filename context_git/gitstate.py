"""gitstate — read-only Git state collection for context-git.

Design rules:

* Every function returns a dict and **never raises** for expected Git
  pathologies (no git binary, not a repo, no commits, detached HEAD).
* Only porcelain/plumbing commands, fixed locale, ``--no-optional-locks``
  so we never fight a concurrent index operation.
* No full diff is ever collected — ``--numstat`` aggregates only.
* This module NEVER mutates the repository (no commit/push/checkout/reset).

Used by snapshot creation and drift detection, so both always agree on
what "the Git state" means.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT = 15  # seconds per command; a huge repo should still be fast

CORE_MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
    "Gemfile", "Makefile", "Dockerfile", "tsconfig.json",
}


def _git_env():
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Never trigger an editor or credential prompt from inside an agent.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(root, args, timeout=GIT_TIMEOUT):
    """Run a git command. Returns (ok, stdout, stderr) — never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False, "", "git invocation failed"
    if proc.returncode != 0:
        return False, proc.stdout or "", (proc.stderr or "").strip()
    return True, proc.stdout or "", ""


def _lines(text):
    return [ln for ln in text.splitlines() if ln.strip()]


def find_repo_root(start="."):
    """Walk up from ``start`` to find the repository root, or None."""
    if not git_available():
        return None
    cur = Path(start).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    ok, out, _ = _run(cur, ["rev-parse", "--show-toplevel"])
    if ok and out.strip():
        return Path(out.strip())
    return None


def _parse_status_porcelain_v1(text):
    """Parse ``git status --porcelain=v1 -z``.

    The -z form avoids all quoting/escaping ambiguity (spaces, newlines,
    non-ASCII, rename arrows) — including on Windows.
    """
    staged, unstaged, untracked = [], [], []
    conflicted, renamed, deleted = [], [], []
    entries = text.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry or len(entry) < 3:
            continue
        x, y = entry[0], entry[1]
        path = entry[3:]
        orig = None
        if x in ("R", "C") and i < len(entries):
            orig = entries[i]
            i += 1

        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x == "!" or y == "!":
            continue
        if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
            conflicted.append(path)
            continue

        if x not in (" ", "?"):
            staged.append(path)
        if y not in (" ", "?"):
            unstaged.append(path)
        if x == "R" or y == "R":
            renamed.append({"from": orig or path, "to": path})
        if x == "D" or y == "D":
            deleted.append(path)
    return {
        "staged": sorted(set(staged)),
        "unstaged": sorted(set(unstaged)),
        "untracked": sorted(set(untracked)),
        "conflicted": sorted(set(conflicted)),
        "renamed": renamed,
        "deleted": sorted(set(deleted)),
    }


def _numstat(root, args):
    """Aggregate ``--numstat`` into {path: {additions, deletions, binary}}."""
    ok, out, _ = _run(root, args)
    if not ok:
        return {}
    result = {}
    for line in _lines(out):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], "\t".join(parts[2:])
        binary = add == "-" or dele == "-"
        result[path] = {
            "additions": 0 if binary else int(add or 0),
            "deletions": 0 if binary else int(dele or 0),
            "binary": binary,
        }
    return result


def _classify_change(path, status):
    if path in status.get("untracked", []):
        return "untracked"
    if path in status.get("conflicted", []):
        return "conflicted"
    if path in status.get("deleted", []):
        return "deleted"
    for r in status.get("renamed", []):
        if r.get("to") == path or r.get("from") == path:
            return "renamed"
    if path in status.get("staged", []) and path in status.get("unstaged", []):
        return "staged+modified"
    if path in status.get("staged", []):
        return "staged"
    return "modified"


def _strip_own_store(snapshot):
    """Remove tool-store paths AND sensitive paths from all file lists.

    Contexts must not record the *names* of credential files either —
    knowing a repo has .env / id_rsa is itself unwanted metadata.
    """
    from .common import is_forbidden_path

    def keep(path):
        if not path or path.startswith(".context-git/") or path.startswith(".git/"):
            return False
        return not is_forbidden_path(path)

    for key in ("staged", "unstaged", "untracked", "conflicted", "deleted"):
        snapshot[key] = [p for p in snapshot.get(key, []) if keep(p)]
    snapshot["renamed"] = [r for r in snapshot.get("renamed", [])
                           if keep(r.get("from", "")) and keep(r.get("to", ""))]
    snapshot["changed_files"] = [f for f in snapshot.get("changed_files", [])
                                 if keep(f.get("path", ""))]
    ds = snapshot.get("diff_stat") or {}
    changed = snapshot["changed_files"]
    ds["files_changed"] = len(changed)
    ds["additions"] = sum(f["additions"] for f in changed)
    ds["deletions"] = sum(f["deletions"] for f in changed)
    snapshot["diff_stat"] = ds
    return snapshot


def collect(root=".", recent_commits=8):
    """Collect a complete, read-only Git snapshot.

    Always returns a dict with an ``available`` flag. Callers never need to
    guard against exceptions or missing keys.
    """
    snapshot = {
        "available": False, "reason": None, "root": None,
        "branch": None, "detached_head": False,
        "head": None, "head_short": None, "head_subject": None,
        "upstream": None, "ahead": None, "behind": None,
        "repo_state": "clean",
        "staged": [], "unstaged": [], "untracked": [], "conflicted": [],
        "renamed": [], "deleted": [],
        "changed_files": [],
        "diff_stat": {"files_changed": 0, "additions": 0, "deletions": 0},
        "recent_commits": [],
        "is_shallow": False, "commit_count": None,
        "error": None,
    }

    if not git_available():
        snapshot["reason"] = "git-not-installed"
        return snapshot

    root_path = Path(root).resolve()
    ok, out, _ = _run(root_path, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        snapshot["reason"] = "not-a-git-repository"
        return snapshot

    snapshot["available"] = True

    ok, out, _ = _run(root_path, ["rev-parse", "--show-toplevel"])
    snapshot["root"] = out.strip() if ok and out.strip() else str(root_path)

    # HEAD / branch -----------------------------------------------------------
    ok, out, _ = _run(root_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if ok and out.strip():
        snapshot["branch"] = out.strip()
    else:
        snapshot["detached_head"] = True

    ok, out, _ = _run(root_path, ["rev-parse", "HEAD"])
    if ok and out.strip():
        snapshot["head"] = out.strip()
        snapshot["head_short"] = out.strip()[:12]

    ok, out, _ = _run(root_path, ["log", "-1", "--pretty=%s"])
    if ok:
        snapshot["head_subject"] = out.strip() or None

    # upstream tracking (best effort) ------------------------------------------
    ok, out, _ = _run(root_path, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
    if ok and out.strip():
        parts = out.split()
        if len(parts) == 2:
            snapshot["behind"] = int(parts[0])
            snapshot["ahead"] = int(parts[1])
            ok2, out2, _ = _run(root_path, ["rev-parse", "--abbrev-ref", "@{upstream}"])
            if ok2:
                snapshot["upstream"] = out2.strip()

    # working tree ---------------------------------------------------------------
    ok, out, _ = _run(root_path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if ok:
        snapshot.update(_parse_status_porcelain_v1(out))
        # Never let the tool's own store pollute project state.
        snapshot = _strip_own_store(snapshot)

    # aggregate diff stats (never hunk content) -----------------------------------
    staged_numstat = _numstat(root_path, ["diff", "--cached", "--numstat"])
    unstaged_numstat = _numstat(root_path, ["diff", "--numstat"])
    changed = sorted(
        set(snapshot["staged"]) | set(snapshot["unstaged"])
        | set(snapshot["untracked"]) | set(snapshot["conflicted"])
    )
    snapshot["changed_files"] = [
        {
            "path": p,
            "change_type": _classify_change(p, snapshot),
            "additions": staged_numstat.get(p, {}).get("additions", 0)
            + unstaged_numstat.get(p, {}).get("additions", 0),
            "deletions": staged_numstat.get(p, {}).get("deletions", 0)
            + unstaged_numstat.get(p, {}).get("deletions", 0),
        }
        for p in changed
    ]
    snapshot["diff_stat"] = {
        "files_changed": len(changed),
        "additions": sum(f["additions"] for f in snapshot["changed_files"]),
        "deletions": sum(f["deletions"] for f in snapshot["changed_files"]),
    }

    # repository operation in progress (merge/rebase/...) -------------------------
    git_dir = root_path / ".git"
    if git_dir.is_file():  # worktree / submodule pointer file
        try:
            content = git_dir.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                git_dir = (root_path / content.split(":", 1)[1].strip()).resolve()
        except OSError:
            pass
    for marker, label in (
        ("rebase-merge", "rebase-in-progress"),
        ("rebase-apply", "rebase-in-progress"),
        ("MERGE_HEAD", "merge-in-progress"),
        ("CHERRY_PICK_HEAD", "cherry-pick-in-progress"),
        ("REVERT_HEAD", "revert-in-progress"),
        ("BISECT_LOG", "bisect-in-progress"),
    ):
        if (git_dir / marker).exists():
            snapshot["repo_state"] = label
            break
    else:
        if snapshot["conflicted"]:
            snapshot["repo_state"] = "conflicts-present"

    ok, out, _ = _run(root_path, ["rev-parse", "--is-shallow-repository"])
    snapshot["is_shallow"] = ok and out.strip() == "true"

    ok, out, _ = _run(root_path, ["rev-list", "--count", "HEAD"])
    if ok and out.strip().isdigit():
        snapshot["commit_count"] = int(out.strip())

    # recent commits ---------------------------------------------------------------
    ok, out, _ = _run(
        root_path, ["log", "-{}".format(max(1, recent_commits)), "--pretty=%h%x1f%an%x1f%ar%x1f%s"]
    )
    if ok:
        commits = []
        for line in _lines(out):
            fields = line.split("\x1f")
            if len(fields) == 4:
                commits.append(
                    {"hash": fields[0], "author": fields[1],
                     "when": fields[2], "subject": fields[3]}
                )
        snapshot["recent_commits"] = commits
    elif snapshot["head"] is None:
        snapshot["error"] = "repository has no commits yet"

    return snapshot


def commits_between(root, old_head, new_head):
    """Count commits on new_head not reachable from old_head. 0 on failure."""
    ok, out, _ = _run(root, ["rev-list", "--count", "{}..{}".format(old_head, new_head)])
    if ok and out.strip().isdigit():
        return int(out.strip())
    return 0  # history rewritten — caller signals via head-lost instead

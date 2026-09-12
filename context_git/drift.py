"""drift — detect when reality has moved away from a stored Context.

Agent context's biggest failure mode: the context describes yesterday, the
repository is today. Drift detection compares the Context's *observed*
evidence (git HEAD, branch, file fingerprints) against the live repository
and reports **graded** drift with **local** invalidation:

* it never declares the whole context invalid because one file moved
* it names exactly which sections/files must be re-verified

It also computes:

* per-file validity (VALID / STALE) via stored fingerprints
* validation freshness (a test result recorded before the code changed
  is reported STALE, not PASS)

Drift levels:
    NONE    — repository matches the context's observed state
    LOW     — untracked churn only; narrative context still trustworthy
    MEDIUM  — HEAD moved / working tree changed; history claims stale
    HIGH    — important files changed under the context's feet; their
              content-level claims must be re-read
"""

from __future__ import annotations

from pathlib import Path

from .common import fingerprint
from .gitstate import collect as git_collect, commits_between

_LEVELS = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


class Report(dict):
    """Drift report — a dict with convenience accessors."""

    @property
    def level(self):
        return self.get("level", "NONE")

    @property
    def drifted(self):
        return self.level != "NONE"


def check(root, ctx_obj, live=None):
    """Compare a Context Object against the live repository.

    ``live`` optionally injects a git snapshot (used by tests). Never raises.
    """
    root = Path(root).resolve()
    report = Report({
        "level": "NONE",
        "context_id": (ctx_obj or {}).get("context_id"),
        "captured_at": (ctx_obj or {}).get("created_at"),
        "signals": [],
        "stale_files": [],
        "stale_sections": [],
        "recommendations": [],
        "summary": "",
        "counts": {
            "commits_since": 0,
            "working_tree_changed": 0,
            "important_files_changed": 0,
            "new_untracked": 0,
        },
    })

    if not ctx_obj:
        report["level"] = "HIGH"
        report["summary"] = "No context object to check against."
        return report

    ctx_git = ctx_obj.get("git") or {}
    live = live or git_collect(root)
    signals = []

    # -- 1. HEAD movement -----------------------------------------------------
    old_head = ctx_git.get("head")
    new_head = live.get("head")
    if old_head and new_head and old_head != new_head:
        n = commits_between(root, old_head, new_head)
        report["counts"]["commits_since"] = n
        signals.append({
            "kind": "head-moved",
            "level": "MEDIUM",
            "detail": "{} commit(s) since context snapshot ({} → {})".format(
                n, ctx_git.get("head_short"), live.get("head_short")),
        })
        report["stale_sections"].extend(["git.recent_commits", "git.working_tree"])
        report["recommendations"].append(
            "Review the {} new commit(s) before trusting the context's history claims."
            .format(n) if n else "History moved (possibly rewritten) — verify git log by hand."
        )
    elif old_head and not new_head and ctx_git.get("available"):
        signals.append({
            "kind": "history-lost",
            "level": "HIGH",
            "detail": "context HEAD {} no longer exists".format(ctx_git.get("head_short")),
        })
        report["stale_sections"].append("git")
    elif not old_head and ctx_git.get("available") and new_head:
        signals.append({
            "kind": "history-created",
            "level": "LOW",
            "detail": "repository now has commits (context saw none)",
        })

    # -- 2. branch movement ------------------------------------------------------
    old_branch = ctx_git.get("branch")
    new_branch = live.get("branch")
    if (old_branch or new_branch) and old_branch != new_branch:
        signals.append({
            "kind": "branch-changed",
            "level": "MEDIUM",
            "detail": "branch {} → {}".format(
                old_branch or "(detached)", new_branch or "(detached)"),
        })
        report["stale_sections"].append("git.branch")

    # -- 3. important file fingerprints (local invalidation) ------------------------
    stale_files = []
    fingerprints = (ctx_obj.get("metadata") or {}).get("fingerprints") or {}
    for f in ctx_obj.get("important_files") or []:
        rel = f.get("path")
        old_fp = f.get("fingerprint") or fingerprints.get(rel)
        if not old_fp:
            continue
        new_fp = fingerprint(root / rel)
        if new_fp != old_fp:
            stale_files.append(rel)
    report["stale_files"] = stale_files
    if stale_files:
        report["counts"]["important_files_changed"] = len(stale_files)
        signals.append({
            "kind": "important-files-changed",
            "level": "HIGH",
            "detail": "{} important file(s) changed since snapshot".format(len(stale_files)),
            "paths": stale_files,
        })
        report["stale_sections"].append("files.important")
        report["recommendations"].append(
            "Re-read: " + ", ".join(stale_files[:8])
            + (" …" if len(stale_files) > 8 else "")
        )
        architecture_sources = {
            a.get("source") for a in (ctx_obj.get("architecture") or [])
            if isinstance(a, dict) and a.get("source")
        }
        if architecture_sources.intersection(stale_files):
            report["stale_sections"].append("architecture")

    # -- 4. working tree change set -------------------------------------------------
    old_wt = ctx_git.get("working_tree") or {}
    old_set = set(old_wt.get("staged") or []) | set(old_wt.get("unstaged") or []) \
        | set(old_wt.get("untracked") or [])
    new_set = set(live.get("staged") or []) | set(live.get("unstaged") or []) \
        | set(live.get("untracked") or [])
    delta = old_set ^ new_set
    report["counts"]["working_tree_changed"] = len(delta)
    if delta:
        new_untracked = set(live.get("untracked") or []) - set(old_wt.get("untracked") or [])
        report["counts"]["new_untracked"] = len(new_untracked)
        lvl = "LOW" if new_untracked and not (old_set ^ new_set) - new_untracked else "MEDIUM"
        signals.append({
            "kind": "working-tree-changed",
            "level": lvl,
            "detail": "{} path(s) in play now were not at snapshot time".format(len(delta)),
            "paths": sorted(delta)[:20],
        })
        report["stale_sections"].append("git.working_tree")

    # -- 5. validation freshness --------------------------------------------------------
    val = ctx_obj.get("validation") or {}
    if val.get("observed_at") and val.get("freshness") != "stale":
        # If code changed after validation was recorded, the result may no longer hold.
        code_changed = bool(stale_files) or bool(
            report["counts"]["commits_since"] or report["counts"]["working_tree_changed"]
        )
        if code_changed:
            val = dict(val)
            val["freshness"] = "stale"
            val["stale_reason"] = (
                "source files changed after validation was recorded"
            )
            report["validation_freshness"] = "STALE"
            signals.append({
                "kind": "validation-stale",
                "level": "MEDIUM",
                "detail": "validation results may be outdated: source changed after they "
                          "were recorded",
            })
            report["stale_sections"].append("validation")
            report["recommendations"].append(
                "Re-run build/test before relying on the recorded validation results."
            )
        else:
            report["validation_freshness"] = "FRESH"
    elif val.get("freshness") == "stale":
        report["validation_freshness"] = "STALE"
    else:
        report["validation_freshness"] = "UNKNOWN"

    # -- roll-up ------------------------------------------------------------------------
    report["signals"] = signals
    report["level"] = max(
        (s.get("level", "LOW") for s in signals),
        key=lambda lv: _LEVELS.get(lv, 0),
        default="NONE",
    ) if signals else "NONE"

    if not signals:
        report["summary"] = ("No drift — repository matches the context's observed state. "
                             "Context can be trusted as-is.")
    else:
        parts = ["{}: {}".format(s["kind"], s["detail"]) for s in signals]
        report["summary"] = " · ".join(parts)

    return report


def file_validity(ctx_obj, root):
    """Per-important-file validity map: {path: VALID|STALE|UNKNOWN}."""
    root = Path(root).resolve()
    out = {}
    fingerprints = (ctx_obj.get("metadata") or {}).get("fingerprints") or {}
    for f in ctx_obj.get("important_files") or []:
        rel = f.get("path")
        old_fp = f.get("fingerprint") or fingerprints.get(rel)
        if not old_fp:
            out[rel] = "UNKNOWN"
            continue
        out[rel] = "VALID" if fingerprint(root / rel) == old_fp else "STALE"
    return out


def format_report(report):
    """Human-readable drift report (the `status` output)."""
    L = []
    L.append("Drift: {}".format(report["level"]))
    if report.get("captured_at"):
        L.append("  Context captured: {}".format(report["captured_at"]))
    for s in report["signals"]:
        L.append("  [{}] {}".format(s["level"], s["detail"]))
        for p in (s.get("paths") or [])[:10]:
            L.append("      - {}".format(p))
        if len(s.get("paths") or []) > 10:
            L.append("      … and {} more".format(len(s["paths"]) - 10))
    vf = report.get("validation_freshness")
    if vf and vf != "UNKNOWN":
        L.append("  Validation freshness: {}".format(vf))
    if report.get("stale_files"):
        L.append("  Files to re-read: " + ", ".join(report["stale_files"][:10]))
    if report.get("recommendations"):
        for r in report["recommendations"][:4]:
            L.append("  → {}".format(r))
    L.append("  Verdict: {}".format(
        "context trustworthy" if report["level"] in ("NONE", "LOW")
        else "re-verify stale sections before trusting them"))
    return "\n".join(L)

"""render — human-readable views over Context Objects.

HANDOFF.md is *only* a rendered view of the current context (regenerated
on every commit); the structured Context Object is the source of truth.
This module renders:

* HANDOFF.md            — the full human handoff document
* show(ctx)             — pretty single-context display
* log(history)          — the context history listing
* resume_briefing(...)  — the compact agent-ready resume output
"""

from __future__ import annotations

from .diff import semantic_diff, render as render_diff
from .security import redact


def _items(items):
    if not items:
        return ["- _(not recorded)_"]
    lines = []
    for it in items:
        if isinstance(it, dict):
            text = it.get("text") or it.get("decision") or str(it)
            st = it.get("source_type")
            suffix = "  `[agent]`" if st == "agent" else ""
            lines.append("- {}{}".format(redact(text), suffix))
        else:
            lines.append("- {}".format(redact(str(it))))
    return lines


def _v_entry(v):
    if v is None:
        return "❔ not recorded"
    if isinstance(v, dict):
        res = v.get("result", "unknown")
        icon = {"pass": "✅", "fail": "❌", "skip": "⏭️"}.get(str(res).lower(), "❔")
        return "{} {}".format(icon, res)
    return str(v)


# --------------------------------------------------------------------------
# HANDOFF.md
# --------------------------------------------------------------------------

def handoff_md(ctx, drift_report=None, compat=None):
    """Render the human handoff document for a Context Object."""
    git = ctx.get("git") or {}
    wt = git.get("working_tree") or {}
    stack = (ctx.get("project") or {}).get("stack") or {}
    val = ctx.get("validation") or {}
    env = ctx.get("environment") or {}
    progress = ctx.get("progress") or {}
    L = []

    L.append("# Context Handoff")
    L.append("")
    L.append("> Protocol: **UACP/{}** · schema {} · context `{}` · {}".format(
        ctx.get("protocol_version"), ctx.get("schema_version"),
        ctx.get("context_id"), ctx.get("created_at")))
    if ctx.get("parent_context_id"):
        L.append("> Parent context: `{}`".format(ctx["parent_context_id"]))
    parents = ctx.get("parent_context_ids") or []
    if len(parents) > 1:
        L.append("> Merge parents: {}".format(
            ", ".join("`{}`".format(parent) for parent in parents)
        ))
    if ctx.get("context_branch"):
        L.append("> Context branch at creation: `{}`".format(ctx["context_branch"]))
    if ctx.get("message"):
        L.append("> Summary: {}".format(ctx["message"]))
    L.append("")
    L.append("_Structured source of truth: `.context-git/contexts/{}.json` — "
             "this file is a render. Run `context-git resume` for the live briefing._"
             .format(ctx.get("context_id")))
    L.append("")

    # Project ------------------------------------------------------------------
    L.append("## Project")
    L.append("")
    L.append("**{}**".format(ctx["project"]["name"]))
    L.append("")
    L.append("- Root: `{}`".format(ctx["project"]["root"]))
    L.append("- Type: {} · Package manager: {}".format(
        ctx["project"].get("type", "unknown"),
        stack.get("package_manager", "unknown")))
    frameworks = stack.get("frameworks") or []
    L.append("- Frameworks: {}".format(", ".join(frameworks) if frameworks else "unknown"))
    cmds = {k: v for k, v in (stack.get("commands") or {}).items() if v}
    if cmds:
        L.append("- Commands: " + " · ".join("`{}`".format(v) for v in cmds.values()))
    L.append("")

    # Goal / objective -----------------------------------------------------------
    L.append("## Project Goal")
    L.append("")
    L.append(redact(ctx.get("goal") or "_not recorded — the exporting agent should fill this in._"))
    L.append("")
    L.append("## Current Objective")
    L.append("")
    L.append(redact(ctx.get("current_objective") or "_not recorded._"))
    L.append("")

    architecture = ctx.get("architecture") or []
    if architecture:
        L.append("## Architecture")
        L.append("")
        for item in architecture:
            source = " · source: `{}`".format(item["source"]) if item.get("source") else ""
            L.append("- {} _(confidence: {:.0%} · freshness: {}{})_".format(
                item.get("fact", ""), item.get("confidence", 0.0),
                item.get("freshness", "unknown"), source,
            ))
        L.append("")

    # Progress ---------------------------------------------------------------------
    L.append("## Completed")
    L.append("")
    L.extend(_items(progress.get("completed")))
    L.append("")
    L.append("## In Progress")
    L.append("")
    L.extend(_items(progress.get("in_progress")))
    L.append("")
    L.append("## Pending")
    L.append("")
    L.extend(_items(progress.get("pending")))
    L.append("")

    # Decisions -----------------------------------------------------------------------
    L.append("## Decisions")
    L.append("")
    decisions = ctx.get("decisions") or []
    if not decisions:
        L.append("_none recorded._")
    else:
        for d in decisions:
            L.append("**Decision:** {}".format(d.get("decision", "")))
            if d.get("reason"):
                L.append("**Reason:** {}".format(d["reason"]))
            if d.get("rejected"):
                L.append("**Rejected:** {}".format("; ".join(d["rejected"])))
            L.append("")
    L.append("")

    # Constraints / do not change / issues ------------------------------------------------
    L.append("## Constraints")
    L.append("")
    L.extend(_items(ctx.get("constraints")))
    L.append("")
    L.append("## Do Not Change")
    L.append("")
    L.extend(_items(ctx.get("do_not_change")))
    L.append("")
    L.append("## Known Issues")
    L.append("")
    L.extend(_items(ctx.get("known_issues")))
    L.append("")

    # Important files -----------------------------------------------------------------------
    L.append("## Important Files")
    L.append("")
    files = ctx.get("important_files") or []
    if files:
        L.append("```text")
        for f in files:
            L.append(f["path"])
            L.append("    {}".format(f.get("why", "")))
        L.append("```")
    else:
        L.append("_none identified._")
    L.append("")

    # Changes since previous context ------------------------------------------------------------
    parent_id = ctx.get("parent_context_id")
    L.append("## Changes Since Previous Context")
    L.append("")
    L.append("_Rendered at commit time — see `context-git diff {} {}` for the live recompute._"
             .format(parent_id or "(root)", ctx.get("context_id")))
    L.append("")

    # Git state -----------------------------------------------------------------------------------
    L.append("## Repository State")
    L.append("")
    if git.get("available"):
        L.append("- Branch: **{}**{}".format(
            git.get("branch") or "(detached HEAD)",
            " — HEAD is detached" if git.get("detached_head") else ""))
        L.append("- HEAD: `{}`{}".format(
            git.get("head_short") or "none",
            " — “{}”".format(git["head_subject"]) if git.get("head_subject") else ""))
        if git.get("upstream"):
            L.append("- Upstream: {} (ahead {}, behind {})".format(
                git["upstream"], git.get("ahead"), git.get("behind")))
        if git.get("repo_state") and git["repo_state"] != "clean":
            L.append("- ⚠️ Repository state: **{}**".format(git["repo_state"]))
        L.append("- Working tree: {} (staged {}, modified {}, untracked {})".format(
            "clean" if wt.get("clean") else "dirty",
            len(wt.get("staged") or []), len(wt.get("unstaged") or []),
            len(wt.get("untracked") or [])))
        ds = git.get("diff_stat") or {}
        L.append("- Uncommitted diff: {} file(s), +{}/-{} lines".format(
            ds.get("files_changed", 0), ds.get("additions", 0), ds.get("deletions", 0)))
        commits = git.get("recent_commits") or []
        if commits:
            L.append("- Recent commits:")
            for c in commits[:8]:
                L.append("    - `{}` {} ({})".format(c["hash"], redact(c["subject"]), c["when"]))
        else:
            L.append("- Recent commits: _none yet_")
    else:
        L.append("_Not a Git repository ({}) — change tracking degraded._".format(
            git.get("reason") or "unknown reason"))
    L.append("")

    # Drift (live at render time, if provided) --------------------------------------------------------
    L.append("## Context Drift")
    L.append("")
    if drift_report is None:
        L.append("_Computed at resume time by `context-git status` / `resume`._")
    else:
        L.append("Drift level at render time: **{}**".format(drift_report.get("level")))
        if drift_report.get("stale_files"):
            L.append("Stale files: " + ", ".join(drift_report["stale_files"]))
    L.append("")

    # Agent compatibility --------------------------------------------------------------------------------
    L.append("## Agent Compatibility")
    L.append("")
    if compat is None:
        L.append("_Computed at resume time against the target agent._")
    elif ctx.get("capabilities_required"):
        L.append("- Required: {}".format(", ".join(
            compat.get("satisfied", []) + compat.get("missing", [])) or "none"))
        L.append("- Compatibility: **{}%**".format(compat.get("percent")))
        for w in compat.get("warnings") or []:
            L.append("- ⚠️ {}".format(w.get("message")))
            for t in w.get("affected") or []:
                L.append("    - affected: {}".format(t))
    else:
        L.append("_This context declares no special capability requirements._")
    L.append("")

    # Validation ---------------------------------------------------------------------------------------------
    L.append("## Validation")
    L.append("")
    L.append("- build: {}".format(_v_entry(val.get("build"))))
    L.append("- test: {}".format(_v_entry(val.get("test"))))
    L.append("- lint: {}".format(_v_entry(val.get("lint"))))
    L.append("- typecheck: {}".format(_v_entry(val.get("typecheck"))))
    if val.get("observed_at"):
        L.append("- Observed: {} · freshness: {}".format(
            val["observed_at"], val.get("freshness", "unknown")))
    if val.get("note"):
        L.append("- Note: {}".format(val["note"]))
    L.append("")

    # Environment ---------------------------------------------------------------------------------------------
    L.append("## Environment")
    L.append("")
    L.append("- OS: {} {} ({})".format(
        env.get("os", "unknown"), env.get("os_release", ""), env.get("arch", "")))
    L.append("- Python: {}".format(env.get("python", "unknown")))
    for k, v in (env.get("project_runtime") or {}).items():
        L.append("- {}: {}".format(k, v))
    L.append("- _No credentials, tokens or connection strings are recorded in this document._")
    L.append("")

    # Next step -------------------------------------------------------------------------------------------------
    L.append("## Recommended Next Step")
    L.append("")
    L.extend(_items(ctx.get("recommended_actions")))
    L.append("")
    L.append("---")
    L.append("_No chat history, no chain-of-thought, no full diffs — by protocol design. "
             "Contents passed secret redaction before write._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# show / log / resume
# --------------------------------------------------------------------------

def show(ctx):
    """Compact single-context display."""
    git = ctx.get("git") or {}
    wt = git.get("working_tree") or {}
    progress = ctx.get("progress") or {}
    L = []
    L.append("Context   {}".format(ctx.get("context_id")))
    L.append("Protocol  UACP/{}".format(ctx.get("protocol_version")))
    L.append("Created   {}".format(ctx.get("created_at")))
    L.append("Parent    {}".format(ctx.get("parent_context_id") or "(root)"))
    parents = ctx.get("parent_context_ids") or []
    if len(parents) > 1:
        L.append("Parents   {}".format(", ".join(parents)))
    if ctx.get("context_branch"):
        L.append("Branch    {}".format(ctx["context_branch"]))
    if ctx.get("message"):
        L.append("Summary   {}".format(ctx["message"]))
    L.append("Project   {} ({})".format(
        ctx["project"]["name"], ctx["project"].get("type", "unknown")))
    if ctx.get("goal"):
        L.append("Goal      {}".format(ctx["goal"]))
    if ctx.get("current_objective"):
        L.append("Objective {}".format(ctx["current_objective"]))
    L.append("Progress  {} completed · {} in-progress · {} pending".format(
        len(progress.get("completed") or []),
        len(progress.get("in_progress") or []),
        len(progress.get("pending") or [])))
    L.append("Decisions {} · Issues {} · Constraints {}".format(
        len(ctx.get("decisions") or []),
        len(ctx.get("known_issues") or []),
        len(ctx.get("constraints") or [])))
    if ctx.get("architecture"):
        L.append("Architecture {} fact(s)".format(len(ctx["architecture"])))
    if git.get("available"):
        L.append("Repo      {} @ {} ({} files in play)".format(
            git.get("branch") or "(detached)", git.get("head_short") or "none",
            len((wt.get("staged") or []) + (wt.get("unstaged") or [])
                + (wt.get("untracked") or []))))
    val = ctx.get("validation") or {}
    L.append("Validation build:{} test:{} lint:{} typecheck:{} [{}]".format(
        (val.get("build") or {}).get("result", "-") if isinstance(val.get("build"), dict) else "-",
        (val.get("test") or {}).get("result", "-") if isinstance(val.get("test"), dict) else "-",
        (val.get("lint") or {}).get("result", "-") if isinstance(val.get("lint"), dict) else "-",
        (val.get("typecheck") or {}).get("result", "-") if isinstance(val.get("typecheck"), dict) else "-",
        val.get("freshness", "unknown")))
    files = ctx.get("important_files") or []
    if files:
        L.append("Files     {}".format(", ".join(f["path"] for f in files[:8]))
                 + (" …" if len(files) > 8 else ""))
    actions = ctx.get("recommended_actions") or []
    for i, a in enumerate(actions[:3], 1):
        text = a.get("text") if isinstance(a, dict) else str(a)
        L.append("Next {}    {}".format(i, text))
    return "\n".join(L)


def log(history, decorations=None):
    """History listing, newest first. ``history`` is newest-first chain."""
    L = []
    if not history:
        L.append("(no contexts yet — run `context-git snapshot`)")
        return "\n".join(L)
    decorations = decorations or {}
    for ctx in history:
        summary = ctx.get("message") or ctx.get("current_objective") \
            or ctx.get("goal") or "(no summary)"
        ctx_id = ctx.get("context_id")
        refs = decorations.get(ctx_id) or []
        suffix = " ({})".format(", ".join(refs)) if refs else ""
        L.append("{}{}  {}  {}".format(
            ctx_id, suffix, (ctx.get("created_at") or "")[:19], redact(summary)))
        parents = ctx.get("parent_context_ids") or []
        if len(parents) > 1:
            L.append("              merge parents {}".format(", ".join(parents)))
        prog = ctx.get("progress") or {}
        counts = "completed {} · in-progress {} · pending {}".format(
            len(prog.get("completed") or []),
            len(prog.get("in_progress") or []),
            len(prog.get("pending") or []))
        L.append("              {}".format(counts))
    return "\n".join(L)


def resume_briefing(ctx, drift_report, compat, agent_adapter):
    """The compact agent-ready resume output (token-cheap, action-first)."""
    L = []
    L.append("Resume Context")
    L.append("=" * 40)
    git = ctx.get("git") or {}
    progress = ctx.get("progress") or {}
    L.append("Project:           {}".format(ctx["project"]["name"]))
    if ctx.get("goal"):
        L.append("Goal:              {}".format(ctx["goal"]))
    if ctx.get("current_objective"):
        L.append("Current Objective: {}".format(ctx["current_objective"]))
    L.append("Last Context:      {} ({})".format(
        ctx.get("context_id"), ctx.get("created_at")))
    L.append("Repository:        {} @ {}".format(
        git.get("branch") or "(no git)", git.get("head_short") or "-"))
    L.append("Drift:             {}".format(drift_report.get("level", "UNKNOWN")))
    if drift_report.get("summary"):
        L.append("  {}".format(redact(drift_report["summary"])))

    L.append("")
    L.append("Completed:")
    L.extend(_items(progress.get("completed")))
    L.append("Pending:")
    L.extend(_items(progress.get("pending")))

    architecture = ctx.get("architecture") or []
    if architecture:
        L.append("")
        L.append("Architecture:")
        for item in architecture[:6]:
            L.append("- {} [confidence {:.0%}; {}]".format(
                item.get("fact", ""), item.get("confidence", 0.0),
                item.get("freshness", "unknown"),
            ))

    decisions = ctx.get("decisions") or []
    if decisions:
        L.append("")
        L.append("Important Decisions:")
        for d in decisions:
            line = "- {}".format(d.get("decision", ""))
            if d.get("reason"):
                line += " ({})".format(d["reason"])
            L.append(line)

    L.append("")
    L.append("Agent:             {} ({}% compatible)".format(
        agent_adapter.get("display_name", agent_adapter.get("name")),
        compat.get("percent", 100)))
    req = list(compat.get("satisfied") or [])
    miss = compat.get("missing") or []
    if req or miss:
        cap_line = []
        if req:
            cap_line.append("✓ " + " ".join(req))
        if miss:
            cap_line.append("✗ " + " ".join(miss))
        L.append("  Capabilities:    " + " · ".join(cap_line))
    for w in compat.get("warnings") or []:
        L.append("  ⚠️ {}".format(w.get("message")))
        for t in w.get("affected") or []:
            L.append("     - affected: {}".format(t))

    if drift_report.get("stale_files"):
        L.append("")
        L.append("Files to read first (changed since context):")
        for p in drift_report["stale_files"][:6]:
            L.append("  1. {}".format(p))

    actions = ctx.get("recommended_actions") or []
    if actions:
        L.append("")
        L.append("Recommended Next Step:")
        for a in actions[:3]:
            text = a.get("text") if isinstance(a, dict) else str(a)
            L.append("  → {}".format(text))

    if drift_report.get("recommendations"):
        L.append("")
        L.append("Before you trust the context:")
        for r in drift_report["recommendations"][:3]:
            L.append("  - {}".format(r))
    return "\n".join(L) + "\n"


def diff_view(old, new):
    return render_diff(semantic_diff(old, new))

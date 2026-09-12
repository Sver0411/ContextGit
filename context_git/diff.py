"""diff — Semantic Context Diff between two Context Objects.

This is context-git's core differentiator. The output is NOT a JSON field
dump ("$.progress.completed[2] changed") — it is a structured, semantic
comparison that answers what a *new agent* actually cares about:

* what moved forward (progress)
* what decisions were made or reversed
* which issues appeared / were resolved
* which files entered or left the important set
* how the repository moved (HEAD, branch, working tree)
* how validation results changed

The engine is fully local and deterministic — structured comparison of two
Context Objects, no LLM, no API key. An LLM could summarise further later;
nothing here depends on one.

Item matching is text-normalised so "Implement refresh rotation" reworded
as "implement refresh-token rotation" still matches as the same item.
"""

from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _norm(text):
    """Normalise an item's text for identity matching."""
    if text is None:
        return ""
    if isinstance(text, dict):
        text = text.get("text") or text.get("decision") or ""
    normalised = unicodedata.normalize("NFKC", str(text).casefold())
    tokens = _WORD_RE.findall(normalised)
    return " ".join(tokens) if tokens else normalised.strip()


def _item_text(item):
    if isinstance(item, dict):
        return item.get("text") or item.get("decision") or str(item)
    return str(item)


def _index(items):
    """{norm_key: [display_text…]} preserving order."""
    idx = {}
    for it in items or []:
        idx.setdefault(_norm(_item_text(it)), []).append(_item_text(it))
    return idx


def _text_index_map(items):
    """{norm_key: first_display_text} for sections like constraints."""
    out = {}
    for it in items or []:
        k = _norm(_item_text(it))
        if k and k not in out:
            out[k] = _item_text(it)
    return out


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def semantic_diff(old, new):
    """Compare two Context Objects. Returns a structured semantic diff.

    Never raises; tolerates missing sections (older schema versions).
    """
    old = old or {}
    new = new or {}
    changes = []          # list of {section, kind, detail}
    changed_sections = set()

    def add(section, kind, detail):
        changes.append({"section": section, "kind": kind, "detail": detail})
        changed_sections.add(section)

    # -- identity -----------------------------------------------------------
    old_id = old.get("context_id") or "(none)"
    new_id = new.get("context_id") or "(none)"

    # -- goal / objective ----------------------------------------------------
    old_goal, new_goal = old.get("goal"), new.get("goal")
    if old_goal != new_goal:
        if old_goal is None:
            add("goal", "added", new_goal)
        else:
            add("goal", "changed", {"from": old_goal, "to": new_goal})

    old_obj, new_obj = old.get("current_objective"), new.get("current_objective")
    if old_obj != new_obj:
        add("objective", "changed" if old_obj else "added",
            {"from": old_obj, "to": new_obj} if old_obj else new_obj)

    # -- architecture facts -------------------------------------------------
    old_a = _text_index_map([
        a.get("fact") if isinstance(a, dict) else a for a in old.get("architecture") or []
    ])
    new_a = _text_index_map([
        a.get("fact") if isinstance(a, dict) else a for a in new.get("architecture") or []
    ])
    for k, text in new_a.items():
        if k not in old_a:
            add("architecture", "added", text)
    for k, text in old_a.items():
        if k not in new_a:
            add("architecture", "removed", text)

    # -- progress ---------------------------------------------------------------
    # Each item is reported at most once: unchanged items are silent, items
    # that switched buckets are one "moved" line, brand-new items are "added".
    old_p = old.get("progress") or {}
    new_p = new.get("progress") or {}
    buckets = (("completed", "completed"), ("in_progress", "in-progress"),
               ("pending", "pending"))
    old_index = {b: _index(old_p.get(b)) for b, _ in buckets}
    new_index = {b: _index(new_p.get(b)) for b, _ in buckets}
    old_bucket_of = {}
    for b, _ in buckets:
        for k in old_index[b]:
            old_bucket_of.setdefault(k, b)

    for b, label in buckets:
        for k, texts in new_index[b].items():
            prev = old_bucket_of.get(k)
            if prev == b:
                continue  # unchanged
            if prev is None:
                add("progress", "{}-added".format(b), {"text": texts[0], "label": label})
            else:
                add("progress", "moved", {"text": texts[0], "from": prev, "to": b})
    for b, _ in buckets:
        for k, texts in old_index[b].items():
            # gone entirely — present in no new bucket
            if not any(k in new_index[ob] for ob, _ in buckets):
                add("progress", "{}-removed".format(b),
                    {"text": texts[0], "label": label})

    # -- decisions -----------------------------------------------------------------
    old_d = _text_index_map([
        d.get("decision") if isinstance(d, dict) else d for d in old.get("decisions") or []
    ])
    new_d = _text_index_map([
        d.get("decision") if isinstance(d, dict) else d for d in new.get("decisions") or []
    ])
    for k, text in new_d.items():
        if k not in old_d:
            detail = text
            # attach reason/rejected if available
            for d in new.get("decisions") or []:
                if isinstance(d, dict) and _norm(d.get("decision")) == k:
                    parts = [text]
                    if d.get("reason"):
                        parts.append("reason: {}".format(d["reason"]))
                    if d.get("rejected"):
                        parts.append("rejected: {}".format("; ".join(d["rejected"])))
                    detail = " — ".join(parts)
                    break
            add("decisions", "added", detail)
    for k, text in old_d.items():
        if k not in new_d:
            add("decisions", "removed", text)

    # -- known issues (appearance / resolution) --------------------------------------
    old_i = _text_index_map(old.get("known_issues"))
    new_i = _text_index_map(new.get("known_issues"))
    for k, text in new_i.items():
        if k not in old_i:
            add("known_issues", "issue-opened", text)
    for k, text in old_i.items():
        if k not in new_i:
            add("known_issues", "issue-resolved", text)

    # -- constraints / do-not-change ---------------------------------------------------
    for section in ("constraints", "do_not_change"):
        old_c = _text_index_map(old.get(section))
        new_c = _text_index_map(new.get(section))
        for k, text in new_c.items():
            if k not in old_c:
                add(section, "added", text)
        for k, text in old_c.items():
            if k not in new_c:
                add(section, "removed", text)

    # -- important files -----------------------------------------------------------------
    def file_map(ctx):
        return {f.get("path"): f for f in ctx.get("important_files") or [] if f.get("path")}

    old_f, new_f = file_map(old), file_map(new)
    for path, meta in new_f.items():
        if path not in old_f:
            add("files", "file-added", {"path": path, "why": meta.get("why", "")})
        elif (old_f[path].get("fingerprint") and new_f[path].get("fingerprint")
              and old_f[path].get("fingerprint") != new_f[path].get("fingerprint")):
            add("files", "file-modified", {"path": path})
    for path, meta in old_f.items():
        if path not in new_f:
            add("files", "file-removed", {"path": path})

    # -- git ------------------------------------------------------------------------------
    old_g, new_g = old.get("git") or {}, new.get("git") or {}
    if old_g.get("head") != new_g.get("head"):
        if old_g.get("head") and new_g.get("head"):
            add("git", "head-moved", {
                "from": old_g.get("head_short") or old_g.get("head"),
                "to": new_g.get("head_short") or new_g.get("head"),
            })
        else:
            add("git", "head-changed",
                {"from": old_g.get("head_short"), "to": new_g.get("head_short")})
    if old_g.get("branch") != new_g.get("branch"):
        add("git", "branch-changed", {
            "from": old_g.get("branch") or "(detached)",
            "to": new_g.get("branch") or "(detached)",
        })
    old_wt = old_g.get("working_tree") or {}
    new_wt = new_g.get("working_tree") or {}
    old_files = set(old_wt.get("staged") or []) | set(old_wt.get("unstaged") or []) \
        | set(old_wt.get("untracked") or [])
    new_files = set(new_wt.get("staged") or []) | set(new_wt.get("unstaged") or []) \
        | set(new_wt.get("untracked") or [])
    if old_files != new_files:
        detail = {
            "changed_files": len(old_files ^ new_files),
            "working_tree_before": "clean" if old_wt.get("clean") else "dirty",
            "working_tree_after": "clean" if new_wt.get("clean") else "dirty",
        }
        add("git", "working-tree-changed", detail)
    old_ds, new_ds = old_g.get("diff_stat") or {}, new_g.get("diff_stat") or {}
    if old_ds.get("additions") != new_ds.get("additions") or \
       old_ds.get("deletions") != new_ds.get("deletions"):
        add("git", "diff-stat-changed", {
            "before": "+{}/-{}".format(old_ds.get("additions", 0), old_ds.get("deletions", 0)),
            "after": "+{}/-{}".format(new_ds.get("additions", 0), new_ds.get("deletions", 0)),
        })

    # -- validation ---------------------------------------------------------------------------
    old_v, new_v = old.get("validation") or {}, new.get("validation") or {}
    for k in ("build", "test", "lint", "typecheck"):
        ov = (old_v.get(k) or {}).get("result") if isinstance(old_v.get(k), dict) else old_v.get(k)
        nv = (new_v.get(k) or {}).get("result") if isinstance(new_v.get(k), dict) else new_v.get(k)
        if ov != nv:
            add("validation", "validation-changed",
                {"check": k, "from": ov or "not recorded", "to": nv or "not recorded"})

    # -- recommended actions ---------------------------------------------------------------------
    old_r = _index(old.get("recommended_actions"))
    new_r = _index(new.get("recommended_actions"))
    for k, texts in new_r.items():
        if k not in old_r:
            add("recommended_actions", "action-added", texts[0])
    for k, texts in old_r.items():
        if k not in new_r:
            add("recommended_actions", "action-removed", texts[0])

    return {
        "from": old_id,
        "to": new_id,
        "from_created_at": old.get("created_at"),
        "to_created_at": new.get("created_at"),
        "changed": bool(changes),
        "sections": sorted(changed_sections),
        "changes": changes,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_KIND_LABELS = {
    "added": "+", "changed": "~", "removed": "-", "moved": "~",
    "completed-added": "+", "in_progress-added": "+", "pending-added": "+",
    "completed-removed": "-", "in_progress-removed": "-", "pending-removed": "-",
    "issue-opened": "+", "issue-resolved": "-",
    "file-added": "+", "file-modified": "M", "file-removed": "D",
    "head-moved": "~", "head-changed": "~", "branch-changed": "~",
    "working-tree-changed": "~", "diff-stat-changed": "~",
    "validation-changed": "~", "action-added": "+", "action-removed": "-",
}

_SECTION_TITLES = {
    "goal": "Goal",
    "objective": "Current Objective",
    "architecture": "Architecture",
    "progress": "Progress",
    "decisions": "Decisions",
    "known_issues": "Known Issues",
    "constraints": "Constraints",
    "do_not_change": "Do Not Change",
    "files": "Files",
    "git": "Repository",
    "validation": "Validation",
    "recommended_actions": "Next Step",
}

_SECTION_ORDER = [k for k in _SECTION_TITLES]


def _show(value):
    """Display-safe rendering of a value (None → readable placeholder)."""
    if value is None:
        return "(not recorded)"
    return str(value)


def format_change(c):
    """One human line for one semantic change."""
    kind, section, detail = c["kind"], c["section"], c["detail"]
    sym = _KIND_LABELS.get(kind, "~")

    if section == "progress":
        text = detail["text"] if isinstance(detail, dict) else detail
        if kind == "moved":
            return "  {} {} ({} → {})".format(sym, text, detail["from"], detail["to"])
        return "  {} {}".format(sym, text)
    if section == "goal":
        if kind == "changed":
            return "  ~ Goal changed: “{}” → “{}”".format(
                _show(detail["from"]), _show(detail["to"]))
        return "  + Goal: {}".format(_show(detail))
    if section == "objective":
        if isinstance(detail, dict):
            return "  ~ Objective: “{}” → “{}”".format(
                _show(detail["from"]), _show(detail["to"]))
        return "  + Objective: {}".format(_show(detail))
    if section == "known_issues":
        verb = "RESOLVED" if kind == "issue-resolved" else "NEW ISSUE"
        return "  {} {} [{}]".format(sym, detail, verb)
    if section == "files":
        path = detail["path"] if isinstance(detail, dict) else detail
        return "  {} {}".format(sym, path)
    if section == "git":
        if kind in ("head-moved", "head-changed"):
            return "  ~ HEAD: {} → {}".format(_show(detail.get("from")), _show(detail.get("to")))
        if kind == "branch-changed":
            return "  ~ Branch: {} → {}".format(_show(detail["from"]), _show(detail["to"]))
        if kind == "working-tree-changed":
            return "  ~ Working tree: {} → {} ({} file(s) in play)".format(
                detail["working_tree_before"], detail["working_tree_after"],
                detail["changed_files"])
        if kind == "diff-stat-changed":
            return "  ~ Diff stat: {} → {}".format(detail["before"], detail["after"])
    if section == "validation":
        return "  ~ {}: {} → {}".format(
            detail["check"], _show(detail["from"]), _show(detail["to"]))
    if section == "decisions":
        return "  {} {}".format(sym, detail)
    return "  {} {}".format(sym, detail)


def render(diff_obj):
    """Render a semantic diff the way the spec's example looks."""
    L = []
    L.append("Context Diff")
    L.append("  {} → {}".format(diff_obj["from"], diff_obj["to"]))
    if diff_obj.get("to_created_at"):
        L.append("  at {}".format(diff_obj["to_created_at"]))
    L.append("")
    if not diff_obj["changed"]:
        L.append("  (no semantic change — context is identical)")
        return "\n".join(L)

    by_section = {}
    for c in diff_obj["changes"]:
        by_section.setdefault(c["section"], []).append(c)

    for section in _SECTION_ORDER:
        if section not in by_section:
            continue
        L.append(_SECTION_TITLES[section])
        for c in by_section[section]:
            L.append(format_change(c))
        L.append("")
    return "\n".join(L).rstrip() + "\n"

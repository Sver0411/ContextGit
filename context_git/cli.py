"""cli — context-git command line interface.

Commands:
    init                     create .context-git/ store
    snapshot                 capture a new Context Object (sets HEAD)
    commit                   snapshot that refuses no-op changes (alias semantics)
    status                   current context + live drift + recommendation
    diff [old] [new]         semantic diff between two contexts
    log                      context history (parent chain)
    show [ctx_id]            pretty-print one context
    resume [ctx_id]          agent-ready briefing: drift + capability + next steps
    checkout <ctx_id|->      move context HEAD (never touches your git repo)
    verify                   residual secret scan over the store
    sessions                 discover project-matching private sessions (opt-in)
    import-session           create a context from an explicit private session
    capabilities             auto-profile the current agent environment
    branch                   list/create/delete context branches
    switch                   switch the active context branch
    merge                    three-way merge another context branch

Global flags: --root, --json, -q. Run from any directory inside the project.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .common import eprint, read_json, write_text
from .context import build as build_context, is_meaningful_change
from .capabilities import compatibility_report, detect_agent, load_adapters
from .diff import semantic_diff, render as render_diff
from .drift import check as drift_check, format_report
from .merge import MergeError, merge_contexts
from .render import handoff_md, log as render_log, resume_briefing, show as render_show
from .security import redact, scan_object
from .sessions import (
    SessionImportError, discover_sessions, import_metadata, infer_provider,
    read_session, session_to_notes,
)
from .storage import Store, StoreError, utc_now_iso

PROMPT_FIELDS = [
    ("goal", "Project goal (what is this project ultimately for)?"),
    ("current_objective", "Current objective (what are you working on right now)?"),
    ("recommended_actions", "Recommended next step(s) for the next agent?"),
]

LIST_KEYS = {
    "completed", "in_progress", "pending", "constraints", "do_not_change",
    "known_issues", "recommended_actions", "capabilities_required",
}


# --------------------------------------------------------------------------
# Note collection
# --------------------------------------------------------------------------

def parse_set_args(pairs):
    """Parse --set KEY=VALUE pairs into a notes dict.

    List fields accept comma-separated values. `decisions` accepts a JSON
    array string. Validation uses dotted form: --set validation.test=pass.
    """
    notes = {}
    for item in pairs or []:
        if "=" not in item:
            eprint("warning: ignoring malformed --set {!r} (expected KEY=VALUE)".format(item))
            continue
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()

        if key.startswith("validation."):
            sub = key.split(".", 1)[1]
            notes.setdefault("validation", {})[sub] = value
            continue

        if key in ("decisions", "architecture"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    notes.setdefault(key, []).extend(parsed)
                    continue
            except json.JSONDecodeError:
                pass
            notes.setdefault(key, []).append(value)
            continue

        if key in LIST_KEYS:
            values = [v.strip() for v in value.split(",") if v.strip()]
            notes.setdefault(key, []).extend(values)
            continue

        if key in ("goal", "current_objective", "notes"):
            notes[key] = value
            continue

        eprint("warning: unknown --set key {!r} ignored".format(key))
    return notes


def _interactive_notes(skip=False):
    """Prompt for the P0 narrative when stdin is a TTY. Empty answer = skip."""
    notes = {}
    if skip or not (sys.stdin is not None and sys.stdin.isatty()):
        return notes
    print("\ncontext-git — capture your working context.")
    print("Press Enter to skip any question.\n")
    for key, question in PROMPT_FIELDS:
        try:
            answer = input("{}\n> ".format(question)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if answer:
            if key == "recommended_actions":
                notes[key] = [a.strip() for a in answer.split(",") if a.strip()]
            else:
                notes[key] = answer
        print()
    return notes


# --------------------------------------------------------------------------
# Shared snapshot machinery
# --------------------------------------------------------------------------

def _snapshot(root, args, require_change):
    store = Store(root)
    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")

    notes = dict(getattr(args, "imported_notes", None) or {})
    # Explicit --set values are corrections and therefore replace a
    # heuristic session extraction field-by-field.
    notes.update(parse_set_args(args.set))
    notes.update(_interactive_notes(skip=args.no_prompt))

    if store.current_branch() is None:
        if store.refs_dir.is_dir():
            current = store.head()
            eprint(
                "cannot commit from historical HEAD {} while context HEAD is detached."
                .format(current or "(empty)")
            )
            eprint("Create a branch with `context-git switch -c NAME`, or use `checkout -`.")
            return 1
        store.ensure_branch_layout()
    parent = store.head_object()
    parent_id = parent.get("context_id") if parent else None

    supplied_agent = getattr(args, "source_agent_obj", None)
    if supplied_agent:
        agent, how = supplied_agent, "session-import"
    else:
        agent, how = detect_agent(getattr(args, "agent", None), root=root)
    if not args.quiet:
        eprint("[snapshot] agent: {} ({})".format(agent.get("name"), how))

    ctx = build_context(
        root, notes,
        parent_id=parent_id,
        parent_obj=parent,
        source_agent=agent,
        message=getattr(args, "message", None),
        session_import=getattr(args, "imported_session_meta", None),
        excluded_paths=[getattr(args, "imported_source_path", None)]
        if getattr(args, "imported_source_path", None) else None,
        parent_ids=[parent_id] if parent_id else [],
        context_branch=store.current_branch(),
    )

    # Security gate FIRST — even a no-op commit must never be the place a
    # secret gets silently dropped (fail closed, always).
    findings = scan_object(ctx)
    if findings:
        eprint("[snapshot] ✋ potential secrets detected in context payload:")
        for reason, snippet in findings[:5]:
            eprint("    - {}: {}".format(reason, snippet))
        eprint("nothing written. Remove the secret from your notes/diff and retry.")
        return 2

    if require_change and parent is not None and not is_meaningful_change(parent, ctx):
        if getattr(args, "allow_empty", False):
            eprint("[snapshot] note: no semantic change vs parent (forced with --allow-empty)")
        else:
            eprint("nothing to commit: context is semantically identical to {}.".format(parent_id))
            eprint("Use --allow-empty to force an explicit checkpoint.")
            return 1

    ctx_id = store.save_context(ctx)
    store.set_head(ctx_id)

    # Render HANDOFF.md from the new HEAD, with live drift + compat views.
    drift = drift_check(root, ctx)
    compat = compatibility_report(ctx, agent) if ctx.get("capabilities_required") else None
    write_text(store.dir / "HANDOFF.md", handoff_md(ctx, drift_report=drift, compat=compat))

    if args.quiet:
        print(ctx_id)
    else:
        eprint("[snapshot] saved {} (parent: {})".format(
            ctx_id, parent_id or "root"))
        eprint("[snapshot] HEAD → {}".format(ctx_id))
        eprint("[snapshot] rendered .context-git/HANDOFF.md")
        important = [f["path"] for f in ctx.get("important_files") or []]
        eprint("[snapshot] important files ({}): {}".format(
            len(important), ", ".join(important[:6]) + ("…" if len(important) > 6 else "")))
        _hint_gitignore(root)
    return 0


def _hint_gitignore(root):
    """One-time hint: contexts are for agents; humans usually gitignore them."""
    try:
        gi = (Path(root) / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        gi = ""
    if ".context-git" not in gi:
        eprint("[hint] consider adding '.context-git/' to .gitignore "
               "(contexts are agent state, usually not committed). "
               "context-git never edits your files — this is only a suggestion.")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(root, args):
    store = Store(root)
    created = store.init("context-git", __version__)
    if created:
        if not args.quiet:
            eprint("initialised store at {}".format(store.dir))
        return 0
    eprint("store already exists at {}".format(store.dir))
    return 0


def cmd_snapshot(root, args):
    return _snapshot(root, args, require_change=False)


def cmd_commit(root, args):
    return _snapshot(root, args, require_change=True)


def cmd_status(root, args):
    store = Store(root)
    ctx = store.head_object()
    if ctx is None:
        if args.json:
            print(json.dumps({
                "store": str(store.dir),
                "store_exists": store.exists(),
                "context_id": None,
                "drift_level": "UNKNOWN",
                "recommendation": "run context-git snapshot",
            }, indent=2, ensure_ascii=False))
            return 1
        print("Context Git Status")
        print("  Store:      {} ({})".format(
            store.dir, "present" if store.exists() else "missing — run `context-git init`"))
        print("  Context:    (none yet)")
        print("  Recommendation: run `context-git snapshot` to capture the current state.")
        return 1

    drift = drift_check(root, ctx)

    if args.json:
        print(json.dumps({
            "context_id": ctx.get("context_id"),
            "created_at": ctx.get("created_at"),
            "context_branch": store.current_branch(),
            "drift_level": drift["level"],
            "signals": drift["signals"],
            "stale_files": drift.get("stale_files", []),
            "stale_sections": drift.get("stale_sections", []),
            "validation_freshness": drift.get("validation_freshness"),
            "recommendations": drift.get("recommendations", []),
        }, indent=2, ensure_ascii=False))
        return 0 if drift["level"] == "NONE" else 3

    print("Context Git Status")
    print("  Current Context: {}".format(ctx.get("context_id")))
    print("  Context Branch:  {}".format(store.current_branch() or "(detached)"))
    print("  Captured:        {}".format(ctx.get("created_at")))
    if ctx.get("message"):
        print("  Summary:         {}".format(ctx["message"]))
    git = ctx.get("git") or {}
    print("  Repository:      {} @ {}".format(
        git.get("branch") or "(no git)", git.get("head_short") or "-"))

    print("  Context:         {}".format(
        "FRESH" if drift["level"] == "NONE" else "STALE"))
    if drift["level"] != "NONE":
        print("  Reason:          {}".format(drift["summary"]))
        for p in drift.get("stale_files") or []:
            print("    - changed: {}".format(p))
        if drift.get("counts", {}).get("commits_since"):
            print("  Commits since snapshot: {}".format(drift["counts"]["commits_since"]))

    if drift.get("validation_freshness") == "STALE":
        print("  Validation:      results recorded earlier are STALE (code changed since)")

    print("  Recommendation:  {}".format(
        "context is current — nothing to do."
        if drift["level"] == "NONE"
        else "create a new context snapshot (`context-git snapshot`)."))

    return 0 if drift["level"] == "NONE" else 3


def cmd_diff(root, args):
    store = Store(root)
    old_id, new_id = args.targets[0] if len(args.targets) > 0 else None, \
        args.targets[1] if len(args.targets) > 1 else None
    if not old_id and not new_id:
        head = store.head_object()
        parent_id = (head or {}).get("parent_context_id")
        old = store.load_context(parent_id) if parent_id else None
        new = head
        if new is None:
            eprint("no contexts yet — nothing to diff.")
            return 1
    elif old_id and new_id:
        old_resolved, new_resolved = store.resolve(old_id), store.resolve(new_id)
        old, new = store.load_context(old_resolved), store.load_context(new_resolved)
        if old is None or new is None:
            eprint("unknown context id(s): {} {}".format(
                old_id if old is None else "", new_id if new is None else "").strip())
            return 1
    else:
        # one arg given: diff that context against its parent
        target_id = store.resolve(old_id)
        target = store.load_context(target_id)
        if target is None:
            eprint("unknown context id: {}".format(old_id))
            return 1
        old = store.load_context(target.get("parent_context_id")) \
            if target.get("parent_context_id") else None
        new = target

    diff_obj = semantic_diff(old, new)
    if args.json:
        print(json.dumps(diff_obj, indent=2, ensure_ascii=False))
    else:
        print(render_diff(diff_obj))
    return 0


def cmd_log(root, args):
    store = Store(root)
    if not store.exists():
        eprint("no context store here — run `context-git init` first.")
        return 1
    history = store.all_contexts(limit=args.limit) if args.all else store.history(limit=args.limit)
    print(render_log(history, decorations=store.decorations()))
    return 0


def cmd_show(root, args):
    store = Store(root)
    resolved = store.resolve(args.ctx_id) if args.ctx_id else store.head()
    ctx = store.load_context(resolved) if resolved else None
    if ctx is None:
        eprint("context not found: {}".format(args.ctx_id or "(HEAD)"))
        return 1
    if args.json:
        print(json.dumps(ctx, indent=2, ensure_ascii=False))
    else:
        print(render_show(ctx))
    return 0


def cmd_resume(root, args):
    store = Store(root)
    resolved = store.resolve(args.ctx_id) if args.ctx_id else store.head()
    ctx = store.load_context(resolved) if resolved else None
    if ctx is None:
        eprint("no context to resume ({}).".format(args.ctx_id or "HEAD is empty"))
        eprint("Run `context-git snapshot` in the source agent first.")
        return 1

    drift = drift_check(root, ctx)
    agent, how = detect_agent(getattr(args, "agent", None), root=root)
    compat = compatibility_report(ctx, agent)
    briefing = resume_briefing(ctx, drift, compat, agent)

    if args.json:
        print(json.dumps({
            "context_id": ctx.get("context_id"),
            "drift": dict(drift),
            "agent": {"name": agent.get("name"), "detected_via": how},
            "compatibility": compat,
            "briefing": briefing,
        }, indent=2, ensure_ascii=False, default=str))
    else:
        if not args.quiet:
            print(format_report(drift))
            print()
        print(briefing)

    if args.write_briefing:
        write_text(store.dir / "RESUME_BRIEFING.md", briefing)
        if not args.quiet:
            eprint("\n[resume] briefing written to .context-git/RESUME_BRIEFING.md")
    return 0


def cmd_checkout(root, args):
    store = Store(root)
    requested = args.target
    current_position = store.position()
    current_id = store.head()
    restore = store.recalled_position() if requested == "-" else None
    if requested == "-" and not restore:
        eprint("no previous position to check out.")
        return 1

    try:
        token = restore or requested
        if token.startswith("branch:"):
            branch = token.split(":", 1)[1]
            target_id = store.switch_branch(branch)
            label = branch
        elif token.startswith("context:"):
            target_id = token.split(":", 1)[1]
            store.detach(target_id)
            label = target_id
        elif token in store.list_branches():
            target_id = store.switch_branch(token)
            label = token
        else:
            target_id = store.resolve(token)
            if target_id is None:
                raise StoreError("unknown context or branch: {}".format(token))
            store.detach(target_id)
            label = target_id
    except StoreError as exc:
        eprint(str(exc))
        return 1

    if store.position() == current_position:
        print("already at {}".format(label))
        return 0
    store.remember_position(current_position)
    ctx = store.head_object()
    _refresh_handoff(root, store, ctx)
    branch = store.current_branch()
    print("context HEAD → {}{}".format(
        target_id, " ({})".format(branch) if branch else " (detached)"
    ))
    if ctx and not args.quiet:
        print("(`context-git show` to inspect · `checkout -` to return to {})".format(
            current_id or "-"))
    # Your git repository was not touched: checkout only moves the context pointer.
    return 0


def _refresh_handoff(root, store, ctx=None):
    ctx = ctx or store.head_object()
    if not ctx:
        return
    drift = drift_check(root, ctx)
    agent, _ = detect_agent(None, root=root)
    compat = compatibility_report(ctx, agent) \
        if ctx.get("capabilities_required") else None
    write_text(
        store.dir / "HANDOFF.md",
        handoff_md(ctx, drift_report=drift, compat=compat),
    )


def cmd_branch(root, args):
    """List, create, or delete context refs."""
    store = Store(root)
    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")
    if store.current_branch() is None and not store.refs_dir.is_dir():
        store.ensure_branch_layout()

    if args.delete:
        store.delete_branch(args.delete)
        if args.json:
            print(json.dumps({"deleted": args.delete}, indent=2))
        else:
            print("deleted context branch {} (contexts were retained)".format(args.delete))
        return 0

    if args.name:
        start = store.resolve(args.start) if args.start else store.head()
        if args.start and start is None:
            raise StoreError("unknown start context or branch: {}".format(args.start))
        store.create_branch(args.name, start)
        if args.json:
            print(json.dumps({"created": args.name, "context_id": start}, indent=2))
        else:
            print("created context branch {} at {}".format(args.name, start or "(unborn)"))
        return 0

    branches = store.list_branches()
    current = store.current_branch()
    if args.json:
        print(json.dumps({
            "current": current,
            "branches": [
                {"name": name, "context_id": ctx_id, "current": name == current}
                for name, ctx_id in branches.items()
            ],
        }, indent=2, ensure_ascii=False))
    else:
        for name, ctx_id in branches.items():
            print("{} {:24} {}".format(
                "*" if name == current else " ", name, ctx_id or "(unborn)"
            ))
        if not branches:
            print("(no context branches)")
    return 0


def cmd_switch(root, args):
    """Switch context branches without touching source-control state."""
    store = Store(root)
    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")
    if store.current_branch() is None and not store.refs_dir.is_dir():
        store.ensure_branch_layout()
    previous = store.position()
    if args.create:
        start = store.resolve(args.start) if args.start else store.head()
        if args.start and start is None:
            raise StoreError("unknown start context or branch: {}".format(args.start))
        store.create_branch(args.create, start)
        target = args.create
    else:
        target = args.name
    ctx_id = store.switch_branch(target)
    if previous != store.position():
        store.remember_position(previous)
    _refresh_handoff(root, store)
    if args.json:
        print(json.dumps({"branch": target, "context_id": ctx_id}, indent=2))
    else:
        print("switched context branch to {} at {}".format(target, ctx_id or "(unborn)"))
        print("source Git branch and working tree were not touched")
    return 0


def _parse_resolutions(items):
    result = {}
    for item in items or []:
        if "=" not in item:
            raise MergeError("resolution must be KEY=ours|theirs|base")
        key, choice = item.rsplit("=", 1)
        key, choice = key.strip(), choice.strip().lower()
        if not key or choice not in ("ours", "theirs", "base"):
            raise MergeError("resolution must be KEY=ours|theirs|base")
        result[key] = choice
    return result


def _print_merge_plan(plan, source):
    print("Context Merge")
    print("  ours:   {}".format(plan.get("ours")))
    print("  theirs: {} ({})".format(plan.get("theirs"), source))
    print("  base:   {}".format(plan.get("base") or "(unrelated)"))
    if plan.get("conflicts"):
        print("  conflicts: {}".format(len(plan["conflicts"])))
        for conflict in plan["conflicts"]:
            print("    - {}".format(conflict["key"]))
        print("Resolve explicitly with `--resolve KEY=ours|theirs|base`.")
    else:
        fields = [key for key, value in plan.get("notes", {}).items() if value]
        print("  mergeable fields: {}".format(", ".join(fields) or "(empty state)"))


def cmd_merge(root, args):
    """Three-way merge a context branch or context id into the current branch."""
    store = Store(root)
    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")
    if store.current_branch() is None:
        if not store.refs_dir.is_dir():
            store.ensure_branch_layout()
        else:
            raise StoreError("cannot merge into detached context HEAD; switch to a branch first")
    branch = store.current_branch()
    ours_id = store.head()
    theirs_id = store.resolve(args.source)
    if ours_id is None:
        raise StoreError("current context branch has no context")
    if theirs_id is None:
        raise StoreError("unknown context or branch: {}".format(args.source))
    if ours_id == theirs_id or store.is_ancestor(theirs_id, ours_id):
        message = "already up to date: {} contains {}".format(branch, args.source)
        if args.json:
            print(json.dumps({"action": "up-to-date", "context_id": ours_id}, indent=2))
        else:
            print(message)
        return 0

    if store.is_ancestor(ours_id, theirs_id) and not args.no_ff:
        if args.dry_run:
            payload = {"action": "fast-forward", "from": ours_id, "to": theirs_id,
                       "branch": branch, "dry_run": True}
            print(json.dumps(payload, indent=2) if args.json else
                  "would fast-forward {}: {} → {}".format(branch, ours_id, theirs_id))
            return 0
        store.set_head(theirs_id)
        _refresh_handoff(root, store)
        if args.json:
            print(json.dumps({"action": "fast-forward", "from": ours_id,
                              "to": theirs_id, "branch": branch}, indent=2))
        else:
            print("fast-forwarded context branch {}: {} → {}".format(
                branch, ours_id, theirs_id
            ))
        return 0

    base_id = store.merge_base(ours_id, theirs_id)
    if base_id is None and not args.allow_unrelated:
        raise MergeError("contexts have no common ancestor; use --allow-unrelated intentionally")
    base = store.load_context(base_id) if base_id else {}
    ours = store.load_context(ours_id)
    theirs = store.load_context(theirs_id)
    resolutions = _parse_resolutions(args.resolve)
    plan = merge_contexts(
        base, ours, theirs, resolutions=resolutions, resolve_all=args.resolve_all
    )

    if args.dry_run or plan["conflicts"]:
        if args.json:
            preview = dict(plan)
            preview["source"] = args.source
            preview["dry_run"] = True
            print(json.dumps(preview, indent=2, ensure_ascii=False))
        else:
            _print_merge_plan(plan, args.source)
            if args.dry_run and not plan["conflicts"]:
                print("No files were written (--dry-run).")
        return 4 if plan["conflicts"] else 0

    agent, how = detect_agent(getattr(args, "agent", None), root=root)
    if not args.quiet:
        eprint("[merge] agent: {} ({})".format(agent.get("name"), how))
    merge_meta = {
        "base_context_id": base_id,
        "source_context_id": theirs_id,
        "source_ref": args.source if args.source in store.list_branches() else None,
        "strategy": "three-way",
        "resolved_conflicts": plan["resolved"],
    }
    ctx = build_context(
        root, plan["notes"],
        parent_id=ours_id,
        parent_ids=[ours_id, theirs_id],
        parent_obj=ours,
        source_agent=agent,
        message=args.message or "Merge context {} into {}".format(args.source, branch),
        context_branch=branch,
        merge_info=merge_meta,
    )
    findings = scan_object(ctx)
    if findings:
        eprint("[merge] potential secrets detected; nothing written")
        return 2
    ctx_id = store.save_context(ctx)
    store.set_head(ctx_id)
    _refresh_handoff(root, store, ctx)
    if args.json:
        print(json.dumps({
            "action": "merge", "context_id": ctx_id, "branch": branch,
            "parents": [ours_id, theirs_id], "base": base_id,
        }, indent=2))
    else:
        print("merged {} into {} as {}".format(args.source, branch, ctx_id))
        print("parents: {}, {} · base: {}".format(ours_id, theirs_id, base_id or "none"))
    return 0


def cmd_verify(root, args):
    """Second-opinion secret scan over stored contexts."""
    store = Store(root)
    targets = []
    if args.ctx_id:
        resolved = store.resolve(args.ctx_id)
        if resolved is None:
            eprint("unknown context or branch: {}".format(args.ctx_id))
            return 1
        targets.append(store.context_path(resolved))
    else:
        targets = [store.context_path(cid) for cid in store.list_context_ids()]
        handoff = store.dir / "HANDOFF.md"
        if handoff.is_file():
            targets.append(handoff)
    if not targets:
        eprint("nothing to verify — no contexts stored.")
        return 1
    clean = True
    for t in targets:
        if not t.is_file():
            eprint("missing: {}".format(t.name))
            clean = False
            continue
        try:
            text = t.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            eprint("cannot read {}: {}".format(t.name, exc))
            clean = False
            continue
        from .security import scan_text
        findings = scan_text(text)
        if findings:
            clean = False
            print("{}: {} potential leak(s)".format(t.name, len(findings)))
            for reason, snippet in findings[:10]:
                print("  - {}: {}".format(reason, snippet))
        else:
            print("{}: clean".format(t.name))
    return 0 if clean else 2


def cmd_adapters(root, args):
    adapters = load_adapters()
    agent, how = detect_agent(getattr(args, "agent", None), root=root)
    if args.json:
        print(json.dumps({
            "available": sorted(adapters.keys()),
            "detected": {"name": agent.get("name"), "via": how,
                         "capabilities": agent.get("capabilities")},
        }, indent=2, ensure_ascii=False))
        return 0
    print("Available adapters: {}".format(", ".join(sorted(adapters.keys()))))
    print("Detected agent:     {} ({}) — capabilities: {}".format(
        agent.get("display_name", agent.get("name")), how,
        ", ".join(agent.get("capabilities") or [])))
    return 0


def cmd_capabilities(root, args):
    """Show the V2 static-declaration + environment-probe profile."""
    agent, how = detect_agent(getattr(args, "agent", None), root=root)
    payload = {
        "agent": agent.get("name"),
        "display_name": agent.get("display_name", agent.get("name")),
        "detected_via": how,
        "effective_capabilities": agent.get("capabilities") or [],
        "declared_capabilities": agent.get("declared_capabilities") or [],
        "profile": agent.get("capability_profile") or {},
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print("Agent Capability Profile")
    print("  Agent: {} ({})".format(payload["display_name"], how))
    for name in sorted(payload["profile"]):
        entry = payload["profile"][name]
        print("  {:18} {:11} {}".format(
            name, entry.get("status", "unknown"), entry.get("evidence", "")
        ))
    print("  Effective: {}".format(
        ", ".join(payload["effective_capabilities"]) or "(none)"
    ))
    return 0


def _session_provider(root, requested, source=None):
    if requested:
        return requested.strip().lower()
    if source:
        inferred = infer_provider(source)
        if inferred != "generic":
            return inferred
    agent, _ = detect_agent(None, root=root)
    return agent.get("name") or "generic"


def cmd_sessions(root, args):
    """Opt-in discovery of private sessions associated with this project."""
    provider = _session_provider(root, getattr(args, "agent", None))
    found = discover_sessions(provider, root, limit=args.limit)
    if args.json:
        print(json.dumps({
            "provider": provider,
            "project_root": str(root),
            "sessions": found,
        }, indent=2, ensure_ascii=False))
        return 0 if found else 1
    if not found:
        eprint("no {} sessions with an embedded cwd matching {}".format(provider, root))
        eprint("Pass an explicit file to `context-git import-session FILE` instead.")
        return 1
    print("Private sessions for {} ({})".format(root.name, provider))
    for index, item in enumerate(found, 1):
        selector = " · session {}".format(item["session_id"]) \
            if item.get("session_id") else ""
        count = " · {} visible messages".format(item["message_count"]) \
            if item.get("message_count") is not None else ""
        print("  {}. {}{}{}".format(index, item["path"], selector, count))
    print("Nothing was imported. Choose a path explicitly, or use import-session --latest.")
    return 0


def _same_path(left, right):
    try:
        return Path(left).expanduser().resolve() == Path(right).resolve()
    except OSError:
        return False


def cmd_import_session(root, args):
    """Compress an explicitly selected private session into a context."""
    if args.source and args.latest:
        raise SessionImportError("choose either an explicit session file or --latest")
    provider = _session_provider(root, getattr(args, "agent", None), args.source)
    source = args.source
    if args.latest:
        found = discover_sessions(provider, root, limit=1)
        if not found:
            raise SessionImportError(
                "no {} session has an embedded cwd matching this project"
                .format(provider)
            )
        source = found[0]["path"]
        args.session_id = found[0].get("session_id")
    if not source:
        raise SessionImportError("pass a session file or use --latest")

    read_root = None if (args.allow_other_project and args.session_id) else root
    parsed = read_session(
        source, provider=provider, project_root=read_root, session_id=args.session_id,
    )
    embedded_root = parsed.get("project_root")
    if embedded_root and not _same_path(embedded_root, root) and not args.allow_other_project:
        raise SessionImportError(
            "session belongs to a different project; pass --allow-other-project "
            "only if that is intentional"
        )
    store = Store(root)
    parent = store.head_object() if store.exists() else None
    notes = session_to_notes(parsed, include_goal=not bool((parent or {}).get("goal")))
    if not notes:
        raise SessionImportError("session contained no importable work-state fields")
    provenance = import_metadata(parsed, notes)

    preview = {
        "provider": provider,
        "message_count": parsed.get("message_count", 0),
        "extracted": notes,
        "provenance": provenance,
    }
    if args.dry_run:
        if args.json:
            print(json.dumps(preview, indent=2, ensure_ascii=False))
        else:
            print("Session Import Preview")
            print("  Provider: {}".format(provider))
            print("  Messages considered: {}".format(parsed.get("message_count", 0)))
            print("  Extracted fields: {}".format(
                ", ".join(sorted(notes.keys()))
            ))
            print(json.dumps(notes, indent=2, ensure_ascii=False))
            print("No files were written (--dry-run).")
        return 0

    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")
    # The session may be an export from another machine. Do not project this
    # host's runtime probes onto the historical source agent.
    agent, _ = detect_agent(provider, root=root, probe=False)
    args.source_agent_obj = agent
    args.imported_notes = notes
    args.imported_session_meta = provenance
    args.imported_source_path = source
    if not args.message:
        args.message = "Imported {} session context".format(provider)
    return _snapshot(root, args, require_change=parent is not None)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser():
    # Shared parent so global flags work *after* the subcommand too
    # (`context-git show --json`, not only `context-git --json show`).
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS is important because these flags exist on both the top-level
    # parser and every subparser. A subparser default must not overwrite a
    # value supplied before the command (`--root DIR status`).
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="project root (default: cwd)")
    common.add_argument("--json", action="store_true",
                        help="machine-readable JSON output where supported")
    common.add_argument("-q", "--quiet", action="store_true", help="minimal output")

    ap = argparse.ArgumentParser(
        prog="context-git",
        parents=[common],
        description="Git for AI Agent Context — version, diff, drift-check and "
                    "resume an agent's working state (UACP/1.0).",
    )
    ap.add_argument("--version", action="version", version="context-git {}".format(__version__))
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("init", parents=[common], help="create .context-git/ store")
    p.set_defaults(func=cmd_init, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser("snapshot", parents=[common], help="capture a new context (sets HEAD)")
    _add_snapshot_flags(p)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("commit", parents=[common], help="snapshot that refuses no-op changes")
    _add_snapshot_flags(p)
    p.add_argument("--allow-empty", action="store_true",
                   help="allow committing even with no semantic change")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("status", parents=[common], help="current context + live drift")
    p.set_defaults(func=cmd_status, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser("diff", parents=[common], help="semantic diff between two contexts")
    p.add_argument("targets", nargs="*", metavar="ctx_id",
                   help="0 args: HEAD vs parent · 1 arg: ctx vs its parent · 2 args: ctx ctx")
    p.set_defaults(func=cmd_diff, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   write_briefing=False)

    p = sub.add_parser("log", parents=[common], help="context history")
    p.add_argument("--limit", type=int, default=30, help="max entries (default 30)")
    p.add_argument("--all", action="store_true",
                   help="show contexts reachable from all branches")
    p.set_defaults(func=cmd_log, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser("show", parents=[common], help="pretty-print one context")
    p.add_argument("ctx_id", nargs="?", default=None, help="context id (default: HEAD)")
    p.set_defaults(func=cmd_show, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser("resume", parents=[common], help="agent-ready briefing + drift + compatibility")
    p.add_argument("ctx_id", nargs="?", default=None, help="context id (default: HEAD)")
    p.add_argument("--agent", default=None, help="target agent name (default: auto-detect)")
    p.add_argument("--write-briefing", action="store_true",
                   help="also write .context-git/RESUME_BRIEFING.md")
    p.set_defaults(func=cmd_resume, set=[], no_prompt=True, message=None,
                   allow_empty=False, limit=None, targets=None)

    p = sub.add_parser("checkout", parents=[common], help="move context HEAD (never touches your git repo)")
    p.add_argument("target", help="context id, or '-' for the previous context")
    p.set_defaults(func=cmd_checkout, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser(
        "branch", parents=[common],
        help="list or create context branches (never touches Git branches)",
    )
    p.add_argument("name", nargs="?", help="new context branch name")
    p.add_argument("start", nargs="?", help="start context or branch (default: HEAD)")
    p.add_argument("-d", "--delete", metavar="NAME", help="delete a branch ref; keep contexts")
    p.set_defaults(func=cmd_branch, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser(
        "switch", parents=[common],
        help="switch context branches (never touches Git branches or files)",
    )
    switch_group = p.add_mutually_exclusive_group(required=True)
    switch_group.add_argument("name", nargs="?", help="existing context branch")
    switch_group.add_argument("-c", "--create", metavar="NAME",
                              help="create and switch to a context branch")
    p.add_argument("--start", help="start context or branch for --create")
    p.set_defaults(func=cmd_switch, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser(
        "merge", parents=[common],
        help="three-way merge a context branch into the current branch",
    )
    p.add_argument("source", help="context branch or context id to merge")
    p.add_argument("-m", "--message", help="merge context summary")
    p.add_argument("--no-ff", action="store_true",
                   help="create a two-parent merge context even when fast-forward is possible")
    p.add_argument("--dry-run", action="store_true",
                   help="preview merge/conflicts without writing")
    p.add_argument("--resolve", action="append", default=[], metavar="KEY=CHOICE",
                   help="resolve one conflict with ours, theirs, or base; repeatable")
    p.add_argument("--resolve-all", choices=("ours", "theirs", "base"),
                   help="fallback resolution for every remaining conflict")
    p.add_argument("--allow-unrelated", action="store_true",
                   help="merge contexts without a common ancestor")
    p.add_argument("--agent", default=None, help="source agent name override")
    p.set_defaults(func=cmd_merge, set=[], no_prompt=True, allow_empty=False,
                   ctx_id=None, limit=None, targets=None, write_briefing=False)

    p = sub.add_parser("verify", parents=[common], help="residual secret scan over the store")
    p.add_argument("ctx_id", nargs="?", default=None, help="scan one context only")
    p.set_defaults(func=cmd_verify, set=[], no_prompt=True, message=None,
                   allow_empty=False, agent=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser("adapters", parents=[common], help="list agent adapters and detect the current one")
    p.add_argument("--agent", default=None, help="override agent detection")
    p.set_defaults(func=cmd_adapters, set=[], no_prompt=True, message=None,
                   allow_empty=False, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser(
        "capabilities", parents=[common],
        help="auto-profile agent capabilities using safe local probes",
    )
    p.add_argument("--agent", default=None, help="agent name override")
    p.set_defaults(func=cmd_capabilities, set=[], no_prompt=True, message=None,
                   allow_empty=False, ctx_id=None, limit=None,
                   targets=None, write_briefing=False)

    p = sub.add_parser(
        "sessions", parents=[common],
        help="discover project-matching private sessions (explicit opt-in)",
    )
    p.add_argument("--agent", default=None, help="session provider (default: detected agent)")
    p.add_argument("--limit", type=int, default=10, help="maximum matches (default 10)")
    p.set_defaults(func=cmd_sessions, set=[], no_prompt=True, message=None,
                   allow_empty=False, ctx_id=None, targets=None,
                   write_briefing=False)

    p = sub.add_parser(
        "import-session", parents=[common],
        help="create a context from a private session (explicit opt-in)",
    )
    p.add_argument("source", nargs="?", help="session export, or an OpenCode .db")
    p.add_argument("--latest", action="store_true",
                   help="use the latest detected session whose cwd matches --root")
    p.add_argument("--agent", default=None,
                   help="source session provider (codex, claude-code, cursor, gemini-cli, opencode)")
    p.add_argument("--session-id", default=None,
                   help="specific OpenCode SQLite session id (default: latest for --root)")
    p.add_argument("--dry-run", action="store_true",
                   help="preview redacted extracted fields without writing")
    p.add_argument("--allow-other-project", action="store_true",
                   help="allow an explicit session whose embedded cwd differs from --root")
    p.add_argument("-m", "--message", default=None,
                   help="one-line context summary (default: imported provider session)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="correct an extracted field before saving; same keys as snapshot")
    p.set_defaults(func=cmd_import_session, no_prompt=True, allow_empty=False,
                   ctx_id=None, limit=None, targets=None, write_briefing=False,
                   imported_notes=None, imported_session_meta=None,
                   imported_source_path=None, source_agent_obj=None)

    return ap


def _add_snapshot_flags(p):
    p.add_argument("-m", "--message", default=None,
                   help="one-line summary of what changed cognitively")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="fill narrative fields non-interactively. Keys: goal, "
                        "current_objective, completed, in_progress, pending, "
                        "architecture/decisions (JSON lists), constraints, do_not_change, "
                        "known_issues, recommended_actions, capabilities_required, "
                        "validation.build/test/lint/typecheck, notes. Repeatable.")
    p.add_argument("--no-prompt", action="store_true",
                   help="never prompt interactively (agents should pass this)")
    p.add_argument("--agent", default=None,
                   help="source agent name override (default: auto-detect)")


def _configure_stdio():
    """Make Unicode CLI output deterministic, including on Windows runners."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv=None):
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    root = Path(getattr(args, "root", ".")).resolve()

    try:
        return args.func(root, args)
    except StoreError as exc:
        eprint("error: {}".format(exc))
        return 1
    except SessionImportError as exc:
        eprint("session import refused: {}".format(exc))
        return 2
    except MergeError as exc:
        eprint("merge refused: {}".format(exc))
        return 4
    except KeyboardInterrupt:
        eprint("\ninterrupted.")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # last-resort guard: fail with a message, not a crash
        eprint("error: {}: {}".format(type(exc).__name__, exc))
        if os.environ.get("CONTEXT_GIT_DEBUG"):
            raise
        eprint("(set CONTEXT_GIT_DEBUG=1 for a traceback)")
        return 1


if __name__ == "__main__":
    sys.exit(main())

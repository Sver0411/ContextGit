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
    remote                   configure Context remotes
    push / fetch / pull      synchronize immutable Context objects and refs
    network                  Agent identities, handoffs, inbox, accept, receipts

Global flags: --root, --json, -q. Run from any directory inside the project.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import STORE_DIR, __version__
from .common import eprint, read_json, write_text
from .context import build as build_context, is_meaningful_change
from .capabilities import compatibility_report, detect_agent, load_adapters
from .diff import semantic_diff, render as render_diff
from .drift import check as drift_check, format_report
from .gitstate import find_repo_root
from .merge import MergeError, merge_contexts
from .network import (
    NetworkError, accept as network_accept, inbox as network_inbox,
    list_agents as network_list_agents, register as network_register,
    reply as network_reply, send as network_send,
    sent_status as network_sent_status, sync as network_sync,
)
from .remote import (
    FileTransport, RemoteError, create_transport, describe_lock,
    fetch as remote_fetch, lock_status, make_remote_config,
    pull as remote_pull, push as remote_push, unlock as remote_unlock,
)
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
    integrity = store.context_problems(ctx.get("context_id"), ctx)

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
            "integrity": "clean" if not integrity else "failed",
            "integrity_problems": integrity,
        }, indent=2, ensure_ascii=False))
        if integrity:
            return 7
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
    if integrity:
        print("  Integrity:       FAILED — {}".format(integrity[0]))
        print("                   run `context-git verify`; `resume` will refuse this object")
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

    if integrity:
        return 7
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

    # An agent is about to act on this briefing. If the object no longer
    # matches its own id, the briefing would be a confident lie — refuse
    # rather than hand over a plausible-looking narrative.
    problems = store.context_problems(resolved, ctx)
    if problems:
        eprint("resume refused: {} failed integrity verification".format(resolved))
        for problem in problems:
            eprint("  - {}".format(problem))
        eprint("Inspect the store with `context-git verify`; do not act on a "
               "briefing built from an object that does not verify.")
        return 7

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
    remote_refs = store.list_remote_refs() if args.all else {}
    if args.json:
        print(json.dumps({
            "current": current,
            "branches": [
                {"name": name, "context_id": ctx_id, "current": name == current}
                for name, ctx_id in branches.items()
            ],
            "remote_branches": [
                {"name": name, "context_id": ctx_id}
                for name, ctx_id in remote_refs.items()
            ],
        }, indent=2, ensure_ascii=False))
    else:
        for name, ctx_id in branches.items():
            print("{} {:24} {}".format(
                "*" if name == current else " ", name, ctx_id or "(unborn)"
            ))
        if not branches:
            print("(no context branches)")
        for name, ctx_id in remote_refs.items():
            print("  {:24} {}".format("remotes/" + name, ctx_id))
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
        "source_ref": args.source if (
            args.source in store.list_branches() or args.source.startswith("remote:")
        ) else None,
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
    """Full store verification: Context integrity plus residual secret scan.

    Both questions are answered here because both mean "can this store be
    trusted?":

    * integrity — every Context Object still hashes to its own id, its parent
      lineage is well formed, and every referenced parent exists locally
    * secrecy — no context, network object or rendered view still looks like it
      carries a credential

    Exit codes: 0 clean, 2 a secret-looking string survived, 7 an object failed
    integrity verification (2 wins when both are present — a live secret on
    disk is the more urgent finding).
    """
    store = Store(root)
    if args.ctx_id:
        resolved = store.resolve(args.ctx_id)
        if resolved is None:
            eprint("unknown context or branch: {}".format(args.ctx_id))
            return 1
        target_ids = [resolved]
        results = {resolved: {"problems": store.context_problems(resolved)}}
    else:
        target_ids = store.list_context_ids()
        results, _ = store.verify_graph()

    integrity_failed = False
    if target_ids:
        print("Context objects:")
        for ctx_id in target_ids:
            problems = (results.get(ctx_id) or {}).get("problems") or []
            if problems:
                integrity_failed = True
                print("{}:".format(ctx_id))
                for problem in problems:
                    print("  ERROR {}".format(problem))
            else:
                print("{}: clean".format(ctx_id))

    targets = [store.context_path(cid) for cid in target_ids]
    if not args.ctx_id:
        for directory in (store.network_handoffs_dir, store.network_receipts_dir):
            if directory.is_dir() and not directory.is_symlink():
                targets.extend(
                    path for path in sorted(directory.glob("*.json"))
                    if path.is_file() and not path.is_symlink()
                )
        handoff = store.dir / "HANDOFF.md"
        if handoff.is_file():
            targets.append(handoff)
    if not targets:
        eprint("nothing to verify — no contexts stored.")
        return 1

    leaked = False
    for t in targets:
        if not t.is_file():
            eprint("missing: {}".format(t.name))
            integrity_failed = True
            continue
        try:
            text = t.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            eprint("cannot read {}: {}".format(t.name, exc))
            integrity_failed = True
            continue
        from .security import scan_text
        findings = scan_text(text)
        if findings:
            leaked = True
            print("{}: {} potential leak(s)".format(t.name, len(findings)))
            for reason, snippet in findings[:10]:
                print("  - {}: {}".format(reason, snippet))
        else:
            print("{}: clean".format(t.name))

    if leaked:
        return 2
    if integrity_failed:
        eprint("integrity verification failed — the store is not trustworthy "
               "as-is; do not hand these objects to an agent.")
        return 7
    return 0


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


# --------------------------------------------------------------------------
# V4 remote synchronization
# --------------------------------------------------------------------------

def _require_store(root):
    store = Store(root)
    if not store.exists():
        raise StoreError("no context store here — run `context-git init` first")
    return store


def _select_remote(store, requested=None):
    remotes = store.remotes()
    if requested:
        name = store.validate_remote_name(requested)
        if name not in remotes:
            raise RemoteError("unknown remote: {}".format(name))
        config = remotes[name]
        if not isinstance(config, dict):
            raise RemoteError("remote configuration is invalid: {}".format(name))
        return name, config
    if "origin" in remotes:
        config = remotes["origin"]
        if not isinstance(config, dict):
            raise RemoteError("remote configuration is invalid: origin")
        return "origin", config
    if len(remotes) == 1:
        name = next(iter(remotes))
        config = remotes[name]
        if not isinstance(config, dict):
            raise RemoteError("remote configuration is invalid: {}".format(name))
        return name, config
    if not remotes:
        raise RemoteError("no remotes configured; run `context-git remote add NAME URL`")
    raise RemoteError("multiple remotes configured; specify one explicitly")


def cmd_remote_list(root, args):
    store = _require_store(root)
    remotes = store.remotes()
    if args.json:
        print(json.dumps({"remotes": remotes}, indent=2, ensure_ascii=False))
    elif not remotes:
        print("(no context remotes)")
    else:
        for name, config in sorted(remotes.items()):
            if not isinstance(config, dict):
                print("{}  (invalid configuration)".format(name))
                continue
            auth = " · auth env {}".format(config["auth_env"]) \
                if config.get("auth_env") else ""
            print("{}  {}{}".format(name, config.get("url"), auth))
    return 0


def cmd_remote_add(root, args):
    store = _require_store(root)
    name = store.validate_remote_name(args.name)
    replacing = name in store.remotes()
    if replacing and not args.force:
        raise RemoteError("remote already exists: {}; use --force to replace it".format(name))
    config = make_remote_config(
        args.url, root, auth_env=args.auth_env,
        allow_insecure_http=args.allow_insecure_http,
    )
    if replacing:
        store.delete_remote_refs(name)
        store.clear_network_identity(name)
    store.set_remote(name, config)
    if args.json:
        print(json.dumps({"name": name, **config}, indent=2, ensure_ascii=False))
    else:
        print("configured context remote {} → {}".format(name, config["url"]))
        if config.get("auth_env"):
            print("credential source: environment variable {} (value not stored)".format(
                config["auth_env"]
            ))
    return 0


def cmd_remote_remove(root, args):
    store = _require_store(root)
    name = store.validate_remote_name(args.name)
    if name not in store.remotes():
        raise StoreError("unknown remote: {}".format(name))
    store.delete_remote_refs(name)
    store.clear_network_identity(name)
    store.remove_remote(name)
    if args.json:
        print(json.dumps({"removed": name}, indent=2))
    else:
        print("removed context remote {} and its tracking refs".format(name))
        print("local Context Objects were retained")
    return 0


def cmd_remote_show(root, args):
    store = _require_store(root)
    name, config = _select_remote(store, args.name)
    refs = store.list_remote_refs(name)
    lock = _file_remote_lock(store, config)
    payload = {"name": name, "config": config, "tracking_refs": refs}
    if lock is not None:
        payload["writer_lock"] = lock
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("Context Remote {}".format(name))
        print("  URL: {}".format(config.get("url")))
        print("  Auth env: {}".format(config.get("auth_env") or "(none)"))
        for branch, ctx_id in refs.items():
            print("  {}/{} → {}".format(name, branch, ctx_id))
        if lock is not None:
            print("  Writer lock: {}".format(describe_lock(lock) if lock else "none"))
            if lock:
                print("               clear it with `context-git remote unlock {}`".format(name))
    return 0


def _file_remote_lock(store, config):
    """Lock status for a file remote, or None for transports without one."""
    try:
        transport = create_transport(config, store.root)
    except (RemoteError, StoreError):
        return None
    if not isinstance(transport, FileTransport):
        return None
    try:
        return lock_status(transport.root)
    except OSError:
        return None


def cmd_remote_unlock(root, args):
    """Clear a stale writer lock on a file remote."""
    store = _require_store(root)
    name, config = _select_remote(store, args.name)
    transport = create_transport(config, root)
    if not isinstance(transport, FileTransport):
        raise RemoteError("only file remotes use a local writer lock")
    result = remote_unlock(transport, force=args.force)
    if args.json:
        print(json.dumps({"remote": name, **result}, indent=2, ensure_ascii=False))
        return 0
    if not result["removed"]:
        print("{}: no writer lock present".format(name))
        return 0
    print("{}: cleared writer lock ({}){}".format(
        name, result["detail"], " [forced]" if result["forced"] else ""))
    return 0


def _render_sync_result(result, args):
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    action = result.get("action")
    if action == "push":
        prefix = "would push" if result.get("dry_run") else "pushed"
        print("{} {}/{} at {}".format(
            prefix, result["remote"], result["branch"], result["context_id"]
        ))
        print("objects: {} new / {} reachable".format(
            result["objects_uploaded"], result["objects_total"]
        ))
    elif action == "fetch":
        print("fetched {}: {} new object(s), {} verified".format(
            result["remote"], result["objects_received"], result["objects_verified"]
        ))
        for branch, ctx_id in result.get("refs", {}).items():
            print("  remote:{}/{} → {}".format(result["remote"], branch, ctx_id))
    else:
        print("pulled {}/{}: {} ({} → {})".format(
            result["remote"], result["branch"], action,
            result.get("from") or "(unborn)", result.get("to"),
        ))


def cmd_push(root, args):
    store = _require_store(root)
    remote_name, config = _select_remote(store, args.remote)
    branch = args.branch or store.current_branch()
    if not branch:
        raise RemoteError("detached context HEAD has no branch to push")
    result = remote_push(
        store, remote_name, branch, create_transport(config, root),
        force=args.force, dry_run=args.dry_run,
        allow_other_project=args.allow_other_project,
    )
    _render_sync_result(result, args)
    return 0


def cmd_fetch(root, args):
    store = _require_store(root)
    remote_name, config = _select_remote(store, args.remote)
    result = remote_fetch(
        store, remote_name, create_transport(config, root), branch=args.branch,
        allow_other_project=args.allow_other_project,
    )
    _render_sync_result(result, args)
    return 0


def cmd_pull(root, args):
    store = _require_store(root)
    remote_name, config = _select_remote(store, args.remote)
    branch = args.branch or store.current_branch()
    if not branch:
        raise RemoteError("cannot pull into detached context HEAD")
    result = remote_pull(
        store, remote_name, branch, create_transport(config, root),
        allow_other_project=args.allow_other_project,
    )
    if result.get("action") == "fast-forward":
        _refresh_handoff(root, store)
    _render_sync_result(result, args)
    return 0


# --------------------------------------------------------------------------
# V5 Agent Context Network
# --------------------------------------------------------------------------

def _network_remote(store, requested=None):
    return _select_remote(store, requested)


def cmd_network_root(root, args):
    args.network_parser.print_help()
    return 0


def _network_sync(store, remote_name, config, allow_other_project=False):
    transport = create_transport(config, store.root)
    # A handoff points into the V4 object graph. Authenticate/import that graph
    # before accepting network metadata that references it.
    remote_fetch(
        store, remote_name, transport,
        allow_other_project=allow_other_project,
    )
    return network_sync(
        store, remote_name, transport,
        allow_other_project=allow_other_project,
    )


def cmd_network_register(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    agent, _ = detect_agent(args.agent, root=root)
    result = network_register(
        store, remote_name, create_transport(config, root), args.agent_id,
        args.name, agent, allow_other_project=args.allow_other_project,
        force=args.force,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        profile = result["profile"]
        print("registered {} ({}) on {}".format(
            profile["agent_id"], profile["display_name"], remote_name
        ))
        print("capabilities: {}".format(
            ", ".join(profile["capabilities"]) or "(none declared)"
        ))
    return 0


def cmd_network_agents(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    agents = network_list_agents(
        store, create_transport(config, root),
        allow_other_project=args.allow_other_project,
    )
    if args.json:
        print(json.dumps({"remote": remote_name, "agents": agents}, indent=2,
                         ensure_ascii=False))
    elif not agents:
        print("(no agents registered)")
    else:
        for agent_id, profile in agents.items():
            print("{:<20} {:<24} {}".format(
                agent_id, profile.get("display_name") or "",
                ", ".join(profile.get("capabilities") or []) or "(none)",
            ))
    return 0


def cmd_network_send(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    branch = args.branch or store.current_branch()
    if not branch:
        raise NetworkError("detached Context HEAD has no branch to send")
    result = network_send(
        store, remote_name, create_transport(config, root), args.recipient,
        branch, message=args.message, expires_hours=args.expires_hours,
        allow_other_project=args.allow_other_project,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        handoff = result["handoff"]
        print("sent {} to {} via {}".format(
            handoff["handoff_id"], handoff["to_agent"], remote_name
        ))
        print("Context: {} ({})".format(
            handoff["context_id"], handoff["context_branch"]
        ))
        compat = handoff["compatibility"]
        print("recipient compatibility: {}%{}".format(
            compat["percent"],
            " · missing " + ", ".join(compat["missing"])
            if compat["missing"] else "",
        ))
    return 0


def _print_network_items(items, heading):
    print(heading)
    if not items:
        print("  (none)")
        return
    for item in items:
        receipts = item.get("receipts") or []
        status = receipts[-1]["status"] if receipts else "pending"
        print("  {}  {} → {}  {}  [{}]".format(
            item["handoff_id"], item["from_agent"], item["to_agent"],
            item["context_id"], status,
        ))
        if item.get("message"):
            print("    {}".format(item["message"]))


def cmd_network_inbox(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    synced = _network_sync(store, remote_name, config, args.allow_other_project)
    items = network_inbox(store, synced)
    payload = {
        "remote": remote_name, "objects_received": synced["objects_received"],
        "identity": store.network_identity(), "handoffs": items,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_network_items(items, "Inbox for {}".format(
            payload["identity"]["agent_id"]
        ))
        print("network objects received: {}".format(synced["objects_received"]))
    return 0


def cmd_network_accept(root, args):
    store = _require_store(root)
    result = network_accept(
        store, args.handoff_id, branch=args.branch, switch=args.switch,
    )
    if result["switched"]:
        _refresh_handoff(root, store)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("accepted {} as branch {} at {}".format(
            result["handoff_id"], result["branch"], result["context_id"]
        ))
        if result["switched"]:
            print("switched Context HEAD to {}".format(result["branch"]))
        print("publish acknowledgement with: context-git network reply {} accepted".format(
            result["handoff_id"]
        ))
    return 0


def cmd_network_reply(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    result = network_reply(
        store, remote_name, create_transport(config, root), args.handoff_id,
        args.status, message=args.message,
        allow_other_project=args.allow_other_project,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        receipt = result["receipt"]
        print("published {} receipt {} for {}".format(
            receipt["status"], receipt["receipt_id"], receipt["handoff_id"]
        ))
    return 0


def cmd_network_status(root, args):
    store = _require_store(root)
    remote_name, config = _network_remote(store, args.remote)
    synced = _network_sync(store, remote_name, config, args.allow_other_project)
    items = network_sent_status(store, synced, args.handoff_id)
    if args.json:
        print(json.dumps({"remote": remote_name, "handoffs": items}, indent=2,
                         ensure_ascii=False))
    else:
        _print_network_items(items, "Sent handoffs")
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
    p.add_argument("-a", "--all", action="store_true",
                   help="also show remote-tracking context branches")
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

    # V4 remote configuration uses a nested command to keep credentials and
    # destructive removal explicit in the visible CLI grammar.
    p = sub.add_parser(
        "remote", parents=[common], help="configure Context remotes",
    )
    p.set_defaults(func=cmd_remote_list)
    remote_sub = p.add_subparsers(dest="remote_action")

    rp = remote_sub.add_parser("add", parents=[common], help="add a Context remote")
    rp.add_argument("name", help="remote name, commonly origin")
    rp.add_argument("url", help="file path/file:// URL, or HTTPS endpoint")
    rp.add_argument("--auth-env", help="environment variable containing a bearer token")
    rp.add_argument("--allow-insecure-http", action="store_true",
                    help="allow plain HTTP only for localhost/loopback")
    rp.add_argument("--force", action="store_true", help="replace an existing config")
    rp.set_defaults(func=cmd_remote_add)

    rp = remote_sub.add_parser("remove", parents=[common], help="remove a remote config")
    rp.add_argument("name")
    rp.set_defaults(func=cmd_remote_remove)

    rp = remote_sub.add_parser("show", parents=[common], help="show config and tracking refs")
    rp.add_argument("name", nargs="?", help="remote name (default: origin or only remote)")
    rp.set_defaults(func=cmd_remote_show)

    rp = remote_sub.add_parser(
        "unlock", parents=[common],
        help="clear a stale writer lock on a file remote",
    )
    rp.add_argument("name", nargs="?", help="remote name (default: origin or only remote)")
    rp.add_argument("--force", action="store_true",
                    help="clear the lock even when its writer may still be alive")
    rp.set_defaults(func=cmd_remote_unlock)

    p = sub.add_parser("push", parents=[common], help="push a Context branch to a remote")
    p.add_argument("remote", nargs="?", help="remote name (default: origin or only remote)")
    p.add_argument("branch", nargs="?", help="local context branch (default: current)")
    p.add_argument("--force", action="store_true", help="allow non-fast-forward ref update")
    p.add_argument("--dry-run", action="store_true", help="validate and preview without writing")
    p.add_argument("--allow-other-project", action="store_true",
                   help="allow sync with a mismatched project identity")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("fetch", parents=[common], help="fetch Context objects and tracking refs")
    p.add_argument("remote", nargs="?", help="remote name (default: origin or only remote)")
    p.add_argument("--branch", help="fetch/update one remote branch only")
    p.add_argument("--allow-other-project", action="store_true",
                   help="allow sync with a mismatched project identity")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("pull", parents=[common], help="fetch and fast-forward a Context branch")
    p.add_argument("remote", nargs="?", help="remote name (default: origin or only remote)")
    p.add_argument("branch", nargs="?", help="remote/current branch (default: current)")
    p.add_argument("--allow-other-project", action="store_true",
                   help="allow sync with a mismatched project identity")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser(
        "network", parents=[common],
        help="Agent identities, directed Context handoffs, and receipts",
    )
    network_sub = p.add_subparsers(dest="network_action")
    p.set_defaults(func=cmd_network_root, network_parser=p)

    np = network_sub.add_parser("register", parents=[common],
                                help="register/update this Agent identity")
    np.add_argument("agent_id", help="stable lowercase network id")
    np.add_argument("--name", help="human-readable display name")
    np.add_argument("--agent", help="capability adapter (default: auto-detect)")
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("--allow-other-project", action="store_true")
    np.add_argument("--force", action="store_true",
                    help="intentionally take over an existing Agent id")
    np.set_defaults(func=cmd_network_register)

    np = network_sub.add_parser("agents", parents=[common],
                                help="list registered network Agents")
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("--allow-other-project", action="store_true")
    np.set_defaults(func=cmd_network_agents)

    np = network_sub.add_parser("send", parents=[common],
                                help="send a published Context to one Agent")
    np.add_argument("recipient", help="registered recipient Agent id")
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("--branch", help="published local Context branch (default: current)")
    np.add_argument("-m", "--message", help="short outcome/intent, never a transcript")
    np.add_argument("--expires-hours", type=int,
                    help="optional handoff expiry, 1..8760 hours")
    np.add_argument("--allow-other-project", action="store_true")
    np.set_defaults(func=cmd_network_send)

    np = network_sub.add_parser("inbox", parents=[common],
                                help="sync and list handoffs addressed to this Agent")
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("--allow-other-project", action="store_true")
    np.set_defaults(func=cmd_network_inbox)

    np = network_sub.add_parser("accept", parents=[common],
                                help="create a local Context branch from a handoff")
    np.add_argument("handoff_id")
    np.add_argument("--branch", help="local branch name (default: handoff/SENDER/ID)")
    np.add_argument("--switch", action="store_true",
                    help="also switch Context HEAD; source Git remains untouched")
    np.set_defaults(func=cmd_network_accept)

    np = network_sub.add_parser("reply", parents=[common],
                                help="publish an immutable handoff receipt")
    np.add_argument("handoff_id")
    np.add_argument("status", choices=("accepted", "completed", "rejected"))
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("-m", "--message", help="short receipt outcome")
    np.add_argument("--allow-other-project", action="store_true")
    np.set_defaults(func=cmd_network_reply)

    np = network_sub.add_parser("status", parents=[common],
                                help="sync receipts for sent handoffs")
    np.add_argument("handoff_id", nargs="?")
    np.add_argument("--remote", help="Context remote (default: origin or only remote)")
    np.add_argument("--allow-other-project", action="store_true")
    np.set_defaults(func=cmd_network_status)

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


def resolve_project_root(explicit=None, start=None):
    """Resolve the project root the way the CLI advertises: run it anywhere.

    Precedence, highest first:

    1. an explicit ``--root PATH`` — always wins, for every command, and is
       never overridden by discovery
    2. the nearest ancestor of the working directory (inclusive) that contains
       a ``.context-git/`` store — ``cd src/auth`` still resolves the project
    3. the containing Git repository root
    4. the working directory itself

    Step 3 is what stops ``init`` from creating a nested store when it is run
    from a subdirectory of an identified project. Discovery only ever moves
    *upwards*, so it can never escape into an unrelated sibling tree.
    """
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit)).expanduser().resolve()
    cwd = Path(start or os.getcwd()).expanduser().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / STORE_DIR).is_dir():
            return candidate
    repo_root = find_repo_root(cwd)
    if repo_root is not None:
        try:
            return Path(repo_root).resolve()
        except OSError:
            pass
    return cwd


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
    root = resolve_project_root(getattr(args, "root", None))

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
    except RemoteError as exc:
        eprint("remote refused: {}".format(exc))
        return 5
    except NetworkError as exc:
        eprint("network refused: {}".format(exc))
        return 6
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

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
from .render import handoff_md, log as render_log, resume_briefing, show as render_show
from .security import redact, scan_object
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

    notes = parse_set_args(args.set)
    notes.update(_interactive_notes(skip=args.no_prompt))

    parent = store.head_object()
    parent_id = parent.get("context_id") if parent else None
    if parent_id and store.has_children(parent_id):
        eprint(
            "cannot create a v1 context from historical HEAD {}: it already has a child."
            .format(parent_id)
        )
        eprint("Return to the latest context with `context-git checkout -` first.")
        return 1

    agent, how = detect_agent(getattr(args, "agent", None))
    if not args.quiet:
        eprint("[snapshot] agent: {} ({})".format(agent.get("name"), how))

    ctx = build_context(
        root, notes,
        parent_id=parent_id,
        parent_obj=parent,
        source_agent=agent,
        message=getattr(args, "message", None),
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
        old, new = store.load_context(old_id), store.load_context(new_id)
        if old is None or new is None:
            eprint("unknown context id(s): {} {}".format(
                old_id if old is None else "", new_id if new is None else "").strip())
            return 1
    else:
        # one arg given: diff that context against its parent
        target = store.load_context(old_id)
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
    history = store.history(limit=args.limit)
    print(render_log(history))
    return 0


def cmd_show(root, args):
    store = Store(root)
    ctx = store.load_context(args.ctx_id) if args.ctx_id else store.head_object()
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
    ctx = store.load_context(args.ctx_id) if args.ctx_id else store.head_object()
    if ctx is None:
        eprint("no context to resume ({}).".format(args.ctx_id or "HEAD is empty"))
        eprint("Run `context-git snapshot` in the source agent first.")
        return 1

    drift = drift_check(root, ctx)
    agent, how = detect_agent(getattr(args, "agent", None))
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
    target = args.target
    current = store.head()
    if target == "-":
        target = store.recalled_position()
        if not target:
            eprint("no previous position to check out.")
            return 1
        if target == current:
            eprint("already at {}.".format(target))
            return 0
    if target == current:
        print("already at {}".format(target))
        return 0
    try:
        store.set_head(target)  # validates existence
    except StoreError as exc:
        eprint(str(exc))
        return 1
    store.remember_position(current)  # enables `checkout -`
    ctx = store.head_object()
    if ctx:
        drift = drift_check(root, ctx)
        agent, _ = detect_agent(None)
        compat = compatibility_report(ctx, agent) \
            if ctx.get("capabilities_required") else None
        write_text(
            store.dir / "HANDOFF.md",
            handoff_md(ctx, drift_report=drift, compat=compat),
        )
    print("context HEAD → {}".format(target))
    if ctx and not args.quiet:
        print("(`context-git show` to inspect · `checkout -` to return to {})".format(
            current or "-"))
    # Your git repository was not touched: checkout only moves the context pointer.
    return 0


def cmd_verify(root, args):
    """Second-opinion secret scan over stored contexts."""
    store = Store(root)
    targets = []
    if args.ctx_id:
        targets.append(store.context_path(args.ctx_id))
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
    agent, how = detect_agent(getattr(args, "agent", None))
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


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser():
    # Shared parent so global flags work *after* the subcommand too
    # (`context-git show --json`, not only `context-git --json show`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="project root (default: cwd)")
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    root = Path(args.root).resolve()

    try:
        return args.func(root, args)
    except StoreError as exc:
        eprint("error: {}".format(exc))
        return 1
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

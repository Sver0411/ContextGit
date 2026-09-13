"""context — build UACP Context Objects and compute context ids.

A Context Object is the compressed, versioned representation of an agent's
working state at a point in time. Two kinds of information live inside it,
and the schema distinguishes them explicitly (the **Evidence Model**):

* ``source_type: "observed"`` — measured directly from the machine
  (git HEAD, file fingerprints, command results). Reproducible.
* ``source_type: "agent"`` — asserted by the exporting agent or the user
  (goal, decisions, pending work). *Claims*, to be trusted but verified.

Later agents must not mistake an agent-supplied claim for machine-verified
fact; the drift engine downgrades validity of stale evidence accordingly.

Context ids are content+lineage hashes: ``ctx_`` + 8 hex chars over
(parent id, timestamp, repo HEAD, normalised core payload).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .common import fingerprint, is_forbidden_path, iter_files, rel_posix
from .gitstate import collect as git_collect
from .project import detect as project_detect
from .project import environment_block
from .security import redact

TOOL_NAME = "context-git"
MAX_IMPORTANT_FILES = 20
MIN_IMPORTANT_FILES = 5

ENTRYPOINT_NAMES = {
    "main.py", "app.py", "run.py", "manage.py", "cli.py", "server.py",
    "index.js", "index.ts", "main.js", "main.ts", "app.js", "app.ts",
    "server.js", "server.ts", "main.go", "lib.rs", "main.rs",
    "index.tsx", "main.tsx", "App.tsx", "App.jsx",
}
CONFIG_NAMES = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "tsconfig.json",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile",
    "Makefile", "Dockerfile", "docker-compose.yml", "compose.yaml",
}

# Fields considered when deciding "did anything meaningful change?"
# (volatile fields like created_at are excluded from this comparison).
_CORE_FIELDS = [
    "goal", "current_objective", "progress", "architecture", "decisions", "constraints",
    "do_not_change", "known_issues", "important_files", "git.head",
    "git.branch", "git.working_tree", "validation", "recommended_actions",
]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Important file selection
# --------------------------------------------------------------------------

def select_important_files(root, git_snap, detection):
    """Rank candidate files, 5..20, by handoff usefulness.

    Signals (not just recency/size):
      * currently modified / staged / untracked  — the live work
      * entrypoint-shaped names
      * manifests & configs
      * recently touched in history (git log --name-only)
    Deterministic output; never includes forbidden paths.
    """
    scores = {}
    reasons = {}

    def add(rel, pts, why):
        if is_forbidden_path(rel) or (root / rel).is_symlink():
            return
        scores[rel] = scores.get(rel, 0) + pts
        reasons.setdefault(rel, []).append(why)

    for f in (git_snap.get("changed_files") or [])[:40]:
        add(f["path"], 10, "currently {}".format(f["change_type"]))
    for p in (git_snap.get("untracked") or [])[:20]:
        add(p, 6, "new untracked file")

    for rel in iter_files(root, limit=2000):
        base = rel.rsplit("/", 1)[-1]
        if base in ENTRYPOINT_NAMES:
            add(rel, 7, "likely entrypoint")
        elif base in CONFIG_NAMES:
            add(rel, 5, "project configuration")
        if rel.startswith(".github/workflows/"):
            add(rel, 3, "CI definition")

    from .gitstate import _run
    from .common import is_ignored_dir
    ok, out, _ = _run(root, ["log", "-12", "--name-only", "--pretty=format:"])
    if ok:
        seen_hist = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # git history may touch ignored trees (e.g. committed node_modules);
            # the ignore rules must hold for history-derived paths too.
            if any(is_ignored_dir(seg) for seg in line.split("/")):
                continue
            seen_hist[line] = seen_hist.get(line, 0) + 1
        for rel, n in sorted(seen_hist.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
            add(rel, min(2 + n, 6), "touched in {} of last 12 commits".format(n))

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    selected = [rel for rel, _ in ranked[:MAX_IMPORTANT_FILES]]
    if len(selected) < MIN_IMPORTANT_FILES:
        for rel in iter_files(root, limit=500):
            if rel not in selected and not is_forbidden_path(rel):
                selected.append(rel)
            if len(selected) >= MIN_IMPORTANT_FILES:
                break
    return selected[:MAX_IMPORTANT_FILES], reasons


# --------------------------------------------------------------------------
# Narrative note normalisation
# --------------------------------------------------------------------------

def _as_items(value, source_type="agent"):
    """Normalise user/agent-supplied notes into item dicts with provenance."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")] if "," in value else [value.strip()]
    items = []
    for v in value:
        if isinstance(v, dict):
            v = dict(v)
            v.setdefault("source_type", source_type)
            items.append(v)
        elif str(v).strip():
            items.append({"text": redact(str(v).strip()), "source_type": source_type})
    return items


def _normalise_decisions(value):
    """Decisions: accept dicts {decision, reason, rejected} or plain strings."""
    out = []
    for d in value or []:
        if isinstance(d, dict):
            out.append({
                "decision": redact(str(d.get("decision", "")).strip()),
                "reason": redact(str(d.get("reason", "")).strip()),
                "rejected": [
                    redact(str(r).strip()) for r in (d.get("rejected") or []) if str(r).strip()
                ],
                "source_type": "agent",
            })
        elif str(d).strip():
            out.append({
                "decision": redact(str(d).strip()), "reason": "", "rejected": [],
                "source_type": "agent",
            })
    return out


def _normalise_architecture(value):
    """Architecture facts carry explicit uncertainty and provenance."""
    out = []
    for item in value or []:
        if isinstance(item, dict):
            fact = redact(str(item.get("fact") or item.get("text") or "").strip())
            if not fact:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.7))))
            except (TypeError, ValueError):
                confidence = 0.7
            freshness = str(item.get("freshness") or "unknown").lower()
            if freshness not in ("fresh", "stale", "unknown"):
                freshness = "unknown"
            out.append({
                "fact": fact,
                "confidence": confidence,
                "source": redact(str(item.get("source") or "").strip()) or None,
                "freshness": freshness,
                "source_type": "agent",
            })
        elif str(item).strip():
            out.append({
                "fact": redact(str(item).strip()),
                "confidence": 0.7,
                "source": None,
                "freshness": "unknown",
                "source_type": "agent",
            })
    return out


# --------------------------------------------------------------------------
# Context Object construction
# --------------------------------------------------------------------------

# Narrative fields inherited from the parent context when the agent doesn't
# restate them. This is what makes commits *incremental*: an agent says what
# changed, not everything again. Explicit --set values always win.
_INHERIT_SCALAR = ("goal", "current_objective")
_INHERIT_LIST_TOP = (
    "architecture", "decisions", "constraints", "do_not_change", "known_issues",
    "recommended_actions", "capabilities_required",
)
_INHERIT_LIST_PROGRESS = ("completed", "in_progress", "pending")
_APPEND_LIST_TOP = (
    "architecture", "decisions", "constraints", "do_not_change", "capabilities_required",
)


def _semantic_key(value):
    """Unicode-safe identity key for incremental list merging."""
    if isinstance(value, dict):
        value = value.get("text") or value.get("decision") or value.get("name") or ""
    normalised = unicodedata.normalize("NFKC", str(value).casefold())
    tokens = re.findall(r"\w+", normalised, flags=re.UNICODE)
    return " ".join(tokens) if tokens else normalised.strip()


def _list_value(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _merge_unique(old, new):
    out = []
    seen = set()
    for item in _list_value(old) + _list_value(new):
        key = _semantic_key(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build(root, notes, parent_id=None, parent_obj=None, source_agent=None,
          message=None, session_import=None, excluded_paths=None,
          parent_ids=None, context_branch=None, merge_info=None):
    """Build a full UACP Context Object from live state + agent notes.

    ``notes`` is a plain dict of agent/user-supplied fields. Fields not
    restated in ``notes`` are inherited from ``parent_obj`` (incremental
    semantics); providing an empty list explicitly clears an inherited list.

    Never raises for environment problems; degrades to unknown/None.
    """
    root = Path(root).resolve()
    git_snap = git_collect(root)
    detection = project_detect(root)

    excluded = set()
    for candidate in excluded_paths or []:
        try:
            excluded.add(Path(candidate).resolve().relative_to(root).as_posix())
        except (OSError, ValueError):
            continue
    if excluded:
        git_snap = _exclude_git_paths(git_snap, excluded)

    notes = _inherit_notes(notes, parent_obj)

    selected, reasons = select_important_files(root, git_snap, detection)
    selected = [rel for rel in selected if rel not in excluded]
    important_files = []
    fingerprints = {}
    for rel in selected:
        fp = fingerprint(root / rel)
        why = "; ".join(reasons.get(rel, [])) or "recently touched"
        important_files.append({
            "path": rel,
            "why": redact(why),
            "fingerprint": fp,
            "confidence": 1.0 if fp else 0.5,
            "freshness": "fresh" if fp else "unknown",
            "source_type": "observed",
        })
        if fp:
            fingerprints[rel] = fp

    validation = notes.get("validation") or {}
    validation_obj = {
        "build": _validation_entry(validation.get("build")),
        "test": _validation_entry(validation.get("test")),
        "lint": _validation_entry(validation.get("lint")),
        "typecheck": _validation_entry(validation.get("typecheck")),
        "observed_at": validation.get("observed_at")
        or (utc_now_iso() if _validation_observed(validation) else None),
        # The CLI records this claim but does not execute the command itself.
        "source_type": "agent",
        "confidence": 0.8 if _validation_observed(validation) else 0.0,
        "note": redact(str(validation.get("note"))) if validation.get("note") else None,
        "freshness": "unknown" if not _validation_observed(validation) else "fresh",
    }

    project_name = _project_name(root, detection)

    lineage = list(parent_ids) if parent_ids is not None else (
        [parent_id] if parent_id else []
    )
    if lineage and parent_id != lineage[0]:
        raise ValueError("parent_context_id must be the first parent_context_ids entry")

    payload = {
        "protocol": "UACP",
        "protocol_version": "1.0",
        "schema_version": SCHEMA_VERSION,
        "context_id": None,           # filled below
        "parent_context_id": parent_id,
        "parent_context_ids": lineage,
        "context_branch": context_branch,
        "created_at": utc_now_iso(),
        "message": redact(str(message).strip()) if message else None,
        "source_agent": {
            "name": (source_agent or {}).get("name", "generic"),
            "capabilities": (source_agent or {}).get("capabilities", []),
            "declared_capabilities": (source_agent or {}).get(
                "declared_capabilities", (source_agent or {}).get("capabilities", [])
            ),
            "capability_profile": (source_agent or {}).get("capability_profile", {}),
            "source_type": "observed",
        },
        "target_agent": None,
        "project": {
            "name": redact(project_name),
            "root": str(root),
            "type": detection.get("primary_language", "unknown"),
            "stack": {
                "languages": detection.get("languages", []),
                "frameworks": detection.get("frameworks", []),
                "package_manager": detection.get("package_manager", "unknown"),
                "commands": detection.get("commands", {}),
                "is_monorepo": detection.get("is_monorepo", False),
                "evidence": detection.get("evidence", {}),
            },
            "source_type": "observed",
        },
        "goal": redact(str(notes.get("goal") or "").strip()) or None,
        "current_objective": redact(str(notes.get("current_objective") or "").strip()) or None,
        "progress": {
            "completed": _as_items(notes.get("completed")),
            "in_progress": _as_items(notes.get("in_progress")),
            "pending": _as_items(notes.get("pending")),
        },
        "architecture": _normalise_architecture(notes.get("architecture")),
        "decisions": _normalise_decisions(notes.get("decisions")),
        "constraints": _as_items(notes.get("constraints")),
        "do_not_change": _as_items(notes.get("do_not_change")),
        "known_issues": _as_items(notes.get("known_issues")),
        "important_files": important_files,
        "git": _git_block(git_snap),
        "validation": validation_obj,
        "environment": environment_block(detection, git_snap),
        "capabilities_required": _as_items(notes.get("capabilities_required")) or [],
        "recommended_actions": _as_items(notes.get("recommended_actions")),
        "security": {
            "secret_guard": "context-git/4.0",
            "redacted_on_write": True,
            "sensitive_files_read": False,
            "raw_session_stored": False,
        },
        "metadata": {
            "tool": {"name": TOOL_NAME, "version": __version__},
            "notes": redact(str(notes.get("notes") or "").strip()) or None,
            "fingerprints": fingerprints,
            "fingerprint_algorithm": "sha256:full-file-up-to-5MiB+size",
            "session_import": session_import,
            "merge": merge_info,
        },
    }

    payload["context_id"] = compute_context_id(payload, parent_id, lineage)
    return payload


def _exclude_git_paths(git_snap, excluded):
    """Remove an explicitly imported transcript from persisted repo evidence."""
    clean = dict(git_snap)
    for key in ("staged", "unstaged", "untracked", "conflicted"):
        clean[key] = [path for path in clean.get(key, []) if path not in excluded]
    changes = [
        item for item in clean.get("changed_files", [])
        if item.get("path") not in excluded
    ]
    clean["changed_files"] = changes
    clean["diff_stat"] = {
        "files_changed": len(changes),
        "additions": sum(int(item.get("additions") or 0) for item in changes),
        "deletions": sum(int(item.get("deletions") or 0) for item in changes),
    }
    return clean


def _inherit_notes(notes, parent_obj):
    """Merge agent notes over parent context values (incremental semantics).

    Rules:
    * scalar fields (goal, current_objective): inherited when empty/absent
    * list fields: inherited when absent; an explicitly provided empty list
      clears the inherited value (agents say "nothing pending" deliberately)
    * validation: inherited wholesale when the agent records none — the
      drift engine then marks it STALE if code changed afterwards
    """
    if not parent_obj:
        return notes
    merged = dict(notes or {})
    for key in _INHERIT_SCALAR:
        if not str(merged.get(key) or "").strip():
            inherited = parent_obj.get(key)
            if inherited:
                merged[key] = inherited
    for key in _INHERIT_LIST_TOP:
        if key not in merged or merged[key] is None:
            merged[key] = parent_obj.get(key) or []
        elif merged[key] and key in _APPEND_LIST_TOP:
            merged[key] = _merge_unique(parent_obj.get(key) or [], merged[key])
    progress = parent_obj.get("progress") or {}
    for key in _INHERIT_LIST_PROGRESS:
        if key not in merged or merged[key] is None:
            merged[key] = progress.get(key) or []
    if merged.get("completed") and "completed" in (notes or {}):
        merged["completed"] = _merge_unique(
            progress.get("completed") or [], merged["completed"]
        )

    # A task can occupy only one state. Higher-progress buckets win over
    # inherited lower-progress buckets during an incremental commit.
    completed_keys = {_semantic_key(v) for v in _list_value(merged.get("completed"))}
    in_progress_keys = {_semantic_key(v) for v in _list_value(merged.get("in_progress"))}
    merged["in_progress"] = [
        v for v in _list_value(merged.get("in_progress"))
        if _semantic_key(v) not in completed_keys
    ]
    merged["pending"] = [
        v for v in _list_value(merged.get("pending"))
        if _semantic_key(v) not in completed_keys
        and _semantic_key(v) not in in_progress_keys
    ]
    if "validation" not in merged or not merged.get("validation"):
        pv = parent_obj.get("validation") or {}
        if any(pv.get(k) for k in ("build", "test", "lint", "typecheck")):
            inherited_val = {k: v for k, v in pv.items() if k not in ("freshness", "stale_reason")}
            merged["validation"] = inherited_val
    return merged


def _validation_entry(value):
    if isinstance(value, dict):
        value = value.get("result")
    if value is None or str(value).strip() in ("", "unknown"):
        return None
    return {"result": redact(str(value).strip())}


def _validation_observed(validation):
    return any(
        isinstance(validation.get(k), (str, dict)) and str(validation.get(k)).strip()
        for k in ("build", "test", "lint", "typecheck")
    )


def _git_block(git_snap):
    """Git block — observed, file lists and stats, never diff bodies."""
    wt = {
        "clean": (
            not git_snap.get("staged") and not git_snap.get("unstaged")
            and not git_snap.get("untracked")
        ) if git_snap.get("available") else None,
        "staged": git_snap.get("staged", []),
        "unstaged": git_snap.get("unstaged", []),
        "untracked": git_snap.get("untracked", [])[:100],
        "conflicted": git_snap.get("conflicted", []),
    }
    return {
        "available": git_snap.get("available", False),
        "reason": git_snap.get("reason"),
        "repo_root": git_snap.get("root"),
        "branch": git_snap.get("branch"),
        "detached_head": git_snap.get("detached_head", False),
        "head": git_snap.get("head"),
        "head_short": git_snap.get("head_short"),
        "head_subject": redact(git_snap.get("head_subject") or "") or None,
        "upstream": git_snap.get("upstream"),
        "ahead": git_snap.get("ahead"),
        "behind": git_snap.get("behind"),
        "repo_state": git_snap.get("repo_state"),
        "commit_count": git_snap.get("commit_count"),
        "working_tree": wt,
        "changed_files": git_snap.get("changed_files", [])[:100],
        "diff_stat": git_snap.get("diff_stat"),
        "recent_commits": git_snap.get("recent_commits", []),
        "source_type": "observed",
        "observed_at": utc_now_iso(),
        "freshness": "fresh" if git_snap.get("available") else "unknown",
        "confidence": 1.0 if git_snap.get("available") else 0.0,
    }


def _project_name(root, detection):
    pkg = None
    try:
        pkg_path = root / "package.json"
        if pkg_path.is_file():
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        pkg = None
    if isinstance(pkg, dict) and isinstance(pkg.get("name"), str) and pkg["name"].strip():
        return pkg["name"].strip()
    for manifest in ("pyproject.toml", "Cargo.toml"):
        try:
            text = (root / manifest).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("name") and "=" in s:
                val = s.split("=", 1)[1].strip().strip('"').strip("'")
                if val and len(val) < 100:
                    return val
    if (root / "go.mod").is_file():
        try:
            first = (root / "go.mod").read_text(encoding="utf-8", errors="replace")
            first = first.splitlines()[0] if first else ""
            if first.startswith("module "):
                return first.split()[1].strip()
        except (OSError, IndexError):
            pass
    return root.name or "unknown-project"


# --------------------------------------------------------------------------
# Context id
# --------------------------------------------------------------------------

def core_view(obj):
    """The comparable/normalised view of a context (volatile fields removed).

    Used for context-id computation, empty-commit detection and diffing.
    """
    core = {
        "goal": obj.get("goal"),
        "current_objective": obj.get("current_objective"),
        "progress": obj.get("progress"),
        "architecture": obj.get("architecture"),
        "decisions": obj.get("decisions"),
        "constraints": obj.get("constraints"),
        "do_not_change": obj.get("do_not_change"),
        "known_issues": obj.get("known_issues"),
        "important_files": [
            {"path": f.get("path"), "why": f.get("why")}
            for f in obj.get("important_files", [])
        ],
        "git": {
            "head": (obj.get("git") or {}).get("head"),
            "branch": (obj.get("git") or {}).get("branch"),
            "working_tree": (obj.get("git") or {}).get("working_tree"),
            "diff_stat": (obj.get("git") or {}).get("diff_stat"),
        },
        "validation": obj.get("validation"),
        "recommended_actions": obj.get("recommended_actions"),
        "capabilities_required": obj.get("capabilities_required"),
        "notes": (obj.get("metadata") or {}).get("notes"),
    }
    return core


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_context_id(payload, parent_id, parent_ids=None):
    """ctx_<8hex> over (parents, timestamp, repo HEAD, canonical core payload).

    Timestamp inclusion guarantees uniqueness even for identical states;
    the store also enforces uniqueness as a backstop.
    """
    h = hashlib.sha256()
    lineage = list(parent_ids) if parent_ids is not None else (
        [parent_id] if parent_id else []
    )
    h.update(_canonical(lineage or ["root"]).encode("utf-8"))
    h.update(payload["created_at"].encode("utf-8"))
    h.update(((payload.get("git") or {}).get("head") or "no-git").encode("utf-8"))
    h.update(_canonical(core_view(payload)).encode("utf-8"))
    return "ctx_" + h.hexdigest()[:8]


def verify_context_id(payload):
    """Verify an object's id using the writer generation that created it.

    V1/V2 hashed the scalar parent directly. V3+ objects carry
    ``parent_context_ids`` and hash the canonical parent array. Supporting
    both makes remote transport able to authenticate old immutable objects.
    """
    if not isinstance(payload, dict) or not payload.get("context_id"):
        return False
    parent_id = payload.get("parent_context_id")
    if "parent_context_ids" in payload:
        expected = compute_context_id(
            payload, parent_id, payload.get("parent_context_ids") or []
        )
    else:
        h = hashlib.sha256()
        h.update((parent_id or "root").encode("utf-8"))
        h.update(str(payload.get("created_at") or "").encode("utf-8"))
        h.update(((payload.get("git") or {}).get("head") or "no-git").encode("utf-8"))
        h.update(_canonical(core_view(payload)).encode("utf-8"))
        expected = "ctx_" + h.hexdigest()[:8]
    return payload.get("context_id") == expected


def is_meaningful_change(prev_obj, new_obj):
    """True if the new context differs from its parent in any core field."""
    if prev_obj is None:
        return True
    return _canonical(core_view(prev_obj)) != _canonical(core_view(new_obj))

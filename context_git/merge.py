"""Three-way semantic merge for UACP Context Objects.

Only agent-authored working-state fields are merged. Machine observations
(Git state, fingerprints, environment and validation freshness) are captured
again by the normal Context Object builder when a merge context is created.
"""

from __future__ import annotations

import json
import re
import unicodedata


class MergeError(Exception):
    """Raised when a merge request or explicit resolution is invalid."""


_MISSING = object()
_LIST_FIELDS = (
    "architecture",
    "decisions",
    "constraints",
    "do_not_change",
    "known_issues",
    "recommended_actions",
    "capabilities_required",
)
_PROGRESS_STATES = ("completed", "in_progress", "pending")


def _identity(item):
    if isinstance(item, dict):
        item = (
            item.get("text")
            or item.get("decision")
            or item.get("fact")
            or item.get("name")
            or ""
        )
    text = unicodedata.normalize("NFKC", str(item or "").casefold())
    words = re.findall(r"\w+", text, re.UNICODE)
    return " ".join(words) if words else text.strip()


def _key(section, identity=None):
    if identity is None:
        return section
    slug = re.sub(r"[^\w.-]+", "-", identity, flags=re.UNICODE).strip("-")
    return "{}:{}".format(section, slug[:80] or "item")


def _canonical(value):
    if value is _MISSING:
        return "<missing>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _equal(left, right):
    return _canonical(left) == _canonical(right)


def _public(value):
    return None if value is _MISSING else value


def _choose(conflict_key, section, base, ours, theirs, resolutions,
            resolve_all, conflicts, used):
    """Apply ordinary three-way rules, recording an unresolved conflict."""
    if _equal(ours, theirs):
        return ours
    if _equal(ours, base):
        return theirs
    if _equal(theirs, base):
        return ours

    choice = resolutions.get(conflict_key) or resolve_all
    if choice:
        used.add(conflict_key)
        return {"ours": ours, "theirs": theirs, "base": base}[choice]

    conflicts.append({
        "key": conflict_key,
        "section": section,
        "base": _public(base),
        "ours": _public(ours),
        "theirs": _public(theirs),
    })
    # A tentative value keeps previews readable; callers must not persist a
    # result while conflicts is non-empty.
    return ours


def _index(items):
    result = {}
    order = []
    for item in items or []:
        identity = _identity(item)
        if not identity:
            continue
        if identity not in result:
            order.append(identity)
            result[identity] = item
    return result, order


def _merge_list(section, base_items, our_items, their_items, resolutions,
                resolve_all, conflicts, used):
    base, base_order = _index(base_items)
    ours, our_order = _index(our_items)
    theirs, their_order = _index(their_items)
    order = []
    for identity in our_order + their_order + base_order:
        if identity not in order:
            order.append(identity)

    merged = []
    for identity in order:
        value = _choose(
            _key(section, identity), section,
            base.get(identity, _MISSING),
            ours.get(identity, _MISSING),
            theirs.get(identity, _MISSING),
            resolutions, resolve_all, conflicts, used,
        )
        if value is not _MISSING:
            merged.append(value)
    return merged


def _progress_index(ctx):
    result = {}
    order = []
    progress = (ctx or {}).get("progress") or {}
    # More advanced states win if a malformed object repeats the same task.
    for state in reversed(_PROGRESS_STATES):
        for item in progress.get(state) or []:
            identity = _identity(item)
            if not identity:
                continue
            if identity not in order:
                order.append(identity)
            result[identity] = {"state": state, "item": item}
    return result, order


def _merge_progress(base_ctx, ours_ctx, theirs_ctx, resolutions,
                    resolve_all, conflicts, used):
    base, base_order = _progress_index(base_ctx)
    ours, our_order = _progress_index(ours_ctx)
    theirs, their_order = _progress_index(theirs_ctx)
    order = []
    for identity in our_order + their_order + base_order:
        if identity not in order:
            order.append(identity)

    result = {state: [] for state in _PROGRESS_STATES}
    for identity in order:
        value = _choose(
            _key("progress", identity), "progress",
            base.get(identity, _MISSING),
            ours.get(identity, _MISSING),
            theirs.get(identity, _MISSING),
            resolutions, resolve_all, conflicts, used,
        )
        if value is not _MISSING:
            result[value["state"]].append(value["item"])
    return result


def merge_contexts(base, ours, theirs, resolutions=None, resolve_all=None):
    """Return a deterministic three-way merge plan for three contexts.

    The returned ``notes`` can be passed directly to ``context.build``.
    Conflicts are field-level and never silently resolved.
    """
    resolutions = dict(resolutions or {})
    allowed = {"ours", "theirs", "base"}
    if resolve_all is not None and resolve_all not in allowed:
        raise MergeError("resolve-all must be ours, theirs, or base")
    invalid = {key: value for key, value in resolutions.items() if value not in allowed}
    if invalid:
        key = sorted(invalid)[0]
        raise MergeError("invalid resolution for {}: {}".format(key, invalid[key]))

    base = base or {}
    ours = ours or {}
    theirs = theirs or {}
    conflicts = []
    used = set()
    notes = {}

    for field in ("goal", "current_objective"):
        notes[field] = _choose(
            field, field,
            base.get(field), ours.get(field), theirs.get(field),
            resolutions, resolve_all, conflicts, used,
        )

    progress = _merge_progress(
        base, ours, theirs, resolutions, resolve_all, conflicts, used
    )
    notes.update(progress)

    for field in _LIST_FIELDS:
        notes[field] = _merge_list(
            field, base.get(field), ours.get(field), theirs.get(field),
            resolutions, resolve_all, conflicts, used,
        )

    unknown = sorted(set(resolutions) - used)
    if unknown:
        raise MergeError(
            "resolution key did not match a conflict: {}".format(", ".join(unknown))
        )

    return {
        "base": base.get("context_id"),
        "ours": ours.get("context_id"),
        "theirs": theirs.get("context_id"),
        "notes": notes,
        "conflicts": conflicts,
        "resolved": sorted(used),
    }


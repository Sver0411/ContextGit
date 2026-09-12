"""capabilities — agent adapters and cross-agent capability mapping.

Different agents can do different things. A context that says "validated
by taking browser screenshots" is useless to an agent with no browser.
This module:

* loads lightweight agent adapters from ``adapters/*.json``
* detects the *current* agent from environment markers (best effort,
  overridable via ``--agent``)
* computes target-agent compatibility against ``capabilities_required``

Adapters declare capabilities; they do NOT hack private session data —
that belongs to a future version.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent / "adapters"

KNOWN_CAPABILITIES = [
    "filesystem", "shell", "git", "python", "node",
    "browser", "gui", "network", "image-generation", "subagents",
]


def load_adapters():
    """{name: adapter_dict} from bundled adapters/*.json."""
    out = {}
    if not ADAPTERS_DIR.is_dir():
        return out
    for p in sorted(ADAPTERS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name"):
            out[data["name"]] = data
    return out


def detect_agent(override=None):
    """Detect the current agent.

    Order: explicit override > environment markers > generic.
    Returns (adapter_dict, how_detected).
    """
    adapters = load_adapters()
    if override:
        key = override.strip().lower()
        if key in adapters:
            return adapters[key], "explicit"
        # unknown agent name: build a generic adapter with declared name
        return {
            "name": key,
            "display_name": key,
            "capabilities": [],
            "notes": "unknown adapter; capabilities are undeclared",
        }, "explicit-unknown"

    for name, adapter in adapters.items():
        for env_key in adapter.get("detect_env", []) or []:
            if os.environ.get(env_key):
                return adapter, "env:{}".format(env_key)

    generic = adapters.get("generic") or {
        "name": "generic", "display_name": "Generic Agent",
        "capabilities": ["filesystem", "shell", "git"],
    }
    return generic, "fallback"


def required_capabilities(ctx_obj):
    """Normalised list of capability names required by a context."""
    out = []
    for item in ctx_obj.get("capabilities_required") or []:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name")
        if item and str(item).strip():
            out.append(str(item).strip().lower())
    return out


def compatibility(required, adapter):
    """Compare required capabilities against a target agent adapter.

    Returns {percent, satisfied, missing, unknown, warnings}.
    """
    caps = {c.lower() for c in adapter.get("capabilities") or []}
    satisfied, missing, unknown = [], [], []
    for cap in required:
        if cap in caps:
            satisfied.append(cap)
        elif cap in KNOWN_CAPABILITIES:
            missing.append(cap)
        else:
            unknown.append(cap)
    total = len(required)
    percent = int(round(100.0 * len(satisfied) / total)) if total else 100
    return {
        "percent": percent,
        "satisfied": satisfied,
        "missing": missing,
        "unknown": unknown,   # we can't judge capabilities we don't know
        "warnings": [],
        "target": adapter.get("name"),
    }


def compatibility_report(ctx_obj, adapter):
    """Compatibility + warnings that name affected work items."""
    required = required_capabilities(ctx_obj)
    report = compatibility(required, adapter)
    if report["missing"]:
        targets = []
        for action in ctx_obj.get("recommended_actions") or []:
            text = action.get("text") if isinstance(action, dict) else str(action)
            low = (text or "").lower()
            for cap in report["missing"]:
                if cap in low or (cap == "browser" and ("screenshot" in low or "web" in low)):
                    targets.append(text)
                    break
        if not targets:
            for issue in ctx_obj.get("known_issues") or []:
                text = issue.get("text") if isinstance(issue, dict) else str(issue)
                for cap in report["missing"]:
                    if cap in (text or "").lower():
                        targets.append(text)
                        break
        if targets:
            report["warnings"].append({
                "message": "previous workflow relied on: {}".format(
                    ", ".join(report["missing"])),
                "affected": targets[:5],
            })
        else:
            report["warnings"].append({
                "message": "target agent does not declare: {}".format(
                    ", ".join(report["missing"])),
                "affected": [],
            })
    return report

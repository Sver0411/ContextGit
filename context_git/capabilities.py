"""capabilities — agent adapters and cross-agent capability mapping.

Different agents can do different things. A context that says "validated
by taking browser screenshots" is useless to an agent with no browser.
This module:

* loads lightweight agent adapters from ``adapters/*.json``
* detects the *current* agent from environment markers (best effort,
  overridable via ``--agent``)
* computes target-agent compatibility against ``capabilities_required``

V2 enriches those declarations with conservative, read-only environment
probes.  A probe can prove that a local runtime is available; capabilities
that cannot be measured safely (for example an agent's browser tool) remain
explicitly ``declared`` rather than being presented as observed fact.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
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


def profile_capabilities(adapter, root=None, environ=None, which=None):
    """Return an adapter enriched with a conservative capability profile.

    No project code is executed and no network request is made.  Executable
    probes use ``PATH`` only; the filesystem probe checks whether ``root`` is
    an accessible directory.  Capabilities such as browser, GUI and
    subagents cannot be verified portably, so an adapter declaration is kept
    with status ``declared``.

    ``environ`` and ``which`` are injectable to keep platform tests
    deterministic.
    """
    env = os.environ if environ is None else environ
    find = shutil.which if which is None else which
    declared = {str(c).lower() for c in adapter.get("capabilities") or []}
    profile = {}

    def record(name, available, evidence):
        if available:
            status = "available"
        elif name in declared:
            status = "unavailable"
        else:
            status = "unknown"
        profile[name] = {"status": status, "evidence": evidence}

    probe_root = Path(root or ".")
    try:
        fs_available = probe_root.is_dir() and os.access(str(probe_root), os.R_OK)
    except OSError:
        fs_available = False
    record("filesystem", fs_available, "project-root-readable")

    shell_available = bool(
        (env.get("COMSPEC") and Path(env["COMSPEC"]).is_file())
        or find("sh", path=env.get("PATH"))
        or find("bash", path=env.get("PATH"))
        or find("pwsh", path=env.get("PATH"))
        or find("powershell", path=env.get("PATH"))
    )
    record("shell", shell_available, "shell-executable")
    record("git", bool(find("git", path=env.get("PATH"))), "executable:git")
    # The running interpreter itself is stronger evidence than PATH.
    record("python", bool(sys.executable), "running-python")
    record("node", bool(find("node", path=env.get("PATH"))), "executable:node")

    for name in KNOWN_CAPABILITIES:
        if name in profile:
            continue
        profile[name] = {
            "status": "declared" if name in declared else "unknown",
            "evidence": "adapter-declaration" if name in declared else "none",
        }

    effective = [
        name for name in KNOWN_CAPABILITIES
        if profile[name]["status"] in ("available", "declared")
    ]
    enriched = dict(adapter)
    enriched["declared_capabilities"] = sorted(declared)
    enriched["capabilities"] = effective
    enriched["capability_profile"] = profile
    enriched["profile_version"] = 1
    return enriched


def detect_agent(override=None, root=None, probe=True):
    """Detect the current agent.

    Order: explicit override > environment markers > generic.
    Returns (adapter_dict, how_detected).
    """
    adapters = load_adapters()
    if override:
        key = override.strip().lower()
        if key in adapters:
            adapter = adapters[key]
            return (profile_capabilities(adapter, root) if probe else adapter), "explicit"
        # unknown agent name: build a generic adapter with declared name
        adapter = {
            "name": key,
            "display_name": key,
            "capabilities": [],
            "notes": "unknown adapter; capabilities are undeclared",
        }
        # An arbitrary target name is not evidence that its agent can use the
        # current process's tools.  Keep it unknown instead of projecting the
        # host profile onto a possibly remote agent.
        return adapter, "explicit-unknown"

    for name, adapter in adapters.items():
        for env_key in adapter.get("detect_env", []) or []:
            if os.environ.get(env_key):
                return (profile_capabilities(adapter, root) if probe else adapter), \
                    "env:{}".format(env_key)

    generic = adapters.get("generic") or {
        "name": "generic", "display_name": "Generic Agent",
        "capabilities": ["filesystem", "shell", "git"],
    }
    return (profile_capabilities(generic, root) if probe else generic), "fallback"


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
    profile = adapter.get("capability_profile") or {}
    satisfied, missing, unknown = [], [], []
    verified, declared_only = [], []
    for cap in required:
        if cap in caps:
            satisfied.append(cap)
            if (profile.get(cap) or {}).get("status") == "available":
                verified.append(cap)
            elif (profile.get(cap) or {}).get("status") == "declared":
                declared_only.append(cap)
        elif cap in KNOWN_CAPABILITIES:
            missing.append(cap)
        else:
            unknown.append(cap)
    total = len(required)
    percent = int(round(100.0 * len(satisfied) / total)) if total else 100
    return {
        "percent": percent,
        "satisfied": satisfied,
        "verified": verified,
        "declared_only": declared_only,
        "missing": missing,
        "unknown": unknown,   # we can't judge capabilities we don't know
        "warnings": [],
        "target": adapter.get("name"),
    }


def compatibility_report(ctx_obj, adapter):
    """Compatibility + warnings that name affected work items."""
    required = required_capabilities(ctx_obj)
    report = compatibility(required, adapter)
    if report.get("declared_only"):
        report["warnings"].append({
            "message": "adapter declares but local probes cannot verify: {}".format(
                ", ".join(report["declared_only"])
            ),
            "affected": [],
        })
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

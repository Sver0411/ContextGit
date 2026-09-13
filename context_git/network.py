"""V5 Agent Context Network built on verified V4 remotes.

The network exchanges pointers to already-published Context Objects, compact
handoff intent, capability compatibility, and immutable status receipts. It
never transports source files, transcripts, tool payloads, or credentials.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timedelta, timezone

from .capabilities import compatibility_report, required_capabilities
from .remote import (
    MAX_NETWORK_OBJECT_BYTES,
    MAX_REMOTE_OBJECTS,
    RemoteError,
    _canonical_bytes,
    _check_project,
    _digest,
    project_descriptor,
    validate_context_object,
    validate_manifest,
)
from .security import redact, scan_object
from .storage import CTX_ID_RE, Store, StoreError, utc_now_iso

NETWORK_FORMAT = "context-git-network"
NETWORK_VERSION = "1.0"
HANDOFF_FORMAT = "context-git-handoff"
RECEIPT_FORMAT = "context-git-receipt"
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
HANDOFF_ID_RE = re.compile(r"^hnd_[0-9a-f]{16}$")
RECEIPT_ID_RE = re.compile(r"^rcp_[0-9a-f]{16}$")
RECEIPT_STATUSES = ("accepted", "completed", "rejected")
MAX_AGENTS = 1000
MAX_MESSAGE_LENGTH = 2000
MAX_CAPABILITIES = 50
CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
CAPABILITY_STATUSES = ("available", "declared", "unavailable", "unknown")


class NetworkError(Exception):
    """Safe, user-facing Agent Context Network failure."""


def validate_agent_id(value):
    value = str(value or "").strip().lower()
    if not AGENT_ID_RE.match(value):
        raise NetworkError(
            "agent id must be 2-64 lowercase letters, digits, '.', '_' or '-'"
        )
    return value


def _safe_text(value, label, required=False, max_length=MAX_MESSAGE_LENGTH):
    text = redact(str(value or "").strip())
    if required and not text:
        raise NetworkError("{} cannot be empty".format(label))
    if len(text) > max_length:
        raise NetworkError("{} exceeds {} characters".format(label, max_length))
    return text


def _object_id(prefix, obj, key):
    payload = copy.deepcopy(obj)
    payload.pop(key, None)
    return "{}_{}".format(
        prefix, hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:16]
    )


def _object_meta(obj):
    payload = _canonical_bytes(obj)
    return {"sha256": _digest(obj), "size": len(payload)}


def _validate_meta(meta):
    return bool(
        isinstance(meta, dict)
        and re.match(r"^sha256:[0-9a-f]{64}$", str(meta.get("sha256") or ""))
        and isinstance(meta.get("size"), int)
        and not isinstance(meta.get("size"), bool)
        and 0 <= meta["size"] <= MAX_NETWORK_OBJECT_BYTES
    )


def _parse_time(value, label):
    if not isinstance(value, str) or not value:
        raise NetworkError("{} is invalid".format(label))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NetworkError("{} is invalid".format(label))
    if parsed.tzinfo is None:
        raise NetworkError("{} must include a timezone".format(label))
    return parsed


def _normalise_capabilities(values):
    result = []
    for item in values or []:
        name = str(item or "").strip().lower()
        if name and name not in result:
            result.append(name)
    return sorted(result)


def _validate_capabilities(values, label):
    if (
        not isinstance(values, list)
        or len(values) > MAX_CAPABILITIES
        or len(values) != len(set(values))
        or any(
            not isinstance(item, str)
            or not CAPABILITY_RE.match(item)
            for item in values
        )
    ):
        raise NetworkError("{} are invalid".format(label))
    return values


def _local_identity(store, remote_name=None):
    identity = store.network_identity()
    if not isinstance(identity, dict):
        raise NetworkError("no local network identity; run `context-git network register` first")
    agent_id = validate_agent_id(identity.get("agent_id"))
    remote = identity.get("remote")
    if not isinstance(remote, str) or not remote:
        raise NetworkError("local network identity is invalid; register it again")
    if remote_name is not None and remote != remote_name:
        raise NetworkError("local network identity is registered on another remote")
    value = dict(identity)
    value["agent_id"] = agent_id
    return value


def empty_network_manifest(project):
    return {
        "format": NETWORK_FORMAT,
        "version": NETWORK_VERSION,
        "protocol": "UACP/1.0",
        "updated_at": utc_now_iso(),
        "project": dict(project),
        "agents": {},
        "handoffs": {},
        "receipts": {},
    }


def validate_network_manifest(manifest):
    if not isinstance(manifest, dict):
        raise NetworkError("network manifest is missing or invalid")
    if manifest.get("format") != NETWORK_FORMAT or manifest.get("version") != NETWORK_VERSION:
        raise NetworkError("unsupported Agent Context Network format/version")
    if manifest.get("protocol") != "UACP/1.0":
        raise NetworkError("unsupported network Context protocol")
    project = manifest.get("project")
    if (
        not isinstance(project, dict)
        or not isinstance(project.get("name"), str)
        or not project["name"].strip()
        or len(project["name"]) > 200
        or "repository_fingerprint" not in project
    ):
        raise NetworkError("network project identity is invalid")
    fingerprint = project.get("repository_fingerprint")
    if fingerprint is not None and not re.match(
        r"^sha256:[0-9a-f]{24}$", str(fingerprint)
    ):
        raise NetworkError("network project identity is invalid")
    _parse_time(manifest.get("updated_at"), "network updated_at")
    agents = manifest.get("agents")
    handoffs = manifest.get("handoffs")
    receipts = manifest.get("receipts")
    if not all(isinstance(value, dict) for value in (agents, handoffs, receipts)):
        raise NetworkError("network agents/object indexes are invalid")
    if len(agents) > MAX_AGENTS:
        raise NetworkError("network exceeds the agent-count limit")
    if len(handoffs) + len(receipts) > MAX_REMOTE_OBJECTS:
        raise NetworkError("network exceeds the object-count limit")
    for agent_id, profile in agents.items():
        validate_agent_id(agent_id)
        validate_profile(profile, expected_id=agent_id)
    for object_id, meta in handoffs.items():
        if not HANDOFF_ID_RE.match(str(object_id)) or not _validate_meta(meta):
            raise NetworkError("network contains an invalid handoff index")
    for object_id, meta in receipts.items():
        if not RECEIPT_ID_RE.match(str(object_id)) or not _validate_meta(meta):
            raise NetworkError("network contains an invalid receipt index")
    return manifest


def build_profile(agent_id, display_name, adapter, previous=None):
    agent_id = validate_agent_id(agent_id)
    adapter = adapter if isinstance(adapter, dict) else {}
    capabilities = _normalise_capabilities(adapter.get("capabilities"))
    _validate_capabilities(capabilities, "agent profile capabilities")
    now = utc_now_iso()
    return {
        "agent_id": agent_id,
        "display_name": _safe_text(
            display_name or adapter.get("display_name") or agent_id,
            "display name", required=True, max_length=120,
        ),
        "adapter": _safe_text(
            adapter.get("name") or "generic", "adapter", required=True, max_length=80
        ),
        "capabilities": capabilities,
        "capability_profile": {
            name: {"status": str((entry or {}).get("status") or "unknown")}
            for name, entry in (adapter.get("capability_profile") or {}).items()
            if name in capabilities and isinstance(entry, dict)
        },
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
    }


def validate_profile(profile, expected_id=None):
    if not isinstance(profile, dict):
        raise NetworkError("network contains an invalid agent profile")
    agent_id = validate_agent_id(profile.get("agent_id"))
    if expected_id and agent_id != expected_id:
        raise NetworkError("agent profile id does not match its index")
    _safe_text(profile.get("display_name"), "display name", required=True, max_length=120)
    _safe_text(profile.get("adapter"), "adapter", required=True, max_length=80)
    caps = _validate_capabilities(
        profile.get("capabilities"), "agent profile capabilities"
    )
    capability_profile = profile.get("capability_profile")
    if not isinstance(capability_profile, dict) or len(capability_profile) > MAX_CAPABILITIES:
        raise NetworkError("agent capability profile is invalid")
    for name, entry in capability_profile.items():
        if (
            name not in caps
            or not isinstance(entry, dict)
            or entry.get("status") not in CAPABILITY_STATUSES
        ):
            raise NetworkError("agent capability profile is invalid")
    if scan_object(profile):
        raise NetworkError("agent profile failed the residual secret scan")
    created = _parse_time(profile.get("created_at"), "agent created_at")
    updated = _parse_time(profile.get("updated_at"), "agent updated_at")
    if updated < created:
        raise NetworkError("agent updated_at predates created_at")
    return profile


def build_handoff(sender, recipient, context_obj, branch, message=None,
                  expires_hours=None, recipient_profile=None):
    sender = validate_agent_id(sender)
    recipient = validate_agent_id(recipient)
    if sender == recipient:
        raise NetworkError("a handoff recipient must be a different agent")
    validate_context_object(context_obj, context_obj.get("context_id"))
    branch = Store.validate_branch_name(branch)
    expires_at = None
    if expires_hours is not None:
        if expires_hours < 1 or expires_hours > 24 * 365:
            raise NetworkError("handoff expiry must be between 1 and 8760 hours")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    target = recipient_profile or {
        "agent_id": recipient, "capabilities": [], "capability_profile": {}
    }
    compat = compatibility_report(context_obj, {
        "name": recipient,
        "capabilities": target.get("capabilities") or [],
        "capability_profile": target.get("capability_profile") or {},
    })
    obj = {
        "format": HANDOFF_FORMAT,
        "version": NETWORK_VERSION,
        "protocol": "UACP/1.0",
        "handoff_id": None,
        "created_at": utc_now_iso(),
        "expires_at": expires_at,
        "from_agent": sender,
        "to_agent": recipient,
        "context_id": context_obj["context_id"],
        "context_branch": branch,
        "message": _safe_text(message, "handoff message") or None,
        "capabilities_required": _normalise_capabilities(
            required_capabilities(context_obj)
        ),
        "compatibility": {
            "percent": compat["percent"],
            "satisfied": compat["satisfied"],
            "missing": compat["missing"],
            "unknown": compat["unknown"],
        },
    }
    obj["handoff_id"] = _object_id("hnd", obj, "handoff_id")
    validate_handoff(obj)
    return obj


def validate_handoff(obj, expected_id=None, expected_meta=None):
    if not isinstance(obj, dict) or obj.get("format") != HANDOFF_FORMAT \
            or obj.get("version") != NETWORK_VERSION:
        raise NetworkError("remote object is not a supported Context Handoff")
    if obj.get("protocol") != "UACP/1.0":
        raise NetworkError("unsupported handoff Context protocol")
    object_id = obj.get("handoff_id")
    if not HANDOFF_ID_RE.match(str(object_id or "")) or (
        expected_id and object_id != expected_id
    ):
        raise NetworkError("handoff id is invalid")
    if _object_id("hnd", obj, "handoff_id") != object_id:
        raise NetworkError("handoff failed identity verification")
    validate_agent_id(obj.get("from_agent"))
    validate_agent_id(obj.get("to_agent"))
    if obj.get("from_agent") == obj.get("to_agent"):
        raise NetworkError("handoff sender and recipient must differ")
    if not CTX_ID_RE.match(str(obj.get("context_id") or "")):
        raise NetworkError("handoff Context id is invalid")
    try:
        Store.validate_branch_name(obj.get("context_branch"))
    except StoreError as exc:
        raise NetworkError(str(exc))
    _safe_text(obj.get("message"), "handoff message")
    required = _validate_capabilities(
        obj.get("capabilities_required"), "handoff required capabilities"
    )
    created = _parse_time(obj.get("created_at"), "handoff created_at")
    if obj.get("expires_at") is not None:
        expires = _parse_time(obj["expires_at"], "handoff expiry")
        if expires <= created:
            raise NetworkError("handoff expiry must follow creation")
    compat = obj.get("compatibility")
    if not isinstance(compat, dict) or not isinstance(compat.get("percent"), int) \
            or not 0 <= compat["percent"] <= 100:
        raise NetworkError("handoff capability compatibility is invalid")
    for name in ("satisfied", "missing", "unknown"):
        _validate_capabilities(
            compat.get(name), "handoff capability compatibility"
        )
    partition = compat["satisfied"] + compat["missing"] + compat["unknown"]
    if len(partition) != len(set(partition)) or sorted(partition) != sorted(required):
        raise NetworkError("handoff capability compatibility is inconsistent")
    expected_percent = int(round(100.0 * len(compat["satisfied"]) / len(required))) \
        if required else 100
    if compat["percent"] != expected_percent:
        raise NetworkError("handoff capability compatibility percent is inconsistent")
    if scan_object(obj):
        raise NetworkError("handoff failed the residual secret scan")
    meta = _object_meta(obj)
    if expected_meta and (
        expected_meta.get("sha256") != meta["sha256"]
        or expected_meta.get("size") != meta["size"]
    ):
        raise NetworkError("handoff failed full SHA-256/size verification")
    return obj, meta


def build_receipt(handoff, by_agent, status, message=None):
    validate_handoff(handoff)
    by_agent = validate_agent_id(by_agent)
    if by_agent != handoff["to_agent"]:
        raise NetworkError("only the handoff recipient can publish a receipt")
    if status not in RECEIPT_STATUSES:
        raise NetworkError("receipt status must be accepted, completed, or rejected")
    obj = {
        "format": RECEIPT_FORMAT,
        "version": NETWORK_VERSION,
        "protocol": "UACP/1.0",
        "receipt_id": None,
        "handoff_id": handoff["handoff_id"],
        "by_agent": by_agent,
        "status": status,
        "created_at": utc_now_iso(),
        "message": _safe_text(message, "receipt message") or None,
    }
    obj["receipt_id"] = _object_id("rcp", obj, "receipt_id")
    validate_receipt(obj)
    return obj


def validate_receipt(obj, expected_id=None, expected_meta=None):
    if not isinstance(obj, dict) or obj.get("format") != RECEIPT_FORMAT \
            or obj.get("version") != NETWORK_VERSION:
        raise NetworkError("remote object is not a supported Context receipt")
    if obj.get("protocol") != "UACP/1.0":
        raise NetworkError("unsupported receipt Context protocol")
    object_id = obj.get("receipt_id")
    if not RECEIPT_ID_RE.match(str(object_id or "")) or (
        expected_id and object_id != expected_id
    ):
        raise NetworkError("receipt id is invalid")
    if _object_id("rcp", obj, "receipt_id") != object_id:
        raise NetworkError("receipt failed identity verification")
    if not HANDOFF_ID_RE.match(str(obj.get("handoff_id") or "")):
        raise NetworkError("receipt handoff id is invalid")
    validate_agent_id(obj.get("by_agent"))
    if obj.get("status") not in RECEIPT_STATUSES:
        raise NetworkError("receipt status is invalid")
    _safe_text(obj.get("message"), "receipt message")
    _parse_time(obj.get("created_at"), "receipt created_at")
    if scan_object(obj):
        raise NetworkError("receipt failed the residual secret scan")
    meta = _object_meta(obj)
    if expected_meta and (
        expected_meta.get("sha256") != meta["sha256"]
        or expected_meta.get("size") != meta["size"]
    ):
        raise NetworkError("receipt failed full SHA-256/size verification")
    return obj, meta


def _load_network(transport, project, required=False, allow_other_project=False):
    raw, state = transport.read_network_manifest(required=required)
    if raw is None:
        return empty_network_manifest(project), state
    manifest = validate_network_manifest(raw)
    try:
        _check_project(project, manifest.get("project"), allow_other_project)
    except RemoteError as exc:
        raise NetworkError(str(exc))
    return manifest, state


def _download_network_objects(store, transport, manifest):
    downloaded = {"handoffs": {}, "receipts": {}}
    for kind, validator in (("handoffs", validate_handoff), ("receipts", validate_receipt)):
        for object_id, meta in manifest[kind].items():
            local = store.load_network_object(kind, object_id)
            obj = local if local is not None else transport.read_network_object(kind, object_id)
            validator(obj, object_id, meta)
            downloaded[kind][object_id] = obj

    receipts_by_handoff = {}
    for receipt in downloaded["receipts"].values():
        handoff = downloaded["handoffs"].get(receipt["handoff_id"])
        if handoff is None:
            raise NetworkError("receipt points to a missing handoff")
        if receipt["by_agent"] != handoff["to_agent"]:
            raise NetworkError("receipt publisher is not the handoff recipient")
        if _parse_time(receipt["created_at"], "receipt created_at") < _parse_time(
            handoff["created_at"], "handoff created_at"
        ):
            raise NetworkError("receipt predates its handoff")
        receipts_by_handoff.setdefault(receipt["handoff_id"], []).append(receipt)
    for receipts in receipts_by_handoff.values():
        _validate_receipt_history(receipts)
    return downloaded


def _validate_context_pointers(downloaded, remote_manifest, local_store=None):
    advertised = remote_manifest.get("objects") or {}
    for handoff in downloaded["handoffs"].values():
        if handoff["context_id"] not in advertised:
            raise NetworkError(
                "handoff {} points to a Context missing from the remote manifest".format(
                    handoff["handoff_id"]
                )
            )
        if local_store is not None and local_store.load_context(handoff["context_id"]) is None:
            raise NetworkError(
                "handoff {} Context was not fetched; retry network sync".format(
                    handoff["handoff_id"]
                )
            )


def register(store, remote_name, transport, agent_id, display_name, adapter,
             allow_other_project=False, force=False):
    project = project_descriptor(store.root, store)
    remote_manifest, _ = transport.read_manifest(required=True)
    validate_manifest(remote_manifest)
    try:
        _check_project(project, remote_manifest.get("project"), allow_other_project)
    except RemoteError as exc:
        raise NetworkError(str(exc))
    manifest, state = _load_network(
        transport, project, allow_other_project=allow_other_project
    )
    downloaded = _download_network_objects(store, transport, manifest)
    _validate_context_pointers(downloaded, remote_manifest)
    agent_id = validate_agent_id(agent_id)
    local_identity = store.network_identity()
    if agent_id in manifest["agents"] and not (
        local_identity
        and local_identity.get("agent_id") == agent_id
        and local_identity.get("remote") == remote_name
    ) and not force:
        raise NetworkError(
            "agent id is already registered; use --force only for an intentional takeover"
        )
    profile = build_profile(
        agent_id, display_name, adapter, previous=manifest["agents"].get(agent_id)
    )
    manifest["agents"][agent_id] = profile
    manifest["updated_at"] = utc_now_iso()
    transport.write_network({}, manifest, state)
    local_identity = dict(profile)
    local_identity["remote"] = remote_name
    store.set_network_identity(local_identity)
    return {"action": "register", "remote": remote_name, "profile": profile}


def list_agents(store, transport, allow_other_project=False):
    project = project_descriptor(store.root, store)
    manifest, _ = _load_network(
        transport, project, required=True,
        allow_other_project=allow_other_project,
    )
    return dict(sorted(manifest["agents"].items()))


def send(store, remote_name, transport, recipient, branch, message=None,
         expires_hours=None, allow_other_project=False):
    identity = _local_identity(store, remote_name)
    branch = Store.validate_branch_name(branch)
    tip = store.list_branches().get(branch)
    if not tip:
        raise NetworkError("unknown or unborn local Context branch: {}".format(branch))
    context_obj = store.load_context(tip)
    project = project_descriptor(store.root, store)

    remote_manifest, _ = transport.read_manifest(required=True)
    validate_manifest(remote_manifest)
    try:
        _check_project(project, remote_manifest.get("project"), allow_other_project)
    except RemoteError as exc:
        raise NetworkError(str(exc))
    if tip not in remote_manifest["objects"]:
        raise NetworkError("Context {} is not published; push it before sending".format(tip))

    manifest, state = _load_network(
        transport, project, required=True,
        allow_other_project=allow_other_project,
    )
    downloaded = _download_network_objects(store, transport, manifest)
    _validate_context_pointers(downloaded, remote_manifest)
    recipient = validate_agent_id(recipient)
    if recipient not in manifest["agents"]:
        raise NetworkError("recipient is not registered on this network: {}".format(recipient))
    if identity["agent_id"] not in manifest["agents"]:
        raise NetworkError("local agent is no longer registered on this network")
    handoff = build_handoff(
        identity["agent_id"], recipient, context_obj, branch, message,
        expires_hours, manifest["agents"][recipient],
    )
    object_id = handoff["handoff_id"]
    manifest["handoffs"][object_id] = _object_meta(handoff)
    manifest["updated_at"] = utc_now_iso()
    transport.write_network({"handoffs": {object_id: handoff}}, manifest, state)
    store.save_network_object("handoffs", handoff)
    return {"action": "send", "remote": remote_name, "handoff": handoff}


def sync(store, remote_name, transport, allow_other_project=False):
    project = project_descriptor(store.root, store)
    remote_manifest, _ = transport.read_manifest(required=True)
    validate_manifest(remote_manifest)
    try:
        _check_project(project, remote_manifest.get("project"), allow_other_project)
    except RemoteError as exc:
        raise NetworkError(str(exc))
    manifest, _ = _load_network(
        transport, project, required=True,
        allow_other_project=allow_other_project,
    )
    downloaded = _download_network_objects(store, transport, manifest)
    _validate_context_pointers(downloaded, remote_manifest, local_store=store)

    added = 0
    for kind in ("handoffs", "receipts"):
        for obj in downloaded[kind].values():
            if store.save_network_object(kind, obj):
                added += 1
    return {
        "action": "sync", "remote": remote_name, "objects_received": added,
        "agents": dict(sorted(manifest["agents"].items())),
        "handoffs": list(downloaded["handoffs"].values()),
        "receipts": list(downloaded["receipts"].values()),
    }


def inbox(store, sync_result):
    identity = _local_identity(store)
    handoffs = [
        item for item in sync_result["handoffs"]
        if item["to_agent"] == identity["agent_id"]
    ]
    handoffs.sort(key=lambda item: (item.get("created_at") or "", item["handoff_id"]), reverse=True)
    receipts = sync_result["receipts"]
    for item in handoffs:
        item["receipts"] = sorted(
            [receipt for receipt in receipts if receipt["handoff_id"] == item["handoff_id"]],
            key=lambda receipt: (receipt.get("created_at") or "", receipt["receipt_id"]),
        )
    return handoffs


def accept(store, handoff_id, branch=None, switch=False):
    identity = _local_identity(store)
    handoff = store.load_network_object("handoffs", handoff_id)
    if handoff is None:
        raise NetworkError("unknown local handoff; run `context-git network inbox` first")
    validate_handoff(handoff, handoff_id)
    if handoff["to_agent"] != identity["agent_id"]:
        raise NetworkError("handoff belongs to another agent")
    if handoff.get("expires_at"):
        expires = _parse_time(handoff["expires_at"], "handoff expiry")
        if expires <= datetime.now(timezone.utc):
            raise NetworkError("handoff has expired")
    ctx_id = handoff["context_id"]
    if store.load_context(ctx_id) is None:
        raise NetworkError("handoff Context is missing; fetch the Context remote first")
    branch = branch or "handoff/{}/{}".format(
        handoff["from_agent"], handoff_id.split("_", 1)[1][:8]
    )
    branch = Store.validate_branch_name(branch)
    branches = store.list_branches()
    existing = branches.get(branch)
    if branch in branches and existing != ctx_id:
        raise NetworkError("Context branch already exists at a different tip: {}".format(branch))
    if branch not in branches:
        store.create_branch(branch, ctx_id)
    if switch:
        store.switch_branch(branch)
    return {
        "action": "accept", "handoff_id": handoff_id,
        "context_id": ctx_id, "branch": branch, "switched": bool(switch),
    }


def reply(store, remote_name, transport, handoff_id, status, message=None,
          allow_other_project=False):
    identity = _local_identity(store, remote_name)
    handoff = store.load_network_object("handoffs", handoff_id)
    if handoff is None:
        raise NetworkError("unknown local handoff; sync the inbox first")
    receipt = build_receipt(handoff, identity["agent_id"], status, message)
    project = project_descriptor(store.root, store)
    remote_manifest, _ = transport.read_manifest(required=True)
    validate_manifest(remote_manifest)
    try:
        _check_project(project, remote_manifest.get("project"), allow_other_project)
    except RemoteError as exc:
        raise NetworkError(str(exc))
    manifest, state = _load_network(
        transport, project, required=True,
        allow_other_project=allow_other_project,
    )
    downloaded = _download_network_objects(store, transport, manifest)
    _validate_context_pointers(downloaded, remote_manifest)
    if handoff_id not in manifest["handoffs"]:
        raise NetworkError("handoff is no longer advertised by the network")
    existing_receipts = [
        candidate for candidate in downloaded["receipts"].values()
        if candidate["handoff_id"] == handoff_id
    ]
    _validate_receipt_history(existing_receipts + [receipt])
    object_id = receipt["receipt_id"]
    manifest["receipts"][object_id] = _object_meta(receipt)
    manifest["updated_at"] = utc_now_iso()
    transport.write_network({"receipts": {object_id: receipt}}, manifest, state)
    store.save_network_object("receipts", receipt)
    return {"action": "reply", "remote": remote_name, "receipt": receipt}


def sent_status(store, sync_result, handoff_id=None):
    identity = _local_identity(store)
    items = [
        item for item in sync_result["handoffs"]
        if item["from_agent"] == identity["agent_id"]
        and (handoff_id is None or item["handoff_id"] == handoff_id)
    ]
    if handoff_id and not items:
        raise NetworkError("unknown sent handoff: {}".format(handoff_id))
    for item in items:
        item["receipts"] = sorted(
            [r for r in sync_result["receipts"] if r["handoff_id"] == item["handoff_id"]],
            key=lambda r: (r.get("created_at") or "", r["receipt_id"]),
        )
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def _validate_receipt_history(receipts):
    ordered = sorted(
        receipts,
        key=lambda item: (_parse_time(item["created_at"], "receipt created_at"),
                          item["receipt_id"]),
    )
    previous = None
    for receipt in ordered:
        status = receipt["status"]
        if previous in ("completed", "rejected"):
            raise NetworkError("receipt cannot follow terminal status {}".format(previous))
        if previous == "accepted" and status == "accepted":
            raise NetworkError("accepted receipt has already been published")
        previous = status

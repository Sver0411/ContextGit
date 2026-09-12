"""Safe, explicit ingestion of local agent session exports.

Session files are private and often contain secrets, tool payloads and model
reasoning.  Context Git therefore never reads them during normal commands.
This module is reached only through ``import-session``/``sessions`` and keeps
only deterministic, compact work-state fields.  Raw messages, tool calls,
system/developer prompts and reasoning are never returned to the caller.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .common import is_forbidden_path
from .security import redact, scan_object

MAX_SESSION_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 2000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_FIELD_CHARS = 600
MAX_ITEMS_PER_FIELD = 30

_ROLE_NAMES = {
    "user": "user", "human": "user", "用户": "user",
    "user_message": "user",
    "assistant": "assistant", "ai": "assistant", "gemini": "assistant",
    "agent_message": "assistant", "助手": "assistant",
}
_TEXT_TYPES = {"text", "input_text", "output_text"}
_PRIVATE_TYPES = {
    "reasoning", "thinking", "analysis", "tool", "tool_call",
    "tool_result", "function_call", "function_result", "image",
    "input_image", "output_image",
}
_ACKS = {
    "ok", "okay", "thanks", "thank you", "done", "continue", "yes", "no",
    "好的", "好", "谢谢", "继续", "可以", "完成",
}

_SECTION_ALIASES = {
    "completed": {"completed", "done", "completed work", "已完成", "完成事项"},
    "in_progress": {"in progress", "current work", "working on", "进行中", "当前工作"},
    "pending": {"pending", "todo", "remaining", "remaining work", "待办", "未完成"},
    "decisions": {"decisions", "decision", "决策", "重要决策"},
    "constraints": {"constraints", "constraint", "约束", "限制"},
    "do_not_change": {"do not change", "must not change", "不要修改", "不可修改"},
    "known_issues": {"known issues", "issues", "blockers", "已知问题", "阻塞问题"},
    "recommended_actions": {
        "next steps", "next step", "recommended actions", "recommendation",
        "下一步", "建议操作", "后续步骤",
    },
    "capabilities_required": {
        "capabilities required", "required capabilities", "所需能力", "需要的能力",
    },
    "architecture": {"architecture", "architecture facts", "架构", "架构事实"},
}


class SessionImportError(Exception):
    """A private session cannot be safely or meaningfully imported."""


def _normalise_role(value):
    return _ROLE_NAMES.get(str(value or "").strip().lower())


def _visible_text(content):
    """Extract visible message text while rejecting tool/reasoning payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            if kind in _PRIVATE_TYPES:
                continue
            if kind in _TEXT_TYPES or (not kind and isinstance(item.get("text"), str)):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(content, dict):
        kind = str(content.get("type") or "").lower()
        if kind in _PRIVATE_TYPES:
            return ""
        if kind in _TEXT_TYPES or not kind:
            value = content.get("text")
            return value if isinstance(value, str) else ""
    return ""


def _message_from_record(record):
    if not isinstance(record, dict):
        return None
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if str(record.get("type") or "").lower() == "response_item":
        if str(payload.get("type") or "").lower() != "message":
            return None

    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    role = _normalise_role(message.get("role"))
    if not role:
        role = _normalise_role(record.get("role"))
    if not role:
        role = _normalise_role(record.get("type"))
    if not role:
        role = _normalise_role(message.get("type"))
    if not role:
        return None
    text = _visible_text(message.get("content"))
    if not text and isinstance(message.get("text"), str):
        text = message["text"]
    if not text and isinstance(message.get("message"), str):
        text = message["message"]
    text = _clean_message(text)
    return {"role": role, "text": text} if text else None


def _metadata_from_record(record, meta):
    if not isinstance(record, dict):
        return
    candidates = [record]
    for key in ("payload", "session", "metadata", "workspace"):
        if isinstance(record.get(key), dict):
            candidates.append(record[key])
    for item in candidates:
        for key in ("cwd", "project_path", "workspace_path", "root"):
            value = item.get(key)
            if isinstance(value, str) and value.strip() and "project_root" not in meta:
                meta["project_root"] = value.strip()


def _clean_message(text):
    if not isinstance(text, str):
        return ""
    # Common agent envelopes are transport metadata, not user work state.
    for tag in (
        "system-reminder", "developer", "app-context", "environment_context",
        "permissions", "skills_instructions", "recommended_plugins",
    ):
        text = re.sub(
            r"<" + re.escape(tag) + r"\b[^>]*>.*?</" + re.escape(tag) + r">",
            " ", text, flags=re.IGNORECASE | re.DOTALL,
        )
    text = text.replace("\x00", " ")
    return text.strip()


def _parse_json_records(data):
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("messages", "history", "turns", "items"):
            if isinstance(data.get(key), list):
                records = data[key]
                break
        if not records:
            records = [data]
    return records


def _parse_text_transcript(text):
    marker = re.compile(
        r"^(?:#{1,4}\s*)?(user|human|assistant|ai|用户|助手)\s*:\s*(.*)$",
        re.IGNORECASE,
    )
    messages = []
    role = None
    lines = []
    for line in text.splitlines():
        match = marker.match(line.strip())
        if match:
            if role and lines:
                cleaned = _clean_message("\n".join(lines))
                if cleaned:
                    messages.append({"role": role, "text": cleaned})
            role = _normalise_role(match.group(1))
            lines = [match.group(2)]
        elif role:
            lines.append(line)
    if role and lines:
        cleaned = _clean_message("\n".join(lines))
        if cleaned:
            messages.append({"role": role, "text": cleaned})
    if not messages and text.strip():
        # A plain handoff-style document can still contribute structured
        # headings, but is deliberately not copied as a raw user message.
        messages.append({"role": "assistant", "text": _clean_message(text)})
    return messages


def read_session(path, provider=None, project_root=None, session_id=None):
    """Parse a supported session file into visible role/text messages.

    The return value never contains tool calls, reasoning or raw records.
    """
    source = Path(path).expanduser()
    if source.is_symlink():
        raise SessionImportError("refusing a symlinked session file")
    try:
        source = source.resolve()
    except OSError as exc:
        raise SessionImportError("cannot resolve session path: {}".format(exc))
    if is_forbidden_path(source):
        raise SessionImportError("refusing a credential-shaped source path")
    try:
        stat = source.stat()
    except OSError as exc:
        raise SessionImportError("cannot read session: {}".format(exc))
    if not source.is_file():
        raise SessionImportError("session source is not a regular file")
    suffix = source.suffix.lower()
    if suffix in (".db", ".sqlite", ".sqlite3"):
        if provider != "opencode":
            raise SessionImportError("SQLite session stores currently require --agent opencode")
        return _read_opencode_db(source, project_root=project_root, session_id=session_id)
    if stat.st_size > MAX_SESSION_BYTES:
        raise SessionImportError(
            "session is larger than {} MiB; export a smaller transcript"
            .format(MAX_SESSION_BYTES // (1024 * 1024))
        )
    if suffix not in (".json", ".jsonl", ".md", ".txt"):
        raise SessionImportError(
            "supported session formats: .json, .jsonl, .md, .txt, "
            "or an OpenCode .db"
        )
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SessionImportError("cannot read session: {}".format(exc))
    text = raw.decode("utf-8", errors="replace")
    meta = {}
    messages = []
    fmt = suffix.lstrip(".")

    if fmt == "jsonl":
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            _metadata_from_record(record, meta)
            message = _message_from_record(record)
            if message:
                messages.append(message)
            if len(messages) >= MAX_MESSAGES:
                break
    elif fmt == "json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SessionImportError("invalid JSON session: {}".format(exc))
        if isinstance(data, dict):
            _metadata_from_record(data, meta)
        for record in _parse_json_records(data)[:MAX_MESSAGES]:
            _metadata_from_record(record, meta)
            message = _message_from_record(record)
            if message:
                messages.append(message)
    else:
        messages = _parse_text_transcript(text)[:MAX_MESSAGES]

    total = 0
    bounded = []
    for message in messages:
        size = len(message["text"].encode("utf-8", errors="replace"))
        if total + size > MAX_TEXT_BYTES:
            break
        total += size
        bounded.append(message)
    if not bounded:
        raise SessionImportError("no visible user/assistant messages found")
    return {
        "provider": provider or infer_provider(source),
        "format": fmt,
        "messages": bounded,
        "message_count": len(bounded),
        "project_root": meta.get("project_root"),
        "source_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
    }


def _sqlite_connection(source):
    try:
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (sqlite3.Error, ValueError) as exc:
        raise SessionImportError("cannot open OpenCode database read-only: {}".format(exc))


def _same_project_path(value, root):
    try:
        return Path(value).expanduser().resolve() == Path(root).resolve()
    except (OSError, ValueError, TypeError):
        return False


def _opencode_sessions(source, project_root, limit=10):
    connection = _sqlite_connection(source)
    try:
        rows = connection.execute(
            "SELECT id, directory, time_updated FROM session "
            "ORDER BY time_updated DESC LIMIT 1000"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SessionImportError("unsupported OpenCode database schema: {}".format(exc))
    finally:
        connection.close()
    matches = []
    for session_id, directory, updated in rows:
        if _same_project_path(directory, project_root):
            matches.append({
                "path": str(source),
                "provider": "opencode",
                "format": "sqlite",
                "session_id": str(session_id),
                "modified_at": (float(updated) / 1000.0) if updated else source.stat().st_mtime,
            })
        if len(matches) >= max(1, limit):
            break
    return matches


def _read_opencode_db(source, project_root=None, session_id=None):
    """Read only visible text parts from one OpenCode SQLite session."""
    connection = _sqlite_connection(source)
    try:
        if session_id:
            row = connection.execute(
                "SELECT id, directory, time_updated FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row and project_root and not _same_project_path(row[1], project_root):
                raise SessionImportError("OpenCode session belongs to a different project")
        else:
            rows = connection.execute(
                "SELECT id, directory, time_updated FROM session "
                "ORDER BY time_updated DESC LIMIT 1000"
            ).fetchall()
            row = next(
                (item for item in rows if project_root and _same_project_path(item[1], project_root)),
                None,
            )
        if not row:
            raise SessionImportError("no matching OpenCode session in database")

        message_rows = connection.execute(
            "SELECT m.id, m.data, p.data "
            "FROM message AS m LEFT JOIN part AS p ON p.message_id = m.id "
            "WHERE m.session_id = ? "
            "ORDER BY m.time_created, m.id, p.time_created, p.id LIMIT 10000",
            (row[0],),
        ).fetchall()
    except SessionImportError:
        raise
    except sqlite3.Error as exc:
        raise SessionImportError("unsupported OpenCode database schema: {}".format(exc))
    finally:
        connection.close()

    grouped = {}
    order = []
    for message_id, message_blob, part_blob in message_rows:
        if message_id not in grouped:
            try:
                message_data = json.loads(message_blob)
            except (json.JSONDecodeError, TypeError, ValueError):
                message_data = {}
            role = _normalise_role(message_data.get("role"))
            grouped[message_id] = {"role": role, "parts": []}
            order.append(message_id)
        try:
            part = json.loads(part_blob) if part_blob else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            part = {}
        if str(part.get("type") or "").lower() == "text" and isinstance(part.get("text"), str):
            grouped[message_id]["parts"].append(part["text"])

    messages = []
    total = 0
    for message_id in order:
        item = grouped[message_id]
        text = _clean_message("\n".join(item["parts"]))
        if not item["role"] or not text:
            continue
        size = len(text.encode("utf-8", errors="replace"))
        if total + size > MAX_TEXT_BYTES or len(messages) >= MAX_MESSAGES:
            break
        total += size
        messages.append({"role": item["role"], "text": text})
    if not messages:
        raise SessionImportError("no visible user/assistant text in OpenCode session")
    fingerprint_input = "{}:{}:{}".format(row[0], row[2] or 0, len(messages))
    return {
        "provider": "opencode",
        "format": "sqlite",
        "messages": messages,
        "message_count": len(messages),
        "project_root": row[1],
        "source_fingerprint": "sha256:" + hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest()[:16],
    }


def infer_provider(path):
    low = str(Path(path)).replace("\\", "/").lower()
    if "/.codex/" in low:
        return "codex"
    if "/.claude/" in low:
        return "claude-code"
    if "/.gemini/" in low:
        return "gemini-cli"
    if "/.cursor/" in low:
        return "cursor"
    if "/opencode/" in low:
        return "opencode"
    return "generic"


def _compact_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    value = " ".join(lines)
    value = re.sub(r"\s+", " ", value)
    value = redact(value)
    if len(value) > MAX_FIELD_CHARS:
        value = value[:MAX_FIELD_CHARS - 1].rstrip() + "…"
    return value


def _objective_summary(text):
    """Select one request-sized line, not a pasted attachment or transcript."""
    value = _clean_message(text)
    request_markers = list(re.finditer(
        r"(?:^|\n)\s*#*\s*(?:my request|我的请求|用户请求)\s*[:：]\s*",
        value, flags=re.IGNORECASE,
    ))
    if request_markers:
        value = value[request_markers[-1].end():]
    for raw in value.splitlines():
        line = raw.strip()
        if not line or line.startswith(("```", "<", ">")):
            continue
        low = line.lstrip("# ").lower()
        if low.startswith((
            "files mentioned", "files pasted", "attached files", "attachments",
            "文件", "附件",
        )):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        compact = _compact_line(line)
        if compact:
            if len(compact) > 320:
                compact = compact[:319].rstrip() + "…"
            return compact
    return ""


def _is_substantive(text):
    compact = _objective_summary(text)
    return len(compact) >= 4 and compact.lower().strip(".!。！ ") not in _ACKS


def _heading_key(line):
    value = re.sub(r"^[#>*\s]+", "", line.strip())
    value = re.sub(r"[:：\s]+$", "", value).strip().lower()
    for key, aliases in _SECTION_ALIASES.items():
        if value in aliases:
            return key
    return None


def _parse_structured_sections(messages):
    out = {}
    bullet = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
    checkbox = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$")
    for message in messages[-8:]:
        if message["role"] != "assistant":
            continue
        section = None
        for raw_line in message["text"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = _heading_key(line)
            if heading:
                section = heading
                continue
            checked = checkbox.match(line)
            if checked:
                key = "completed" if checked.group(1).strip().lower() == "x" else "pending"
                value = _compact_line(checked.group(2))
                if value:
                    out.setdefault(key, []).append(value)
                continue
            match = bullet.match(line)
            if section and match:
                value = _compact_line(match.group(1))
                if value:
                    out.setdefault(section, []).append(value)
                continue
            # A heading followed by one unbulleted value is common in compact
            # agent summaries.  Stop a section on prose longer than one line.
            if section and len(line) <= MAX_FIELD_CHARS:
                value = _compact_line(line)
                if value:
                    out.setdefault(section, []).append(value)
                section = None

    for key, values in list(out.items()):
        unique = []
        seen = set()
        for value in values:
            identity = value.casefold()
            if identity not in seen:
                unique.append(value)
                seen.add(identity)
        out[key] = unique[:MAX_ITEMS_PER_FIELD]
    return out


def session_to_notes(parsed, include_goal=True):
    """Compress parsed visible messages into UACP narrative input fields."""
    messages = parsed.get("messages") or []
    users = [
        _objective_summary(m["text"]) for m in messages
        if m.get("role") == "user" and _is_substantive(m["text"])
    ]
    notes = _parse_structured_sections(messages)
    if users:
        if include_goal:
            notes["goal"] = users[0]
        notes["current_objective"] = users[-1]
    elif not notes:
        assistants = [
            _objective_summary(m["text"]) for m in messages
            if m.get("role") == "assistant" and _objective_summary(m["text"])
        ]
        if assistants:
            notes["current_objective"] = assistants[-1]
    # Do not save an entire transcript as metadata.  The provenance record
    # below is sufficient to audit how the context was produced.
    notes = {key: value for key, value in notes.items() if value not in (None, [], "")}
    findings = scan_object(notes)
    if findings:
        # redact() has already been applied field-by-field; this is a fail-
        # closed guard in case a new secret shape survived compression.
        raise SessionImportError("potential secret remained after session redaction")
    return notes


def import_metadata(parsed, notes):
    """Non-sensitive provenance persisted alongside an imported context."""
    return {
        "provider": parsed.get("provider") or "generic",
        "format": parsed.get("format"),
        "source_fingerprint": parsed.get("source_fingerprint"),
        "messages_considered": parsed.get("message_count", 0),
        "fields_extracted": sorted(notes.keys()),
        "raw_transcript_stored": False,
        "tool_payloads_stored": False,
        "reasoning_stored": False,
    }


def discover_sessions(provider, project_root, home=None, limit=10):
    """Find recent provider sessions whose embedded cwd matches the project.

    Discovery is intentionally opt-in and returns paths only to the caller of
    the explicit ``sessions`` command.  Unrelated sessions are not returned.
    """
    provider = str(provider or "").lower()
    # Locations belong to adapters, not the ingestion core, so new agents can
    # be added without changing parser code.
    from .capabilities import load_adapters
    adapter = load_adapters().get(provider) or {}
    patterns = adapter.get("session_globs") or []
    if not patterns:
        return []
    base = Path(home or Path.home()).expanduser()
    candidates = []
    for pattern in patterns:
        candidates.extend(Path(p) for p in glob.glob(str(base / pattern), recursive=True))
    if home is None:
        for location in adapter.get("session_env_roots") or []:
            env_root = os.environ.get(str(location.get("env") or ""))
            if not env_root:
                continue
            for pattern in location.get("globs") or []:
                candidates.extend(Path(p) for p in glob.glob(
                    str(Path(env_root).expanduser() / pattern), recursive=True,
                ))
    regular = []
    for path in set(candidates):
        try:
            is_database = path.suffix.lower() in (".db", ".sqlite", ".sqlite3")
            if (path.is_file() and not path.is_symlink()
                    and (is_database or path.stat().st_size <= MAX_SESSION_BYTES)):
                regular.append(path)
        except OSError:
            continue
    regular.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    wanted = Path(project_root).resolve()
    matches = []
    # Bound private reads even when a user's session directory is enormous.
    for path in regular[:100]:
        if provider == "opencode" and path.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
            try:
                matches.extend(_opencode_sessions(path, wanted, limit=limit - len(matches)))
            except SessionImportError:
                continue
            if len(matches) >= max(1, limit):
                break
            continue
        if _path_encodes_project(provider, path, wanted):
            embedded = str(wanted)
        else:
            try:
                embedded = _session_project_root(path)
            except SessionImportError:
                continue
        if not embedded:
            continue
        try:
            same_project = Path(embedded).expanduser().resolve() == wanted
        except OSError:
            same_project = False
        if same_project:
            try:
                parsed = read_session(path, provider=provider)
            except SessionImportError:
                continue
            matches.append({
                "path": str(path),
                "provider": provider,
                "format": parsed.get("format"),
                "message_count": parsed.get("message_count"),
                "source_fingerprint": parsed.get("source_fingerprint"),
                "modified_at": path.stat().st_mtime,
            })
        if len(matches) >= max(1, limit):
            break
    return matches


def _path_encodes_project(provider, path, project_root):
    normal = str(path).replace("\\", "/")
    if provider == "gemini-cli":
        project_hash = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()
        return "/.gemini/tmp/{}/chats/".format(project_hash) in normal
    if provider == "cursor":
        root_text = str(project_root).replace("\\", "/").lstrip("/")
        slug = root_text.replace("/", "-")
        return "/.cursor/projects/{}/agent-transcripts/".format(slug) in normal
    return False


def _session_project_root(path, sniff_bytes=512 * 1024):
    """Read only a bounded prefix while matching a session to a project."""
    source = Path(path)
    try:
        with open(source, "rb") as handle:
            text = handle.read(sniff_bytes).decode("utf-8", errors="replace")
    except OSError as exc:
        raise SessionImportError("cannot inspect session: {}".format(exc))
    meta = {}
    if source.suffix.lower() == ".jsonl":
        for line in text.splitlines():
            try:
                _metadata_from_record(json.loads(line), meta)
            except (json.JSONDecodeError, ValueError):
                continue
            if meta.get("project_root"):
                break
    elif source.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(data, dict):
            _metadata_from_record(data, meta)
            for record in _parse_json_records(data)[:20]:
                _metadata_from_record(record, meta)
                if meta.get("project_root"):
                    break
    return meta.get("project_root")

"""storage — the .context-git/ store: HEAD, config, context objects.

Layout::

    .context-git/
    ├── HEAD            # "context: ctx_ab12ef34" — current context pointer
    ├── config.json     # store metadata (created_at, tool, defaults)
    ├── contexts/
    │   └── ctx_ab12ef34.json   # one Context Object per file
    └── HANDOFF.md      # auto-rendered human view of HEAD (updated on commit)

The store is append-only for contexts/; HEAD and HANDOFF.md are the only
files that get rewritten. Everything is plain files so humans (and other
tools) can read it without this package.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .common import read_json, write_json, write_text

CTX_ID_RE = re.compile(r"^ctx_[0-9a-f]{8,16}$")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class StoreError(Exception):
    """Raised for store-level problems the CLI should report nicely."""


class Store:
    """Filesystem-backed context store rooted at ``<project>/.context-git``."""

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.dir = self.root / ".context-git"
        self.contexts_dir = self.dir / "contexts"
        self.head_file = self.dir / "HEAD"
        self.config_file = self.dir / "config.json"

    # -- lifecycle ----------------------------------------------------------

    def exists(self) -> bool:
        return self.dir.is_dir() and self.contexts_dir.is_dir()

    def init(self, tool_name, tool_version):
        if self.exists():
            return False
        self.contexts_dir.mkdir(parents=True, exist_ok=True)
        if not self.head_file.exists():
            write_text(self.head_file, "context: (no context yet)\n")
        config = {
            "version": 1,
            "created_at": utc_now_iso(),
            "tool": {"name": tool_name, "version": tool_version},
            "notes": "Append-only context store. Do not commit secrets into "
                     "this directory; contexts are redacted on write.",
        }
        write_json(self.config_file, config)
        return True

    # -- HEAD -----------------------------------------------------------------

    def head(self):
        """Return the current context id, or None."""
        try:
            text = self.head_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m = re.search(r"ctx_[0-9a-f]{8,16}", text or "")
        return m.group(0) if m else None

    def set_head(self, ctx_id):
        if not CTX_ID_RE.match(ctx_id or ""):
            raise StoreError("invalid context id: {!r}".format(ctx_id))
        if self.load_context(ctx_id) is None:
            raise StoreError("unknown context: {}".format(ctx_id))
        write_text(self.head_file, "context: {}\n".format(ctx_id))

    def head_object(self):
        ctx_id = self.head()
        return self.load_context(ctx_id) if ctx_id else None

    # -- contexts ---------------------------------------------------------------

    def context_path(self, ctx_id) -> Path:
        return self.contexts_dir / "{}.json".format(ctx_id)

    def save_context(self, obj) -> str:
        """Persist a Context Object atomically. Returns its id."""
        ctx_id = obj.get("context_id")
        if not ctx_id or not CTX_ID_RE.match(ctx_id):
            raise StoreError("refusing to save context without a valid context_id")
        path = self.context_path(ctx_id)
        if path.exists():
            raise StoreError(
                "context {} already exists — context ids are immutable".format(ctx_id)
            )
        write_json(path, obj)
        return ctx_id

    def load_context(self, ctx_id):
        """Load a context by id. Returns None if missing/corrupt."""
        if not ctx_id:
            return None
        data = read_json(self.context_path(ctx_id))
        if data is not None and data.get("context_id") != ctx_id:
            # corrupt or renamed file — treat as missing
            return None
        return data

    def list_context_ids(self):
        """All context ids, oldest first."""
        if not self.contexts_dir.is_dir():
            return []
        ids = []
        for p in self.contexts_dir.glob("ctx_*.json"):
            ids.append(p.stem)
        ids.sort(key=lambda cid: (self._created_at(cid), cid))
        return ids

    def _created_at(self, ctx_id):
        obj = self.load_context(ctx_id)
        return (obj or {}).get("created_at") or ""

    def history(self, from_id=None, limit=None):
        """Walk the parent chain from ``from_id`` (default HEAD) newest-first."""
        seen = set()
        chain = []
        cur = from_id or self.head()
        while cur and cur not in seen:
            seen.add(cur)
            obj = self.load_context(cur)
            if obj is None:
                break
            chain.append(obj)
            cur = obj.get("parent_context_id")
            if limit and len(chain) >= limit:
                break
        return chain

    def previous_head(self, before_id=None):
        """The parent of HEAD (or of ``before_id``) — used by checkout -."""
        obj = self.load_context(before_id or self.head())
        return (obj or {}).get("parent_context_id")

    def has_children(self, ctx_id):
        """Whether any stored context already descends directly from ctx_id."""
        if not ctx_id:
            return False
        return any(
            (self.load_context(candidate) or {}).get("parent_context_id") == ctx_id
            for candidate in self.list_context_ids()
        )

    # -- checkout undo (single-level reflog) ---------------------------------

    @property
    def prev_file(self) -> Path:
        return self.dir / "PREV"

    def remember_position(self, ctx_id):
        """Record where we are before a checkout, enabling `checkout -`."""
        if ctx_id:
            write_text(self.prev_file, ctx_id + "\n")

    def recalled_position(self):
        """Last remembered position (or None). Does not clear it."""
        try:
            text = self.prev_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        m = re.search(r"ctx_[0-9a-f]{8,16}", text or "")
        return m.group(0) if m else None

    # -- summary for status/doctor -------------------------------------------------

    def stats(self):
        ids = self.list_context_ids()
        return {
            "exists": self.exists(),
            "context_count": len(ids),
            "head": self.head(),
            "oldest": ids[0] if ids else None,
            "newest": ids[-1] if ids else None,
        }

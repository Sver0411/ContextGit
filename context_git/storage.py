"""storage — the .context-git/ store: refs, HEAD, and context objects.

Layout::

    .context-git/
    ├── HEAD            # symbolic branch ref, or detached context pointer
    ├── refs/heads/     # V3 context branches (never Git repository refs)
    ├── config.json     # store metadata (created_at, tool, defaults)
    ├── contexts/
    │   └── ctx_ab12ef34.json   # one Context Object per file
    └── HANDOFF.md      # auto-rendered human view of HEAD (updated on commit)

The store is append-only for contexts/. HEAD, branch refs, and rendered views
may move. Everything is plain files so humans and other tools can inspect it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .common import read_json, write_json, write_text

CTX_ID_RE = re.compile(r"^ctx_[0-9a-f]{8,16}$")
BRANCH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
        self.refs_dir = self.dir / "refs" / "heads"
        self.head_file = self.dir / "HEAD"
        self.config_file = self.dir / "config.json"

    # -- lifecycle ----------------------------------------------------------

    def exists(self) -> bool:
        return self.dir.is_dir() and self.contexts_dir.is_dir()

    def init(self, tool_name, tool_version):
        if self.exists():
            return False
        self.contexts_dir.mkdir(parents=True, exist_ok=True)
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        if not self.head_file.exists():
            write_text(self._ref_path("main"), "")
            self._write_symbolic_head("main", None)
        config = {
            "version": 2,
            "created_at": utc_now_iso(),
            "tool": {"name": tool_name, "version": tool_version},
            "notes": "Append-only context store. Do not commit secrets into "
                     "this directory; contexts are redacted on write.",
        }
        write_json(self.config_file, config)
        return True

    # -- HEAD -----------------------------------------------------------------

    @staticmethod
    def validate_branch_name(name):
        """Return a conservative, cross-platform branch name or raise.

        Context branch names deliberately support fewer edge cases than Git
        refs. This keeps refs safe on Windows and prevents path traversal.
        """
        name = str(name or "").strip().replace("\\", "/")
        parts = name.split("/")
        if (
            not name or name.startswith("/") or name.endswith("/")
            or "//" in name or len(name) > 180
            or any(
                part in ("", ".", "..")
                or part.endswith((".", ".lock"))
                or not BRANCH_PART_RE.match(part)
                for part in parts
            )
        ):
            raise StoreError("invalid context branch name: {!r}".format(name))
        return name

    def _read_head_text(self):
        try:
            return self.head_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _write_symbolic_head(self, branch, ctx_id):
        cached = ctx_id or "(no context yet)"
        write_text(
            self.head_file,
            "ref: refs/heads/{}\ncontext: {}\n".format(branch, cached),
        )

    def _ref_path(self, branch):
        branch = self.validate_branch_name(branch)
        path = self.refs_dir.joinpath(*branch.split("/"))
        try:
            path.resolve().relative_to(self.refs_dir.resolve())
        except (OSError, ValueError):
            raise StoreError("context branch escapes refs directory")
        return path

    def current_branch(self):
        """Return the symbolic context branch, or None when detached/legacy."""
        match = re.search(r"^ref: refs/heads/(.+)$", self._read_head_text(), re.MULTILINE)
        if not match:
            return None
        try:
            return self.validate_branch_name(match.group(1).strip())
        except StoreError:
            return None

    def ensure_branch_layout(self, default="main"):
        """Migrate a V1/V2 linear HEAD to a V3 symbolic branch, in place."""
        branch = self.current_branch()
        if branch:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
            path = self._ref_path(branch)
            if not path.exists():
                write_text(path, "")
            return branch
        current = self.head()
        branch = self.validate_branch_name(default)
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        if current:
            write_text(self._ref_path(branch), current + "\n")
        self._write_symbolic_head(branch, current)
        return branch

    def head(self):
        """Return the current context id, or None."""
        text = self._read_head_text()
        branch = self.current_branch()
        if branch:
            try:
                ref_text = self._ref_path(branch).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                ref_text = ""
            match = re.search(r"ctx_[0-9a-f]{8,16}", ref_text)
            if match:
                return match.group(0)
            # The cached context line makes HEAD understandable to older tools,
            # but a present symbolic ref is authoritative even when unborn.
            return None
        m = re.search(r"ctx_[0-9a-f]{8,16}", text or "")
        return m.group(0) if m else None

    def set_head(self, ctx_id):
        """Advance the active branch, or detached HEAD, to ``ctx_id``."""
        if not CTX_ID_RE.match(ctx_id or ""):
            raise StoreError("invalid context id: {!r}".format(ctx_id))
        if self.load_context(ctx_id) is None:
            raise StoreError("unknown context: {}".format(ctx_id))
        branch = self.current_branch()
        if branch:
            write_text(self._ref_path(branch), ctx_id + "\n")
            self._write_symbolic_head(branch, ctx_id)
        else:
            write_text(self.head_file, "context: {}\n".format(ctx_id))

    def detach(self, ctx_id):
        """Detach context HEAD at an existing object."""
        if self.load_context(ctx_id) is None:
            raise StoreError("unknown context: {}".format(ctx_id))
        write_text(self.head_file, "context: {}\n".format(ctx_id))

    # -- branches -----------------------------------------------------------

    def list_branches(self):
        """Return ``{branch_name: context_id_or_None}`` in name order."""
        if not self.refs_dir.is_dir():
            return {}
        result = {}
        for path in self.refs_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            name = path.relative_to(self.refs_dir).as_posix()
            try:
                self.validate_branch_name(name)
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, StoreError):
                continue
            match = re.search(r"ctx_[0-9a-f]{8,16}", text)
            ctx_id = match.group(0) if match else None
            if ctx_id is None or self.load_context(ctx_id) is not None:
                result[name] = ctx_id
        return dict(sorted(result.items()))

    def create_branch(self, name, start_id=None):
        name = self.validate_branch_name(name)
        if self.current_branch() is None and not self.refs_dir.is_dir():
            self.ensure_branch_layout()
        else:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
        path = self._ref_path(name)
        if path.exists():
            raise StoreError("context branch already exists: {}".format(name))
        target = start_id or self.head()
        if target and self.load_context(target) is None:
            raise StoreError("unknown context: {}".format(target))
        if target:
            write_text(path, target + "\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text(path, "")
        return target

    def switch_branch(self, name):
        name = self.validate_branch_name(name)
        branches = self.list_branches()
        if name not in branches:
            raise StoreError("unknown context branch: {}".format(name))
        self._write_symbolic_head(name, branches[name])
        return branches[name]

    def delete_branch(self, name):
        name = self.validate_branch_name(name)
        if name == self.current_branch():
            raise StoreError("cannot delete the current context branch: {}".format(name))
        path = self._ref_path(name)
        if not path.is_file() or path.is_symlink():
            raise StoreError("unknown context branch: {}".format(name))
        path.unlink()
        parent = path.parent
        while parent != self.refs_dir and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def resolve(self, name):
        """Resolve HEAD, a context id, or a context branch to a context id."""
        if name in (None, "HEAD"):
            return self.head()
        if CTX_ID_RE.match(str(name)):
            return str(name) if self.load_context(str(name)) is not None else None
        try:
            return self.list_branches().get(self.validate_branch_name(name))
        except StoreError:
            return None

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
        """Walk the first-parent chain from ``from_id`` (default HEAD)."""
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

    @staticmethod
    def parent_ids(obj):
        parents = obj.get("parent_context_ids") if obj else None
        if isinstance(parents, list):
            return [p for p in parents if CTX_ID_RE.match(str(p))]
        parent = (obj or {}).get("parent_context_id")
        return [parent] if parent else []

    def ancestors(self, ctx_id, include_self=True):
        """Return ``{ancestor_id: shortest_distance}`` across the context DAG."""
        if not ctx_id or self.load_context(ctx_id) is None:
            return {}
        distances = {}
        queue = [(ctx_id, 0)] if include_self else [
            (p, 1) for p in self.parent_ids(self.load_context(ctx_id))
        ]
        cursor = 0
        while cursor < len(queue):
            current, distance = queue[cursor]
            cursor += 1
            if current in distances and distances[current] <= distance:
                continue
            obj = self.load_context(current)
            if obj is None:
                continue
            distances[current] = distance
            queue.extend((p, distance + 1) for p in self.parent_ids(obj))
        return distances

    def merge_base(self, left, right):
        """Find the nearest common ancestor of two contexts."""
        left_anc = self.ancestors(left)
        right_anc = self.ancestors(right)
        common = set(left_anc) & set(right_anc)
        if not common:
            return None
        return min(
            common,
            key=lambda cid: (
                max(left_anc[cid], right_anc[cid]),
                left_anc[cid] + right_anc[cid],
                -len(self.history(cid)),
                cid,
            ),
        )

    def is_ancestor(self, ancestor, descendant):
        return ancestor in self.ancestors(descendant)

    def all_contexts(self, limit=None):
        objects = [self.load_context(cid) for cid in self.list_context_ids()]
        objects = [obj for obj in objects if obj is not None]
        objects.sort(key=lambda obj: (obj.get("created_at") or "", obj["context_id"]), reverse=True)
        return objects[:limit] if limit else objects

    def decorations(self):
        result = {}
        for branch, ctx_id in self.list_branches().items():
            if ctx_id:
                result.setdefault(ctx_id, []).append(branch)
        return result

    def previous_head(self, before_id=None):
        """The parent of HEAD (or of ``before_id``) — used by checkout -."""
        obj = self.load_context(before_id or self.head())
        return (obj or {}).get("parent_context_id")

    def has_children(self, ctx_id):
        """Whether any stored context already descends directly from ctx_id."""
        if not ctx_id:
            return False
        return any(
            ctx_id in self.parent_ids(self.load_context(candidate))
            for candidate in self.list_context_ids()
        )

    # -- checkout undo (single-level reflog) ---------------------------------

    @property
    def prev_file(self) -> Path:
        return self.dir / "PREV"

    def position(self):
        branch = self.current_branch()
        if branch:
            return "branch:{}".format(branch)
        ctx_id = self.head()
        return "context:{}".format(ctx_id) if ctx_id else None

    def remember_position(self, position):
        """Record where we are before checkout, enabling ``checkout -``."""
        if position:
            write_text(self.prev_file, position + "\n")

    def recalled_position(self):
        """Last remembered position (or None). Does not clear it."""
        try:
            text = self.prev_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("branch:"):
            try:
                return "branch:" + self.validate_branch_name(text[7:])
            except StoreError:
                return None
        m = re.search(r"ctx_[0-9a-f]{8,16}", text or "")
        return "context:" + m.group(0) if m else None

    # -- summary for status/doctor -------------------------------------------------

    def stats(self):
        ids = self.list_context_ids()
        return {
            "exists": self.exists(),
            "context_count": len(ids),
            "head": self.head(),
            "branch": self.current_branch(),
            "branch_count": len(self.list_branches()),
            "oldest": ids[0] if ids else None,
            "newest": ids[-1] if ids else None,
        }

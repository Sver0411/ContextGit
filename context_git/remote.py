"""V4 Remote Context transport and synchronization protocol.

The remote is a content-addressed object store plus movable context refs. It
is deliberately independent from source Git remotes. Two transports are
built in with no runtime dependencies:

* local/file path — useful for shared volumes, Dropbox-style folders, or tests
* HTTPS — GET objects; PUT new objects and the manifest

Objects are portable copies: machine-specific repository root fields are
replaced with ``.`` without changing their Context ids (those fields are not
part of the identity core). Full SHA-256 digests in the remote manifest
protect the intentionally short human-facing Context ids during transport.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from urllib import error, parse, request

from .common import read_json, write_json
from .context import _project_name, verify_context_id
from .gitstate import _run as git_run
from .project import detect as project_detect
from .security import scan_object
from .storage import CTX_ID_RE, Store, StoreError, utc_now_iso

REMOTE_FORMAT = "context-git-remote"
REMOTE_VERSION = "1.0"
MAX_REMOTE_OBJECTS = 10000
MAX_CONTEXT_BYTES = 5 * 1024 * 1024
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RemoteError(Exception):
    """Safe, user-facing remote protocol or transport failure."""


def _canonical_bytes(value):
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def _digest(value):
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def portable_object(obj):
    """Return a deep copy with machine-specific project roots removed."""
    value = copy.deepcopy(obj)
    project = value.get("project")
    if isinstance(project, dict) and project.get("root"):
        project["root"] = "."
    git = value.get("git")
    if isinstance(git, dict) and git.get("repo_root"):
        git["repo_root"] = "."
    return value


def _object_meta(obj):
    payload = _canonical_bytes(obj)
    return {"sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size": len(payload)}


def validate_context_object(obj, expected_id=None, expected_meta=None):
    """Validate identity, lineage, size, digest, and residual secret safety."""
    if not isinstance(obj, dict) or obj.get("protocol") != "UACP":
        raise RemoteError("remote object is not a UACP Context Object")
    ctx_id = obj.get("context_id")
    if not CTX_ID_RE.match(str(ctx_id or "")):
        raise RemoteError("remote object has an invalid context id")
    if expected_id and ctx_id != expected_id:
        raise RemoteError("remote object id does not match its manifest key")
    parents = Store.parent_ids(obj)
    if len(parents) > 2 or len(set(parents)) != len(parents):
        raise RemoteError("{} has invalid parent lineage".format(ctx_id))
    if parents and obj.get("parent_context_id") != parents[0]:
        raise RemoteError("{} first-parent compatibility pointer is invalid".format(ctx_id))
    if not verify_context_id(obj):
        raise RemoteError("{} failed Context id verification".format(ctx_id))
    portable = portable_object(obj)
    meta = _object_meta(portable)
    if meta["size"] > MAX_CONTEXT_BYTES:
        raise RemoteError("{} exceeds the remote object size limit".format(ctx_id))
    if expected_meta:
        if expected_meta.get("sha256") != meta["sha256"]:
            raise RemoteError("{} failed full SHA-256 verification".format(ctx_id))
        if int(expected_meta.get("size") or -1) != meta["size"]:
            raise RemoteError("{} size does not match the manifest".format(ctx_id))
    findings = scan_object(portable)
    if findings:
        raise RemoteError("{} failed the residual secret scan".format(ctx_id))
    return portable, meta


def _normalise_repository_url(value):
    value = str(value or "").strip()
    if not value:
        return None
    if "://" in value:
        parts = parse.urlsplit(value)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        try:
            parsed_port = parts.port
        except ValueError:
            return None
        if scheme == "file":
            try:
                return "file:" + str(Path(parse.unquote(parts.path)).resolve())
            except OSError:
                return "file:" + parts.path
        if not host:
            return None
        if (scheme, parsed_port) in (("ssh", 22), ("https", 443), ("http", 80)):
            parsed_port = None
        port = ":{}".format(parsed_port) if parsed_port else ""
        path = parts.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return "repo://{}{}{}".format(host, port, path)
    # SCP-like Git URL: user@host:owner/repo.git. Drop the user component.
    if ":" in value and not value.startswith(("/", "./", "../")):
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1].lower()
        if path.endswith(".git"):
            path = path[:-4]
        return "repo://{}/{}".format(host, path)
    try:
        return "file:" + str(Path(value).expanduser().resolve())
    except OSError:
        return "file:" + value


def project_descriptor(root, store):
    head = store.head_object() or {}
    root = Path(root).resolve()
    detected_name = _project_name(root, project_detect(root))
    name = ((head.get("project") or {}).get("name") or detected_name
            or root.name or "unknown-project")
    ok, output, _ = git_run(root, ["config", "--get", "remote.origin.url"])
    canonical = _normalise_repository_url(output.strip()) if ok else None
    fingerprint = None
    if canonical:
        fingerprint = "sha256:" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:24]
    return {"name": name, "repository_fingerprint": fingerprint}


def _check_project(local, remote, allow_other_project=False):
    if not remote:
        return
    local_fp = local.get("repository_fingerprint")
    remote_fp = remote.get("repository_fingerprint")
    mismatch = bool(local_fp and remote_fp and local_fp != remote_fp)
    if not local_fp or not remote_fp:
        mismatch = str(local.get("name")) != str(remote.get("name"))
    if mismatch and not allow_other_project:
        raise RemoteError(
            "remote belongs to a different project; use --allow-other-project "
            "only if that is intentional"
        )


def empty_manifest(project):
    return {
        "format": REMOTE_FORMAT,
        "version": REMOTE_VERSION,
        "protocol": "UACP/1.0",
        "updated_at": utc_now_iso(),
        "project": project,
        "refs": {"heads": {}},
        "objects": {},
    }


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise RemoteError("remote manifest is missing or invalid")
    if manifest.get("format") != REMOTE_FORMAT or manifest.get("version") != REMOTE_VERSION:
        raise RemoteError("unsupported remote manifest format/version")
    if manifest.get("protocol") != "UACP/1.0":
        raise RemoteError("unsupported remote Context protocol")
    project = manifest.get("project")
    if (
        not isinstance(project, dict)
        or not isinstance(project.get("name"), str)
        or not project["name"].strip()
        or len(project["name"]) > 200
        or "repository_fingerprint" not in project
    ):
        raise RemoteError("remote manifest project identity is invalid")
    fingerprint = project.get("repository_fingerprint")
    if fingerprint is not None and not re.match(
        r"^sha256:[0-9a-f]{24}$", str(fingerprint)
    ):
        raise RemoteError("remote manifest project identity is invalid")
    refs = manifest.get("refs")
    if not isinstance(refs, dict):
        raise RemoteError("remote manifest refs/objects are invalid")
    objects = manifest.get("objects")
    heads = refs.get("heads")
    if not isinstance(objects, dict) or not isinstance(heads, dict):
        raise RemoteError("remote manifest refs/objects are invalid")
    if len(objects) > MAX_REMOTE_OBJECTS:
        raise RemoteError("remote manifest exceeds the object-count limit")
    for ctx_id, meta in objects.items():
        if not CTX_ID_RE.match(str(ctx_id)) or not isinstance(meta, dict):
            raise RemoteError("remote manifest contains an invalid object entry")
        digest = meta.get("sha256")
        if not re.match(r"^sha256:[0-9a-f]{64}$", str(digest or "")):
            raise RemoteError("remote manifest contains an invalid object digest")
        size = meta.get("size")
        if not isinstance(size, int) or size < 0 or size > MAX_CONTEXT_BYTES:
            raise RemoteError("remote manifest contains an invalid object size")
    for branch, ctx_id in heads.items():
        try:
            Store.validate_branch_name(branch)
        except StoreError as exc:
            raise RemoteError(str(exc))
        if ctx_id not in objects:
            raise RemoteError("remote ref {} points to a missing object".format(branch))
    return manifest


def normalise_remote_url(value, project_root, allow_insecure_http=False):
    """Validate and canonicalise a configured local/file/HTTPS endpoint."""
    value = str(value or "").strip()
    if not value:
        raise RemoteError("remote URL/path cannot be empty")
    parts = parse.urlsplit(value)
    if parts.scheme in ("https", "http"):
        if parts.username or parts.password:
            raise RemoteError("credentials must not be embedded in a remote URL")
        if parts.query or parts.fragment or not parts.hostname:
            raise RemoteError("remote HTTP URL must not contain query/fragment data")
        try:
            parts.port
        except ValueError:
            raise RemoteError("remote HTTP URL has an invalid port")
        if parts.scheme == "http":
            local_hosts = {"localhost", "127.0.0.1", "::1"}
            if not allow_insecure_http or parts.hostname not in local_hosts:
                raise RemoteError("plain HTTP is allowed only for loopback with --allow-insecure-http")
        return value.rstrip("/")
    if parts.scheme not in ("", "file"):
        raise RemoteError("unsupported remote scheme: {}".format(parts.scheme))
    if parts.scheme == "file" and parts.netloc not in ("", "localhost"):
        raise RemoteError("file remotes must not name a network host")
    raw_path = parse.unquote(parts.path) if parts.scheme == "file" else value
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(project_root) / path
    path = path.resolve()
    root = Path(project_root).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        pass
    else:
        raise RemoteError("a file remote must live outside the project directory")
    try:
        root.relative_to(path)
    except ValueError:
        pass
    else:
        raise RemoteError("a file remote must not contain the project directory")
    return path.as_uri()


def make_remote_config(value, project_root, auth_env=None,
                       allow_insecure_http=False):
    if auth_env and not ENV_NAME_RE.match(str(auth_env)):
        raise RemoteError("auth environment variable name is invalid")
    url = normalise_remote_url(value, project_root, allow_insecure_http)
    config = {"url": url}
    if auth_env:
        config["auth_env"] = str(auth_env)
    if parse.urlsplit(url).scheme == "http":
        config["allow_insecure_http"] = True
    return config


def _decode_json(data, limit, label):
    if len(data) > limit:
        raise RemoteError("{} exceeds the size limit".format(label))
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RemoteError("{} is not valid UTF-8 JSON".format(label))
    if not isinstance(value, dict):
        raise RemoteError("{} must be a JSON object".format(label))
    return value


class FileTransport:
    def __init__(self, url):
        path = request.url2pathname(parse.urlsplit(url).path)
        self.root = Path(path).resolve()
        self.manifest_path = self.root / "manifest.json"
        self.contexts_dir = self.root / "contexts"

    def read_manifest(self, required=False):
        if self.manifest_path.is_symlink():
            raise RemoteError("refusing a symlinked remote manifest")
        if not self.manifest_path.is_file():
            if required:
                raise RemoteError("remote has no manifest; push a branch first")
            return None, None
        try:
            data = self.manifest_path.read_bytes()
        except OSError:
            raise RemoteError("remote manifest cannot be read")
        manifest = _decode_json(data, MAX_MANIFEST_BYTES, "remote manifest")
        return validate_manifest(manifest), _digest(manifest)

    def read_object(self, ctx_id):
        path = self.contexts_dir / (ctx_id + ".json")
        if self.contexts_dir.is_symlink() or path.is_symlink():
            raise RemoteError("refusing a symlinked remote object")
        try:
            data = path.read_bytes()
        except OSError:
            raise RemoteError("remote object is missing: {}".format(ctx_id))
        return _decode_json(data, MAX_CONTEXT_BYTES, "remote object {}".format(ctx_id))

    def write(self, objects, manifest, expected_state):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.contexts_dir.is_symlink() or self.manifest_path.is_symlink():
            raise RemoteError("refusing unsafe symlinks in the file remote")
        lock_path = self.root / ".context-git.lock"
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise RemoteError("remote is locked by another writer; retry later")
        try:
            os.write(descriptor, "{} {}\n".format(os.getpid(), utc_now_iso()).encode("ascii"))
            os.close(descriptor)
            current, current_state = self.read_manifest(required=False)
            if current_state != expected_state:
                raise RemoteError("remote changed during push; fetch and retry")
            self.contexts_dir.mkdir(parents=True, exist_ok=True)
            for ctx_id, obj in objects.items():
                target = self.contexts_dir / (ctx_id + ".json")
                if target.is_symlink():
                    raise RemoteError("refusing a symlinked remote object")
                if target.exists():
                    existing = read_json(target)
                    if existing is None or _digest(existing) != _digest(obj):
                        raise RemoteError("remote object collision: {}".format(ctx_id))
                    continue
                write_json(target, obj)
            write_json(self.manifest_path, manifest)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass


class _NoRedirect(request.HTTPRedirectHandler):
    """Prevent bearer credentials from crossing the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self, url, auth_env=None):
        self.base = url.rstrip("/")
        self.auth_env = auth_env
        self.etag = None
        self.manifest_exists = False
        self._opener = request.build_opener(_NoRedirect)

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "context-git/remote"}
        if self.auth_env:
            token = os.environ.get(self.auth_env)
            if not token:
                raise RemoteError("remote credential environment variable is not set")
            headers["Authorization"] = "Bearer " + token
        return headers

    def _request(self, method, path, data=None, headers=None, missing_ok=False):
        merged = self._headers()
        merged.update(headers or {})
        req = request.Request(self.base + path, data=data, headers=merged, method=method)
        try:
            with self._opener.open(req, timeout=20) as response:
                length = response.headers.get("Content-Length")
                limit = MAX_MANIFEST_BYTES if path.endswith("manifest.json") else MAX_CONTEXT_BYTES
                if length and int(length) > limit:
                    raise RemoteError("remote response exceeds the size limit")
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise RemoteError("remote response exceeds the size limit")
                return body, response.headers
        except error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None, exc.headers
            if exc.code in (409, 412):
                raise RemoteError("remote changed during push; fetch and retry")
            raise RemoteError("remote HTTP request failed with status {}".format(exc.code))
        except (error.URLError, OSError, ValueError):
            raise RemoteError("remote HTTPS request failed")

    def read_manifest(self, required=False):
        data, headers = self._request("GET", "/manifest.json", missing_ok=not required)
        if data is None:
            self.manifest_exists = False
            if required:
                raise RemoteError("remote has no manifest; push a branch first")
            self.etag = None
            return None, None
        manifest = validate_manifest(_decode_json(data, MAX_MANIFEST_BYTES, "remote manifest"))
        self.manifest_exists = True
        self.etag = headers.get("ETag")
        return manifest, self.etag or _digest(manifest)

    def read_object(self, ctx_id):
        data, _ = self._request("GET", "/contexts/{}.json".format(ctx_id))
        return _decode_json(data, MAX_CONTEXT_BYTES, "remote object {}".format(ctx_id))

    def write(self, objects, manifest, expected_state):
        for ctx_id, obj in objects.items():
            self._request(
                "PUT", "/contexts/{}.json".format(ctx_id),
                data=_canonical_bytes(obj), headers={"Content-Type": "application/json"},
            )
        headers = {"Content-Type": "application/json"}
        if self.etag:
            headers["If-Match"] = self.etag
        elif self.manifest_exists:
            raise RemoteError("HTTPS remote must provide an ETag for safe updates")
        elif expected_state is None:
            headers["If-None-Match"] = "*"
        self._request("PUT", "/manifest.json", data=_canonical_bytes(manifest), headers=headers)


def create_transport(remote_config, project_root):
    url = remote_config.get("url") if isinstance(remote_config, dict) else None
    if not url:
        raise RemoteError("remote configuration has no URL")
    url = normalise_remote_url(
        url, project_root,
        allow_insecure_http=bool(remote_config.get("allow_insecure_http")),
    )
    scheme = parse.urlsplit(url).scheme
    if scheme == "file":
        return FileTransport(url)
    return HttpTransport(url, auth_env=remote_config.get("auth_env"))


def _reachable(store, tip):
    objects = {}
    queue = [tip]
    while queue:
        ctx_id = queue.pop()
        if ctx_id in objects:
            continue
        obj = store.load_context(ctx_id)
        if obj is None:
            raise RemoteError("local history is missing {}".format(ctx_id))
        portable, _ = validate_context_object(obj, expected_id=ctx_id)
        objects[ctx_id] = portable
        queue.extend(Store.parent_ids(obj))
        if len(objects) > MAX_REMOTE_OBJECTS:
            raise RemoteError("local history exceeds the remote object-count limit")
    return objects


def _is_ancestor(ancestor, descendant, objects):
    seen = set()
    queue = [descendant]
    while queue:
        current = queue.pop()
        if current == ancestor:
            return True
        if current in seen:
            continue
        seen.add(current)
        obj = objects.get(current)
        if obj:
            queue.extend(Store.parent_ids(obj))
    return False


def _download_manifest_objects(store, transport, manifest):
    """Validate every advertised object before refs are trusted or updated."""
    downloaded = {}
    for ctx_id, meta in manifest["objects"].items():
        local = store.load_context(ctx_id)
        if local is not None:
            portable, _ = validate_context_object(local, ctx_id, meta)
            downloaded[ctx_id] = portable
            continue
        remote_obj = transport.read_object(ctx_id)
        portable, _ = validate_context_object(remote_obj, ctx_id, meta)
        downloaded[ctx_id] = portable

    advertised = set(manifest["objects"])
    for ctx_id, obj in downloaded.items():
        for parent in Store.parent_ids(obj):
            if parent not in advertised and store.load_context(parent) is None:
                raise RemoteError("remote history for {} is incomplete".format(ctx_id))
    return downloaded


def push(store, remote_name, branch, transport, force=False, dry_run=False,
         allow_other_project=False):
    branches = store.list_branches()
    if branch not in branches:
        raise RemoteError("unknown local context branch: {}".format(branch))
    tip = branches[branch]
    if not tip:
        raise RemoteError("local context branch has no context: {}".format(branch))
    local_project = project_descriptor(store.root, store)
    local_objects = _reachable(store, tip)
    remote_manifest, remote_state = transport.read_manifest(required=False)
    if remote_manifest is None:
        remote_manifest = empty_manifest(local_project)
        remote_objects = {}
    else:
        _check_project(local_project, remote_manifest.get("project"), allow_other_project)
        remote_objects = _download_manifest_objects(store, transport, remote_manifest)

    remote_tip = ((remote_manifest.get("refs") or {}).get("heads") or {}).get(branch)
    graph = dict(remote_objects)
    graph.update(local_objects)
    if remote_tip and not _is_ancestor(remote_tip, tip, graph) and not force:
        raise RemoteError(
            "push would be non-fast-forward; fetch and merge remote:{}/{} first, "
            "or use --force intentionally".format(remote_name, branch)
        )

    manifest = copy.deepcopy(remote_manifest)
    manifest["updated_at"] = utc_now_iso()
    manifest["project"] = remote_manifest.get("project") or local_project
    manifest.setdefault("refs", {}).setdefault("heads", {})[branch] = tip
    manifest.setdefault("objects", {})
    upload = {}
    for ctx_id, obj in local_objects.items():
        meta = _object_meta(obj)
        existing = manifest["objects"].get(ctx_id)
        if existing and existing != meta:
            raise RemoteError("remote object collision: {}".format(ctx_id))
        manifest["objects"][ctx_id] = meta
        if not existing:
            upload[ctx_id] = obj

    result = {
        "action": "push", "remote": remote_name, "branch": branch,
        "context_id": tip, "objects_total": len(local_objects),
        "objects_uploaded": len(upload), "forced": bool(force),
        "dry_run": bool(dry_run),
    }
    if not dry_run:
        transport.write(upload, manifest, remote_state)
        store.set_remote_ref(remote_name, branch, tip)
    return result


def fetch(store, remote_name, transport, branch=None, allow_other_project=False):
    manifest, _ = transport.read_manifest(required=True)
    local_project = project_descriptor(store.root, store)
    _check_project(local_project, manifest.get("project"), allow_other_project)
    objects = _download_manifest_objects(store, transport, manifest)
    added = 0
    for ctx_id, obj in objects.items():
        if store.load_context(ctx_id) is None:
            store.save_context(obj)
            added += 1
    heads = manifest["refs"]["heads"]
    if branch:
        Store.validate_branch_name(branch)
        if branch not in heads:
            raise RemoteError("remote has no context branch: {}".format(branch))
        heads = {branch: heads[branch]}
    for name, ctx_id in heads.items():
        store.set_remote_ref(remote_name, name, ctx_id)
    return {
        "action": "fetch", "remote": remote_name,
        "objects_received": added, "objects_verified": len(objects),
        "refs": dict(heads),
    }


def pull(store, remote_name, branch, transport, allow_other_project=False):
    result = fetch(
        store, remote_name, transport, branch=branch,
        allow_other_project=allow_other_project,
    )
    if store.current_branch() != branch:
        raise RemoteError("pull branch must be the current context branch")
    local_tip = store.head()
    remote_tip = result["refs"][branch]
    if local_tip is None:
        store.set_head(remote_tip)
        action = "fast-forward"
    elif store.is_ancestor(remote_tip, local_tip):
        action = "up-to-date"
    elif store.is_ancestor(local_tip, remote_tip):
        store.set_head(remote_tip)
        action = "fast-forward"
    else:
        raise RemoteError(
            "local and remote contexts diverged; merge remote:{}/{} explicitly"
            .format(remote_name, branch)
        )
    result.update({"action": action, "from": local_tip, "to": store.head(),
                   "branch": branch})
    return result

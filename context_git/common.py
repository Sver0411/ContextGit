"""common — shared primitives for context-git.

Owns the pieces every module agrees on:

* what the tool refuses to walk into (``node_modules``, ``.git``, ...)
* which paths are categorically forbidden to read or record
* how a file gets fingerprinted cheaply (bounded, streaming SHA-256)
* atomic JSON/text IO and path normalisation

Keeping these in one place is what makes "the same rules apply everywhere"
true rather than aspirational.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Ignore rules
# --------------------------------------------------------------------------

# Directory names we never descend into: dependency trees, build outputs,
# VCS internals. Walking them is pure cost and zero signal.
IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".bzr",
        "node_modules", "bower_components", "vendor",
        "dist", "build", "out", "target", "bin", "obj",
        ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
        ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
        "coverage", ".coverage", "htmlcov", "__pycache__",
        ".venv", "venv", "env", "site-packages",
        "Pods", "DerivedData",
        ".idea", ".vscode-test", ".gradle", ".m2",
        ".terraform", ".serverless",
        ".context-git",  # never version the versioning tool's own store
    }
)

# Path fragments that must never be read, recorded, or hashed.
FORBIDDEN_PATH_NAMES = frozenset(
    {
        ".env",
        ".env.local", ".env.development", ".env.production", ".env.test",
        ".env.staging",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
        "credentials", "credentials.json", ".netrc", ".npmrc", ".pypirc",
        ".htpasswd",
        "secrets.yml", "secrets.yaml", "secrets.json",
        "terraform.tfstate", "kubeconfig", ".dockercfg", ".git-credentials",
    }
)

FORBIDDEN_SUFFIXES = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk", ".pgp"}
)

# Directories whose *contents* are off-limits even though the names look benign.
FORBIDDEN_DIR_MARKERS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})

# Editor/backup/scratch copies of a secret are still secrets. Matching is
# suffix-first so "monkey.bak" is not swept up by a bare "key" marker.
SECRET_BACKUP_SUFFIXES = frozenset({
    ".bak", ".old", ".orig", ".copy", ".swp", ".swo", ".tmp", ".temp",
    ".backup", ".save", "~",
})

SECRET_NAME_MARKERS = (
    ".env", "secret", "credential", "password", "passwd", "token",
    "apikey", "api_key", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "privatekey", "private_key",
)

# Case-insensitive comparisons are precomputed once. Every platform runs the
# same rules: a case-sensitive check on POSIX would silently stop protecting
# the moment the same tree is read on a case-preserving/case-insensitive
# filesystem (macOS default, Windows), so the rule is normalised everywhere
# rather than special-cased per platform.
_CASEFOLDED_IGNORED_DIRS = frozenset(name.casefold() for name in IGNORED_DIRS)
_CASEFOLDED_PATH_NAMES = frozenset(name.casefold() for name in FORBIDDEN_PATH_NAMES)
_CASEFOLDED_SUFFIXES = tuple(sorted(suffix.casefold() for suffix in FORBIDDEN_SUFFIXES))
_CASEFOLDED_DIR_MARKERS = frozenset(marker.casefold() for marker in FORBIDDEN_DIR_MARKERS)


def normalise_path_component(name) -> str:
    """Casefolded, trailing-dot/space-trimmed form used for all path matching.

    Stripping trailing dots and spaces is a Windows filesystem semantic
    (``.env.`` and ``.env`` name the same file there) applied on every
    platform so the security rule cannot be defeated by moving a repository
    between filesystems. On POSIX such names are merely unusual.
    """
    return str(name).casefold().rstrip(" .")


def is_ignored_dir(name: str) -> bool:
    """True if a directory with this basename should never be walked."""
    return normalise_path_component(name) in _CASEFOLDED_IGNORED_DIRS


def is_forbidden_path(path) -> bool:
    """True if this path is categorically sensitive and must not be read.

    Consulted before any read, hash, or inclusion. Deliberately conservative:
    a false positive costs one file in the report; a false negative costs
    the user a leaked credential.

    Matching is case-insensitive and trailing-dot/space-insensitive on every
    platform, so ``.ENV``, ``.SSH/config``, ``ID_RSA``, ``SECRET.PEM`` and
    ``TOKEN.BAK`` are all refused exactly like their lowercase spellings.
    """
    if path is None:
        return False
    p = Path(str(path))
    for part in p.parts:
        if normalise_path_component(part) in _CASEFOLDED_DIR_MARKERS:
            return True
    name = normalise_path_component(p.name)
    if not name:
        return False
    if name in _CASEFOLDED_PATH_NAMES:
        return True
    if name.startswith(".env"):
        return True
    if name.endswith(_CASEFOLDED_SUFFIXES):
        return True
    if name.endswith(tuple(SECRET_BACKUP_SUFFIXES)):
        return any(marker in name for marker in SECRET_NAME_MARKERS)
    return False


def is_probably_binary(path, sniff: int = 4096) -> bool:
    """Cheap binary sniff — never read a whole file to decide this."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sniff)
    except (OSError, IOError):
        return True
    return b"\x00" in chunk


def iter_files(root, limit=None):
    """Yield relative POSIX paths of candidate source files under ``root``.

    Skips ignored directories, dotfiles (except a few signal carriers),
    symlinks (they can escape the repo and loop), and huge files.
    """
    root = Path(root).resolve()
    count = 0
    allowed_dotfiles = {".gitignore", ".editorconfig", ".nvmrc", ".python-version"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if not is_ignored_dir(d) and not d.startswith(".")
        )
        for fname in sorted(filenames):
            if fname.startswith(".") and fname not in allowed_dotfiles:
                continue
            full = Path(dirpath) / fname
            if full.is_symlink():
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            if not full.is_file() or st.st_size > 2 * 1024 * 1024:
                continue
            yield full.relative_to(root).as_posix()
            count += 1
            if limit is not None and count >= limit:
                return


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

FINGERPRINT_MAX_BYTES = 5 * 1024 * 1024


def fingerprint(path) -> "str | None":
    """Streaming SHA-256 for files up to 5 MiB, including the file size.

    Large files and symlinks are skipped. Refusing symlinks is a security
    boundary: a repository path must never make us read outside the repo.
    Never touches forbidden paths.
    Returns ``None`` for missing/unreadable/forbidden files rather than raising.
    """
    p = Path(path)
    if is_forbidden_path(p) or p.is_symlink():
        return None
    try:
        if not p.is_file():
            return None
        size = p.stat().st_size
        if size > FINGERPRINT_MAX_BYTES:
            return None
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        h.update("\x00size={}".format(size).encode("utf-8"))
        return "sha256:" + h.hexdigest()[:32]
    except (OSError, IOError):
        return None


# --------------------------------------------------------------------------
# IO helpers
# --------------------------------------------------------------------------

def read_json(path) -> "dict | None":
    """Read JSON, tolerating corruption by returning None instead of raising."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, IOError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, p)


def write_text(path, text) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, p)


def rel_posix(root, path) -> str:
    """POSIX-style relative path, safe on Windows, never escaping root."""
    root = Path(root).resolve()
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(str(p)))
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p).replace("\\", "/")


def eprint(*args) -> None:
    print(*args, file=sys.stderr)

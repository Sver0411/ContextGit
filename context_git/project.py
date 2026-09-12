"""project — infer the project's language, stack and commands.

Governing rule: **evidence or silence.** Every field is backed by a file we
actually read. When evidence is thin (a bare ``package.json`` with no
scripts, a lone ``.py`` file) we say ``unknown`` rather than guess. A
downstream agent acting on an invented build command is worse than one
that knows to look around.

Manifests are read as text with a bounded size. We never execute anything,
never install anything, never touch the network.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .common import is_forbidden_path, is_ignored_dir

MANIFEST_RULES = [
    ("package.json", "node", "npm"),
    ("pnpm-lock.yaml", "node", None),
    ("yarn.lock", "node", None),
    ("bun.lockb", "node", None),
    ("pyproject.toml", "python", "pip"),
    ("requirements.txt", "python", "pip"),
    ("setup.py", "python", "pip"),
    ("Pipfile", "python", "pipenv"),
    ("poetry.lock", "python", "poetry"),
    ("uv.lock", "python", "uv"),
    ("Cargo.toml", "rust", "cargo"),
    ("go.mod", "go", "go"),
    ("pom.xml", "java", "maven"),
    ("build.gradle", "java", "gradle"),
    ("build.gradle.kts", "kotlin", "gradle"),
    ("composer.json", "php", "composer"),
    ("Gemfile", "ruby", "bundler"),
    ("pubspec.yaml", "dart", "pub"),
    ("mix.exs", "elixir", "mix"),
    ("Package.swift", "swift", "swiftpm"),
]

LOCKFILE_PACKAGE_MANAGER = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "package-lock.json": "npm",
    "bun.lockb": "bun",
    "poetry.lock": "poetry",
    "uv.lock": "uv",
    "Pipfile.lock": "pipenv",
    "Gemfile.lock": "bundler",
    "Cargo.lock": "cargo",
    "go.sum": "go",
    "composer.lock": "composer",
    "pdm.lock": "pdm",
}

MAX_MANIFEST_BYTES = 512 * 1024

# Framework fingerprints: (file, substring-in-file, framework-name)
FRAMEWORK_HINTS = [
    ("package.json", '"next"', "Next.js"),
    ("package.json", '"react"', "React"),
    ("package.json", '"vue"', "Vue"),
    ("package.json", '"svelte"', "Svelte"),
    ("package.json", '"@angular/core"', "Angular"),
    ("package.json", '"express"', "Express"),
    ("package.json", '"fastify"', "Fastify"),
    ("package.json", '"electron"', "Electron"),
    ("package.json", '"vitest"', "Vitest"),
    ("package.json", '"jest"', "Jest"),
    ("package.json", '"playwright"', "Playwright"),
    ("package.json", '"tailwindcss"', "Tailwind CSS"),
    ("pyproject.toml", "fastapi", "FastAPI"),
    ("pyproject.toml", "django", "Django"),
    ("pyproject.toml", "flask", "Flask"),
    ("pyproject.toml", "pytest", "pytest"),
    ("requirements.txt", "fastapi", "FastAPI"),
    ("requirements.txt", "django", "Django"),
    ("requirements.txt", "flask", "Flask"),
    ("requirements.txt", "pytest", "pytest"),
    ("go.mod", "gin-gonic", "Gin"),
    ("Cargo.toml", "axum", "Axum"),
    ("Cargo.toml", "tokio", "Tokio"),
]

RUNTIME_VERSION_SOURCES = [
    ".nvmrc", ".node-version", ".python-version", ".ruby-version",
    ".tool-versions", ".go-version",
]


def _read_text(path, max_bytes=MAX_MANIFEST_BYTES):
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path):
    raw = _read_text(path)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_info(root):
    """(package_manager, scripts) for a Node project."""
    pkg = _read_json(root / "package.json")
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}

    pm = None
    for lock, manager in LOCKFILE_PACKAGE_MANAGER.items():
        if (root / lock).is_file():
            pm = manager
            break
    if pm is None and (root / "package.json").is_file():
        pm = "npm"  # package.json with no lockfile: npm is the safe default
    declared = pkg.get("packageManager")
    if pm and declared and "@" in str(declared):
        pass  # lockfile evidence wins
    elif declared and not pm:
        pm = str(declared).split("@")[0]
    return pm, scripts


def _commands_from_scripts(scripts, pm):
    """Derive commands only from scripts that actually exist."""
    cmds = {"build": None, "test": None, "lint": None, "typecheck": None, "dev": None}
    if not scripts:
        return cmds
    run = "{} run".format(pm or "npm")

    def pick(*names):
        for n in names:
            if n in scripts:
                return "{} {}".format(run, n)
        return None

    cmds["build"] = pick("build", "compile", "dist")
    cmds["test"] = pick("test", "tests", "test:unit", "jest", "vitest")
    cmds["lint"] = pick("lint", "eslint", "lint:js", "check")
    cmds["typecheck"] = pick("typecheck", "type-check", "tsc", "types")
    cmds["dev"] = pick("dev", "start", "serve")
    return cmds


def _python_commands(root, has_tests_dir):
    cmds = {"build": None, "test": None, "lint": None, "typecheck": None, "dev": None}
    pyproject = _read_text(root / "pyproject.toml")
    requirements = _read_text(root / "requirements.txt")
    blob = (pyproject + "\n" + requirements).lower()

    if "pytest" in blob or has_tests_dir or (root / "pytest.ini").is_file():
        cmds["test"] = "python -m pytest"
    if "ruff" in blob or (root / "ruff.toml").is_file():
        cmds["lint"] = "ruff check ."
    elif "flake8" in blob or (root / ".flake8").is_file():
        cmds["lint"] = "flake8 ."
    if "mypy" in blob or (root / "mypy.ini").is_file():
        cmds["typecheck"] = "mypy ."
    elif "pyright" in blob:
        cmds["typecheck"] = "pyright"
    if "poetry" in blob and (root / "poetry.lock").is_file():
        cmds["build"] = "poetry build"
    elif (root / "pyproject.toml").is_file() and "[build-system]" in pyproject:
        cmds["build"] = "python -m build"
    return cmds


def _platform_guess(files):
    platforms = []
    if {"Dockerfile", "docker-compose.yml", "compose.yaml"} & files:
        platforms.append("docker")
    if any(f.startswith(".github/workflows/") for f in files):
        platforms.append("github-actions")
    if ".gitlab-ci.yml" in files:
        platforms.append("gitlab-ci")
    if {"vercel.json", "next.config.js", "next.config.mjs", "next.config.ts"} & files:
        platforms.append("vercel")
    if "netlify.toml" in files:
        platforms.append("netlify")
    if "fly.toml" in files:
        platforms.append("fly.io")
    if "Makefile" in files:
        platforms.append("make")
    return platforms


def detect(root="."):
    """Return a detection report. Never raises; unknown is spelled 'unknown'."""
    root = Path(root).resolve()
    files = set()
    try:
        # Actually prune ignored trees while walking. Filtering only after a
        # recursive glob has already paid the cost of traversing node_modules,
        # .git, build output, etc. and violates the large-repo contract.
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            rel_dir = current.relative_to(root)
            depth = len(rel_dir.parts)
            if depth >= 2:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not is_ignored_dir(d)
                    and (not d.startswith(".") or d == ".github")
                    and not (current / d).is_symlink()
                )
            for name in sorted(filenames):
                p = current / name
                if p.is_symlink() or is_forbidden_path(p):
                    continue
                rel = p.relative_to(root).as_posix()
                if rel.count("/") <= 2:
                    files.add(rel)
    except OSError:
        pass

    root_files = {f for f in files if "/" not in f}

    languages, manifest_paths = [], []
    for manifest, lang, _ in MANIFEST_RULES:
        if manifest in root_files:
            manifest_paths.append(manifest)
            if lang not in languages:
                languages.append(lang)

    if not languages:
        # Weak fallback: extension evidence only — marked as such.
        counts = {}
        ext_map = {
            ".py": "python", ".js": "node", ".ts": "node", ".tsx": "node",
            ".jsx": "node", ".rs": "rust", ".go": "go", ".java": "java",
            ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
            ".cs": "csharp", ".cpp": "cpp", ".c": "c",
        }
        try:
            for p in root.iterdir():
                if p.is_file() and p.suffix in ext_map:
                    counts[ext_map[p.suffix]] = counts.get(ext_map[p.suffix], 0) + 1
        except OSError:
            pass
        if counts:
            languages = [max(counts, key=counts.get)]
            evidence_strength = "weak"
        else:
            evidence_strength = "none"
    else:
        evidence_strength = "strong"

    primary = languages[0] if languages else "unknown"

    # package manager ----------------------------------------------------------
    package_manager = None
    for lock, manager in LOCKFILE_PACKAGE_MANAGER.items():
        if lock in root_files:
            package_manager = manager
            break
    if package_manager is None and "package.json" in root_files:
        package_manager = "npm"
    if package_manager is None and primary == "rust":
        package_manager = "cargo"
    if package_manager is None and primary == "go":
        package_manager = "go"
    if package_manager is None and primary == "python":
        for cand in ("uv.lock", "poetry.lock", "Pipfile.lock"):
            if cand in root_files:
                package_manager = LOCKFILE_PACKAGE_MANAGER[cand]
                break
        if package_manager is None and any(
            m in root_files for m in ("pyproject.toml", "requirements.txt", "setup.py")
        ):
            package_manager = "pip"

    # commands -------------------------------------------------------------------
    commands = {"build": None, "test": None, "lint": None, "typecheck": None, "dev": None}
    if "package.json" in root_files:
        pm, scripts = _node_info(root)
        package_manager = package_manager or pm
        commands = _commands_from_scripts(scripts, package_manager)
    elif primary == "python":
        has_tests = "tests" in {f.split("/")[0] for f in files if "/" in f}
        commands = _python_commands(root, has_tests)
    elif primary == "rust":
        commands = {"build": "cargo build", "test": "cargo test",
                    "lint": "cargo clippy", "typecheck": "cargo check", "dev": None}
    elif primary == "go":
        commands = {"build": "go build ./...", "test": "go test ./...",
                    "lint": "go vet ./...", "typecheck": None, "dev": None}

    # frameworks ------------------------------------------------------------------
    frameworks = []
    for manifest, needle, framework in FRAMEWORK_HINTS:
        if manifest in root_files and needle in _read_text(root / manifest):
            if framework not in frameworks:
                frameworks.append(framework)

    # runtime versions ---------------------------------------------------------------
    runtime = {}
    for src in RUNTIME_VERSION_SOURCES:
        if src in root_files:
            text = _read_text(root / src, 4096).strip()
            if text:
                runtime[src] = text.splitlines()[0][:64]

    # monorepo signals ----------------------------------------------------------------
    workspaces = []
    pkg = _read_json(root / "package.json")
    if isinstance(pkg.get("workspaces"), (list, dict)):
        ws = pkg["workspaces"]
        workspaces = ws if isinstance(ws, list) else ws.get("packages", [])
    if (root / "pnpm-workspace.yaml").is_file():
        workspaces = workspaces or ["(pnpm-workspace.yaml)"]
    if (root / "lerna.json").is_file():
        workspaces = workspaces or ["(lerna)"]
    manifest_nested = sum(
        1 for f in files
        if 0 < f.count("/") <= 1 and f.split("/")[-1] in
        {"package.json", "Cargo.toml", "go.mod", "pyproject.toml"}
    )
    is_monorepo = bool(workspaces) or manifest_nested > 2

    return {
        "primary_language": primary,
        "languages": languages,
        "frameworks": frameworks,
        "package_manager": package_manager or "unknown",
        "commands": commands,
        "manifests": manifest_paths,
        "runtime_versions": runtime,
        "platforms": _platform_guess(files),
        "is_monorepo": is_monorepo,
        "workspaces": [str(w) for w in workspaces][:20],
        "has_tests": bool(
            commands.get("test")
            or any(f.startswith("tests/") or f.startswith("test/") for f in files)
        ),
        "evidence": {
            "strength": evidence_strength,
            "based_on": manifest_paths or ["file-extensions-scan"],
        },
    }


def environment_block(detection, git_snap):
    """Environment block for a Context Object — safe, non-sensitive fields."""
    import platform
    import sys

    return {
        "os": platform.system() or "unknown",
        "os_release": platform.release() or "unknown",
        "arch": platform.machine() or "unknown",
        "python": "{}.{}.{}".format(
            sys.version_info.major, sys.version_info.minor, sys.version_info.micro
        ),
        "project_runtime": detection.get("runtime_versions", {}),
        "package_manager": detection.get("package_manager", "unknown"),
        "is_git_repo": bool(git_snap.get("available")),
        "is_monorepo": bool(detection.get("is_monorepo")),
        "source_type": "observed",
    }

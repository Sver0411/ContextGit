<div align="center">

# Context Git

**Git for AI Agent Context.**

Most handoff tools generate a document.<br>
Context Git versions the working state itself.

Snapshot · Diff · Detect drift · Import sessions · Resume anywhere.

```
Codex
  ↓ ctx_001 → ctx_002 → ctx_003   (context commits)
  ↓
Context Diff  (what actually changed cognitively)
  ↓
Claude Code / OpenCode / Cursor / Gemini / another Codex
```

[![protocol](https://img.shields.io/badge/protocol-UACP%2F1.0-blue)](references/protocol.md)
[![tests](https://github.com/Sver0411/ContextGit/actions/workflows/tests.yml/badge.svg)](https://github.com/Sver0411/ContextGit/actions/workflows/tests.yml)
[![deps](https://img.shields.io/badge/dependencies-std--lib%20only-green)]()
[![python](https://img.shields.io/badge/python-3.8%2B-informational)]()
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

## What is Context Git?

AI agents lose everything between sessions. Existing "handoff" tools answer
that by generating a `HANDOFF.md` — every single time, from scratch, full
price in tokens, with no way to know if yesterday's claims are still true.

Context Git treats agent context like source code:

| Git concept | Context Git concept |
|---|---|
| commit | **Context Commit** — a meaningful cognitive state change ("auth middleware implemented") |
| HEAD | **Context HEAD** — pointer to the newest context in `.context-git/` |
| diff | **Semantic Context Diff** — progress moved, decisions made, issues resolved — not JSON field noise |
| log | **Context Log** — the chain of working states |
| checkout | **Context Checkout** — inspect any past state (never touches your git repo) |
| status | **Drift Detection** — is the context still true against the live repo? |

And it is honest about evidence: machine-observed facts (`git HEAD`,
file fingerprints) are marked `observed`; agent claims (goals, decisions)
are marked `agent` — so the next agent knows what to re-verify instead of
trusting a document blindly.

## Why not just write a HANDOFF.md?

| | One-shot HANDOFF.md | Context Git |
|---|---|---|
| Cost per handoff | full context every time | first time full, then a diff |
| Staleness | unknowable | graded drift (NONE/LOW/MEDIUM/HIGH) with per-file validity |
| Validation claims | "tests pass" (maybe) | freshness-checked: code changed after the test → `STALE` |
| History | overwritten | immutable context chain, checkout any point |
| Cross-agent | prose conventions | typed objects + capability compatibility report |
| Secrets | hope the prompt was careful | redaction before write + independent residual scan |

## Install

```bash
# no install needed — run from the repo
python scripts/context_git.py --help

# or install as a CLI
pip install .
context-git --help
```

Requirements: Python 3.8+, standard library only. Windows / macOS / Linux.

## Usage

```bash
context-git init                  # create .context-git/
context-git snapshot              # capture the first context (ctx_001)
# ... work happens ...
context-git status                # is my context still true? → drift level
context-git commit -m "Auth middleware implemented" \
  --set completed="Auth middleware" \
  --set in_progress="Refresh token rotation" \
  --set validation.test=pass
context-git diff                  # semantic diff: HEAD vs parent
context-git log                   # the context chain
context-git resume                # the agent-ready briefing (drift + capabilities + next step)
context-git checkout ctx_001      # move context HEAD back in time (git repo untouched)
context-git verify                # residual secret scan over the store
context-git capabilities          # probe capabilities available in this environment

# V2: private session access is always explicit
context-git sessions --agent codex
context-git import-session /path/to/session.jsonl --dry-run
context-git import-session /path/to/session.jsonl --agent codex
context-git import-session --latest --agent claude-code
```

Agents pass `--no-prompt` and fill fields with `--set KEY=VALUE`
(repeatable; list fields accept commas; decisions accept JSON; validation
uses `validation.test=pass` form). Fields you don't restate are inherited
from the parent context — commits are incremental, like good commit
messages, not like re-writing the whole README.

## V2 — private session import

V2 can compress an existing Codex, Claude Code, Cursor, Gemini CLI,
OpenCode, or generic JSON/JSONL/text session into a Context Object. Current
OpenCode SQLite stores are queried read-only:

```bash
# Preview first. The preview is redacted and writes nothing.
context-git import-session ~/.codex/sessions/.../rollout.jsonl --dry-run

# Create the first context, or an incremental commit when HEAD exists.
context-git import-session ~/.codex/sessions/.../rollout.jsonl \
  --set current_objective="Implement token rotation"

# OpenCode: selects the latest session for this project from the DB
context-git import-session ~/.local/share/opencode/opencode.db --agent opencode
```

This is deliberately **opt-in**. Ordinary commands never inspect session
directories. `sessions` and `--latest` only return sessions whose embedded
working directory matches the current project; an explicit file from a
different project is refused unless `--allow-other-project` is supplied.

The importer keeps visible user/assistant outcomes only, extracts compact
fields such as goal, current objective, completed work, issues, decisions,
constraints and next steps, then runs the normal secret guards. It never
stores raw messages, system/developer instructions, tool calls, tool results,
thinking/reasoning blocks, source paths, or the original transcript. Import
provenance records a truncated source hash and confirms each excluded class.

Supported formats and discovery paths live in adapter JSON files, so adding
an agent does not require changing the ingestion core.

| Agent | Project-scoped discovery | Input |
|---|---|---|
| Codex | embedded `cwd` | rollout JSONL (`response_item` and `event_msg`) |
| Claude Code | embedded `cwd` | project session JSONL |
| Cursor | workspace path encoded in directory | nested agent-transcript JSONL or text export |
| Gemini CLI | SHA-256 project directory | current JSONL and legacy JSON sessions |
| OpenCode | `session.directory` | current SQLite `message`/`part` store, opened read-only |
| Generic | explicit file only | role/content JSON, JSONL, Markdown, or text |

## V2 — capability auto-profiling

Static adapter declarations are now combined with conservative read-only
probes:

```text
$ context-git capabilities
Agent Capability Profile
  filesystem         available   project-root-readable
  git                available   executable:git
  browser            declared    adapter-declaration
  node               unavailable executable:node
```

Filesystem access and local `shell`, `git`, `python`, and `node` runtimes can
be observed without executing project code. Browser, GUI, network,
image-generation, and subagent facilities cannot be verified portably, so
they remain explicitly marked `declared` or `unknown`. Resume compatibility
reports distinguish verified capabilities from declaration-only claims.

## What a handoff looks like

```text
$ context-git diff
Context Diff
  ctx_4b4ffecb → ctx_a909f9ac

Progress
  ~ Refresh token rotation (in_progress → completed)
  + Docs

Known Issues
  - Safari SameSite cookie issue [RESOLVED]
```

```text
$ context-git resume
Drift: MEDIUM
  [head-moved] 2 commit(s) since context snapshot
  [important-files-changed] 1 important file(s) changed since snapshot
      - src/auth.ts
  Validation freshness: STALE
  → Re-read: src/auth.ts
Resume Context
========================================
Project:           demo-api
Goal:              Build a demo API
Last Context:      ctx_a909f9ac
Drift:             MEDIUM
Completed:
- Auth middleware  `[agent]`
...
Recommended Next Step:
  → Implement refresh token rotation endpoint
```

More real output in [`examples/`](examples/).

## Protocol

Context Objects speak **UACP/1.0** (Universal Agent Context Protocol):

- [`references/protocol.md`](references/protocol.md) — object model, history,
  semantic diff, drift grades, conformance
- [`schemas/uacp-1.0.schema.json`](schemas/uacp-1.0.schema.json) — normative JSON Schema
- [`references/schema.md`](references/schema.md) — field-by-field reference
- [`references/security.md`](references/security.md) — threat model and guarantees
- [`references/adapters.md`](references/adapters.md) — V2 session formats and discovery boundaries
- [`CHANGELOG.md`](CHANGELOG.md) — release-by-release changes

Any tool that reads JSON can consume a context; the `protocol: "UACP"`
field is the discriminator.

## Security

Contexts are designed to be commit-safe and share-safe:

1. **Never read**: `.env*`, `id_rsa`/`*.pem`/`*.key`, credential stores,
   `.ssh/.aws/.kube` contents — excluded before any file access.
2. **Redact before write**: `sk-…`, `ghp_…`, `AKIA…`, JWTs, Bearer headers,
   `password=…` → `[REDACTED:kind]`; failures fail **closed**.
3. **Independent residual scan** aborts a snapshot if a secret still looks
   present. `context-git verify` re-scans anytime.

No chat history, no chain-of-thought, no full diffs — by protocol design,
not by convention.

## File structure

```
context-git/
├── SKILL.md                # agent integration (triggers, workflows, rules)
├── README.md
├── LICENSE
├── pyproject.toml
├── scripts/
│   └── context_git.py      # zero-install entry point
├── context_git/            # the package
│   ├── cli.py              # argparse CLI
│   ├── context.py          # Context Object builder + context ids
│   ├── storage.py          # .context-git/ store (HEAD, contexts/)
│   ├── diff.py             # semantic diff engine
│   ├── drift.py            # drift detection + validity + freshness
│   ├── gitstate.py         # read-only git snapshots
│   ├── project.py          # stack detection (evidence or "unknown")
│   ├── security.py         # secret guard (redact + residual scan)
│   ├── capabilities.py     # agent adapters + compatibility
│   ├── sessions.py         # explicit, compressed private-session ingestion
│   ├── render.py           # HANDOFF.md / show / log / resume views
│   ├── common.py           # ignore rules, fingerprints, safe IO
│   └── adapters/           # codex / claude-code / opencode / cursor / gemini / generic
├── schemas/uacp-1.0.schema.json
├── references/             # protocol.md, schema.md, security.md
├── examples/               # real generated artifacts
└── tests/                  # stdlib unittest suite
```

## Compatibility

| Agent | Role | Notes |
|---|---|---|
| OpenAI Codex | source & target | adapter bundled |
| Claude Code | source & target | adapter bundled |
| OpenCode | source & target | adapter bundled; read-only SQLite import |
| Cursor | source & target | workspace-scoped transcript import |
| Gemini CLI | source & target | project-hash-scoped session import |
| Any agent | both | contexts are plain JSON + Markdown; `generic` adapter is the floor |

Store location is a plain directory: sync it, commit it, or copy it —
no server, no lock-in.

## Known limitations (v2)

- Linear history only (branch/merge is schema-ready but not implemented).
- Semantic diff is structural — deterministic, LLM-free; it matches reworded
  items by normalisation, not by meaning.
- Drift reads the working tree of one machine; contexts are not remotely
  syncable yet (copy the directory and re-run `status`).
- Agent detection uses environment markers and can fall back to `generic`.
- Session extraction is deterministic and LLM-free. Unstructured conversation
  becomes only goal/current objective; structured headings and checklists
  produce richer progress/decision/issue fields. Use `--set` to correct a
  heuristic extraction before saving.
- Session formats are private implementation details of third-party tools and
  may change. Explicit JSON/JSONL/text import remains the stable fallback.
- Validation results are agent-recorded, not executed — the tool never runs
  your build (by design); freshness logic compensates.

## Roadmap

- **v1 — Context Versioning** ✓
- **v2 — richer adapters** (private session ingestion, capability auto-profiling) ← you are here
- **v3 — context branch / merge** (experimental lines of work)
- **v4 — remote contexts** (push/pull a context chain)
- **v5 — agent-to-agent context network**

## Development

```bash
python -m unittest discover -s tests -v     # full test suite
```

MIT licensed. Contributions welcome — the fastest way to help is to run it
on your real project and file the drift/diff oddities you see.

<div align="center">

# Context Git

**Git for AI Agent Context.**

[English](README.md) · [简体中文](README.zh-CN.md)

Most handoff tools generate a document.<br>
Context Git versions the working state itself.

Snapshot · Branch · Semantic merge · Secure sync · Directed handoff · Resume anywhere.

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
| branch | **Context Branch** — explore competing plans without overwriting working state |
| merge | **Semantic Merge** — reconcile outcomes, progress and constraints with a common ancestor |
| remote | **Remote Context** — securely push/fetch immutable context history across machines |
| handoff | **Agent Context Handoff** — send one published Context to one registered Agent |
| status | **Drift Detection** — is the context still true against the live repo? |

And it is honest about evidence: machine-observed facts (`git HEAD`,
file fingerprints) are marked `observed`; agent claims (goals, decisions)
are marked `agent` — so the next agent knows what to re-verify instead of
trusting a document blindly.

The Context is a **navigation layer**; the live repository stays the source of
truth for code facts. A `NONE` drift grade means the recorded *observed*
evidence is still current on this machine — it does not promote
agent-supplied claims into verified facts. Those keep the confidence and
provenance they were recorded with.

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
context-git switch -c experiment  # branch the working context, not the Git repository
context-git merge experiment      # fast-forward or three-way semantic merge
context-git remote add origin /path/outside/project/context-remote
context-git push                  # publish the current Context branch
context-git pull                  # fetch + fast-forward; never touches source Git
context-git remote unlock         # clear a stale writer lock on a file remote
context-git network register alice --agent codex
context-git network send bob -m "Review the auth boundary"
context-git network inbox         # verified handoffs addressed to this Agent
context-git verify                # Context id / lineage / DAG integrity + secret scan
context-git capabilities          # probe capabilities available in this environment

# V2: private session access is always explicit
context-git sessions --agent codex
context-git import-session /path/to/session.jsonl --dry-run
context-git import-session /path/to/session.jsonl --agent codex
context-git import-session --latest --agent claude-code
```

Run it from **any** directory inside the project. With no `--root`, the project
root is the nearest ancestor holding a `.context-git/` store, otherwise the
containing Git repository root, otherwise the working directory; an explicit
`--root PATH` always wins. That is why `context-git init` run from
`repo/src/auth/` writes `repo/.context-git/` rather than nesting a second store.

Agents pass `--no-prompt` and fill fields with `--set KEY=VALUE`
(repeatable; list fields accept commas; decisions accept JSON; validation
uses `validation.test=pass` form). Fields you don't restate are inherited
from the parent context — commits are incremental, like good commit
messages, not like re-writing the whole README.

## V5 — Agent Context Network

V5 turns a V4 Context remote into a small, asynchronous Agent Context Network.
Agents advertise a stable id and capability profile; a sender addresses an
immutable handoff to one recipient, and that recipient can accept it as a
local Context branch and publish immutable status receipts:

```bash
# Alice publishes a Context and registers on its V4 remote.
context-git push origin main
context-git network register alice --name "Alice" --agent codex

# Bob configures/pulls the same remote, then registers from his checkout.
context-git network register bob --name "Bob" --agent claude-code

# Alice can now address the published Context to Bob.
context-git network agents
context-git network send bob -m "Review the auth boundary" --expires-hours 24

# In Bob's checkout:
context-git network inbox
context-git network accept hnd_0123456789abcdef --switch
context-git network reply hnd_0123456789abcdef accepted -m "Review started"
context-git network reply hnd_0123456789abcdef completed -m "Review finished"

# Back in Alice's checkout:
context-git network status hnd_0123456789abcdef
```

A handoff contains a Context id/branch, sender, recipient, compact intent,
expiry, required capabilities, and the recipient compatibility result. It does
not contain source files, raw chats, prompts, tool payloads, or credentials.
Inbox/status sync verifies the V4 Context graph first, then verifies every
advertised handoff and receipt by content id plus full SHA-256 and byte size
before caching anything.

Agent ids are names inside one authenticated remote, not cryptographic
identities. An existing id cannot be claimed accidentally; `register --force`
is the explicit administrative takeover path. Use TLS, scoped credentials and
server-side authorization when publisher authenticity matters.

See [`references/network.md`](references/network.md) for lifecycle rules,
wire objects, HTTP endpoints, concurrency and the trust model.

## V4 — Remote Context

V4 synchronizes Context history without uploading source files, raw agent
sessions, or source Git state. A remote contains immutable, portable Context
Objects plus a small manifest of branch tips:

```bash
# A file remote may be a shared volume or synced folder outside the project.
context-git remote add origin /Volumes/team/context-demo
context-git push origin main

# On another checkout of the same project:
context-git remote add origin /Volumes/team/context-demo
context-git pull origin main
context-git branch --all                 # includes remotes/origin/main

# Divergence is explicit and semantic.
context-git fetch origin
context-git merge remote:origin/main --dry-run
context-git merge remote:origin/main --resolve current_objective=ours
context-git push origin main
```

HTTPS remotes use a token from an environment variable; the value is never
written to `.context-git/config.json`:

```bash
export CONTEXT_GIT_TOKEN="..."
context-git remote add origin https://contexts.example.com/team/demo \
  --auth-env CONTEXT_GIT_TOKEN
context-git push
```

Push is fast-forward-only unless `--force` is deliberate. Pull fetches and
then fast-forwards only; divergent histories stay intact and require
`merge remote:NAME/BRANCH`. Remote objects replace machine-local repository
roots with `.` while retaining the same Context id. The manifest records a
full SHA-256 and size for every object, and all objects, lineage, project
identity, and residual-secret checks pass before local refs move.

A file remote serialises writers by creating `.context-git.lock` with
`O_CREAT|O_EXCL`; the lock body records pid, host, operation and timestamp.
A writer killed mid-push leaves that file behind, so `context-git remote show`
reports it and `context-git remote unlock REMOTE` clears it — refusing unless
the pid is provably gone on this host. `--force` is the explicit override for
a lock written on another host, a malformed body, or a platform where
liveness cannot be probed. A lock is never removed for age alone: age cannot
distinguish a slow writer from a dead one.

See [`references/remote.md`](references/remote.md) for the HTTPS endpoint
contract, concurrency rules, threat boundary, and deployment checklist.

## V3 — context branch and semantic merge

Context branches let multiple agents or experiments evolve from the same
working state without overwriting each other. They are lightweight files in
`.context-git/refs/heads/`; they are completely separate from Git branches:

```bash
context-git branch                       # list context branches
context-git switch -c auth-passkeys       # create from context HEAD and switch
context-git commit --no-prompt \
  --set completed="Passkey design" \
  --set decisions='[{"decision":"Use WebAuthn","reason":"phishing resistant"}]'
context-git switch main
context-git merge auth-passkeys --dry-run # preview; writes nothing
context-git merge auth-passkeys           # merge when conflict-free
context-git log --all                     # all branch tips and merge parents
```

Merging is a local, deterministic three-way operation:

1. Find the nearest common Context ancestor.
2. Automatically combine one-sided changes and independent list additions.
3. Detect incompatible changes to the same goal, objective, task state,
   architecture fact, decision, constraint, issue, or next action.
4. Refuse to write unresolved conflicts. Resolve deliberately with
   `--resolve KEY=ours|theirs|base` (repeatable) or `--resolve-all`.
5. Capture current machine evidence again and save an immutable merge context
   with two parents: `[ours, theirs]`.

If the current tip is an ancestor of the source, merge fast-forwards by
default; `--no-ff` records a two-parent merge context. Unrelated roots are
refused unless `--allow-unrelated` is explicit. `checkout <context-id>` is a
detached inspection state; use `switch -c NAME` before committing from it.

Observed fields such as Git state, important-file fingerprints, environment,
and validation freshness are never blended from stale contexts. The merge
captures them again from the live project.

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
The complete V3 branch/conflict/merge run is in
[`examples/branch-merge.example.txt`](examples/branch-merge.example.txt).
The V4 two-machine/divergence flow is in
[`examples/remote-sync.example.txt`](examples/remote-sync.example.txt).
The V5 directed handoff/receipt flow is in
[`examples/network-handoff.example.txt`](examples/network-handoff.example.txt).

## Protocol

Context Objects speak **UACP/1.0** (Universal Agent Context Protocol):

- [`references/protocol.md`](references/protocol.md) — object model, history,
  semantic diff, drift grades, conformance
- [`schemas/uacp-1.0.schema.json`](schemas/uacp-1.0.schema.json) — normative JSON Schema
- [`references/schema.md`](references/schema.md) — field-by-field reference
- [`references/security.md`](references/security.md) — threat model and guarantees
- [`references/adapters.md`](references/adapters.md) — V2 session formats and discovery boundaries
- [`references/branching.md`](references/branching.md) — V3 refs, merge rules and conflict handling
- [`references/remote.md`](references/remote.md) — V4 transports, refs, integrity and HTTP contract
- [`schemas/uacp-remote-1.0.schema.json`](schemas/uacp-remote-1.0.schema.json) — remote manifest schema
- [`references/network.md`](references/network.md) — V5 identities, handoffs, receipts and endpoints
- [`schemas/uacp-network-1.0.schema.json`](schemas/uacp-network-1.0.schema.json) — network manifest schema
- [`schemas/uacp-handoff-1.0.schema.json`](schemas/uacp-handoff-1.0.schema.json) — handoff schema
- [`schemas/uacp-receipt-1.0.schema.json`](schemas/uacp-receipt-1.0.schema.json) — receipt schema
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
│   ├── storage.py          # .context-git/ store (HEAD, refs/heads, contexts/)
│   ├── diff.py             # semantic diff engine
│   ├── merge.py            # deterministic three-way context merge
│   ├── remote.py           # file/HTTPS transport + verified push/fetch/pull
│   ├── network.py          # Agent profiles, directed handoffs + receipts
│   ├── drift.py            # drift detection + validity + freshness
│   ├── gitstate.py         # read-only git snapshots
│   ├── project.py          # stack detection (evidence or "unknown")
│   ├── security.py         # secret guard (redact + residual scan)
│   ├── capabilities.py     # agent adapters + compatibility
│   ├── sessions.py         # explicit, compressed private-session ingestion
│   ├── render.py           # HANDOFF.md / show / log / resume views
│   ├── common.py           # ignore rules, fingerprints, safe IO
│   └── adapters/           # codex / claude-code / opencode / cursor / gemini / generic
├── schemas/                # Context Object + Remote Manifest JSON Schemas
├── references/             # protocol, schema, security, adapters, branching
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

The local store remains a plain directory. V5 adds an interoperable Agent
Context Network on the V4 file/HTTPS remote without requiring a hosted service
or coupling to source Git.

## Known limitations (v5)

- Merge is deterministic and structural rather than LLM-assisted. Two
  differently worded bullets may require an explicit resolution even when a
  human considers them equivalent.
- A single local `.context-git/` store still assumes one writer at a time.
  File remotes serialize pushes with a lock; HTTPS services must implement
  immutable object PUTs and ETag/conditional manifest updates.
- Semantic diff is structural — deterministic, LLM-free; it matches reworded
  items by normalisation, not by meaning.
- Drift always describes the current machine. After pulling a portable
  Context, run `status`/`resume` to re-evaluate its observations locally.
- Remote/network storage has no deletion, pruning, encryption-at-rest,
  signatures, or partial object negotiation. Fetch validates the complete
  advertised manifest (up to 10,000 objects) before moving tracking refs.
- Legacy 8-hex Context ids stay readable and verifiable, but at 32-bit
  strength — objects written before 5.0.1 are not rewritten in place. Local
  verification catches corruption and casual tampering; it is not a defence
  against an attacker who can write to the store and rewrite the declared id
  generation at the same time.
- A file remote's writer lock is never cleared automatically, not even when it
  looks stale. A crashed writer needs an explicit `remote unlock`, which is
  the deliberate trade against silently losing mutual exclusion.
- Drift grades are computed from Git and file evidence only. Index-state-only
  movement (`git add` with unchanged bytes) is reported at LOW and never
  expires a recorded validation result.
- Agent ids are authenticated only by the remote transport and its access
  control. V5 does not provide per-Agent keys, signatures, discovery across
  remotes, live messaging, task scheduling, or automatic work execution.
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
- **v2 — richer adapters** ✓
- **v3 — context branch / merge** ✓
- **v4 — remote contexts** ✓
- **v5 — agent-to-agent context network** ✓ ← you are here

## Development

```bash
python -m unittest discover -s tests -v     # full test suite
```

MIT licensed. Contributions welcome — the fastest way to help is to run it
on your real project and file the drift/diff oddities you see.

# Changelog

All notable changes to Context Git are documented here.

## 5.0.1 — 2026-09-14

Correctness, reliability, security and semantic-consistency fixes across
V1–V5. No new capabilities, no protocol redesign, no behaviour change to the
normal CLI flow beyond the bugs below.

### Fixed

- Resolve the ContextGit project root from nested working directories: an
  explicit `--root` still wins, otherwise the nearest ancestor with a
  `.context-git/` store, otherwise the containing Git repository root, and only
  then the working directory. `init` from `repo/src/auth/` no longer creates a
  second, nested store.
- Harden forbidden-path checks against case and trailing-dot/space variants.
  `.ENV`, `.Env`, `.SSH/config`, `.AWS/CREDENTIALS`, `ID_RSA`, `SECRET.PEM`,
  `PRIVATE.KEY` and `TOKEN.BAK` are now refused exactly like their lowercase
  spellings, on every platform. The tool-store prefix check (`.git/`,
  `.context-git/`) and the ignored-directory check are normalised the same way.
- Detect Git index-state drift such as `unstaged → staged`. Path *sets* are no
  longer the comparison unit: each path's staged/unstaged/untracked/conflicted
  state set is compared, so `git add` on an already-modified file is reported
  (`index-state-changed`, graded LOW) instead of looking like no drift at all.
  Index drift no longer expires a recorded validation result — only content
  drift, HEAD movement and important-file fingerprint changes do.
- Include important-file fingerprints in meaningful-change detection. A file
  whose contents changed is now a real change even when Git's path set, status
  and diff stat are byte-identical, so `commit` no longer reports "nothing to
  commit" for genuine edits. The fingerprint enters the core view at
  `core_view_version` 2 while the historical identity shape is preserved.
- Verify local Context Object identity and DAG integrity. `load` stays cheap;
  `verify` now checks Context id vs contents, first-parent alignment, lineage
  shape and dangling parent references, and reports per object. `resume` fails
  closed (exit 7) rather than emitting a briefing from an object that does not
  verify, and `status` flags the same condition.
- Harden `--numstat` parsing for unusual filenames by using `--numstat -z` and
  parsing on NUL instead of splitting lines. Spaces, tabs, newlines, Unicode,
  binary entries and renames/copies now aggregate correctly.
- Improve stale file-remote lock recovery. The lock body records pid, host,
  operation and timestamp; `remote show` reports it and `remote unlock REMOTE`
  clears it — refusing unless the pid is provably gone on this host. `--force`
  is the explicit override for another host, a malformed body, or a platform
  where liveness cannot be probed. Locks are never removed for age alone.
- Generate 16-hex Context IDs (`ctx_` + SHA-256 truncated to 64 bits) while
  retaining legacy 8-hex compatibility. The writer generation is declared as
  `id_hash_version`, so every stored object still verifies against the rule it
  was written with. The read regex now accepts exactly 8 or 16 hex.
- Harden `Store.load_context` to document its deliberately narrow contract and
  keep full-store verification opt-in.

### Documentation

- Clarify that the Context is a navigation layer, while the live repository
  remains the source of truth for code facts. Removed the "trust the briefing,
  not the repo" framing, which contradicted the project's own evidence model.
- Clarify the distinction between observed evidence and agent-supplied claims:
  a NONE drift grade means the recorded *observed* evidence is still current on
  this machine, not that agent claims have been verified.
- Document project-root resolution, `verify`'s integrity scope, and stale-lock
  recovery in `SKILL.md`.
- Development status moved to `4 - Beta` for the first round of real-boundary
  fixes; restore `5 - Production/Stable` once a real user project has run
  against it.

## 5.0.0 — 2026-09-13

- Added the Agent Context Network: registered Agent identities and capability
  profiles on top of an existing V4 Context remote.
- Added directed, content-addressed Context Handoffs with optional expiry,
  compact intent, recipient compatibility, and pointers to published Contexts.
- Added `network register`, `agents`, `send`, `inbox`, `accept`, `reply`, and
  `status` commands with stable JSON output and a dedicated exit code.
- Added immutable accepted/completed/rejected receipts and validated lifecycle
  transitions, including terminal-state and recipient-ownership enforcement.
- Added file and authenticated HTTP network storage with full SHA-256/size
  verification, bounded objects, shared writer locking, and ETag concurrency.
- Added local handoff branches that never modify source files, source Git refs,
  the index, or the working tree.
- Added takeover protection, remote-replacement identity invalidation, secret
  redaction/scanning, cross-project checks, and fail-closed graph validation.
- Published normative network, handoff, and receipt schemas plus an endpoint
  contract and explicit trust-boundary documentation.

## 4.0.0 — 2026-09-13

- Added named Context remotes and `remote`, `push`, `fetch`, and `pull`
  commands without coupling Context refs to source Git remotes.
- Added local/file and HTTPS transports, including bearer-token lookup from a
  named environment variable; credential values are never stored or printed.
- Added a versioned remote manifest, immutable content-addressed objects,
  portable path sanitisation, and full SHA-256 transport verification.
- Added remote-tracking Context refs under `.context-git/refs/remotes/` and
  support for `remote:NAME/BRANCH` in semantic merge.
- Added fast-forward enforcement, explicit force pushes, project identity
  checks, atomic file-remote updates, and ETag/conditional HTTPS updates.
- Added fail-closed validation for malformed, incomplete, tampered, oversized,
  secret-bearing, cross-project, redirected, and unsafe-path remote content.
- Added end-to-end two-clone, divergence/merge, corruption, authentication,
  file transport, and HTTP protocol coverage.

## 3.0.0 — 2026-09-13

- Added independent context branches under `.context-git/refs/heads/` with
  `branch` and `switch` commands; these never touch source Git refs or files.
- Added backward-compatible migration of V1/V2 linear stores to a `main`
  context branch.
- Added DAG ancestry and nearest-common-ancestor discovery.
- Added deterministic three-way semantic merge for goals, objectives,
  progress states, architecture, decisions, constraints, issues, capability
  requirements, and recommended actions.
- Added fail-closed conflict handling with field-level
  `--resolve KEY=ours|theirs|base` and `--resolve-all` choices.
- Added fast-forward merges, `--no-ff`, `--dry-run`, unrelated-history
  protection, branch-aware revision lookup, and `log --all` decorations.
- Advanced the additive schema to 1.1 with `parent_context_ids`,
  `context_branch`, and merge provenance while retaining `parent_context_id`
  for UACP/1.0 readers.

## 2.0.0 — 2026-09-12

- Added explicit, opt-in private session ingestion for Codex, Claude Code,
  Cursor, Gemini CLI, OpenCode, and generic JSON/JSONL/Markdown/text exports.
- Added project-scoped session discovery and `import-session --latest`.
- Added redacted dry-run previews and field-level `--set` corrections.
- Excluded raw transcripts, tool payloads, reasoning, system/developer prompts,
  source paths, and imported session files from persisted context content.
- Added adapter-defined session discovery locations and formats.
- Added project-hash-scoped Gemini JSONL discovery, nested Cursor transcript
  support, and read-only ingestion of current OpenCode SQLite stores.
- Added capability auto-profiling with observed, declared, unavailable, and
  unknown states, plus the `capabilities` command.
- Added session-import provenance and capability-profile fields without
  breaking UACP/1.0 readers.

## 1.0.0 — 2026-09-12

- Initial Context Snapshot, Commit, Semantic Diff, Log, Checkout, Status,
  Drift Detection, Resume, capability mapping, and secret-safety release.

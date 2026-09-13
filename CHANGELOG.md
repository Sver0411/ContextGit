# Changelog

All notable changes to Context Git are documented here.

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

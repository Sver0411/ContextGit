# Changelog

All notable changes to Context Git are documented here.

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

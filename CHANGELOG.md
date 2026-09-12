# Changelog

All notable changes to Context Git are documented here.

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

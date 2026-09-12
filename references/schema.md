# UACP/1.0 Field Reference

Normative machine schema: [`schemas/uacp-1.0.schema.json`](../schemas/uacp-1.0.schema.json).
This document explains intent, defaults and prohibited content per field.

## Identity block

| Field | Type | Notes |
|---|---|---|
| `protocol` | const `"UACP"` | discriminator |
| `protocol_version` | `"1.0"` | semantics version; readers accept any 1.x |
| `schema_version` | `"1.0"` | field-level version; advances additively |
| `context_id` | `ctx_<8hex>` | sha256(parent, timestamp, HEAD, core payload) |
| `parent_context_id` | id \| null | null = root context |
| `created_at` | ISO-8601 UTC | always ends in `Z` |
| `message` | string \| null | commit summary; never treated as evidence |

## Agent blocks

* `source_agent` — `{name, capabilities[], declared_capabilities[],
  capability_profile{}, source_type}`. Who created this. V2 profiles use
  `available / declared / unavailable / unknown`; older readers can ignore
  these additive fields. Unknown capability names are preserved.
* `target_agent` — always `null` when stored; resuming tools may fill a
  working copy for their own compatibility report.

## Narrative block (P0 — agent-supplied unless noted)

| Field | Shape | Inheritance |
|---|---|---|
| `goal` | string \| null | inherited when absent |
| `current_objective` | string \| null | inherited when absent |
| `progress.completed / .in_progress / .pending` | item[] | inherited per-bucket; completed is cumulative; forward moves de-duplicate |
| `decisions` | `{decision, reason, rejected[], source_type}[]` | appended uniquely; empty clears |
| `constraints` | item[] | appended uniquely; empty clears |
| `do_not_change` | item[] | appended uniquely; empty clears — receivers MUST honour |
| `architecture` | fact[] | appended; each fact carries confidence/freshness/source |
| `known_issues` | item[] | removal in a child = resolved |
| `recommended_actions` | item[] | inherited wholesale |
| `capabilities_required` | item[] | appended uniquely; empty clears |

`item` = `{text, source_type}`. An explicitly empty list **clears** the
inherited value (agents deliberately say "no more pending work").

## Observed block (P1 — machine-measured)

* `project` — name (from package.json / pyproject / Cargo.toml / go.mod /
  directory), root path, primary type, `stack` (languages, frameworks,
  package manager, commands, monorepo flag, evidence strength). Detection
  rule: **evidence or "unknown"** — never guess.
* `important_files[]` — `{path, why, fingerprint, confidence, freshness,
  source_type}`; 5–20 items. Files up to 5 MiB are hashed in full with
  SHA-256 plus size; larger files and all symlinks are skipped. Forbidden
  paths (credentials, keys) are excluded before selection.
* `git` — branch, head(+short), subject, upstream/ahead/behind,
  repo_state (clean / merge-in-progress / conflicts-present / …),
  working_tree lists (staged/unstaged/untracked/conflicted), changed_files
  with change_type and +/- counts, diff_stat, recent_commits (≤8, no
  bodies). **Never hunk content.**
* `validation` — `{build, test, lint, typecheck}` as `{result}` entries,
  plus `observed_at`, `freshness`, `confidence`, `note`. The tool never runs
  commands, so these results remain `source_type: agent`; the timestamp and
  subsequent freshness calculation are machine-generated.
* `environment` — OS/release/arch, Python version, project runtime pins,
  package manager, git/monorepo flags. **Never credentials or hostnames
  beyond the local path.**

## Coordination blocks

* `recommended_actions` — ordered next steps (P0 despite being agent text).
* `security` — `{secret_guard, redacted_on_write: true, sensitive_files_read: false}`.
  Lets auditors verify the guard ran without re-scanning.
* `metadata` — tool name/version, free notes, `fingerprints` map
  (drift baseline), `fingerprint_algorithm`, and optional `session_import`
  provenance. Session provenance contains no source path or transcript and
  records that raw messages, tools and reasoning were not stored.
* `extensions` — reserved empty object for future capability blocks.

## Prohibited content (P3 — rejects or redacts on write)

* chat transcripts, user/agent dialogue
* chain-of-thought or internal reasoning
* full git diffs (hunks), build/test output logs
* contents of `.env*`, key files, credential stores
* any string matching secret patterns — replaced by `[REDACTED:kind]`

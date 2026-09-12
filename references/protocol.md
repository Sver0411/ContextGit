# UACP — Universal Agent Context Protocol

Version 1.0 · status: stable (implemented by Context Git 1.x and 2.x)

UACP defines what an AI agent's *working state* looks like when written
down, how those snapshots form a history, how two snapshots are compared,
and how a new agent resumes from one. It is platform-neutral, model-neutral,
and storage-neutral (the reference implementation stores one JSON file per
context in `.context-git/contexts/`).

---

## 1. What a Context is — and is not

> **Context = minimum sufficient state required to continue useful work.**

A Context Object is **not**:

| Not this | Why |
|---|---|
| Chat history | conversations contain 95% noise; the protocol stores outcomes |
| Chain of thought | reasoning is the agent's private process, not the work product |
| Repository copy | the repo already exists; contexts store *pointers + fingerprints* |
| A prompt template | it is data with a schema, renderable however a tool likes |

## 2. Object model

A Context Object is a JSON document with `protocol: "UACP"` and
`protocol_version: "1.x"`. See `schemas/uacp-1.0.schema.json` for the
normative schema and `schema.md` for field-by-field reference.

Top-level sections map to the Context Priority classes:

| Priority | Sections | Rule |
|---|---|---|
| **P0 — must survive** | `goal`, `current_objective`, `progress`, `architecture`, `decisions`, `constraints`, `do_not_change`, `known_issues`, `recommended_actions`, `capabilities_required` | Losing any of these makes the handoff useless or dangerous |
| **P1 — important** | `important_files`, `git`, `validation`, `environment`, `project` | Machine-observed backbone the narrative hangs on |
| **P2 — auxiliary** | `git.recent_commits`, `metadata.notes` | Nice to have; safe to truncate |
| **P3 — forbidden** | chat logs, CoT, full diffs, build output, `node_modules`-style trees | **MUST NOT be stored**, not even truncated |

## 3. Evidence model

Every piece of information carries `source_type`:

* `"observed"` — measured by the CLI: git HEAD, file fingerprints and
  project metadata. Reproducible by any agent with filesystem access.
* `"agent"` — asserted by the exporting agent or the user: goals, decisions,
  pending work. **Claims.** A resuming agent may trust them for orientation
  but must not cite them as verified fact.

This distinction is the protocol's defence against context rot: agent
assertions that were true three hours ago decay; machine observations can
be re-checked mechanically. The drift engine uses exactly this split.

## 4. History model

Contexts form a chain via `parent_context_id`:

```
ctx_a1 (root)
  └── ctx_b2   "Auth middleware implemented"
        └── ctx_c3   "Refresh token rotation done"
```

* **v1 is linear.** A context has exactly one parent. Branch/merge is
  reserved for future versions; because history is already a DAG-shaped
  pointer structure, adding branches later requires **no schema change**.
* Contexts are **immutable**. Correction = create a new context whose
  parent is the same as the wrong one's (or record an explicit decision).
* `context_id` is `ctx_` + 8 hex chars of
  `sha256(parent_id ‖ created_at ‖ repo_HEAD ‖ canonical_core_payload)`.
  The timestamp guarantees uniqueness; the payload hash makes ids
  self-verifying (recompute and compare).
* The **context HEAD** is a pointer to the newest context in the chain
  (reference impl: `.context-git/HEAD`). `checkout` moves only this
  pointer — it MUST never touch the user's git repository or files.

## 5. Incremental commits

Agents must not restate everything on every commit. Fields absent from the
commit notes are **inherited from the parent**:

* scalar fields (`goal`, `current_objective`) inherit when empty
* list fields (`progress.*`, `decisions`, `constraints`, `do_not_change`,
  `known_issues`, `recommended_actions`, `capabilities_required`) inherit
  when absent; supplying an **empty list clears** the inherited value
  (e.g. "there are no longer any known issues")
* completed work, decisions, architecture facts, constraints, do-not-change
  rules and capability requirements append uniquely when supplied; moving a
  task forward automatically removes it from inherited lower-progress buckets
* `validation` inherits wholesale; the drift engine then marks it STALE
  if code changed after `observed_at` (Validation Freshness rule, §8)

A commit that changes nothing semantically (identical core payload) SHOULD
be refused; tools MAY offer an explicit override (`--allow-empty`).

## 6. Semantic Diff

The unit of incremental transfer is the **semantic diff** between two
contexts — never a JSON field dump. A conforming diff reports, at minimum:

* goal / objective changes
* progress items as `added` / `moved between buckets` / `removed`
* decisions made or reversed (with reason and rejected alternatives)
* issues opened (`issue-opened`) and resolved (`issue-resolved`)
* constraint / do-not-change changes
* important files added / modified (fingerprint compare) / removed
* repository movement: HEAD, branch, working tree, diff stat
* validation result changes

Item identity is text-normalised (case/punctuation-insensitive), so
reworded items still match. The reference engine is deterministic and
LLM-free; an LLM layer may summarise on top but MUST NOT be required.

## 7. Token-efficient transfer

Full contexts are only sent once. Afterwards, a tool MAY send:
* `diff(parent, new)` per step, or
* an **aggregated diff** `diff(old_checkpoint, new)` — the reference engine
  accepts any pair, so aggregation is just calling it twice with the older id.

This is what turns "resend 5000 tokens of handoff" into "here are the four
lines that changed".

## 8. Drift and validity

Drift compares a context's *observed* evidence against the live repository:
HEAD movement (+ commit count), branch change, working-tree change set,
and per-file fingerprint compare for important files.

* Drift is **graded**: `NONE / LOW / MEDIUM / HIGH`.
* Invalidation is **local**: stale important files are listed by path and
  only the sections that depend on them are marked stale. The narrative
  (goal, decisions, constraints) stays trustworthy even when code moved on.
* **Validation freshness**: a recorded `validation` result is STALE when any
  relevant source changed after `observed_at`. Resuming agents MUST treat
  STALE validation as "unknown", not "passing".
* Every important file gets per-file validity: `VALID / STALE / UNKNOWN`.

## 9. Capability mapping

Contexts MAY declare `capabilities_required` (from the shared vocabulary:
`filesystem shell git python node browser gui network image-generation
subagents`). A resuming tool compares them against the target agent's
effective capabilities and reports compatibility percentage plus
**affected work items** — naming which next action breaks if a capability
is missing. Compatibility is advisory, never a gate: agents may proceed
with degraded plans.

Context Git 2.x profiles distinguish `available` (safe local probe),
`declared` (adapter claim that cannot be measured portably), `unavailable`
(a measurable declaration was not found), and `unknown`. Tools MUST NOT
present an adapter declaration as machine-observed evidence.

### Private session ingestion (optional V2 implementation feature)

UACP stores compressed working state, never a transcript. An implementation
MAY derive Context fields from a private agent session only when the user
explicitly selects it or requests project-scoped latest-session discovery.
Ordinary snapshot, status, diff and resume operations MUST NOT scan private
session directories.

An importer MUST discard system/developer prompts, tool calls/results,
reasoning/thinking blocks and all raw messages. It MAY retain only extracted
P0 work-state fields plus non-sensitive provenance (provider, format, source
fingerprint, counts and exclusion guarantees). Extracted strings pass the
same pre-write redaction and residual scan as manually supplied fields.

## 10. Security

All free text passes secret redaction **before** entering a Context Object
(`[REDACTED:kind]` placeholders). Sensitive files (`.env*`, keys,
credential stores, `.ssh/` contents, …) are never read in the first place.
Full threat model and guarantees: `security.md`.

## 11. Versioning and extensibility

* `protocol_version` changes only for semantic breaks; readers MUST accept
  any `1.x` they understand and warn on higher majors.
* `schema_version` may advance independently (additive fields).
* Unknown fields MUST be ignored, not rejected — forward compatibility.
* The `extensions` slot (arbitrary JSON object) is reserved for future
  capability blocks (encryption, signatures, remote references, branching
  metadata). v1 writers MUST leave it empty.

## 12. Conformance

A tool is UACP/1.0-conformant if it can, at minimum:

1. produce a Context Object valid against the JSON Schema,
2. link contexts via `parent_context_id` and maintain a HEAD pointer,
3. refuse to store P3 content and redact secrets pre-write,
4. compute a semantic diff between two of its own contexts,
5. report graded drift with per-file validity against a live repository.

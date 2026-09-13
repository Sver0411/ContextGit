# UACP — Universal Agent Context Protocol

Version 1.0 · status: stable (implemented by Context Git 1.x–5.x)

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

Contexts form a directed acyclic graph. `parent_context_id` remains the
first-parent compatibility pointer; V3 writers also emit
`parent_context_ids`:

```
ctx_a1 (root)
  └── ctx_b2   "Auth middleware implemented"
        ├── ctx_c3   "Refresh token rotation done" ──┐  (main)
        └── ctx_d4   "Explore passkeys" ─────────────┴─ ctx_e5 (merge)
```

* Root contexts have no parents, ordinary contexts have one, and V3 merge
  contexts have exactly two ordered parents: `[ours, theirs]`.
  `parent_context_id` MUST equal the first parent so V1/V2 readers retain a
  valid linear first-parent view.
* Contexts are **immutable**. Correction = create a new context whose
  parent is the same as the wrong one's (or record an explicit decision).
* `context_id` is `ctx_` + 8 hex chars of
  `sha256(parent_ids ‖ created_at ‖ repo_HEAD ‖ canonical_core_payload)`.
  The timestamp guarantees uniqueness; the payload hash makes ids
  self-verifying (recompute and compare).
* The **context HEAD** is a pointer to the newest context in the chain
  (reference impl: `.context-git/HEAD`). `checkout` moves only this
  pointer — it MUST never touch the user's git repository or files.

### 4.1 Context branches

A context branch is a validated name pointing to one Context Object. In the
reference store, refs live below `.context-git/refs/heads/` and `HEAD` is
either symbolic (`ref: refs/heads/main`) or detached (`context: ctx_…`).

Branch operations MUST NOT read or mutate source Git refs, the Git index, or
working-tree files. Deleting a branch removes only the ref; Context Objects
remain immutable and may still be reachable through merge ancestry.

V1/V2 stores with a direct context HEAD migrate losslessly to a `main`
context branch. Checking out a raw context id is detached inspection; a new
branch is required before an ordinary commit.

### 4.2 Three-way semantic merge

Given current `ours`, selected `theirs`, and their nearest common ancestor
`base`, a conforming V3 merge:

1. fast-forwards when `ours` is an ancestor of `theirs`, unless explicitly
   asked to record a merge object;
2. treats a side unchanged from `base` as accepting the other side;
3. unions independent additions to list fields and honours one-sided
   removals;
4. merges each progress task by semantic identity and state;
5. reports a conflict when both sides changed the same semantic value
   differently;
6. persists nothing until every conflict has an explicit
   `ours` / `theirs` / `base` resolution;
7. captures Git state, important-file fingerprints, environment and
   validation freshness again instead of merging stale observations; and
8. writes `[ours, theirs]` plus merge provenance into the new immutable
   context.

Unrelated roots MUST be refused by default. Merge previews MUST be read-only.

### 4.3 Remote Context transport

V4 implementations MAY synchronize the Context DAG through a separate,
versioned remote manifest. Remote refs and transports are implementation
capabilities; they do not change the UACP/1.0 Context Object schema.

A conforming remote implementation:

1. transfers portable copies with machine-local root paths removed;
2. preserves and recomputes each Context id, including legacy scalar-parent
   ids, and authenticates the full serialized object with SHA-256;
3. verifies parent closure before moving a local or remote-tracking ref;
4. protects remote updates against lost writes and non-fast-forwards;
5. separates remote-tracking refs from writable local Context branches;
6. validates project identity unless a cross-project operation is explicit;
7. does not transfer P3 content, raw sessions, or source files; and
8. does not mutate source Git state.

The reference manifest/HTTPS contract is specified in `remote.md` and
`schemas/uacp-remote-1.0.schema.json`. Unknown manifest fields SHOULD be
ignored for forward compatibility.

### 4.4 Agent Context Network

V5 implementations MAY add an asynchronous, directed handoff layer over a V4
Context remote. This layer does not change the Context Object schema. A
conforming Agent Context Network implementation:

1. registers bounded Agent ids with capability profiles;
2. points each handoff to an already-published, verified Context Object;
3. addresses each handoff to exactly one different recipient;
4. stores handoffs and receipts as immutable, content-addressed objects;
5. allows only the named recipient to publish accepted, completed, or rejected
   receipts, and rejects transitions after a terminal receipt;
6. verifies the complete advertised network object set before caching it;
7. imports an accepted handoff as a Context branch without mutating source Git
   or repository files; and
8. transfers no transcript, source file, tool payload, prompt, or credential.

Agent ids are transport-authenticated names, not cryptographic principals.
The normative wire formats and lifecycle are specified in `network.md` and
the three `schemas/uacp-{network,handoff,receipt}-1.0.schema.json` files.

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
* V3 branch/merge is an additive UACP/1.0 capability: legacy readers follow
  `parent_context_id`; V3 readers prefer `parent_context_ids` when present.
* V4 remote sync is an additive transport capability. It does not embed
  credentials or authoritative remote refs in immutable Context Objects.
* The `extensions` slot (arbitrary JSON object) remains reserved for future
  capability blocks such as encryption and signatures.

## 12. Conformance

A tool is UACP/1.0-conformant if it can, at minimum:

1. produce a Context Object valid against the JSON Schema,
2. link contexts via `parent_context_id` and maintain a HEAD pointer (V3+
   implementations additionally understand `parent_context_ids` and refs),
3. refuse to store P3 content and redact secrets pre-write,
4. compute a semantic diff between two of its own contexts,
5. report graded drift with per-file validity against a live repository.

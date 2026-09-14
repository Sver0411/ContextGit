# Security Model

Context files are designed to be safe to commit, safe to share with another
agent, and safe to paste into an issue. That is a strong claim, so this
document states exactly how it is engineered, what the guarantees are, and
where the edges are.

## Threat model

| Threat | Consequence | Defence |
|---|---|---|
| Secret in source notes ("the key is sk-abc…") | key persisted into `.context-git/` | content redaction pre-write (layer 2) |
| Tool reads `.env` / key files while collecting state | secret enters fingerprint/file lists | path-level denial (layer 1), case- and trailing-dot/space-normalised |
| Sensitive path recorded as "important file" | leaks project structure of secret stores | forbidden-path filter before selection |
| A case variant (`.ENV`, `ID_RSA`) slips past a case-sensitive check | credential file read or named in the report | every path comparison is `casefold()`ed on every platform, never Windows-only |
| Locally stored Context edited by hand | an agent acts on a briefing that is confidently false | `verify` recomputes the id from the object's own contents; `resume` fails closed with exit 7 |
| git commit subjects / changed paths contain tokens | token lands in `git` block | every free-text field passes `redact()` |
| Redaction bug ships a secret anyway | leaked on disk | residual scan **before write** aborts the commit (`scan_object`), plus post-hoc `context-git verify` |
| Context store committed to a public repo | historical leak | redaction runs at write time; we never "store now, scrub later" |
| Private agent session contains prompts, secrets or reasoning | sensitive transcript persisted into a context | sessions are read only after an explicit import command; only compact work-state fields survive; private records and raw messages are discarded before context construction |
| Remote contains a tampered/colliding object | false context accepted | Context id recomputation + full SHA-256/size verification before refs move |
| Concurrent remote writers lose updates | branch history overwritten | file lock/state compare or HTTPS ETag conditional update; non-fast-forward refusal |
| Bearer token leaks through config, output, or redirect | remote account compromise | config stores the environment-variable name only; values are never printed; redirects are refused |

## Layer 1 — path-level denial (before any read)

These are never read, hashed, fingerprinted, or listed:

* `.env`, `.env.*` (any suffix)
* `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, `*.pem`, `*.key`, `*.pfx`,
  `*.p12`, `*.jks`, `*.keystore`, `*.ppk`, `*.pgp`
* `credentials`, `credentials.json`, `.netrc`, `.npmrc`, `.pypirc`,
  `.htpasswd`, `.git-credentials`, `.dockercfg`, `kubeconfig`
* `secrets.{yml,yaml,json}`, `terraform.tfstate`
* anything under `.ssh/`, `.aws/`, `.gnupg/`, `.kube/`, `.docker/`
* backup copies: `*.env.bak`, `secret.key.old`, `token.txt.swp`, …

Matching is **case-insensitive and trailing-dot/space-insensitive on every
platform**, not only on Windows. `.ENV`, `.Env`, `.SSH/config`, `.AWS/CREDENTIALS`,
`ID_RSA`, `SECRET.PEM`, `PRIVATE.KEY` and `TOKEN.BAK` are refused exactly like
their lowercase spellings, and so is a path whose *any* segment is the tool's
own store (`.GIT/`, `.CONTEXT-GIT/`, including a nested store deeper in the
tree). A case-sensitive check would silently stop protecting a repository the
moment it is read on a case-preserving/case-insensitive filesystem, so the
rule is normalised everywhere rather than special-cased per platform.

The filter is deliberately over-inclusive: a false positive costs one file
in a report; a false negative costs the user a credential.

## Layer 2 — content redaction (before any write)

Every string that enters a Context Object passes `redact()`:

* typed placeholders, e.g. `[REDACTED:api-key]`, `[REDACTED:jwt]`,
  `[REDACTED:aws-access-key]`, `[REDACTED:private-key]`
* shape-based detection, not a vendor list: `sk-…`, `sk-ant-…`, `sk-proj-…`,
  `ghp_…`/`github_pat_…`, `AKIA…`/`ASIA…`, `xox…-`, `glpat-…`, `AIza…`,
  JWTs (`eyJ…`), `Bearer …`, `Authorization:`/`Cookie:` headers,
  `-----BEGIN … PRIVATE KEY-----`
* keyword assignments: `password=`, `token:`, `SECRET_KEY="…"`,
  `api_key: …` → value replaced
* URL credentials `scheme://user:pass@host` → password replaced
* high-entropy strings following `key|token|secret|password|credential`
* idempotent — redacting twice is a no-op; on internal failure the whole
  string collapses to `[REDACTED]` (fails closed)

## Layer 3 — residual scan (independent second opinion)

`scan_object()` re-checks the assembled payload with a **different** detector
set before anything touches disk. Findings abort the snapshot/commit with
exit code 2 and name the pattern (never the value). `context-git verify`
runs the same scan over stored artifacts any time.

## V2 private-session boundary

Normal commands (`snapshot`, `commit`, `status`, `diff`, `log`, `resume`,
`checkout`, `verify`, `capabilities`) never search or read agent session
directories. Access happens only after `sessions`, `import-session FILE`, or
`import-session --latest` is explicitly invoked.

The importer accepts only regular, non-symlinked `.json`, `.jsonl`, `.md`, or
`.txt` files up to 20 MiB. OpenCode `.db` files are exempt from that size cap
because they are queried through SQLite in `mode=ro` with `query_only=ON`; the
database is never copied or modified. Project-scoped discovery considers at
most 100 recent file candidates (or 1,000 indexed OpenCode session rows) and
returns only sessions tied to the requested project. Cross-project explicit
imports require the visible `--allow-other-project` override.

Only visible user/assistant text enters compression. System/developer
envelopes, tool calls/results, function payloads, images, reasoning and
thinking records are discarded. The compressor extracts short
goal/objective values and structured outcome headings/checklists. It stores
no raw message, original source path, transcript, or session identifier. A
source file inside the repository is excluded from `important_files`.

## V3 branch and merge boundary

- Context refs live only below `.context-git/refs/heads/`. Branch names use a
  conservative cross-platform character set and reject absolute paths,
  traversal components, repeated separators, `.lock` suffixes and symlinks.
- Context `switch`, `checkout`, and `merge` do not call Git mutation commands
  and never update source Git refs, the index, or working-tree files.
- Merge operates only on already-redacted Context Objects. The merged object
  passes the independent residual secret scan before any ref advances.
- Unresolved semantic conflicts are preview-only and are never persisted.
- Merge provenance contains context ids, a validated local ref name and
  resolution keys, never discarded branch contents or private transcripts.

## V4 remote boundary

- Remote commands transfer only already-redacted Context Objects and a
  manifest. Source files, full diffs, private sessions, credentials, and
  source Git objects/refs are never part of the wire format.
- Transported copies replace `project.root` and `git.repo_root` with `.`.
  Every object passes Context-id, parent-lineage, full-digest, size, and
  residual-secret validation before refs advance.
- A remote is bound to a privacy-preserving source-repository fingerprint
  when a Git origin exists, otherwise to the detected project name.
  Cross-project sync requires `--allow-other-project`.
- File remotes must be outside and must not contain the project directory.
  Network-host file URLs and symlinked manifest/object paths are refused.
- HTTPS bearer values are read only from the configured environment variable
  at request time. URL-embedded credentials, query/fragment data, redirects,
  external plain HTTP, and missing credentials fail closed.
- File remotes use an exclusive `O_CREAT|O_EXCL` lock plus state comparison.
  The lock body records pid, host, operation and timestamp, is reported by
  `remote show`, and is cleared only by an explicit `remote unlock` — never
  automatically and never on age alone. HTTPS remotes must
  expose an ETag and honor `If-Match`/`If-None-Match`; `409`/`412` means retry
  after fetching. Push is non-fast-forward by default.
- Local Context Objects get the same identity treatment as transported ones:
  `verify` recomputes each id from its own contents, checks first-parent
  alignment, validates lineage shape and reports dangling parent references,
  and `resume` refuses (exit 7) rather than briefing an agent from an object
  that does not verify. `load` stays deliberately cheap and is not a gate.

## V5 Agent Context Network boundary

- The network publishes Agent profiles, Context pointers, compact handoff
  messages, capability compatibility and status receipts only. It never
  transports source files, diffs, transcripts, prompts, reasoning, tool
  payloads, environment values, or credentials.
- Handoffs and receipts are immutable and content-addressed. The mutable
  network manifest records their full SHA-256 and canonical byte size; the
  entire advertised set is checked before new objects enter the local cache.
- A handoff must reference a Context already present in the verified V4 remote
  manifest. Inbox/status sync authenticates that Context graph first.
- Receipt publishers and state history are checked against the handoff:
  recipient-only, at most one `accepted`, and no receipt after `completed` or
  `rejected`. Expired handoffs remain auditable but cannot be accepted.
- Agent ids are names within one remote. They have no per-Agent signature or
  key in V5, so authenticity depends on TLS, bearer scope and server-side
  authorization. `register --force` is an explicit takeover, not proof of
  identity. Replacing/removing a remote clears its bound local identity.
- Accepting creates or selects only a `.context-git/refs/heads/handoff/...`
  branch. It never changes source Git state or repository files.

## Guarantees

1. Sensitive paths are never opened — not for hashing, not for listing.
2. Free text is redacted before serialization, never after.
3. A failed redaction stops the write; it does not proceed degraded.
4. The tool never executes project code, never runs builds/tests, never
   touches git history (no commit/push/checkout/reset on the user's repo).
5. Private sessions are never read implicitly and raw transcript content is
   never written to the context store.
6. Remote sync never stores a configured bearer-token value and never follows
   a response redirect with it.
7. Network messages and profiles pass the same redaction and residual-secret
   checks before publication or caching.

## Known limits (honesty section)

* Redaction is pattern-based. A novel encoding (base64 of a key inside a
  sentence, a made-up vendor prefix) can slip through layer 2. Layer 3
  catches common shapes only. **Review `HANDOFF.md` before sharing contexts
  outside your machine** — the files are plain JSON/Markdown for exactly
  this reason.
* Files up to 5 MiB are read only to compute a one-way SHA-256 fingerprint;
  their contents are never stored. Larger files and symbolic links are
  skipped, so links cannot escape the repository boundary.
* Environment block records OS/runtime/package-manager only. If your
  hostname or absolute paths are sensitive, treat context files as
  internal artifacts.
* Third-party private session formats are not stable APIs. Preview every
  import with `--dry-run` after a tool upgrade and correct heuristic
  extraction with `--set`.
* Transport integrity is not publisher authenticity. V4/V5 do not sign or
  encrypt Context, handoff, or receipt objects; use TLS, authenticated
  endpoints, access controls, and encrypted storage where required.

# Security Model

Context files are designed to be safe to commit, safe to share with another
agent, and safe to paste into an issue. That is a strong claim, so this
document states exactly how it is engineered, what the guarantees are, and
where the edges are.

## Threat model

| Threat | Consequence | Defence |
|---|---|---|
| Secret in source notes ("the key is sk-abc…") | key persisted into `.context-git/` | content redaction pre-write (layer 2) |
| Tool reads `.env` / key files while collecting state | secret enters fingerprint/file lists | path-level denial (layer 1) |
| Sensitive path recorded as "important file" | leaks project structure of secret stores | forbidden-path filter before selection |
| git commit subjects / changed paths contain tokens | token lands in `git` block | every free-text field passes `redact()` |
| Redaction bug ships a secret anyway | leaked on disk | residual scan **before write** aborts the commit (`scan_object`), plus post-hoc `context-git verify` |
| Context store committed to a public repo | historical leak | redaction runs at write time; we never "store now, scrub later" |
| Private agent session contains prompts, secrets or reasoning | sensitive transcript persisted into a context | sessions are read only after an explicit import command; only compact work-state fields survive; private records and raw messages are discarded before context construction |

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

## Guarantees

1. Sensitive paths are never opened — not for hashing, not for listing.
2. Free text is redacted before serialization, never after.
3. A failed redaction stops the write; it does not proceed degraded.
4. The tool never executes project code, never runs builds/tests, never
   touches git history (no commit/push/checkout/reset on the user's repo).
5. Private sessions are never read implicitly and raw transcript content is
   never written to the context store.

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

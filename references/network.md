# Agent Context Network Protocol

V5 adds directed, asynchronous Context handoffs between Agents that share a V4
Context remote. It coordinates pointers to working state; it is not chat,
process orchestration, an Agent runtime, or source-code transport.

## Remote model

```text
remote-root/
├── manifest.json                         # V4 Context DAG and branch tips
├── contexts/ctx_....json                 # V4 immutable Context Objects
├── network.json                          # V5 agents and object indexes
└── network/
    ├── handoffs/hnd_................json # immutable directed handoffs
    └── receipts/rcp_................json # immutable status events
```

`network.json` has format `context-git-network`, version `1.0`, protocol
`UACP/1.0`, the same project descriptor as the Context manifest, up to 1,000
Agent profiles, and indexes for up to 10,000 total handoff/receipt objects.
Each index entry contains the full SHA-256 and canonical byte size.

The normative shapes are:

- [`../schemas/uacp-network-1.0.schema.json`](../schemas/uacp-network-1.0.schema.json)
- [`../schemas/uacp-handoff-1.0.schema.json`](../schemas/uacp-handoff-1.0.schema.json)
- [`../schemas/uacp-receipt-1.0.schema.json`](../schemas/uacp-receipt-1.0.schema.json)

## Identity and capabilities

```bash
context-git network register AGENT_ID [--name NAME] [--agent ADAPTER]
context-git network agents
```

An Agent id is 2–64 lowercase letters, digits, dots, underscores or hyphens,
starting with a letter. Registration publishes a redacted display name,
adapter, effective capabilities, and available/declared/unavailable/unknown
profile states. An existing id may be updated by the same locally bound
identity. A different local store must use explicit `--force` to take it over.

This is a namespace and safety rail, not cryptographic identity. A client with
write access to the remote can alter profiles or manifests. Deployments that
need publisher authenticity must enforce it at the HTTPS authorization layer;
V5 has no Agent keys or signatures.

## Handoff lifecycle

The sender first publishes the Context branch through V4, then sends it:

```bash
context-git push origin main
context-git network send bob --branch main \
  --message "Review the auth boundary" --expires-hours 24
```

The handoff names exactly one sender, one different recipient, a published
Context id and Context branch. It also carries compact intent, optional expiry,
required capabilities and the compatibility partition computed from the
recipient's advertised profile. It does not embed the Context Object.

The recipient synchronizes and accepts it:

```bash
context-git network inbox
context-git network accept hnd_0123456789abcdef --switch
context-git network reply hnd_0123456789abcdef accepted
context-git network reply hnd_0123456789abcdef completed
```

Accept creates `handoff/SENDER/IDPREFIX` by default and optionally switches
Context HEAD. The source Git branch, refs, index, working tree and project files
are untouched. Acceptance is local; a receipt is published only by an explicit
`network reply`.

Allowed receipt histories are:

```text
pending ──> accepted ──> completed
   │            └──────> rejected
   ├───────────────────> completed
   └───────────────────> rejected
```

Only the named recipient can create a valid receipt. At most one `accepted`
receipt is allowed, timestamps cannot predate the handoff, and no event may
follow `completed` or `rejected`. Expired handoffs remain visible for audit but
cannot be accepted. Senders inspect the history with `network status [ID]`.

## Content identity and validation

Handoff ids are `hnd_` plus the first 16 hexadecimal characters of SHA-256 over
canonical JSON with `handoff_id` removed. Receipt ids use the same construction
with `rcp_` and `receipt_id`. Canonical JSON is UTF-8, sorted by key, compactly
separated, and terminated by a newline. The mutable manifest also authenticates
the complete canonical object with its full SHA-256 and byte size.

Inbox and status operations first perform a V4 fetch. Only after the Context
DAG, project identity, lineage and residual-secret checks pass does the client
validate the complete V5 object index. Network validation checks ids, full
digests, sizes, bounds, times, capabilities, Context pointers, recipient-owned
receipts and lifecycle history before caching new objects. Handoffs and receipts
are append-only in the local cache and remote object namespace.

## File and HTTPS concurrency

File remotes use the same `.context-git.lock` as V4 so a Context manifest and a
network manifest cannot be updated concurrently. The writer compares the state
read during negotiation, writes immutable objects, then atomically replaces
`network.json`.

For base URL `https://host/team/project`, a V5 service adds:

| Method | Path | Required behavior |
|---|---|---|
| `GET` | `/team/project/network.json` | `200` JSON + `ETag`, or `404` before first registration |
| `GET` | `/team/project/network/handoffs/{id}.json` | `200` immutable JSON, or `404` |
| `GET` | `/team/project/network/receipts/{id}.json` | `200` immutable JSON, or `404` |
| `PUT` | either object path | create/idempotently accept identical bytes; reject different bytes with `409` |
| `PUT` | `/team/project/network.json` | honor initial `If-None-Match: *` and later `If-Match: <etag>`; reject stale writes with `409` or `412` |

Network objects are limited to 1 MiB each; the manifest is limited to 5 MiB.
The V4 rules for TLS, bearer tokens, redirects and loopback-only plain HTTP
apply unchanged. A raced HTTP update may leave an unreferenced immutable object;
it is harmless and should be retained until the service's safe GC window.

## Deliberate V5 limits

V5 does not provide discovery across remotes, presence, live chat, broadcast
handoffs, scheduling, automatic Agent execution, object deletion, pruning,
partial fetch, signatures, end-to-end encryption, or conflict-free replicated
manifest updates. These can be layered on without changing UACP Context Objects.

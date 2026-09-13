# Remote Context Protocol

V4 adds how Context Git transfers immutable UACP Context Objects between
machines. It does not define source-code hosting and does not reuse or mutate
source Git remotes, refs, indexes, branches, or working trees.

V5 reuses the same configured transport and authentication for a separate
`network.json` namespace. Its additional endpoints and rules are specified in
[`network.md`](network.md); the V4 Context manifest remains unchanged.

## Model

A Context remote is an object store with one mutable manifest:

```text
remote-root/
├── manifest.json
└── contexts/
    ├── ctx_1a2b3c4d.json
    └── ctx_5e6f7788.json
```

The manifest is versioned independently from UACP Context Objects. Its
normative shape is in
[`../schemas/uacp-remote-1.0.schema.json`](../schemas/uacp-remote-1.0.schema.json).
`refs.heads` maps validated Context branch names to object ids. `objects` maps
every advertised id to a full SHA-256 digest and byte size of its canonical
JSON representation.

Local tracking refs live under `.context-git/refs/remotes/NAME/BRANCH` and are
addressed as `remote:NAME/BRANCH`, for example:

```bash
context-git fetch origin
context-git merge remote:origin/main
```

Tracking refs are observations of a completed fetch or push. They are not a
second local branch and cannot be committed on directly.

## Commands and state transitions

```bash
context-git remote                         # list configured remotes
context-git remote add NAME URL
context-git remote show [NAME]
context-git remote remove NAME

context-git push [REMOTE] [BRANCH] [--dry-run] [--force]
context-git fetch [REMOTE] [--branch BRANCH]
context-git pull [REMOTE] [BRANCH]
```

`origin` is the default when present; otherwise the only configured remote is
selected. Multiple non-`origin` remotes require an explicit name.

- **Push** walks the complete local DAG reachable from the selected tip,
  verifies every object, uploads only objects absent from the manifest, then
  advances the remote ref. The update must be a fast-forward unless `--force`
  is explicit. `--dry-run` performs validation and negotiation without writes.
- **Fetch** downloads and verifies the complete advertised object set before
  saving new immutable objects or moving any tracking ref. `--branch` limits
  which tracking ref moves, not which objects are authenticated.
- **Pull** fetches, then advances the same-named current local Context branch
  only when it is unborn, already current, or a fast-forward. Divergence exits
  without moving local HEAD; resolve it with an explicit semantic merge.
- **Remote remove** deletes its configuration and tracking refs only. Local
  Context Objects remain available.

Push/fetch/pull never upload project files or raw sessions and never invoke a
mutating Git command.

## Portable objects and integrity

Machine-specific `project.root` and `git.repo_root` values are replaced by `.`
in the transported copy. Those fields are not part of the Context identity
core, so the original `context_id` remains self-verifying. The local source
object is not modified.

Before a remote ref is trusted, Context Git checks:

1. manifest format `context-git-remote/1.0` and protocol `UACP/1.0`;
2. branch names, ids, sizes, full SHA-256 values, and ref/object linkage;
3. each object's UACP discriminator, Context id recomputation, and parent
   lineage (including V1/V2 scalar-parent ids);
4. complete parent closure against the advertised or already-local objects;
5. a residual secret scan over the portable payload; and
6. project identity unless `--allow-other-project` is explicit.

The full digest protects transport even though human-facing Context ids are
short. Context ids remain the stable DAG identifiers.

Project identity uses a truncated hash of the normalized source Git `origin`
URL when available. The URL itself is not stored in the remote manifest. If
one side has no origin, the detected package/project name is compared.

## File transport

A bare path or `file://` URL selects the file transport. The path must be
outside the project and may not contain the project directory; network-host
file URLs and unsafe symlinks are refused.

Pushes take an exclusive `.context-git.lock`, compare the manifest state seen
during negotiation, write immutable objects, and replace the manifest
atomically. A lock means another writer is active. If a writer terminated
abnormally, inspect the remote before manually removing a stale lock.

The file remote is suitable for a trusted shared disk or a folder synchronized
by another system. That system must preserve file contents and atomic renames.

## HTTPS transport contract

Given configured base URL `https://host/team/project`, a service implements:

| Method | Path | Required behavior |
|---|---|---|
| `GET` | `/team/project/manifest.json` | `200` JSON + `ETag`, or `404` before first push |
| `GET` | `/team/project/contexts/{context_id}.json` | `200` immutable JSON, or `404` |
| `PUT` | `/team/project/contexts/{context_id}.json` | create/idempotently accept identical bytes; reject different bytes with `409` |
| `PUT` | `/team/project/manifest.json` | honor `If-None-Match: *` initially and `If-Match: <etag>` later; reject stale updates with `409` or `412` |

Responses are bounded to 5 MiB per object/manifest, and manifests are bounded
to 10,000 objects. Redirects are deliberately not followed so bearer
credentials cannot cross the configured origin. External plain HTTP is
refused; `--allow-insecure-http` exists only for localhost/loopback testing.

Authentication is optional bearer auth configured by environment-variable
**name**, never by value:

```bash
export CONTEXT_GIT_TOKEN="value supplied by your secret manager"
context-git remote add origin https://contexts.example.com/team/project \
  --auth-env CONTEXT_GIT_TOKEN
```

Every HTTP request uses `Authorization: Bearer <value>`. A missing variable
fails closed. The token is not printed or serialized by Context Git.

## Service deployment checklist

- Require TLS and authorization for both reads and writes.
- Scope credentials to one tenant/project where possible.
- Treat object paths as immutable; never overwrite different bytes at an
  existing Context id.
- Generate strong ETags and enforce both conditional manifest headers.
- Enforce the same size/object-count limits server-side.
- Store manifest and objects durably; unreferenced objects from a raced push
  are safe and may be garbage-collected only after a retention window.
- Log ids and response codes, not Authorization headers or object bodies.
- Back up or version the manifest because forced updates can move refs
  backwards even though objects remain immutable.

## Deliberate V4 limits

V4 does not define remote ref deletion, garbage collection, shallow fetch,
delta transfer, object signing, end-to-end encryption, interactive credential
prompts, or conflict auto-resolution. These can be added without changing the
UACP Context Object format.

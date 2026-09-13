# Context Branch and Merge

V3 adds parallel lines of Agent working state without coupling them to source
Git branches. Context Objects stay immutable; a branch is only a movable ref.

## Store layout

```text
.context-git/
├── HEAD
├── refs/
│   └── heads/
│       ├── main
│       └── experiments/passkeys
└── contexts/
    ├── ctx_....json
    └── ctx_....json
```

`HEAD` is symbolic while on a branch and direct while detached. It also keeps
a cached context line so older Context Git releases can still find the active
object:

```text
ref: refs/heads/main
context: ctx_a1b2c3d4
```

Branch names are deliberately more conservative than Git ref names. Each
slash-separated component must start with an ASCII letter or digit and may
contain ASCII letters, digits, `.`, `_`, or `-`. Empty, `.`, `..`, `.lock`,
absolute, repeated-separator, whitespace, and path-escaping names are refused.

## Commands

```bash
context-git branch
context-git branch experiment [START]
context-git branch --delete experiment

context-git switch experiment
context-git switch --create experiment [--start START]

context-git merge SOURCE --dry-run
context-git merge SOURCE [--no-ff]
context-git merge SOURCE --resolve current_objective=ours
context-git merge SOURCE --resolve progress:auth=theirs
context-git merge SOURCE --resolve-all ours

context-git log --all
```

`START`, `SOURCE`, and read-only revision arguments accept a context id or
local context branch name. After V4 fetch, read/merge selectors also accept
`remote:NAME/BRANCH`; `HEAD` names the active context.

`branch --delete` deletes only the ref. It never deletes Context Objects.
`checkout CONTEXT_ID` creates a detached inspection state; `switch -c NAME`
is required before committing from it. Neither command touches Git state.

Remote-tracking refs under `refs/remotes/` are fetch results, not writable
local branches. Merge `remote:origin/main` into the current branch or create a
local branch at that revision; do not edit tracking-ref files manually.

## Merge rules

The engine finds the nearest common Context ancestor and handles each
agent-authored field with ordinary three-way rules:

| Base | Ours | Theirs | Result |
|---|---|---|---|
| A | A | B | B |
| A | B | A | B |
| A | B | B | B |
| A | B | C | conflict |

Items in lists are keyed by Unicode-normalized text (or decision/fact name).
Independent additions are combined. A removal wins when the other side is
unchanged. A removal versus a changed same-key item is a conflict.

Progress is merged per task identity. A task moving from `pending` to
`completed` on one branch merges cleanly if the other branch left it pending.
Different moves on both branches are a conflict, because silently choosing a
state could misrepresent completed work.

The merged notes include:

- `goal` and `current_objective`
- `progress.completed`, `in_progress`, and `pending`
- architecture facts and decisions
- constraints and do-not-change rules
- known issues and recommended actions
- required capabilities

Machine-observed fields are not merged. Project metadata, Git state,
important-file fingerprints, runtime environment and validation freshness are
captured again from the live repository when the merge context is written.

## Conflict safety

An unresolved merge exits with code `4`, prints stable conflict keys, and
writes nothing. `--dry-run` also writes nothing. A resolution chooses the full
field/item state from `ours`, `theirs`, or `base`; arbitrary replacement text
must be recorded in a follow-up context commit so it passes the normal input
and redaction path.

A successful divergent merge records:

```json
{
  "parent_context_id": "ctx_ours",
  "parent_context_ids": ["ctx_ours", "ctx_theirs"],
  "metadata": {
    "merge": {
      "base_context_id": "ctx_base",
      "source_context_id": "ctx_theirs",
      "source_ref": "experiment",
      "strategy": "three-way",
      "resolved_conflicts": ["current_objective"]
    }
  }
}
```

The first-parent field preserves UACP/1.0 compatibility. V3-aware readers use
both parent ids for ancestry and merge-base discovery.

## Fast-forward and unrelated histories

If the current tip is an ancestor of the source, the receiving ref advances
without creating an object. `--no-ff` forces an auditable two-parent merge
context. If the source is already an ancestor, the merge is a no-op.

Contexts with no common ancestor are refused by default. `--allow-unrelated`
uses an empty base and should be reserved for an intentional reconciliation of
separately initialized stores.

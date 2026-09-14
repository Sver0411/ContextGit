---
name: context-git
description: Git for AI Agent Context — version, branch, semantically merge, securely sync, send directed Agent handoffs, drift-check, explicitly import private sessions, and resume an agent's working state (UACP/1.0). Use when the user asks to save or sync progress, compare or merge parallel agent work, import a local agent session, hand off to another agent, resume/接手 a project, or inspect what changed between work sessions. Captures compressed working state (not chat history) into .context-git/, detects drift against the live repository, and produces agent-ready resume briefings.
---

# Context Git

You now have version control for *your own working context*. The store lives
at `.context-git/` in the project. One JSON Context Object per snapshot;
`HANDOFF.md` is only a rendered view. Protocol: **UACP/1.0**.

**Division of labour (do not blur it):**

* **You (the agent)** supply semantic knowledge: goal, current objective,
  completed/in-progress/pending, decisions with reasons, constraints,
  do-not-change list, architecture facts with evidence, known issues, next actions, validation results you
  have actually observed.
* **The CLI** captures machine-observable state: git snapshot, important-file
  fingerprints, drift detection, semantic diffs, secret redaction.

## Project root

Every command resolves the project root itself, so run it from anywhere inside
the project. Precedence: an explicit `--root PATH` always wins; otherwise the
nearest ancestor containing `.context-git/`; otherwise the containing Git
repository root; otherwise the working directory. This is also why `init` from
`repo/src/auth/` writes `repo/.context-git/` rather than nesting a second store.
Pass `--root` explicitly when a project deliberately holds more than one store.

## When to trigger

Export/save (create or update contexts):
- "保存当前上下文" / "context snapshot" / "context commit" / "checkpoint"
- "handoff" / "交接" / "交给另一个 Agent" / "生成交接"

Resume (adopt existing context):
- "resume" / "接手这个项目" / "读取 context" / "继续之前的工作" / "查看 Agent 进度"

Inspect:
- "context diff" / "what changed since last snapshot" / "context log" / "drift"
- "context branch" / "并行方案" / "merge agent context" / "合并上下文"
- "push/pull context" / "同步上下文" / "remote context" / "跨机器接手"
- "send context to agent" / "Agent inbox" / "把上下文发给另一个 Agent"

## Export workflow (snapshot / commit)

1. `python <skill_dir>/scripts/context_git.py status` — if no store: `init` first.
2. If a store exists with a HEAD, **read the previous context first**
   (`show`) and compute what actually changed since. Commit **incrementally**:
   only pass fields that changed — everything else is inherited from the
   parent context. To clear a list, pass it empty.
3. Compress ruthlessly (Context Compression — protocol.md §1):
   - BAD: "opened auth.ts, edited middleware, ran npm test, test failed, edited again"
   - GOOD: "Auth middleware implemented" + changed files + "npm test fails:
     refresh-token test incomplete" + remaining: "implement rotation"
   Keep **decisions and outcomes**, never the operation stream.
4. Run snapshot (first context) or commit (subsequent; refuses no-ops):
   ```
   python scripts/context_git.py commit --no-prompt \
     --set current_objective="..." \
     --set completed="Auth middleware,Login page" \
     --set in_progress="Refresh token rotation" \
     --set pending="Rate limiting" \
     --set architecture='[{"fact":"Auth uses cookie sessions","confidence":0.9,"source":"src/auth.ts"}]' \
     --set known_issues="Safari SameSite cookie issue" \
     --set decisions='[{"decision":"httpOnly cookie sessions","reason":"XSS-safe","rejected":["localStorage: XSS-prone"]}]' \
     --set constraints="Must support macOS","No backend" \
     --set do_not_change="Database schema" \
     --set validation.test=pass --set validation.build=pass \
     --set recommended_actions="Implement refresh token rotation endpoint" \
     -m "Auth middleware implemented"
   ```
   Always pass `--no-prompt` (you are the interactor, not the user's keyboard).
   Use `snapshot` for the first context in a store.
5. Verify your narrative fields landed: the tool warns about missing P0
   fields. Never leave goal/current_objective empty on a *handoff* commit —
   if the user didn't state a goal, ask once, or record it as unknown
   explicitly (do not guess — forbidden below).
6. Tell the user where the handoff lives: `.context-git/HANDOFF.md`.

## Resume workflow (new agent)

1. `python scripts/context_git.py resume` — prints drift report, capability
   compatibility, and the briefing. **The Context is the map; the live
   repository is the ground truth for code facts.** `resume` refuses to emit a
   briefing at all if the current Context fails integrity verification, so an
   exit code 7 means the store must be inspected (`verify`) before you act.
2. Read `.context-git/HANDOFF.md` only if you need the full narrative view.
3. If drift is NONE/LOW: continue from `recommended_actions` directly. NONE
   means the recorded *observed* evidence is still current on this machine — it
   does not upgrade agent-supplied claims into verified facts. Trust the fields
   the same way you would trust a colleague's notes: usable, but re-check
   anything load-bearing against the code before you change behaviour.
4. If drift is MEDIUM/HIGH: the report lists stale files and stale sections —
   re-read **only those files**, re-run the stale validation, then commit a
   fresh context noting what you re-verified. Never silently trust sections
   the drift engine marked stale.
5. Do **not** re-explore the repository from scratch. Only read code when a
   needed fact is genuinely absent from the context.

Index-state-only drift (`unstaged → staged` with identical file contents) is
reported at LOW and never expires a validation result: `git add` does not
change the bytes that were built or tested.

## V2 session import (only on explicit request)

Never inspect an agent's private session store implicitly. When the user asks
to import a session:

1. Prefer an explicit session file and preview it first:
   `python scripts/context_git.py import-session FILE --dry-run`.
2. Use `sessions --agent NAME` or `import-session --latest --agent NAME` only
   when the user explicitly asks for discovery/latest-session import.
3. Review the redacted extracted fields, then use repeatable `--set KEY=VALUE`
   corrections while saving. The importer discards raw messages, system and
   developer prompts, tools and reasoning by design.
4. Run `verify` after saving and report the new context id.

Use `capabilities --json` when the user needs the V2 observed/declaration
profile. A `declared` capability is not the same as an observed one.

## V3 branch / merge workflow

Context branches are independent from source Git branches. `switch`,
`checkout`, and `merge` must never modify repository files, the Git index, or
Git refs.

1. Create an experimental line with `switch -c NAME` (or `branch NAME`, then
   `switch NAME`). Commit normal incremental contexts on that branch.
2. Return to the receiving branch with `switch main` and preview using
   `merge SOURCE --dry-run`.
3. A fast-forward is safe when the receiving tip is an ancestor. A divergent
   merge uses the nearest common Context ancestor and combines semantic fields.
4. Never guess through a conflict. The command exits 4 and prints stable field
   keys. Ask the user when the correct outcome is not already explicit; then
   repeat with `--resolve KEY=ours|theirs|base`. Use `--resolve-all` only when
   the user has clearly chosen one side as authoritative.
5. Verify the resulting two-parent context with `show` and `log --all`, then
   run `verify`. Observed project/Git/file evidence is freshly captured at the
   merge; it is not copied from either branch.

`checkout <context-id>` deliberately detaches context HEAD. Create a branch
with `switch -c NAME` before committing. Deleting a context branch deletes only
its ref; immutable context objects remain in `contexts/`.

## V4 remote sync workflow

Context remotes are separate from source Git remotes. Use remote operations
only when the user asks to publish, fetch, pull, sync, or transfer Context
state; ordinary snapshot/resume commands remain local.

1. Inspect configured endpoints with `remote` / `remote show NAME`. Add a
   local shared directory outside the project, or an HTTPS endpoint with
   `remote add origin URL`. For auth, pass `--auth-env NAME`; never put a
   token value in a URL, command, config, Context field, or response.
2. Before publishing, run `verify`, then `push [REMOTE] [BRANCH] --dry-run`.
   A real push sends portable Context Objects only — no source files or raw
   sessions — and updates a separate Context remote ref.
3. Use `fetch` when the user wants to inspect remote work without changing a
   local branch. Inspect with `branch --all`, `show remote:NAME/BRANCH`, or a
   merge preview.
4. Use `pull` only on the same-named current Context branch. It may
   fast-forward but never auto-merges divergence. On divergence, preview
   `merge remote:NAME/BRANCH --dry-run`, obtain explicit choices for real
   conflicts, merge, verify, and then push.
5. Never use `push --force` unless the user explicitly chose to rewrite the
   remote Context ref. Immutable remote objects remain, but other agents may
   lose the advertised branch tip.
6. After fetching on another machine, run `status` or `resume`; observed
   evidence is re-evaluated against that machine's working tree.

A file remote serialises writers with `O_CREAT|O_EXCL` on
`.context-git.lock`, and the lock body records pid, host, operation and time.
A crashed or SIGKILLed writer leaves that file behind. Never delete it by
hand and never assume an old lock is dead — inspect it first with
`remote show` (it prints the writer lock) and clear it deliberately with
`remote unlock REMOTE`, which refuses unless the pid is provably gone on this
host. `--force` is the explicit override for a lock written on another host or
one whose liveness cannot be probed.

External plain HTTP is forbidden. `--allow-insecure-http` is only for an
explicit localhost test service. `--allow-other-project` is likewise an
explicit override, not a recovery default. Remote failures exit 5 and should
be resolved without hand-editing manifests, objects, or refs.

## V5 Agent Context Network workflow

The network is asynchronous coordination over a configured V4 Context remote;
it does not launch agents or send chat messages.

1. Publish the Context branch with `push`, then register the local identity:
   `network register AGENT_ID --agent NAME`. Treat `--force` as an intentional
   administrative takeover and use it only when the user explicitly chooses
   that outcome.
2. Inspect `network agents` before sending. `network send RECIPIENT` publishes
   the current branch tip, compact intent and recipient compatibility. Never
   put prompts, transcripts, credentials, source content or tool output in
   `--message`.
3. A recipient runs `network inbox`; this first fetches and verifies the V4
   Context graph, then the complete network object set. Run
   `network accept ID --switch` to create a local `handoff/SENDER/...` Context
   branch.
4. Publish progress with `network reply ID accepted|completed|rejected`.
   `completed` and `rejected` are terminal. The sender inspects immutable
   receipts with `network status [ID]`.
5. After acceptance, run `status` or `resume` on the handoff branch and obey
   normal drift/freshness rules before changing project code.

Agent ids are not cryptographic identities. Trust derives from the configured
remote's TLS, bearer scope and access control. Network protocol failures exit
6; do not hand-edit manifests, handoffs, receipts or the local cache.

## Drift / status / diff / log

- `status` — is the current context still fresh? (run before resuming work)
- `diff [old] [new]` — semantic diff; 0 args = HEAD vs parent, 1 arg = that
  context vs its parent
- `log` — context history with summaries
- `log --all` — all stored contexts with branch decorations and merge parents
- `show [id]` / `checkout <id|->` — inspect or move the context HEAD
  (checkout never touches the user's git repo)
- `branch [NAME] [START]` / `branch -d NAME` — list, create, or delete refs
- `switch NAME` / `switch -c NAME [--start REV]` — change context branch
- `merge SOURCE [--dry-run]` — fast-forward or three-way semantic merge
- `remote [add|show|remove|unlock]` — manage Context-only remote endpoints
- `push [REMOTE] [BRANCH]` — publish an immutable Context DAG and branch tip
- `fetch [REMOTE]` — verify objects and refresh remote-tracking refs
- `pull [REMOTE] [BRANCH]` — fetch and fast-forward only
- `verify [ctx_id]` — integrity (Context id, lineage, DAG refs) + secret scan
- `network register|agents|send|inbox|accept|reply|status` — directed Agent handoffs

## Security rules (hard)

- Never disable or bypass the redaction step; if a snapshot aborts with a
  `[REDACTED]` finding, remove the secret from your inputs and retry.
- Never read `.env*`, key files, or credential stores into your notes —
  even redacted, even "just to check".
- Never read private sessions unless the user explicitly requested session
  import or discovery. Ordinary context operations must remain session-blind.
- The tool writes only inside `.context-git/`. If you find yourself editing
  project files "for the handoff", stop — that is a violation.
- Remote sync is the only exception to local-only writes: when explicitly
  requested, it and V5 network commands may write the configured Context
  remote. They must never mutate source Git state or upload project/session
  content.

## Failure handling

- No store → `init`, then snapshot.
- Not a git repo / git missing / empty repo → contexts still work; git block
  records the reason. Mention degraded drift detection to the user.
- `commit` refuses ("nothing to commit") → nothing semantic changed; use
  `--allow-empty` only for explicit checkpoints.
- Corrupt context JSON → treat as missing; re-snapshot; do not hand-edit
  stored contexts.
- Remote non-fast-forward → fetch and semantic-merge the tracking ref; never
  force unless the user explicitly chose ref replacement.
- Remote credential missing → ask the user to supply the configured
  environment variable outside Context Git; never request or store its value.
- Network id already registered → do not take it over silently; use another id
  or obtain explicit user direction before `network register --force`.
- Large repos → fine: collection is git-driven and ignore-listed
  (node_modules, dist, .venv, … are never walked); files above 5 MiB and
  symbolic links are not fingerprinted.

## Prohibited behaviours (all hard rules)

1. No chat history, no chain-of-thought, no internal reasoning in any field.
2. No full git diffs — the CLI already records path+stats only; describe
   crucial changes in one natural sentence instead.
3. No guessing to fill fields. Empty/unknown beats invented. If the user's
   goal is unclear, ask or leave it out — never fabricate.
4. No dumping the repo into `important_files` (5–20 files, with reasons).
5. No trusting stale sections on resume; re-verify what drift flags.
6. No modifying project code to make handoff easier.
7. No source `git commit` / `git push` / repository mutation. A requested
   `context-git push` writes only the configured Context remote.
8. No secrets in any field — the tool aborts, do not retry around it.
9. No re-analysing the whole codebase on resume — the context is the map.

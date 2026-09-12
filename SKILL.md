---
name: context-git
description: Git for AI Agent Context — version, diff, drift-check and resume an agent's working state (UACP/1.0). Use when the user asks to save progress, hand off to another agent, resume/接手 a project, or inspect what changed between work sessions. Captures compressed working state (not chat history) into .context-git/, detects drift against the live repository, and produces agent-ready resume briefings.
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

## When to trigger

Export/save (create or update contexts):
- "保存当前上下文" / "context snapshot" / "context commit" / "checkpoint"
- "handoff" / "交接" / "交给另一个 Agent" / "生成交接"

Resume (adopt existing context):
- "resume" / "接手这个项目" / "读取 context" / "继续之前的工作" / "查看 Agent 进度"

Inspect:
- "context diff" / "what changed since last snapshot" / "context log" / "drift"

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
   compatibility, and the briefing. **Trust the briefing, not the repo.**
2. Read `.context-git/HANDOFF.md` only if you need the full narrative view.
3. If drift is NONE/LOW: continue from `recommended_actions` directly.
4. If drift is MEDIUM/HIGH: the report lists stale files and stale sections —
   re-read **only those files**, re-run the stale validation, then commit a
   fresh context noting what you re-verified. Never silently trust sections
   the drift engine marked stale.
5. Do **not** re-explore the repository from scratch. Only read code when a
   needed fact is genuinely absent from the context.

## Drift / status / diff / log

- `status` — is the current context still fresh? (run before resuming work)
- `diff [old] [new]` — semantic diff; 0 args = HEAD vs parent, 1 arg = that
  context vs its parent
- `log` — context history with summaries
- `show [id]` / `checkout <id|->` — inspect or move the context HEAD
  (checkout never touches the user's git repo)

## Security rules (hard)

- Never disable or bypass the redaction step; if a snapshot aborts with a
  `[REDACTED]` finding, remove the secret from your inputs and retry.
- Never read `.env*`, key files, or credential stores into your notes —
  even redacted, even "just to check".
- The tool writes only inside `.context-git/`. If you find yourself editing
  project files "for the handoff", stop — that is a violation.

## Failure handling

- No store → `init`, then snapshot.
- Not a git repo / git missing / empty repo → contexts still work; git block
  records the reason. Mention degraded drift detection to the user.
- `commit` refuses ("nothing to commit") → nothing semantic changed; use
  `--allow-empty` only for explicit checkpoints.
- Corrupt context JSON → treat as missing; re-snapshot; do not hand-edit
  stored contexts.
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
7. No `git commit` / `git push` / any repository mutation.
8. No secrets in any field — the tool aborts, do not retry around it.
9. No re-analysing the whole codebase on resume — the context is the map.

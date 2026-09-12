# V2 Agent Adapter Reference

Context Git adapters contain only detection markers, declared capabilities,
and private-session discovery locations. Session access is opt-in: loading an
adapter does not read its paths.

## Support matrix

| Adapter | Discovery scope | Visible-message forms | Notes |
|---|---|---|---|
| `codex` | `$CODEX_HOME/sessions/**` or `~/.codex/sessions/**`; embedded `cwd` must match | `response_item.message`, `event_msg.user_message`, `event_msg.agent_message` | Other rollout records are ignored |
| `claude-code` | `$CLAUDE_CONFIG_DIR/projects/**` or `~/.claude/projects/**`; embedded `cwd` must match | top-level user/assistant JSONL messages and text content blocks | Tool/thinking blocks are ignored |
| `cursor` | `$AGENT_TRANSCRIPTS` or workspace-slug-scoped `~/.cursor/projects/**/agent-transcripts/**` | nested role/message JSONL and text exports | Workspace slug is derived before a transcript is opened |
| `gemini-cli` | SHA-256 project directory under `~/.gemini/tmp/**/chats/` | user/gemini JSONL; legacy JSON arrays | `thoughts` and non-message records are ignored |
| `opencode` | `$XDG_DATA_HOME/opencode/opencode.db` or the default data directory; `session.directory` must match | SQLite `message` role plus `part` rows whose type is `text` | Opened with SQLite `mode=ro` and `query_only=ON` |
| `generic` | no automatic discovery | explicit role/content JSON, JSONL, Markdown, or text | Stable fallback for exported sessions |

## Upstream format evidence

These formats are third-party implementation details, so adapters are tested
defensively and unsupported records are ignored:

- [OpenAI Codex rollout recorder tests](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder_tests.rs)
- [Gemini CLI session import and JSONL writer](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/gemini.tsx)
- [Cursor workspace transcript documentation](https://github.com/cursor/plugins/blob/main/pstack/skills/recall/SKILL.md)
- [OpenCode SQLite session/message/part schema](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.sql.ts)
- [Claude Code project JSONL location evidence](https://github.com/anthropics/claude-code/issues/43110)

If an upstream shape changes, pass an explicit exported JSON/JSONL/text file
and use `--dry-run`. A parser failure never falls back to copying raw records.

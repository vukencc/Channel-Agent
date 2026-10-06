# Project-scoped Codex subagents

This directory customizes Codex for the Channel-Agent (ai-agent-startup) repository.

- `.codex/agents/*.toml` — one file per custom subagent (required fields: `name`,
  `description`, `developer_instructions`). Codex loads these by name when it spawns
  a subagent.
- `.codex/config.toml` — subagent limits (`[agents] max_threads`, `max_depth`) and the
  `project_doc_max_bytes` budget for the `AGENTS.md` chain.
- Dispatch policy (when to use which subagent) — the "Multi-agent Workflow" section in
  the repository root [`AGENTS.md`](../AGENTS.md).
- Design notes and extension guide — [`docs/codex-multiagent.md`](../docs/codex-multiagent.md).

To add a subagent: create `.codex/agents/<name>.toml` with the required fields, then add a
row to the roster table in `AGENTS.md`.

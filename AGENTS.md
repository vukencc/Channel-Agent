# Repository Guidelines

## Project Structure & Module Organization

- `src/ai_agent_startup/` is the application package: `main.py` (console entry), `config.py`, `core/` (agent loop, model calls, sessions, persistence), `tools/` (registration, file operations, sandbox), `rag/` (chunking, embeddings, retrieval, reranking). Knowledge-base documents live in `data/raw/`.
- `tests/` contains pytest tests and shared fixtures. `dev/` holds diagnostics, performance benchmarks, RAG data preparation and historical reports. `crud_tests/` is the runtime tool sandbox, not the test suite; `logs/` holds generated logs.

## Build, Test, and Development Commands

Use Python 3.12+ and run commands from the repository root:

- `uv sync --locked`: install locked dependencies, including the development group.
- `cp .env.example .env`: create local configuration if `.env` does not already exist; populate model credentials and service settings.
- `uv run ai-agent-startup`: launch the interactive agent.
- `uv run pytest`: run the full test suite.
- `uv run pytest tests/test_command.py -q`: run focused command-tool tests.

Installable application (`hatchling` + `[project.scripts]`); the console entry is `ai-agent-startup`, and `uv run pytest` uses the installed package.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` modules/functions/variables, `PascalCase` classes, and uppercase configuration constants. Use type hints for new interfaces and concise docstrings for non-obvious behavior. No formatter or linter is configured in `pyproject.toml`.

Define new tools in `src/ai_agent_startup/tools/<name>.py` using a Pydantic argument model and `register_tool`; import the module in `src/ai_agent_startup/tools/__init__.py` to register it. Keep tool-specific behavior outside the generic agent loop.

## Testing Guidelines

Name files `tests/test_<feature>.py` and functions `test_<behavior>`. Use pytest fixtures, `tmp_path`, and `monkeypatch`; reuse `sandbox_env` for isolated file/command tests. Mock external services where practical. Cover relevant validation failures, retry behavior, sandbox boundaries, denied confirmations, and timeouts. No minimum coverage threshold is configured.

## Commit & Pull Request Guidelines

`main` is the protected template branch. Start new work from the current baseline tag (`git switch -c feat/<topic> <tag>`); do not push directly to `main`. Use concise prefixes such as `feat:`, `fix:`, `perf:`, `docs:`, `ci:`, `chore:`. PRs should explain the behavior change, link relevant issues, list validation commands/results, and identify configuration or dependency changes. Update `uv.lock` alongside dependency edits. Release/tagging steps live in `docs/release.md`; container usage in `docs/docker.md`.

## Security & Configuration

Keep credentials in ignored `.env`; document new settings in `.env.example`. Preserve sandbox checks, confirmation handling, and audit logging. Do not commit generated logs, vector databases, or sandbox output.

## Multi-agent Workflow (Codex subagents)

This repository defines project-scoped Codex subagents in `.codex/agents/`, with limits in
`.codex/config.toml`. This section is a standing instruction: when a task matches a trigger
below, delegate that slice of work to the named subagent instead of doing it on the main
thread. Codex only spawns subagents when a request or applicable `AGENTS.md`/skill
instruction asks for it, so treat these rules as that explicit request.

Design notes live in [docs/codex-multiagent.md](docs/codex-multiagent.md).

### Roster

| Subagent | Model / effort | Sandbox | Delegate when |
| --- | --- | --- | --- |
| `repo_explorer` | gpt-6-luna / low | read-only | You need to locate or trace code, map a call path, or gather file:line evidence before editing. |
| `rag_specialist` | gpt-6-sol / high | inherit | The change touches `src/ai_agent_startup/rag/**` or `tools/rag_search.py`, or involves retrieval quality, indexing, or RAG latency. |
| `sandbox_auditor` | gpt-6-astra / high | read-only | The change touches `tools/sandbox.py`, `tools/command*.py`, `tools/file_*.py`, `tools/network_*.py`, `core/permissions.py`, `core/audit_writer.py`, or `core/budgets.py`, or you need a boundary/security review. |
| `test_engineer` | gpt-6-sol / medium | inherit | A behavior change needs regression tests, or failing tests need diagnosis and a fix. |
| `reviewer` | gpt-6-astra / high | read-only | An implementation is ready for a defect-first review of its diff against the base branch. |
| `docs_writer` | gpt-6-luna / medium | inherit | Docs, `.env.example`, `README.md`, `CONTRIBUTING.md`, or `CHANGELOG.md` must change to match a code change. |
| `perf_analyst` | gpt-6-luna / medium | inherit | You need measured before/after numbers for a performance change (RAG, sandbox, or CLI). |

### How to dispatch

- Spawn by role name, for example "spawn `rag_specialist` to ...". Give the concrete task,
  the exact files and commands to use, and the expected output.
- Keep context lean: pass `fork_turns = "none"` (or a small number) unless the subagent
  genuinely needs the parent transcript. A fresh context keeps the agent's pinned
  model/effort effective and avoids polluting it with unrelated history.
- Run independent read-only tasks in parallel, but keep each batch to at most three
  workers (Codex currently exposes four concurrency slots including the root agent).
- Never run two writers against the same file or directory at once: all agents share one
  working tree, so edits are visible immediately and can conflict. Serialize writes.

### Do not delegate

- Small, tightly coupled changes you can finish in a few tool calls.
- Single-file edits with an obvious fix.
- Steps that depend on the previous step's result; keep sequential reasoning on the main
  thread.
- Work that needs the running context you already hold.

### Output contract

Subagents return distilled results, not raw transcripts:

- Findings: `path/to/file.py:line` plus one line of relevance, ordered by importance.
- Changes: files touched, tests run, exact commands, and final status.
- Reviews: findings first (`[P0]`-`[P3]`), then a short assessment and residual risks.

After a subagent edits code, the main thread owns final verification: run
`uv run pytest -m 'not integration' -q` and, when tool or sandbox boundaries changed,
`uv run python dev/ci_sandbox_probe.py`.

### Compatibility fallback

If a runtime cannot resolve a custom agent by name, spawn the built-in `worker` (or
`explorer` for read-only work) and paste the matching `developer_instructions` from that
agent's `.codex/agents/<name>.toml` into the task.

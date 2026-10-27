# Repository Guidelines

## Start Here

Read [docs/session-handoff.md](docs/session-handoff.md), then [docs/README.md](docs/README.md) and the relevant current guide. Run `git status --short`, `git branch --show-current`, and `git log --oneline -5` before editing. This checkout contains substantial uncommitted work; preserve it. Source code and current guides take precedence over historical reports. Do not reset, clean, switch to an old tag, or re-create the environment to recover missing context.

## Layout and Development

- `src/agent_core/`: runtime core, tools and RAG; no UI or Debug implementation imports. `src/agent_test/`: diagnostic adapters and Debug UI. `src/agent_frontend/`: CLI, WebUI and application composition. See `docs/architecture.md`.
- `src/agent_test/tests/` is the pytest suite. `src/agent_test/scripts/` contains distributed diagnostics; `docs/dev/` contains local benchmarks and archives. `crud_tests/` is the runtime workspace, not a test directory.
- From the Linux repository root: `uv sync --locked --extra web`, then `uv run --no-sync ai-agent-startup` or `uv run --no-sync ai-agent-web`. Create `.env` from `.env.example` only if absent. Do not overwrite credentials.
- On Windows with a WSL/UNC checkout, invoke Linux uv, Python and Node through WSL. Never run Windows uv against this `.venv`. Resolve Node through the Linux/nvm PATH before declaring it unavailable; host-specific commands are in the handoff.

## Coding and Safety

Use four spaces, type hints, `snake_case` functions/modules, `PascalCase` classes, and Chinese comments/documentation. Define tools with Pydantic arguments and `register_tool`; register modules through `tools/__init__.py`. Keep tool behavior out of the generic agent loop. No formatter or linter is configured.

Preserve sandbox fail-closed behavior, path boundaries, the selected permission policy, required confirmations, atomic writes, audit logging and tool/result pairing. Never relax them to pass tests. Do not modify or delete user data in `.agent/`, `crud_tests/`, `data/raw/`, `logs/` or existing `.cache/` contents; put new validation artifacts in a unique `.cache/reports/` directory or a temporary directory. Never print or commit keys, conversations, logs or downloaded models.

## Verification and Delivery

Reproduce bugs with a failing test before fixing them. Use `src/agent_test/tests/test_<feature>.py`, fixtures, `tmp_path` and `monkeypatch`; reuse `sandbox_env` for tool tests. Do not delete/weaken existing tests or fabricate model responses, tool results or benchmark numbers.

After syncing dependencies, run focused tests and `uv run --no-sync pytest -m 'not integration' -q`. Tool/sandbox changes also require `uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe`; report environment failures honestly. DEBUG UI changes require actual browser checks with multiple Agents, exceptions, local details and a narrow viewport. Distinguish real model execution, saved-trace replay and controlled UI fixtures. Dependency edits require `uv lock` and locked sync; behavior/config changes require current docs and `.env.example` updates.

Keep `main` protected. Continue the existing branch unless a new branch is needed; use `codex/<topic>` and preserve local changes. Use `fix:`, `feat:`, `perf:`, `docs:`, `ci:` or `chore:` commits. PRs explain behavior, validation and configuration/dependency changes. Do not stage unrelated work or generated artifacts. Update the handoff when material state changes.

## Codex Delegation

Use named specialists for independent substantial work; skip delegation for a small, obvious edit. Up to three workers may run concurrently, subject to the runtime limit. Assign disjoint file ownership and tell writers they share the checkout. Children return follow-ups to the parent instead of spawning more agents. The parent owns integration and final verification.

| Role | Responsibility |
| --- | --- |
| `repo_explorer` | Read-only code paths and file/line evidence before editing. |
| `rag_specialist` | `rag/**`, `tools/rag_search.py`, indexing, retrieval and reranking. |
| `sandbox_auditor` | Read-only sandbox, permissions, command/file/network tools, audit and budget review. |
| `test_engineer` | Reproduction and regression coverage; no weakened assertions. |
| `reviewer` | Read-only defect-first review of completed changes. |
| `docs_writer` | Current guides, README, changelog, examples and documentation index. |
| `perf_analyst` | Reproducible before/after measurement with uncertainty. |
| `implementation_worker` | Bounded code changes with explicit file ownership and acceptance checks. |
| `ui_specialist` | Native/Web UI reproduction, visual and interaction fixes within the assigned layout. |
| `architecture_advisor` | Read-only escalation for a concrete unresolved cross-module decision. |

Models and effort live only in `.codex/agents/*.toml`. Prefer `fork_turns="none"` with a concrete scope; use `"all"` only when shared history is necessary. Return findings, changed files, commands/results and residual risks, not raw transcripts. If custom roles are unavailable, use a built-in worker/explorer with the relevant role instructions. Read-only is an instruction-level duty; custom roles inherit the parent sandbox and cannot grant themselves permissions. Codex delegation and the application's own sub-Agent system are separate; see [docs/codex-multiagent.md](docs/codex-multiagent.md).

Choose `implementation_worker` for a clearly specified fix and `ui_specialist` for work that needs screenshots or UI interaction. Use the RAG, test, performance, review and sandbox specialists for their corresponding reasoning-heavy work. Reserve `architecture_advisor` for an unresolved decision after focused evidence collection; do not add an architecture pass to routine changes. A named role's configured model and effort take precedence over spawn overrides; select the right role instead of assuming an override changes it.

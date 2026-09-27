# Repository Guidelines

## Project Structure & Module Organization

- `main.py` starts the interactive, streaming agent; `config.py` loads environment settings.
- `core/` contains the agent loop, model calls, message structures, and logging.
- `tools/` provides tool registration, web/RAG search, file operations, and sandboxed commands.
- `rag/` handles document chunking, embeddings, retrieval, and answer assessment. Knowledge-base documents live in `data/raw/`.
- `dev/tests/` contains pytest tests and shared fixtures. `crud_tests/` is the runtime tool sandbox, not the test suite; `logs/` holds generated logs.

## Build, Test, and Development Commands

Use Python 3.12+ and run commands from the repository root:

- `uv sync --locked`: install locked dependencies, including the development group.
- `cp .env.example .env`: create local configuration if `.env` does not already exist; populate model credentials and service settings.
- `uv run main.py`: launch the interactive agent.
- `uv run pytest`: run the full test suite.
- `uv run pytest dev/tests/test_command.py -q`: run focused command-tool tests.

This is a script application (`tool.uv.package = false`); no packaging build step is configured.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` modules/functions/variables, `PascalCase` classes, and uppercase configuration constants. Use type hints for new interfaces and concise docstrings for non-obvious behavior. No formatter or linter is configured in `pyproject.toml`.

Define new tools in `tools/<name>.py` using a Pydantic argument model and `register_tool`; import the module in `tools/__init__.py` to register it. Keep tool-specific behavior outside the generic agent loop.

## Testing Guidelines

Name files `dev/tests/test_<feature>.py` and functions `test_<behavior>`. Use pytest fixtures, `tmp_path`, and `monkeypatch`; reuse `sandbox_env` for isolated file/command tests. Mock external services where practical. Cover relevant validation failures, retry behavior, sandbox boundaries, denied confirmations, and timeouts. No minimum coverage threshold is configured.

## Commit & Pull Request Guidelines

History is limited to an initial Chinese description and a `feat:` commit. Prefer concise, descriptive messages with prefixes such as `feat:` or `fix:`. PRs should explain the behavior change, link relevant issues, list validation commands/results, and identify configuration or dependency changes. Update `uv.lock` alongside dependency edits.

## Security & Configuration

Keep credentials in ignored `.env`; document new settings in `.env.example`. Preserve sandbox checks, confirmation handling, and audit logging. Do not commit generated logs, vector databases, or sandbox output.

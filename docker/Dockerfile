# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# 固定 uv 版本；升级时同步修改此版本并重新验证。
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

# 命令沙箱依赖：bubblewrap + prlimit（util-linux）；容器内可用性见 docs/docker.md。
RUN apt-get update \
 && apt-get install -y --no-install-recommends bubblewrap util-linux ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 默认仅安装运行依赖；测试/可选功能通过构建参数开启，例如：
#   --build-arg UV_SYNC_ARGS="--dev --extra documents"
ARG UV_SYNC_ARGS="--no-dev"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project ${UV_SYNC_ARGS}

COPY . .
RUN uv sync --locked ${UV_SYNC_ARGS}

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="ai-agent-startup" \
      org.opencontainers.image.revision=$GIT_SHA \
      org.opencontainers.image.source="https://github.com/vukencc/ai-agent-startup"

# 运行数据统一落在 /data，便于挂载卷持久化。
ENV AGENT_STATE_DIR=/data/state \
    SANDBOX_DIR=/data/sandbox \
    RAG_CACHE_DIR=/data/rag-cache \
    AUDIT_LOG=/data/logs/audit.log \
    DOC_DIR=/app/data/raw
VOLUME ["/data"]

ENTRYPOINT ["uv", "run", "--no-sync", "ai-agent-startup"]

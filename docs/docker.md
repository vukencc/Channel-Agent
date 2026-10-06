# Docker 使用指南

镜像保存的是**可运行环境**，源码版本仍以 git tag 为准；构建参数 `GIT_SHA` 会把镜像关联到提交。

## 快速开始（开包即用）

### 方式 A：从 GHCR 拉取（推荐给其他人）

```bash
cp .env.example .env            # 1) 填好 OPENCODE_API_KEY 等
docker pull ghcr.io/vukencc/channel-agent:v0.2.1   # 2) 拉取镜像（约 2.3GB）

# 3) 运行交互式 TUI
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox -v ai-agent-rag:/data/rag-cache \
  ghcr.io/vukencc/channel-agent:v0.2.1

# 或 headless 单次执行
docker run --rm --env-file .env -v ai-agent-state:/data/state \
  ghcr.io/vukencc/channel-agent:v0.2.1 --prompt "只回复：你好" --json
```

GHCR 包若为私有：`echo $GITHUB_TOKEN | docker login ghcr.io -u <用户名> --password-stdin`，
或在 GitHub 的 Package 设置里改为 public。

### 方式 B：从归档文件加载（离线/内网）

```bash
docker load < ai-agent-startup-v0.2.1-image.tar.gz
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox -v ai-agent-rag:/data/rag-cache \
  ai-agent-startup:v0.2.1
```

### 方式 C：用 compose 一条命令

```bash
cd docker && docker compose up         # 或 docker compose -f docker/docker-compose.yml up
```

compose 已默认开启 `seccomp=unconfined`（容器内命令沙箱需要）并挂载三个数据卷。

### 首次运行说明

- **命令沙箱**：容器内 `run_command` 需要 `--security-opt seccomp=unconfined`（compose 已配置）；
  不加也能用文件工具与 RAG。
- **RAG 模型**：首次调用 `rag_search` 会自动下载 embedding/reranker 模型到 `/data/rag-cache`
  （数百 MB~数 GB，需要网络）。离线环境请预先准备该目录并挂载。

## 前置

- Docker Engine 或 Docker Desktop（WSL2 需在 Docker Desktop 中开启对应发行版的 WSL Integration）。
- `.env` 已按 `.env.example` 填好；**不要**把 `.env` 打进镜像（`.dockerignore` 已排除）。

## 构建

```bash
docker build -f docker/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t ai-agent-startup:v0.2.0 .
# 需要测试或可选功能：
docker build -f docker/Dockerfile --build-arg UV_SYNC_ARGS="--dev --extra documents" -t ai-agent-startup:test .
# GPU 环境：torch 索引由 pyproject.toml 的 [tool.uv.sources] 固定为 CPU；
# 需要 CUDA 时编辑该处（如 pytorch-cu126）并重新执行 `uv lock`。
# 国内网络可改用 PyPI 镜像源：
docker build -f docker/Dockerfile --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t ai-agent-startup:v0.2.0 .
```

镜像默认安装 **CPU 版 torch**（体积约 1~2GB，显著小于 CUDA 版）；需要 GPU 重排时用 `UV_TORCH_BACKEND=cu126` 等覆盖。

## 运行

交互式 TUI（全屏界面必须 `-it`）：

```bash
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state \
  -v ai-agent-sandbox:/data/sandbox \
  -v ai-agent-rag:/data/rag-cache \
  ai-agent-startup:v0.2.0
```

headless 单次执行（默认拒绝一切待确认操作；需要写操作时显式 `--policy trusted` 并自行评估风险）：

```bash
docker run --rm --env-file .env \
  -v ai-agent-state:/data/state \
  ai-agent-startup:v0.2.0 --prompt "把 hello 保存到 hello.txt 并读回" --json
```

平台/沙箱能力说明：

```bash
docker run --rm ai-agent-startup:v0.2.0 --platform
```

## 模型缓存与知识库

RAG 首次使用会下载本地模型（约数 GB 量级）。复用宿主已有缓存可避免重复下载：

```bash
docker run --rm -it --env-file .env \
  -v "$PWD/.cache/rag:/data/rag-cache" \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox \
  ai-agent-startup:v0.2.0
```

知识库默认使用镜像内的 `/app/data/raw`；要挂载自己的语料：

```bash
-v "$PWD/data/raw:/app/data/raw:ro"
```

## 命令沙箱（run_command）在容器内

`run_command` 依赖 bwrap 创建用户/网络命名空间，Docker 默认 seccomp 会拦截。先验证：

```bash
docker run --rm --entrypoint uv ai-agent-startup:v0.2.0 \
  run --no-sync python scripts/ci_sandbox_probe.py
```

失败时依次尝试：

```bash
--security-opt seccomp=unconfined        # 通常足够
--cap-add SYS_ADMIN                      # 仍失败时
--privileged                             # 最后手段，不建议用于对外服务
```

若环境始终不支持：保持 fail-closed，命令工具报错，CRUD/RAG 仍可用；不要为了跑通命令把容器改成特权模式还对外暴露。

## docker compose

```bash
GIT_SHA=$(git rev-parse --short HEAD) TAG=v0.2.0 docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml run --rm agent            # TUI
docker compose -f docker/docker-compose.yml run --rm agent --prompt "..." --json
```

需要命令沙箱时取消 `docker/docker-compose.yml` 中 `security_opt` 的注释，并用探针验证。

## 保存、分发与发布

```bash
# 离线归档
docker save ai-agent-startup:v0.2.0 | gzip > ai-agent-startup-v0.2.0-image.tar.gz
docker load < ai-agent-startup-v0.2.0-image.tar.gz

# 推送到 GitHub 容器仓库（token 需 write:packages）
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <用户名> --password-stdin
docker tag ai-agent-startup:v0.2.0 ghcr.io/vukencc/channel-agent:v0.2.0
docker push ghcr.io/vukencc/channel-agent:v0.2.0
```

推 `v*` 标签会触发 `.github/workflows/release.yml`：离线回归 + 沙箱探测 → 构建并推送 GHCR → 创建 GitHub Release。

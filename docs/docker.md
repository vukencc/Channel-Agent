# Docker 使用指南

镜像保存的是**可运行环境**，源码版本仍以 git tag 为准；构建参数 `GIT_SHA` 会把镜像关联到提交。

## 前置

- Docker Engine 或 Docker Desktop（WSL2 需在 Docker Desktop 中开启对应发行版的 WSL Integration）。
- `.env` 已按 `.env.example` 填好；**不要**把 `.env` 打进镜像（`.dockerignore` 已排除）。

## 构建

```bash
docker build -f docker/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t ai-agent-startup:v0.2.0 .
# 需要测试或可选功能：
docker build -f docker/Dockerfile --build-arg UV_SYNC_ARGS="--dev --extra documents" -t ai-agent-startup:test .
```

首次构建会下载 Python 依赖（含 torch CPU 轮子），镜像通常 1~3GB。

## 运行

交互式 TUI（全屏界面必须 `-it`）：

```bash
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state \
  -v ai-agent-sandbox:/data/sandbox \
  -v ai-agent-rag:/data/rag-cache \
  ai-agent-startup:v0.1.0
```

headless 单次执行（默认拒绝一切待确认操作；需要写操作时显式 `--policy trusted` 并自行评估风险）：

```bash
docker run --rm --env-file .env \
  -v ai-agent-state:/data/state \
  ai-agent-startup:v0.1.0 --prompt "把 hello 保存到 hello.txt 并读回" --json
```

平台/沙箱能力说明：

```bash
docker run --rm ai-agent-startup:v0.1.0 --platform
```

## 模型缓存与知识库

RAG 首次使用会下载本地模型（约数 GB 量级）。复用宿主已有缓存可避免重复下载：

```bash
docker run --rm -it --env-file .env \
  -v "$PWD/.cache/rag:/data/rag-cache" \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox \
  ai-agent-startup:v0.1.0
```

知识库默认使用镜像内的 `/app/data/raw`；要挂载自己的语料：

```bash
-v "$PWD/data/raw:/app/data/raw:ro"
```

## 命令沙箱（run_command）在容器内

`run_command` 依赖 bwrap 创建用户/网络命名空间，Docker 默认 seccomp 会拦截。先验证：

```bash
docker run --rm --entrypoint uv ai-agent-startup:v0.1.0 \
  run --no-sync python dev/ci_sandbox_probe.py
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
docker tag ai-agent-startup:v0.2.0 ghcr.io/vukencc/ai-agent-startup:v0.2.0
docker push ghcr.io/vukencc/ai-agent-startup:v0.2.0
```

推 `v*` 标签会触发 `.github/workflows/release.yml`：离线回归 + 沙箱探测 → 构建并推送 GHCR → 创建 GitHub Release。

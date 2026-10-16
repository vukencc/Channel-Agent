# Docker 使用指南

GHCR 上的 `v0.2.1` 是已发布镜像，只包含该 tag 的代码，不包含当前工作树里的未提交功能。本文本次更新没有拉取或实机验收该发布镜像。需要运行当前源码时，从工作树本地构建；只有构建上下文中实际包含的文件会进入镜像。构建参数 `GIT_SHA` 标记当前提交，不表示工作树干净或涵盖未提交差异。

## 快速开始（开包即用）

### 方式 A：从 GHCR 拉取（推荐给其他人）

```bash
if [ ! -f .env ]; then cp .env.example .env; fi  # 首次运行创建后填写 OPENCODE_API_KEY 等
docker pull ghcr.io/vukencc/channel-agent:v0.2.1   # 2) 拉取已发布镜像（约 2.3GB 为历史测量值）

# 3) 运行交互式 TUI
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox -v ai-agent-rag:/data/rag-cache \
  ghcr.io/vukencc/channel-agent:v0.2.1

# 或 headless 单次执行
docker run --rm --env-file .env -v ai-agent-state:/data/state \
  ghcr.io/vukencc/channel-agent:v0.2.1 --prompt "只回复：你好" --json --policy readonly
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
docker compose -f docker/docker-compose.yml up
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
- Docker 默认 `ENTRYPOINT` 启动 CLI。Web extra 只安装 WebUI 依赖，不改变入口；运行 WebUI 进程需通过 `--entrypoint uv` 覆盖入口，但服务只接受回环地址，当前不支持用 `-p` 端口映射从宿主访问。需要使用 WebUI 时按[本机 WebUI 指南](web-ui.md)在宿主或 WSL 启动。

## 构建

```bash
docker build -f docker/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t ai-agent-startup:v0.2.1 .
# 上一条从当前工作树构建，包含上下文中的未提交源文件。构建 Web extra（只安装依赖，不提供容器端口访问）：
docker build -f docker/Dockerfile --build-arg UV_SYNC_ARGS="--no-dev --extra web" -t ai-agent-startup:web .
# 需要测试或 documents 可选功能：
docker build -f docker/Dockerfile --build-arg UV_SYNC_ARGS="--dev --extra documents" -t ai-agent-startup:test .
# 当前 pyproject.toml 将 torch 固定到 CPU 索引；需要 CUDA 时修改 [tool.uv.sources] 后重新执行 `uv lock`，再按新锁文件构建。
# 国内网络可改用 PyPI 镜像源：
docker build -f docker/Dockerfile --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t ai-agent-startup:v0.2.1 .
```

镜像默认安装 **CPU 版 torch**。需要 GPU 时，修改 `pyproject.toml` 的 torch 索引配置并重新生成锁文件后构建。

## 运行

交互式 TUI（全屏界面必须 `-it`）：

```bash
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state \
  -v ai-agent-sandbox:/data/sandbox \
  -v ai-agent-rag:/data/rag-cache \
  ai-agent-startup:v0.2.1
```

headless 单次执行默认使用 `readonly`。下面的写入任务显式使用 `--policy full_access`，仍受工具开关、沙箱和路径边界约束：

```bash
docker run --rm --env-file .env \
  -v ai-agent-state:/data/state \
  ai-agent-startup:v0.2.1 --prompt "把 hello 保存到 hello.txt 并读回" --json --policy full_access
```

平台/沙箱能力说明：

```bash
docker run --rm ai-agent-startup:v0.2.1 --platform
```

## 模型缓存与知识库

RAG 首次使用会下载本地模型（约数 GB 量级）。复用宿主已有缓存可避免重复下载：

```bash
docker run --rm -it --env-file .env \
  -v "$PWD/.cache/rag:/data/rag-cache" \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox \
  ai-agent-startup:v0.2.1
```

知识库默认使用镜像内的 `/app/data/raw`；要挂载自己的语料：

```bash
-v "$PWD/data/raw:/app/data/raw:ro"
```

## 命令沙箱（run_command）在容器内

`run_command` 依赖 bwrap 创建用户/网络命名空间，Docker 默认 seccomp 会拦截。先验证：

```bash
docker run --rm --entrypoint uv ai-agent-startup:v0.2.1 \
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
GIT_SHA=$(git rev-parse --short HEAD) TAG=v0.2.1 docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml run --rm agent            # TUI
docker compose -f docker/docker-compose.yml run --rm agent --prompt "只回复：你好" --json --policy readonly
```

Compose 的 headless 调用同样默认为 `readonly`；确有写入需求时，应显式指定 `--policy full_access` 并检查沙箱与路径限制。

需要命令沙箱时取消 `docker/docker-compose.yml` 中 `security_opt` 的注释，并用探针验证。

## 保存、分发与发布

```bash
# 离线归档
docker save ai-agent-startup:v0.2.1 | gzip > ai-agent-startup-v0.2.1-image.tar.gz
docker load < ai-agent-startup-v0.2.1-image.tar.gz

# 推送到 GitHub 容器仓库（token 需 write:packages）
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <用户名> --password-stdin
docker tag ai-agent-startup:v0.2.1 ghcr.io/vukencc/channel-agent:v0.2.1
docker push ghcr.io/vukencc/channel-agent:v0.2.1
```

推 `v*` 标签会触发 `.github/workflows/release.yml`：离线回归 + 沙箱探测 → 构建并推送 GHCR → 创建 GitHub Release。

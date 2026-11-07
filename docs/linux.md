# Linux 桌面成品

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

Linux x86_64 成品为 `ChannelAgent-<版本>-linux-x64-<构建时间>.tar.gz`。从 [GitHub Releases](https://github.com/vukencc/Channel-Agent/releases) 下载后解压，进入目录运行 `./ChannelAgent`，保留 `_internal` 和附带许可证。GUI 使用包内 Python，不需要安装项目开发环境；模型连接可在「模型管理」添加。

## 系统与命令环境

发布流水线在 Ubuntu 22.04 构建。每份成品的 `manifest.json` 记录实际构建的 glibc，`使用说明.txt` 说明该包的环境要求；本地在更新发行版构建的包不能假定兼容旧版系统。需要图形桌面或 WSLg，以及 Qt 所需的系统图形库；musl/Alpine 和 ARM64 不属于当前构建目标。

Ubuntu 可按需安装以下依赖与中文字体：

```bash
sudo apt install libegl1 libopengl0 libxcb-cursor0 libxkbcommon-x11-0 fonts-noto-cjk bubblewrap util-linux python3
```

命令隔离需要 Bubblewrap、util-linux、`/usr/bin/python3`，系统还须允许所需的用户命名空间。系统 Python 用于隔离辅助脚本和命令环境，与包内 GUI Python 分开；程序不回退到未隔离执行。程序随包附带网络中继与 npm/npx 辅助脚本，按原有权限、确认、审计和资源限制工作。模型权重、知识库和项目依赖由用户另外准备。

## 数据与更新

默认数据根为 `$XDG_DATA_HOME/ChannelAgent`；没有有效的绝对 XDG 路径时使用 `~/.local/share/ChannelAgent`。配置、会话、日志、缓存和默认工作区以该根为基准，与启动目录无关。显式 `AI_AGENT_PROJECT_ROOT` 可覆盖默认值；`--state-dir` 可选择独立会话库。

更新时将新成品解压到新的程序目录，用户数据保留在独立位置。安装目录和运行库不能被当作可写项目。成品的 `configuration.example.env` 是配置样例，需要时复制为数据根下的 `.env`，勿覆盖已有配置；首次使用也可直接在界面添加模型。

## 自检与构建

```bash
./ChannelAgent --self-check /tmp/channel-agent-check.json
```

自检使用独立临时数据，检查 GUI、Core、Flow 导入导出、文件权限、文档图片、CPU 运行库及实际沙箱写入，不调用模型服务或下载权重。`ok: true` 表示本轮检查通过，不代表模型业务结果或 RAG 质量验收。运行真实检索前仍须准备语料及模型。

从 Linux 源码根构建：

```bash
uv sync --locked --group packaging --extra desktop --extra web --extra documents --extra vision
uv run --no-sync python -m agent_test.scripts.build_linux
```

输出到 `dist/`，包含成品目录、tar.gz 与 SHA256。构建先核对应用字节码及辅助脚本，再从独立目录移除开发 Python 路径进行实际自检，通过后归档。新构建记录位于 `.cache/reports/linux-package-*`。发布及标签规则见[发布说明](release.md)。

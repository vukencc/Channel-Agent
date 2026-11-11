# 0.5.0 动态工具安全验证

> 2026-11-11 · 发布前验证记录 · 实现说明见 [动态工具](dynamic-tools.md)。

## 计划与范围

针对注册/导入、模型工具目录、实际分派、子会话继承、执行环境和异常排空分层验证。不请求模型服务；模型相关用例使用明确标注的受控回调，执行路径使用真实工具、文件、进程和 Bubblewrap。此前真实客服模型验证另见 [业务报告](agent-flow-dynamic-tools-validation-2026-11-11.md)，不能替代安全回归。

| 维度 | 用例与断言 |
| --- | --- |
| 定义边界 | 全部内置名保留、重复名、16/17 项、32,000 UTF-8 字节、入口绑定/签名、无注册副作用。 |
| Schema/参数 | 深度/节点/属性/序列化体积、循环数据、类型/多余字段/非有限值、64 KiB 相邻边界及 Unicode 实际传递；源码和参数同时达到上限时真实执行，防止单 argv 组合超限。 |
| 权限/继承 | 部署关闭命令、节点未选择、父级收窄、同名源码替换、子级篡改、孤立/分支/真实委派及两轮复用。 |
| 执行隔离 | 真实项目外绝对/相对路径、符号链接、硬链接和网络负例；检查无敏感内容泄漏、外部文件不变。本机监听端口作为网络阳性对照。 |
| 并发撤权 | 跨线程持有真实工作区锁，在工具等待期间撤销或替换父定义；分别覆盖启用/关闭 command_jobs。Smart 确认等待中也改变定义，断言无旧代码副作用且不死锁。 |
| 异常/一致性 | Python 异常、不可 JSON 序列化、输出过量、超时/取消、拒绝确认，以及起始/完成审计失败；检查会话排空和工具结果配对。 |
| 成品 | Windows/Linux 冻结自检必须实际校验 jsonschema 资源、动态工具定义及 Flow JSON 保存/导入导出。 |

## 问题及修复

1. **入口校验可绕过**：合法函数后追加同名 class 等模块级绑定，注册仍通过。红测后加入可静态判定的绑定校验及执行环境内最终函数/签名检查。后者不约束源码顶层副作用；任意 Python 源码不是安全 DSL。
2. **审计失败仍执行**：审计目录不可写或选择性拒绝 command_started，原路径仍产生文件并报告退出 0。执行起始和成功授权审计现采用严格写入，失败阻止启动；完成审计失败明确返回失败并提示已经执行、可能有副作用，不能宣称回滚或自动重试。
3. **排队后执行旧能力**：工具捕获定义后等待工作区锁，父会话撤销/同名替换仍执行旧定义。现用短授权锁协调工具集合更新，在排队结束及命令启动前重新比较当前有效权限和完整定义。锁不跨确认等待或进程运行；已启动的操作不会被假称回滚。WSL 在 command_started 审计确认处提交授权。
4. **WSL 完成故障误称拦截**：命令获得执行授权后原生审计/协议失败，原路径可能误称未执行。现在返回明确的结果未知/可能副作用失败提示；授权前失败继续关闭执行。

## 结果

- 本地 Linux 非集成全量：**2108 passed、39 skipped、5 deselected，128.25 秒**；该次之后新增的 WSL 协议用例另行记录。
- 真实 `ci_sandbox_probe`：Bubblewrap + prlimit 实际隔离通过。
- 最终专项（禁用本机 `.env`，8 个文件）：**107 passed，69.17 秒**，含真实 worker 协议四项与源码/参数同时达到上限的组合用例。
- 本机 Windows 原生：按工作流选择的 57 个文件，**572 passed、56 skipped，339.23 秒**；另显式启用真实 Windows→WSL：**8 passed，80.52 秒**。WSL 用例未因原生选择集跳过而被追认为已通过，而是独立实际执行。
- PR 首轮 Windows CI：**583 passed、45 skipped，79.57 秒**。平台权限、可选依赖及显式 WSL 开关导致跳过数不同，集成模型/权重下载未启用。
- Linux CI 的 Azure mirrorlist 和直接 Ubuntu 主站两轮均在 APT 下载阶段因 DNS 失败，未运行代码测试。安装动作现先限时探测 Ubuntu 主站、kernel.org 和 Ubuntu 国家镜像，排除超过七天的安全索引；保留 Ubuntu GPG 签名、APT 有效期/TLS 校验及真实隔离探针。实测发现 Iceland 的已签名安全索引陈旧近三个月而被拒绝；kernel.org 的索引签名有效且日期为当天。该主机探测不证明 runner 可达。五项受控 shell 回归覆盖回退、全部不可达、陈旧索引、更新/安装失败传播，均通过。最终双平台 CI 已通过，见下述结果。
- [PR #46 检查](https://github.com/vukencc/Channel-Agent/actions/runs/43044028156)：Ubuntu 22.04 **2119 passed、42 skipped、5 deselected，207.65 秒**；Windows **583 passed、46 skipped，81.46 秒**，三个警告来自 jieba 依赖的转义语法。Linux runner 实际通过 kernel.org 获取 Ubuntu 签名的依赖，随后真实隔离探针通过。模型/语料集成下载未启用，未将 integration 跳过算作通过。
- 本机 Windows 干净源码 `8a351e5` 构建的 0.5.0 exe：移除开发 Python PATH，**15/15 离线自检通过**，含 JSON Schema 资源、动态工具模板保存/导入导出；再开启真实 WSL，**16/16 通过**，错误集为空。后续提交仅改变测试、CI 和文档，应用源码未变化。本机包为发布前验收证据；正式发布资产由合并后标签工作流重新构建。
- 三个 workspace wheel 均构建成功，已核对动态 Core 模块、Web Flow/Working 与成品自检脚本资源，未发现用户状态/凭据文件。

正式 Windows ZIP（内含 `ChannelAgent.exe`）、Linux tar.gz 与 SHA256 见 [v0.5.0 发布页](https://github.com/vukencc/Channel-Agent/releases/tag/v0.5.0)。发布工作流须重新通过双平台测试、双平台冻结自检和镜像构建后才上传资产；各成品内的 `self-check.json`、`manifest.json` 记录实际验收与构建信息，不以本机结果替代官方构建。

## 复跑命令

从 Linux 仓库根目录，隔离状态/审计/缓存并将 Linux Node 加入 PATH：

```bash
PYTHON_DOTENV_DISABLED=1 COMMAND_EXECUTION_ENVIRONMENT=sandbox QT_QPA_PLATFORM=offscreen \
uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

最终专项为 `test_custom_tools_adversarial.py`、`test_custom_tools_authority.py`、`test_dynamic_wsl_authority.py`、`test_custom_tools_runtime.py`、`test_custom_tools_end_to_end.py`、`test_flow_custom_tools.py`、`test_custom_tool_entry_contract.py`、`test_packaged_dynamic_smoke.py`。Windows 选择集见 `.github/workflows/tests.yml`；真实 WSL 用例须设置 `RUN_WINDOWS_WSL_TESTS=1` 与可用的 `TEST_WINDOWS_WSL_PYTHON`，单独运行 `test_windows_wsl_bridge.py`。不使用 Windows uv 操作 WSL `.venv`。

## 适用范围

AST/schema 检查只约束协议与资源边界，不证明源码无恶意。选择动态工具授予当前环境的命令能力；Full Access 不等于解除所选沙箱。local 环境的网络/宿主能力按现有策略执行。AUDIT_SYNC=false 默认写入内核，true 才要求 fsync；不能据此宣称停电安全。测试覆盖已列举的范围，不构成所有攻击均安全的证明。未联系真实客户或运维系统，未访问用户已有项目、会话、知识库。

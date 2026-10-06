# CLI 命令与权限模式

## 命令输入与选择

在 CLI 输入 `/` 可浏览命令补全及参数提示，按 Tab 接受补全。Ctrl+P 打开命令搜索面板；使用上下键选择，Enter 执行或填入参数命令，Esc 返回。`/sessions` 和别名 `/resume` 搜索恢复会话（含已隐藏会话）；`/models` 搜索模型预设；`/permissions` 搜索权限模式。选择器均支持文字搜索。

`/policy` 可直接设置或查看当前会话模式：

```text
/policy smart
/policy full_access
/policy standard
/policy default
```

可用值为 `readonly`、`standard`、`trusted`、`smart`、`full_access`。`default` 清除当前会话覆盖，回到环境变量 `TOOL_PERMISSION_POLICY` 指定的默认值。`/permissions` 选择模式后会立即应用到当前会话。

## 权限模式

- `readonly` 拒绝写操作和命令执行。
- `standard` 按工具的确认要求逐次请求用户批准。
- `trusted` 只对匹配 `TOOL_PERMISSION_RULES` 的操作自动批准；其他操作仍请求确认。
- `smart` 使用本地确定性规则判断操作风险，不调用额外模型。已验证的工作区新建、向已有文件追加，以及简单只读 `pwd`、`ls`、`cat`、`head`、`tail`、`wc` 可自动通过；覆盖、删除、网络访问及无法判定的操作仍需确认。
- `full_access` 自动批准工具确认，包括工具自身请求的强制确认；会话删除仍要求用户明确确认。

联网搜索仍需结合 `WEB_SEARCH_CONFIRM` 配置理解：设为 `off` 时，Standard 和 Trusted 可跳过该工具的确认；Smart 仍要求确认，Full Access 自动批准。`request_permission` 的强制计划申请不受此开关影响。

当工具集包含 `request_permission` 时，Agent 可在 Smart 模式下先为敏感内容等无法由本地规则识别的风险申请一次计划确认。该工具接收 `action`、`detail`、`reason`，返回 JSON `allowed` 结果；拒绝后应停止该操作。它不改变权限模式、不授予后续调用权限，也不替代具体工具的检查和确认。此申请会按当前模式处理并写入审计；Full Access 自动允许，`readonly` 会拒绝；headless 中需要人工确认的申请会拒绝。

所有模式仍受工具可用子集、工具注册开关、路径范围、命令沙箱、资源配额、审计和取消机制约束。`TOOL_PERMISSION_POLICY` 的环境默认值为 `standard`；`--policy` 可为 headless 单次任务显式指定 `readonly`、`standard`、`trusted`、`smart` 或 `full_access`。headless 默认 `standard`，没有交互确认能力，需确认的操作会立即拒绝。

## 删除与恢复会话

`/delete` 或 `/del` 删除当前会话；可传唯一 ID 前缀选择会话，例如 `/delete abc123`。CLI 会先切换到目标会话并显示确认，必须输入 `/yes` 或按 Ctrl+Y 才执行；`/no` 或 Ctrl+R 取消。Full Access 也不能跳过此确认。

删除前会拒绝仍有任务、关联子任务、命令或工具运行的会话，并要求先处理该会话已有确认。若会话目录含旧版 `workspace/`，删除会被拒绝，需先整理或迁移旧工作区。保存队列会先完成写入，再将会话目录原子移至 `AGENT_STATE_DIR/.trash/<会话ID>/`（默认状态目录为 `.agent/`）。回收区只包含会话记录、消息、记忆、附件、收件箱和会话审计；工作区、导出文件和共享记忆保留。

当前没有 CLI 清空回收区或恢复命令。需要恢复时，先退出 CLI，再把回收目录完整移回原状态目录中的原 ID 目录；不要合并已有目录或手动拆分其中内容。保留工作区不随会话恢复而移动。

# 会话创建与 Agent 通信

会话工具用于 CLI 用户和模型创建会话，以及让直接关联的父、子 Agent 交换持久消息。`create_session`、`send_session_message` 和 `read_session_messages` 默认注册，不受 `ENABLE_AGENT_TASKS` 控制。该开关只控制旧版 `delegate` 工具注册和启动时的任务队列预载；创建子任务时会按需初始化队列，因此 `/tasks` 不要求开启此开关。尚无已加载的子任务时，`/tasks` 会提示可让 Agent 用 `create_session` 创建独立任务。模型可通过 `/tools` 的会话工具子集设置隐藏或重新启用这些工具。

## 创建会话

CLI 启动时没有已恢复会话，或用户执行 `/new [名称]` 时，会调用已注册的 `create_session` 一次创建空会话，不会额外调用模型。模型调用 `create_session` 时必须提供非空 `task`；Standard、Trusted、Smart 模式下每次都要经过用户确认，`full_access` 会自动批准工具确认，`readonly` 会拒绝创建。

每个根会话有自己的项目工作区 `SANDBOX_DIR/<根会话 ID>/`（默认 `crud_tests/<根会话 ID>/`）。新建子 Agent 自动绑定直接父 Agent 的项目工作区，因此可共同读写项目文件；不再提供工作区模式选择。历史上已持久化的隔离子会话不会迁移文件。旧环境变量 `AGENT_WORKSPACE_MODE` 不再生效。子 Agent 不能指定其他项目根。

项目文件共享不改变会话边界：会话记录、上下文、`inbox.json`、记忆和审计日志仍各自保存在独立会话目录中。子 Agent 继承父会话完整权限策略与预算所有者。省略 `tool_names` 时继承父会话完整工具集；显式指定时只能取父工具集子集，不能扩权。子 Agent 暂时不能调用 `create_session` 或 `delegate` 再创建子 Agent；后端会拒绝递归委派，系统提示也会说明此限制。Agent Tasks 当前在全局委派并发槽内等待完整子任务；若子任务等待孙任务且占满并发槽，孙任务无法启动。开放递归前需先改造调度，并验证任务树的停止、超时和回收。子 Agent 使用相同工具能力不代表能读取父会话私有记录或查询父级任务。`max_rounds` 默认为 6，且不能超过父会话有效轮数上限或 256。需要双方模型主动通信时，须在 `tool_names` 显式包含 `send_session_message` 与 `read_session_messages`；`create_session` 返回实际提供的 `communication_tools`。Standard、Trusted、Smart 下创建子会话必须明确确认；Full Access 会自动批准工具确认；`readonly` 会拒绝创建。直接 `create_session` 不要求启用 `ENABLE_AGENT_TASKS`；该队列开关仅对应旧版 `delegate` 功能。

在 CLI 或 WebUI 删除会话时，父会话及其通过委派创建的后代会一起移入回收区；独立手动分支保留。任一关联 Agent、工具或长命令仍在运行或完成审计时，删除会被拒绝。工作区文件、会话导出和共享记忆不会随会话记录一同移入回收区。

文件、命令与网络工具按当前权限模式执行确认规则。默认 `standard` 沿用工具逐次确认要求；headless 没有交互确认界面，待确认操作会立即拒绝。Smart 与 Full Access 的行为见 [CLI 命令与权限模式](cli-command-modes.md)。

## 父子消息

模型可用 `send_session_message(session_id, message)` 向直接父或子会话发消息，用 `read_session_messages(after_seq=0, limit=20)` 分页读取自己的信箱。只允许直接父子双方通信，不能向无关会话、同级会话或自己发送；调用身份由当前 Agent 上下文确定，不能通过工具参数伪造。工具也必须已包含在调用者会话的工具子集中。

消息写入收件会话的持久信箱。收件 Agent 空闲时不会因此自动启动；来信会在收件方下一轮模型调用开始时投递，不会唤醒空闲会话。每个模型轮次开始时自动读取最多一条待处理消息，并在完整工具结果批次之后作为带发送者标记的参考消息注入。批量读取工具每页默认 20 条，允许用 `limit` 请求 1 至 100 条。响应 JSON 的总字符数还受 `TOOL_MAX_OUTPUT` 限制，因此实际返回条数可能少于 `limit`；系统不会截断单条消息或跳过未返回的消息，`next_seq` 指向本页最后一条实际返回的消息。若单条消息也超过输出上限，工具会返回可恢复错误并保留信件；提高 `TOOL_MAX_OUTPUT` 后可重试读取。注入内容明确标为参考数据，不是用户审批或系统指令。读取不会删除消息。

`SESSION_MESSAGE_MAX_CHARS` 限制单条消息字符数，默认 `4000`；`SESSION_INBOX_MAX_BYTES` 限制单个信箱文件的 UTF-8 字节数，默认 `1048576`。两项都必须为正整数。消息超长、信箱已满或信箱超过读取上限时操作会失败；信箱满时不会删除历史，也不会自动重试或丢弃旧消息。

发送和读取审计仅记录收件会话、序号、消息字符数、读取数量等元数据，不记录消息正文。消息仍作为会话内容持久保存，应按会话数据妥善保护。

信箱文件为 `<state-dir>/<session-id>/inbox.json`。工具响应长度限制仅用于显式读取工具，不影响内部逐条投递或 CLI 未读计数。读取工具不推进自动投递游标；同一序号可能在工具结果和稍后的自动来信中出现，可按发送者与序号辨认。

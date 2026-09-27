# 平台能力设计与本轮边界

## BUG-18：取消与进程隔离

本轮实现低风险的协作取消：检索阶段、嵌入批次与重排批次前检查取消，工具超时使用独立标志，CLI 显示等待当前批次退出。
Web 搜索由 WEB_SEARCH_TIMEOUT 限制网络等待；评估池与前台分开。写操作仍等待旧线程结束，避免停止后迟到写入。
原生模型加载/单个推理批次、在途 DNS/网络等待不能由 Python 安全强杀，因此不承诺所有路径立即停止或绝对截止。

后续方案是独立常驻模型进程（有界请求队列、请求 ID、进程级 deadline），超时终止并重建只读推理 worker。
内存预算需覆盖多进程模型副本；命令仍由 Bubblewrap 隔离，写工具不迁入可随意杀死的后台线程。
验收应注入卡死 native worker，验证回收进程、释放槽位、请求不串轮、已完成工具不重放；本轮不实现该架构。

## BUG-19：Headless 契约（设计，尚未提供命令）

计划入口 `--prompt TEXT --json --state-dir PATH`：复用 SessionStore/SessionManager，一次输入执行至 idle/checkpoint/error，等待已授权工具收尾，刷新保存队列后退出。
不启用 TUI、不读取键盘；无确认代理时所有写入/命令/网络确认默认拒绝，拒绝仍作为对应 tool_call_id 的工具结果反馈。禁止提供默认自动批准的 --yes-all。
需要自动化批准时，未来独立审批通道应校验 session_id、turn_id、tool_call_id、请求内容哈希与有效期，审批不能串会话。

标准输出仅一个 JSON 对象：`version/session_id/turn_id/status/messages/metrics/error`，stderr 为诊断日志，不输出密钥；状态码 0=正常完成、2=配置或参数错误、3=检查点未完成、4=执行失败、130=取消。
流式版本另设 `--jsonl`，事件含递增 seq、session/turn、type、payload，最终 done 事件；重连不重放工具副作用。
现有 `main.py` 和 `--list` 保持兼容。最小验收包括 fake model 的纯文本结束、拒绝真实写入、配对结果、超时检查点、SIGINT 后持久化、无 stdout 日志污染。

## HTTP API + SSE（仅设计）

单独服务进程拥有状态目录锁，CLI 未来作为客户端；不能让服务与现有 CLI 同时写同一目录。
建议端点 POST /sessions、POST /sessions/{id}/turns、GET /turns/{id}/events、POST /confirmations/{id}、POST /turns/{id}/cancel。
认证和工作区所有权在路由入口验证；SSE 用 seq 游标恢复展示，不重放调用；幂等键绑定请求摘要。审批 token 单次使用、有时限、绑定调用原文。
先做契约和隔离测试，再独立 PR 实现。尚未开放监听端口。

## MCP 工具生态（仅设计）

动态工具目录必须声明参数 schema、只读/写/出网能力、确认策略、时限与服务来源；不可信 MCP 服务不能自行声明免确认。
服务注册与会话启用由用户显式选择，工具名带命名空间并限制 schema 总预算。所有结果继续校验 call/result 配对并写审计。
结果适配层将结构化数据与渲染分离；保留现有 Pydantic + register_tool 兼容路径。不直接把 MCP 输出当系统指令。

## 多模态与网页输入（仅设计）

文本加载器扩展不等于多模态。图片/PDF 图像需先确定模型能力、上传确认、大小/页数限制和敏感内容处理；OCR 结果标记来源与置信度。
网页抓取/浏览器必须沿用出网审批，并限制重定向、目标地址和下载大小；浏览器放入独立进程沙箱，不暴露宿主凭据。
分别提交输入格式、能力探测、失败降级和端到端隔离测试。本轮不添加图片上传、HTTP 服务、MCP 客户端或浏览器依赖。

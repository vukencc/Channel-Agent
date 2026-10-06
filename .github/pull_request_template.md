## 行为变化

<!-- 描述具体问题、改动后的行为，并链接相关 issue。 -->

## 验证

<!-- 列出实际执行的命令和结果；未执行的检查说明原因。 -->

- [ ] `uv sync --locked --extra web`
- [ ] `uv run pytest -m 'not integration' -q`
- [ ] `uv run python dev/ci_sandbox_probe.py`（Linux）

## 配置与发布影响

<!-- 说明新增配置、依赖、数据兼容性和版本变化；没有则写“无”。 -->

- [ ] 配置与行为变化已同步 `.env.example` 和文档
- [ ] 依赖变化已同步 `uv.lock`
- [ ] 保留沙箱检查、确认、审计与路径边界
- [ ] 未提交密钥、会话数据、日志、缓存或沙箱输出

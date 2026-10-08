# 运行事件与前端状态

## 处理边界

- `run_events.py`：三个 SSE 入口共用游标、轮询、终止及序列化逻辑。保留运行时事件的类型、身份和轮次；不得将完成转换为开始事件。
- `runProtocol.js`：仅在这里转换旧 wire 名称和身份字段。历史 `step.*` 转成 `node.*`，`done/error` 转成 `run.completed/run.failed`，旧审批名称转成 `approval.required`。
- `runStream.js`：按规范事件类型分发状态变更。`answer.status` 属于 Run，`steps` 属于节点，`turns` 属于智能体展示，`tools` 属于具体工具调用。Agent 完成不意味着节点内其他 Agent 完成。
- `runFrameBuffer.js`：每个智能体分别缓存字符；生命周期事件即时交付，动画输出使用 `display_only`，不能改变执行状态。

## 状态规则

| 事件 | 作用范围 |
| --- | --- |
| `node.started/completed/failed/skipped` | 对应节点及其展示卡片 |
| `agent.started/completed/failed` | 对应 Agent；不得结束并行同伴 |
| `tool.started/completed/failed` | 同一 `tool_call_id`；工具失败不等于 Agent 失败 |
| `node.waiting_input` | 对应节点等待补充信息 |
| `run.waiting_input` | Run 暂停及提问正文；不改写所有并行节点 |
| `approval.required` | Run 进入 `waiting_approval`；不得伪造子节点完成 |
| `run.completed/failed` | Run 结束及仍未收束工作的最终处理 |
| `llm.delta/llm.thinking/delta` | 文本或活动提示；不得把终态改回运行 |

输入等待统一使用 `waiting_input`，审批等待统一使用 `waiting_approval`，包含运行详情接口与页面状态。旧 `waiting_for_feedback` 只在前端协议适配层接受，不再由后端详情接口生成。

`run_attempt` 隔离重试。新轮次清理旧正文、文件、等待状态；旧轮次的生命周期事件和缓冲字符不再影响新轮次。`event_cursor` 是持久化事件偏移，与文字动画进度无关。

节点完成携带 `output` 时，该正文具有替换语义，丢弃对应节点尚未播放的旧草稿。未收到独立完成事件的工具只能标记为已结束，不能声称执行成功。

## 回归验证

- `uv run pytest -q`
- 前端 Node 22：`node --test tests/runStream.test.js`
- `npm run build`

协议、游标续传和终止测试在 `tests/test_run_event_protocol.py`；前端测试覆盖别名兼容、并行隔离、输入等待、审批、迟到文本、重试轮次及工具失败恢复。

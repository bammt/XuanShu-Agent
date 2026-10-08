# 玄枢应用 API

前端端口和后端端口分别由 `.env` 的 `FRONTEND_PORT`、`BACKEND_PORT` 控制。后端 OpenAPI 位于 `/docs`；每个已发布应用还提供 `/automations/{app_id}/develop` 可视化接入页。

## 发布与凭据

发布应用后会得到两个入口：

- `/public/{public_token}`：无需登录的独立聊天页。
- `/api/v1/apps/{public_token}`：必须使用应用专属 API Key 的程序接口。

管理接口使用登录令牌：

- `GET /api/apps/{app_id}/api-keys`：列出 Key，不返回原文。
- `POST /api/apps/{app_id}/api-keys`：创建 Key，原文只返回一次。
- `DELETE /api/apps/{app_id}/api-keys/{key_id}`：立即撤销 Key。

程序接口接受 `Authorization: Bearer xsk_...` 或 `X-API-Key: xsk_...`。

## 上传文件

`POST /api/v1/apps/{public_token}/files`，使用 `multipart/form-data` 的 `file` 字段。单文件和单次运行总大小均受 `MAX_UPLOAD_MB` 限制。

响应中的 `id` 供运行请求绑定到文件变量。上传时必须携带 API Key，并显式提供表单字段 `user_id`。上传对象按 `EXTERNAL_UPLOAD_RETENTION_DAYS` 保留，默认 30 天；同一用户可以在后续会话中复用该 ID，提交运行不会立即消费它。

## 发起运行

`POST /api/v1/apps/{public_token}/runs`

```json
{
  "response_mode": "blocking",
  "inputs": {
    "message": "请审查合同",
    "risk_level": "strict"
  },
  "files": {
    "files": ["UPLOAD_ID"]
  },
  "user_id": "client-user-001"
}
```

`response_mode` 支持以下值：

- `blocking`（默认）：等待本轮生成完成、追问或审批暂停，再返回完整 JSON。多轮信息不足时返回 `waiting_input`，追问正文在 `output` 与 `waiting_input.question`。
- `streaming`：同一个 POST 直接返回 SSE，curl 使用 `-N`。第一帧 `run.accepted` 包含运行和会话 ID；结束或暂停帧的 `result` 包含完整状态、回复与文件。
- `async`：立即返回 `queued`、任务 ID 和 `events_url`，随后查询运行或读取 SSE。

`wait_timeout_seconds` 仅用于 blocking，默认 120，范围 1–300 秒。超时返回当前状态、`wait_timed_out: true`、`status_url` 和 `events_url`，后台任务继续执行。原有立即返回任务的调用方应显式设置 `response_mode: "async"`。

收到追问后，用同一个 `user_id` 和响应中的 `conversation_id` 再次提交补充消息，以继续当前应用会话。

### 第二轮及后续请求

`conversation_id` 写在 JSON 请求体**顶层**，与 `user_id`、`inputs`、`files` 同级，不能写在 `inputs` 里面，也不是 URL 参数。字段名为 `conversation_id`，不是 `conversition_id`。

例如第一轮响应：

```json
{
  "id": "RUN_ID",
  "conversation_id": "CONVERSATION_ID",
  "status": "waiting_input",
  "output": "请提供请示事项、发文机关和主送机关。"
}
```

第二轮仍向同一个 `/runs` 接口 POST：

```bash
curl -X POST 'http://YOUR_HOST:8012/api/v1/apps/APP_TOKEN/runs' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "client-user-001",
    "conversation_id": "CONVERSATION_ID",
    "response_mode": "blocking",
    "inputs": {
      "message": "请示事项是申请防汛专项经费，发文机关为市水利局，主送机关为市人民政府。"
    },
    "files": {"files": []}
  }'
```

第三轮及之后使用相同方式，并始终保持同一用户和会话 ID。`id` 是每轮运行 ID，不能替代 `conversation_id`。没有新附件时数组留空，已有附件仍保留。流式调用的 `run.accepted` 帧以及最终/暂停帧的 `result` 都包含 `conversation_id`。

所有 API 请求必须显式提供调用方分配的稳定 `user_id`，不会从 Cookie 推断身份。首次运行省略 `conversation_id`，服务端创建并返回会话 ID；后续请求携带相同的 `user_id` 和返回的 `conversation_id`，恢复历史消息和运行状态。发送 `message: "新建对话"` 或设置 `new_conversation: true` 会创建新的会话。`inputs` 和 `files` 必须使用编排时确认的英文变量名。单次运行由 Redis 队列投递，Worker 使用 PostgreSQL 条件更新原子领取，同一运行不会被两个 Worker 重复执行。

会话管理接口：

- `POST /api/v1/apps/{public_token}/conversations?user_id=...`：创建新的会话；必须提供 `user_id`。
- `GET /api/v1/apps/{public_token}/conversations?user_id=...`：列出该外部用户的历史会话。
- `GET /api/v1/apps/{public_token}/conversations/{conversation_id}?user_id=...`：读取会话及其中的运行历史。
- `DELETE /api/v1/apps/{public_token}/conversations/{conversation_id}?user_id=...`：删除会话及其运行历史，清空上下文。

## 状态与事件

- `GET /api/v1/apps/{public_token}/runs/{run_id}`：返回状态、最终回复、每个节点的最终输出、审批信息和交付文件。
- `GET /api/v1/apps/{public_token}/runs/{run_id}/events`：SSE 事件流；可附加 `after_event=N` 从持久化游标继续，避免断线或审批恢复后重放旧节点。事件会实时报告 `node.started`、`agent.started`、`tool.started`、`llm.thinking`、`llm.delta`、`tool.completed`、`node.completed` 和终态事件，客户端可据此展示当前正在理解、调用工具或整理结果的阶段并增量渲染模型输出。
- `GET /api/v1/apps/{public_token}/runs/{run_id}/files/{path}`：下载本次运行生成的文件。

终态包括 `completed`、`failed`、`waiting_input`、`waiting_approval`、`rejected` 和 `needs_revision`。`waiting_input` 表示 Flow 的交互节点调用了平台 `ask_user` 工具；响应里的 `waiting_input.question` 是需要用户补充的问题。下一次使用同一个 `user_id`/`conversation_id` 发起运行时，从保存的暂停节点恢复，不会重复已完成节点。事件只公开节点状态与节点最终输出，不公开 CrewAI 的内部规划或思考过程。

多轮 Flow 的信息收集节点必须是单独的普通 Agent；后续 Crew 或任务必须通过 `depends_on` 等待该节点完成。编排定义违反这一拓扑时会在运行前拒绝，避免信息未收集齐就顺序启动下游 Crew。

## 人工审批

当状态为 `waiting_approval` 时：

`POST /api/v1/apps/{public_token}/runs/{run_id}/approval`

```json
{
  "outcome": "approved",
  "feedback": "审核通过"
}
```

可用 outcome 由编排节点的 `feedback_outcomes` 决定，状态响应中的 `approval.outcomes`
是本次审批的唯一有效枚举。`approved` 会恢复同一运行，并跳过已完成节点；Flow
节点可将任意已配置 outcome 用于后续分支并继续运行。对于不启用分支恢复的 Crew 审批，非
`approved` 结果会将本次运行标记为 `needs_revision`。

## 匿名聊天页

公开聊天页内部使用 `/api/public/{public_token}` 系列接口，不要求 API Key。应用描述接口同时返回 `interaction_mode`；`multi_turn` 应用允许先发送不完整输入，由 Flow 的 `ask_user` 工具提出问题。首次运行同样生成随机用户与会话标识，并通过 HttpOnly Cookie 自动延续历史；`waiting_input` 后的下一条消息会恢复同一 Flow。页面按照应用输入契约显示文本、数字、布尔、JSON、图片和文件字段，支持上传进度、节点输出展开、最终回复、人工审批与交付文件下载。

### 流式运行完成响应

`GET /api/v1/apps/{public_token}/runs/{run_id}/events?user_id=...` 的最后一条完成帧包含 `result`，内容与运行详情接口的完整 JSON 一致。`output` 是面向用户的正文；`node_outputs` 是各节点输出；`files` 是可下载产物。文件下载仍需按接口要求携带用户身份和 API 密钥。

```text
event: run.completed
data: {"type":"run.completed","event_cursor":42,"run_attempt":0,"output":"周报已生成。","files":[],"result":{"id":"run_example","status":"completed","output":"周报已生成。","created_at":"2026-09-21T10:00:00","idempotency_key":"","inputs":{"title":"周报"},"user_id":"user_example","conversation_id":"conversation_example","node_outputs":{"draft":"周报正文"},"checkpoint":{},"files":[],"approval":null,"waiting_input":null,"error":null}}
```

连接在完成后关闭。断线重连通过 `after_event` 续传；已完成运行即使没有新的增量事件，也会返回完整完成帧。暂停等待用户输入不属于完成，客户端应处理 `run.waiting_input` 后提交补充输入。

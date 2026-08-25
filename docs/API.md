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

响应中的 `id` 供运行请求绑定到文件变量。上传对象 24 小时有效，成功提交运行后会被消费并从临时区删除。

## 发起运行

`POST /api/v1/apps/{public_token}/runs`

```json
{
  "inputs": {
    "message": "请审查合同",
    "risk_level": "strict"
  },
  "files": {
    "contract_files": ["UPLOAD_ID"]
  },
  "user_id": "client-user-001",
  "conversation_id": "customer-session-001"
}
```

首次请求可以省略 `user_id` 和 `conversation_id`，服务端会生成随机 `user_id` 和统一会话 ID，并在响应中返回，同时写入 `xuanshu_user_id` HttpOnly Cookie；后续请求只需复用该 Cookie 或显式携带 `user_id`（也可显式携带 `conversation_id`），服务端会加载同一会话的历史消息和运行状态。发送 `message: "新建对话"` 或设置 `new_conversation: true` 会创建新的会话。`inputs` 和 `files` 必须使用编排时确认的英文变量名。单次运行由 Redis 队列投递，Worker 使用 PostgreSQL 条件更新原子领取，同一运行不会被两个 Worker 重复执行。

会话管理接口：

- `POST /api/v1/apps/{public_token}/conversations?user_id=...`：创建新的会话；省略 `user_id` 时同时生成随机用户 ID。
- `GET /api/v1/apps/{public_token}/conversations?user_id=...`：列出该外部用户的历史会话。
- `GET /api/v1/apps/{public_token}/conversations/{conversation_id}?user_id=...`：读取会话及其中的运行历史。
- `DELETE /api/v1/apps/{public_token}/conversations/{conversation_id}?user_id=...`：删除会话及其运行历史，清空上下文。

## 状态与事件

- `GET /api/v1/apps/{public_token}/runs/{run_id}`：返回状态、最终回复、每个节点的最终输出、审批信息和交付文件。
- `GET /api/v1/apps/{public_token}/runs/{run_id}/events`：SSE 事件流；可附加 `after_event=N` 从持久化游标继续，避免断线或审批恢复后重放旧节点。
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

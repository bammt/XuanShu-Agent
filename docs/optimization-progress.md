# 八项优化实施记录

此文件为历史开发记录。当前功能、部署和验证方式以 README、API 文档及仓库测试为准；下文的阶段性“进行中”描述不代表当前版本状态。

本文件用于记录可核验进展；以下状态以当前代码和测试为准。

## 当前已修改并完成开发环境验证

- 新增 `VariableTextarea.vue` 和独立的变量插入函数，接入 Studio 任务描述、期望输出；支持 / 搜索、上下键、Enter、Escape、保留光标两侧文字。
- 新增 `ToolInputEditor.vue`，HTTP 工具通过添加变量编辑 JSON Schema。
- `HttpRequestEditor.vue` 已支持请求头、查询参数、JSON、表单、form-data、raw、binary、SSL、超时和有界重试；请求模板只描述 HTTP 传输，Input schema 只描述工具输入。Input schema 的 `format: binary` 文件变量会从当前会话工作区读取 bytes。
- 新增 `http_requests.py`，版本 2 请求显式绑定，不把输入参数自动泄露到请求正文；保留旧版本执行语义。工具执行入口验证输入 Schema。
- Connected App 可打开配置，缺少集成令牌时明确提示配置前提；真实授权需要部署环境提供 CrewAI 集成令牌。

## 八项范围的当前状态

1. HTTP 表单、JSON、raw、binary、SSL、超时和有界重试已实现并通过生产 Node 22 构建；Connected App 的外部授权仍取决于部署令牌。
2. 变量插入扩展到其他编排字段、Crew 内部任务和新节点；键盘、中文输入、无结果和依赖范围验证。
3. Flow/Crew 输出与依赖契约已增加显式输入绑定、结构化 `output` 和嵌套 Crew 的 memory/planning/cache/max_rpm/日志/管理 Agent 设置；保存、校验和运行路径均有测试覆盖。
4. 代码节点和工具节点显式输入、标准结构化输出、执行与生成器支持。
5. Flow 创建节点选择类型，编辑不允许直接切换类型，代码/工具节点使用专用面板。
6. 三条 SSE 路由和重连均返回完整 `run.completed.result` JSON，已有 API 文档示例；外部部署凭据只影响 Connected App 的现场授权。
7. 模型级多模态开关：迁移、API、UI 和运行时能力使用。
8. 图片产物已由 Worker 同步到 MinIO，并按 MIME 类型登记；RichMessage 在登录、公开和 API 文件 URL 上均按图片 MIME/扩展名渲染。已完成真实代码节点生成 PNG → MinIO → 文件接口 → SSE 文件列表的端到端验证。

## 已进行的验证

- 变量插入函数 2 项测试通过。
- HTTP 显式绑定函数 2 项测试通过。
- 新变量组件、输入编辑组件和 HTTP 编辑器已在 Docker Node 22 中构建；`runStream` 13 项、变量插入 2 项 Node 测试通过。
- Selenium 视觉巡检已覆盖桌面与移动端控制台、Studio、资源、模型、运行页和导入弹窗，所有页面无水平溢出。
- 真实烟雾运行临时 Flow 151：隔离代码节点生成 `generated.png`，Worker 发布到 MinIO，文件接口返回 200，SSE 最后一帧的 `result.files` 与运行详情一致；临时应用已清理。
- 真实浏览器点击测试确认 Connected App 可打开配置，HTTP 编辑器可显示“添加查询参数”；Crew 内部 Task 的描述/期望输出、Agent 目标/背景已接入 `/` 选择器；代码/工具输入下拉仅列出运行输入，依赖输出单独选择。

## 当前环境

历史验证使用 Docker Compose 主配置与开发覆盖配置，相关记录保留用于追踪实现过程。

## 后续进展（同轮实施）

- 模型 `supports_vision` 开关：DB、API、ModelsView、Worker 配置与 Agent multimodal 已接通；新增 Alembic `0002_model_vision` 并在开发 PostgreSQL 升级成功。
- `node_execution.py` 实现显式输入绑定、Python main 返回对象包装为 output、缺失字段报错；代码输入通过 JSON 参数传递而非源码插值。
- 新增 tool 节点执行 HTTP/Python 工具；Schema、Composer 提案模型、架构提示与 Studio 创建按钮已接通，Python 工具结构化执行路径和代码节点真实 Docker 执行均已验证。
- Studio 已移除编辑节点类型下拉框，新增代码/工具入口和 `NodeInputBindings` 面板；尚需端到端浏览器审查。
- API SSE 最终 `run.completed.result` 包含详情 JSON（三条路由均已统一），API 文档增加示例。
- `docker-compose.dev.yml` 给运行容器及 schema-init 挂载 Alembic 目录，解决新迁移之后旧镜像启动时 revision 缺失。已重新创建 backend/worker/studio-worker，健康接口正常。
- 全量 Python 333 项通过；前端 Node 测试 15 项通过，Docker Node 22 Vite 构建通过。新增 multipart/binary、真实工具 Schema 调用、typed file 依赖映射、嵌套 Crew 管理模型和确定性节点输出落盘覆盖。
- 真实 HTTP API 代码节点烟雾脚本 `/tmp/xuanshu_code_smoke.py` 已创建。第一次输入“测试代码输入”被现有 conversation router 识别成闲聊，没有执行节点；临时 app 已清理。第二次改为明确指令后测试进行中，需查看实际进程结果，不能视为通过。

仍需部署环境提供 Connected App 集成令牌并使用真实模型返回图片，完成授权和模型供应商相关的现场验收；代码、工具、API、MinIO 和前端链路已在开发环境验证。

### 真实链路结果更新

第三次烟雾运行成功（明确业务指令，同时提供声明的 message 输入）：HTTP 创建 Flow → Worker → Docker Python 子进程 → `{ "output": { "text": "请计算这句话的字符数量", "length": 11 } }` → SSE 完整 result，均通过。临时应用已由 API 删除。

### 验收范围纠正

模型密钥可能加密保存在数据库，不能根据 `.env` 缺少 API Key 判断没有可用模型。Connected App 的真实授权仍需要部署环境提供 `CREWAI_PLATFORM_INTEGRATION_TOKEN`；这不影响本地 HTTP、代码、工具、SSE、MinIO 和前端链路。

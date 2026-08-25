# 旧版功能迁移矩阵

玄枢保留 `/data/crewai_amp` 的核心操作模型，并在独立项目中完成生产化重构。以下状态以当前 Docker 部署和自动化验收为准。

| 模块 | 当前实现 |
|---|---|
| Dashboard | 工作空间统计、项目网格、最近运行与状态概览 |
| Automations | Crew/Flow 网格、草稿/发布、运行、API 接入、删除 |
| Studio | 左侧自然语言编排、中间曲线画布、右侧中文参数面板 |
| Composer | Redis 持久化 CrewAI Flow；需求分析 Agent 与架构/工具审查 Agent；多轮确认、修改循环、确认卡锁定 |
| Canvas | Agent、Task、Crew、Router、Code、人工审批和非直角连线 |
| Flow Crew | 内部 Agent、Task、依赖以及顺序/层级运行方式均可编辑并真实运行 |
| Preview | Studio 右侧浮动聊天，支持输入变量、文件、节点结果折叠、审批和最终回复 |
| Published App | 独立匿名 URL、聊天、变量、上传进度、审批、节点输出和交付文件 |
| Runs & Traces | 运行列表、持久事件、节点最终输出、最终回复、断线恢复与事件游标 |
| Code execution | Agent/Flow 节点可选使用统一隔离 executor；不再提供编排转源码或源码反向恢复 |
| Models | 工作空间列表、创建、保存、测试、删除和独立默认模型页面 |
| Skills | 在线开发、目录上传、标准校验、资源文件、删除及 CrewAI 原生渐进加载 |
| Tools | HTTP、隔离 Python、远程 MCP、Connected App；敏感配置加密和 API 脱敏 |
| Upload UX | 环境变量大小限制、上传进度、文件清单、删除和 Skill 文件夹弹窗 |
| User/Workspace | admin 创建子账号、用户自建空间、邀请同意、成员编辑/使用权限 |
| Workspace isolation | 模型、Skill、Tool、应用、发布、API Key 和运行记录均按工作空间隔离 |
| Runtime | PostgreSQL、Redis 队列、MinIO、每应用目录、长期记忆和统一沙箱 executor |

## 运行语义

- Crew 使用 CrewAI `Crew`、`Task` 和顺序/层级 `Process`。
- Flow 使用 CrewAI `Flow` 管理显式节点状态，并可在节点内运行 Crew。
- Worker 通过 PostgreSQL 条件更新原子领取 run，并用心跳判断失联；恢复时根据持久化 `outputs` 跳过已完成节点。
- 单任务 Agent 不启动 experimental reasoning；多步骤 reasoning 默认最多一次规划。UI 与 API 只展示节点最终输出。
- 审批恢复和网络重连使用事件游标，前端再按 `node_id` 幂等合并，不重复展示已完成节点。

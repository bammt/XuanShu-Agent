# Runtime data

Docker Compose 将 PostgreSQL、Redis、MinIO、Qdrant 和应用工作目录挂载到本目录下。
实际运行数据已被 Git 忽略，不要提交到代码仓库。Compose 启动时会自动创建所需子目录。

| 子目录 | 用途 |
| --- | --- |
| `postgres/` | PostgreSQL 数据库 |
| `redis/` | Redis 队列和持久化数据 |
| `minio/` | 上传文件和交付文件的对象存储 |
| `qdrant/` | 知识库向量数据 |
| `workspaces/` | 应用工作目录和会话文件 |
| `frontend-node-modules/` | Docker Compose dev 前端依赖 |

生产版和开发版共用这些数据目录。请保留运行数据并做好备份。

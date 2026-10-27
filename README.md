# Dify 知识库能力扩展包

> 基于 Dify 基座构建的独立知识库能力包，不侵入 Dify 底层框架，补齐文档版本管理、版本快照检索、数据集标注与文档异步索引四类生产级能力。

## 项目简介

本项目在 Dify 知识库（Dataset / Knowledge）之上，解决知识库在生产环境中的三个实际问题：

- 文档反复上传后，历史对话引用的内容会「漂移」；
- 索引任务不可追踪、失败后难以恢复；
- 标注数据缺少统一的维护入口与导出能力。

四大模块整体封装在知识库领域内，不侵入 Dify 底层核心框架。

## 改动规模

| 维度 | 数据 |
| --- | --- |
| 改动文件 | 77 个 |
| 新增 / 删除 | +2047 / -246 行 |
| 数据库迁移 | 3 个 |
| 后端服务 / 任务 | 4 个 |
| 前端组件 | 5 个 |
| 新增测试用例 | 138 个（全部通过） |

## 核心能力

### 1. 文档版本管理 Document Versioning

- 新增 `document_versions` 表，`documents.current_version_id`、`document_segments.version_id` 关联版本。
- 通过 `business_key` 识别同一逻辑文档，SHA256 文件哈希去重。
- `DocumentVersionService` 提供版本列表、详情、归档、版本提升。
- Redis 缓存（single-flight 单飞锁 + 空值哨兵）防缓存击穿。
- 同时提供 Console API 与 Service API。

### 2. 版本快照检索 Version Snapshot Retrieval

- 新增 `version_snapshot` 检索参数，召回「启用分片 + 快照绑定的历史版本分片」。
- `conversation_service` 构建 `reference_info` 与 `version_snapshot`，历史会话引用不漂移。
- 版本信息贯穿 RAG 检索、工作流、各类 App runner。
- 前端引用气泡展示版本徽章，支持查看历史原文。

### 3. 数据集标注 Dataset Annotations

- 新增 `dataset_annotations` 表（question / answer / tags / feedback_type）。
- 支持标注增删改查、标签管理、SFT/评估 JSONL 导出。
- 接口带 RBAC 权限控制，配套标注列表页与弹窗。

### 4. 文档异步索引任务 Document Async Task

- 新增 `document_async_task` 表，Celery dataset 队列执行。
- `trace_id` + `(tenant_id, dataset_id, trace_id)` 唯一约束保证幂等。
- 记录 `attempt_count`、`last_error`，支持失败落库与手动恢复脚本 `dispatch_recover.py`。
- 上传 Service API 改造为异步返回 `task_id`。

## 技术栈

- 后端：Flask-RESTX · Pydantic v2 · SQLAlchemy 2 · Alembic · Celery · Redis
- 前端：Next.js · React · TanStack Query · Dify UI · i18n · ORPC 类型契约
- 测试：pytest

## 并发与一致性

- Redis single-flight 缓存 + `SELECT ... FOR UPDATE` 行锁。
- `trace_id` 幂等约束，避免重复派发。
- `version_snapshot` 全链路透传，保证历史会话引用稳定。

## 测试结果

- 新增用例：138 passed
- `core/workflow`：1120 passed
- `core/rag`：998 passed，4 skipped
- 前端：20 passed

## 已知局限

1. 索引任务本身非幂等：自动重试会产生孤儿分片、重复 segment，当前仅开放手动重试，自动重试需后续幂等改造。
2. 标注导出为内存组装，大批量导出存在 OOM 风险，后续改为流式输出。
3. 缓存仅防击穿，缺少热点缓存降级策略。

## 快速开始

本项目基于 Dify 源码部署运行，新增迁移已包含在 `api/migrations/versions/` 中，会随标准迁移流程自动应用：

```bash
# 1. 按 Dify 官方流程启动（Docker 或本地源码部署）
cd docker
cp .env.example .env
docker compose up -d

# 2. 执行数据库迁移（本地源码部署时）
uv run --project api flask db upgrade
```

> 详细安装与配置请参考 [Dify 官方文档](https://docs.dify.ai)。

## 文档

- [Feature Summary](docs/FEATURE_SUMMARY.md) —— 功能、设计、并发与测试的完整说明。

## License

遵循 [Dify Open Source License](./LICENSE)（基于 Apache 2.0，附带附加条款）。
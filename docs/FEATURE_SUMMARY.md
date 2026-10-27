# Dify 知识库能力扩展包 · Feature Summary

> 基于 Dify 基座构建的独立知识库能力包，不侵入 Dify 底层框架。

## 概述

本扩展包在 Dify 知识库（Dataset / Knowledge）之上补齐 **文档版本管理、版本快照检索、数据集标注、文档异步索引** 四类能力，解决生产环境中「文档更新后历史引用漂移」「索引任务不可追踪」「标注数据缺少维护入口」等问题。

改动规模（相对上游 Dify）：

- 77 个文件，+2047 行，-246 行
- 新增 3 个数据库迁移
- 新增 4 个后端服务 / 任务
- 新增 5 个前端组件
- 配套单元测试

## 设计原则

- 独立演进：能力全部封装在知识库领域内，不改动 Dify 底层核心框架。
- 领域内聚：新增模型、服务、控制器集中在 `knowledge` / `dataset` 相关模块。
- 可回滚：数据库变更通过 Alembic 迁移管理，可独立升级与降级。
- 一致性优先：通过版本快照固化「历史会话引用的文档版本」。

## 模块一：文档版本管理（Document Versioning）

为知识库文档引入版本体系，支持同名文档多次上传时按内容生成版本。

- 数据模型：新增 `document_versions` 表；`documents` 增加 `current_version_id`；`document_segments` 增加 `version_id`。
- 版本识别：`business_key`（默认取文件名，支持 `business_id` / `business_key` 等元数据）关联同一逻辑文档。
- 内容去重：SHA256 文件哈希去重，内容未变化时复用当前版本，不产生重复记录。
- 版本操作：`DocumentVersionService` 提供版本列表、详情、内容 / 原文读取、归档、版本提升（promote）。
- 缓存：`DocumentVersionCache` 基于 Redis 缓存版本列表与摘要，采用 single-flight 单飞锁与空值哨兵，防止缓存击穿。
- API：同时提供 Console API 与 Service API。

## 模块二：版本快照检索（Version Snapshot Retrieval）

解决历史对话引用漂移问题：文档重新上传后，旧对话仍召回并展示当时引用的版本。

- 检索参数：`format_retrieval_documents` 新增 `version_snapshot`，检索时同时召回「启用分片 + 快照绑定的历史版本分片」。
- 快照构建：`conversation_service` 构建 `reference_info`（segment → version）与 `version_snapshot`（document → version）。
- 全链路透传：版本信息贯穿 RAG 检索、知识检索节点、工作流以及 Chat / Completion / AdvancedChat 等 App runner。
- 前端展示：引用气泡展示版本徽章（`v{n}`），支持展开查看历史版本原文。

## 模块三：数据集标注（Dataset Annotations）

为知识库提供问答对标注能力，便于后续评估与模型微调。

- 数据模型：新增 `dataset_annotations` 表（question / answer / tags / feedback_type）。
- 服务：`DatasetAnnotationService` 支持标注增删改查、标签管理、SFT / 评估 JSONL 导出。
- 权限：接口带 RBAC 权限控制。
- 前端：配套标注列表页、标注弹窗组件。

## 模块四：文档异步索引任务（Document Async Task）

将文档索引改造为可追踪、可重试的异步任务。

- 数据模型：新增 `document_async_task` 表，记录 `trace_id`、`payload`、`status`、`document_id`、`version_id`、`attempt_count`、`last_error` 等。
- 幂等：以 `trace_id` + `(tenant_id, dataset_id, trace_id)` 唯一约束实现任务幂等。
- 可观测：记录尝试次数与错误信息，支持失败落库。
- 恢复：提供手动恢复脚本 `dispatch_recover.py`。
- API：上传 Service API 改造为异步返回 `task_id`。

## 技术栈

- 后端：Flask-RESTX、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis（缓存 + 分布式锁）。
- 前端：Next.js + React + TanStack Query、Dify 组件、i18n、ORPC 类型契约。
- 测试：pytest。

## 并发与一致性设计

- Redis single-flight 缓存：版本列表读取通过单飞锁合并并发请求，避免缓存击穿。
- 行锁：`SELECT ... FOR UPDATE` 保证版本提升等写操作的串行化。
- `trace_id` 幂等约束：避免重复派发产生重复任务。
- `version_snapshot` 全链路透传：保证历史会话引用稳定，不随文档更新漂移。

## 测试结果

- 新增 138 个用例全部通过。
- `core/workflow`：1120 passed。
- `core/rag`：998 passed，4 skipped。
- 前端：20 passed。
- 修复 3 个 mock 适配类测试问题。

## 已知局限

1. 索引任务本身非幂等：自动重试会产生孤儿分片、重复 segment；当前只开放手动重试，自动重试需后续幂等改造。
2. 标注数据集导出为内存组装：大批量导出存在 OOM 风险，后续改为流式输出。
3. 缓存仅实现防击穿：缺少热点缓存降级策略。

## 目录结构

```
api/
├── migrations/versions/
│   ├── 2026_10_25_1000-a1f2e3d4c5b6_add_document_versions.py
│   ├── 2026_10_26_1000-b3e4c5d6f708_add_dataset_annotations.py
│   └── 2026_10_26_1100-c7d8e9f0a1b2_add_document_async_task.py
├── models/dataset.py                         # DocumentVersion / DatasetAnnotation / DocumentAsyncTask
├── services/knowledge/
│   ├── document_version_service.py           # 版本解析、提升、归档
│   ├── document_version_cache.py             # Redis 缓存 + 单飞锁
│   ├── annotation_service.py                 # 标注 CRUD / 导出
│   └── document_async_task_service.py        # 异步任务状态管理
├── tasks/document_async_task.py              # Celery 异步索引任务
├── controllers/console/datasets/dataset_annotations.py
├── controllers/console/datasets/datasets_document.py
├── controllers/service_api/dataset/document.py
└── dispatch_recover.py                       # 手动恢复脚本

web/app/
├── components/datasets/documents/detail/version-history/index.tsx
├── components/datasets/annotations/index.tsx
├── components/datasets/annotations/annotation-modal.tsx
└── components/base/chat/chat/citation/popup.tsx
```
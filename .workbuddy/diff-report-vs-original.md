# 当前工作区 vs 原始项目（dify 1.18.0）差异报告

- 对比基线：`E:\work\dify-main.zip`（原始压缩包，14918 个文件，根目录 `dify-main/`）
- 对比对象：`E:\work\dify-main\dify-main`（当前工作区）
- 对比方法：逐文件 CRC32 比对（工作区无 `.git`，无法 `git diff`）；文本差异按 LF 归一化后生成 unified diff，避免 CRLF/LF 差异造成误判
- 生成时间：2026-10-26

## 一、总体数字

| 类别 | 数量 |
|---|---|
| 与原始完全一致 | 14661 |
| 内容被修改 | 253（其中 api 36、web 41、packages 生成物 176） |
| 新增文件 | 4507（其中约 4400 个是运行/构建产物，真正新增源码约 25 个） |
| 原始有、现在没有 | 4 |

真实代码改动（排除 packages 生成物后，77 个文件）：**+2047 行 / -246 行**。

## 二、功能改动（这是与原始项目的实质区别）

工作区在原始 1.18.0 之上实现了 **三个新功能**，其中「文档版本」是主线，另两个分别是上传链路的异步化和知识库标注。

### 功能 1：文档版本管理（Document Versions）—— 改动最大

新增表 `document_versions` + `Document.current_version_id` + `DocumentSegment.version_id`，实现「文档重新上传后保留历史版本、可回溯、可归档」。

**新增文件（api）**
- `api/services/knowledge/document_version_service.py`（469 行）—— 核心：`resolve_or_create_version`（按 SHA-256 判断是否建新版本）、`_promote_version`、`set_version_archived`、`list_version_summaries`、`get_version_content`、正文快照读写
- `api/services/knowledge/document_version_cache.py`（164 行）—— Redis 缓存：列表/摘要 TTL 300s、空值哨兵 60s、单飞锁 10s，全部 `@redis_fallback` 降级
- `api/migrations/versions/2026_10_25_1000-a1f2e3d4c5b6_add_document_versions.py`
- 测试：`tests/unit_tests/services/knowledge/test_document_version_service.py`（245）、`test_document_version_cache.py`（546）

**新增文件（web）**
- `web/app/components/datasets/documents/detail/version-history/index.tsx`（227）—— 版本历史面板
- 20 个语言的 `dataset-documents.json` 各 +21 条 `versionHistory.*` 文案

**修改的关键文件**
- `api/models/dataset.py`：新增 `DocumentVersion` 模型、`Document.current_version_id` + `versions` 关系、`DocumentSegment.version_id`
- `api/controllers/console/datasets/datasets_document.py`（+163）：新增 3 个接口
  - `GET /datasets/{id}/documents/{doc_id}/versions`
  - `GET .../versions/{version_id}`（含正文）
  - `POST .../versions/{version_id}/archive`（归档/取消归档，latest 不可归档）
- `web/app/components/datasets/documents/detail/index.tsx`（+47/-22）：文档详情页改成 Tabs（Chunks / Version History）
- 索引链路打版本标记：`core/rag/docstore/dataset_docstore.py`、`services/knowledge/segments/indexing.py`、`services/knowledge/summaries/{adapters,application}.py`、`services/knowledge/indexing/adapters/execution.py`、`tasks/batch_create_segment_to_index_task.py` —— 切片/摘要写入时带上 `version_id`、`version`、`workspace_id` 元数据
- 重建索引改为「只动当前版本」：`tasks/duplicate_document_indexing_task.py`、`tasks/retry_document_indexing_task.py`、`tasks/document_indexing_update_task.py`、`repositories/knowledge/segment_repository.py` —— 删除/更新 segment 时加 `or_(version_id == current_version_id, version_id.is_(None))` 过滤，旧版本切片不再被清掉
- `repositories/knowledge/document_repository.py`（+49）：新增 `_snapshot_current_version_content`（索引完成时把切片正文回写进版本行）与 `get_version_metadata`

### 功能 2：Service API 文档上传异步化

原来 `POST /datasets/{id}/document/create-by-file` 同步跑完索引，现在改为「落任务 + 立即返回」，重活交给 Celery worker。

**新增文件（api）**
- `api/services/knowledge/document_async_task_service.py`（109）：trace id 解析、按 `(tenant_id, dataset_id, trace_id)` 唯一约束做幂等、`create_pending`
- `api/tasks/document_async_task.py`（217）：`queue="dataset"`, `bind=True`, `max_retries=3`，Redis 锁 3600s → processing → 建/更新文档 → 派发索引 → success；失败 `countdown=30 * 2**retries`
- `api/migrations/versions/2026_10_26_1100-c7d8e9f0a1b2_add_document_async_task.py`
- 测试：`tests/unit_tests/tasks/test_document_async_task.py`（194）、`test_document_async_task_service.py`（54）

**修改**
- `api/models/dataset.py` 新增 `DocumentAsyncTask` 表；`api/models/enums.py` 新增 `DocumentAsyncTaskStatus`（pending/processing/success/failed）
- `api/controllers/service_api/dataset/document.py`（+78/-23）：
  - 响应模型从 `DocumentAndBatchResponse` 换成 `DocumentAsyncTaskResponse`（`task_id` / `trace_id` / `status`）
  - 支持 `X-Trace-Id` 头或 `trace_id` 表单做幂等，同 traceId 重放返回同一 task
  - 不再调用 `save_document_with_dataset_id`，改为 `create_pending` → commit → `document_async_task.delay(task_id)`
  - 上传文件 `file_ids` 置空，改由 worker 侧处理；新增 `file_hash = sha256(storage.load(...))`
- `api/extensions/ext_celery.py`（+1）：imports 注册 `tasks.document_async_task`
- `api/services/knowledge/dataset_service.py`（+293/-93）：**纯方法抽取**为主——`apply_indexing_configuration`、`resolve_dataset_process_rule`、`_count_configured_sources`、`document_creation_dataset`、`get_upload_file`、`get_operator_name`、`mark_document_indexing_error`、`create_or_update_document_for_async_task`；同步路径 `save_document_with_dataset_id` 内插入 `resolve_or_create_version` 调用（hash 未变则不重新索引）
- 测试改造：`tests/unit_tests/controllers/service_api/dataset/test_document.py`（+80/-10），原「创建成功返回 document+batch」用例被替换为「返回 task_id 且不触发索引」+「同 traceId 幂等」

### 功能 3：知识库标注（Dataset Annotations）

**新增文件**
- `api/services/knowledge/annotation_service.py`（202）：增删改查 + `export_annotations`（SFT JSONL / 评估 JSON）
- `api/controllers/console/datasets/dataset_annotations.py`（311）：`GET/POST /datasets/{id}/annotations`、`GET .../annotations/export`、`PATCH/DELETE .../annotations/{annotation_id}`
- `api/fields/dataset_annotation_fields.py`（35）
- `api/migrations/versions/2026_10_26_1000-b3e4c5d6f708_add_dataset_annotations.py`
- web：`app/(commonLayout)/datasets/(datasetDetailLayout)/[datasetId]/annotations/page.tsx`、`app/components/datasets/annotations/{index.tsx,annotation-modal.tsx}`、`service/dataset-annotation.ts`、`i18n/locales/{en-US,zh-Hans}/dataset-annotations.json`

**修改**
- `api/models/dataset.py` 新增 `DatasetAnnotation` 模型；`api/models/__init__.py`、`api/controllers/console/__init__.py` 注册
- `web/app/components/app-sidebar/dataset-detail-section.tsx`（+10）：知识侧边栏在 hitTesting 后插入 Annotations 入口
- `web/app/components/base/chat/chat/answer/operation.tsx`（+24）：聊天回答下方新增「标注」按钮，直接把问答对存入引用来源所属知识库
- `resources.ts` / `resources.generated.d.ts` / `common.json` 注册新命名空间 `datasetAnnotations` 与 `operation.annotate`、`datasetMenus.annotations`

### 功能 4：检索结果的版本快照（会话级引用不漂移）

这是功能 1 的配套：**历史会话里的引用固定在回答生成时的版本**。

- `api/services/conversation_service.py`（+97）：新增 `build_reference_info`（把 `segment_id -> {version_id, version_no}` 写进消息 metadata 的 `reference_info`）和 `build_version_snapshot`（从最近一条助手消息反推 `document_id -> version_id`）
- 4 个 app runner（chat / completion / advanced_chat / workflow）+ `entities/app_invoke_entities.py` + `task_pipeline`：运行时计算并透传 `conversation_id` / `version_snapshot`
- `core/rag/datasource/retrieval_service.py`：检索 segment 时可见性从 `enabled == True` 放宽为 `or_(enabled, version_id.in_(snapshot))`
- `core/rag/retrieval/dataset_retrieval.py`、`repositories/knowledge/dataset_read_repository.py`、`services/knowledge/retrieval/attachments.py`：同步放开授权校验，返回 `document_version` / `document_revision`
- `core/workflow/nodes/knowledge_retrieval/{knowledge_retrieval_node.py,retrieval.py}`、`services/entities/knowledge_retrieval_inner.py`、`services/knowledge_retrieval_inner_service.py`：透传 `version_snapshot`
- web：`utils.ts` 新增 `applyReferenceInfo`，在 chat / embedded-chatbot / chat-with-history / workflow 面板 / app log 共 6 处把 `reference_info` 合并进 citation；`popup.tsx`（+70）显示 `v{version_no}` 徽标并可展开查看该版本原始正文

### 契约生成物（非手写）
`packages/contracts/generated/**` 共 176 个文件变化（`orpc.gen.ts` / `types.gen.ts` / `zod.gen.ts`），以及 `packages/contracts/openapi/*.json` 6 个新增——都是按新接口重新生成的产物，不是手改代码。

## 三、新增文件完整清单（源码类）

api（14 个）
```
api/controllers/console/datasets/dataset_annotations.py            311
api/fields/dataset_annotation_fields.py                             35
api/dispatch_recover.py                                              9
api/migrations/versions/2026_10_25_1000-a1f2e3d4c5b6_add_document_versions.py
api/migrations/versions/2026_10_26_1000-b3e4c5d6f708_add_dataset_annotations.py
api/migrations/versions/2026_10_26_1100-c7d8e9f0a1b2_add_document_async_task.py
api/services/knowledge/annotation_service.py                       202
api/services/knowledge/document_async_task_service.py              109
api/services/knowledge/document_version_cache.py                   164
api/services/knowledge/document_version_service.py                 469
api/tasks/document_async_task.py                                   217
api/tests/unit_tests/services/knowledge/test_document_async_task_service.py
api/tests/unit_tests/services/knowledge/test_document_version_cache.py
api/tests/unit_tests/services/knowledge/test_document_version_service.py
api/tests/unit_tests/tasks/test_document_async_task.py
```
alembic 链：`a1f2e3d4c5b6` → `b3e4c5d6f708` → `c7d8e9f0a1b2`（当前 head）。

web（8 个）
```
web/app/(commonLayout)/datasets/(datasetDetailLayout)/[datasetId]/annotations/page.tsx
web/app/components/datasets/annotations/index.tsx                  426
web/app/components/datasets/annotations/annotation-modal.tsx       227
web/app/components/datasets/documents/detail/version-history/index.tsx
web/service/dataset-annotation.ts                                   78
web/i18n/locales/en-US/dataset-annotations.json
web/i18n/locales/zh-Hans/dataset-annotations.json
```

## 四、被修改的文件（按改动量排序，前 15）

```
+293 -93   api/services/knowledge/dataset_service.py
+163  -0   api/controllers/console/datasets/datasets_document.py
+138  -1   api/models/dataset.py
 +78 -23   api/controllers/service_api/dataset/document.py
 +97  -0   api/services/conversation_service.py
 +80 -10   api/tests/unit_tests/controllers/service_api/dataset/test_document.py
 +70  -2   web/app/components/base/chat/chat/citation/popup.tsx
 +47 -22   web/app/components/datasets/documents/detail/index.tsx
 +57  -0   web/i18n/resources.generated.d.ts
 +49  -1   api/repositories/knowledge/document_repository.py
 +42  -5   api/repositories/knowledge/segment_repository.py
 +40  -3   api/core/rag/retrieval/dataset_retrieval.py
 +20 -12   api/tasks/duplicate_document_indexing_task.py
 +19 -11   api/tasks/retry_document_indexing_task.py
 +22  -5   api/services/knowledge/summaries/adapters.py
```
其余 62 个多为 1~20 行的小改（透传参数、i18n 文案、import 注册）。

## 五、非代码差异（环境运行产物，不是改造内容）

| 类别 | 说明 |
|---|---|
| `docker/volumes/**`（4116 个） | 跑过 docker compose：Postgres 数据目录 2656、pgvector 1270、plugin_daemon 182 |
| `docker/.env`、`docker/middleware.env`、`docker/nginx/conf.d/default.conf` | 部署配置（其中 `.env` 含实际密钥） |
| `api/.env`、`web/.env.local`、`api/storage/**`（上传 PDF、图片、privkeys） | 本地跑过 API 产生的配置与文件 |
| `api/api-run.out.log`、`worker-run.out.log`、`celerybeat-schedule*` | 运行日志与 beat 调度文件 |
| `api/providers/**/*.egg-info`（约 240 个）、`dify-agent/src/*.egg-info` | `uv` 安装依赖生成 |
| `web/next-env.d.ts`、`tsconfig.tsbuildinfo`、`.vinext/dev/lock.json` | 前端构建产物 |
| `dev/*.ps1`（7 个） | 新增的 Windows 本地启动脚本（start-api / start-worker / start-web / start-beat / start-backend / run-*-logged） |
| `.patch-docver.txt` | 仓库根目录遗留的 patch 记录（3860 字节，`*** Begin Patch` 格式，针对 `document_version_service.py`） |

## 六、被删除的文件（原始有、现在没有）

- `api/uv.lock` —— **依赖锁文件被删除**（会影响可复现构建，建议确认是有意为之还是误删）
- `dify-agent/uv.lock` —— 同上
- `api/.idea/icon.png`、`api/.idea/vcs.xml` —— IDE 元数据

## 七、需要注意的点

1. 有几个文件被加了 UTF-8 BOM（`api/models/__init__.py`、`api/core/rag/docstore/dataset_docstore.py`、`api/tasks/batch_create_segment_to_index_task.py`），原��版本没有 BOM。
2. `dataset_service.py` 的改动以方法抽取为主，`resolve_or_create_version` 的 hash 判断语义未变；但同步路径现在在 hash 未变时**不再重新索引**（原来无条件置 WAITING）。
3. Service API 的 `create-by-file` 返回结构变了（不再返回 `document` + `batch`），属于**破坏性变更**，旧调用方需要适配。
4. 异步状态机目前只有 pending/processing/success/failed，没有死信态；`.workbuddy/plans/route1/` 下有 8 个已产出未落盘的改造文件（重试/死信 + `next_attempt_at`），尚未应用到代码。

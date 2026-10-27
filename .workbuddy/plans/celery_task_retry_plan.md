# 路线 1 实施清单（方案 Z / 扩展 DocumentAsyncTask）

> 状态：**待你确认后出 diff**。本文只列变更清单与验证步骤，不含全量代码。
> 行号均为 `api/` 下相对引用，已逐条核实。

---

## 0. 核实结论先读（3 条影响清单的判断）

| # | 结论 | 依据 |
|---|---|---|
| 1 | **加 `dead_letter` 枚举值不需要任何 DDL** | `models/types.py:174` `EnumText.impl = VARCHAR`，`load_dialect_impl` 返回 `VARCHAR(length)`。`DocumentAsyncTaskStatus` 列声明为 `EnumText(..., length=32)`，`"dead_letter"` 仅 11 字符。**纯代码改动，迁移脚本里不需要碰枚举。** |
| 2 | **退避改 2/4/8s 只需改 1 个常量** | `tasks/document_async_task.py:69` 已是 `countdown=RETRY_DELAY_SECONDS * (2**self.request.retries)`。把第 36 行 `RETRY_DELAY_SECONDS = 30` 改成 `2`，即精确得到 retries=0→2s、1→4s、2→8s。第 53 行 `retries >= MAX_RETRIES(3)` 时落终态，**序列天然对齐，不需要改公式**。 |
| 3 | **当前 alembic head 是 `c7d8e9f0a1b2`** | 245 个迁移文件中，无任何文件以 `c7d8e9f0a1b2` 作为 `down_revision`，确认它是单头。新迁移直接以它为父。 |

---

## 1. 迁移脚本与模型变更清单

### 1.1 枚举（纯代码，无 DDL）

`models/enums.py:135` `DocumentAsyncTaskStatus` 增加 `DEAD_LETTER = "dead_letter"`。

**注意两处语义必须一起改**，否则会出现「状态是死信但任务还在跑」：

| 现有值 | 路线 1 后的语义 |
|---|---|
| `pending` | 已入队/等待执行（手动重试也重置回这个状态） |
| `processing` | 正在执行 |
| `success` | 执行成功 |
| `failed` | **保留**：单次失败但仍有重试预算（第 197 行 `_record_attempt_failure` 写它），前端看到 `failed` + `next_attempt_at` 非空 = 即将自动重试 |
| `dead_letter`（新增） | 重试预算耗尽，停止自动重试，等待人工介入 |

> 这里有个必须你拍板的取舍：`failed` 现在是「终态失败」的语义，改成「可重试的中间态」是**破坏性变更**（前端/调用方若按 `failed` 判定终态会误判）。
> 备选：保留 `failed` 为终态，新增 `retrying` 表示「失败待重试」。代价是多一个枚举值。`models/enums.py` 里 `DocumentAsyncTaskStatus` 的 docstring 也要同步。
> **我倾向前者**（`failed` = 可重试中间态，`dead_letter` = 终态），因为 `next_attempt_at` 字段已经能区分，且少一个状态；但如果你有外部调用方依赖 `failed`，请选备选。

### 1.2 新增列（迁移 `xxxx_extend_document_async_task_retry.py`，`down_revision = "c7d8e9f0a1b2"`）

| 列 | 类型 | 用途 | 是否本期必需 |
|---|---|---|---|
| `last_error_code` | `String(255)` nullable | 结构化错误码（如 `embedding_provider_unavailable`），便于前端分类展示与告警聚合 | **是**（你的缺口清单） |
| `next_attempt_at` | `DateTime` nullable | 下次自动重试时间，前端轮询展示「x 秒后重试」 | **建议**：没有它，前端无法区分「还会重试」和「已停」 |
| `progress_current` / `progress_total` | `Integer` nullable | 进度 | **待定，见 1.3** |

索引：建议加 `Index("document_async_task_dead_letter_idx", "status", "created_at")`，为后续死信列表页预留；本期若不做列表页可省。

### 1.3 进度字段：需要你定

你的原始需求第 4 条要「查询任务状态、**进度**、报错信息」，但缺口清单 5 项里没列进度字段。两个做法：

- **做法 A（最小改动，推荐）**：**不建列**。响应体里派生一个 `progress`：
  - `pending` → 0%；`processing` → 50%；`success` → 100%；`failed`/`dead_letter` → 100%（终态）
  - 若 `task.document_id` 非空，附带 `document.indexing_status`，前端用文档自身的索引状态展示细粒度进度
- **做法 B**：建 `progress_current` / `progress_total` 两列，由 worker 显式上报。粒度更细，但要改 worker 埋点，且本期上传任务本身没有天然的分段进度可报

**我推荐 A。** 真实的分片/embedding 进度在 `Document` 与 segment 上，不在 `document_async_task` 上；为了它往上传任务表里塞计数器是错位的。

### 1.4 退避参数修改位置

`tasks/document_async_task.py`:
- 第 36 行 `RETRY_DELAY_SECONDS = 30` → `2`
- 第 42 行 `default_retry_delay=RETRY_DELAY_SECONDS` 同步生效，无需改
- 第 55 行 `_mark_failed(task_id, exc)` → 改为写 `dead_letter`
- 第 65–70 行 `self.retry(...)` **必须包三层 try**（见第 5 节 C4）
- 第 197 行 `_record_attempt_failure` 补写 `last_error_code` + `next_attempt_at`

---

## 2. 接口规划：**推荐挂 service_api**

### 2.1 二选一评估

| 维度 | service_api | console |
|---|---|---|
| 鉴权 | `DatasetApiResource`（`controllers/service_api/wraps.py:464`）自带 `method_decorators = [validate_dataset_token]`，**零新增鉴权代码** | 需新写登录 + RBAC 权限位 |
| 租户隔离 | 基类 `get_dataset(dataset_id, tenant_id)` 天然按 dataset 授权；再比对 `task.tenant_id` 即可闭环 | 需自己实现 |
| 调用方匹配 | `task_id` 由 `service_api` 创建接口（第 910 行）返回，轮询方就是 API 调用方 | 控制台 UI 不是主要调用方 |
| 与创建接口一致性 | 同 namespace、同 response model、同 `register_response_schema_models` | 另起一套 |

**结论：挂 service_api。** 你的描述是「后台接口」，但从调用链路看，轮询方是拿 API Key 的集成方而不是控制台用户；且复用 `DatasetApiResource` 能让鉴权代码量为 0。若你确实需要控制台运维入口，可以**后续**再在 console 加一个薄封装转发，不影响本期。

### 2.2 路由设计（均继承 `DatasetApiResource`）

```
GET    /v1/datasets/<uuid:dataset_id>/documents/async-tasks/<uuid:task_id>
       → 查询状态、进度、报错

POST   /v1/datasets/<uuid:dataset_id>/documents/async-tasks/<uuid:task_id>/retry
       → 手动重试死信任务
```

`dataset_id` 进路径是为了复用 `get_dataset()` 做鉴权；handler 内再校验 `task.dataset_id == dataset_id and task.tenant_id == tenant_id`，不匹配一律 404（不返回 403，避免探测任务是否存在）。

### 2.3 响应体

扩展 `controllers/service_api/dataset/document.py:320` 的 `DocumentAsyncTaskResponse`：

```
task_id, trace_id, status,
attempt_count,            # 已执行次数，见第 3 节
retry_count,              # 派生 = max(attempt_count - 1, 0)
max_retries,
next_attempt_at,
last_error_code, last_error_message,   # 注意：模型字段叫 last_error，响应里建议改名 last_error_message
progress,                 # 派生百分比
document_id, document_indexing_status, # 做法 A 下的细粒度进度
created_at, completed_at
```

> **lint 约束（必做）**：新响应模型必须加进第 402 行的 `register_response_schema_models(service_api_ns, ...)`，否则 `python api/dev/lint_response_contracts.py` 会报 mismatch（当前基线 735 valid / 0 mismatch）。另外 ruff 禁 `flask_restx.reqparse`，参数校验一律用 Pydantic + `model_validate`。

---

## 3. `attempt_count` 口径确认

### 3.1 现有实现其实是「已执行次数」

`tasks/document_async_task.py:103` 在置 `PROCESSING` 时 `task.attempt_count = (task.attempt_count or 0) + 1`。即：首投执行后 = 1，重试 1 次后 = 2 …… 死信时 = **4**（首投 + 3 次重试）。

### 3.2 现有口径有个漏洞，必须一并修

`_process` 第 95–100 行有两处短路返回（`already_succeeded` / `already_processing`），这两条路径**不经过第 103 行**，`attempt_count` 不会 +1。手动重试会频繁命中这两条路径，导致计数失真。修法：把 +1 提到 `_process` 入口、两处短路判断之前。

### 3.3 推荐口径

**存储字段 `attempt_count` = 已执行次数（沿用现有语义，不改存储、不动迁移）**，与 `models/knowledge_fs.py:838` 的 `knowledge_fs` 范式一致。
**响应体额外给 `retry_count = max(attempt_count - 1, 0)`**，前端要显示「已重试 3 次」就用它。

这样既不破坏现有语义，又满足两种展示需求，且不新增字段。

---

## 4. 手动重试实现方案：**推荐接口内重置 + 投递，立即返回**

### 4.1 三个方案的取舍

| 方案 | 描述 | 评价 |
|---|---|---|
| **A（推荐）** | 接口内：`apply_async` 到原 `dataset` 队列，立即返回 202 | 只需一次 Redis 投递；**不新增队列 → 不用改 `api/docker/entrypoint.sh` 两处 + `ext_celery.py` imports**；worker 侧已有 Redis 锁（第 88 行）与 `already_processing` 短路（第 97 行）防并发重入 |
| B | 新增独立重试队列，异步重投 | 多一处 entrypoint + imports 同步（你的 C5 约束），本期只有一个接入方，投入产出不划算；**仅在将来要支持「批量重投全部死信」时才需要** |
| C | 接口内同步执行任务体 | **排除**：会把 HTTP 请求阻塞在 embedding 上，必然超时，且绕过 Celery 的并发与锁机制 |

> 澄清一个容易混淆的点：A 说的「同步」是指**接口同步调用 `apply_async` 后立即返回**，不是同步跑任务。任务本身仍在 worker 执行。所以不存在接口超时问题。

### 4.2 关键约束（必须遵守）

1. **只接受 `status == dead_letter`**，其他状态返回 409 并带上当前状态。
2. **复用同一行、同一 `task_id`**，不要新建行：
   - 前端正在轮询这个 id，换 id 会断链；
   - `(tenant_id, dataset_id, trace_id)` 唯一约束下新建行必须换 trace_id，语义就变了。
3. **重置字段**：`status=pending`、`attempt_count=0`、`last_error=None`、`last_error_code=None`、`completed_at=None`、`next_attempt_at=None`、`processing_started_at=None`。保留 `document_id`/`version_id`/`payload`（重投要用）。
4. **重置与投递要有原子性语义**：先 commit 重置，再 `apply_async`。若投递失败（broker 不可用），把状态回滚回 `dead_letter` 并返回 503，避免留下一个永远不会被执行的 `pending` 僵尸行。
5. **幂等**：并发点两次重试时，靠 worker 侧的 Redis 锁 + `already_processing` 短路兜底，接口层不额外加锁。

---

## 5. FlaskTask 薄钩子最小实现范围

> 原则：**只做 opt-in 埋点，不接管重试、不接管死信、不碰未标记任务。**

### 5.1 做

在 `api/extensions/ext_celery.py:134` 的 `FlaskTask.__call__` 内：

1. **判定**：从 `kwargs` 取 `task_execution_id`；为空 → **直接 `return self.run(...)`，零额外开销、零行为变化**。这是 opt-in 的唯一开关，也是避免与 20 个自带 `max_retries` 的任务嵌套的关键。
2. **埋点**：命中时，在 `run()` 前记录 `started_at`；`run()` 成功写 `success`；异常时写 `last_error` / `last_error_code`。
3. **绝不吞异常**：埋点全部包独立 try/except（DB 抖了也要能继续），且 `run()` 的异常必须原样 `raise` —— 重试决策权留给任务自身。

### 5.2 不做

- 不调 `self.retry()`、不算退避、不写 `dead_letter`（这些都是 `document_async_task` 自己的职责）
- 不给 `on_retry` / `on_failure` 加业务逻辑（按你的 C4，它们只做幂等兜底；本期甚至可以**完全不动**这两个钩子）
- 不改 `imports` 列表、不加队列（不新增队列）
- 不引入任何 AST 解析工具（避免 `batch_create_segment_to_index_task.py` 的 BOM 问题）

### 5.3 一个必须提前说清的边界

你的 C4 要求「只有 `__call__` 能包住 `self.retry()` 自身的异常」——这条在**薄钩子方案下由任务自己实现**：三层 try 结构加在 `tasks/document_async_task.py:65-70` 的 `self.retry(...)` 外层（照抄 `ops_trace_task.py:132-142`），而不是加在基类里。因为基类不调 `self.retry()`，就没得包。

如果将来路线 2 让基类接管重试，那时再把三层 try 上移到基类。

---

## 6. R2 / R5 并行调研的验证步骤

### R2：索引任务重跑是否幂等 —— **已调研完毕，结论：不幂等，重跑必然产生重复数据**

> 这一节已从「待调研」改为「已结论」。以下均为静态分析可得的硬证据，不需要跑测试即可定论。

#### 决定性原因：`index_node_id` 是随机 uuid4

`core/rag/index_processor/processor/paragraph_index_processor.py:110-116`（`transform` 内）：

```python
doc_id = str(uuid.uuid4())            # 每次 transform 都是新值
hash = helper.generate_text_hash(document_node.page_content)   # doc_hash 才是稳定的
document_node.metadata["doc_id"] = doc_id
```

`qa_index_processor.py:87/202/269`、`parent_child_index_processor.py:90/136/260/283/291`、`paragraph_index_processor.py:112/210/225` 全部同理。

写入落点 `core/rag/docstore/dataset_docstore.py:67-186` 的 `add_documents` 是 **select-then-insert-or-update**（无 `on_conflict_*`）：先 `get_document_segment(doc_id=...)` 查，命中则覆盖更新，未命中则 `session.add()`。**因为 key 每次都是新 uuid，第二次永远查不中，必然走 insert 分支 → 行数翻倍。** `max_position` 每次从库里取 max 继续递增，所以不会撞 position，静默成功。

`models/dataset.py:558-568` 的 `DocumentSegment` 没有 `index_node_id` 唯一约束兜底（只有普通 index）。

#### 四类污染

| 环节 | 是否重复 | 原因 |
|---|---|---|
| `document_segments` 行 | **翻倍** | index_node_id 每次变，update 分支永不命中 |
| `child_chunks` 行 | **翻倍**（父子索引） | 同上，`save_child` 走 insert |
| 向量库向量 | **翻倍** | `vector_base.py:71-72` 向量 id == `index_node_id`；且 `load` 走 `Vector.create`，**从不传 `duplicate_check`**（只有 `Vector.add_texts` 才有去重，`vector_factory.py:274-277`） |
| 旧数据 | **孤儿残留** | 无 delete；`segment_repository.py:104-117` 把旧 segment 打回 `INDEXING` 但**不改 `enabled`**，而检索恰恰按 `enabled` 过滤（`retrieval_service.py:485-487`）→ 旧行继续参与检索 |

#### 唯一有保护的地方，也正是「run() 不清理」的设计前提

`tasks/duplicate_document_indexing_task.py:135-166` 是**全仓库唯一**显式先删后插的地方：先 `index_processor.clean(...)` 删向量+关键词，再 `delete(DocumentSegment)`，然后才 `indexing_service.run()`。

这解释了 `services/knowledge/indexing/execution.py:105-140` 里为什么只有 `start == "splitting"` 才调 `clear_for_indexing`——**`start == "parsing"` 这个默认路径假定调用方自己已经清理过了**。

反例：`recover_document_indexing_task.py:38-48` 的 `waiting/parsing/cleaning` 分支直接 `run()`，**没有任何清理，会翻倍**（另两个状态分支走 `run_in_splitting_status` / `run_in_indexing_status`，是幂等的）。

#### ★ 新发现的第三层吞异常（比 R1 更深，直接影响路线 2 的工作量）

`services/knowledge/indexing/execution.py:132-138`：

```python
except DocumentIsPausedError:
    raise
except DocumentIsDeletedPausedError:
    logger.info(...)
except Exception as error:
    logger.exception("Document indexing failed: %s", ref)
    self._documents.fail_indexing(ref, self._backend.describe_error(error))
    # ← 没有 raise
```

**除 `DocumentIsPausedError` 外全部吞掉。** 后果：`run()` 正常返回 → `document_indexing_task._document_indexing` 的 `has_error` 保持 `False` → 任务被标记 **SUCCESS**，Celery 重试根本无从触发。

> 也就是说：路线 2 只改 `document_indexing_task.py` 补 `raise` **是不够的**，必须连 `IndexingService._run` 一起改（两层），否则重试仍然不会触发。这一点请纳入路线 2 的排期评估。

#### 验证用例（回归防护用，非判定用）

静态证据已足够定论，下面这条测试的价值是**防止将来被改回去**：

- **推荐做法：只断言 DB 行数，不 mock embedding / 向量库**，直接测 `SQLAlchemySegmentRepository`：
  ```python
  repo.save_for_indexing(job, chunks_with_fresh_uuid(), [3,3,3])
  assert count_segments(doc.id) == 3
  repo.save_for_indexing(job, chunks_with_fresh_uuid(), [3,3,3])   # 第二次
  assert count_segments(doc.id) == 6   # 当前代码会翻倍到 6
  ```
  其中 `chunks_with_fresh_uuid()` 复刻 `paragraph_index_processor.transform` 的行为（content 固定、`doc_id` 每次 `uuid4()`），这样只隔离出 id 差异这一个变量。
- **若要端到端测 `run()`**，需 mock：`ModelManager.for_tenant`（embedding 返回定长零向量）、`Vector`（断言两次 `create` 的 `metadata["doc_id"]` 集合完全不相交）、`IndexProcessorFactory`、`redis_client.get` 返回 None（避免 `DocumentIsPausedError`）、`current_app`。
- 现成夹具可直接复用：`tests/unit_tests/services/knowledge/indexing/adapters/test_execution.py:331-360`（已演示如何 patch `Vector` 并拦截 `create`）。
- **注意**：因为 `_run` 吞异常，测试**不能**用 `pytest.raises` 判失败，必须直接查 DB 行数 / `document.indexing_status == ERROR`。

#### 判定：**不开启索引任务重试**

路线 2 前置条件不成立。若要开，必须先补幂等，二选一：
- **治本（改 key）**：`index_node_id` 从 `uuid4()` 换成由 `(document_id, version_id, 序号/内容)` 派生的稳定值（如 `uuid5`）。这样 `add_documents` 的 update 分支自然命中，向量因 id 稳定而天然 upsert。**风险**：派生 key 必须包含 `version_id`，否则会跨版本误覆盖历史版本数据。
- **治标（先删后插）**：在 `_run` 的 `start == "parsing"` 分支前、或各 task 调用方，补上 `duplicate_document_indexing_task.py:135-166` 那套清理逻辑。

### R5：补 `raise` 后与 `_document_indexing_with_tenant_queue` 的 finally 交互

**目标**：确认异常外抛触发 Celery 重试时，租户隔离队列不会饥饿（下一批任务没人拉）或重复消费（同一批被拉两次）。

**关注点**（`tasks/document_indexing_task.py:205-244`）：

- `finally` 里 `TenantIsolatedTaskQueue.pull_tasks()` 用 `rpop` 取下一批 → 重试时不会重复拉同一批（已弹出），但要确认**异常传播后 `finally` 仍会执行**（Python 语义保证会，但需实测）
- 无下一批时 `delete_task_key()` 清标志 → 需确认重试执行时标志状态正确，不会出现「标志已清但任务还在跑」导致并发超限
- `TENANT_ISOLATED_TASK_CONCURRENCY` 的并发上限在重试路径下是否被正确遵守

**验证步骤**

1. 写单测：mock `_document_indexing` 抛异常、`pull_tasks` 返回 1 个下一批任务 → 断言异常**确实向外传播**（这是路线 2 能成立的前提）+ `task_func.apply_async` 被调用一次
2. 反例：mock `pull_tasks` 返回空 → 断言 `delete_task_key()` 被调用
3. 模拟「连续重试 3 次」→ 断言 `pull_tasks` 被调用 4 次（首投 + 3 次重试）、每批任务只被 `apply_async` 一次、标志最终被清理
4. 集成层（需要真实 Redis）：起一个最小 worker，确认重试期间队列标志的 Redis key TTL 行为符合预期

> 这两项都属于**路线 2 的前置**，本期路线 1 不阻塞。建议 R2 优先（数据污染风险更高），R5 次之。

---

## 7. 本期路线 1 的最终变更清单（汇总）

**新增文件（3 个）**
1. `api/migrations/versions/<rev>_extend_document_async_task_retry.py`（`down_revision="c7d8e9f0a1b2"`）
2. `api/tests/unit_tests/services/knowledge/test_document_async_task_retry.py`（退避序列 + 死信流转 + 口径）
3. `api/tests/unit_tests/controllers/service_api/...`（查询 + 手动重试各若干例）

**修改文件（5 个）**
1. `api/models/enums.py:135` — 加 `DEAD_LETTER`
2. `api/models/dataset.py:606` — 加 `last_error_code` / `next_attempt_at`（进度列待定）
3. `api/tasks/document_async_task.py` — `RETRY_DELAY_SECONDS` 30→2；三层 try 包 `self.retry()`；终态写 `dead_letter`；修 `attempt_count` 短路漏洞
4. `api/extensions/ext_celery.py` — `FlaskTask.__call__` 加 opt-in 埋点（约 20 行，不改 `imports`）
5. `api/controllers/service_api/dataset/document.py` — 新增 2 个 Resource + 扩展 `DocumentAsyncTaskResponse` + 注册进 `register_response_schema_models`

**明确不动**
- `api/tasks/document_indexing_task.py` 的 try-except（R1，等 R2/R5 结论，路线 2 处理）
- `api/docker/entrypoint.sh`（本期不新增队列）
- `KnowledgeFSLifecycleOutboxStatus`、`models/trigger.py` 的 `retry_count` 等历史命名
- 其余 77 个任务文件

---

## 8. 等你确认的 4 个点

1. **第 1.1 节 `failed` 语义取舍**：`failed` 改为「可重试中间态」（我倾向）vs 保留 `failed` 为终态 + 新增 `retrying`。
2. **第 1.3 节进度**：派生（推荐，不建列）vs 建 `progress_current/progress_total` 两列。
3. **第 2.1 节挂载位置**：service_api（推荐）vs console。
4. **第 1.2 节**：是否加 `next_attempt_at` 列（我建议加，否则前端无法区分「还会重试」和「已停止」）。

确认后我输出：完整 diff、alembic 迁移脚本、单元测试。

# R2 / R5 测试用例文档（仅文档，本期不实现业务代码）

> **本期红线**：路线 2 整体后置。本文只定义用例，**不修改** `document_indexing_task.py`
> 的异常捕获，**不打开**索引任务重试闭环。

---

## R2：索引任务重跑幂等性

### 结论（静态调研已定论，无需跑测试判定）

**不幂等，重跑必然产生重复数据。** 三条硬证据：

1. `core/rag/index_processor/processor/paragraph_index_processor.py:110-116`
   `doc_id = str(uuid.uuid4())`，每次 `transform` 都是新值
   （`qa_index_processor.py:87/202/269`、`parent_child_index_processor.py:90/136/260/283/291`、
   `paragraph_index_processor.py:112/210/225` 同理）。
2. 写入落点 `core/rag/docstore/dataset_docstore.py:67-186` `add_documents` 是
   **select-then-insert-or-update**，无 `on_conflict_*`；按 `index_node_id` 查不中就
   `session.add()`。key 每次变 → 第二次永不命中 → 行数翻倍。
   `models/dataset.py:558-568` 对 `index_node_id` 只有普通 index、无唯一约束兜底。
3. 向量侧 `core/rag/datasource/vdb/vector_base.py:71-72` 向量 id == `index_node_id`；
   `load` 走 `Vector.create`，**从不传 `duplicate_check`**（只有 `Vector.add_texts` 有去重）。

### 污染矩阵

| 环节 | 是否重复 | 原因 |
|---|---|---|
| `document_segments` | 翻倍 | index_node_id 每次变 |
| `child_chunks` | 翻倍（父子索引） | 同上，`save_child` 走 insert |
| 向量库 | 翻倍 | 向量 id == index_node_id，全新 uuid |
| 旧数据 | 孤儿残留 | `segment_repository.py:104-117` 把旧 segment 打回 `INDEXING` 但**不改 `enabled`**，而检索按 `enabled` 过滤（`retrieval_service.py:485-487`） |

### 为什么 `run()` 不清理

`services/knowledge/indexing/execution.py:105-140`：`_run` 只有 `start == "splitting"` 才
`clear_for_indexing`；**`start == "parsing"`（`run()` 默认路径）假定调用方自己先删了**。
全仓库唯一守这个约定的是 `tasks/duplicate_document_indexing_task.py:135-166`。
反例：`recover_document_indexing_task.py:38-48` 的 `waiting/parsing/cleaning` 分支直接 `run()` → 会翻倍。

### ★ 第三层吞异常（路线 2 的真实工作量）

`services/knowledge/indexing/execution.py:132-138`：除 `DocumentIsPausedError` 外，
`except Exception` 只调 `fail_indexing`、**不 raise** → `run()` 正常返回 →
`_document_indexing` 的 `has_error` 保持 `False` → 任务被标记 **SUCCESS**。

> 路线 2 只改 `document_indexing_task.py` 补 `raise` 不够，必须连 `IndexingService._run`
> 一起改（两层）。这是排期评估时必须计入的。

### 回归测试用例定义（本期不实现，仅归档）

**用例 R2-1（推荐，纯 DB 断言，不 mock 外部服务）**

```
前置：SQLite session + dataset + document
步骤：
  1. chunks_a = 复刻 transform 行为构造 3 个 chunk（content 固定，doc_id 每次 uuid4()）
  2. repo.save_for_indexing(job, chunks_a, [3,3,3])
  3. 断言 count(DocumentSegment where document_id=...) == 3
  4. chunks_b = 同样 content 但 doc_id 重新 uuid4()
  5. repo.save_for_indexing(job, chunks_b, [3,3,3])
  6. 断言 count(...) == 6        # 当前代码的实际行为，翻倍
期望（幂等修复后）：第 6 步应为 == 3
变异测试：把 add_documents 改成纯 insert 后本用例必须变红，证明断言非恒真
```

**用例 R2-2（端到端，需 mock）**

| 依赖 | 位置 | mock 方式 |
|---|---|---|
| Embedding model | `adapters/execution.py:112` | `ModelManager.for_tenant` → 定长零向量 |
| 向量库 | `adapters/execution.py:215` | patch `Vector`，断言两次 `create` 的 `metadata["doc_id"]` 集合**完全不相交** |
| 解析器 | `adapters/execution.py:90` | `IndexProcessorFactory` → 固定返回 N 个 chunk（隔离出 id 差异一个变量） |
| Redis | `adapters/execution.py:83` | `redis_client.get` 返回 None（避免 `DocumentIsPausedError`） |
| Flask app | `adapters/execution.py:156` | `current_app` → MagicMock（ThreadPoolExecutor 内需要） |

夹具可复用 `tests/unit_tests/services/knowledge/indexing/adapters/test_execution.py:331-360`
（已演示如何 patch `Vector` 并拦截 `create`）。

> **注意**：因 `_run` 吞异常，测试**不能**用 `pytest.raises` 判失败，
> 必须直接查 DB 行数或 `document.indexing_status == IndexingStatus.ERROR`。

### 修复方向（路线 2 前置，二选一）

- **治本（改 key）**：`index_node_id` 从 `uuid4()` 换成 `uuid5(document_id, version_id, 序号)`。
  派生 key **必须含 `version_id`**，否则会跨版本误覆盖历史版本数据。
- **治标（先删后插）**：在 `_run` 的 `parsing` 分支前、或各 task 调用方，
  补 `duplicate_document_indexing_task.py:135-166` 的清理逻辑。

---

## R5：补 `raise` 后与 `_document_indexing_with_tenant_queue` 的 finally 交互

> 关注 `tasks/document_indexing_task.py:205-244`：`finally` 里 `pull_tasks()`（rpop 取下一批）
> + `apply_async`，或在无任务时 `delete_task_key()`。

### 用例 R5-1：异常外传时 finally 仍拉取下一批

```
前置：mock _document_indexing 抛异常；mock TenantIsolatedTaskQueue.pull_tasks 返回 1 个 next_task
步骤：调用 _document_indexing_with_tenant_queue(tenant_id, dataset_id, doc_ids, task_func)
断言：
  a) 异常确实向外传播（pytest.raises）—— 这是路线 2 能成立的前提
  b) task_func.apply_async 被调用 1 次，kwargs 与 next_task 一致
  c) tenant_isolated_task_queue.set_task_waiting_time 被调用
```

### 用例 R5-2：无下一批时清理标志

```
前置：mock _document_indexing 抛异常；pull_tasks 返回空
断言：delete_task_key 被调用 1 次；apply_async 未被调用
```

### 用例 R5-3：连续重试 3 次不重复消费、不队列饥饿

```
前置：pull_tasks 依次返回 [batch1, batch2, batch3, None]；_document_indexing 每次抛异常
步骤：模拟首投 + 3 次重试（共 4 次进入）
断言：
  a) pull_tasks 被调用 4 次
  b) 每个 batch 只被 apply_async 一次（无重复消费）
  c) 最后一次 delete_task_key 被调用（标志最终清理，无队列饥饿）
  d) TENANT_ISOLATED_TASK_CONCURRENCY 并发上限在每次拉取时被遵守
```

### 用例 R5-4（集成，需真实 Redis）

起最小 worker，故障注入连续失败，观察 `TenantIsolatedTaskQueue` 的 Redis key TTL
在重试期间的行为：确认不会因 TTL 过期导致「标志已清但任务仍在跑」的并发超限。

---

## 本期执行边界

| 项 | 本期 | 路线 2 |
|---|---|---|
| R2 修复幂等 | ❌ | ✅ 前置 |
| R5 用例实现 | ❌（仅文档） | ✅ |
| 改 `document_indexing_task.py` 的 `except Exception` | ❌ | ✅ |
| 改 `IndexingService._run` 的吞异常 | ❌ | ✅（与上一项必须同时做） |
| 打开索引任务重试闭环 | ❌ | ✅（幂等结论落地后） |

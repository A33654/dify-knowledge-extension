# 本地验证命令清单

> 工作目录：`E:\work\dify-main\dify-main\api`
> **坑**：bash 直接跑 pytest 输出会丢，一律重定向到文件再读。

---

## 1. 前置：确认迁移链

```bash
cd /e/work/dify-main/dify-main/api
uv run --project . flask db heads > /tmp/heads.txt 2>&1; cat /tmp/heads.txt
```

期望：改造前后都应只有单个 head。改造前为 `c7d8e9f0a1b2`，
应用本次迁移后为 `a1b2c3d4e5f6`。

## 2. 应用迁移 + 校验可回滚

```bash
cd /e/work/dify-main/dify-main/api
uv run --project . flask db upgrade > /tmp/upgrade.txt 2>&1; cat /tmp/upgrade.txt
uv run --project . flask db downgrade > /tmp/downgrade.txt 2>&1; cat /tmp/downgrade.txt
uv run --project . flask db upgrade > /tmp/upgrade2.txt 2>&1; cat /tmp/upgrade2.txt
```

> 没起 Postgres 时用不了上面的命令，改用静态检查：
> ```bash
> uv run --project . python -c "import importlib,sys; m=importlib.import_module('migrations.versions.2026_10_26_2100-a1b2c3d4e5f6_extend_document_async_task_retry'); print(m.revision, m.down_revision)" > /tmp/mig.txt 2>&1; cat /tmp/mig.txt
> ```
> 注意文件名带 `-`，作为模块名不合法，改用 exec 或临时复制成合法模块名再 import。

## 3. 单元测试（按文件分别跑，不要整目录）

```bash
cd /e/work/dify-main/dify-main/api

uv run --project . python -m pytest tests/unit_tests/tasks/test_document_async_task.py -p no:cacheprovider --no-cov -q > /tmp/t1.txt 2>&1; cat /tmp/t1.txt

uv run --project . python -m pytest tests/unit_tests/services/knowledge/test_document_async_task_service.py -p no:cacheprovider --no-cov -q > /tmp/t2.txt 2>&1; cat /tmp/t2.txt

uv run --project . python -m pytest tests/unit_tests/extensions/test_ext_celery.py -p no:cacheprovider --no-cov -q > /tmp/t3.txt 2>&1; cat /tmp/t3.txt
```

**已知坑**
- 覆盖插件删 `.coverage` 会触发 safe-delete 失败 → 必须带 `--no-cov -p no:cacheprovider`。
- `tests/unit_tests/services/knowledge` **整目录跑会崩**（导入 `controllers/console/__init__.py`
  时 exit 127）→ 按文件分别跑。
- `tests/unit_tests/services/knowledge/test_dataset_service.py` 有 4 例 + 1 ERROR 的
  **既有失败**（storage 单例未 `init_app`），非本次引入，不要因为红就回滚。

## 4. 变异测试（证明新增断言非恒真）

```bash
cd /e/work/dify-main/dify-main/api
# 临时把 RETRY_DELAY_SECONDS 改回 30
uv run --project . python -m pytest tests/unit_tests/tasks/test_document_async_task.py -p no:cacheprovider --no-cov -q > /tmp/mut1.txt 2>&1; cat /tmp/mut1.txt
```

期望：`test_retry_policy_is_pinned_to_2_4_8_seconds` 与
`test_failure_schedules_exponential_backoff_and_records_next_attempt` 变红 → 断言有效。
验证后还原。

## 5. Lint

```bash
cd /e/work/dify-main/dify-main/api
# 只对自己改过的文件跑，避免大范围 reformat
uv run --project . ruff check --fix \
  models/enums.py models/dataset.py \
  tasks/document_async_task.py \
  extensions/ext_celery.py \
  services/knowledge/document_async_task_service.py \
  controllers/service_api/dataset/document.py \
  controllers/service_api/dataset/error.py > /tmp/lint1.txt 2>&1; cat /tmp/lint1.txt

uv run --project . ruff format \
  tasks/document_async_task.py \
  extensions/ext_celery.py \
  services/knowledge/document_async_task_service.py \
  controllers/service_api/dataset/document.py > /tmp/lint2.txt 2>&1; cat /tmp/lint2.txt
```

**ruff 已知约束**
- `typing.Any` 被 banned-api + any-type 全面禁用 → 用 `object` / 具体类型。
- 未使用的 fixture 参数会报 unused-method-argument → 在断言里真正用到它。
- 禁复合断言（`a in x and b in x` 要拆开）。
- 禁 `flask_restx.reqparse`。
- 迁移文件 `import models as models` 会报 PLC0414，但仓库 98 个既有迁移同样如此，保持一致即可。

## 6. 响应契约校验（新增响应模型必做）

```bash
cd /e/work/dify-main/dify-main
python api/dev/lint_response_contracts.py > /tmp/contracts.txt 2>&1; cat /tmp/contracts.txt
```

期望：`735 valid / 0 mismatch` → 变成 `736 valid / 0 mismatch`（新增
`DocumentAsyncTaskStatusResponse` 后）。**若 mismatch 非 0，说明忘了加进
`register_response_schema_models`。**

## 7. 路由注册自检

```bash
cd /e/work/dify-main/dify-main/api
uv run --project . python -c "
from app_factory import create_app
app = create_app()
for r in app.url_map.iter_rules():
    if 'async-tasks' in str(r):
        print(r, sorted(r.methods))
" > /tmp/routes.txt 2>&1; cat /tmp/routes.txt
```

期望输出两条规则：
- `/v1/datasets/<uuid:dataset_id>/documents/async-tasks/<uuid:task_id>` — GET
- `/v1/datasets/<uuid:dataset_id>/documents/async-tasks/<uuid:task_id>/retry` — POST

## 8. 队列配置自检（确认本期确实没动）

```bash
cd /e/work/dify-main/dify-main
grep -n "DEFAULT_QUEUES" api/docker/entrypoint.sh
grep -n "tasks\." api/extensions/ext_celery.py | head -30
```

期望：`DEFAULT_QUEUES` 两行内容与改造前一致；`ext_celery.py` 的 `imports` 列表未新增条目。

## 9. 手工冒烟（可选，需起服务）

```bash
# 1) 创建异步任务拿 task_id
curl -X POST "http://localhost:5001/v1/datasets/<dataset_id>/document/create-by-file" \
  -H "Authorization: Bearer <dataset-api-key>" -F "file=@a.txt" -F 'data={}'

# 2) 轮询
curl "http://localhost:5001/v1/datasets/<dataset_id>/documents/async-tasks/<task_id>" \
  -H "Authorization: Bearer <dataset-api-key>"

# 3) 构造死信后手动重试（预期 202；非死信预期 409）
curl -X POST "http://localhost:5001/v1/datasets/<dataset_id>/documents/async-tasks/<task_id>/retry" \
  -H "Authorization: Bearer <dataset-api-key>"
```

---

## 验收对照

| 验收项 | 验证方式 |
|---|---|
| 退避 2/4/8s | 第 3 节 `test_failure_schedules_exponential_backoff_and_records_next_attempt` 三参数 |
| 死信流转 | 第 3 节 `test_retry_budget_exhausted_marks_dead_letter` |
| Broker 异常不静默丢失 | 第 3 节 `test_broker_failure_while_scheduling_retry_still_marks_dead_letter` |
| attempt_count 计数修复 | 第 3 节 `TestDocumentAsyncTaskAttemptCount` 两条 |
| 枚举 dead_letter | 第 3 节 `test_status_enum_exposes_dead_letter_as_the_terminal_state` |
| 接口可路由 + 契约 | 第 6、7 节 |
| 未越红线（不动队列/索引任务） | 第 8 节 |

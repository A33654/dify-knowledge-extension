# Dify 工作区长期笔记

## 已定决策

### 文档版本缓存：删除文档 / 删除知识库**不**主动失效缓存（2026-10-26 定）
- 决定：接受 300s TTL 兜底，`DatasetService.delete_document` / `delete_dataset` 不显式删 key。
- 依据：两个读接口（`/datasets/{id}/documents/{doc_id}/versions[/{vid}]`）的 RBAC locator
  是 `DatasetByDocument`，文档不存在时直接 `raise NotFound` → **删文档后缓存根本读不到**，
  无用户可见脏读。另：models 里无 `ondelete`/`cascade`，删 Document 不级联删 DocumentVersion，
  所以缓存陈旧永不超过 DB 陈旧。文档 id 是 uuid4，不复用。
- **翻转条件**：若出现「删除即消失」的合规要求（缓存含 file_name/file_hash），
  或新增了绕过 `DatasetByDocument` 的读路径（worker / 内部 service / 新接口），需改投方案B。
  届时优先补**删知识库**那条（`DatasetId.locate` 不校验 dataset 是否存在，门禁较弱），
  删文档那条性价比最低。
- 方案B 若实施：只能「查 id 再逐个删 key」，**不能**按前缀 scan（见下）。
  注意 `document_versions` 无单独 `dataset_id` 索引，查询必须带 `tenant_id`。
  commit 后不能再访问被删实例的属性（DetachedInstanceError），需先快照到局部变量。

## 环境事实

- 该工作区**不是 git 仓库**，无法 `git diff`。
  **但原始基线压缩包在 `E:\work\dify-main.zip`（14918 文件，根 `dify-main/`）**，
  可逐文件比 CRC32 做精确 diff，远优于 mtime。脚本留在 `E:\work\dify-main\_zipcheck\`
  （compare.py 比对 / analyze.py 分类 / mkdiff.py 出 diff）。
  **坑**：zip 与工作区换行符不一致（CRLF vs LF），直接 diff 会把整文件显示成全删全加，
  必须先 `replace(b"\r\n", b"\n")` 再比（实测 dataset_service.py 8124 行 → 386 行）。
  报告产物：`.workbuddy/diff-report-vs-original.md`。
- 跑单测（bash 直接跑输出会丢，重定向到文件再读）：
  `cd api && uv run --project . python -m pytest <file> -p no:cacheprovider --no-cov -q`
  `tests/unit_tests/services/knowledge` 整目录跑会崩（导入 `controllers/console/__init__.py` exit 127），**按文件分别跑**。
- ruff 配置极严：
  - `typing.Any` 被 banned-api + any-type 全面禁用 → 用 `object` / 具体类型。
  - **未使用的 fixture 参数会报 unused-method-argument** → fixture 仍要生效时，就在断言里真正用到它，别删参数。
  - 禁复合断言（`a in x and b in x` 要拆成多行）。
- `RedisClientWrapper`（`extensions/ext_redis.py`）**没有** `scan`/`scan_iter`/`keys`，
  只有 `:253` 的 `__getattr__` 兜底且**不套 `REDIS_KEY_PREFIX`**；全仓库零 scan 先例。
  要做前缀批量删必须改公共基础设施，风险高。
- 已知既有失败（非本次引入）：`test_dataset_service.py` 4 例 + 1 ERROR，
  `storage` 单例未 `init_app` 导致 `AttributeError: 'Storage' object has no attribute 'storage_runner'`。

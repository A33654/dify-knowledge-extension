# Dify 知识库能力扩展包

基于 Dify 基座构建的独立知识库能力包，不侵入 Dify 底层框架。补齐文档版本管理、版本快照检索、数据集标注与文档异步索引四类生产级能力。

## 改动量化

- 77 个文件 · +2047 行 · -246 行
- 3 个数据库迁移 · 4 个后端服务/任务 · 5 个前端组件
- 138 个新增用例通过

## 核心能力

- **文档版本管理**：business_key + SHA256 去重，版本列表 / 详情 / 归档 / 提升
- **版本快照检索**：历史会话引用不漂移，引用气泡展示版本徽章与历史原文
- **数据集标注**：增删改查、标签、SFT/评估 JSONL 导出，RBAC 控制
- **文档异步索引**：trace_id 幂等、错误落库、手动恢复

## 文档

- [Feature Summary](docs/FEATURE_SUMMARY.md)

## 技术栈

Flask-RESTX · SQLAlchemy 2 · Alembic · Celery · Redis · Next.js · React · TanStack Query
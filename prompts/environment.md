# 环境

## 可用的取数工具

- `mc-query` — 阿里云 MaxCompute 取数
- `datawind-dashboard-skill` — dashboard 视图数据

你只能通过这些工具执行 SQL 查询。取数工具会自动校验字段名合法性，并将中文字段名映射成物理列名。如果校验失败，检查报错提示修正 SQL 后重试。

## Schema 来源

Schema 由 runbook-router 按需注入。你写 SQL 时使用 schema 中定义的中文字段名，系统自动映射成物理列名。漏斗阶段的值在 SQL 中仍用英文（如 `WHERE 漏斗阶段='created'`）。

# bi-bridge eval harness

bi-bridge Agent 的自动化评估系统。改了 prompt、schema 或 runbook 之后，跑一轮 eval 确认没有回归。

## 快速开始

```bash
cd bi-skills/bridge/eval

# 列出所有测试问题
python3 eval.py --list

# 只看 prompt 不实际调用（检查 runbook 路由是否正确）
python3 eval.py --dry-run

# 跑全部问题（约 5-10 分钟，取决于 Agent 响应速度）
python3 eval.py

# 只跑前 3 题
python3 eval.py --max 3

# 只跑指定题
python3 eval.py -q q1_simple_funnel -q q6_cross_table_join
```

## 输出

每次运行生成 `results_<timestamp>.json`，结构如下：

```json
{
  "timestamp": "2026-06-29T...",
  "total": 10,
  "passed": 8,
  "failed": 2,
  "results": [
    {
      "id": "q1_simple_funnel",
      "question": "昨天 Banxa 的整体转化率是多少？",
      "status": "pass",
      "duration_secs": 45.2,
      "sql_valid": true,
      "sql": "SELECT ...",
      "tables_used": ["Banxa转化漏斗"],
      "cost_usd": 0.05,
      "num_turns": 3
    }
  ]
}
```

## 判定规则

- **pass**: 有结论 + 有 SQL + SQL 字段校验通过
- **fail**: SQL 字段不合法（幻觉字段）/ 无输出 / 超时 / 报错
- **warn**: 有结论但未提取到 SQL（格式问题，需人工确认）
- 边界题（如问了 schema 里没有的字段）: Agent 明确告知无法计算即为 pass

## 添加新问题

编辑 `questions.yaml`，按格式添加：

```yaml
  - id: q_new_scenario
    question: "你的测试问题"
    difficulty: easy|medium|hard
    features: [feature_tag_1, feature_tag_2]
    expected_tables: [Banxa转化漏斗]
    notes: "预期行为说明"
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLAUDE_BIN` | `claude` | claude 命令路径 |
| `EVAL_MAX_TURNS` | `20` | Agent 最大对话轮数 |
| `EVAL_ALLOWED_TOOLS` | `Skill Read Grep Glob` | 允许的工具 |
| `EVAL_TIMEOUT` | `300` | 单题超时秒数 |

# ADR-0003: Runbook 路由模式

**状态**: Implemented
**日期**: 2026-06-29

## Context

bi-bridge 初期只有一张 Banxa 漏斗表。prompt 里塞一份 schema + 几个 exemplar，LLM 能正常工作。当第二张表（Banxa 订单）加进来后，问题出现了：

1. **context 膨胀**：两张表的 schema + 各自的 exemplar + 各自的分析规则全部塞进每次请求的 prompt，token 消耗翻倍，但大部分内容和当前问题无关
2. **噪声干扰**：LLM 看到两张表的字段时，容易在单表查询里错误引用另一张表的字段，或者在不需要 JOIN 的场景做不必要的跨表查询
3. **规则冲突**：漏斗表和订单表各有自己的分析框架和注意事项（如漏斗表的"不要跨阶段 SUM"和订单表的"金额已是美元不需要汇率转换"），混在一起 LLM 难以区分哪条规则适用于当前场景

这个问题会随着表数量增长线性恶化。

## Decision

引入 runbook 路由模式：每个 runbook 是一个 YAML 文件，定义一个分析场景需要的全部上下文；`runbook-router.py` 根据用户问题选择匹配的 runbook，只注入相关部分。

### Runbook 结构

每个 runbook YAML 包含五个部分：

- **tables**: 引用的语义表名列表（对应 schema/*.yaml），router 据此调 schema-prompt.py 生成 schema 文本
- **exemplars**: 引用的 exemplar 集合名列表，router 据此调 inject-exemplars.py 生成 few-shot 示例
- **triggers**: 关键词/正则列表，用于匹配用户问题
- **analysis**: 分析框架文本（如漏斗分析的"先确认整体 → 逐级拆解 → 维度下钻 → 量化贡献度"路径）
- **rules**: 场景专属规则（如"漏斗表每个 key 组合有 5 行，聚合时不要混阶段"）

### 路由匹配机制

`match_runbooks` 对每个 runbook 的 triggers 列表做匹配：

- 每个 trigger 先尝试作为正则（`re.search`）匹配用户问题
- 正则解析失败时退回子串匹配（`trigger.lower() in question.lower()`）
- 每命中一个 trigger 得 2 分，按总分降序排列
- 匹配结果可以是多个 runbook（所有得分 > 0 的都入选）

这意味着一个问题可以同时命中多个 runbook。比如"转化率和金额同时下跌"会命中 banxa-cross（trigger: `转化率.*金额`）。

### Fallback 策略

无任何 runbook 匹配时，加载全部 runbook 的 tables 和 exemplars。这保证了即使 trigger 覆盖不到的问题也不会拿到空 prompt，代价是退回到"全量 context"的状态。

### Prompt 组装

`assemble_prompt` 把匹配结果组装成完整 prompt，顺序是：

1. base-prompt.md（角色定义、环境说明、输出格式 — 每次必加载）
2. schema 文本（由匹配到的 tables 集合生成，自动去重）
3. 分析框架（各 runbook 的 analysis 拼接）
4. exemplar 文本（由匹配到的 exemplars 集合生成）
5. 场景专属规则（各 runbook 的 rules 拼接）

多表场景自动追加 JOIN 关系说明。

### 跨域 Runbook 的设计

banxa-cross 是一个跨域 runbook，`tables: [Banxa转化漏斗, Banxa订单]` 同时引用两张表。它的 trigger 用正则（如 `转化率.*金额`、`金额.*转化`）匹配需要联合分析的问题。它的 rules 里额外强调了跨表 JOIN 的注意事项（"漏斗表每个 key 有 5 行，必须先用 CTE 聚合再 JOIN"）。

这样，单表问题只拿到单表 context，跨表问题才加载多表 schema + JOIN 规则 + 跨域分析框架。

## Consequences

### Positive
- prompt 按需加载：单表问题的 prompt token 消耗大约减半，LLM 不会被无关 schema 干扰
- 分析框架和规则按场景隔离：漏斗分析的"逐级拆解"路径不会和订单分析的"客单价/手续费"路径混在一起
- 新增一个分析场景只需加一个 runbook YAML + 对应的 schema YAML，不用改 router 代码
- trigger 支持正则，可以表达复合条件（如 `转化率.*金额` 匹配"转化率和金额同时"但不匹配单独提到"转化率"）
- fallback 兜底保证了 trigger 覆盖不到的问题也有回应

### Negative
- trigger 关键词匹配是硬编码的，覆盖率取决于维护者能否穷举用户的措辞变体；用户换一种说法（如"漏斗各步骤的通过比例"没有命中任何 trigger 但"通过率"能命中），可能导致路由到 fallback 全量加载
- 评分机制简单（每个 trigger 命中 +2 分），没有权重区分：高特异性的 trigger（如"KYC"）和低特异性的 trigger（如"下跌"）得分相同
- 多 runbook 同时命中时没有优先级声明，只靠分数排序；如果 banxa-funnel 和 banxa-cross 都匹配到，两个 runbook 的 rules 会拼接在一起，可能出现重复或轻微矛盾
- runbook YAML 的简易解析器用正则处理，对格式要求严格（如 triggers 必须写成单行 `[...]`，不支持多行列表）

## Alternatives Considered

| 方案 | 优点 | 淘汰原因 |
|------|------|---------|
| 全量 prompt（不做路由） | 零额外复杂度，一份 prompt 搞定 | 表增多后 context 膨胀、噪声干扰、规则冲突问题会线性恶化；两张表时已经观察到 LLM 在单表查询里错误引用另一张表的字段 |
| 嵌入相似度匹配（embedding-based routing） | 语义理解更准，不依赖关键词穷举 | 需要引入 embedding 模型和向量检索依赖，与零依赖原则冲突；当前 runbook 数量（3 个）不需要这个精度；trigger 关键词在 BI 场景下覆盖率足够（用户问题的术语相对固定） |
| LLM 自行选择（把所有 runbook 描述给 LLM，让它选） | 最灵活，能处理任意措辞 | 多一轮 LLM 调用增加延迟和成本；选择结果不可预测，调试困难；违背"护栏放在 shell 层"的设计原则 |
| 按表名路由（检测问题中是否包含表名） | 最简单 | 用户不会在问题里写"Banxa转化漏斗"这种语义表名；跨域场景（同时涉及两张表但问题里没有任何表名）覆盖不到 |

# ADR-0002: Schema YAML 作为唯一事实来源

**状态**: Implemented
**日期**: 2026-06-29

## Context

LLM 生成 SQL 时需要知道表结构、字段含义、合法值、聚合方式。这些信息存在三个消费场景：

1. **prompt 注入**：告诉 LLM 有哪些表和字段可用
2. **SQL 映射**：把 LLM 生成的中文语义 SQL 翻译成物理 SQL
3. **SQL 校验**：检测 LLM 生成的 SQL 是否引用了 schema 中不存在的字段（幻觉检测）、聚合函数是否匹配声明类型、跨表引用是否缺少 JOIN

如果这三处各自维护一份 schema 定义，漏改是必然的。prompt 里写了"用户数"但 mapper 里没有对应物理名，或者 validator 的合法字段列表少了一个别名，都会导致端到端失败且难以定位。

## Decision

用 `schema/*.yaml` 作为唯一事实来源，所有消费方从 YAML 派生。

### YAML 结构

每个 YAML 文件定义一张语义表，包含：

- **table**: `name`（语义名，如"Banxa转化漏斗"）、`physical`（物理表名，如 `banxa_funnel`）、`comment`（表说明）、`ai_description`（给 LLM 的详细业务上下文和注意事项）
- **joins**: 声明和其他表的关联关系，包括 JOIN key 和注意事项（如"漏斗表每个 key 有 5 行，JOIN 前需先聚合"）
- **fields**: 每个字段包含 `name`（中文语义名）、`physical`（物理列名）、`datatype`、`type`/`aggregate`（聚合类型声明）、`alias`（别名列表）、`values`（枚举值映射）、`comment`（业务含义和使用注意事项）

### 四个派生用途

**schema-prompt.py**：读取 YAML，输出兼容 SuperSonic 格式的 prompt schema 文本。LLM 看到的是 `Table=[Banxa转化漏斗]`、`Metrics=[<用户数 AGGREGATE 'SUM'>]`、`Dimensions=[<国家 DATATYPE 'varchar'>]` 这种格式。多表时追加 JOIN 关系说明。

**sql-mapper.py**：从 YAML 构建 `{语义名: 物理名}` 的扁平映射（包括别名），按名称长度降序替换 SQL 中的中文名为物理名。LLM 写 `SELECT 日期, SUM(用户数) FROM Banxa转化漏斗`，mapper 翻译成 `SELECT ds, SUM(users) FROM banxa_funnel`。

**sql-validator.py**：从 YAML 提取所有合法标识符（语义名 + 物理名 + 表名 + 枚举值），校验 SQL 中引用的标识符是否存在。额外校验两类问题：第一，聚合函数类型是否匹配 schema 声明（如 `type: countDistinct` 的字段用了 `SUM()` 会报错，用了 `COUNT()` 但缺少 `DISTINCT` 也会报错）；第二，SQL 引用了多张表的字段但 FROM/JOIN 中缺少对应表时，给出 JOIN 建议。

**runbook 的 tables 字段**：runbook YAML 通过 `tables: [Banxa转化漏斗]` 引用语义表名，runbook-router 再调 schema-prompt.py 按需生成 schema 文本注入 prompt。

### 中文语义名的设计考量

LLM 用中文字段名写 SQL，而不是直接用物理列名。原因是中文名自带业务语义（"用户数"比"users"更明确这是一个 SUM 指标而不是用户列表），且用户在群里的提问是中文，LLM 从问题到 SQL 的映射路径更短。alias 机制允许同一个物理字段有多个中文别名（如"用户数"/"人数"/"用户量"），提高 LLM 命中率。

### 零依赖 fallback

三个 Python 脚本都实现了"无 PyYAML 时的最小解析器"，只处理 schema YAML 的固定结构。sql-validator.py 也实现了"无 sqlglot 时的正则 fallback"。这和 ADR-0001 的零依赖原则一致：schema 工具链在只有 stock Python 的 Mac 上也能跑。

## Consequences

### Positive
- 单一事实来源：改一处 YAML，prompt、mapper、validator 同步生效，不可能漏改
- 聚合类型声明（type: sum/count/countDistinct）让 validator 能在 SQL 执行前拦截聚合函数误用，这是纯 schema 校验做不到的
- ai_description 字段让 YAML 承担了"给 LLM 的业务背景 + 注意事项"的角色，不只是技术 schema
- 别名机制降低了 LLM 因措辞差异写错字段名的概率
- 枚举值映射（values）让 validator 能区分合法值和幻觉值
- JOIN 声明让 validator 能检测跨表引用并给出具体 JOIN 建议

### Negative
- YAML 的表达能力有限：复杂的校验规则（如"漏斗表不能直接 SUM(users) 跨阶段"）只能写在 comment 和 ai_description 里靠 LLM 理解，不能被 validator 程序化执行
- 每新增一张表需要同时写 YAML 和 runbook，前期建设成本高于直接在 prompt 里硬编码 schema
- sql-mapper 的字符串替换是贪心的（按长度降序），如果两个语义名存在包含关系（如"日期"和"数据日期"），需要靠排序保证正确性，边界情况不好测试
- 无 PyYAML 的 fallback 解析器只覆盖当前 YAML 结构的子集，如果 YAML 格式扩展（如嵌套 map、多行列表），fallback 需要同步更新

## Alternatives Considered

| 方案 | 优点 | 淘汰原因 |
|------|------|---------|
| prompt 里直接硬编码 schema | 零前期成本，改 prompt 文件就行 | 当表增多或 schema 变更时，prompt、mapper、validator 之间的一致性无法保证；实测 prompt 漏更新是最常见的端到端失败原因 |
| SuperSonic 的 DSL | 成熟的语义层方案，有配套的 prompt 生成和 SQL 改写 | 是一个 Java 服务，部署和维护成本远超当前需求；bi-bridge 只需要"YAML → 文本"的单向派生 |
| dbt 的 YAML（schema.yml） | 行业标准，工具生态丰富 | dbt YAML 面向 data pipeline 的测试和文档，缺少 ai_description、中文别名、聚合类型声明这些 LLM 场景的字段；适配成本不低于自建 |
| Cube 的 data model YAML | 天然面向 BI 查询，有 measure/dimension 的语义 | 同样是一个服务端组件，引入它只为了 schema 定义是杀鸡用牛刀 |
| JSON Schema | 标准化、有校验工具 | 可读性差（大量花括号嵌套），分析师不愿意直接编辑 |

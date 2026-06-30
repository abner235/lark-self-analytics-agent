---
name: mc-query-sandbox
description: "【Sandbox 模拟】模拟 mc-query 对阿里云 MaxCompute 取数。需要查询 Banxa 转化漏斗数据、写 SQL 取数、分析转化率/各阶段流失时使用。底层跑在本地 sqlite，方言兼容 SELECT/JOIN/GROUP BY/CASE WHEN。"
---

# mc-query-sandbox（取数沙盒）

模拟生产环境的 `mc-query` skill。通过运行 CLI 把 SQL 发给数据仓库并取回结果。

## 取数方式
运行（用 Bash，工作目录为 sandbox 根，mcq-safe 就在当前目录）：
```
./mcq-safe "<你的 SQL>"
```
返回带表头的结果表。`mcq-safe` 会先校验 SQL 只引用了下面「可用表」里的表和字段（挡幻觉字段/越权表），
再执行。如果校验失败，按报错把字段名改成下方 Schema 里的名字后重试，不要绕过校验。

## 可用表
`banxa_funnel(ds, country, pay_method, stage, users)` —— Banxa 法币入金转化漏斗的每日聚合。
- `ds`：日期分区，格式 `YYYY-MM-DD`（写查询务必裁 ds，别全表扫）
- `country`：SG / AU / HK / JP / GB
- `pay_method`：visa / mastercard / applepay
- `stage`：漏斗阶段，**顺序**为
  `created → kyc_passed → pay_submitted → pay_authorized → completed`
- `users`：处于该阶段的用户数

## 口径
- **整体转化率** = `completed 用户数 / created 用户数`
- **某一步通过率** = `下一阶段 users / 上一阶段 users`（如授权通过率 = pay_authorized / pay_submitted）
- 时区按数据基准（ds 已是业务自然日），不要自己做时区转换。

## 示例
```bash
# 按日整体转化率
./mcq-safe "SELECT ds,
       SUM(CASE WHEN stage='created'   THEN users END) AS created,
       SUM(CASE WHEN stage='completed' THEN users END) AS completed
     FROM banxa_funnel GROUP BY ds ORDER BY ds"

# 某天按支付方式拆解各阶段，定位是哪一步在掉
./mcq-safe "SELECT pay_method, stage, SUM(users) u
     FROM banxa_funnel WHERE ds='2026-06-27' GROUP BY pay_method, stage"
```

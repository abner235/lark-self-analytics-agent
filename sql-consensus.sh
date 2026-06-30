#!/usr/bin/env bash
# =============================================================================
# sql-consensus.sh — Self-Consistency 投票：并行调 LLM N 次，投票选共识 SQL
#
# 对高风险查询（涉及合规数据、外部报告等）使用，日常查询无需开启（3x 成本）。
#
# 原理（来自 SuperSonic）：
#   1. 用不同的 few-shot 样例组合构造 N 个 prompt
#   2. 并行调 Claude N 次
#   3. 提取每次返回的 SQL
#   4. 投票选出现次数最多的 SQL（归一化后比较）
#
# 用法:
#   ./sql-consensus.sh "昨天 visa 转化率是多少" --rounds 3
#   ./sql-consensus.sh "各国家完成人数" --rounds 3 --model sonnet
#
# 退出码: 0=有共识, 1=无共识（所有 SQL 都不同）, 2=用法错误
# =============================================================================
set -o pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"

# ---- 参数解析 ----
QUESTION=""
ROUNDS=3
MODEL="sonnet"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rounds) ROUNDS="$2"; shift 2 ;;
    --model)  MODEL="$2"; shift 2 ;;
    *)        QUESTION="$1"; shift ;;
  esac
done

if [[ -z "$QUESTION" ]]; then
  echo "用法: sql-consensus.sh \"<问题>\" [--rounds N] [--model MODEL]" >&2
  exit 2
fi

SCHEMA=$(cat <<'SCHEMA_EOF'
DatabaseType=[SQLite], Table=[Banxa转化漏斗],
PartitionTimeField=[日期 FORMAT 'yyyy-MM-dd'],
Metrics=[<用户数 ALIAS '人数;用户量' AGGREGATE 'SUM' COMMENT '该阶段的用户人数'>],
Dimensions=[
  <日期 ALIAS '数据日期' DATATYPE 'date' FORMAT 'yyyy-MM-dd' COMMENT '数据分区日期'>,
  <国家 ALIAS '地区;区域' DATATYPE 'varchar' COMMENT '用户所在国家/地区'>,
  <支付方式 ALIAS '支付渠道;付款方式' DATATYPE 'varchar' COMMENT '支付渠道'>,
  <漏斗阶段 ALIAS '环节;步骤' DATATYPE 'varchar' COMMENT '转化漏斗环节'>
],
Values=[<漏斗阶段='created','kyc_passed','pay_submitted','pay_authorized','completed'>]
SCHEMA_EOF
)

TODAY=$(date +%Y-%m-%d)
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "🗳️  Self-Consistency: 并行调 LLM $ROUNDS 次..." >&2

# ---- 并行调用 ----
for i in $(seq 1 "$ROUNDS"); do
  # 每轮用不同的 exemplar 子集（随机选 2 个）
  EXEMPLARS=$(python3 "$BRIDGE_DIR/inject-exemplars.py" "$QUESTION" --top 5 2>/dev/null | \
    python3 -c "
import sys, random
lines = sys.stdin.read().strip().split('\n')
pairs = []
current = []
for l in lines:
    if l.startswith('Q:'):
        if current:
            pairs.append('\n'.join(current))
        current = [l]
    elif current:
        current.append(l)
if current:
    pairs.append('\n'.join(current))
random.shuffle(pairs)
selected = pairs[:min(2, len(pairs))]
print('\n\n'.join(selected))
" 2>/dev/null)

  PROMPT="你是 SQL 专家。根据 Schema 把问题转成 SQL。只输出 SQL，不要解释。

Schema: $SCHEMA
CurrentDate=[$TODAY]

$EXEMPLARS

Question: $QUESTION
SQL:"

  echo "$PROMPT" | "$CLAUDE_BIN" -p --model "$MODEL" --print 2>/dev/null > "$TMPDIR/result_$i.txt" &
done

wait

# ---- 提取 SQL + 投票（用 Python，兼容 macOS bash 3.2）----
python3 - "$TMPDIR" "$ROUNDS" <<'PYEOF'
import sys, os, re
from collections import Counter

tmpdir = sys.argv[1]
rounds = int(sys.argv[2])

sqls = []
for i in range(1, rounds + 1):
    path = os.path.join(tmpdir, f"result_{i}.txt")
    if not os.path.exists(path):
        print(f"  Round {i}: (无结果)", file=sys.stderr)
        continue
    raw = open(path).read()
    # 提取 SQL（支持 WITH ... SELECT ...）
    m = re.search(r'((?:WITH\b.+?\)\s*)?SELECT\b[^;`]+)', raw, re.IGNORECASE | re.DOTALL)
    if m:
        sql = m.group(1).replace('```', '').strip()
        sqls.append(sql)
        # 截断显示
        display = sql[:100] + "..." if len(sql) > 100 else sql
        print(f"  Round {i}: {display}", file=sys.stderr)
    else:
        print(f"  Round {i}: (未提取到 SQL)", file=sys.stderr)

if not sqls:
    print("\n❌ 所有轮次都未能生成 SQL", file=sys.stderr)
    sys.exit(1)

# 归一化后投票
def normalize(s):
    s = re.sub(r'\s+', ' ', s.lower()).strip()
    # 去掉 AS 别名（不影响查询语义）
    s = re.sub(r'\bas\s+[\u4e00-\u9fffa-z_]+', '', s)
    # 去掉 ORDER BY（排序差异不影响数据正确性）
    s = re.sub(r'order\s+by\s+.+$', '', s)
    return s.strip()

normed = [normalize(s) for s in sqls]
counter = Counter(normed)
best_norm, best_count = counter.most_common(1)[0]
# 找回原始 SQL
best_sql = sqls[normed.index(best_norm)]
confidence = best_count * 100 // len(sqls)

print("", file=sys.stderr)
if best_count >= 2:
    print(f"✅ 共识 SQL（{best_count}/{len(sqls)} 票，置信度 {confidence}%）:", file=sys.stderr)
    print(best_sql)
    sys.exit(0)
else:
    print(f"⚠️  无共识（{len(sqls)} 条 SQL 都不同），返回第一个:", file=sys.stderr)
    print(sqls[0])
    sys.exit(1)
PYEOF

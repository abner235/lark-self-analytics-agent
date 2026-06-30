#!/usr/bin/env python3
"""
rule-parser.py — 高频查询模式的规则解析器

对简单、可枚举的查询模式直接生成语义 SQL，不调 LLM。
匹配成功返回 SQL（exit 0），不匹配返回空（exit 1），交由 LLM 处理。

用法:
  python3 rule-parser.py "昨天转化率"
  python3 rule-parser.py "visa 昨天漏斗"
  python3 rule-parser.py "过去7天各支付方式转化率趋势"

设计原则（来自 SuperSonic）：
  - 规则只覆盖高频简单查询，复杂查询走 LLM
  - 模式匹配宽松（同义词、顺序不敏感），宁可多匹配让 SQL 跑通，不漏匹配
  - 规则生成的 SQL 用语义名（中文），后续由 sql-mapper 映射
"""
import re
import sys
import os
from datetime import datetime, timedelta


# ---- 日期解析 ----

def parse_date_ref(text: str, today: str = None) -> dict:
    """从文本中提取日期引用，返回 {single: 'YYYY-MM-DD'} 或 {range_start, range_end} 或 {}"""
    if today:
        base = datetime.strptime(today, "%Y-%m-%d")
    else:
        base = datetime.now()

    # 单日
    if re.search(r'昨[天日]', text):
        d = (base - timedelta(days=1)).strftime("%Y-%m-%d")
        return {"single": d}
    if re.search(r'今[天日]', text):
        return {"single": base.strftime("%Y-%m-%d")}
    if re.search(r'前[天日]', text):
        d = (base - timedelta(days=2)).strftime("%Y-%m-%d")
        return {"single": d}

    # 范围: "过去N天" / "最近N天"
    m = re.search(r'(?:过去|最近|近)\s*(\d+)\s*天', text)
    if m:
        n = int(m.group(1))
        start = (base - timedelta(days=n)).strftime("%Y-%m-%d")
        end = (base - timedelta(days=1)).strftime("%Y-%m-%d")
        return {"range_start": start, "range_end": end}

    # 本周
    if re.search(r'本周|这周', text):
        start = (base - timedelta(days=base.weekday())).strftime("%Y-%m-%d")
        return {"range_start": start, "range_end": base.strftime("%Y-%m-%d")}

    # 具体日期 YYYY-MM-DD
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        return {"single": m.group(1)}

    return {}


# ---- 维度过滤提取 ----

PAYMENT_METHODS = {"visa", "mastercard", "applepay", "apple pay", "pix", "googlepay", "google pay"}
STAGES = {"created", "kyc_passed", "pay_submitted", "pay_authorized", "completed",
          "kyc", "支付提交", "支付授权", "订单创建", "交易完成"}

def extract_filters(text: str) -> dict:
    """提取维度过滤条件"""
    filters = {}
    text_lower = text.lower()

    for pm in PAYMENT_METHODS:
        if pm.replace(" ", "") in text_lower.replace(" ", ""):
            filters["支付方式"] = pm.replace(" ", "")
            break

    # 国家（简单匹配常见国家名）
    countries = {"巴西": "BR", "美国": "US", "英国": "GB", "日本": "JP", "韩国": "KR",
                 "印度": "IN", "澳大利亚": "AU", "加拿大": "CA", "德国": "DE", "法国": "FR"}
    for cn, code in countries.items():
        if cn in text:
            filters["国家"] = code
            break

    return filters


# ---- 查询模式 ----

def try_conversion_rate(text: str, date_info: dict, filters: dict) -> str:
    """模式: 转化率查询"""
    if not re.search(r'转化率|转化|conversion', text, re.IGNORECASE):
        return None

    where_parts = []
    if "single" in date_info:
        where_parts.append(f"日期='{date_info['single']}'")
    elif "range_start" in date_info:
        where_parts.append(f"日期>='{date_info['range_start']}' AND 日期<='{date_info['range_end']}'")

    for dim, val in filters.items():
        where_parts.append(f"{dim}='{val}'")

    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # 判断是否需要按维度分组
    group_dims = []
    if re.search(r'各\s*支付|按.*支付|支付.*[分拆]', text):
        group_dims.append("支付方式")
    if re.search(r'各\s*国|按.*国|国家.*[分拆]', text):
        group_dims.append("国家")
    if "range_start" in date_info or re.search(r'趋势|走势|变化', text):
        group_dims.insert(0, "日期")

    if not group_dims:
        group_dims = ["日期"]

    group_by = ", ".join(group_dims)
    select_dims = ", ".join(group_dims)

    return (
        f"SELECT {select_dims}, "
        f"CAST(SUM(CASE WHEN 漏斗阶段='completed' THEN 用户数 ELSE 0 END) AS FLOAT) / "
        f"SUM(CASE WHEN 漏斗阶段='created' THEN 用户数 ELSE 0 END) AS 转化率 "
        f"FROM Banxa转化漏斗{where} "
        f"GROUP BY {group_by} ORDER BY {group_dims[0]}"
    )


def try_funnel_breakdown(text: str, date_info: dict, filters: dict) -> str:
    """模式: 漏斗各阶段明细"""
    if not re.search(r'漏斗|各阶段|各环节|各步骤|哪一步|哪个阶段', text):
        return None

    where_parts = []
    if "single" in date_info:
        where_parts.append(f"日期='{date_info['single']}'")
    elif "range_start" in date_info:
        where_parts.append(f"日期>='{date_info['range_start']}' AND 日期<='{date_info['range_end']}'")

    for dim, val in filters.items():
        where_parts.append(f"{dim}='{val}'")

    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    return (
        f"SELECT 漏斗阶段, SUM(用户数) AS 用户数 "
        f"FROM Banxa转化漏斗{where} "
        f"GROUP BY 漏斗阶段"
    )


def try_dimension_breakdown(text: str, date_info: dict, filters: dict) -> str:
    """模式: 按维度拆解（如"各国家完成人数"、"各支付方式用户数"）"""
    dim = None
    if re.search(r'各\s*国|按.*国|国家', text):
        dim = "国家"
    elif re.search(r'各.*支付|按.*支付', text):
        dim = "支付方式"
    else:
        return None

    # 确定指标过滤
    stage_filter = None
    if re.search(r'完成|completed|成功', text):
        stage_filter = "completed"
    elif re.search(r'创建|created|新增', text):
        stage_filter = "created"
    elif re.search(r'kyc|实名', text):
        stage_filter = "kyc_passed"

    where_parts = []
    if "single" in date_info:
        where_parts.append(f"日期='{date_info['single']}'")
    elif "range_start" in date_info:
        where_parts.append(f"日期>='{date_info['range_start']}' AND 日期<='{date_info['range_end']}'")

    if stage_filter:
        where_parts.append(f"漏斗阶段='{stage_filter}'")

    for d, val in filters.items():
        if d != dim:
            where_parts.append(f"{d}='{val}'")

    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    return (
        f"SELECT {dim}, SUM(用户数) AS 用户数 "
        f"FROM Banxa转化漏斗{where} "
        f"GROUP BY {dim} ORDER BY 用户数 DESC"
    )


# ---- 主流程 ----

PATTERNS = [
    try_conversion_rate,
    try_funnel_breakdown,
    try_dimension_breakdown,
]


def needs_llm(question: str) -> bool:
    """判断问题是否需要多步分析（归因、对比、趋势解释），应交给 LLM。"""
    return bool(re.search(
        r'为什么|原因|归因|怎么回事|出了什么问题|分析.*原因|对比.*分析|环比|同比|异常|下[跌降].*原因|暴[涨跌]',
        question
    ))


def parse(question: str, today: str = None) -> str:
    """尝试用规则解析问题，返回 SQL 或 None。"""
    if needs_llm(question):
        return None

    date_info = parse_date_ref(question, today)
    filters = extract_filters(question)

    for pattern_fn in PATTERNS:
        sql = pattern_fn(question, date_info, filters)
        if sql:
            return sql

    return None


def main():
    today = os.environ.get("TODAY", None)

    if len(sys.argv) < 2:
        print("用法: rule-parser.py \"<问题>\"", file=sys.stderr)
        sys.exit(2)

    question = " ".join(sys.argv[1:])
    sql = parse(question, today)

    if sql:
        print(sql)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

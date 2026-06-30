#!/usr/bin/env python3
"""
eval.py — bi-bridge Agent 评估运行器

对 questions.yaml 中定义的问题逐一调用 Agent，捕获生成的 SQL、tool calls、
耗时、成功/失败，用 sql-validator.py 校验 SQL，输出 JSON 评估报告。

用法:
  python3 eval.py                           # 跑全部问题
  python3 eval.py --max 3                   # 只跑前 3 题
  python3 eval.py -q q1_simple_funnel       # 只跑指定题
  python3 eval.py --list                    # 列出所有测试问题
  python3 eval.py --dry-run                 # 只显示会生成的 prompt，不执行
  python3 eval.py --output results.json     # 指定输出文件

依赖: PyYAML（已在项目中可用）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(EVAL_DIR)

# ---------- 配置默认值 ----------

QUESTIONS_FILE = os.path.join(EVAL_DIR, "questions.yaml")
RUNBOOK_ROUTER = os.path.join(BRIDGE_DIR, "runbook-router.py")
SQL_VALIDATOR = os.path.join(BRIDGE_DIR, "sql-validator.py")
BASE_PROMPT = os.path.join(BRIDGE_DIR, "base-prompt.md")
SCHEMA_DIR = os.path.join(BRIDGE_DIR, "schema")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MAX_TURNS = int(os.environ.get("EVAL_MAX_TURNS", "20"))
CLAUDE_ALLOWED_TOOLS = os.environ.get(
    "EVAL_ALLOWED_TOOLS", "Skill Read Grep Glob"
)
TASK_TIMEOUT_SECS = int(os.environ.get("EVAL_TIMEOUT", "300"))

SGT = timezone(timedelta(hours=8))


# ---------- 加载问题 ----------

def load_questions(filepath):
    """加载 questions.yaml，返回问题列表。"""
    try:
        import yaml
    except ImportError:
        print("错误: 需要 PyYAML。运行 pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data.get("questions", [])


# ---------- 组装 prompt ----------

def assemble_prompt(question_text):
    """调用 runbook-router.py 组装完整 prompt。

    返回组装好的 system prompt 文本。runbook-router 会根据问题匹配 runbook，
    加载对应的 schema、exemplars、分析框架、规则。
    """
    try:
        result = subprocess.run(
            ["python3", RUNBOOK_ROUTER, question_text, "--base", BASE_PROMPT],
            capture_output=True, text=True, cwd=BRIDGE_DIR, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"  警告: runbook-router 调用失败: {e}", file=sys.stderr)

    # fallback: 只用 base prompt
    if os.path.exists(BASE_PROMPT):
        with open(BASE_PROMPT, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


# ---------- 提取 SQL ----------

def extract_sql(text):
    """从 Agent 输出中提取 SQL。

    优先从 <!-- BI-BRIDGE-SQL --> 标记中提取，
    fallback 到 ```sql 代码块。
    """
    # 方式 1: BI-BRIDGE-SQL 标记
    # 格式见 prompts/sql-footer.md:
    #   <!-- BI-BRIDGE-SQL
    #   ```sql
    #   ...
    #   ```
    #   BI-BRIDGE-SQL -->
    m = re.search(
        r'<!--\s*BI-BRIDGE-SQL\s*\n(.*?)\nBI-BRIDGE-SQL\s*-->',
        text, re.DOTALL
    )
    if m:
        return m.group(1).strip()

    # 方式 2: ```sql 代码块（取所有，返回列表拼接）
    blocks = re.findall(r'```sql\s*\n(.*?)\n```', text, re.DOTALL | re.IGNORECASE)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks)

    # 方式 3: 行内 SELECT 语句
    m = re.search(r'(SELECT\s.+?)(?:\n\n|\Z)', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return ""


def extract_tables_from_sql(sql):
    """从 SQL 中提取引用的表名。"""
    if not sql:
        return []
    tables = re.findall(
        r'(?:FROM|JOIN)\s+([\u4e00-\u9fffa-zA-Z_]\S*)',
        sql, re.IGNORECASE
    )
    # 去掉 CTE 名
    cte_names = set(re.findall(
        r'WITH\s+([\u4e00-\u9fffa-zA-Z_]\S+)\s+AS',
        sql, re.IGNORECASE
    ))
    return [t for t in tables if t not in cte_names]


# ---------- 校验 SQL ----------

def validate_sql(sql):
    """调用 sql-validator.py 校验 SQL。

    返回 (is_valid, detail_text)。
    """
    if not sql:
        return False, "未提取到 SQL"

    try:
        result = subprocess.run(
            ["python3", SQL_VALIDATOR, sql],
            capture_output=True, text=True, cwd=BRIDGE_DIR, timeout=15
        )
        detail = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        return result.returncode == 0, detail
    except Exception as e:
        return False, f"校验器调用失败: {e}"


# ---------- 解析 claude JSON 输出 ----------

def parse_claude_json_output(raw_output):
    """解析 claude --output-format json 的输出。

    claude JSON 输出结构:
    {
      "type": "result",
      "subtype": "success",
      "result": "...",          # Agent 最终文本输出
      "cost_usd": 0.123,
      "duration_ms": 45000,
      "duration_api_ms": 30000,
      "num_turns": 5,
      ...
    }
    """
    info = {
        "result_text": "",
        "cost_usd": 0,
        "num_turns": 0,
        "is_error": False,
    }

    if not raw_output.strip():
        info["is_error"] = True
        return info

    try:
        data = json.loads(raw_output)
        info["result_text"] = data.get("result", "")
        info["cost_usd"] = data.get("cost_usd", 0)
        info["num_turns"] = data.get("num_turns", 0)
        info["is_error"] = data.get("subtype") != "success"
        return info
    except json.JSONDecodeError:
        # 可能是纯文本输出（没用 --output-format json）
        info["result_text"] = raw_output
        return info


# ---------- 运行单题 ----------

def run_question(q, dry_run=False):
    """运行单个问题，返回评估结果 dict。"""
    qid = q["id"]
    question = q["question"]
    difficulty = q.get("difficulty", "unknown")
    features = q.get("features", [])
    expected_tables = q.get("expected_tables", [])

    print(f"\n{'='*60}")
    print(f"[{qid}] ({difficulty}) {question}")
    print(f"{'='*60}")

    # 组装 prompt
    sys_prompt = assemble_prompt(question)

    if dry_run:
        print(f"\n--- 系统 prompt（前 500 字）---")
        print(sys_prompt[:500])
        if len(sys_prompt) > 500:
            print(f"... (共 {len(sys_prompt)} 字)")
        print(f"\n--- 用户问题 ---")
        print(question)
        return {
            "id": qid,
            "question": question,
            "difficulty": difficulty,
            "status": "dry_run",
            "prompt_length": len(sys_prompt),
            "features_tested": features,
        }

    # 调用 claude
    start = time.time()
    cmd = [
        CLAUDE_BIN,
        "-p", question,
        "--append-system-prompt", sys_prompt,
        "--allowedTools", CLAUDE_ALLOWED_TOOLS,
        "--max-turns", str(CLAUDE_MAX_TURNS),
        "--output-format", "json",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=BRIDGE_DIR,
            timeout=TASK_TIMEOUT_SECS,
        )
        duration = time.time() - start
        raw_out = proc.stdout
        raw_err = proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        print(f"  超时（>{TASK_TIMEOUT_SECS}s）")
        return {
            "id": qid,
            "question": question,
            "difficulty": difficulty,
            "status": "timeout",
            "duration_secs": round(duration, 1),
            "features_tested": features,
        }
    except Exception as e:
        duration = time.time() - start
        print(f"  执行异常: {e}")
        return {
            "id": qid,
            "question": question,
            "difficulty": difficulty,
            "status": "error",
            "duration_secs": round(duration, 1),
            "error": str(e),
            "features_tested": features,
        }

    # 解析输出
    parsed = parse_claude_json_output(raw_out)
    result_text = parsed["result_text"]

    if rc != 0 or parsed["is_error"] or not result_text:
        err_tail = (raw_err or "")[-300:]
        print(f"  失败 (rc={rc})")
        if err_tail:
            print(f"  stderr: {err_tail[:150]}")
        return {
            "id": qid,
            "question": question,
            "difficulty": difficulty,
            "status": "fail",
            "duration_secs": round(duration, 1),
            "return_code": rc,
            "error": err_tail,
            "cost_usd": parsed["cost_usd"],
            "num_turns": parsed["num_turns"],
            "features_tested": features,
        }

    # 提取 SQL
    sql = extract_sql(result_text)
    tables_used = extract_tables_from_sql(sql)

    # 校验 SQL
    sql_valid, sql_detail = validate_sql(sql)

    # 判断是否有结论
    has_conclusion = bool(re.search(r'\*\*结论\*\*', result_text))

    # 综合判定
    # pass 条件: 有输出 + 有结论性回答 + （有 SQL 且校验通过 || 是边界题无需 SQL）
    is_boundary = "boundary_case" in features or "graceful_failure" in features
    if is_boundary:
        # 边界题：只要 Agent 明确说了无法计算/不在 schema 就算 pass
        no_field_mentioned = bool(re.search(
            r'(不在|没有|无法|不支持|不包含|schema.*没)', result_text
        ))
        status = "pass" if no_field_mentioned else "fail"
    else:
        if sql and sql_valid and has_conclusion:
            status = "pass"
        elif sql and not sql_valid:
            status = "fail"
        elif not sql and has_conclusion:
            # 有结论但没提取到 SQL（可能 Agent 用了其他格式）
            status = "warn"
        else:
            status = "fail"

    status_icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[status]
    print(f"  [{status_icon}] {round(duration, 1)}s | SQL合法={sql_valid} | 有结论={has_conclusion}")
    if sql:
        # 显示 SQL 的前 120 字符
        sql_preview = sql.replace("\n", " ")[:120]
        print(f"  SQL: {sql_preview}")
    if sql_detail and not sql_valid:
        print(f"  校验: {sql_detail[:150]}")

    return {
        "id": qid,
        "question": question,
        "difficulty": difficulty,
        "status": status,
        "duration_secs": round(duration, 1),
        "sql_valid": sql_valid,
        "sql_validation_detail": sql_detail if not sql_valid else "",
        "sql": sql,
        "tables_used": list(set(tables_used)),
        "expected_tables": expected_tables,
        "has_conclusion": has_conclusion,
        "features_tested": features,
        "cost_usd": parsed["cost_usd"],
        "num_turns": parsed["num_turns"],
        "result_preview": result_text[:300],
    }


# ---------- 汇总 ----------

def summarize(results):
    """打印汇总统计。"""
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    warned = sum(1 for r in results if r["status"] == "warn")
    other = total - passed - failed - warned

    total_duration = sum(r.get("duration_secs", 0) for r in results)
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"评估汇总")
    print(f"{'='*60}")
    print(f"  总题数:   {total}")
    print(f"  PASS:     {passed}")
    print(f"  FAIL:     {failed}")
    if warned:
        print(f"  WARN:     {warned}")
    if other:
        print(f"  OTHER:    {other}")
    print(f"  通过率:   {passed}/{total} ({100*passed/total:.0f}%)" if total else "")
    print(f"  总耗时:   {total_duration:.0f}s")
    if total_cost:
        print(f"  总成本:   ${total_cost:.4f}")
    print()

    # 按难度分组
    by_difficulty = {}
    for r in results:
        d = r.get("difficulty", "unknown")
        by_difficulty.setdefault(d, []).append(r)

    for diff in ["easy", "medium", "hard"]:
        group = by_difficulty.get(diff, [])
        if not group:
            continue
        g_pass = sum(1 for r in group if r["status"] == "pass")
        print(f"  [{diff}] {g_pass}/{len(group)} 通过")

    # 列出失败的题目
    fails = [r for r in results if r["status"] == "fail"]
    if fails:
        print(f"\n失败题目:")
        for r in fails:
            reason = r.get("sql_validation_detail") or r.get("error") or "无结论"
            print(f"  - {r['id']}: {reason[:100]}")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(
        description="bi-bridge Agent 评估运行器"
    )
    parser.add_argument(
        "-q", "--question-id",
        help="只跑指定 id 的题目（可多次指定）",
        action="append", default=[]
    )
    parser.add_argument(
        "--max", type=int, default=0,
        help="最多跑 N 题（0=全部）"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="列出所有测试问题"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只显示会生成的 prompt，不实际调用 Agent"
    )
    parser.add_argument(
        "--output", "-o", default="",
        help="输出 JSON 文件路径（默认 eval/results_<timestamp>.json）"
    )
    parser.add_argument(
        "--questions-file", default=QUESTIONS_FILE,
        help="问题集文件路径"
    )

    args = parser.parse_args()

    # 加载问题
    questions = load_questions(args.questions_file)
    if not questions:
        print("错误: 未加载到问题", file=sys.stderr)
        sys.exit(2)

    # --list: 列出问题
    if args.list:
        print(f"共 {len(questions)} 题:\n")
        for q in questions:
            features = ", ".join(q.get("features", []))
            tables = ", ".join(q.get("expected_tables", []))
            print(f"  {q['id']:30s} [{q.get('difficulty', '?'):6s}] {q['question']}")
            print(f"  {'':30s}  特征: {features}")
            if tables:
                print(f"  {'':30s}  涉及: {tables}")
        return

    # 筛选题目
    if args.question_id:
        id_set = set(args.question_id)
        questions = [q for q in questions if q["id"] in id_set]
        missing = id_set - {q["id"] for q in questions}
        if missing:
            print(f"警告: 以下 id 未找到: {missing}", file=sys.stderr)

    if args.max > 0:
        questions = questions[:args.max]

    print(f"bi-bridge eval: 将运行 {len(questions)} 题")
    if args.dry_run:
        print("(dry-run 模式，不实际调用 Agent)\n")

    # 逐题执行
    results = []
    for q in questions:
        r = run_question(q, dry_run=args.dry_run)
        results.append(r)

    # 汇总
    if not args.dry_run:
        summarize(results)

    # 输出 JSON
    now = datetime.now(SGT)
    report = {
        "timestamp": now.isoformat(),
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "results": results,
    }

    if args.dry_run:
        # dry-run 不写文件
        return

    out_path = args.output
    if not out_path:
        ts = now.strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(EVAL_DIR, f"results_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n结果已写入: {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
runbook-router.py — 根据用户问题选择 runbook，组装完整 prompt。

解决的问题：prompt 随业务表增多而膨胀，每次查询把所有 schema + exemplars
塞进 context，大部分是无关信息。runbook 路由让 prompt 只包含当前问题需要的部分。

架构（v2 — 模块化 prompt）：
  prompts/ 目录下的模块文件按顺序加载（角色/环境/工作流/高频错误/SQL规则/输出格式/风格/SQL附录/错误处理）
  + runbook 选中的 schema + exemplars + 分析框架 + 规则 (按需加载)

  向后兼容：如果 prompts/ 目录不存在，回退到 base-prompt.md。

用法:
  python3 runbook-router.py "昨天 visa 转化率"          # 输出组装好的完整 prompt
  python3 runbook-router.py "昨天各支付方式交易金额"      # 匹配订单 runbook
  python3 runbook-router.py "转化率和金额同时下跌"       # 匹配跨域 runbook
  python3 runbook-router.py --list                      # 列出所有 runbook
  python3 runbook-router.py --debug "问题"              # 显示匹配过程
"""
import os
import re
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# prompt 模块加载顺序（文件名，不含 .md 后缀）
PROMPT_MODULE_ORDER = [
    "role",
    "environment",
    "workflow",
    "sql-rules",
    "output-format",
    "style",
    "sql-footer",
    "error-handling",
]


def load_prompt_modules(prompts_dir=None):
    """按固定顺序加载 prompts/ 目录下的模块文件，拼接成 base prompt 文本。

    如果 prompts_dir 不存在，返回 None（调用方回退到 base-prompt.md）。
    """
    if prompts_dir is None:
        prompts_dir = os.path.join(SCRIPT_DIR, "prompts")

    if not os.path.isdir(prompts_dir):
        return None

    parts = []
    for name in PROMPT_MODULE_ORDER:
        filepath = os.path.join(prompts_dir, f"{name}.md")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(content)

    return "\n\n".join(parts) if parts else None


def parse_runbook_yaml(filepath):
    """解析 runbook YAML（简易解析器，无外部依赖）。"""
    result = {"name": "", "description": "", "tables": [], "exemplars": [],
              "triggers": [], "analysis": "", "rules": ""}

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 单行字段
    for key in ("name", "description"):
        m = re.search(rf'^{key}:\s*(.+)$', content, re.MULTILINE)
        if m:
            result[key] = m.group(1).strip()

    # 列表字段
    for key in ("tables", "exemplars", "triggers"):
        m = re.search(rf'^{key}:\s*\[(.+)\]', content, re.MULTILINE)
        if m:
            result[key] = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]

    # 多行文本字段（YAML | 块）
    for key in ("analysis", "rules"):
        m = re.search(rf'^{key}:\s*\|\s*\n((?:[ \t]+.+\n?)+)', content, re.MULTILINE)
        if m:
            lines = m.group(1).split("\n")
            # 去掉公共缩进
            min_indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
            result[key] = "\n".join(l[min_indent:] for l in lines).strip()

    return result


def load_runbooks(runbook_dir):
    """加载所有 runbook。"""
    runbooks = []
    p = Path(runbook_dir)
    if not p.exists():
        return runbooks
    for f in sorted(p.glob("*.yaml")):
        rb = parse_runbook_yaml(str(f))
        if rb["name"]:
            rb["_path"] = str(f)
            runbooks.append(rb)
    return runbooks


def match_runbooks(question, runbooks):
    """根据问题匹配 runbook，返回按相关度排序的列表。"""
    scored = []

    for rb in runbooks:
        score = 0
        matched_triggers = []

        for trigger in rb["triggers"]:
            # 支持正则 trigger（如 "转化率.*金额"）
            try:
                if re.search(trigger, question, re.IGNORECASE):
                    score += 2
                    matched_triggers.append(trigger)
            except re.error:
                # 非正则 trigger，用子串匹配
                if trigger.lower() in question.lower():
                    score += 2
                    matched_triggers.append(trigger)

        if score > 0:
            scored.append((score, matched_triggers, rb))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def generate_schema_section(tables):
    """调用 schema-prompt.py 生成 schema 文本。"""
    schema_prompt = os.path.join(SCRIPT_DIR, "schema-prompt.py")
    parts = []
    for table in tables:
        try:
            result = subprocess.run(
                ["python3", schema_prompt, "--table", table],
                capture_output=True, text=True, cwd=SCRIPT_DIR
            )
            if result.returncode == 0 and result.stdout.strip():
                parts.append(result.stdout.strip())
        except Exception:
            pass

    # 如果多表，追加 JOIN 关系
    if len(tables) > 1:
        try:
            result = subprocess.run(
                ["python3", schema_prompt, "--with-joins"],
                capture_output=True, text=True, cwd=SCRIPT_DIR
            )
            if result.returncode == 0:
                # 只取 ## 表间关系 部分
                join_section = re.search(r'(## 表间关系.+)', result.stdout, re.DOTALL)
                if join_section:
                    parts.append(join_section.group(1).strip())
        except Exception:
            pass

    return "\n\n".join(parts)


def generate_exemplars_section(exemplar_names, question):
    """调用 inject-exemplars.py 生成 few-shot 文本。"""
    inject = os.path.join(SCRIPT_DIR, "inject-exemplars.py")
    all_exemplars = []

    for name in exemplar_names:
        try:
            result = subprocess.run(
                ["python3", inject, question, "--top", "3", "--table", name],
                capture_output=True, text=True, cwd=SCRIPT_DIR
            )
            if result.returncode == 0 and result.stdout.strip():
                all_exemplars.append(result.stdout.strip())
        except Exception:
            pass

    return "\n\n".join(all_exemplars)


def assemble_prompt(question, matched, base_prompt_path=None, prompts_dir=None):
    """组装完整 prompt：prompt 模块 + schema + exemplars + analysis + rules。

    优先从 prompts/ 目录加载模块化 prompt。如果 prompts/ 不存在，回退到 base-prompt.md。
    """
    # 优先尝试模块化 prompt
    base = load_prompt_modules(prompts_dir)

    # 回退到 base-prompt.md
    if base is None:
        if base_prompt_path and os.path.exists(base_prompt_path):
            with open(base_prompt_path, "r", encoding="utf-8") as f:
                base = f.read().strip()
        else:
            base = ""

    # 收集所有匹配 runbook 的 tables / exemplars（去重）
    all_tables = []
    all_exemplars = []
    analysis_sections = []
    rules_sections = []
    seen_tables = set()
    seen_exemplars = set()

    for _, _, rb in matched:
        for t in rb["tables"]:
            if t not in seen_tables:
                all_tables.append(t)
                seen_tables.add(t)
        for e in rb["exemplars"]:
            if e not in seen_exemplars:
                all_exemplars.append(e)
                seen_exemplars.add(e)
        if rb["analysis"]:
            analysis_sections.append(rb["analysis"])
        if rb["rules"]:
            rules_sections.append(rb["rules"])

    # 生成各部分
    schema_text = generate_schema_section(all_tables)
    exemplar_text = generate_exemplars_section(all_exemplars, question)

    # 组装
    parts = []
    if base:
        parts.append(base)

    if schema_text:
        parts.append(f"# 数据表 Schema\n\nDatabaseType=[SQLite]\n\n{schema_text}")
        parts.append(
            "**重要：你写 SQL 时使用上面的中文字段名，系统会自动映射成物理列名。**"
            " 漏斗阶段的值在 SQL 中仍用英文（如 `WHERE 漏斗阶段='created'`）。"
        )

    if analysis_sections:
        parts.append("\n\n".join(analysis_sections))

    if exemplar_text:
        parts.append(f"# 参考样例\n\n{exemplar_text}")

    if rules_sections:
        combined_rules = "\n".join(rules_sections)
        parts.append(f"# 场景专属规则\n\n{combined_rules}")

    return "\n\n".join(parts)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Route questions to runbooks and assemble prompts")
    parser.add_argument("question", nargs="?", default="", help="User question")
    parser.add_argument("--list", action="store_true", help="List all available runbooks")
    parser.add_argument("--debug", action="store_true", help="Show matching details")
    parser.add_argument("--base", default=None, help="Path to base prompt file")
    parser.add_argument("--dir", default=None, help="Runbooks directory")
    args = parser.parse_args()

    runbook_dir = args.dir or os.path.join(SCRIPT_DIR, "runbooks")
    runbooks = load_runbooks(runbook_dir)

    if args.list:
        for rb in runbooks:
            tables = ", ".join(rb["tables"])
            print(f"  {rb['name']:20s} — {rb['description']}  [{tables}]")
        return

    if not args.question:
        print("用法: runbook-router.py \"<问题>\"", file=sys.stderr)
        print("      runbook-router.py --list", file=sys.stderr)
        sys.exit(2)

    matched = match_runbooks(args.question, runbooks)

    if args.debug:
        print(f"问题: {args.question}", file=sys.stderr)
        if matched:
            for score, triggers, rb in matched:
                print(f"  ✓ {rb['name']} (score={score}, triggers={triggers})", file=sys.stderr)
        else:
            print("  ✗ 无匹配，加载全部 schema", file=sys.stderr)
        print("", file=sys.stderr)

    if not matched:
        # fallback：全部加载
        matched = [(0, [], rb) for rb in runbooks]
        if args.debug:
            print("  → fallback: 加载全部 runbook", file=sys.stderr)

    base_path = args.base
    if not base_path:
        # 默认 base prompt（仅在 prompts/ 目录不存在时使用）
        candidate = os.path.join(SCRIPT_DIR, "base-prompt.md")
        if os.path.exists(candidate):
            base_path = candidate

    prompts_dir = os.path.join(SCRIPT_DIR, "prompts")
    prompt = assemble_prompt(args.question, matched, base_path, prompts_dir)
    print(prompt)


if __name__ == "__main__":
    main()

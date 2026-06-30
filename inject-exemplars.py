#!/usr/bin/env python3
"""
inject-exemplars.py — 从 exemplars/*.json 中选取相关样例，输出格式化的 few-shot 片段。

两种模式：
  1. 关键词匹配（默认，零依赖）：按词重叠度选 top-N
  2. Embedding 匹配（--embedding，需 API）：按语义相似度选 top-N（TODO）

用法:
  python3 inject-exemplars.py "昨天 visa 转化率" --top 3
  python3 inject-exemplars.py "各支付方式趋势" --top 2 --table banxa_funnel
"""
import json
import os
import re
import sys
import argparse
from pathlib import Path


def tokenize(text: str) -> set:
    """简易中文分词：按标点和空格切分，去掉单字（噪声太大）。"""
    tokens = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z_]+', text.lower())
    return set(tokens)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_exemplars(exemplar_dir: str, table: str = None) -> list:
    """加载 exemplars 目录下的 JSON 文件。可按 table 名过滤。"""
    results = []
    p = Path(exemplar_dir)
    if not p.exists():
        return results

    for f in p.glob("*.json"):
        if table and f.stem != table:
            continue
        with open(f, "r", encoding="utf-8") as fh:
            items = json.load(fh)
            for item in items:
                if item.get("status") == "verified":
                    item["_source"] = f.stem
                    results.append(item)
    return results


def select_exemplars(question: str, exemplars: list, top_n: int = 3) -> list:
    """按关键词重叠度选出 top-N 样例。"""
    q_tokens = tokenize(question)
    scored = []
    for ex in exemplars:
        ex_tokens = tokenize(ex["question"])
        score = jaccard(q_tokens, ex_tokens)
        scored.append((score, ex))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ex for _, ex in scored[:top_n]]


def format_exemplars(exemplars: list) -> str:
    """格式化为 prompt 可用的 few-shot 片段。"""
    if not exemplars:
        return ""

    lines = ["#Exemplars:"]
    for ex in exemplars:
        lines.append(f"Q: {ex['question']}")
        lines.append(f"SQL: {ex['sql']}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Select and format exemplars for few-shot prompting")
    parser.add_argument("question", help="User question to match against")
    parser.add_argument("--top", type=int, default=3, help="Number of exemplars to return (default: 3)")
    parser.add_argument("--table", default=None, help="Filter by table name (e.g. banxa_funnel)")
    parser.add_argument("--dir", default=None, help="Exemplars directory (default: script_dir/exemplars/)")
    args = parser.parse_args()

    if args.dir:
        exemplar_dir = args.dir
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        exemplar_dir = os.path.join(script_dir, "exemplars")

    exemplars = load_exemplars(exemplar_dir, args.table)
    if not exemplars:
        print("# 无可用样例", file=sys.stderr)
        sys.exit(0)

    selected = select_exemplars(args.question, exemplars, args.top)
    print(format_exemplars(selected))


if __name__ == "__main__":
    main()

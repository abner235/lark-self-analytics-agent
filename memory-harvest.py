#!/usr/bin/env python3
"""
memory-harvest.py — 从成功报告中提取 question→SQL 对，自动充实 exemplars。

bi-bridge 每次成功分析后，会把报告存入 $STATE_DIR/memory/ 目录：
  - {date}_{mid}.md       — 报告正文（含 <!-- BI-BRIDGE-SQL --> 标记包裹的 SQL）
  - {date}_{mid}.meta.json — 元数据（question, timestamp, message_id）

本脚本扫描 memory/ 目录，提取 SQL，用 sql-validator 校验，校验通过的
自动追加到 exemplars/*.json（status="auto"），并把已处理的文件移入 processed/ 子目录。

用法:
  python3 memory-harvest.py /path/to/state_dir
  python3 memory-harvest.py /path/to/state_dir --dry-run    # 只预览，不写入
  python3 memory-harvest.py /path/to/state_dir --promote     # 把 auto 提升为 verified（人工确认后）

流程:
  1. 扫描 memory/*.meta.json（跳过 processed/ 里已处理的）
  2. 读对应的 .md 文件，提取 <!-- BI-BRIDGE-SQL --> 标记内的 SQL 代码块
  3. 用 sql-validator.py 校验每条 SQL
  4. 校验通过 → 追加到 exemplars/{table}.json，status="auto"
  5. 移入 memory/processed/
"""
import json
import os
import re
import shutil
import sys
import argparse
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


def extract_sqls(report: str) -> list:
    """从报告中提取 <!-- BI-BRIDGE-SQL --> 标记内的 SQL 代码块。"""
    pattern = r'<!--\s*BI-BRIDGE-SQL\s*\n(.*?)\nBI-BRIDGE-SQL\s*-->'
    match = re.search(pattern, report, re.DOTALL)
    if not match:
        return []

    block = match.group(1)
    # 提取所有 ```sql ... ``` 代码块
    sqls = re.findall(r'```sql\s*\n(.*?)\n```', block, re.DOTALL)
    return [sql.strip() for sql in sqls if sql.strip()]


def guess_table(sql: str) -> str:
    """从 SQL 中猜测主表名（FROM 后的第一个表）。"""
    # 匹配 FROM 后的表名（中文或英文）
    m = re.search(r'\bFROM\s+([\u4e00-\u9fffa-zA-Z_]\S*)', sql, re.IGNORECASE)
    if m:
        table = m.group(1)
        # 如果是 CTE 引用，继续找下一个 FROM
        cte_names = set(re.findall(r'WITH\s+([\u4e00-\u9fffa-zA-Z_]\S+)\s+AS', sql, re.IGNORECASE))
        if table in cte_names:
            # 找 CTE 内部的 FROM
            for m2 in re.finditer(r'\bFROM\s+([\u4e00-\u9fffa-zA-Z_]\S*)', sql, re.IGNORECASE):
                if m2.group(1) not in cte_names:
                    return m2.group(1)
        return table
    return ""


# 语义表名 → exemplar 文件名映射
_TABLE_TO_FILE = {
    "Banxa转化漏斗": "banxa_funnel",
    "banxa_funnel": "banxa_funnel",
    "Banxa订单": "banxa_orders",
    "banxa_orders": "banxa_orders",
}


def validate_sql(sql: str, schema_dir: str) -> dict:
    """调用 sql-validator 的 validate 函数。"""
    try:
        from importlib import import_module
        # 动态导入，避免循环
        spec = import_module("sql-validator".replace("-", "_") if False else "importlib")
        # 直接 import
        validator_path = os.path.join(SCRIPT_DIR, "sql-validator.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("sql_validator", validator_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.validate(sql, schema_dir)
    except Exception as e:
        return {"valid": False, "issues": [{"message": f"校验异常: {e}"}], "warnings": []}


def load_exemplars(filepath: str) -> list:
    """加载 exemplars JSON 文件。"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def is_duplicate(question: str, sql: str, exemplars: list) -> bool:
    """检查是否已有相同或高度相似的 exemplar。"""
    # 精确匹配：相同问题或相同 SQL
    for ex in exemplars:
        if ex["question"].strip() == question.strip():
            return True
        # SQL 去空白后比较
        if re.sub(r'\s+', ' ', ex["sql"]).strip() == re.sub(r'\s+', ' ', sql).strip():
            return True
    return False


def save_exemplars(filepath: str, exemplars: list):
    """保存 exemplars JSON 文件。"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(exemplars, f, ensure_ascii=False, indent=2)
    print(f"  写入 {filepath}（共 {len(exemplars)} 条）")


def harvest(state_dir: str, dry_run: bool = False) -> dict:
    """主流程：扫描 → 提取 → 校验 → 入库。"""
    memory_dir = os.path.join(state_dir, "memory")
    processed_dir = os.path.join(memory_dir, "processed")
    exemplar_dir = os.path.join(SCRIPT_DIR, "exemplars")
    schema_dir = os.path.join(SCRIPT_DIR, "schema")

    if not os.path.exists(memory_dir):
        print("memory 目录不存在，无待处理报告。")
        return {"scanned": 0, "extracted": 0, "added": 0, "skipped": 0}

    if not dry_run:
        os.makedirs(processed_dir, exist_ok=True)

    stats = {"scanned": 0, "extracted": 0, "added": 0, "skipped": 0, "invalid": 0, "duplicate": 0}

    # 扫描 meta.json 文件
    meta_files = sorted(Path(memory_dir).glob("*.meta.json"))
    if not meta_files:
        print("无待处理报告。")
        return stats

    for meta_path in meta_files:
        stats["scanned"] += 1
        stem = meta_path.stem.replace(".meta", "")
        report_path = meta_path.parent / f"{stem}.md"

        if not report_path.exists():
            print(f"  跳过 {stem}：缺少报告文件")
            stats["skipped"] += 1
            continue

        # 读取元数据和报告
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(report_path, "r", encoding="utf-8") as f:
            report = f.read()

        question = meta.get("question", "").strip()
        if not question:
            print(f"  跳过 {stem}：无 question")
            stats["skipped"] += 1
            continue

        # 提取 SQL
        sqls = extract_sqls(report)
        if not sqls:
            print(f"  跳过 {stem}：报告中无 BI-BRIDGE-SQL 标记")
            stats["skipped"] += 1
            # 仍然移入 processed（不要反复扫描）
            if not dry_run:
                shutil.move(str(meta_path), os.path.join(processed_dir, meta_path.name))
                shutil.move(str(report_path), os.path.join(processed_dir, report_path.name))
            continue

        # 对每条 SQL：校验 → 入库
        for sql in sqls:
            stats["extracted"] += 1
            print(f"\n  [{stem}] Q: {question[:60]}")
            print(f"  SQL: {sql[:80]}...")

            # 校验
            result = validate_sql(sql, schema_dir)
            if not result["valid"]:
                print(f"  ✗ 校验未通过：{result['issues'][0]['message']}")
                stats["invalid"] += 1
                continue

            # 确定目标 exemplar 文件
            table = guess_table(sql)
            file_key = _TABLE_TO_FILE.get(table, "")
            if not file_key:
                # 多表查询：放入涉及的第一个已知表
                for t, fk in _TABLE_TO_FILE.items():
                    if t in sql:
                        file_key = fk
                        break
            if not file_key:
                print(f"  ✗ 无法确定目标表：{table}")
                stats["skipped"] += 1
                continue

            exemplar_file = os.path.join(exemplar_dir, f"{file_key}.json")
            existing = load_exemplars(exemplar_file)

            # 去重
            if is_duplicate(question, sql, existing):
                print(f"  → 已存在，跳过")
                stats["duplicate"] += 1
                continue

            # 追加
            new_entry = {
                "question": question,
                "sql": sql,
                "status": "auto",
                "harvested_from": meta.get("message_id", ""),
                "harvested_at": meta.get("timestamp", ""),
            }

            if dry_run:
                print(f"  → [dry-run] 将追加到 {file_key}.json")
            else:
                existing.append(new_entry)
                save_exemplars(exemplar_file, existing)
                print(f"  ✓ 已追加到 {file_key}.json（status=auto）")

            stats["added"] += 1

        # 移入 processed
        if not dry_run:
            shutil.move(str(meta_path), os.path.join(processed_dir, meta_path.name))
            shutil.move(str(report_path), os.path.join(processed_dir, report_path.name))

    return stats


def promote(exemplar_dir: str):
    """把所有 status=auto 的 exemplar 提升为 verified（人工确认后运行）。"""
    changed = 0
    for f in Path(exemplar_dir).glob("*.json"):
        with open(f, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        updated = False
        for item in items:
            if item.get("status") == "auto":
                item["status"] = "verified"
                updated = True
                changed += 1
        if updated:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(items, fh, ensure_ascii=False, indent=2)
            print(f"  {f.name}: 已提升 {sum(1 for i in items if i.get('harvested_from'))} 条")
    print(f"\n共提升 {changed} 条 auto → verified")


def main():
    parser = argparse.ArgumentParser(
        description="从 bi-bridge 成功报告中提取 exemplars（查询记忆飞轮）"
    )
    parser.add_argument("state_dir", help="bi-bridge STATE_DIR 路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入文件")
    parser.add_argument("--promote", action="store_true",
                        help="把 exemplars 中 status=auto 的提升为 verified（人工确认后运行）")
    args = parser.parse_args()

    if args.promote:
        exemplar_dir = os.path.join(SCRIPT_DIR, "exemplars")
        promote(exemplar_dir)
        return

    stats = harvest(args.state_dir, dry_run=args.dry_run)

    print(f"\n{'[dry-run] ' if args.dry_run else ''}完成："
          f"扫描 {stats['scanned']} 份报告，"
          f"提取 {stats['extracted']} 条 SQL，"
          f"新增 {stats['added']} 条，"
          f"重复 {stats.get('duplicate', 0)} 条，"
          f"校验失败 {stats.get('invalid', 0)} 条，"
          f"跳过 {stats['skipped']} 条")


if __name__ == "__main__":
    main()

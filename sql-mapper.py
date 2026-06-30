#!/usr/bin/env python3
"""
sql-mapper.py — 语义 SQL → 物理 SQL 映射器

读取 schema/*.yaml 中的语义名→物理名映射，
把 LLM 生成的语义 SQL（中文字段名）翻译成可执行的物理 SQL。

用法:
  echo "SELECT 日期, SUM(用户数) FROM Banxa转化漏斗 GROUP BY 日期" | python3 sql-mapper.py
  python3 sql-mapper.py "SELECT 日期, SUM(用户数) FROM Banxa转化漏斗 GROUP BY 日期"
"""
import sys
import re
import os
from pathlib import Path

try:
    import yaml
except ImportError:
    # 无 PyYAML 时用简易解析（覆盖 schema YAML 的子集）
    yaml = None


def parse_yaml_simple(path: str) -> dict:
    """无 PyYAML 依赖的最小解析器，只处理 schema YAML 的固定结构。"""
    result = {"table": {}, "fields": []}
    current_field = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue

            # table level
            m = re.match(r"name:\s*(.+)", stripped)
            if m and current_field is None:
                result["table"]["name"] = m.group(1).strip()
                continue
            m = re.match(r"physical:\s*(.+)", stripped)
            if m and current_field is None:
                result["table"]["physical"] = m.group(1).strip()
                continue

            # field start
            if stripped == "- name:" or stripped.startswith("- name:"):
                if current_field:
                    result["fields"].append(current_field)
                name_val = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                current_field = {"name": name_val, "physical": "", "alias": []}
                continue

            if current_field is not None:
                if stripped.startswith("physical:"):
                    current_field["physical"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("alias:"):
                    alias_str = stripped.split(":", 1)[1].strip()
                    if alias_str.startswith("["):
                        aliases = alias_str.strip("[]").split(",")
                        current_field["alias"] = [a.strip().strip("'\"") for a in aliases if a.strip()]

    if current_field:
        result["fields"].append(current_field)

    return result


def load_schemas(schema_dir: str) -> dict:
    """加载 schema 目录下所有 YAML，返回 {语义名: 物理名} 的扁平映射。"""
    mapping = {}  # 语义名 → 物理名
    schema_path = Path(schema_dir)

    if not schema_path.exists():
        return mapping

    for f in schema_path.glob("*.yaml"):
        if yaml:
            with open(f, "r", encoding="utf-8") as fh:
                schema = yaml.safe_load(fh)
        else:
            schema = parse_yaml_simple(str(f))

        table = schema.get("table", {})
        table_name = table.get("name", "")
        table_physical = table.get("physical", "")

        if table_name and table_physical:
            mapping[table_name] = table_physical

        for field in schema.get("fields", []):
            fname = field.get("name", "")
            fphysical = field.get("physical", "")
            if fname and fphysical:
                mapping[fname] = fphysical
                # 别名也映射到同一个物理名
                for alias in field.get("alias", []):
                    if alias:
                        mapping[alias] = fphysical

    return mapping


def map_sql(sql: str, mapping: dict) -> str:
    """把语义 SQL 中的中文名替换成物理名。按名称长度降序替换，避免短名误匹配长名的子串。"""
    sorted_names = sorted(mapping.keys(), key=len, reverse=True)
    result = sql
    for name in sorted_names:
        if name in result:
            result = result.replace(name, mapping[name])
    return result


def main():
    # schema 目录：脚本同级的 schema/
    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_dir = os.path.join(script_dir, "schema")

    mapping = load_schemas(schema_dir)

    if not mapping:
        print("警告: 未找到 schema 映射，原样输出 SQL", file=sys.stderr)

    # 从参数或 stdin 读取 SQL
    if len(sys.argv) > 1:
        sql = " ".join(sys.argv[1:])
    else:
        sql = sys.stdin.read().strip()

    if not sql:
        print("用法: sql-mapper.py \"<语义SQL>\"", file=sys.stderr)
        print("  或: echo \"<语义SQL>\" | sql-mapper.py", file=sys.stderr)
        sys.exit(2)

    physical_sql = map_sql(sql, mapping)
    print(physical_sql)


if __name__ == "__main__":
    main()

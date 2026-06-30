#!/usr/bin/env python3
"""
schema-prompt.py — 从 schema/*.yaml 自动生成 prompt-ready 的 schema 文本。

解决的问题：手动在 prompt 和 YAML 之间维护两份 schema 容易漏改。
这个脚本让 YAML 成为唯一事实来源，prompt schema 自动派生。

输出格式兼容 SuperSonic 的 prompt schema（LLM 已验证有效），
同时追加 JOIN 关系说明。

用法:
  python3 schema-prompt.py                    # 输出所有表的 schema
  python3 schema-prompt.py --table Banxa转化漏斗  # 只输出指定表
  python3 schema-prompt.py --with-joins       # 追加 JOIN 关系说明
"""
import argparse
import os
import sys
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def parse_yaml_file(filepath):
    """解析 schema YAML 文件。"""
    if HAS_YAML:
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # 无 PyYAML 时的最小解析器
    import re
    result = {"table": {}, "fields": [], "joins": []}
    current_field = None
    current_join = None
    in_joins = False
    in_fields = False
    in_values = False

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue

            # Top-level sections
            if stripped == "joins:" or stripped.startswith("joins:"):
                in_joins = True
                in_fields = False
                continue
            if stripped == "fields:" or stripped.startswith("fields:"):
                in_fields = True
                in_joins = False
                continue

            # table level (before fields/joins)
            if not in_fields and not in_joins:
                m = re.match(r"name:\s*(.+)", stripped)
                if m:
                    result["table"]["name"] = m.group(1).strip()
                m = re.match(r"physical:\s*(.+)", stripped)
                if m:
                    result["table"]["physical"] = m.group(1).strip()
                m = re.match(r"comment:\s*(.+)", stripped)
                if m:
                    result["table"]["comment"] = m.group(1).strip().strip('"\'')
                continue

            # joins section
            if in_joins:
                if stripped.startswith("- target:"):
                    if current_join:
                        result["joins"].append(current_join)
                    current_join = {"target": stripped.split(":", 1)[1].strip()}
                elif current_join:
                    if stripped.startswith("relationship:"):
                        current_join["relationship"] = stripped.split(":", 1)[1].strip()
                    elif stripped.startswith("keys:"):
                        keys_str = stripped.split(":", 1)[1].strip()
                        if keys_str.startswith("["):
                            current_join["keys"] = [k.strip().strip("'\"") for k in keys_str.strip("[]").split(",") if k.strip()]
                    elif stripped.startswith("note:"):
                        current_join["note"] = stripped.split(":", 1)[1].strip()
                continue

            # fields section
            if in_fields:
                if stripped.startswith("- name:"):
                    if current_field:
                        result["fields"].append(current_field)
                    current_field = {"name": stripped.split(":", 1)[1].strip()}
                    in_values = False
                elif current_field:
                    if stripped.startswith("values:"):
                        in_values = True
                        current_field.setdefault("values", [])
                    elif in_values and stripped.startswith("- ") and ":" in stripped:
                        val_key = stripped.lstrip("- ").split(":")[0].strip()
                        val_label = stripped.split(":", 1)[1].strip()
                        current_field["values"].append({"key": val_key, "label": val_label})
                    elif not in_values:
                        for key in ("physical", "datatype", "type", "aggregate", "format", "comment"):
                            if stripped.startswith(f"{key}:"):
                                val = stripped.split(":", 1)[1].strip().strip('"\'')
                                current_field[key] = val
                                break
                        if stripped.startswith("alias:"):
                            alias_str = stripped.split(":", 1)[1].strip()
                            if alias_str.startswith("["):
                                current_field["alias"] = [a.strip().strip("'\"") for a in alias_str.strip("[]").split(",") if a.strip()]

    if current_join:
        result["joins"].append(current_join)
    if current_field:
        result["fields"].append(current_field)

    return result


def load_schemas(schema_dir):
    """加载 schema 目录下所有 YAML。"""
    schemas = []
    p = Path(schema_dir)
    if not p.exists():
        return schemas
    for f in sorted(p.glob("*.yaml")):
        schema = parse_yaml_file(str(f))
        if schema and schema.get("table", {}).get("name"):
            schemas.append(schema)
    return schemas


def format_field_prompt(field):
    """格式化单个字段为 prompt 文本。"""
    name = field.get("name", "")
    parts = [name]

    aliases = field.get("alias", [])
    if aliases:
        parts.append(f"ALIAS '{';'.join(aliases)}'")

    datatype = field.get("datatype")
    if datatype:
        parts.append(f"DATATYPE '{datatype}'")

    ftype = field.get("type") or field.get("aggregate")
    if ftype:
        parts.append(f"AGGREGATE '{ftype.upper()}'")

    fmt = field.get("format")
    if fmt:
        parts.append(f"FORMAT '{fmt}'")

    comment = field.get("comment")
    if comment:
        parts.append(f"COMMENT '{comment}'")

    return f"<{' '.join(parts)}>"


def format_values_prompt(field):
    """格式化字段的枚举值。"""
    values = field.get("values", [])
    if not values:
        return None
    name = field.get("name", "")
    vals = []
    if isinstance(values, list):
        for v in values:
            if isinstance(v, dict):
                for k, label in v.items():
                    vals.append(f"'{k}({label})'")
            else:
                vals.append(f"'{v}'")
    elif isinstance(values, dict):
        for k, label in values.items():
            vals.append(f"'{k}({label})'")
    if not vals:
        return None
    return f"<{name}={','.join(vals)}>"


def schema_to_prompt(schema):
    """把一个 schema 转成 prompt 文本。"""
    table = schema.get("table", {})
    fields = schema.get("fields", [])

    metrics = []
    dimensions = []
    values_parts = []
    partition_field = None

    for f in fields:
        ftype = f.get("type") or f.get("aggregate")
        if ftype:
            metrics.append(format_field_prompt(f))
        else:
            dimensions.append(format_field_prompt(f))
            # 检测时间分区字段
            if f.get("datatype") == "date" and f.get("format"):
                partition_field = f"{f['name']} FORMAT '{f['format']}'"

        val_str = format_values_prompt(f)
        if val_str:
            values_parts.append(val_str)

    lines = [f"Table=[{table.get('name', '')}]"]
    if partition_field:
        lines[0] = f"Table=[{table.get('name', '')}], PartitionTimeField=[{partition_field}]"
    lines.append(f"Metrics=[{', '.join(metrics)}]")
    lines.append(f"Dimensions=[\n  {(','+chr(10)+'  ').join(dimensions)}\n]")
    if values_parts:
        lines.append(f"Values=[{', '.join(values_parts)}]")

    return "\n".join(lines)


def format_joins_prompt(schemas):
    """生成 JOIN 关系说明文本。"""
    join_lines = []
    seen = set()

    for schema in schemas:
        table_name = schema["table"]["name"]
        for j in schema.get("joins", []):
            pair = tuple(sorted([table_name, j["target"]]))
            if pair in seen:
                continue
            seen.add(pair)

            keys = j.get("keys", [])
            note = j.get("note", "")
            keys_str = ", ".join(keys) if keys else "未声明"
            line = f"- {pair[0]} ↔ {pair[1]}: JOIN key = ({keys_str})"
            if note:
                line += f"\n  ⚠️ {note}"
            join_lines.append(line)

    if not join_lines:
        return ""

    return "## 表间关系\n" + "\n".join(join_lines)


def main():
    parser = argparse.ArgumentParser(description="Generate prompt schema from YAML definitions")
    parser.add_argument("--table", default=None, help="Only output schema for this table")
    parser.add_argument("--with-joins", action="store_true", help="Append JOIN relationship info")
    parser.add_argument("--dir", default=None, help="Schema directory (default: script_dir/schema/)")
    args = parser.parse_args()

    if args.dir:
        schema_dir = args.dir
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        schema_dir = os.path.join(script_dir, "schema")

    schemas = load_schemas(schema_dir)
    if not schemas:
        print("未找到 schema 定义", file=sys.stderr)
        sys.exit(1)

    output_parts = []
    for schema in schemas:
        if args.table and schema["table"]["name"] != args.table:
            continue
        output_parts.append(schema_to_prompt(schema))

    print("\n\n".join(output_parts))

    if args.with_joins:
        joins_text = format_joins_prompt(schemas)
        if joins_text:
            print(f"\n\n{joins_text}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
sql-validator.py — 校验语义 SQL 中的字段名和值是否合法。

读取 schema/*.yaml，检查 SQL 中引用的字段是否存在于 schema 定义中。
不合法的字段会被标记，帮助发现 LLM 幻觉。

用法:
  python3 sql-validator.py "SELECT 日期, SUM(用户数) FROM Banxa转化漏斗 GROUP BY 日期"
  echo "SELECT 瞎编字段 FROM Banxa转化漏斗" | python3 sql-validator.py

退出码: 0=合法, 1=有问题, 2=用法错误
"""
import json
import os
import re
import sys
from pathlib import Path

try:
    import sqlglot
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False

# 复用 sql-mapper 的 schema 加载逻辑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from importlib import import_module


def load_known_names(schema_dir: str) -> tuple:
    """从 schema YAML 中提取所有合法的语义名和物理名。"""
    semantic_names = set()
    physical_names = set()
    table_names = set()
    known_values = set()

    try:
        import yaml
        use_yaml = True
    except ImportError:
        use_yaml = False

    p = Path(schema_dir)
    if not p.exists():
        return semantic_names, physical_names, table_names, known_values

    for f in p.glob("*.yaml"):
        if use_yaml:
            with open(f, "r", encoding="utf-8") as fh:
                schema = yaml.safe_load(fh)
        else:
            # 简易解析
            schema = {"table": {}, "fields": []}
            current_field = None
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("#") or not s:
                        continue
                    if s.startswith("- name:"):
                        if current_field:
                            schema["fields"].append(current_field)
                        current_field = {"name": s.split(":", 1)[1].strip(), "physical": "", "alias": [], "values": {}}
                    elif current_field and s.startswith("physical:"):
                        current_field["physical"] = s.split(":", 1)[1].strip()
                    elif current_field and s.startswith("alias:"):
                        alias_str = s.split(":", 1)[1].strip()
                        if alias_str.startswith("["):
                            aliases = alias_str.strip("[]").split(",")
                            current_field["alias"] = [a.strip().strip("'\"") for a in aliases if a.strip()]
                    elif not current_field:
                        m = re.match(r"name:\s*(.+)", s)
                        if m:
                            schema["table"]["name"] = m.group(1).strip()
                        m = re.match(r"physical:\s*(.+)", s)
                        if m:
                            schema["table"]["physical"] = m.group(1).strip()
                    elif current_field and ":" in s and not s.startswith("-"):
                        k, v = s.split(":", 1)
                        k = k.strip()
                        v = v.strip()
                        # 捕获 values 中的英文值
                        if k in ("created", "kyc_passed", "pay_submitted", "pay_authorized", "completed"):
                            known_values.add(k)
                    elif current_field and s.startswith("- ") and ":" in s:
                        # values 列表项
                        val_key = s.lstrip("- ").split(":")[0].strip()
                        if val_key:
                            known_values.add(val_key)

            if current_field:
                schema["fields"].append(current_field)

        table = schema.get("table", {})
        if table.get("name"):
            table_names.add(table["name"])
        if table.get("physical"):
            table_names.add(table["physical"])

        for field in schema.get("fields", []):
            if field.get("name"):
                semantic_names.add(field["name"])
            if field.get("physical"):
                physical_names.add(field["physical"])
            for alias in field.get("alias", []):
                if alias:
                    semantic_names.add(alias)

            # values
            vals = field.get("values", {})
            if isinstance(vals, dict):
                for v in vals.keys():
                    known_values.add(str(v))
            elif isinstance(vals, list):
                for v in vals:
                    if isinstance(v, dict):
                        known_values.update(str(k) for k in v.keys())
                    else:
                        known_values.add(str(v))

    return semantic_names, physical_names, table_names, known_values


def extract_identifiers_sqlglot(sql: str) -> list:
    """用 sqlglot 解析 SQL，提取所有标识符。"""
    try:
        parsed = sqlglot.parse(sql)
        identifiers = []
        for stmt in parsed:
            for node in stmt.walk():
                if isinstance(node, sqlglot.exp.Column):
                    col_name = node.name
                    if col_name:
                        identifiers.append(("column", col_name))
                elif isinstance(node, sqlglot.exp.Table):
                    tbl_name = node.name
                    if tbl_name:
                        identifiers.append(("table", tbl_name))
        return identifiers
    except Exception:
        return extract_identifiers_regex(sql)


def extract_identifiers_regex(sql: str) -> list:
    """用正则提取 SQL 中的标识符（fallback）。"""
    identifiers = []

    # 提取 FROM/JOIN 后的表名
    for m in re.finditer(r'(?:FROM|JOIN)\s+([^\s,()]+)', sql, re.IGNORECASE):
        identifiers.append(("table", m.group(1)))

    # 提取 SELECT / WHERE / GROUP BY / ORDER BY 中的列名
    # 去掉字符串常量和数字
    clean = re.sub(r"'[^']*'", "", sql)  # 去掉字符串
    clean = re.sub(r'\b\d+\b', '', clean)  # 去掉数字

    # 匹配中文或英文标识符
    for m in re.finditer(r'(?:[\u4e00-\u9fff]+|[a-zA-Z_]\w*)', clean):
        word = m.group()
        # 排除 SQL 关键字
        sql_keywords = {
            'select', 'from', 'where', 'group', 'by', 'order', 'asc', 'desc',
            'and', 'or', 'not', 'in', 'between', 'like', 'is', 'null', 'as',
            'case', 'when', 'then', 'else', 'end', 'cast', 'float', 'int',
            'sum', 'count', 'avg', 'min', 'max', 'join', 'on', 'left', 'right',
            'inner', 'outer', 'with', 'having', 'limit', 'offset', 'union',
            'distinct', 'exists', 'all', 'any', 'into', 'insert', 'update',
            'delete', 'create', 'drop', 'alter', 'table', 'index', 'view',
        }
        if word.lower() not in sql_keywords:
            identifiers.append(("unknown", word))

    return identifiers


def load_field_types(schema_dir: str) -> dict:
    """从 schema YAML 中提取字段的聚合类型声明，返回 {语义名: type} 映射。
    支持 type 和 aggregate（兼容旧格式）两个字段名。"""
    field_types = {}  # name → type (sum/count/countDistinct/avg/min/max)

    try:
        import yaml
        use_yaml = True
    except ImportError:
        use_yaml = False

    p = Path(schema_dir)
    if not p.exists():
        return field_types

    for f in p.glob("*.yaml"):
        if use_yaml:
            with open(f, "r", encoding="utf-8") as fh:
                schema = yaml.safe_load(fh)
        else:
            schema = {"fields": []}
            current_field = None
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("#") or not s:
                        continue
                    if s.startswith("- name:"):
                        if current_field:
                            schema["fields"].append(current_field)
                        current_field = {"name": s.split(":", 1)[1].strip(), "type": None, "alias": []}
                    elif current_field:
                        if s.startswith("type:") or s.startswith("aggregate:"):
                            current_field["type"] = s.split(":", 1)[1].strip().lower()
                        elif s.startswith("alias:"):
                            alias_str = s.split(":", 1)[1].strip()
                            if alias_str.startswith("["):
                                current_field["alias"] = [a.strip().strip("'\"") for a in alias_str.strip("[]").split(",") if a.strip()]
            if current_field:
                schema["fields"].append(current_field)

        for field in schema.get("fields", []):
            ftype = field.get("type") or field.get("aggregate")
            if ftype:
                ftype = ftype.lower()
                # 兼容旧格式: "SUM" → "sum"
                fname = field.get("name", "")
                if fname:
                    field_types[fname] = ftype
                for alias in field.get("alias", []):
                    if alias:
                        field_types[alias] = ftype

    return field_types


# 聚合类型 → 允许的 SQL 聚合函数
_TYPE_TO_VALID_FUNCS = {
    "sum": {"SUM"},
    "count": {"COUNT"},
    "countdistinct": {"COUNT"},  # 必须是 COUNT(DISTINCT ...)
    "avg": {"AVG"},
    "min": {"MIN"},
    "max": {"MAX"},
}


def validate_aggregation_types(sql: str, field_types: dict) -> list:
    """校验 SQL 中的聚合函数是否匹配 schema 声明的类型。

    检测两类错误：
    1. 聚合函数类型不匹配（如 SUM(去重用户数) 但 type=countDistinct）
    2. countDistinct 类型的字段缺少 DISTINCT 关键字
    """
    issues = []

    # 匹配 AGG_FUNC( [DISTINCT] field_name )，支持中英文字段名
    # 也匹配 AGG_FUNC(CASE WHEN ... THEN field_name ...) 中的直接引用
    agg_pattern = re.compile(
        r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(\s*(DISTINCT\s+)?([\u4e00-\u9fffa-zA-Z_]\w*)\s*\)',
        re.IGNORECASE
    )

    for m in agg_pattern.finditer(sql):
        func = m.group(1).upper()
        has_distinct = bool(m.group(2))
        field_name = m.group(3)

        if field_name not in field_types:
            continue

        declared_type = field_types[field_name].lower()
        valid_funcs = _TYPE_TO_VALID_FUNCS.get(declared_type)

        if valid_funcs is None:
            continue

        if func not in valid_funcs:
            issues.append({
                "type": "aggregation_mismatch",
                "name": field_name,
                "message": (
                    f"字段 '{field_name}' 声明为 type={declared_type}，"
                    f"但 SQL 中使用了 {func}()。"
                    f"应使用 {'/'.join(sorted(valid_funcs))}()"
                )
            })
        elif declared_type == "countdistinct" and not has_distinct:
            issues.append({
                "type": "missing_distinct",
                "name": field_name,
                "message": (
                    f"字段 '{field_name}' 声明为 type=countDistinct，"
                    f"但 SQL 中使用了 COUNT({field_name}) 缺少 DISTINCT。"
                    f"应使用 COUNT(DISTINCT {field_name})"
                )
            })

    return issues


def load_field_to_tables(schema_dir: str) -> dict:
    """从 schema YAML 中构建 {字段名: set(所属表名)} 映射。
    用于检测跨表字段引用。"""
    field_tables = {}  # name → set of table names

    try:
        import yaml
        use_yaml = True
    except ImportError:
        use_yaml = False

    p = Path(schema_dir)
    if not p.exists():
        return field_tables

    for f in p.glob("*.yaml"):
        if use_yaml:
            with open(f, "r", encoding="utf-8") as fh:
                schema = yaml.safe_load(fh)
        else:
            # 用 schema-prompt.py 相同的简易解析；这里只需 table.name + field.name/alias
            schema = {"table": {}, "fields": []}
            current_field = None
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("#") or not s:
                        continue
                    if s.startswith("- name:"):
                        if current_field:
                            schema["fields"].append(current_field)
                        current_field = {"name": s.split(":", 1)[1].strip(), "alias": []}
                    elif current_field and s.startswith("alias:"):
                        alias_str = s.split(":", 1)[1].strip()
                        if alias_str.startswith("["):
                            current_field["alias"] = [a.strip().strip("'\"") for a in alias_str.strip("[]").split(",") if a.strip()]
                    elif not current_field:
                        m = re.match(r"name:\s*(.+)", s)
                        if m and "table" not in schema or not schema["table"].get("name"):
                            schema["table"]["name"] = m.group(1).strip()
            if current_field:
                schema["fields"].append(current_field)

        table_name = schema.get("table", {}).get("name", "")
        if not table_name:
            continue

        for field in schema.get("fields", []):
            for name in [field.get("name", "")] + field.get("alias", []):
                if name:
                    field_tables.setdefault(name, set()).add(table_name)

    return field_tables


def load_join_declarations(schema_dir: str) -> dict:
    """加载 schema YAML 中的 join 声明，返回 {frozenset(表A,表B): {keys: [...]}} 映射。"""
    joins = {}

    try:
        import yaml
        use_yaml = True
    except ImportError:
        use_yaml = False

    p = Path(schema_dir)
    if not p.exists():
        return joins

    for f in p.glob("*.yaml"):
        if use_yaml:
            with open(f, "r", encoding="utf-8") as fh:
                schema = yaml.safe_load(fh)
        else:
            schema = {"table": {}, "joins": []}
            current_join = None
            with open(f, "r", encoding="utf-8") as fh:
                in_joins = False
                for line in fh:
                    s = line.strip()
                    if s.startswith("joins:"):
                        in_joins = True
                        continue
                    if s.startswith("fields:"):
                        in_joins = False
                        continue
                    if not in_joins:
                        m = re.match(r"name:\s*(.+)", s)
                        if m:
                            schema["table"]["name"] = m.group(1).strip()
                        continue
                    if s.startswith("- target:"):
                        if current_join:
                            schema["joins"].append(current_join)
                        current_join = {"target": s.split(":", 1)[1].strip()}
                    elif current_join and s.startswith("keys:"):
                        keys_str = s.split(":", 1)[1].strip()
                        if keys_str.startswith("["):
                            current_join["keys"] = [k.strip().strip("'\"") for k in keys_str.strip("[]").split(",") if k.strip()]
            if current_join:
                schema["joins"].append(current_join)

        table_name = schema.get("table", {}).get("name", "")
        for j in schema.get("joins", []):
            target = j.get("target", "")
            keys = j.get("keys", [])
            if table_name and target and keys:
                pair = frozenset([table_name, target])
                joins[pair] = {"keys": keys}

    return joins


def validate_cross_table(sql: str, field_tables: dict, join_decls: dict) -> list:
    """检测 SQL 是否引用了多张表的字段，并给出 JOIN 建议。

    返回 warnings（非 errors）：不阻断，但提示可能的问题。"""
    warnings = []

    # 提取 SQL 中 FROM/JOIN 引用的表名
    from_tables = set(re.findall(
        r'(?:FROM|JOIN)\s+([\u4e00-\u9fffa-zA-Z_]\S*)',
        sql, re.IGNORECASE
    ))
    # 排除 CTE 名
    cte_names = set(re.findall(r'WITH\s+([\u4e00-\u9fffa-zA-Z_]\S+)\s+AS', sql, re.IGNORECASE))
    from_tables -= cte_names

    if not from_tables:
        return warnings

    # 找出 SQL 中引用的字段分别属于哪些表
    # 只检查「专属字段」（只属于一张表的字段），共享字段不触发
    needed_tables = set(from_tables)
    exclusive_fields = {}  # field_name → its_only_table
    for field_name, tables in field_tables.items():
        if len(tables) == 1:
            exclusive_fields[field_name] = next(iter(tables))

    for field_name, owner_table in exclusive_fields.items():
        # 用词边界匹配，避免子串误匹配
        if re.search(r'(?<![a-zA-Z\u4e00-\u9fff])' + re.escape(field_name) + r'(?![a-zA-Z\u4e00-\u9fff])', sql):
            needed_tables.add(owner_table)

    # 如果需要的表比 FROM/JOIN 中的多，提示缺少 JOIN
    missing_tables = needed_tables - from_tables
    if missing_tables:
        for mt in missing_tables:
            # 找到和 FROM 表之间的 join 声明
            for ft in from_tables:
                pair = frozenset([ft, mt])
                if pair in join_decls:
                    keys = join_decls[pair]["keys"]
                    keys_str = ", ".join(keys)
                    warnings.append({
                        "type": "missing_join",
                        "name": mt,
                        "message": (
                            f"SQL 引用了 {mt} 的字段，但 FROM/JOIN 中未包含该表。"
                            f"建议添加: JOIN {mt} USING ({keys_str})。"
                            f"⚠️ 注意漏斗表每组 key 有5行(5个阶段)，"
                            f"直接 JOIN 会导致数据膨胀，建议先用 CTE 聚合。"
                        )
                    })
                    break
            else:
                warnings.append({
                    "type": "missing_join",
                    "name": mt,
                    "message": f"SQL 引用了 {mt} 的字段，但 FROM/JOIN 中未包含该表，且未找到 JOIN 声明。"
                })

    return warnings


def validate(sql: str, schema_dir: str) -> dict:
    """校验 SQL，返回 {valid: bool, issues: [...], warnings: [...], identifiers: [...]}"""
    semantic_names, physical_names, table_names, known_values = load_known_names(schema_dir)
    all_known = semantic_names | physical_names | table_names | known_values

    if HAS_SQLGLOT:
        identifiers = extract_identifiers_sqlglot(sql)
    else:
        identifiers = extract_identifiers_regex(sql)

    issues = []
    for id_type, name in identifiers:
        if name not in all_known:
            # 跳过 AS 别名（用户自定义的输出列名）
            if re.search(rf'AS\s+{re.escape(name)}\b', sql, re.IGNORECASE):
                continue
            # 跳过 CTE 名
            if re.search(rf'WITH\s+{re.escape(name)}\s+AS', sql, re.IGNORECASE):
                continue
            # 跳过 CTE 引用（在 FROM/JOIN 中引用 CTE 名）
            cte_names = set(re.findall(r'WITH\s+(\S+)\s+AS', sql, re.IGNORECASE))
            if name in cte_names:
                continue

            issues.append({
                "type": id_type,
                "name": name,
                "message": f"字段 '{name}' 不在 schema 定义中，可能是 LLM 幻觉"
            })

    # 聚合类型校验
    field_types = load_field_types(schema_dir)
    agg_issues = validate_aggregation_types(sql, field_types)
    issues.extend(agg_issues)

    # 跨表字段检测
    field_tables = load_field_to_tables(schema_dir)
    join_decls = load_join_declarations(schema_dir)
    warnings = validate_cross_table(sql, field_tables, join_decls)

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "identifier_count": len(identifiers)
    }


def main():
    schema_dir = os.path.join(SCRIPT_DIR, "schema")

    if len(sys.argv) > 1:
        sql = " ".join(sys.argv[1:])
    else:
        sql = sys.stdin.read().strip()

    if not sql:
        print("用法: sql-validator.py \"<SQL>\"", file=sys.stderr)
        sys.exit(2)

    result = validate(sql, schema_dir)

    # 输出 warnings（不影响退出码）
    for w in result.get("warnings", []):
        print(f"⚠ {w['message']}", file=sys.stderr)

    if result["valid"]:
        print(f"✓ SQL 校验通过（{result['identifier_count']} 个标识符）")
        sys.exit(0)
    else:
        print(f"✗ SQL 校验发现 {len(result['issues'])} 个问题：")
        for issue in result["issues"]:
            print(f"  - {issue['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

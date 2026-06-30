#!/usr/bin/env python3
"""
skills_builder.py — 从 prompt 模块 + schema 自动生成多 IDE 格式的 AI skill 文件。

维护一份 prompt，生成 Claude Code / Cursor / Codex 三种 IDE 格式。
改了 prompt 后跑一次 python3 skills_builder.py，三份文件自动同步。

用法:
  python3 skills_builder.py                # 生成所有格式
  python3 skills_builder.py --check        # 检查是否过期（CI 用）
  python3 skills_builder.py --format claude # 只生成 Claude Code 格式
  python3 skills_builder.py --format cursor # 只生成 Cursor 格式
  python3 skills_builder.py --format codex  # 只生成 Codex 格式
  python3 skills_builder.py --dry-run       # 预览输出，不写文件
"""
import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

SKILL_NAME = "bi-bridge-analyst"
SKILL_DESCRIPTION = "Banxa 支付通道数据分析。当用户问转化率、漏斗、GMV、订单等问题时使用。"
SKILL_DESCRIPTION_CURSOR = "Banxa 支付通道数据分析 Agent 规则"

# prompt 模块的加载顺序（对应 prompts/ 目录下的文件名，不含 .md 后缀）
# 用于 IDE skill 文件生成（不含 environment 和 sql-footer，因为 IDE skill 不需要）
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

# analysis-agent-prompt.md 使用的完整模块顺序
# 必须与 runbook-router.py 的 PROMPT_MODULE_ORDER 保持一致
AGENT_PROMPT_MODULE_ORDER = [
    "role",
    "environment",
    "workflow",
    "common-mistakes",
    "sql-rules",
    "output-format",
    "style",
    "sql-footer",
    "error-handling",
]

# analysis-agent-prompt.md 的输出路径（相对于 bridge_dir）
AGENT_PROMPT_PATH = Path("analysis-agent-prompt.md")

# 写入 analysis-agent-prompt.md 顶部的注释
AGENT_PROMPT_HEADER = (
    "<!-- 本文件由 prompts/ 模块组装生成。"
    "修改请编辑 prompts/ 下的对应文件，然后运行 python3 skills_builder.py 重新生成。 -->"
)

# Cursor globs — 用于 alwaysApply: false 时的文件匹配
CURSOR_GLOBS = '["**/bi-bridge/**", "**/banxa*"]'

# ---------------------------------------------------------------------------
# 加载 prompt 内容
# ---------------------------------------------------------------------------


def load_prompt_modules(bridge_dir: Path) -> str:
    """
    加载 prompt 内容。

    优先从 prompts/ 目录按 PROMPT_MODULE_ORDER 加载各模块文件。
    如果 prompts/ 为空或不存在，回退到读取 analysis-agent-prompt.md。
    """
    prompts_dir = bridge_dir / "prompts"
    modules = []

    if prompts_dir.is_dir():
        # 扫描目录，按定义好的顺序加载
        for name in PROMPT_MODULE_ORDER:
            filepath = prompts_dir / f"{name}.md"
            if filepath.is_file():
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    modules.append(content)

        # 检查是否有不在 PROMPT_MODULE_ORDER 中的模块文件（提醒开发者更新列表）
        known = {f"{n}.md" for n in PROMPT_MODULE_ORDER}
        for f in sorted(prompts_dir.glob("*.md")):
            if f.name not in known:
                print(f"警告：prompts/{f.name} 不在 PROMPT_MODULE_ORDER 中，已跳过。"
                      f"如需加载请更新 PROMPT_MODULE_ORDER 列表。",
                      file=sys.stderr)

    if modules:
        return "\n\n".join(modules)

    # 回退：读取完整的 prompt 文件
    fallback = bridge_dir / "analysis-agent-prompt.md"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()

    print("错误：找不到 prompt 源文件（prompts/ 目录为空，analysis-agent-prompt.md 也不存在）",
          file=sys.stderr)
    sys.exit(1)


def load_schema_text(bridge_dir: Path) -> str:
    """
    调用 schema-prompt.py --with-joins 生成 schema 文本。

    如果 schema-prompt.py 不存在或执行失败，返回空字符串并打印警告。
    """
    script = bridge_dir / "schema-prompt.py"
    if not script.is_file():
        print("警告：schema-prompt.py 不存在，跳过 schema 部分", file=sys.stderr)
        return ""

    try:
        result = subprocess.run(
            [sys.executable, str(script), "--with-joins"],
            capture_output=True,
            text=True,
            cwd=str(bridge_dir),
            timeout=30,
        )
        if result.returncode != 0:
            print(f"警告：schema-prompt.py 执行失败: {result.stderr.strip()}",
                  file=sys.stderr)
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("警告：schema-prompt.py 执行超时", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"警告：调用 schema-prompt.py 时出错: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 组装 prompt
# ---------------------------------------------------------------------------


def assemble_full_prompt(bridge_dir: Path) -> tuple[str, str]:
    """
    组装完整 prompt，返回 (prompt_text, schema_text)。
    分开返回是因为不同 IDE 格式对 schema 的放置位置略有差异。
    """
    prompt = load_prompt_modules(bridge_dir)
    schema = load_schema_text(bridge_dir)
    return prompt, schema


def assemble_agent_prompt(bridge_dir: Path) -> str:
    """
    按 AGENT_PROMPT_MODULE_ORDER 拼接所有模块，生成 analysis-agent-prompt.md 的内容。

    这个文件由 bi-bridge.sh 直接 cat 读取作为 system prompt，
    所以必须是一个完整可用的 prompt 文件（不能是占位说明）。
    """
    prompts_dir = bridge_dir / "prompts"
    parts = []

    for name in AGENT_PROMPT_MODULE_ORDER:
        filepath = prompts_dir / f"{name}.md"
        if filepath.is_file():
            content = filepath.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

    if not parts:
        print("错误：prompts/ 目录为空，无法生成 analysis-agent-prompt.md", file=sys.stderr)
        sys.exit(1)

    return AGENT_PROMPT_HEADER + "\n\n" + "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# 生成各 IDE 格式
# ---------------------------------------------------------------------------


def generate_claude_code(prompt: str, schema: str) -> str:
    """生成 Claude Code 的 SKILL.md 格式（YAML frontmatter + markdown body）。"""
    parts = [
        "---",
        f"name: {SKILL_NAME}",
        f"description: {SKILL_DESCRIPTION}",
        "---",
        "",
        prompt,
    ]
    if schema:
        parts.extend(["", "## Schema", "", schema])
    return "\n".join(parts) + "\n"


def generate_cursor(prompt: str, schema: str) -> str:
    """生成 Cursor 的 .mdc 规则格式（YAML frontmatter + body）。"""
    body = prompt
    if schema:
        body = f"{prompt}\n\n## Schema\n\n{schema}"

    parts = [
        "---",
        f"description: {SKILL_DESCRIPTION_CURSOR}",
        f"globs: {CURSOR_GLOBS}",
        "alwaysApply: false",
        "---",
        "",
        body,
    ]
    return "\n".join(parts) + "\n"


def generate_codex(prompt: str, schema: str) -> str:
    """生成 Codex 的 .codex 格式（纯文本指令，无 frontmatter）。"""
    parts = [
        f"# {SKILL_NAME}",
        "",
        prompt,
    ]
    if schema:
        parts.extend(["", "## Schema", "", schema])
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# 文件输出
# ---------------------------------------------------------------------------

# 格式 → (生成函数, 输出路径相对于 bridge_dir)
FORMAT_REGISTRY = {
    "claude": {
        "generator": generate_claude_code,
        "output_path": Path("skills") / "claude-code" / SKILL_NAME / "SKILL.md",
    },
    "cursor": {
        "generator": generate_cursor,
        "output_path": Path("skills") / "cursor" / f"{SKILL_NAME}.mdc",
    },
    "codex": {
        "generator": generate_codex,
        "output_path": Path("skills") / "codex" / f"{SKILL_NAME}.codex",
    },
}


def content_hash(text: str) -> str:
    """计算内容的 sha256 摘要（用于 --check 比对）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_skill_file(bridge_dir: Path, rel_path: Path, content: str, dry_run: bool = False) -> bool:
    """
    写入生成的 skill 文件。返回是否有变更。

    dry_run=True 时只打印内容，不写磁盘。
    """
    out_path = bridge_dir / rel_path
    changed = True

    if out_path.is_file():
        existing = out_path.read_text(encoding="utf-8")
        if content_hash(existing) == content_hash(content):
            changed = False

    if dry_run:
        print(f"\n{'='*60}")
        print(f"  {rel_path}")
        print(f"{'='*60}")
        print(content[:500])
        if len(content) > 500:
            print(f"\n... ({len(content)} 字符，已截断)")
        return changed

    if changed:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")

    return changed


def run_build(bridge_dir: Path, formats: list[str], dry_run: bool = False) -> dict[str, bool]:
    """
    执行构建。返回 {格式名: 是否有变更}。
    """
    prompt, schema = assemble_full_prompt(bridge_dir)
    results = {}

    for fmt in formats:
        entry = FORMAT_REGISTRY[fmt]
        generator = entry["generator"]
        rel_path = entry["output_path"]
        content = generator(prompt, schema)
        changed = write_skill_file(bridge_dir, rel_path, content, dry_run)
        results[fmt] = changed

    # 同步生成 analysis-agent-prompt.md（bi-bridge.sh 直接读取此文件）
    agent_content = assemble_agent_prompt(bridge_dir)
    agent_changed = write_skill_file(bridge_dir, AGENT_PROMPT_PATH, agent_content, dry_run)
    results["agent-prompt"] = agent_changed

    return results


def run_check(bridge_dir: Path, formats: list[str]) -> bool:
    """
    检查模式：比对已有文件和最新生成内容的 hash。
    返回 True 表示全部一致，False 表示有过期文件。
    """
    prompt, schema = assemble_full_prompt(bridge_dir)
    all_ok = True

    for fmt in formats:
        entry = FORMAT_REGISTRY[fmt]
        generator = entry["generator"]
        rel_path = entry["output_path"]
        out_path = bridge_dir / rel_path

        expected = generator(prompt, schema)

        if not out_path.is_file():
            print(f"  MISSING  {rel_path}")
            all_ok = False
            continue

        actual = out_path.read_text(encoding="utf-8")
        if content_hash(actual) != content_hash(expected):
            print(f"  STALE    {rel_path}")
            all_ok = False
        else:
            print(f"  OK       {rel_path}")

    # 检查 analysis-agent-prompt.md
    agent_expected = assemble_agent_prompt(bridge_dir)
    agent_path = bridge_dir / AGENT_PROMPT_PATH

    if not agent_path.is_file():
        print(f"  MISSING  {AGENT_PROMPT_PATH}")
        all_ok = False
    else:
        agent_actual = agent_path.read_text(encoding="utf-8")
        if content_hash(agent_actual) != content_hash(agent_expected):
            print(f"  STALE    {AGENT_PROMPT_PATH}")
            all_ok = False
        else:
            print(f"  OK       {AGENT_PROMPT_PATH}")

    return all_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="从 prompt 模块 + schema 自动生成多 IDE 格式的 skill 文件"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查已有文件是否和最新 prompt 一致（CI 用），不一致则返回非零退出码",
    )
    parser.add_argument(
        "--format",
        choices=list(FORMAT_REGISTRY.keys()),
        default=None,
        help="只生成指定格式（默认全部生成）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览生成内容，不写文件",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="指定 bridge 目录（默认使用脚本所在目录）",
    )
    args = parser.parse_args()

    bridge_dir = Path(args.dir) if args.dir else SCRIPT_DIR
    formats = [args.format] if args.format else list(FORMAT_REGISTRY.keys())

    if args.check:
        print("检查 skill 文件是否过期...")
        ok = run_check(bridge_dir, formats)
        if ok:
            print("\n全部一致，skill 文件是最新的。")
            sys.exit(0)
        else:
            print("\n有文件过期或缺失。请运行 python3 skills_builder.py 重新生成。")
            sys.exit(1)

    results = run_build(bridge_dir, formats, dry_run=args.dry_run)

    if args.dry_run:
        return

    for fmt, changed in results.items():
        if fmt == "agent-prompt":
            rel_path = AGENT_PROMPT_PATH
        else:
            rel_path = FORMAT_REGISTRY[fmt]["output_path"]
        status = "已更新" if changed else "无变更"
        print(f"  {status}  {rel_path}")

    updated = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n完成：{updated}/{total} 个文件已更新。")


if __name__ == "__main__":
    main()

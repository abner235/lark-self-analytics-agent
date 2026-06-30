# bi-bridge sandbox 配置（示例）——复制成 config.sandbox.sh 后填入你自己的真实值。
#   cp config.sandbox.example.sh config.sandbox.sh && vim config.sandbox.sh
# config.sandbox.sh 已被 .gitignore，不会进仓库（含真实群/用户 ID）。
# 用法： ../bi-bridge.sh ./sandbox/config.sandbox.sh
SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 自动解析为本文件所在目录，可移植

export BOT_NAME="你的机器人显示名"                               # @检测匹配的显示名，如 "BI-YourName"
export ALLOWED_GROUPS="oc_xxxxxxxxxxxxxxxxxxxxxxxxx"              # 群 chat_id；lark-cli im +chat-search 查
export ALLOWED_SENDERS="ou_xxxxxxxxxxxxxxxxxxxxxxxxx"            # 只允许此人触发；fail-closed：留空时退回仅 OWNER_AT，OWNER_AT 也空则无人可触发
export OWNER_AT="ou_xxxxxxxxxxxxxxxxxxxxxxxxx"                  # cookie 过期/取数失败时 @ 的 owner open_id
# @检测：留空=BOT_NAME 子串匹配；建议抓一条真实 @ 事件看渲染形态后收紧（见 SETUP）
export MENTION_MATCH=""

export WORKDIR="$SANDBOX_DIR"                                    # 让 headless 能加载 project skill + mcq
export ANALYSIS_PROMPT_FILE="$SANDBOX_DIR/analysis-agent-prompt.sandbox.md"
export STATE_DIR="$HOME/.bi-bridge-sandbox"

# headless 工具收口（sandbox）。数组语法！
#   - Bash 只授权 ./mcq-safe（不是裸 ./mcq）：mcq-safe 会先过 sql-validator 校验表/字段白名单、
#     再映射执行。注入「SELECT * FROM 用户实名表 / sqlite_master」这类越权 SQL 会在执行前被拒，
#     而不是用 owner 凭证真把数据拉出来回贴进群。实测 Bash(./mcq-safe:*) 放行、Bash(mcq-safe:*) 不匹配。
#   - 不给 Read/Grep/Glob：注入即可读任意本机文件回贴进群。表结构/口径由 mc-query-sandbox 的
#     SKILL.md 携带，取数走 ./mcq-safe，运行时不需要文件读取工具。
CLAUDE_ARGS=(--allowedTools "Skill Bash(./mcq-safe:*)" --max-turns 30)

export TASK_TIMEOUT_SECS=300
export MAX_CONCURRENCY=2
export DAILY_CAP=50
export CONSUME_TIMEOUT="30m"
export LARK_AS="bot"
export LARK_EXTRA=""
export LARK_BIN="lark-cli"
export CLAUDE_BIN="claude"

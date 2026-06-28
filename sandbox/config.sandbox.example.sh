# bi-bridge sandbox 配置（示例）——复制成 config.sandbox.sh 后填入你自己的真实值。
#   cp config.sandbox.example.sh config.sandbox.sh && vim config.sandbox.sh
# config.sandbox.sh 已被 .gitignore，不会进仓库（含真实群/用户 ID）。
# 用法： ../bi-bridge.sh ./sandbox/config.sandbox.sh
SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 自动解析为本文件所在目录，可移植

export BOT_NAME="你的机器人显示名"                               # @检测匹配的显示名，如 "BI-YourName"
export ALLOWED_GROUPS="oc_xxxxxxxxxxxxxxxxxxxxxxxxx"              # 群 chat_id；lark-cli im +chat-search 查
export ALLOWED_SENDERS="ou_xxxxxxxxxxxxxxxxxxxxxxxxx"            # 只允许此人触发（防群内误触发烧额度）；留空=群内任何人
export OWNER_AT="ou_xxxxxxxxxxxxxxxxxxxxxxxxx"                  # cookie 过期/取数失败时 @ 的 owner open_id
# @检测：留空=BOT_NAME 子串匹配；建议抓一条真实 @ 事件看渲染形态后收紧（见 SETUP）
export MENTION_MATCH=""

export WORKDIR="$SANDBOX_DIR"                                    # 让 headless 能加载 project skill + mcq
export ANALYSIS_PROMPT_FILE="$SANDBOX_DIR/analysis-agent-prompt.sandbox.md"
export STATE_DIR="$HOME/.bi-bridge-sandbox"

# headless 工具收口（sandbox）。数组语法！Bash 已收口到只允许 ./mcq（防 prompt 注入放开整个 shell）。
# 实测：Bash(./mcq:*) 能放行（agent 跑的是 ./mcq）；写成 Bash(mcq:*) 不匹配会被拒。
CLAUDE_ARGS=(--allowedTools "Skill Read Grep Glob Bash(./mcq:*)" --max-turns 30)

export TASK_TIMEOUT_SECS=300
export MAX_CONCURRENCY=2
export DAILY_CAP=50
export CONSUME_TIMEOUT="30m"
export LARK_AS="bot"
export LARK_EXTRA=""
export LARK_BIN="lark-cli"
export CLAUDE_BIN="claude"

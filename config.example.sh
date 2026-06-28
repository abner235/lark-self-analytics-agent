# =============================================================================
# bi-bridge 配置示例 —— 复制成 config.<你的名字>.sh 后填写，再 source 启动。
#   cp config.example.sh config.charlie.sh && vim config.charlie.sh
#   ./bi-bridge.sh ./config.charlie.sh
# 这是个被 bash source 的文件（零依赖，不需要 yq）。注意别把它提交进公共 repo。
# =============================================================================

# ---- 必填 ----
# 机器人显示名：群里 @ 出来在 .content 里渲染成的那串名字（@检测靠它）
export BOT_NAME="BI-Charlie"

# 受限分析指令文件（headless Agent 的 system prompt 追加内容）
export ANALYSIS_PROMPT_FILE="$HOME/bi-skills/bridge/analysis-agent-prompt.md"

# 状态目录（去重/计数/审计日志/超时临时文件都放这）
export STATE_DIR="$HOME/.bi-bridge"

# 群白名单：空格分隔的 chat_id（oc_xxx）。留空 = 不响应任何群（安全默认）。
#   用 `lark-cli im +chat-search --query "群名" --as bot` 查 chat_id。
export ALLOWED_GROUPS="oc_xxxxxxxxxxxxxxxxxxxxxxxxx"

# ---- 强烈建议填 ----
# 发起人白名单：空格分隔的 open_id（ou_xxx）。留空 = 白名单群内任何人都能触发。
#   查 open_id：`lark-cli contact +search-user --query "姓名" --as user`
export ALLOWED_SENDERS=""

# owner 的 open_id：cookie 过期/取数失败时回贴里 @ 他提醒重登
export OWNER_AT=""

# headless Agent 的工作目录（必须能 Skill 调用到本人的 mc-query / datawind）
export WORKDIR="$HOME"

# ---- 多机器人 / profile 选择（见 SETUP.md 第2步）----
# 一个 lark-cli profile = 一个 bot appId。若你的 lark-cli 版本用独立 config 路径或
# profile 名来区分机器人，把对应参数放这里透传给每条 lark-cli 命令。
# 例（按你的实际版本二选一，用 `lark-cli config --help` 确认）：
#   export LARK_EXTRA="--config $HOME/.lark/bi-charlie.json"
#   export LARK_EXTRA="--profile bi-charlie"
export LARK_EXTRA=""

# ---- 护栏（有默认值，按需覆盖）----
export TASK_TIMEOUT_SECS=600     # 单任务超时（秒）
export MAX_CONCURRENCY=2         # 同时在跑的分析数
export DAILY_CAP=50              # 每日触发上限
export CONSUME_TIMEOUT="30m"     # consume 单轮时长，到点重启
export REPLY_FILE_THRESHOLD=3000 # 报告超过此字符数转文件上传

# headless 工具收口（核心安全项！）。⚠️ 必须是 bash 数组，不能是字符串——否则带空格的
# --allowedTools 值会被错误拆词。默认放 Bash 是因为 mc-query/datawind 多半要跑各自 CLI；
# 强烈建议按 `claude --help` 的实际语法收成 scoped，禁止任意 shell：
#   CLAUDE_ARGS=(--allowedTools "Skill Read Grep Glob Bash(mc-query:*) Bash(datawind:*)" --max-turns 30)
CLAUDE_ARGS=(--allowedTools "Skill Read Grep Glob Bash" --max-turns 30)
# 可选：固定模型 → CLAUDE_ARGS+=(--model claude-opus-4-8)

# ---- 可选：把调用记录同步进 Lark Base usage_leaderboard（P0 §7 看板）----
# 不填则跳过，本地 $STATE_DIR/invocations.jsonl 仍是可靠审计真相源。
# export LARK_BASE_APP_TOKEN=""
# export LARK_BASE_TABLE_ID=""

# ---- 二进制路径（一般不用改）----
export LARK_BIN="lark-cli"
export CLAUDE_BIN="claude"
export LARK_AS="bot"

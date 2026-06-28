# =============================================================================
# bi-bridge 配置示例 —— 复制成 config.<你的名字>.sh 后填写，再 source 启动。
#   cp config.example.sh config.me.sh && vim config.me.sh
#   ./bi-bridge.sh ./config.me.sh
# 这是个被 bash source 的文件（零依赖，不需要 yq）。注意别把它提交进公共 repo。
# =============================================================================

# ---- 必填 ----
# 机器人显示名：群里 @ 出来在 .content 里渲染成的那串名字（@检测靠它）
export BOT_NAME="BI-YourName"

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

# @检测：留空 = 用 BOT_NAME 子串匹配（默认）。lark-cli 把结构化 mentions 抹掉了、@已渲染进
# content 文本，所以只能按文本匹配。想降误触发就抓一条真实 @ 事件看渲染形态（SETUP 第2步末），
# 再设成精确正则（grep -iE），如 MENTION_MATCH='@?BI-YourName'
export MENTION_MATCH=""

# headless Agent 的工作目录（必须能 Skill 调用到本人的 mc-query / datawind）
export WORKDIR="$HOME"

# ---- 多机器人 / profile 选择（见 SETUP.md 第2步）----
# 一个 lark-cli profile = 一个 bot appId。若你的 lark-cli 版本用独立 config 路径或
# profile 名来区分机器人，把对应参数放这里透传给每条 lark-cli 命令。
# 例（按你的实际版本二选一，用 `lark-cli config --help` 确认）：
#   export LARK_EXTRA="--config $HOME/.lark/bi-yourname.json"
#   export LARK_EXTRA="--profile bi-yourname"
export LARK_EXTRA=""

# ---- 护栏（有默认值，按需覆盖）----
export TASK_TIMEOUT_SECS=600     # 单任务超时（秒）
export MAX_CONCURRENCY=2         # 同时在跑的分析数
export DAILY_CAP=50              # 每日触发上限
export CONSUME_TIMEOUT="30m"     # consume 单轮时长，到点重启
export REPLY_FILE_THRESHOLD=3000 # 报告超过此字符数转文件上传
export STATE_TTL_DAYS=7          # 启动时清理 N 天前的 seen/counter（防状态文件无限增长）
export ERR_LOG_KEEP_LINES=2000   # 启动时把 *.err 截到最后 N 行（防日志涨到 GB）

# headless 工具收口（核心安全项！）。⚠️ 必须是 bash 数组，不能是字符串——否则带空格的
# --allowedTools 值会被错误拆词。⚠️ 别放开整个 Bash（白名单内用户可借 prompt 注入跑任意 shell）：
# 把 Bash 收口到取数 skill 实际运行的命令前缀。下面是占位，按你 mc-query/datawind 真实跑的命令改：
#   - 先观察该 skill 实际执行的 bash 命令（看它 SKILL.md / 跑一次看 tool 调用），再照着写前缀
#   - 注意前缀要和实际命令首 token 完全一致（实测 ./mcq 要写 Bash(./mcq:*)，写 Bash(mcq:*) 不匹配）
CLAUDE_ARGS=(--allowedTools "Skill Read Grep Glob Bash(mc-query:*) Bash(datawind:*)" --max-turns 30)
# 可选：固定模型 → CLAUDE_ARGS+=(--model claude-opus-4-8)

# ---- 可选：把调用记录同步进 Lark Base usage_leaderboard（P0 §7 看板）----
# 不填则跳过，本地 $STATE_DIR/invocations.jsonl 仍是可靠审计真相源。
# export LARK_BASE_APP_TOKEN=""
# export LARK_BASE_TABLE_ID=""

# ---- 二进制路径（一般不用改）----
export LARK_BIN="lark-cli"
export CLAUDE_BIN="claude"
export LARK_AS="bot"

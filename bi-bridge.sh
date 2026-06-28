#!/usr/bin/env bash
# =============================================================================
# bi-bridge.sh — BI 团队 Agent 互联互通 V1 bridge daemon
#
# 作用：常驻消费「飞书群 @本机器人」事件 → 触发本机 headless 分析 Agent
#       （调本人 mc-query / datawind 取数）→ 把报告回贴到群里。
#
# 设计要点（对应 plan）：
#   - 去中心化：本脚本跑在「本人」机器上，用「本人」的 lark-cli config + 本人
#     的 mc-query/datawind cookie。每个分析师各跑一份自己的实例。
#   - 复用：lark-cli event consume（收）+ lark-cli im +messages-reply（回）。
#   - 护栏：群白名单 / 发起人白名单 / @检测 / 去重 / 并发上限 / 单任务超时 /
#     日配额 / headless 工具收口 / cookie 过期优雅降级 / 全程留痕。
#
# 用法：bi-bridge.sh /path/to/config.<analyst>.sh
#   config 是一个被 source 的 bash 文件（零依赖，不需要 yq），变量见 config.example.sh
#
# 关键契约（来自 skills/lark-event/SKILL.md，务必遵守）：
#   - 绝不 kill -9 consume 进程（会泄漏服务端订阅）。本脚本用 --timeout 循环重启，
#     自身从不主动 kill consume；launchd/systemd 停服时发 SIGTERM 即可。
#   - im.message.receive_v1 的 .content 对 text 消息是「已渲染纯文本，@提及=显示名」，
#     不是 <at> tag。所以 @检测 = 匹配机器人显示名（BOT_NAME）。
# =============================================================================
# 注意：刻意不开 `set -u`。stock macOS 自带 bash 3.2，`set -u` 遇到空数组
# （如 "${CLAUDE_ARGS[@]}"）会误报 unbound variable。脚本对所有变量都显式给了默认值，
# 不依赖 -u 兜底。保留 pipefail 捕获管道里的失败。
set -o pipefail

# ---------- 0. 载入配置 ----------
CONFIG="${1:-}"
if [[ -z "$CONFIG" || ! -f "$CONFIG" ]]; then
  echo "用法: $0 <config.<analyst>.sh>   （参考 config.example.sh）" >&2
  exit 2
fi
# shellcheck source=/dev/null
source "$CONFIG"

# 必填项校验
: "${BOT_NAME:?config 缺 BOT_NAME（机器人显示名，用于 @检测）}"
: "${ANALYSIS_PROMPT_FILE:?config 缺 ANALYSIS_PROMPT_FILE}"
: "${STATE_DIR:?config 缺 STATE_DIR}"
[[ -f "$ANALYSIS_PROMPT_FILE" ]] || { echo "找不到分析指令文件: $ANALYSIS_PROMPT_FILE" >&2; exit 2; }

# 带默认值的可选项
LARK_BIN="${LARK_BIN:-lark-cli}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# headless 调用的附加参数：必须是 bash 数组（config 里用 CLAUDE_ARGS=(...) 定义），
# 否则带空格的 --allowedTools 值会被错误拆词。工具收口在这里！见 config.example.sh。
# 默认 fail-closed：不给 Bash，强制由 config 显式授权取数 CLI（如 Bash(./mcq:*) / Bash(mc-query:*)），
# 防止 prompt 注入把整个 shell 放开。未授权时 agent 取不到数会明确报错，而非拥有任意 shell。
if [[ -z "${CLAUDE_ARGS+x}" ]]; then
  CLAUDE_ARGS=(--allowedTools "Skill Read Grep Glob" --max-turns 30)
fi
TASK_TIMEOUT_SECS="${TASK_TIMEOUT_SECS:-600}"   # 单任务 10min 超时
MAX_CONCURRENCY="${MAX_CONCURRENCY:-2}"         # 同时在跑的分析数
DAILY_CAP="${DAILY_CAP:-50}"                    # 每日触发上限（成本/防刷）
CONSUME_TIMEOUT="${CONSUME_TIMEOUT:-30m}"       # consume 单轮时长，到点优雅退出后重启
BACKOFF_SECS="${BACKOFF_SECS:-5}"               # consume 异常退出后的重启退避
REPLY_FILE_THRESHOLD="${REPLY_FILE_THRESHOLD:-3000}"  # 报告超过此字符数 → 转文件上传
STATE_TTL_DAYS="${STATE_TTL_DAYS:-7}"           # 启动时清理 N 天前的 seen/counter（防无限增长）
ERR_LOG_KEEP_LINES="${ERR_LOG_KEEP_LINES:-2000}"  # 启动时把 *.err 截到最后 N 行（防单文件涨到 GB）
ALLOWED_GROUPS="${ALLOWED_GROUPS:-}"            # 空格分隔的 chat_id 白名单（必配，留空=不响应任何群）
ALLOWED_SENDERS="${ALLOWED_SENDERS:-}"          # 空格分隔的 open_id 白名单（留空=允许白名单群内任何人）
OWNER_AT="${OWNER_AT:-}"                        # owner open_id，cookie 过期时 @ 他
# @检测匹配：留空=用 BOT_NAME 子串匹配（默认，向后兼容）；设了则用该正则(grep -iE)精确匹配。
# 注：lark-cli 把结构化 mentions 抹掉、@已渲染进 content 文本，故只能按文本匹配。先用 SETUP 的
# 抓样本命令看真实渲染形态，再把 MENTION_MATCH 收紧（如 '@?BI-YourName'）降误触发。
MENTION_MATCH="${MENTION_MATCH:-}"
WORKDIR="${WORKDIR:-$HOME}"                     # headless Agent 的工作目录（应能访问 mc-query/datawind skill）
LARK_AS="${LARK_AS:-bot}"
# 选择本机器人 profile 的额外参数（如某些版本用 --config <path> 或环境变量；见 SETUP.md）
LARK_EXTRA="${LARK_EXTRA:-}"

# 超时实现：优先用 coreutils 的 timeout/gtimeout（更干净，能连带杀子进程）；
# stock macOS 两者都没有 → 用纯 bash 兜底（见 run_with_timeout）。
TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD="gtimeout"; fi

# 纯 bash 超时：run_with_timeout <secs> <outfile> <errfile> -- cmd args...
# 超时返回 124（与 coreutils timeout 对齐），否则返回子命令退出码。
run_with_timeout() {
  local secs="$1" outf="$2" errf="$3"; shift 3; [[ "${1:-}" == "--" ]] && shift
  # 开 job control，让后台任务自成进程组（pgid==pid），超时时杀整组——否则只杀到
  # claude 直接子进程，它派生的 mc-query/odpscmd 子进程会变孤儿泄漏。
  set -m
  "$@" >"$outf" 2>"$errf" &
  local pid=$!
  set +m
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1; waited=$((waited + 1))
    if (( waited >= secs )); then
      kill -TERM -"$pid" 2>/dev/null; sleep 2; kill -KILL -"$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
  done
  wait "$pid"; return $?
}

# ---------- 1. 目录与日志 ----------
mkdir -p "$STATE_DIR/seen" "$STATE_DIR/running"
AUDIT_LOG="$STATE_DIR/invocations.jsonl"   # 永久审计 + 喂看板的本地真相源
log() { printf '%s [bi-bridge] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >&2; }

# 启动时清理状态，防无限增长：seen/counter 删 N 天前；*.err 截到最后 N 行。
# （seen 删旧不影响去重：飞书不会在数天后重投同一 event_id；running/ 是活跃槽位不清。）
cleanup_state() {
  find "$STATE_DIR/seen" -type f -mtime "+${STATE_TTL_DAYS}" -delete 2>/dev/null
  find "$STATE_DIR" -maxdepth 1 -type f -name 'counter.*' -mtime "+${STATE_TTL_DAYS}" -delete 2>/dev/null
  local f
  for f in "$STATE_DIR/consume.err" "$STATE_DIR/lark.err"; do
    [[ -f "$f" ]] || continue
    if [[ "$(wc -l < "$f" 2>/dev/null || echo 0)" -gt "$ERR_LOG_KEEP_LINES" ]]; then
      tail -n "$ERR_LOG_KEEP_LINES" "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
    fi
  done
}
cleanup_state

# ---------- 2. 小工具 ----------
today() { date '+%Y-%m-%d'; }

# 日配额：返回 0=未超额并已 +1，1=超额
bump_daily_counter() {
  local f="$STATE_DIR/counter.$(today)"
  local n; n=$(cat "$f" 2>/dev/null || echo 0)
  if (( n >= DAILY_CAP )); then return 1; fi
  echo $(( n + 1 )) > "$f"
  return 0
}

# 并发：当前在跑数量
running_count() { find "$STATE_DIR/running" -type f 2>/dev/null | wc -l | tr -d ' '; }

# 回贴（reply 到触发那条消息）。$1=message_id $2=模式(text|markdown|file) $3=内容或文件路径
# 注意：lark-cli 把 {ok:false,error} 错误信封写到 stdout（不是 stderr），所以必须捕获
# stdout 并校验 .ok，否则 reply 失败（cookie 过期/缺 scope）会被静默吞掉、审计仍记 ok。
reply() {
  local mid="$1" mode="$2" body="$3" resp ok
  # shellcheck disable=SC2086
  case "$mode" in
    text)     resp=$("$LARK_BIN" im +messages-reply --message-id "$mid" --as "$LARK_AS" $LARK_EXTRA --text "$body"     2>>"$STATE_DIR/lark.err") ;;
    markdown) resp=$("$LARK_BIN" im +messages-reply --message-id "$mid" --as "$LARK_AS" $LARK_EXTRA --markdown "$body" 2>>"$STATE_DIR/lark.err") ;;
    file)     resp=$("$LARK_BIN" im +messages-reply --message-id "$mid" --as "$LARK_AS" $LARK_EXTRA --file "$body"     2>>"$STATE_DIR/lark.err") ;;
  esac
  ok=$(printf '%s' "$resp" | jq -r '.ok // empty' 2>/dev/null)
  if [[ "$ok" != "true" ]]; then
    log "REPLY-FAIL mid=$mid mode=$mode resp=$(printf '%s' "$resp" | tr -d '\n' | head -c 200)"
    return 1
  fi
  return 0
}

# 审计记一行（本地永久；看板同步见 log_to_base 钩子）
audit() {
  # $1=message_id $2=chat_id $3=sender_id $4=status $5=duration_s $6=task(截断)
  # 用 jq -n 构造 JSON，由 jq 负责转义——任务文本里有引号/反斜杠/控制字符也不会产出非法 JSON。
  local dur="${5:-0}"; [[ "$dur" =~ ^[0-9]+$ ]] || dur=0
  jq -nc \
    --arg ts "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
    --arg message_id "$1" --arg chat_id "$2" --arg sender_id "$3" \
    --arg status "$4" --argjson duration_s "$dur" \
    --arg bot "$BOT_NAME" --arg task "${6:0:200}" \
    '{ts:$ts,message_id:$message_id,chat_id:$chat_id,sender_id:$sender_id,status:$status,duration_s:$duration_s,bot:$bot,task:$task}' \
    >> "$AUDIT_LOG"
  log_to_base "$@" || true
}

# 可选：把一行写进 Lark Base usage_leaderboard（复用 P0 §7 看板）。
# ⚠️ lark-base 的 record 写入命令语法请先用 `lark-cli base --help` 确认后填进来；
#    未配置 LARK_BASE_APP_TOKEN 时本函数直接跳过，本地 invocations.jsonl 仍是可靠真相源。
log_to_base() {
  [[ -n "${LARK_BASE_APP_TOKEN:-}" && -n "${LARK_BASE_TABLE_ID:-}" ]] || return 0
  # TODO(SETUP.md 第6步): 用确认过的命令替换下面占位（保持 --as bot / profile）。
  #   形如: "$LARK_BIN" base record create --app-token "$LARK_BASE_APP_TOKEN" \
  #           --table-id "$LARK_BASE_TABLE_ID" --as "$LARK_AS" $LARK_EXTRA \
  #           --fields '{"触发人":"'"$3"'","状态":"'"$4"'","耗时秒":'"$5"',"机器人":"'"$BOT_NAME"'"}'
  return 0
}

# ---------- 3. 单个任务处理（在后台子进程里跑）----------
run_task() {
  local mid="$1" chat="$2" sender="$3" content="$4"
  # marker 由 handle_event 在主循环里同步创建（避免 TOCTOU）；这里只负责退出时清理。
  local marker="$STATE_DIR/running/$mid"
  trap 'rm -f "$marker"' EXIT

  # 从 content 里剥掉机器人名，得到纯任务文本
  local task; task=$(printf '%s' "$content" | sed "s/@\{0,1\}${BOT_NAME}//g" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  [[ -z "$task" ]] && task="$content"

  reply "$mid" text "收到，开始分析…⏳（约需 1–几分钟）"
  log "RUN mid=$mid chat=$chat sender=$sender task=${task:0:80}"

  local sys; sys=$(cat "$ANALYSIS_PROMPT_FILE")
  local out err outf rc start dur
  start=$(date +%s)
  err="$STATE_DIR/.err.$mid"
  outf="$STATE_DIR/.out.$mid"
  # headless 调用：受限工具（CLAUDE_ARGS 数组）+ 注入分析指令 + 超时兜底
  if [[ -n "$TIMEOUT_CMD" ]]; then
    ( cd "$WORKDIR" && "$TIMEOUT_CMD" "$TASK_TIMEOUT_SECS" "$CLAUDE_BIN" -p "$task" \
        --append-system-prompt "$sys" "${CLAUDE_ARGS[@]}" ) >"$outf" 2>"$err"
    rc=$?
  else
    # 纯 bash 兜底：包一层 cd 后 exec，把 CLAUDE_ARGS 数组原样透传
    run_with_timeout "$TASK_TIMEOUT_SECS" "$outf" "$err" -- \
      bash -c 'cd "$1"; shift; exec "$@"' _ "$WORKDIR" \
        "$CLAUDE_BIN" -p "$task" --append-system-prompt "$sys" "${CLAUDE_ARGS[@]}"
    rc=$?
  fi
  out=$(cat "$outf" 2>/dev/null); rm -f "$outf"
  dur=$(( $(date +%s) - start ))

  if (( rc == 124 )); then
    reply "$mid" text "⏱️ 分析超时（>${TASK_TIMEOUT_SECS}s）已终止。可缩小问题范围后重试。"
    audit "$mid" "$chat" "$sender" "timeout" "$dur" "$task"
  elif (( rc != 0 )) || [[ -z "$out" ]]; then
    local tail; tail=$(tail -c 400 "$err" 2>/dev/null)
    local owner_hint=""; [[ -n "$OWNER_AT" ]] && owner_hint=$' <at user_id="'"$OWNER_AT"$'">owner</at> 请确认 mc-query/datawind 登录是否过期。'
    reply "$mid" markdown "❌ 分析失败（rc=$rc）。若是数据系统登录过期（cookie 失效），需要 owner 在本机重新登录后重试。${owner_hint}"$'\n\n```\n'"${tail}"$'\n```'
    audit "$mid" "$chat" "$sender" "fail" "$dur" "$task"
    log "FAIL mid=$mid rc=$rc err=${tail:0:120}"
  else
    if (( ${#out} > REPLY_FILE_THRESHOLD )); then
      local f="$STATE_DIR/report.$mid.md"; printf '%s\n' "$out" > "$f"
      reply "$mid" text "✅ 分析完成（报告较长，见附件）。耗时 ${dur}s。"
      reply "$mid" file "$f"
      rm -f "$f"
    else
      reply "$mid" markdown "$out"
    fi
    audit "$mid" "$chat" "$sender" "ok" "$dur" "$task"
    log "OK mid=$mid dur=${dur}s"
  fi
  rm -f "$err"
}

# ---------- 4. 事件分发（含全部护栏）----------
in_list() {  # $1=needle, $2=space-separated haystack（空 haystack 视配置语义另判）
  local x; for x in $2; do [[ "$x" == "$1" ]] && return 0; done; return 1
}

handle_event() {
  local line="$1"
  # 解析投影后的字段（consume 已用 --jq 投成 {event_id,message_id,chat_id,sender_id,content}）
  local evid mid chat sender content
  evid=$(printf '%s' "$line"    | jq -r '.event_id // empty'   2>/dev/null)   || return 0
  mid=$(printf '%s' "$line"     | jq -r '.message_id // empty' 2>/dev/null)
  chat=$(printf '%s' "$line"    | jq -r '.chat_id // empty'    2>/dev/null)
  sender=$(printf '%s' "$line"  | jq -r '.sender_id // empty'  2>/dev/null)
  content=$(printf '%s' "$line" | jq -r '.content // empty'    2>/dev/null)
  [[ -z "$mid" ]] && return 0

  # 4.1 去重：用 event_id（schema 明确"safe for deduplication"），缺失则退回 message_id
  local dedup_key="${evid:-$mid}"
  local seen="$STATE_DIR/seen/$dedup_key"
  [[ -e "$seen" ]] && return 0
  : > "$seen"

  # 4.2 群白名单
  if [[ -z "$ALLOWED_GROUPS" ]] || ! in_list "$chat" "$ALLOWED_GROUPS"; then
    return 0
  fi
  # 4.3 @检测：MENTION_MATCH 正则优先（精确），否则退回 BOT_NAME 子串（向后兼容）
  if [[ -n "$MENTION_MATCH" ]]; then
    printf '%s' "$content" | grep -qiE "$MENTION_MATCH" || return 0
  else
    [[ "$content" != *"$BOT_NAME"* ]] && return 0
  fi
  # 4.4 发起人白名单（留空=群内任何人可触发）
  if [[ -n "$ALLOWED_SENDERS" ]] && ! in_list "$sender" "$ALLOWED_SENDERS"; then
    log "DROP sender=$sender 不在白名单 mid=$mid"
    return 0
  fi
  # 4.5 并发上限（先于日配额：忙线被拒不应消耗当日额度）
  if (( $(running_count) >= MAX_CONCURRENCY )); then
    reply "$mid" text "🚧 我正忙（已有 $MAX_CONCURRENCY 个分析在跑），请稍后再 @ 我。"
    return 0
  fi
  # 4.6 日配额（只有真正会启动的任务才计数）
  if ! bump_daily_counter; then
    reply "$mid" text "今日已达调用上限（$DAILY_CAP 次），明天再来或联系 owner 调整。"
    return 0
  fi

  # 放行：先在主循环里同步占位（修 TOCTOU：占位先于后台化，running_count 立刻准确），
  # 再后台跑、主循环继续收事件。占位文件由 run_task 退出时清理。
  : > "$STATE_DIR/running/$mid"
  run_task "$mid" "$chat" "$sender" "$content" &
}

# ---------- 5. 主循环：消费 → 分发，consume 退出后自动重启 ----------
log "启动 bi-bridge：bot='$BOT_NAME' groups='$ALLOWED_GROUPS' workdir='$WORKDIR'"
JQ_PROJECT='select(.chat_type=="group" and .message_type=="text") | {event_id, message_id, chat_id, sender_id, content}'

while true; do
  log "开始一轮 consume（--timeout $CONSUME_TIMEOUT）"
  # stderr → 日志文件（含 ready/exit marker）；stdout 逐行喂 handle_event
  # 自身从不 kill consume：靠 --timeout 自然退出，再 while 重启 → 满足「绝不 kill -9」
  # shellcheck disable=SC2086
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    handle_event "$line"
  done < <("$LARK_BIN" event consume im.message.receive_v1 --as "$LARK_AS" $LARK_EXTRA \
              --timeout "$CONSUME_TIMEOUT" --jq "$JQ_PROJECT" 2>>"$STATE_DIR/consume.err")
  rc=$?
  log "consume 本轮结束（rc=$rc），${BACKOFF_SECS}s 后重启"
  sleep "$BACKOFF_SECS"
done

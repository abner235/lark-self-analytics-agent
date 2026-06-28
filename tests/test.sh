#!/usr/bin/env bash
# bi-bridge 回归测试（零外部依赖：stub 掉 lark-cli 和 claude，只用本机 bash/jq）。
# 覆盖：事件分发护栏（event_id 去重 / 群白名单 / @检测 / 发起人白名单）、成功回贴、
#       超时兜底、超时杀子进程不留孤儿。
# 用法： bash tests/test.sh   （全过返回 0，任一失败返回 1）
set -o pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$REPO/bi-bridge.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/bibridge-test.XXXXXX")"
mkdir -p "$TMP/bin"
trap 'rm -rf "$TMP"; pkill -f "sleep 4747" 2>/dev/null' EXIT

PASS=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

mk_lark_stub() { # $1=events-file 内容会被 event consume 原样吐出（已是 --jq 投影后的形态）
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/lark-cli" <<EOF
#!/usr/bin/env bash
if [[ "\$1 \$2" == "event consume" ]]; then
  cat "$1"
  sleep 1
  exit 0
fi
if [[ "\$1 \$2" == "im +messages-reply" ]]; then
  mid=""; mode=""; body=""; shift 2
  while [[ \$# -gt 0 ]]; do case "\$1" in
    --message-id) mid="\$2"; shift 2;; --text) mode=text; body="\$2"; shift 2;;
    --markdown) mode=markdown; body="\$2"; shift 2;; --file) mode=file; body="\$2"; shift 2;; *) shift;; esac; done
  echo "REPLY mid=\$mid mode=\$mode" >> "$TMP/replies.log"
  echo '{"ok":true,"data":{"message_id":"om_reply"}}'   # 模拟 lark-cli 成功信封(走 stdout)
  exit 0
fi
exit 0
EOF
  chmod +x "$TMP/bin/lark-cli"
}

run_bridge() { # 后台跑 bridge，$1=等待秒数
  ( cd "$REPO" && PATH="$TMP/bin:$PATH" bash "$BRIDGE" "$TMP/cfg.sh" >"$TMP/bridge.out" 2>&1 ) &
  BPID=$!
  disown "$BPID" 2>/dev/null || true   # 不让 shell 在被 kill 时打印 "Terminated"
  sleep "$1"
  kill -TERM "$BPID" 2>/dev/null; pkill -TERM -P "$BPID" 2>/dev/null
  sleep 1; pkill -P "$BPID" 2>/dev/null
}

base_cfg() {
  echo "你是测试 agent" > "$TMP/prompt.md"
  : > "$TMP/replies.log"
  rm -rf "$TMP/state"
  cat > "$TMP/cfg.sh" <<EOF
export BOT_NAME="TestBot"
export ANALYSIS_PROMPT_FILE="$TMP/prompt.md"
export STATE_DIR="$TMP/state"
export ALLOWED_GROUPS="oc_good"
export ALLOWED_SENDERS="ou_alice"
export WORKDIR="$TMP"
export CONSUME_TIMEOUT="1s"
export BACKOFF_SECS="30"
export CLAUDE_BIN="$TMP/bin/claude"
export LARK_BIN="lark-cli"
CLAUDE_ARGS=(--max-turns 3)
$1
EOF
}

# ---------------- 测试 A：分发护栏 + event_id 去重 + 成功回贴 ----------------
echo "[A] 分发护栏 / event_id 去重 / 成功回贴"
cat > "$TMP/bin/claude" <<'EOF'
#!/usr/bin/env bash
task=""; while [[ $# -gt 0 ]]; do [[ "$1" == "-p" ]] && { task="$2"; shift 2; continue; }; shift; done
echo "报告OK task=[$task]"
EOF
chmod +x "$TMP/bin/claude"
cat > "$TMP/events.ndjson" <<'EOF'
{"event_id":"e1","message_id":"om_1","chat_id":"oc_good","sender_id":"ou_alice","content":"@TestBot 分析下转化率"}
{"event_id":"e1","message_id":"om_1","chat_id":"oc_good","sender_id":"ou_alice","content":"@TestBot 分析下转化率"}
{"event_id":"e2","message_id":"om_2","chat_id":"oc_BAD","sender_id":"ou_alice","content":"@TestBot 非白名单群"}
{"event_id":"e3","message_id":"om_3","chat_id":"oc_good","sender_id":"ou_alice","content":"没提及机器人的闲聊"}
{"event_id":"e4","message_id":"om_4","chat_id":"oc_good","sender_id":"ou_stranger","content":"@TestBot 陌生人触发"}
EOF
mk_lark_stub "$TMP/events.ndjson"
base_cfg ""
run_bridge 4

acks=$(grep -c 'mode=text'     "$TMP/replies.log" 2>/dev/null); acks=${acks:-0}
reps=$(grep -c 'mode=markdown' "$TMP/replies.log" 2>/dev/null); reps=${reps:-0}
oks=$(grep -c '"status":"ok"'  "$TMP/state/invocations.jsonl" 2>/dev/null); oks=${oks:-0}
[ "$acks" = "1" ] && ok "仅 1 条 ack（去重+护栏生效）" || bad "ack 应为1，实际 $acks"
[ "$reps" = "1" ] && ok "仅 1 条报告回贴"               || bad "报告应为1，实际 $reps"
[ "$oks"  = "1" ] && ok "审计仅 1 条 ok"                || bad "审计ok应为1，实际 $oks"
grep -q 'om_2\|om_3\|om_4' "$TMP/replies.log" 2>/dev/null && bad "有被护栏拦截的消息漏放行" || ok "错误群/无@/陌生人均被拦截"

# ---------------- 测试 B：超时兜底 + 杀子进程不留孤儿 ----------------
echo "[B] 超时兜底 / 进程组清理"
cat > "$TMP/bin/claude" <<'EOF'
#!/usr/bin/env bash
sleep 4747 &     # 模拟取数子进程（孙子）
sleep 4747       # claude 自身 hang
EOF
chmod +x "$TMP/bin/claude"
cat > "$TMP/events.ndjson" <<'EOF'
{"event_id":"t1","message_id":"om_t","chat_id":"oc_good","sender_id":"ou_alice","content":"@TestBot 慢任务"}
EOF
mk_lark_stub "$TMP/events.ndjson"
base_cfg 'export TASK_TIMEOUT_SECS=2'
run_bridge 7

grep -q '"status":"timeout"' "$TMP/state/invocations.jsonl" 2>/dev/null && ok "超时被记为 timeout" || bad "未记录 timeout"
grep -q 'mode=text' "$TMP/replies.log" 2>/dev/null && ok "回贴了超时提示" || bad "未回贴超时提示"
if pgrep -f "sleep 4747" >/dev/null 2>&1; then bad "残留孤儿子进程"; pkill -f "sleep 4747"; else ok "无残留孤儿进程"; fi

echo
echo "==== 结果：PASS=$PASS  FAIL=$FAIL ===="
[ "$FAIL" = "0" ]

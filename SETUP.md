# bi-bridge 安装与联调（V1 最小闭环）

> 目标：在**你自己**的机器上，让飞书群里 `@你的机器人 + 一句话` → 触发本机 headless 分析 Agent（调你本人的 mc-query/datawind 取数）→ 报告回贴到群。
> 前提：本机已装并能用 `lark-cli`、`claude`、`jq`，以及你本人的 `mc-query` / `datawind-dashboard-skill`（cookie 已登录有效）。

整套就一个常驻脚本 `bi-bridge.sh`，其余是配置。按顺序做：

---

## 第 1 步：建一个飞书机器人（应用）
1. 飞书开发者后台 → 创建企业自建应用（每个分析师一个，名字带本人标识，如 `BI-<你的名>`）。
2. 开通 bot scope（`--as bot` 不需要 auth login，只要后台开 scope）：
   - 收消息事件：`im:message`（接收 `im.message.receive_v1`）
   - 发消息：`im:message:send`（回贴）
   - 上传资源（长报告转文件）：`im:resource`
   - 查群（找 chat_id 用）：`im:chat:read`
3. 把机器人**拉进**要服务的 BI 群（机器人不在群里收不到群消息）。
4. 记下应用的 appId / appSecret。

> ⚠️ 机器人的「可用范围/可见性」要覆盖会 @ 它的同事，否则取不到发送者信息。

---

## 第 2 步：把这个机器人绑成本机 lark-cli 的一个 profile
- 关键事实：**一个 lark-cli profile = 一个 bot appId，同一个 bot 的事件只投递给一个消费进程**。所以「一人一机器人、各自本机消费」是唯一干净模型。
- 用 `lark-cli config init` / `lark-cli config --help` 把本机器人的 appId/appSecret 配进来。
- **确认 profile 选择语法**：跑 `lark-cli config --help`，看你的版本是用独立 config 文件路径还是 profile 名来区分多机器人，然后把对应参数填进 config 的 `LARK_EXTRA`（如 `--config ~/.lark/bi-yourname.json` 或 `--profile bi-yourname`）。脚本会把 `LARK_EXTRA` 透传给每条 lark-cli 命令。
- 验证身份：`lark-cli im +chat-list --as bot $LARK_EXTRA` 能列出机器人所在的群即可。

---

## 第 3 步：写受限分析指令 + 手动验证「单点能答好」（真正的 gate）
这一步过不了就别往下做管道——bridge 只是管道，报告质量才是成败。
1. 看 `analysis-agent-prompt.md`，按你的业务/口径微调（引用你们的 L1 metric id、dashboard 名等）。
2. 在 `WORKDIR` 下手动跑一次 headless，确认能取到数、报告过关（下面在仓库根目录执行）：
   ```bash
   claude -p "帮我分析下 Banxa 业务昨天的转化率为什么下降？" \
     --append-system-prompt "$(cat ./analysis-agent-prompt.md)" \
     --allowedTools "Skill Read Grep Glob Bash" --max-turns 30
   ```
3. 看它是否真的调了 mc-query/datawind、数对不对、报告你自己会不会点头。**不满意先改 prompt / skill，别急着上 bridge。**

---

## 第 4 步：配置并启动 bridge
在仓库根目录执行：
```bash
cp config.example.sh config.me.sh
vim config.me.sh           # 填 BOT_NAME / ALLOWED_GROUPS(chat_id) / LARK_EXTRA / OWNER_AT 等
# 找 chat_id：lark-cli im +chat-search --query "你的BI群名" --as bot $LARK_EXTRA
# 找 open_id：lark-cli contact +search-user --query "姓名" --as user

chmod +x bi-bridge.sh
./bi-bridge.sh ./config.me.sh             # 前台先跑，看日志（stderr）
```
在测试群 `@你的机器人 分析下昨天 Banxa 转化率` → 应看到「收到，开始分析…」→ 稍后收到报告。

---

## 第 5 步：保活（launchd）
前台验证 OK 后转后台常驻：
1. 编辑 `com.bi-bridge.plist`：把 `__ANALYST__` 改成你的名、`__REPO__` 改成仓库根的绝对路径、`PATH` 改成能找到 `lark-cli/claude/jq` 的路径。
2. 安装：
   ```bash
   cp com.bi-bridge.plist ~/Library/LaunchAgents/com.bi-bridge.me.plist
   launchctl load ~/Library/LaunchAgents/com.bi-bridge.me.plist
   ```
3. 停服（**优雅 SIGTERM，不会 kill -9，订阅不泄漏**）：
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.bi-bridge.me.plist
   ```

> 在意「常开」的人：把这套跑在你自己的一台常开小机器 / 云 VM 上（**仍是你本人的凭证、你本人的数据域**），既保留去中心化权限隔离，又拿到 uptime。

---

## 第 6 步（可选）：调用记录同步进 Lark Base 看板
脚本默认把每次调用写进本机 `$STATE_DIR/invocations.jsonl`（永久审计真相源）。
要同步进 P0 的 `usage_leaderboard` 看板：
1. `lark-cli base --help` 确认 record 写入命令的真实语法。
2. 把命令填进 `bi-bridge.sh` 的 `log_to_base()`（有占位注释指明位置）。
3. config 里设 `LARK_BASE_APP_TOKEN` / `LARK_BASE_TABLE_ID`。

---

## 验证清单（对应 plan）
- [ ] **正路**：@机器人 → ack → 收到结构化报告。
- [ ] **取数过期**：让 mc-query/datawind cookie 失效 → 回贴明确报错 + @owner，不卡死不静默。
- [ ] **白名单**：非白名单群 / 非白名单人触发 → 无响应。
- [ ] **护栏**：连发多条 → 并发不超上限、超时能终止、日配额到顶提示。
- [ ] **去重**：同一条消息重投 → 只跑一次。
- [ ] **记账**：`invocations.jsonl` 每次落一行。
- [ ] **飞轮判据**：出现第一条「触发人 ≠ 机器人 owner」的成功调用 = 互联咬合一齿。

---

## 已知边界（V1 诚实标注）
- **@检测靠机器人显示名字符串匹配**（飞书把 @提及渲染成显示名，不是结构化 tag）。若机器人名恰好出现在普通文本里会误触发；V1 可接受，V2 改结构化判定。
- **笔记本休眠/关机 = 离线**：靠优雅报错 + 自选常开 host 化解，不靠中心化。
- **这是 V1**：人工 @ 指定机器人，无自动路由、无 Agent 自主互调。那是 V2。

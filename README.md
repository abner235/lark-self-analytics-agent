# lark-self-analytics-agent (bi-bridge)

> 飞书机器人当总线，把"群里 @ 一句话"变成一次本机自助数据分析：去中心化、各用本人凭证、headless Agent 取数出洞察。

群里 `@你的机器人 + 一句话分析问题` → 触发本机 headless 分析 Agent（调你本人的
mc-query / datawind 取数）→ 报告回贴到群。

> 许可证：[MIT](LICENSE)。

```
飞书群 @个人机器人 ──im.message.receive_v1──▶ lark-cli event consume
                                                      │ stdout(NDJSON)
                                                      ▼
                                                 bi-bridge.sh
                                  过滤(群白名单/@/发起人/去重) → ack →
                                  claude -p(受限工具 + 分析指令, 调 mc-query/datawind) →
                                  lark-cli im +messages-reply(报告) → 本地审计记账
```

## 文件
| 文件 | 作用 |
|---|---|
| `bi-bridge.sh` | 守护进程主体（消费循环 + 护栏 + 触发 headless + 回贴 + 记账） |
| `config.example.sh` | 配置模板，复制成 `config.<你的名>.sh` 填写后 source |
| `analysis-agent-prompt.md` | headless 调用注入的受限分析指令 |
| `com.bi-bridge.plist` | macOS launchd 保活（SIGTERM 优雅停，绝不 kill -9） |
| `SETUP.md` | 从建机器人到联调的逐步指南 + 验证清单 |
| `tests/test.sh` | 零依赖回归测试（stub 掉 lark-cli/claude）：护栏 + 去重 + 超时 + 进程清理 |

## 测试
```bash
bash tests/test.sh    # 全过返回 0；改 bi-bridge.sh 后跑一遍防回归
```

## 核心设计（详见 plan）
- **去中心化**：每人在本人机器跑自己的实例，用本人凭证 → 每个 Agent 只看本人有权数据，合规边界=个人边界。
- **护栏优先**：群/发起人白名单、@检测、去重、并发上限、单任务超时、日配额、工具收口、cookie 过期优雅降级、全程留痕。
- **V1 = 最小闭环**：人工 @ 指定机器人，无自动路由、无 Agent 自主互调（那是 V2）。

## 快速开始
见 [`SETUP.md`](SETUP.md)。先过第 3 步的「单点能答好」gate，再上管道。

## 依赖
`lark-cli`（含 lark-event/lark-im skill）、`claude`、`jq`，以及本人已登录的 `mc-query` / `datawind-dashboard-skill`。

## Sandbox 演示（无需真实数据系统即可跑通）
[`sandbox/`](sandbox/) 是一个自包含的端到端演示：用 sqlite 造一份 Banxa 转化漏斗数据（昨天埋了可归因下跌），`mcq` 模拟 mc-query 取数，`mc-query-sandbox` skill 包装。
```bash
cd sandbox && python3 build_db.py          # 造数
cp config.sandbox.example.sh config.sandbox.sh && vim config.sandbox.sh   # 填你的 bot/群/open_id
cd .. && ./bi-bridge.sh ./sandbox/config.sandbox.sh                       # 启动后群里 @ 机器人
```

## 安全 / 敏感信息
- **真实配置不入库**：`config.*.sh`（含真实 chat_id / open_id）已被 `.gitignore`，只提交 `*.example.sh` 占位模板。
- **密钥不在本仓库**：bot 的 appId/appSecret、token 由 `lark-cli` 存在 `~/.lark-cli/`，与本项目分离。
- 克隆后请自行 `cp *.example.sh` 并填入你自己的值。

# ADR-0001: Shell 守护进程架构

**状态**: Implemented
**日期**: 2026-06-29

## Context

bi-bridge 需要一个常驻进程，消费飞书群的 @机器人事件，触发本机 headless Claude Agent 完成数据分析，再把报告回贴到群里。

约束条件：

1. **去中心化部署**：每个分析师在自己的 Mac 上跑自己的实例，用自己的 lark-cli config、mc-query cookie 和 datawind cookie。凭证边界 = 个人边界，不需要中心化的凭证管理。
2. **零公共基础设施**：团队没有共享服务器、没有 k8s、没有 CI/CD。部署目标是 `launchd` 拉起一个脚本。
3. **依赖链要短**：lark-cli、claude、jq 是已有工具，不应该为了一个消费循环引入 Python 虚拟环境或 Node 运行时。
4. **macOS 兼容**：stock macOS 自带 bash 3.2，不能依赖 bash 4+ 特性（如关联数组）。脚本刻意不开 `set -u`，因为 bash 3.2 对空数组误报 unbound variable。

## Decision

用单个 bash 脚本 `bi-bridge.sh` 实现守护进程主体，通过 `source` 一个 per-analyst 的 config shell 文件加载配置。

具体实现：

**消费循环**：外层 `while true` 启动 `lark-cli event consume`，consume 按 `--timeout` 自然退出后重启。脚本从不主动 kill consume 进程（遵守 lark-event skill 的契约：kill -9 会泄漏服务端订阅）。consume 的 stdout 经 `--jq` 投影后逐行喂给 `handle_event`。

**护栏层**（全部在 shell 里实现）：

- 群白名单 + 发起人白名单（`in_list` 子串匹配）
- @检测：优先用 `MENTION_MATCH` 正则（grep -iE），否则退回 `BOT_NAME` 子串匹配
- event_id 文件去重（`$STATE_DIR/seen/` 目录，启动时按 TTL 清理旧文件）
- 并发上限（`$STATE_DIR/running/` 目录下的文件计数）
- 日配额（`$STATE_DIR/counter.YYYY-MM-DD` 文件）
- 单任务超时：优先用 coreutils `timeout`/`gtimeout`，fallback 到纯 bash 实现的 `run_with_timeout`（开 job control，超时杀整个进程组，防子进程泄漏）

**工具收口**：`CLAUDE_ARGS` 必须是 bash 数组（不是字符串），通过 `--allowedTools` 限制 headless Agent 能调用的工具前缀。默认 fail-closed：不给 Bash，由 config 显式授权取数命令前缀（如 `Bash(mc-query:*)`），防止 prompt 注入获取任意 shell。

**配置**：per-analyst 的 `.sh` 文件被 `source`，所有参数用 bash 变量 + 合理默认值。零依赖，不需要 yq 或其他 YAML 解析器。

**自动重试**：分析失败后把错误信息追加到 prompt，让 Agent 自动修正重试（最多 `MAX_RETRY` 次）。

## Consequences

### Positive
- 零额外依赖：只需 bash、jq、lark-cli、claude，所有 Mac 已有或已装
- 部署 = 复制一个脚本 + 填一份 config + 挂 launchd plist，5 分钟内完成
- 去中心化天然解决了凭证管理：每个人用自己的 cookie，不需要凭证共享或轮换机制
- 护栏逻辑透明：白名单、去重、配额全是文件系统操作，出问题直接 ls/cat 调试
- 纯文本审计日志（invocations.jsonl）可以直接用 jq 查询，不依赖外部存储

### Negative
- bash 的错误处理能力有限：没有结构化异常，错误传播靠退出码和约定
- 并发控制是粗粒度的：文件计数存在 TOCTOU 窗口（代码里用"主循环同步占位 + 后台执行"减轻，但没有完全消除）
- 测试需要 stub 外部命令（tests/test.sh 用 PATH 劫持实现），不如 Python 的 mock 灵活
- bash 3.2 限制了可用的语言特性（不能用关联数组、nameref 等）
- 脚本超过 350 行后可读性下降，新功能添加的认知负担递增

## Alternatives Considered

| 方案 | 优点 | 淘汰原因 |
|------|------|---------|
| Python 服务（FastAPI/Flask） | 结构化错误处理、丰富的库生态、更好的测试框架 | 引入 venv/pip 依赖管理，每个分析师的 Mac 上 Python 版本不一致是常见问题，部署步骤从 5 分钟变成调环境 |
| Node.js 服务 | 异步 I/O 天然适合事件消费、npm 生态 | 同样的依赖管理问题，且团队不写 JS |
| Python 脚本（非服务，类似当前 bash 的角色） | 比 bash 更好的字符串处理和错误处理 | 核心逻辑是"调 lark-cli 收事件 → 调 claude 跑分析 → 调 lark-cli 回贴"，全是命令行管道编排，bash 是这类任务的原生语言；Python 做同样的事要 subprocess 包一层，代码量不会更少 |
| 中心化服务（一个进程代替所有人跑） | 运维集中、状态统一 | 需要中心化管理所有人的凭证（mc-query cookie、lark-cli token），合规风险高；单点故障影响所有人；需要服务器基础设施 |

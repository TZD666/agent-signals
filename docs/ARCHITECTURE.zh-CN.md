# Agent Signals

本地 Claude Code / Codex 状态呼吸灯。页面读取本机状态，不修改现有 hooks 或会话文件。

双击 `run.command` 启动，Mac 上访问 `http://127.0.0.1:8812`。同一 Wi-Fi 下的 iPad 使用 `http://<Mac局域网IP>:8812`。

## 状态映射

- 白：空闲
- 蓝：思考或执行命令
- 绿：已完成（点击进入对应任务确认后消失）
- 琥珀：需要输入或批准
- 紫：疑似卡死
- 红：异常退出

点击主灯会在 Mac 上打开对应的 Terminal 标签页或 Codex 任务，并由本地服务确认这次完成状态。子代理显示为主灯外围的小卫星点。

## 疑似卡死怎么判定

只靠"思考了很久"会把正常的长构建误判成卡死，所以要求**时长超阈值**且**完全没有活动信号**两个条件同时成立：

| 平台 | 持续时长 | 活动信号 |
| --- | --- | --- |
| Claude | `statusUpdatedAt` 到现在（该字段在状态不变期间冻结，等于连续 thinking 时长） | 进程树累计 CPU 增量 > `CPU_EPSILON`，或 `~/.claude/projects/*/<sessionId>.jsonl` 有新写入 |
| Codex | rollout 文件最后写入到现在 | rollout 文件 mtime 变化 |

蓝灯卡片会显示"已持续 N 分钟"，让健康的长任务无需判断即可读；紫灯显示"已 N 分无新事件"。

## 负载指示

每张主卡片在状态下方有一条细负载条 + 标签，例如 `67% · 174k/258k · ~6分/步`：

- **上下文重量**：当前上下文 token / 模型窗口。Codex 直接读 rollout 里 `token_count` 事件的 `last_token_usage.input_tokens` 与 `model_context_window`；Claude 读 transcript 最新一条 assistant 消息的 `input + cache_read + cache_creation`，窗口大小 transcript 不提供，用 `AGENT_SIGNALS_CLAUDE_CONTEXT_WINDOW` 指定。
- **步间隔**：最近几步（≤10 个间隔，剔除超过 30 分钟的用户暂停）的平均间隔。重会话一步 5-7 分钟，这个数字就是"它为什么慢"的直接答案。
- 占比 ≥80% 时负载条转琥珀色强调（仅变色，不发通知）。
- 读不到 token 数据时画斜纹条并显示 `—`，绝不显示成 0%（同数据源健康度的原则）。
- 卫星（子代理）不显示负载，保持轻量。

## 费用与历史（¢ 按钮）

顶栏 ¢ 按钮展开「费用与历史」面板：正在运行的会话实时显示估算金额，下方是近期会话列表（启动时间、时长、轮数、token 分类、上下文峰值、金额），点击一行展开每轮交互时间线。主界面与 `/api/agents` 的 304 契约完全不受影响——面板走独立的只读端点 `/api/history` 与 `/api/history/session`，仅在展开时拉取。

- **只记数字，不记内容**：后台每 60 秒按字节偏移增量续读 Claude transcript（含 Task 子代理文件）与 Codex rollout，把时间戳、模型、推理强度、token 用量写进 `agent-history.db`（sqlite WAL）。对话内容一个字不进库（有哨兵测试保证）。
- **金额是估算口径，不落库**：读取时按当前价格表现算。Claude 价格来自 tokenusage(8899) 的 LiteLLM 镜像 `prices.json`（缺失时退内置兜底表），显示为「等效API标价」——订阅用户实际没花这笔钱；Codex/GPT 价格来自 `codex_prices.json`（首次启动播种 GPT-5 公开标价，可手工编辑，改完即生效），一律标「估算」。未收录的模型金额显示 `—`，绝不编造。
- 推理强度只作为字段记录，不进价格公式：强度只放大输出 token 量，不改单价（thinking/reasoning token 已计入 output）。
- 计价口径差异：Codex 的 `input_tokens` 已含 `cached_input_tokens`，计价先减；Claude 四类 token（输入 / 输出 / 缓存读 / 缓存写 5m·1h）分开计价，1h 缓存写按 2× input。
- 已知限制：`--resume` 会话会把旧轮次记进新会话——单会话视图诚实，跨会话求和会重复计入。

`agent-history.db` 与 `codex_prices.json` 落在服务运行目录（launchd 部署即 `~/Library/Application Support/AgentSignals/`），重启与重新部署（只 cp `server.py` + `static/*`）都不会清掉它们。

## 数据源健康度

面板绝不把"读不到数据"显示成"没有任务"。顶部有一条数据源状态，取值 `正常 / 数据源不可用 / 数据结构已变`；某个源不可用时，该区域渲染灰斜纹提示并写明原因，而不是空态。

Codex 数据库不再硬编码版本号：默认在 `~/.codex/` 下挑版本号最大的 `state_*.sqlite`，并校验 `threads` 表字段；字段对不上时报 `数据结构已变` 而不是静默返回空。载荷带 `schemaVersion`，前端遇到不认识的版本会停止渲染并显示横幅，而不是猜。

Codex 写端不在线且 WAL 上次未干净收尾时，纯只读打开会失败（读端建不了 `-shm`，报 `unable to open database file`；2026-08-18 ChatGPT 桌面端升级中断写端时实测踩到）。面板会自动退回 `immutable` 模式读取——此时写端必然离线，不存在脏读；写端回来后 `-shm` 重建，主路径自动恢复。

## 通知

状态进入"需要输入 / 疑似卡死 / 已完成"时：

- **Mac 本机通知**（osascript），每个会话 60 秒内最多一条，全局每分钟最多 10 条。服务刚启动的第一轮采样只登记现状、不发通知，避免开机刷屏。
- **页面内提醒**：提示音 + 标题角标 `(1) 需要输入` + 卡片闪亮。浏览器要求先有一次用户手势才允许播声音，所以首次需要在页面上点一下，未点之前顶部会提示。

首次触发时 macOS 会弹授权框，**需要手动允许一次**。注意：如果授权被拒，系统会静默丢弃通知，`osascript` 仍然返回成功——这种情况程序无法自证，面板只能在 osascript 本身报错时显示"Mac 通知发送失败"。所以请在第一次通知时用眼睛确认一下。

不需要通知就设 `AGENT_SIGNALS_NOTIFY=0`。

## 加到 iPad 主屏

Safari 打开面板 → 分享 → 添加到主屏幕。之后从主屏图标进入是全屏独立窗口，没有地址栏。

iPad 锁屏或切到后台时收不到推送：Web Push 要求安全上下文（HTTPS），局域网 HTTP 地址不满足。后台提醒走的是 Mac 本机通知。

## 省电

- 浏览器用长轮询 `?wait=8` + ETag，状态没变服务端回 304 空响应。
- 页面切到后台自动停止轮询，切回来立刻拉一次。
- rollout 文件按 `(mtime, size)` 缓存解析结果，不变就不重读。实测冷启读 3.0 MB，随后每轮 0 MB（改造前是每 2 秒读满 3.0 MB）。Claude transcript 的负载解析同样按 `(mtime, size)` 缓存。
- 负载值在服务端量化（token 取整千、百分比取整、步间隔按 15s/60s 分桶，全部整数），空闲会话的载荷字节不变，304 契约不受影响。

## 可调环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SIGNALS_STALL_AFTER_MS` | `180000` | 判定疑似卡死的时长阈值 |
| `AGENT_SIGNALS_CPU_EPSILON` | `0.05` | 认定"有 CPU 活动"的秒数下限 |
| `AGENT_SIGNALS_SAMPLE_INTERVAL_MS` | `5000` | 后台采样间隔 |
| `AGENT_SIGNALS_NOTIFY` | `1` | 设为 `0` 关闭 Mac 通知 |
| `AGENT_SIGNALS_NOTIFY_THROTTLE_MS` | `60000` | 同一会话通知最小间隔 |
| `AGENT_SIGNALS_NOTIFY_MAX_PER_MINUTE` | `10` | 全局每分钟通知上限 |
| `CODEX_DB_PATH` | 自动发现 | 指定 Codex sqlite，跳过版本探测 |

Codex 数据库会同时从 `~/.codex/` 与 `~/.codex/sqlite/` 探测，并优先使用版本更高、更新时间更新的文件。`threads` 表的新增或可选字段缺失会自动兼容；只有核心身份、路径或活动时间字段全部不可用时才会停止该数据源，并显示具体缺失字段。
| `CLAUDE_SESSIONS_DIR` | `~/.claude/sessions` | Claude 会话目录 |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude transcript 目录 |
| `AGENT_SIGNALS_CLAUDE_CONTEXT_WINDOW` | `200000` | Claude transcript 不含窗口大小，负载条分母用它 |
| `AGENT_SIGNALS_HISTORY` | `1` | 设为 `0` 完全关闭历史埋点与费用估算 |
| `AGENT_SIGNALS_HISTORY_DB` | 运行目录/`agent-history.db` | 历史库路径 |
| `AGENT_SIGNALS_CODEX_PRICES` | 运行目录/`codex_prices.json` | Codex 估算价格文件（可手工编辑） |
| `AGENT_SIGNALS_CLAUDE_PRICES` | tokenusage 的 `prices.json` | Claude 价格镜像路径 |
| `AGENT_SIGNALS_HISTORY_BACKFILL_DAYS` | `30` | 历史回填窗口（天） |

## 其他

Codex 空闲任务可用右上角的 × 手动隐藏，并会在空闲 24 小时后自动隐藏；点击左上角的 🔒 锁定后可持续保留。隐藏与锁定状态保存在当前浏览器中。

页面右上角可切换暗色与亮色模式，主题选择同样会保存在当前浏览器中。

## 安全现状

服务绑定 `0.0.0.0` 且**没有鉴权**：同一 Wi-Fi 下任何人都能调用 `/api/open`，从而在这台 Mac 上触发 AppleScript 切换窗口。当前接口都是只读或"打开窗口"级别，风险可控；**若以后新增任何操作类接口（批准、发送提示等），必须先补上配对鉴权**。另注意 `/api/history` 会把会话名、目录名与金额估算暴露给同一 Wi-Fi——仍是只读，但敏感度比状态灯高一档。

## 测试

```
python3 -m unittest discover tests -v
```

## 目录

```
server.py                 状态采集 + HTTP 服务 + 通知 + 历史埋点/费用估算
static/index.html         页面骨架
static/app.js             渲染、长轮询、提示音、费用与历史面板
static/styles.css         主题与状态样式
static/icon-180.png       主屏图标
static/manifest.webmanifest
tests/test_server.py      呼吸灯主功能测试
tests/test_history.py     历史埋点与费用估算测试
run.command               双击启动
```

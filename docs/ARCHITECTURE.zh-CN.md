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

点击主灯会在 Mac 上打开对应的 Terminal 标签页、Claude 桌面 App 或 Codex 任务，并由本地服务确认这次完成状态。子代理显示为主灯外围的小卫星点。

## 会话种类

面板不再假设"Claude 会话＝一个 Terminal 标签页"。每盏灯按登记表里的 `entrypoint` 标出自己的来路：

| entrypoint | 灯上显示 |
| --- | --- |
| `cli` / 字段缺失 | Terminal |
| `claude-desktop` | 桌面 App |
| `claude-vscode` | VS Code |
| `sdk-cli` | claude -p 后台 |
| `sdk-ts` / `sdk-py` | SDK |
| `mcp` | MCP |
| `local-agent` / `local_agent` | 桌面 Cowork |

认不出的入口原样显示，不假装是终端。老登记表不写 `entrypoint`，这时靠命令行里的 `Application Support/Claude/claude-code/` 认出桌面 App 自带的那个二进制。

桌面 App 的会话还会和它自己的会话索引对一次（`~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json`，按 `cliSessionId` 关联，60 秒缓存，坏文件跳过）：索引里的标题盖掉自动生成的会话名（自己起的名字不动），点灯打开的是 Claude.app 而不是 Terminal 标签页。

`claude -p` 和桌面 App 的登记表都不写 `status`（已在本机实测），照原来的读法它们永远是一盏假的白灯。这两种会话改看活动信号：`AGENT_SIGNALS_CLAUDE_HEADLESS_ACTIVE_MS`（默认 30 秒）内还有 CPU 增量或 transcript 写入就算思考中，静下来即转已完成；同样缺状态时间戳，时间优先取桌面索引的 `lastActivityAt`，没有索引才退回 `startedAt`。

「状态要自己推」和「点不开」是两件事：桌面 App 的灯点一下会把 Claude.app 拉到前台，只有真正的 `claude -p` 没有可切过去的界面。打不开的灯转绿之后仍然可以点一下——服务不打开任何窗口，只把这次完成确认掉，否则它会一直绿在那儿。

## 卫星

主灯外围的小卫星点有两种来路：

- **登记表卫星**：`claude --bg` 后台会话，以及被某个会话拉起来的 `claude -p`。挂靠改成沿 PPID 链上溯（≤8 层）找真正的父会话，找不到才退回原来的"同目录 / 最近活跃"猜测——之前同目录的另一盏灯会把它抢走。
- **子代理卫星**：Task 子代理（`<transcript 目录>/<sessionId>/subagents/agent-*.jsonl`）。它没有结束标记，只能看 jsonl 的 mtime：`AGENT_SIGNALS_SUBAGENT_ACTIVE_MS`（60 秒）内有写入算思考中，之后转已完成，静默超过 `AGENT_SIGNALS_SUBAGENT_LINGER_MS`（10 分钟）不再显示。只 stat 文件加读同名的小 `.meta.json`，一个字对话内容都不读；这类卫星只作展示，不参与完成确认。

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

`agent-history.db` 与 `codex_prices.json` 落在服务运行目录（launchd 部署即 `~/Library/Application Support/AgentSignals/`），重启与重新部署都不会清掉它们。

## 部署

部署只走 `./deploy.sh`，不要手动 cp：

```
./deploy.sh
```

四步固定顺序：跑测试（用 launchd 里那个 `/usr/bin/python3`）→ 拷 `server.py` 与 `static/*` 到运行目录（`discovery.py`、`cloud.py` 存在才拷，它们是后续阶段才出现的模块）→ `launchctl kickstart -k` 重启服务 → 轮询 `/health` 直到 `version` 与 `server.py` 里的 `APP_VERSION` 对上、**并且** `pid` 与重启前不同（20 秒超时，超时会打印 `agent-signals.err.log` 末尾并非零退出）。测试不过就一个文件都不拷。

比 pid 是必要的：`APP_VERSION` 在同一阶段内是不变的手写常量，只比版本号的话，端口被别的野进程占着、新进程根本没起来时，老进程会用同一个版本号把健康检查骗过去。

`agent-history.db`、`codex_prices.json`、`.agent-signals-state.json`、`runtime-profiles.json` 是运行时数据，脚本永远不覆盖它们。运行目录默认 `~/Library/Application Support/AgentSignals`，可用 `AGENT_SIGNALS_DEPLOY_DIR` 覆盖。

## 平台分区与载荷（schemaVersion 2）

`/api/agents` 的载荷不再让每个数据源占一个根键（老的 `claude` / `codex` 两个数组已删除），改成一个 `platforms` 列表：

```
{
  "schemaVersion": 2, "generatedAt": …, "version": "2.2.0",
  "sources":       { "<源 key>": {state, detail} },      // 采集端健康度，按来源
  "notifications": {state, detail},
  "platforms": [
    { "key", "label", "order", "kind", "hint",
      "dismissible", "lockable", "emptyText",
      "health": {state, detail},
      "agents": [ …这个平台的灯… ] }
  ],
  "counts": { "agents": N, "satellites": M, "byPlatform": {"<平台 key>": n} }
}
```

分区按 **`agent["platform"]` 归堆**，不是按来源切：一个来源可以交出好几个平台的灯（自动发现的运行时就挂在采到它的那个源下）。排序按 `(order, key)`。每个分区的展示元数据来自 `platform_meta()`——先问原生 `SOURCES`（label / order / kind / hint / dismissible / lockable / empty_text 都在 `SourceSpec` 上），再问 `FAMILY_META`（自动发现的家族表，Phase 3 先留空），最后兜底成 `{label: key 首字母大写, order: 90, kind: "discovered"}`。原生平台**即使一盏灯都没有也会出现**，否则它的空态与「数据源不可用」没有地方渲染；非原生平台只在有灯时出现，健康度跟着产出它的那个来源走。源 key 与载荷根键撞名会在注册表建好的当场抛错（`check_source_keys`）。

`/api/open` 的平台白名单也跟着放宽：只要平台出现在最近一轮载荷里就受理；没有登记 `open` 回调的平台（自动发现的那些）不打开任何窗口，只走确认分支，返回 `{"ok": true, "opened": false, …}`。

前端相应地不再把分区写死在 HTML 里：`static/index.html` 只留一个 `<div id="platforms">` 和一份 `<template id="platformSection">`，`app.js` 按 `data.platforms` 惰性克隆出分区、平台消失就整块移除，序号 `01/02/03…` 是排序后的位次。🔒 / × 两个按钮由 `lockable` / `dismissible` 决定，空态文案取 `emptyText`，历史面板的平台标签取 `label`。重建签名只包含 `{key, health, 剔掉 load 的可见灯}`，签名不变就只给负载条打补丁，卡片 DOM 不重建（轨道动画相位靠这个）。

隐藏与锁定的浏览器存储从写死平台的 `agent-signals.dismissed-codex` / `agent-signals.locked-codex` 改成一平台一桶的 `agent-signals.dismissed.<平台 key>` / `agent-signals.locked.<平台 key>`，老键在启动时搬进 `codex` 桶后删除（幂等）。

## 数据源健康度

面板绝不把"读不到数据"显示成"没有任务"。顶部有一条数据源状态，取值 `正常 / 数据源不可用 / 数据结构已变`；某个源不可用时，该区域渲染灰斜纹提示并写明原因，而不是空态。

Codex 数据库不再硬编码版本号：默认在 `~/.codex/` 下挑版本号最大的 `state_*.sqlite`，并校验 `threads` 表字段；字段对不上时报 `数据结构已变` 而不是静默返回空。载荷带 `schemaVersion`，前端遇到不认识的版本会停止渲染并显示横幅，而不是猜；横幅之外还会自动刷新一次页面（`sessionStorage` 守着，一个标签页最多一次），因为 iPad 主屏 PWA 会把旧 `app.js` 常驻，光有横幅等不来新脚本。

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
| `CLAUDE_DESKTOP_SUPPORT_DIR` | `~/Library/Application Support/Claude` | 桌面 App 的会话索引所在目录 |
| `AGENT_SIGNALS_CLAUDE_HEADLESS_ACTIVE_MS` | `30000` | headless 会话静默多久算做完 |
| `AGENT_SIGNALS_SUBAGENT_ACTIVE_MS` | `60000` | 子代理卫星静默多久算做完 |
| `AGENT_SIGNALS_SUBAGENT_LINGER_MS` | `600000` | 做完的子代理卫星还显示多久 |
| `AGENT_SIGNALS_HISTORY` | `1` | 设为 `0` 完全关闭历史埋点与费用估算 |
| `AGENT_SIGNALS_HISTORY_DB` | 运行目录/`agent-history.db` | 历史库路径 |
| `AGENT_SIGNALS_CODEX_PRICES` | 运行目录/`codex_prices.json` | Codex 估算价格文件（可手工编辑） |
| `AGENT_SIGNALS_CLAUDE_PRICES` | tokenusage 的 `prices.json` | Claude 价格镜像路径 |
| `AGENT_SIGNALS_HISTORY_BACKFILL_DAYS` | `30` | 历史回填窗口（天） |

## 其他

标了 `dismissible` 的平台（今天只有 Codex），空闲任务可用右上角的 × 手动隐藏，并会在空闲 24 小时后自动隐藏；标了 `lockable` 的平台，点击左上角的 🔒 锁定后可持续保留。隐藏与锁定状态按平台分桶保存在当前浏览器中。

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
deploy.sh                 部署到 launchd 运行目录（测试 → 拷贝 → 重启 → 验版本）
```

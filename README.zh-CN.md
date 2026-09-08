# Agent 干啥呢 · agent-signals

[![npm](https://img.shields.io/npm/v/agent-signals.svg)](https://www.npmjs.com/package/agent-signals)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

本地 Claude Code / Codex 状态呼吸灯：一眼看出手上的 agent 在思考、在等你、已经干完，还是悄悄卡死了。同一台 Mac 上跑着的其他 agent CLI 会从进程表里自动认出来，各自占一块分区，不用配置。

English: [README.md](README.md)

页面只读取本机状态，不修改现有 hooks、会话文件或 transcript，也不会把任何东西传出这台机器。

```
npx agent-signals
# → http://127.0.0.1:8812
```

把它常驻在手边的 iPad 上，扫一眼就知道 agent 还在不在干活，不用来回切终端窗口翻。

## 运行要求

| | |
| --- | --- |
| 操作系统 | macOS —— 面板要调用 `osascript`（通知、切窗口）和 BSD 版 `ps` |
| Python | 3.9 及以上。macOS 自带的 `/usr/bin/python3` 就够 |
| Node | 16 及以上 —— 只有走 npm 安装才需要；直接跑源码不需要 Node |
| Agent | Claude Code 和/或 Codex Desktop。只装一个也能用，另一个数据源会显示为不可用。别的 agent CLI 这边什么都不用装，见下面的「自动发现的运行时」 |

没有任何第三方 Python 依赖，`server.py` 只用标准库，所以 [`requirements.txt`](requirements.txt) 是刻意留空的。

## 安装

**不装直接跑：**

```bash
npx agent-signals
```

**全局安装：**

```bash
npm install -g agent-signals
agent-signals
```

**跑源码：**

```bash
git clone https://github.com/TZD666/agent-signals.git
cd agent-signals
python3 server.py
```

Mac 上也可以直接双击 clone 下来的 `run.command`。

Mac 本机访问 `http://127.0.0.1:8812`。同一 Wi-Fi 下的 iPad 用 `http://<Mac局域网IP>:8812`。

```bash
agent-signals --port 9000       # 换端口
agent-signals --host 127.0.0.1  # 只监听本机，不开放局域网
```

## 状态映射

| 颜色 | 含义 |
| --- | --- |
| 白 | 空闲 |
| 蓝 | 思考或执行命令 |
| 绿 | 已完成（点击进入对应任务确认后消失） |
| 琥珀 | 需要输入或批准 |
| 紫 | 疑似卡死 |
| 红 | 异常退出 |

点击主灯会在 Mac 上打开对应的 Terminal 标签页、桌面 App 或 Codex 任务，并由本地服务确认这次完成状态。子代理与 team 里排出去的 agent 显示为主灯外围的小卫星点，详见下面的「卫星」一节。

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

桌面 App 的会话还会和它自己的会话索引对一次（按 `cliSessionId` 关联）：索引里的标题盖掉自动生成的会话名（自己起的名字不动），点灯打开的是 Claude.app 而不是 Terminal 标签页。

`claude -p` 和桌面 App 的登记表都不写 `status`，这两种会话改看活动信号：`AGENT_SIGNALS_CLAUDE_HEADLESS_ACTIVE_MS`（默认 30 秒）内还有 CPU 增量或 transcript 写入就算思考中，静下来即转已完成。

「状态要自己推」和「点不开」是两件事：桌面 App 的灯点一下会把 Claude.app 拉到前台，只有真正的 `claude -p` 没有可切过去的界面。打不开的灯转绿之后仍然可以点一下——服务不打开任何窗口，只把这次完成确认掉，否则它会一直绿在那儿。

## 自动发现的运行时

面板不只认 Claude 与 Codex。每轮采样的那份 `ps` 表还会过一遍发现引擎（`discovery.py`），把本机正在跑、却没有原生数据源的 agent 运行时认出来，各自成一块分区。装上新的 agent CLI 跑起来它就出现，不用改任何配置。

一个进程有两条路变成一盏灯：

- **面板已经认识的家族**：种子表里 18 个运行时——Claude、Codex、DeepSeek Harness（`dsh`）、OpenClaw、Gemini CLI、OpenCode、Aider、Goose、Amp、Qwen Code、Kimi CLI、Copilot CLI、Cursor Agent、Hermes、Pi、Droid、Crush、WorkBuddy——按可执行文件名精确相等，或按包路径片段（`@openai/codex/`、`@deepseek-ai/dsh/`、`/Applications/WorkBuddy.app/`）匹配。"解释器 + 脚本"的命令行也算，而且不强求 `.js` / `.py` 后缀：npm 全局装的 CLI 就是一个没有扩展名的软链（`/opt/homebrew/bin/dsh` → `…/node_modules/@deepseek-ai/dsh/lib/bin.js`），所以比包路径之前先把软链解析掉（并缓存）。
- **谁都没教过它的家族**：命令行里出现 `~/.<name>/`、`~/.config/<name>/` 或 `~/Library/Application Support/<Name>/`，且那个目录看起来像某个 agent 的状态目录——含 `sessions/`、`threads/`、`conversations/`、`history.jsonl` 或 `rollout-*.jsonl`；或浅层的 sqlite 里有名字带 session / thread / conversation / run / message 的表；或 `settings.*` 旁边有个 `profiles/`——就现推一个以该目录命名的家族出来。`mkdir -p ~/.fakeagent/sessions` 再让任何进程从那儿跑起来，就能得到一块「Fakeagent」分区。

发现型的灯能说什么、不能说什么：

- 副标题写「自动发现 · <状态目录>」，下面还有一行小字「未登记进程」：这盏灯没有任何登记表背书。
- 没有负载条，一律画斜纹加 `—`。它背后没有 transcript 可读 token，编一个 0% 比什么都不显示更糟。
- 状态全靠活动信号推：进程树的 CPU 增量，或状态目录里有新的写入。`AGENT_SIGNALS_DISCOVERY_ACTIVE_MS`（默认 20 秒）内动过算思考中，静下来即转已完成。
- 活动信号必须反映「在干活」，不是「进程活着」——这条是踩出来的。GUI 类的 agent 应用每隔几秒就重写一次日志，登记表还按固定心跳重写，把这些当活动信号，灯就永远静不下来，绿灯在物理上不可能出现。所以住在 `.app` 包里的家族**只**盯画像里点名的那几个文件，而且**完全不看 CPU**（空转的 Electron 渲染进程有将近一半的采样窗口看起来像在忙）。代价是实打实的：画像里的文件挑错了，那盏灯在干活时也会一直白着。终端里跑的 agent 不受影响，照旧看 CPU。
- 发现型的灯只会是空闲 / 思考中 / 已完成三种。没有原生数据源就判不了"需要输入"与"疑似卡死"，所以一次都不会声称。只忙了一下下就完成的也不弹 Mac 通知，见 `AGENT_SIGNALS_DISCOVERY_NOTIFY_MIN_BUSY_MS`。

点灯打开什么，只由这个家族的本地画像决定：终端标签页、本机 Web 界面（只走回环——`127.0.0.1` 加一个数字端口，端口来自画像或监听探测，**永远不来自请求体**），或者按名字 `open -a` 一个 Mac App。住在 `.app` 包里的进程没有可切过去的终端标签页，直接标成打不开；和裸的 `claude -p` 一样，打不开的绿灯仍然可以点一下确认掉。

靠进程（而不是登记表）认出来的 Claude 会话——桌面 App 自带的那个二进制——标成「桌面 App」，点击打开 Claude.app。

不让它把整台机器点亮的几道栏杆：

- 只看当前用户自己的进程。系统路径（`/System/`、`/usr/libexec/`…）、Electron 的 `--type=` helper、以及一份写死的黑名单永远不出灯，面板也绝不点亮自己。
- 已经被原生源认领的一律跳过：Claude 登记表里的每个 pid 及其全部子孙，以及 Codex 灯所来自的 `ChatGPT.app` 那棵进程树。
- 进程至少要活过 `AGENT_SIGNALS_DISCOVERY_MIN_AGE_S`（5 秒）才出灯；新灯的头两轮采样只登记活动、状态一律按空闲算，否则每盏灯一出生就会先闪一次假的绿。
- 发现型的灯最多 `AGENT_SIGNALS_DISCOVERY_MAX_AGENTS`（24）盏，按启动时间最新优先，截断了几个会写在分区上。

有两条限制值得先说清楚。它找的是**进程**：装了但此刻没在跑的 agent 看不见——这是一块"现在有什么在跑"的面板，不是软件清单。另外，既不在种子表里、也不留下可辨认状态目录的运行时，在后续版本教会它之前根本发现不了。发现型的灯同样没有费用与历史，见下面的「费用与历史」。

设 `AGENT_SIGNALS_DISCOVERY=0` 可以整个关掉。

## 卫星

主灯外围的小卫星点有三种来路：

- **登记表卫星**：`claude --bg` 后台会话，以及被某个会话拉起来的 `claude -p`。挂靠沿进程树上溯（≤8 层）找真正的父会话，找不到才退回"同目录 / 最近活跃"的猜测——这个猜测可能被同目录下另一盏灯抢走。
- **team 卫星**：Claude Code 的 team 里排出去的 agent。它们在 `~/.claude/sessions/` 里只写 `.key` 不写 `.json`，登记表认领不到，只能由自动发现认出来。命令行里带 `--parent-session-id`，指向登记表里某个还活着的会话时，这个进程就挂成那盏灯的卫星，名字取自己的 `--agent-name`（`gh-search`、`exec-runway`，正是你在自己的 team 界面里看到的那个）。状态走自动发现那套活动信号，不读 transcript。父会话不在登记表里时它照旧单独出灯——找不到爹不是让一个真在跑的进程凭空消失的理由。
- **子代理卫星**：Task 子代理。它没有结束标记，只能看它那条 jsonl 文件的写入时间：`AGENT_SIGNALS_SUBAGENT_ACTIVE_MS`（默认 60 秒）内有写入算思考中，之后转已完成，静默超过 `AGENT_SIGNALS_SUBAGENT_LINGER_MS`（默认 10 分钟）不再显示。这类卫星只作展示，不参与完成确认，也不读一个字对话内容。

## 疑似卡死怎么判定

只靠"思考了很久"会把正常的长构建误判成卡死，所以要求**时长超阈值**且**完全没有活动信号**两个条件同时成立：

| 平台 | 持续时长 | 活动信号 |
| --- | --- | --- |
| Claude | `statusUpdatedAt` 到现在（该字段在状态不变期间冻结，等于连续 thinking 时长） | 进程树累计 CPU 增量 > `CPU_EPSILON`，或 `~/.claude/projects/*/<sessionId>.jsonl` 有新写入 |
| Codex | rollout 文件最后写入到现在 | rollout 文件 mtime 变化 |

蓝灯卡片会显示"已持续 N 分钟"，让健康的长任务无需判断即可读；紫灯显示"已 N 分无新事件"。

## 负载指示

每张主卡片在状态下方有一条细负载条 + 标签，例如 `67% · 174k/258k · ~6分/步`：

- **上下文重量**：当前上下文 token 占模型窗口的比例。Codex 直接读 rollout 里的 token 用量；Claude 读 transcript 最新一条消息的输入与缓存 token，窗口大小由 `AGENT_SIGNALS_CLAUDE_CONTEXT_WINDOW` 指定（transcript 本身不提供）。
- **步间隔**：最近几步（剔除超过 30 分钟的用户暂停）的平均间隔，直接回答"它为什么慢"。
- 占比 ≥80% 时负载条转琥珀色强调，只变色不发通知。
- 读不到 token 数据时画斜纹条并显示 `—`，绝不显示成 0%。
- 卫星不显示负载，保持轻量；发现型的灯同样没有负载条，它背后没有 transcript 可读。

## 费用与历史（¢ 按钮）

顶栏 ¢ 按钮展开「费用与历史」面板：正在运行的会话实时显示估算金额，下方是近期会话列表（启动时间、时长、轮数、token 分类、上下文峰值、金额），点击一行展开每轮交互时间线。

- **只记数字，不记内容**：后台每 60 秒增量读取 Claude transcript（含 Task 子代理文件）与 Codex rollout，把时间戳、模型、推理强度、token 用量写进 `agent-history.db`（sqlite）。对话内容一个字不进库。
- **金额是估算口径，不落库**：读取时按当前价格表现算。Claude 价格来自 tokenusage(8899) 的价格镜像（缺失时退内置兜底表），显示为「等效API标价」——订阅用户实际没花这笔钱；Codex/GPT 价格来自本地 `codex_prices.json`（首次启动播种公开标价，可手工编辑），一律标「估算」。未收录的模型金额显示 `—`，绝不编造。
- **只有 Claude 与 Codex**：后台读的是 Claude transcript 与 Codex rollout，自动发现的运行时两样都没有，所以不会出现在费用面板与会话列表里——它们的灯只说进程在干什么，不说花了多少钱。
- 已知限制：`--resume` 会话会把旧轮次记进新会话——单会话视图诚实，跨会话求和会重复计入。

`agent-history.db` 与 `codex_prices.json` 落在服务运行目录，重启与重新部署都不会清掉它们。设 `AGENT_SIGNALS_HISTORY=0` 可完全关闭历史埋点与费用估算。

## 数据源健康度

面板绝不把"读不到数据"显示成"没有任务"。顶部有一条数据源状态，取值 `正常 / 数据源不可用 / 数据结构已变`；某个源不可用时，该区域渲染灰斜纹提示并写明原因，而不是空态。

Codex 数据库不再硬编码版本号：默认在 `~/.codex/` 下挑版本号最大的 `state_*.sqlite`，并校验 `threads` 表字段；字段对不上时报 `数据结构已变` 而不是静默返回空。载荷带 `schemaVersion`，前端遇到不认识的版本会停止渲染并显示横幅，而不是猜。

Codex 数据库会同时从 `~/.codex/` 与 `~/.codex/sqlite/` 探测，并优先使用版本更高、更新时间更新的文件。`threads` 表的新增或可选字段缺失会自动兼容；只有核心身份、路径或活动时间字段全部不可用时才会停止该数据源，并显示具体缺失字段。

Codex 写端不在线且 WAL 上次未干净收尾时，纯只读打开会失败；面板会自动退回 `immutable` 模式读取——此时写端必然离线，不存在脏读的顾虑，写端回来后自动恢复正常读取。

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
- 负载值在服务端量化（token 取整千、百分比取整、步间隔分桶，全部整数），空闲会话的响应字节不变，304 契约不受影响。

## 可调环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SIGNALS_STALL_AFTER_MS` | `180000` | 判定疑似卡死的时长阈值 |
| `AGENT_SIGNALS_CPU_EPSILON` | `0.05` | 认定"有 CPU 活动"的秒数下限 |
| `AGENT_SIGNALS_SAMPLE_INTERVAL_MS` | `5000` | 后台采样间隔 |
| `AGENT_SIGNALS_LONG_POLL_MAX_S` | `8.0` | 单次长轮询最长阻塞秒数 |
| `AGENT_SIGNALS_MAX_WAITERS` | `8` | 同时挂起的长轮询上限 |
| `AGENT_SIGNALS_NOTIFY` | `1` | 设为 `0` 关闭 Mac 通知 |
| `AGENT_SIGNALS_NOTIFY_THROTTLE_MS` | `60000` | 同一会话通知最小间隔 |
| `AGENT_SIGNALS_NOTIFY_MAX_PER_MINUTE` | `10` | 全局每分钟通知上限 |
| `AGENT_SIGNALS_STATE_PATH` | `server.py` 同级的 `.agent-signals-state.json` | 已确认完成状态的存放位置 |
| `CLAUDE_SESSIONS_DIR` | `~/.claude/sessions` | Claude 会话目录 |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude transcript 目录 |
| `CLAUDE_DESKTOP_SUPPORT_DIR` | `~/Library/Application Support/Claude` | 桌面 App 的会话索引所在目录 |
| `AGENT_SIGNALS_CLAUDE_CONTEXT_WINDOW` | `200000` | Claude transcript 不含窗口大小，负载条分母用它 |
| `AGENT_SIGNALS_CLAUDE_HEADLESS_ACTIVE_MS` | `30000` | headless / 桌面 App 会话静默多久算做完 |
| `AGENT_SIGNALS_SUBAGENT_ACTIVE_MS` | `60000` | 子代理卫星静默多久算做完 |
| `AGENT_SIGNALS_SUBAGENT_LINGER_MS` | `600000` | 做完的子代理卫星还显示多久 |
| `CODEX_DIR` | `~/.codex` | Codex 主目录 |
| `CODEX_DB_PATH` | 自动发现 | 指定 Codex sqlite，跳过版本探测 |
| `AGENT_SIGNALS_HISTORY` | `1` | 设为 `0` 完全关闭历史埋点与费用估算 |
| `AGENT_SIGNALS_HISTORY_DB` | 运行目录下 `agent-history.db` | 历史库路径 |
| `AGENT_SIGNALS_CODEX_PRICES` | 运行目录下 `codex_prices.json` | Codex 估算价格文件（可手工编辑） |
| `AGENT_SIGNALS_CLAUDE_PRICES` | tokenusage 的 `prices.json` | Claude 价格镜像路径 |
| `AGENT_SIGNALS_HISTORY_BACKFILL_DAYS` | `30` | 历史回填窗口（天） |
| `AGENT_SIGNALS_DISCOVERY` | `1` | 设为 `0` 关闭自动发现 |
| `AGENT_SIGNALS_DISCOVERY_ACTIVE_MS` | `20000` | 发现型的灯静默多久算做完 |
| `AGENT_SIGNALS_DISCOVERY_WARMUP_SAMPLES` | `2` | 新灯头几轮只登记活动，状态按空闲算 |
| `AGENT_SIGNALS_DISCOVERY_MIN_AGE_S` | `5` | 比这更年轻的进程不出灯 |
| `AGENT_SIGNALS_DISCOVERY_MAX_AGENTS` | `24` | 发现型灯的数量上限 |
| `AGENT_SIGNALS_DISCOVERY_NOTIFY_MIN_BUSY_MS` | `60000` | 忙够这么久，完成时才发通知 |
| `AGENT_SIGNALS_PYTHON` | 自动发现 | npm 启动器使用的 Python 解释器 |

## 其他

Codex 空闲任务可用右上角的 × 手动隐藏，并会在空闲 24 小时后自动隐藏；点击左上角的 🔒 锁定后可持续保留。隐藏与锁定状态保存在当前浏览器中。

页面右上角可切换暗色与亮色模式，主题选择同样会保存在当前浏览器中。

## 安全现状

服务绑定 `0.0.0.0` 且**没有鉴权**：同一 Wi-Fi 下任何人都能调用 `/api/open`，从而在这台 Mac 上触发 AppleScript 切换窗口。当前接口都是只读或"打开窗口"级别，风险可控；**若以后新增任何操作类接口（批准、发送提示等），必须先补上配对鉴权**。另外 `/api/history` 会把会话名、目录名与金额估算暴露给同一 Wi-Fi 上的任何人——仍是只读，但敏感度比状态灯本身高一档。

不接受这个取舍就用 `agent-signals --host 127.0.0.1`，代价是 iPad 看不了。

## 测试

```bash
python3 -m unittest discover tests -v
```

目前 217 个测试，只用标准库，没有额外的测试框架要装。

## 部署

部署走 `./deploy.sh`，不要手动 cp：

```bash
./deploy.sh
```

四步固定顺序：跑测试 → 拷贝 `server.py`、`discovery.py` 与 `static/*` 到运行目录 → `launchctl kickstart -k` 重启服务 → 轮询 `/health` 直到 `version` 对上且 `pid` 换了（20 秒超时）。`discovery.py` 是无条件拷贝的：`server.py` 启动时按路径加载它，缺了就起不来，所以宁可在 `cp` 这一步当场失败，也不要留下一个拷了一半的运行目录。测试不过就一个文件都不拷；比对 pid 是因为光比版本号会被"端口被别的进程占着、新进程根本没起来"这种假阳性骗过去。

`agent-history.db`、`codex_prices.json`、`.agent-signals-state.json`、`runtime-profiles.json` 是运行时数据，部署脚本永远不覆盖它们。运行目录默认 `~/Library/Application Support/AgentSignals`，可用 `AGENT_SIGNALS_DEPLOY_DIR` 覆盖。

`/health` 返回 `{ok, version, schemaVersion, platforms, pid}`，部署脚本正是靠这几个字段判定重启是否真的生效。

## 目录

```
server.py                 状态采集 + HTTP 服务 + 通知 + 历史埋点/费用估算
discovery.py              发现引擎（纯函数，不 import server）：种子表 + dotdir 规则
bin/cli.js                npm 启动器，转交给 python3
static/index.html         页面骨架
static/app.js             渲染、长轮询、提示音、费用与历史面板
static/styles.css         主题与状态样式
static/icon-180.png       主屏图标
static/manifest.webmanifest
tests/test_server.py      呼吸灯主功能测试
tests/test_history.py     历史埋点与费用估算测试
tests/test_discovery.py   发现引擎与发现型灯的测试
run.command               双击启动
deploy.sh                 部署到 launchd 运行目录（测试 → 拷贝 → 重启 → 验版本）
docs/ARCHITECTURE.zh-CN.md  完整技术架构文档
requirements.txt          刻意留空的零依赖声明
```

## 许可

[MIT](LICENSE)

## 路线图

自动发现更多运行时已经落地，见上面的「自动发现的运行时」。云端会话仍在这个分支上开发，尚未发布。

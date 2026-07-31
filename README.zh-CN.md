# Agent 干啥呢 · agent-signals

[![npm](https://img.shields.io/npm/v/agent-signals.svg)](https://www.npmjs.com/package/agent-signals)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

本地 Claude Code / Codex 状态呼吸灯：一眼看出手上的 agent 在思考、在等你、已经干完，还是悄悄卡死了。

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
| Agent | Claude Code 和/或 Codex Desktop。只装一个也能用，另一个数据源会显示为不可用 |

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

点击主灯会在 Mac 上打开对应的 Terminal 标签页或 Codex 任务，并由本地服务确认这次完成状态。子代理显示为主灯外围的小卫星点。

## 疑似卡死怎么判定

只靠"思考了很久"会把正常的长构建误判成卡死，所以要求**时长超阈值**且**完全没有活动信号**两个条件同时成立：

| 平台 | 持续时长 | 活动信号 |
| --- | --- | --- |
| Claude | `statusUpdatedAt` 到现在（该字段在状态不变期间冻结，等于连续 thinking 时长） | 进程树累计 CPU 增量 > `CPU_EPSILON`，或 `~/.claude/projects/*/<sessionId>.jsonl` 有新写入 |
| Codex | rollout 文件最后写入到现在 | rollout 文件 mtime 变化 |

蓝灯卡片会显示"已持续 N 分钟"，让健康的长任务无需判断即可读；紫灯显示"已 N 分无新事件"。

## 数据源健康度

面板绝不把"读不到数据"显示成"没有任务"。顶部有一条数据源状态，取值 `正常 / 数据源不可用 / 数据结构已变`；某个源不可用时，该区域渲染灰斜纹提示并写明原因，而不是空态。

Codex 数据库不再硬编码版本号：默认在 `~/.codex/` 下挑版本号最大的 `state_*.sqlite`，并校验 `threads` 表字段；字段对不上时报 `数据结构已变` 而不是静默返回空。载荷带 `schemaVersion`，前端遇到不认识的版本会停止渲染并显示横幅，而不是猜。

Codex 数据库会同时从 `~/.codex/` 与 `~/.codex/sqlite/` 探测，并优先使用版本更高、更新时间更新的文件。`threads` 表的新增或可选字段缺失会自动兼容；只有核心身份、路径或活动时间字段全部不可用时才会停止该数据源，并显示具体缺失字段。

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
- rollout 文件按 `(mtime, size)` 缓存解析结果，不变就不重读。实测冷启读 3.0 MB，随后每轮 0 MB（改造前是每 2 秒读满 3.0 MB）。

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
| `CODEX_DIR` | `~/.codex` | Codex 主目录 |
| `CODEX_DB_PATH` | 自动发现 | 指定 Codex sqlite，跳过版本探测 |
| `AGENT_SIGNALS_PYTHON` | 自动发现 | npm 启动器使用的 Python 解释器 |

## 其他

Codex 空闲任务可用右上角的 × 手动隐藏，并会在空闲 24 小时后自动隐藏；点击左上角的 🔒 锁定后可持续保留。隐藏与锁定状态保存在当前浏览器中。

页面右上角可切换暗色与亮色模式，主题选择同样会保存在当前浏览器中。

## 安全现状

服务绑定 `0.0.0.0` 且**没有鉴权**：同一 Wi-Fi 下任何人都能调用 `/api/open`，从而在这台 Mac 上触发 AppleScript 切换窗口。当前接口都是只读或"打开窗口"级别，风险可控；**若以后新增任何操作类接口（批准、发送提示等），必须先补上配对鉴权**。

不接受这个取舍就用 `agent-signals --host 127.0.0.1`，代价是 iPad 看不了。

## 测试

```bash
python3 -m unittest discover tests -v
```

## 目录

```
server.py                 状态采集 + HTTP 服务 + 通知
bin/cli.js                npm 启动器，转交给 python3
static/index.html         页面骨架
static/app.js             渲染、长轮询、提示音
static/styles.css         主题与状态样式
static/icon-180.png       主屏图标
static/manifest.webmanifest
tests/test_server.py
run.command               双击启动
requirements.txt          刻意留空的零依赖声明
```

## 许可

[MIT](LICENSE)

# Agent Signals · Agent 干啥呢

[![npm](https://img.shields.io/npm/v/agent-signals.svg)](https://www.npmjs.com/package/agent-signals)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A local breathing-light panel that shows what your Claude Code and Codex sessions
are doing right now — thinking, waiting on you, finished, or quietly stuck.

Read [中文文档](README.zh-CN.md) instead.

It reads local state only. It never modifies your hooks, session files, or
transcripts, and it never sends anything off the machine.

```
npx agent-signals
# → http://127.0.0.1:8812
```

Keep it open on an iPad next to your desk and glance over instead of
alt-tabbing through terminal windows to see whether an agent is still working.

## Requirements

| | |
| --- | --- |
| OS | macOS — the panel shells out to `osascript` (notifications, window focus) and BSD `ps` |
| Python | 3.9 or newer. macOS ships one at `/usr/bin/python3` |
| Node | 16 or newer — only if you install via npm; running from source needs no Node |
| Agents | Claude Code and/or Codex Desktop. Either one alone works; the other source just reports as unavailable |

No third-party Python packages. `server.py` runs on the standard library alone,
which is why [`requirements.txt`](requirements.txt) is empty by design.

## Install

**Run it without installing:**

```bash
npx agent-signals
```

**Install globally:**

```bash
npm install -g agent-signals
agent-signals
```

**From source:**

```bash
git clone https://github.com/TZD666/agent-signals.git
cd agent-signals
python3 server.py
```

On a Mac you can also double-click `run.command` in the cloned folder.

Open `http://127.0.0.1:8812` on the Mac itself. From an iPad on the same Wi-Fi,
use `http://<your-mac-lan-ip>:8812`.

```bash
agent-signals --port 9000     # different port
agent-signals --host 127.0.0.1  # local only, no LAN access
```

## Status colors

| Color | Meaning |
| --- | --- |
| White | Idle |
| Blue | Thinking, or running a command |
| Green | Finished — clears once you click through to the task |
| Amber | Needs your input or approval |
| Purple | Possibly stuck |
| Red | Exited abnormally |

Clicking the main light opens the matching Terminal tab or Codex thread on the
Mac, and the local service marks that completion as acknowledged. Subagents show
up as small satellite dots orbiting the main light.

## How "possibly stuck" is decided

"It has been thinking for a while" on its own would flag every healthy long
build as stuck. So the panel requires **both** a duration over the threshold
**and** the complete absence of activity signals:

| Platform | Duration | Activity signal |
| --- | --- | --- |
| Claude | `statusUpdatedAt` until now — the field freezes while the status is unchanged, so this equals continuous thinking time | Cumulative CPU delta across the process tree > `CPU_EPSILON`, or a fresh write to `~/.claude/projects/*/<sessionId>.jsonl` |
| Codex | Last write to the rollout file until now | Change in the rollout file's mtime |

A blue card reads "已持续 N 分钟" (running for N minutes) so a healthy long task
needs no judgement call; a purple one reads "已 N 分无新事件" (no new events for
N minutes).

## Data source health

The panel never renders "cannot read the data" as "there are no tasks". A data
source line at the top reads `正常` (live) / `数据源不可用` (unavailable) /
`数据结构已变` (schema changed). When a source is down, its region renders as
grey hatching with the reason spelled out, rather than as an empty state.

The Codex database version is not hardcoded: by default the panel picks the
highest-versioned `state_*.sqlite` under `~/.codex/`, and validates the columns
of the `threads` table. If the columns do not line up it reports a schema change
instead of silently returning nothing. The payload carries a `schemaVersion`,
and a frontend that meets a version it does not recognise stops rendering and
shows a banner rather than guessing.

Both `~/.codex/` and `~/.codex/sqlite/` are probed, preferring the higher
version and more recent file. New or missing optional columns on `threads` are
handled gracefully; the source is only dropped when every core identity, path,
or activity-time column is unusable — and then it names the missing fields.

## Notifications

When a session enters *needs input*, *possibly stuck*, or *finished*:

- **Native macOS notification** via `osascript`. At most one per session per 60
  seconds, and at most 10 per minute globally. The first sampling round after
  startup only records the current state without notifying, so booting up does
  not flood you.
- **In-page alert**: a sound, a `(1) 需要输入` title badge, and a flashing card.
  Browsers require a user gesture before allowing audio, so the first visit
  needs one click on the page; until then a hint shows at the top.

macOS prompts for permission the first time — **you have to allow it once**. Be
aware that if permission is denied the system drops notifications silently while
`osascript` still reports success. The program cannot detect this; the panel can
only surface "Mac 通知发送失败" when `osascript` itself errors. So please
eyeball the first notification to confirm it arrived.

Set `AGENT_SIGNALS_NOTIFY=0` to turn notifications off.

## Add to the iPad home screen

Open the panel in Safari → Share → Add to Home Screen. Launching from the home
screen icon then gives you a full-screen standalone window with no address bar.

You will not get pushes while the iPad is locked or backgrounded: Web Push
requires a secure context (HTTPS), which a LAN HTTP address is not. Background
alerting is handled by the native Mac notifications instead.

## Power use

- The browser long-polls with `?wait=8` plus an ETag; when nothing has changed
  the server replies 304 with an empty body.
- Polling stops when the page goes to the background and fires immediately on
  return.
- Rollout files are cached on `(mtime, size)`, so an unchanged file is not
  re-read. Measured: 3.0 MB on a cold start, then 0 MB per round — before this
  change it read the full 3.0 MB every 2 seconds.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_SIGNALS_STALL_AFTER_MS` | `180000` | Duration threshold for calling a session stuck |
| `AGENT_SIGNALS_CPU_EPSILON` | `0.05` | Seconds of CPU delta that count as "there was activity" |
| `AGENT_SIGNALS_SAMPLE_INTERVAL_MS` | `5000` | Background sampling interval |
| `AGENT_SIGNALS_LONG_POLL_MAX_S` | `8.0` | Upper bound on how long a long poll may block |
| `AGENT_SIGNALS_MAX_WAITERS` | `8` | Maximum concurrent long-poll waiters |
| `AGENT_SIGNALS_NOTIFY` | `1` | Set to `0` to disable Mac notifications |
| `AGENT_SIGNALS_NOTIFY_THROTTLE_MS` | `60000` | Minimum gap between notifications for one session |
| `AGENT_SIGNALS_NOTIFY_MAX_PER_MINUTE` | `10` | Global notification ceiling per minute |
| `AGENT_SIGNALS_STATE_PATH` | `.agent-signals-state.json` next to `server.py` | Where acknowledged completions are stored |
| `CLAUDE_SESSIONS_DIR` | `~/.claude/sessions` | Claude session directory |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude transcript directory |
| `CODEX_DIR` | `~/.codex` | Codex home directory |
| `CODEX_DB_PATH` | auto-discovered | Pin a specific Codex sqlite file, skipping version detection |
| `AGENT_SIGNALS_PYTHON` | auto-discovered | Interpreter the npm launcher should use |

## Miscellaneous

Idle Codex tasks can be hidden by hand with the × in the top-right corner, and
are hidden automatically after 24 idle hours; clicking the 🔒 in the top-left
pins one so it stays. Hidden and pinned state lives in the current browser.

The top-right corner toggles dark and light mode, also remembered per browser.

## Security

The service binds `0.0.0.0` and **has no authentication**: anyone on the same
Wi-Fi can call `/api/open` and thereby trigger an AppleScript window switch on
this Mac. Every current endpoint is read-only or "bring a window to the front",
so the risk is contained — but **if any action endpoint is ever added (approve,
send a prompt, and so on), pairing authentication must land first**.

If that trade-off is not for you, run `agent-signals --host 127.0.0.1` and give
up the iPad view.

## Tests

```bash
python3 -m unittest discover tests -v
```

## Layout

```
server.py                 state sampling + HTTP service + notifications
bin/cli.js                npm launcher, hands over to python3
static/index.html         page skeleton
static/app.js             rendering, long polling, alert sound
static/styles.css         theme and status styles
static/icon-180.png       home screen icon
static/manifest.webmanifest
tests/test_server.py
run.command               double-click launcher
requirements.txt          no third-party dependencies, by design
```

## License

[MIT](LICENSE)

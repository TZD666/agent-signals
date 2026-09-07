"""Phase 4：发现引擎与发现型灯的测试。

夹具尽量用本机 2026-09-07 抓的真实 `ps -axo pid=,ppid=,uid=,time=,etime=,command=`
行（Chrome / Lark / ChatGPT.app 的 helper、ChatGPT.app 的 codex 树、三个
`.local/bin/claude`、面板自己），只有当前没在跑的进程（Battle.net Agent、
CodexBar、WorkBuddy）才按台账里记下的形状手写。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).parents[1]

DISCOVERY_SPEC = importlib.util.spec_from_file_location(
    "agent_signals_discovery", REPO / "discovery.py"
)
discovery = importlib.util.module_from_spec(DISCOVERY_SPEC)
sys.modules[DISCOVERY_SPEC.name] = discovery
DISCOVERY_SPEC.loader.exec_module(discovery)

SERVER_SPEC = importlib.util.spec_from_file_location(
    "agent_status_server", REPO / "server.py"
)
server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = server
SERVER_SPEC.loader.exec_module(server)

HOME = Path("/Users/edy")
UID = 501


# ---------------------------------------------------------------------------
# 真实 ps 行（2026-09-07 抓的那一份里挑出来的）
# ---------------------------------------------------------------------------

PANEL = (
    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/"
    "Versions/3.9/Resources/Python.app/Contents/MacOS/Python "
    "/Users/edy/Library/Application Support/AgentSignals/server.py "
    "--host 0.0.0.0 --port 8812"
)
CLAUDE_LOCAL = "/Users/edy/.local/bin/claude"
CLAUDE_LOCAL_HEADLESS = (
    '/Users/edy/.local/bin/claude -p --model opus --output-format json '
    '--json-schema {"type": "object", "required": ["changes", "report"]} '
    "--disable-slash-commands"
)
CLAUDE_DESKTOP_BUNDLED = (
    "/Users/edy/Library/Application Support/Claude/claude-code/2.1.260/"
    "claude.app/Contents/MacOS/claude"
)
CLAUDE_APP_MAIN = "/Applications/Claude.app/Contents/MacOS/Claude"
CLAUDE_APP_CRASHPAD = (
    "/Applications/Claude.app/Contents/Frameworks/Electron Framework.framework/"
    "Helpers/chrome_crashpad_handler --no-rate-limit "
    "--database=/Users/edy/Library/Application Support/Claude/Crashpad"
)
CHROME_HELPER = (
    "/Applications/Google Chrome.app/Contents/Frameworks/"
    "Google Chrome Framework.framework/Versions/152.0.7977.65/Helpers/"
    "Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper "
    "--type=gpu-process --metrics-client-id=a6fac673 --seatbelt-client=26"
)
LARK_HELPER = (
    "/Applications/Lark.app/Contents/Frameworks/Lark Framework.framework/"
    "Versions/131.0.6778.268/Helpers/Lark Helper.app/Contents/MacOS/Lark Helper "
    "--aha-process-t=aha-service --type=monitor"
)
CHATGPT_APP = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
CHATGPT_CODEX = (
    "/Applications/ChatGPT.app/Contents/Resources/codex "
    "-c features.code_mode_host=true app-server --analytics-default-enabled"
)
CHATGPT_CODEX_NODE = (
    "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node "
    "/Users/edy/.codex/plugins/cache/openai-bundled/unified-computer-use/"
    "26.901.20858/scripts/launch.mjs"
)
AMP_DEVICE_AGENT = (
    "/System/Library/PrivateFrameworks/AMPDevices.framework/Versions/A/Support/"
    "AMPDeviceDiscoveryAgent --launchd"
)
AMP_LIBRARY_AGENT = (
    "/System/Library/PrivateFrameworks/AMPLibrary.framework/Versions/A/Support/"
    "AMPLibraryAgent --launchd"
)
CURSOR_UI_SERVICE = (
    "/System/Library/PrivateFrameworks/TextInputUIMacHelper.framework/Versions/A/"
    "XPCServices/CursorUIViewService.xpc/Contents/MacOS/CursorUIViewService"
)
MCP_NODE = (
    "node /Users/edy/.claude/plugins/cache/Yeachan-Heo/oh-my-claudecode/4.8.0/"
    "bridge/mcp-server.cjs"
)
NPX_NODE = (
    "node /Users/edy/.npm/_npx/74dfe5d932228314/node_modules/.bin/lark-mcp "
    "mcp -a cli_aa015e4b"
)

# 当前没在跑，按台账/附录记下的形状手写
BATTLE_NET = (
    "/Applications/Battle.net.app/Contents/MacOS/Battle.net Helper "
    "--updater --session"
)
CODEX_BAR = "/Applications/CodexBar.app/Contents/MacOS/CodexBar"
WORKBUDDY_MAIN = (
    "/Applications/WorkBuddy.app/Contents/MacOS/Electron "
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar/main/"
    "sidecar-entry.js --token 06893"
)
WORKBUDDY_HELPER = (
    "/Applications/WorkBuddy.app/Contents/Frameworks/Electron Framework.framework/"
    "Versions/A/Helpers/WorkBuddy Helper (GPU).app/Contents/MacOS/"
    "WorkBuddy Helper (GPU) --type=gpu-process "
    "--user-data-dir=/Users/edy/.workbuddy"
)
WORKBUDDY_SANDBOX = (
    "/Users/edy/.workbuddy/binaries/sandbox-center "
    '--config {"appHome":"/Users/edy/.workbuddy","port":63296}'
)
DSH_NODE = (
    "/opt/homebrew/Cellar/node/24.4.0/bin/node "
    "/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web --no-open"
)
OPENCLAW_NODE = (
    "/opt/homebrew/bin/node /Users/edy/work/node_modules/openclaw/dist/cli.js gateway"
)


def table(rows):
    """rows: (pid, ppid, uid, command[, start_s]) → 一份 ps 表。"""
    built = {"children": {}, "cpu": {}, "commands": {}, "start_s": {}, "uid": {}}
    for row in rows:
        pid, ppid, uid, command = row[0], row[1], row[2], row[3]
        start_s = row[4] if len(row) > 4 else 1_000
        built["commands"][pid] = command
        built["uid"][pid] = uid
        built["cpu"][pid] = 0.0
        built["start_s"][pid] = start_s
        built["children"].setdefault(ppid, []).append(pid)
    return built


def run(rows, claimed=(), max_agents=discovery.DEFAULT_MAX_AGENTS):
    return discovery.classify(
        table(rows),
        discovery.DEFAULT_PROFILES,
        discovery.DEFAULT_PROFILES["ignore"],
        set(claimed),
        HOME,
        UID,
        max_agents,
    )


def families(result):
    return sorted(candidate.family for candidate in result.candidates)


class ArgvTests(unittest.TestCase):
    def test_parse_argv_interpreter_script(self):
        argv = discovery.parse_argv(MCP_NODE)
        self.assertEqual(argv.exe, "node")
        self.assertEqual(argv.basename, "node")
        self.assertTrue(argv.script.endswith("bridge/mcp-server.cjs"))
        # 解释器自己不说明身份，脚本名才说明。
        self.assertEqual(argv.names, ("mcp-server",))
        self.assertFalse(argv.in_bundle)

    def test_framework_python_is_still_an_interpreter(self):
        # macOS 上 Homebrew 与系统的 python 可执行文件都叫 `Python`（大写 P），
        # 而且住在 `…/Python.app/Contents/MacOS/Python` 里。照 argv[0] 判 `.app`
        # 会把所有 python 写的 agent 一并豁免掉。
        command = (
            "/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/"
            "Versions/3.14/Resources/Python.app/Contents/MacOS/Python "
            "/Users/edy/.fakeagent/run.py"
        )
        argv = discovery.parse_argv(command)
        self.assertEqual(argv.script, "/Users/edy/.fakeagent/run.py")
        self.assertEqual(argv.names, ("run",))
        self.assertFalse(argv.in_bundle)
        self.assertTrue(discovery.is_interpreter("Python"))
        self.assertTrue(discovery.is_interpreter("python3.14"))
        self.assertFalse(discovery.is_interpreter("pythonista"))

    def test_parse_argv_plain_binary(self):
        argv = discovery.parse_argv(CLAUDE_LOCAL_HEADLESS)
        self.assertEqual(argv.exe, "/Users/edy/.local/bin/claude")
        self.assertEqual(argv.names, ("claude",))
        # JSON schema 参数不许混进路径区，否则种子片段会被随便一段文本命中。
        self.assertEqual(argv.region, "/Users/edy/.local/bin/claude")

    def test_path_region_keeps_paths_with_spaces(self):
        argv = discovery.parse_argv(WORKBUDDY_MAIN)
        self.assertIn("/Applications/WorkBuddy.app/Contents/MacOS/Electron", argv.region)
        self.assertIn("sidecar-entry.js", argv.region)
        self.assertTrue(argv.in_bundle)


class SeedMatchTests(unittest.TestCase):
    def test_seed_matches_claude_local_bin(self):
        # 本机真实路径是 .local/bin/，不是种子表写的 .local/share/…：
        # 命中的是 basename 精确相等那一条。
        result = run(
            [
                (29796, 1, UID, CLAUDE_LOCAL),
                (35129, 1, UID, CLAUDE_LOCAL_HEADLESS),
                (35596, 1, UID, CLAUDE_LOCAL),
            ]
        )
        self.assertEqual(families(result), ["claude", "claude", "claude"])
        self.assertEqual(
            sorted(c.pid for c in result.candidates), [29796, 35129, 35596]
        )
        self.assertTrue(all(c.source == "seed" for c in result.candidates))

    def test_seed_matches_desktop_bundled_claude_as_desktop(self):
        # 桌面 App 自带的那个二进制在 .app 包里：只能靠种子里写死的路径片段命中。
        result = run([(4242, 1, UID, CLAUDE_DESKTOP_BUNDLED)])
        self.assertEqual(families(result), ["claude"])
        self.assertEqual(result.candidates[0].source, "seed")

    def test_seed_matches_dsh_and_openclaw_node_scripts(self):
        result = run(
            [
                (5001, 1, UID, DSH_NODE),
                (5002, 1, UID, OPENCLAW_NODE),
            ]
        )
        self.assertEqual(families(result), ["dsh", "openclaw"])
        by_family = {c.family: c for c in result.candidates}
        self.assertEqual(by_family["dsh"].label, "DeepSeek Harness")
        self.assertEqual(by_family["dsh"].open, {"url": 3080})
        self.assertEqual(by_family["dsh"].home_label, "~/.dsh")
        self.assertIsNone(by_family["openclaw"].open)

    def test_bare_amp_or_pi_basename_not_enough(self):
        # `amp` / `pi` 这种裸名太容易撞车，种子里故意不给 basename。
        result = run(
            [
                (6001, 1, UID, "/opt/homebrew/bin/amp --help"),
                (6002, 1, UID, "/usr/local/bin/pi"),
            ]
        )
        self.assertEqual(families(result), [])
        result = run(
            [
                (
                    6003,
                    1,
                    UID,
                    "node /Users/edy/work/node_modules/@sourcegraph/amp/dist/cli.js",
                )
            ]
        )
        self.assertEqual(families(result), ["amp"])

    def test_workbuddy_app_bundle_matches_by_path_only(self):
        # 主程序的可执行文件名就叫 Electron，basename 匹配完全无效。
        argv = discovery.parse_argv(WORKBUDDY_MAIN)
        self.assertEqual(argv.basename, "Electron")
        result = run([(93850, 1, UID, WORKBUDDY_MAIN)])
        self.assertEqual(families(result), ["workbuddy"])
        self.assertEqual(result.candidates[0].label, "WorkBuddy")
        self.assertEqual(result.candidates[0].open, {"app": "WorkBuddy"})


class HardExclusionTests(unittest.TestCase):
    def test_ignores_electron_helpers_with_type_flag(self):
        result = run(
            [
                (1627, 1555, UID, CHROME_HELPER),
                (1665, 1564, UID, LARK_HELPER),
                (93851, 93850, UID, WORKBUDDY_HELPER),
            ]
        )
        self.assertEqual(result.candidates, [])
        # helper 连「未分类」都不进：Phase 5 的探针也不该去扫它们。
        self.assertEqual(result.unclassified, [])

    def test_ignores_battlenet_agent_amp_agents_cursor_ui_service_codexbar(self):
        result = run(
            [
                (1575, 1, UID, AMP_DEVICE_AGENT),
                (4158, 1, UID, AMP_LIBRARY_AGENT),
                (1610, 1, UID, CURSOR_UI_SERVICE),
                (7001, 1, UID, BATTLE_NET),
                (7002, 1, UID, CODEX_BAR),
            ]
        )
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.unclassified, [])

    def test_other_uid_excluded(self):
        rows = [(9001, 1, 0, CLAUDE_LOCAL)]
        self.assertEqual(run(rows).candidates, [])
        self.assertEqual(run(rows).unclassified, [])
        # 同一条命令换成自己的 uid 就该出来，证明拦掉它的确实是 uid。
        self.assertEqual(families(run([(9001, 1, UID, CLAUDE_LOCAL)])), ["claude"])

    def test_claude_app_main_process_is_not_an_agent(self):
        # Claude.app 本体不是一个会话，它的会话由登记表出灯。
        result = run(
            [
                (22100, 1, UID, CLAUDE_APP_MAIN),
                (22123, 22100, UID, CLAUDE_APP_CRASHPAD),
            ]
        )
        self.assertEqual(result.candidates, [])

    def test_panel_itself_is_excluded_by_pid(self):
        # 部署路径 ~/Library/Application Support/AgentSignals/ 与仓库路径不是
        # 一回事，只能靠 pid 排除自己（子进程跟着一起）。
        rows = [(96439, 1, UID, PANEL), (96440, 96439, UID, "sleep 5")]
        self.assertEqual(run(rows, claimed={96439}).candidates, [])

    def test_another_panel_instance_is_not_an_agent_family(self):
        # 实测：面板的运行目录里 agent-history.db 有 sessions 表，完全符合
        # agent home 的形状。自己那个进程有 pid 排除，同时跑着的第二个实例
        # 只能靠 dotdirs 黑名单挡住。
        rows = [(96439, 1, UID, PANEL)]
        self.assertEqual(run(rows).candidates, [])


class ClaimedTests(unittest.TestCase):
    def test_chatgpt_app_codex_tree_is_claimed_not_discovered(self):
        rows = [
            (11422, 1, UID, CHATGPT_APP),
            (11471, 11422, UID, CHATGPT_CODEX),
            (31410, 11471, UID, CHATGPT_CODEX_NODE),
        ]
        claimed = discovery.claimed_pids(table(rows))
        self.assertEqual(claimed, {11422, 11471, 31410})
        self.assertEqual(run(rows, claimed).candidates, [])

    def test_children_of_claimed_claude_pid_skipped(self):
        rows = [
            (29796, 1, UID, CLAUDE_LOCAL),
            (29872, 29796, UID, MCP_NODE),
            (29983, 29796, UID, NPX_NODE),
        ]
        # 登记表认领了 29796，MCP 子进程跟着一起走。
        self.assertEqual(run(rows, {29796, 29872, 29983}).candidates, [])
        # 就算登记表没认领，MCP 子进程也只会剩一盏灯：node 的脚本名不在种子表里，
        # ~/.claude 与 ~/.npm 又都在 dotdirs 黑名单上，它们连候选都不是。
        result = run(rows)
        self.assertEqual([c.pid for c in result.candidates], [29796])
        self.assertEqual(result.candidates[0].members, (29796,))


class DotdirTests(unittest.TestCase):
    def setUp(self):
        discovery.reset_caches()
        self.addCleanup(discovery.reset_caches)

    def test_dotdir_rule_requires_agent_looking_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".fakeagent").mkdir()
            (home / ".dullagent").mkdir()
            (home / ".fakeagent" / "sessions").mkdir()
            rows = table(
                [
                    (100, 1, UID, f"python3 {home}/.fakeagent/run.py"),
                    (101, 1, UID, f"python3 {home}/.dullagent/run.py"),
                ]
            )
            result = discovery.classify(
                rows,
                discovery.DEFAULT_PROFILES,
                discovery.DEFAULT_PROFILES["ignore"],
                set(),
                home,
                UID,
            )
        self.assertEqual(families(result), ["fakeagent"])
        candidate = result.candidates[0]
        self.assertEqual(candidate.label, "Fakeagent")
        self.assertEqual(candidate.source, "dotdir")
        # 状态目录只有这条规则知道：不带出来就既没有活动信号，
        # detail 也写不出「自动发现 · ~/.fakeagent」。
        self.assertEqual(candidate.home, str(home / ".fakeagent"))
        self.assertEqual(candidate.home_label, "~/.fakeagent")
        self.assertEqual(candidate.watch, discovery.DEFAULT_WATCH)
        # 没被认出来的那个要带上原因，供 Phase 5 的探针参考。
        self.assertEqual([entry.pid for entry in result.unclassified], [101])
        self.assertIn("dullagent", result.unclassified[0].reason)

    def test_dotdir_sqlite_with_threads_table_counts(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".sqlagent").mkdir()
            connection = sqlite3.connect(home / ".sqlagent" / "state.db")
            connection.execute("CREATE TABLE threads (id TEXT)")
            connection.commit()
            connection.close()
            self.assertTrue(discovery.looks_like_agent_home(home / ".sqlagent"))

            (home / ".boring").mkdir()
            connection = sqlite3.connect(home / ".boring" / "state.db")
            connection.execute("CREATE TABLE bookmarks (id TEXT)")
            connection.commit()
            connection.close()
            self.assertFalse(discovery.looks_like_agent_home(home / ".boring"))

    def test_settings_plus_profiles_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".dshlike").mkdir()
            (home / ".dshlike" / "profiles").mkdir()
            (home / ".dshlike" / "settings.yaml").write_text("a: 1", encoding="utf-8")
            self.assertTrue(discovery.looks_like_agent_home(home / ".dshlike"))

    def test_ignored_dotdirs_never_become_families(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".omc").mkdir()
            (home / ".omc" / "sessions").mkdir()
            rows = table([(200, 1, UID, f"python3 {home}/.omc/tool.py")])
            result = discovery.classify(
                rows,
                discovery.DEFAULT_PROFILES,
                discovery.DEFAULT_PROFILES["ignore"],
                set(),
                home,
                UID,
            )
        self.assertEqual(result.candidates, [])

    def test_touching_dot_claude_does_not_make_a_claude_light(self):
        # 本机快照实测：`sh ~/.claude/hud/omc-hud-cache.sh` 会被 dotdir 规则推成
        # 一盏 Claude 灯。凡是读一下 ~/.claude 的 hook / statusline / MCP 进程
        # 都会中招，所以 .claude / .codex 进了 dotdirs 黑名单——它们有原生源，
        # 也有种子表的 basename 兜底，动态规则对它们只剩假阳性。
        hud = (
            "sh /Users/edy/.claude/hud/omc-hud-cache.sh "
            "/Users/edy/.claude/hud/omc-hud.mjs"
        )
        self.assertEqual(run([(37496, 1, UID, hud)]).candidates, [])
        codex_plugin = "node /Users/edy/.codex/plugins/cache/x/scripts/launch.mjs"
        self.assertEqual(run([(37497, 1, UID, codex_plugin)]).candidates, [])
        # 真正的 claude 进程照旧靠 basename 命中，不受影响。
        self.assertEqual(families(run([(29796, 1, UID, CLAUDE_LOCAL)])), ["claude"])

    def test_dotdir_converges_onto_the_seed_family_key(self):
        # ~/.workbuddy/sessions/ 会把家族名推成 workbuddy，与种子表同名：
        # 必须收敛到同一个 key，不能出现 workbuddy 与 WorkBuddy 两个分区。
        result = run([(93860, 1, UID, WORKBUDDY_SANDBOX)])
        self.assertEqual(families(result), ["workbuddy"])
        self.assertEqual(result.candidates[0].label, "WorkBuddy")
        self.assertEqual(result.candidates[0].source, "dotdir")

    def test_app_bundle_exe_never_uses_the_dotdir_rule(self):
        # Claude.app 的 crashpad helper 命令里带 ~/Library/Application Support/Claude/，
        # 照 dotdir 规则推会推成 claude 家族——.app 里的 exe 必须豁免。
        argv = discovery.parse_argv(CLAUDE_APP_CRASHPAD)
        self.assertTrue(argv.in_bundle)
        self.assertEqual(
            discovery.match_dotdir(
                argv,
                CLAUDE_APP_CRASHPAD,
                discovery.DEFAULT_PROFILES["families"],
                discovery.DEFAULT_PROFILES["ignore"],
                HOME,
            ),
            discovery.NO_DOTDIR,
        )

    def test_library_application_support_names_are_read(self):
        found = discovery.dotdir_candidates(
            "/Users/edy/Library/Application Support/Steam/steam.sh", HOME
        )
        self.assertEqual(
            found, [("Steam", HOME / "Library" / "Application Support" / "Steam")]
        )


class RollUpTests(unittest.TestCase):
    def test_roll_up_merges_descendants(self):
        # WorkBuddy 的验收标准：出现，且只有一盏灯。
        rows = [
            (93850, 1, UID, WORKBUDDY_MAIN),
            (93860, 93850, UID, WORKBUDDY_SANDBOX),
            (93870, 93860, UID, WORKBUDDY_SANDBOX),
        ]
        result = run(rows)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.pid, 93850)
        self.assertEqual(candidate.family, "workbuddy")
        self.assertEqual(candidate.members, (93850, 93860, 93870))

    def test_roll_up_stops_beyond_eight_generations(self):
        rows = [(100, 1, UID, CLAUDE_LOCAL)]
        parent = 100
        for pid in range(101, 111):
            rows.append((pid, parent, UID, "sleep 1"))
            parent = pid
        rows.append((200, parent, UID, CLAUDE_LOCAL))
        result = run(rows)
        # 隔了 10 层，够不到祖先，两盏灯各归各的。
        self.assertEqual(sorted(c.pid for c in result.candidates), [100, 200])


class TruncationTests(unittest.TestCase):
    def test_max_agents_truncates_newest_first(self):
        rows = [
            (100 + index, 1, UID, CLAUDE_LOCAL, 1_000 + index) for index in range(5)
        ]
        result = run(rows, max_agents=2)
        self.assertEqual(result.truncated, 3)
        self.assertEqual(sorted(c.pid for c in result.candidates), [103, 104])

    def test_unknown_start_sorts_last_when_truncating(self):
        rows = [
            (100, 1, UID, CLAUDE_LOCAL, None),
            (101, 1, UID, CLAUDE_LOCAL, 5_000),
        ]
        result = run(rows, max_agents=1)
        self.assertEqual([c.pid for c in result.candidates], [101])


class UnclassifiedHookTests(unittest.TestCase):
    """Phase 5 的探针出口：本阶段只留接口，不实现探针。"""

    def test_unclassified_carries_pid_exe_and_reason(self):
        result = run(
            [
                (300, 1, UID, "/opt/homebrew/bin/some-daemon --serve"),
                (301, 1, UID, CLAUDE_LOCAL),
            ]
        )
        self.assertEqual([entry.pid for entry in result.unclassified], [300])
        entry = result.unclassified[0]
        self.assertEqual(entry.exe, "/opt/homebrew/bin/some-daemon")
        self.assertTrue(entry.reason)

    def test_claimed_and_ignored_never_reach_the_probe(self):
        rows = [
            (11422, 1, UID, CHATGPT_APP),
            (1627, 1555, UID, CHROME_HELPER),
            (1575, 1, UID, AMP_DEVICE_AGENT),
            (9001, 1, 0, "/opt/homebrew/bin/whatever"),
        ]
        result = run(rows, discovery.claimed_pids(table(rows)))
        self.assertEqual(result.unclassified, [])


class WatchGlobTests(unittest.TestCase):
    def test_expand_watch_bounds_the_depth(self):
        self.assertEqual(
            discovery.expand_watch("**/*.jsonl"),
            ("*.jsonl", "*/*.jsonl", "*/*/*.jsonl"),
        )
        self.assertEqual(discovery.expand_watch("storages/*.json"), ("storages/*.json",))


# ---------------------------------------------------------------------------
# server 侧：DiscoveredSource 的状态机
# ---------------------------------------------------------------------------


def candidate(**overrides):
    fields = {
        "pid": 4242,
        "family": "dsh",
        "label": "DeepSeek Harness",
        "order": 10,
        "hint": "自动发现",
        "home": "",
        "home_label": "~/.dsh",
        "watch": (),
        "open": {"tty": True},
        "source": "seed",
        "exe": "/opt/homebrew/bin/dsh",
        "command": "dsh web",
        "start_s": 1_000,
        "members": (4242,),
    }
    fields.update(overrides)
    return discovery.Candidate(**fields)


class DiscoveredLightTests(unittest.TestCase):
    def setUp(self):
        server._discovery_state.clear()
        server._discovery_transitions.clear()
        server._discovery_cwd.clear()
        server._activity.clear()
        self.addCleanup(server._discovery_state.clear)
        self.addCleanup(server._discovery_transitions.clear)
        self.addCleanup(server._discovery_cwd.clear)
        self.addCleanup(server._activity.clear)
        cwd = patch.object(server, "process_cwd", return_value="/Users/edy/work")
        cwd.start()
        self.addCleanup(cwd.stop)
        mtime = patch.object(server, "watch_mtime", return_value=0.0)
        mtime.start()
        self.addCleanup(mtime.stop)

    def sample(self, current_ms, cpu=0.0, item=None):
        item = item or candidate()
        built = {
            "children": {},
            "cpu": {item.pid: cpu},
            "commands": {item.pid: item.command},
            "start_s": {item.pid: item.start_s},
            "uid": {item.pid: UID},
        }
        return server.discovered_agent(item, current_ms, built)

    def test_warmup_then_thinking_then_completed_then_idle_after_ack(self):
        base = 1_700_000_000_000
        # 预热两轮：note_activity 第一轮必然把 quietSince 记成「刚刚」，
        # 照着闩下去每盏灯一出生就先闪一次假的绿。
        self.assertEqual(self.sample(base)["status"], "idle")
        self.assertEqual(self.sample(base + 5_000, cpu=1.0)["status"], "idle")
        # 第三轮起才真正开始判定：CPU 还在涨 → 思考中。
        busy = self.sample(base + 10_000, cpu=2.0)
        self.assertEqual(busy["status"], "thinking")
        # 静下来超过 DISCOVERY_ACTIVE_MS → 完成，并且闩住。
        done = self.sample(base + 100_000, cpu=2.0)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["completionId"])
        still = self.sample(base + 110_000, cpu=2.0)
        self.assertEqual(still["status"], "completed")
        # 确认掉之后回到空闲。
        original = dict(server._acknowledged_completions)
        self.addCleanup(
            lambda: server._acknowledged_completions.update(original)
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                server, "STATE_PATH", Path(directory) / "state.json"
            ):
                server._acknowledged_completions.clear()
                server.acknowledge_agent(still)
                after = self.sample(base + 120_000, cpu=2.0)
        self.assertEqual(after["status"], "idle")
        self.assertEqual(after["completionId"], 0)

    def test_a_quiet_new_process_never_flashes_a_fake_completion(self):
        # 一个一直没动静的进程被发现后，不许先蓝一阵再转绿：
        # note_activity 初始化 quietSince 那一下不是活动。
        base = 1_700_000_000_000
        for step in range(12):
            agent = self.sample(base + step * 5_000, cpu=0.0)
            self.assertEqual(agent["status"], "idle", f"第 {step} 轮")
            self.assertEqual(agent["completionId"], 0)

    def test_no_stalled_or_needs_input_ever(self):
        base = 1_700_000_000_000
        seen = set()
        for step in range(40):
            agent = self.sample(base + step * 30_000, cpu=float(step % 3))
            seen.add(agent["status"])
        self.assertEqual(seen - {"idle", "thinking", "completed"}, set())

    def test_process_vanish_removes_light_without_error(self):
        base = 1_700_000_000_000
        table_now = {
            "children": {},
            "cpu": {},
            "commands": {},
            "start_s": {},
            "uid": {},
        }
        with patch.object(server, "scan_processes", return_value=table_now):
            agents, health = server.load_discovered(base, {"commands": {}})
        self.assertEqual(agents, [])
        # ps 一个进程都没返回是「读不到」，不是「没有任务」。
        self.assertEqual(health["state"], "unavailable")

    def test_young_process_does_not_light_up(self):
        base = 1_700_000_000_000
        young = candidate(start_s=base // 1000 - 1)
        self.assertIsNone(self.sample(base, item=young))
        old = candidate(start_s=base // 1000 - 60)
        self.assertIsNotNone(self.sample(base, item=old))

    def test_unknown_age_is_treated_as_old_enough(self):
        # etime 认不出来时 start_s 是 None：一个跑了三天的 agent 不该被
        # 年龄门槛误杀，id 也必须每轮都一样。
        base = 1_700_000_000_000
        first = self.sample(base, item=candidate(start_s=None))
        second = self.sample(base + 5_000, item=candidate(start_s=None))
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], "4242-u")
        self.assertEqual(first["id"], second["id"])

    def test_pid_reuse_gets_new_id(self):
        base = 1_700_000_000_000
        first = self.sample(base, item=candidate(start_s=1_000))
        second = self.sample(base, item=candidate(start_s=2_000))
        self.assertEqual(first["id"], "4242-1000")
        self.assertEqual(second["id"], "4242-2000")

    def test_discovered_load_is_empty_load_striped(self):
        agent = self.sample(1_700_000_000_000)
        self.assertEqual(agent["load"], server.empty_load())
        # 读不到 token 数据就画斜纹 `—`，绝不显示成 0%。
        self.assertIsNone(agent["load"]["contextPct"])

    def test_origin_and_detail_say_where_the_light_came_from(self):
        agent = self.sample(1_700_000_000_000)
        self.assertEqual(agent["origin"], "process")
        self.assertEqual(agent["detail"], "自动发现 · ~/.dsh")
        self.assertEqual(agent["name"], "DeepSeek Harness · work")
        self.assertEqual(agent["platform"], "dsh")

    def test_completed_notification_gated_by_min_busy(self):
        base = 1_700_000_000_000
        self.sample(base)
        self.sample(base + 5_000, cpu=1.0)
        self.sample(base + 10_000, cpu=2.0)
        quick = self.sample(base + 40_000, cpu=2.0)
        self.assertEqual(quick["status"], "completed")
        # 只忙了 30 秒，不值得弹一条系统通知。
        self.assertFalse(quick["notify"])

        server._discovery_state.clear()
        server._discovery_transitions.clear()
        server._activity.clear()
        self.sample(base)
        self.sample(base + 5_000, cpu=1.0)
        for step in range(1, 20):
            self.sample(base + 10_000 + step * 10_000, cpu=2.0 + step)
        # CPU 不再涨、又过了 ACTIVE 窗口 → 完成；这次忙了 200 秒，值得响。
        slow = self.sample(base + 400_000, cpu=21.0)
        self.assertEqual(slow["status"], "completed")
        self.assertTrue(slow["notify"])

    def test_notify_flag_suppresses_the_mac_notification(self):
        payload = {
            "generatedAt": 1_700_000_000_000,
            "platforms": [
                {
                    "key": "dsh",
                    "agents": [
                        {
                            "id": "quiet",
                            "status": "completed",
                            "completionId": 1,
                            "name": "x",
                            "notify": False,
                        }
                    ],
                }
            ],
        }
        server._notified.clear()
        server._notify_history.clear()
        with patch.object(server, "_notify_primed", True), patch.object(
            server, "send_mac_notification"
        ) as sender:
            sent = server.dispatch_notifications(payload)
        self.assertEqual(sent, [])
        sender.assert_not_called()

    def test_revision_stable_when_only_cpu_changes_below_epsilon(self):
        base = 1_700_000_000_000
        self.sample(base)
        self.sample(base + 5_000)
        first = self.sample(base + 10_000, cpu=0.0)
        second = self.sample(base + 15_000, cpu=server.CPU_EPSILON / 2)
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )


class DiscoveredOpenTests(unittest.TestCase):
    def test_open_url_only_loopback(self):
        with patch.object(server.subprocess, "run", return_value=Mock(returncode=0)) as run_:
            server.open_discovered({"openVia": "url:3080", "pid": 1})
        self.assertEqual(run_.call_args[0][0], ["open", "http://127.0.0.1:3080"])

    def test_open_url_rejects_anything_but_a_port(self):
        for via in ("url:0", "url:70000", "url:evil.example.com", "url:"):
            with patch.object(server.subprocess, "run") as run_:
                with self.assertRaises(RuntimeError):
                    server.open_discovered({"openVia": via, "pid": 1})
                run_.assert_not_called()

    def test_open_app_only_from_the_local_profile_allowlist(self):
        with patch.object(server.subprocess, "run", return_value=Mock(returncode=0)) as run_:
            server.open_discovered({"openVia": "app:WorkBuddy", "pid": 1})
        self.assertEqual(run_.call_args[0][0], ["open", "-a", "WorkBuddy"])
        with patch.object(server.subprocess, "run") as run_:
            with self.assertRaises(RuntimeError):
                server.open_discovered({"openVia": "app:Calculator", "pid": 1})
            run_.assert_not_called()

    def test_open_via_comes_from_the_profile_not_the_request(self):
        with patch.object(server, "port_listening", return_value=True):
            self.assertEqual(
                server.discovered_open_via(candidate(open={"url": 3080})), "url:3080"
            )
        with patch.object(server, "port_listening", return_value=False):
            # 端口没人听就不给开，免得点开一个白页。
            self.assertEqual(
                server.discovered_open_via(candidate(open={"url": 3080})), ""
            )
        self.assertEqual(server.discovered_open_via(candidate(open=None)), "")
        # .app 包里的进程没有可切过去的 Terminal 标签页。
        self.assertEqual(
            server.discovered_open_via(
                candidate(open={"tty": True}, in_bundle=True)
            ),
            "",
        )

    def test_unopenable_light_still_acknowledges(self):
        agent = {
            "id": "x",
            "platform": "fakeagent",
            "status": "completed",
            "completionId": 5,
            "openable": False,
            "openVia": "",
            "satellites": [],
        }
        original = dict(server._acknowledged_completions)
        self.addCleanup(lambda: server._acknowledged_completions.update(original))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "STATE_PATH", Path(directory) / "state.json"):
                server._acknowledged_completions.clear()
                self.assertTrue(server.acknowledge_agent(agent))


class FamilyMetaTests(unittest.TestCase):
    def test_seed_families_reach_platform_meta(self):
        meta = server.platform_meta("dsh")
        self.assertEqual(meta["label"], "DeepSeek Harness")
        self.assertEqual(meta["order"], 10)
        self.assertEqual(meta["kind"], "discovered")

    def test_unknown_family_falls_back_to_capitalised_key(self):
        meta = server.platform_meta("nosuchfamily")
        self.assertEqual(meta["label"], "Nosuchfamily")
        self.assertEqual(meta["order"], 90)

    def test_family_meta_rejects_an_unknown_field(self):
        # 以前写错键名（empty_text vs emptyText）是无声丢弃，界面上只是少一行字。
        original = server.FAMILY_META.get("typo")
        self.addCleanup(
            lambda: server.FAMILY_META.pop("typo", None)
            if original is None
            else server.FAMILY_META.update({"typo": original})
        )
        server.FAMILY_META["typo"] = {"label": "Typo", "empty_text": "写错了"}
        with self.assertRaises(ValueError):
            server.platform_meta("typo")

    def test_register_family_meta_is_idempotent(self):
        self.addCleanup(server.FAMILY_META.pop, "fakeagent", None)
        item = candidate(family="fakeagent", label="Fakeagent", order=90)
        server.register_family_meta(item)
        first = dict(server.FAMILY_META["fakeagent"])
        server.register_family_meta(item)
        self.assertEqual(server.FAMILY_META["fakeagent"], first)
        self.assertEqual(server.platform_meta("fakeagent")["label"], "Fakeagent")


class DiscoveredSpecTests(unittest.TestCase):
    def test_spec_is_wired_for_task_six(self):
        spec = server.source_for("discovered")
        self.assertIsNotNone(spec)
        self.assertIsNone(spec.history_targets)
        self.assertIsNone(spec.cost_mode)
        self.assertEqual(spec.cost_label, "—")
        self.assertFalse(spec.is_platform)

    def test_history_pass_skips_sources_without_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            # 只要不抛异常就说明 history_targets=None 被正确跳过了。
            server.history_pass(Path(directory) / "history.db")

    def test_fanout_source_section_only_appears_when_unhealthy(self):
        healthy = server.aggregate_platforms(
            [("discovered", [], {"state": "live", "detail": "已识别 0 个家族"})]
        )
        self.assertEqual(healthy, [])
        broken = server.aggregate_platforms(
            [("discovered", [], {"state": "unavailable", "detail": "ps 挂了"})]
        )
        self.assertEqual([entry["key"] for entry in broken], ["discovered"])
        self.assertEqual(broken[0]["health"]["detail"], "ps 挂了")


class DiscoverySourceTests(unittest.TestCase):
    def setUp(self):
        server._discovery_state.clear()
        server._discovery_transitions.clear()
        server._discovery_cwd.clear()
        server._activity.clear()
        discovery.reset_caches()
        self.addCleanup(server._discovery_state.clear)
        self.addCleanup(server._discovery_transitions.clear)
        self.addCleanup(server._discovery_cwd.clear)
        self.addCleanup(server._activity.clear)
        self.addCleanup(discovery.reset_caches)

    def test_health_counts_families_and_processes(self):
        rows = table(
            [
                (5001, 1, UID, DSH_NODE, 1_000),
                (5002, 1, UID, OPENCLAW_NODE, 1_000),
                (5003, 1, UID, CLAUDE_LOCAL, 1_000),
            ]
        )
        with patch.object(server, "process_cwd", return_value=""), patch.object(
            server, "watch_mtime", return_value=0.0
        ), patch.object(server, "claude_claimed_pids", return_value=set()):
            agents, health = server.load_discovered(1_700_000_000_000, rows)
        self.assertEqual(health["state"], "live")
        self.assertEqual(health["detail"], "已识别 3 个家族 / 3 个进程")
        self.assertEqual(
            sorted(agent["platform"] for agent in agents),
            ["claude", "dsh", "openclaw"],
        )

    def test_truncation_is_reported_in_health(self):
        rows = table(
            [(6000 + index, 1, UID, CLAUDE_LOCAL, 1_000) for index in range(30)]
        )
        with patch.object(server, "process_cwd", return_value=""), patch.object(
            server, "watch_mtime", return_value=0.0
        ), patch.object(server, "claude_claimed_pids", return_value=set()):
            agents, health = server.load_discovered(1_700_000_000_000, rows)
        self.assertEqual(len(agents), server.DISCOVERY_MAX_AGENTS)
        self.assertIn("已截断", health["detail"])

    def test_disabled_by_env_flag(self):
        with patch.object(server, "DISCOVERY_ENABLED", False):
            agents, health = server.load_discovered(1, {"commands": {1: "x"}})
        self.assertEqual(agents, [])
        self.assertEqual(health, {"state": "live", "detail": "自动发现已关闭"})

    def test_panel_pid_lands_in_the_claimed_set(self):
        rows = table([(os.getpid(), 1, UID, PANEL)])
        with patch.object(server, "claude_claimed_pids", return_value=set()):
            self.assertIn(os.getpid(), server.discovery_claimed(rows))

    def test_cwd_cache_drops_dead_pids(self):
        server._discovery_cwd[999_999] = "/tmp/gone"
        rows = table([(5001, 1, UID, DSH_NODE, 1_000)])
        with patch.object(server, "process_cwd", return_value=""), patch.object(
            server, "watch_mtime", return_value=0.0
        ), patch.object(server, "claude_claimed_pids", return_value=set()):
            server.load_discovered(1_700_000_000_000, rows)
        self.assertNotIn(999_999, server._discovery_cwd)


class WatchMtimeTests(unittest.TestCase):
    def setUp(self):
        server._watch_cache.clear()
        self.addCleanup(server._watch_cache.clear)

    def test_watch_mtime_takes_the_newest_match(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "storages").mkdir()
            old = home / "storages" / "a.json"
            new = home / "storages" / "b.json"
            old.write_text("{}", encoding="utf-8")
            new.write_text("{}", encoding="utf-8")
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (2_000_000, 2_000_000))
            self.assertEqual(
                server.watch_mtime(str(home), ("storages/*.json",)), 2_000_000.0
            )

    def test_no_home_means_no_signal(self):
        self.assertEqual(server.watch_mtime("", ("*.json",)), 0.0)


if __name__ == "__main__":
    unittest.main()

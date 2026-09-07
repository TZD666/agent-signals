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
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
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
CHROME_MAIN = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
FRAMEWORK_PYTHON = (
    "/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/"
    "Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
)
CHROME_CRASHPAD = (
    "/Applications/Google Chrome.app/Contents/Frameworks/"
    "Google Chrome Framework.framework/Versions/152.0.7977.65/Helpers/"
    "chrome_crashpad_handler --monitor-self-annotation=ptype=crashpad-handler "
    "--database=/Users/edy/Library/Application Support/Google/Chrome/Crashpad"
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
# 以下五条是 2026-09-07 用户真开着 WorkBuddy 时抓的原样 ps 行。
WORKBUDDY_MAIN_REAL = "/Applications/WorkBuddy.app/Contents/MacOS/Electron"
WORKBUDDY_CRASHPAD = (
    "/Applications/WorkBuddy.app/Contents/Frameworks/Electron Framework.framework/"
    "Helpers/chrome_crashpad_handler --no-rate-limit --no-upload-gzip "
    "--monitor-self-annotation=ptype=crashpad-handler "
    "--database=/Users/edy/.workbuddy/app/Crashpad "
    "--url=https://galileotelemetry.tencent.com/crashReport"
)
WORKBUDDY_SANDBOX = (
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/vendor/"
    'sandbox/5.5.5/sandbox-center --config {"appHome":"/Users/edy/.workbuddy",'
    '"cipher":{"detectionEnabled":true}} --app_home /Users/edy/.workbuddy'
)
WORKBUDDY_CODEBUDDY = (
    "/Applications/WorkBuddy.app/Contents/MacOS/Electron "
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/"
    "codebuddy --serve --session-id 251af04a-0000-4000-8000-000000000000 "
    "--model hy4-preview"
)
# 只是在参数里提到 ~/.workbuddy/ 的旁观者：两个插件的 MCP server，和一个
# 采集 shell 环境的临时 zsh。都不是 agent。
WORKBUDDY_PLUGIN_MCP = (
    "/Users/edy/.workbuddy/binaries/node/v22.21.1/bin/node "
    "/Users/edy/.workbuddy/plugins/cache/workbuddy-builtin/weixinpay/1.6.109/"
    "dist/mcp-server.mjs"
)
WORKBUDDY_PLUGIN_MCP_TWO = (
    "node /Users/edy/.workbuddy/plugins/cache/workbuddy-builtin/sheetagent/"
    "1.2.0/mcp/start.mjs"
)
WORKBUDDY_SNAPSHOT_ZSH = (
    "/bin/zsh -c -l SNAPSHOT_FILE='/Users/edy/.workbuddy/shell-snapshots/"
    "snapshot-zsh-1757-abc.sh' && command -v cat"
)
PARENT_SID = "af37fb6d-5c6b-4e6e-8642-736b4798f617"
TEAMMATE = (
    "/Users/edy/.local/share/claude/versions/2.1.260 "
    "--agent-id gh-search@session-af37fb6d --agent-name gh-search "
    "--team-name session-af37fb6d --agent-color blue "
    f"--parent-session-id {PARENT_SID} "
    "--agent-type general-purpose --permission-mode acceptEdits --model sonnet"
)
TEAMMATE_TWO = TEAMMATE.replace("gh-search", "hf-search").replace("blue", "green")
DSH_NODE = (
    "/opt/homebrew/Cellar/node/24.4.0/bin/node "
    "/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web --no-open"
)
OPENCLAW_NODE = (
    "/opt/homebrew/bin/node /Users/edy/work/node_modules/openclaw/dist/cli.js gateway"
)


class FakeLinks:
    """本机 /opt/homebrew/bin 里那几个 npm 全局软链的真实指向。

    夹具不去碰真实文件系统：CI 或别人的机器上这些包不一定装着。
    `/opt/homebrew/bin/amp` 本机没装，按 npm 的固定布局写。
    """

    LINKS = {
        "/opt/homebrew/bin/dsh": (
            "/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"
        ),
        "/opt/homebrew/bin/openclaw": (
            "/opt/homebrew/lib/node_modules/openclaw/openclaw.mjs"
        ),
        "/opt/homebrew/bin/amp": (
            "/opt/homebrew/lib/node_modules/@sourcegraph/amp/dist/index.js"
        ),
    }

    def get_or_self(self, path):
        return self.LINKS.get(path, path)


FAKE_LINKS = FakeLinks()


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
        # 解释器自己不说明身份，脚本名才说明：完整 basename 与去后缀的 stem
        # 都参与匹配，但 stem 是弱证据。
        self.assertEqual(argv.names, ("mcp-server.cjs", "mcp-server"))
        self.assertEqual(argv.weak_names, frozenset({"mcp-server"}))
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
        self.assertEqual(argv.names, ("run.py", "run"))
        self.assertFalse(argv.in_bundle)
        # 解释器自己的 bundle 不是 GUI 应用：连 bundle_anywhere 都不该亮，
        # 否则 dotdir 规则会把所有 python 写的 agent 一并豁免掉。
        self.assertFalse(argv.bundle_anywhere)
        self.assertTrue(discovery.is_interpreter("Python"))
        self.assertTrue(discovery.is_interpreter("python3.14"))
        self.assertFalse(discovery.is_interpreter("pythonista"))

    def test_bundle_flag_survives_spaces_in_the_app_path(self):
        # ps 把 argv 用空格拼平了：`command.split()[0]` 得到的是
        # `/Applications/Google`，`.app/Contents/` 永远匹配不上。这道保险
        # 之前从来没生效过。
        argv = discovery.parse_argv(CHROME_MAIN)
        self.assertEqual(argv.exe, "/Applications/Google")
        self.assertNotIn(discovery.BUNDLE_MARKER, argv.exe)
        self.assertTrue(argv.in_bundle)
        self.assertTrue(argv.bundle_anywhere)

    def test_in_app_bundle_tells_gui_apps_from_interpreter_bundles(self):
        self.assertTrue(discovery.in_app_bundle(CHROME_MAIN))
        self.assertTrue(discovery.in_app_bundle(CHATGPT_APP))
        self.assertTrue(discovery.in_app_bundle(CLAUDE_DESKTOP_BUNDLED))
        self.assertTrue(discovery.in_app_bundle(CLAUDE_APP_CRASHPAD))
        # Python.app 住在 Python.framework 里，是解释器自己的壳，不是 GUI 应用。
        self.assertFalse(discovery.in_app_bundle(FRAMEWORK_PYTHON))
        self.assertFalse(discovery.in_app_bundle(PANEL.split()[0]))
        self.assertFalse(discovery.in_app_bundle("/opt/homebrew/bin/node"))
        self.assertFalse(discovery.in_app_bundle(""))

    def test_gui_app_interpreter_running_a_user_script_is_bundled_anywhere(self):
        # 某个 GUI 应用用自带的 node 跑一个 ~/.foo/x.mjs：脚本不在包里，
        # 但这个进程照样不该按 .foo 推出一个家族来。
        command = (
            "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node "
            "/Users/edy/.fakeagent/x.mjs"
        )
        argv = discovery.parse_argv(command)
        self.assertFalse(argv.in_bundle)
        self.assertTrue(argv.bundle_anywhere)

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

    def test_seed_matches_a_global_npm_shim_without_a_suffix(self):
        # 真机实测的那条命令行（`dsh web --no-open` 起来之后 ps 就长这样）。
        # npm 全局装的 CLI 是一个**没有扩展名**的软链：
        #   /opt/homebrew/bin/dsh -> ../lib/node_modules/@deepseek-ai/dsh/lib/bin.js
        # 只认 .js/.mjs/.cjs/.py 的话它连「脚本」都算不上，basename `dsh`
        # 永远匹配不上；而 ps 显示的是软链本身，包路径片段也不在命令行里。
        command = "node /opt/homebrew/bin/dsh web --no-open"
        argv = discovery.parse_argv(command)
        self.assertEqual(argv.script, "/opt/homebrew/bin/dsh")
        self.assertEqual(argv.names, ("dsh",))
        self.assertEqual(argv.weak_names, frozenset())
        self.assertIn(
            "@deepseek-ai/dsh/",
            discovery.paths_text(argv, resolve=FAKE_LINKS.get_or_self),
        )
        self.assertEqual(
            discovery.match_seed(
                argv,
                discovery.DEFAULT_PROFILES["families"],
                resolve=FAKE_LINKS.get_or_self,
            ),
            "dsh",
        )

    def test_seed_matches_an_mjs_symlink_shim(self):
        # openclaw 的软链指向 .mjs，形状与 dsh 不同，两条路都要走通。
        command = "node /opt/homebrew/bin/openclaw gateway"
        argv = discovery.parse_argv(command)
        self.assertEqual(argv.names, ("openclaw",))
        self.assertIn(
            "node_modules/openclaw/",
            discovery.paths_text(argv, resolve=FAKE_LINKS.get_or_self),
        )
        self.assertEqual(
            discovery.match_seed(
                argv,
                discovery.DEFAULT_PROFILES["families"],
                resolve=FAKE_LINKS.get_or_self,
            ),
            "openclaw",
        )

    def test_path_fragment_only_families_need_the_symlink_resolved(self):
        # amp / pi 禁止裸名匹配，路径片段是它们唯一的路：不解析软链就是
        # 100% 发现不了。
        argv = discovery.parse_argv("node /opt/homebrew/bin/amp --execute")
        self.assertEqual(
            discovery.match_seed(
                argv,
                discovery.DEFAULT_PROFILES["families"],
                resolve=lambda path: path,
            ),
            "",
        )
        self.assertEqual(
            discovery.match_seed(
                argv,
                discovery.DEFAULT_PROFILES["families"],
                resolve=FAKE_LINKS.get_or_self,
            ),
            "amp",
        )

    def test_realpath_failure_falls_back_to_the_unresolved_path(self):
        def boom(path):
            raise OSError("没这个文件")

        # 解析失败不能把整轮采样带崩；退回未解析的路径继续匹配。
        argv = discovery.parse_argv(
            "node /opt/homebrew/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"
        )
        self.assertEqual(
            discovery.match_seed(
                argv,
                discovery.DEFAULT_PROFILES["families"],
                resolve=lambda path: path,
            ),
            "dsh",
        )
        with patch.object(discovery.os.path, "realpath", side_effect=boom):
            discovery._realpath_cache.clear()
            self.addCleanup(discovery._realpath_cache.clear)
            self.assertEqual(
                discovery.resolve_path("/opt/homebrew/bin/dsh"),
                "/opt/homebrew/bin/dsh",
            )
            fallback = discovery.parse_argv("node /opt/homebrew/bin/dsh web")
        # 软链没解析开，就只剩 basename 那条路——它照样把 dsh 认出来。
        self.assertEqual(
            discovery.match_seed(fallback, discovery.DEFAULT_PROFILES["families"]),
            "dsh",
        )

    def test_resolve_path_leaves_relative_paths_alone(self):
        # 相对路径会按面板自己的 cwd 解析，那是错的。
        self.assertEqual(discovery.resolve_path("./server.mjs"), "./server.mjs")
        self.assertEqual(discovery.resolve_path("node"), "node")

    def test_resolve_path_caches_by_string(self):
        discovery._realpath_cache.clear()
        self.addCleanup(discovery._realpath_cache.clear)
        with patch.object(
            discovery.os.path, "realpath", return_value="/resolved"
        ) as real:
            for _ in range(5):
                discovery.resolve_path("/opt/homebrew/bin/dsh")
        # 一个运行中进程的路径不会变：热路径上一次文件系统都不该再碰。
        self.assertEqual(real.call_count, 1)

    def test_inline_code_is_never_mistaken_for_a_script(self):
        argv = discovery.parse_argv(
            "python3 -c import sys; sys.path.append('/opt/claude/x')"
        )
        self.assertEqual(argv.script, "")
        self.assertEqual(argv.names, ())

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

    def test_script_stem_never_matches_a_native_family(self):
        # `python3 ~/work/claude.py` 以前会命中 claude，在原生 Claude 分区里
        # 冒出一盏 tty 可点的假灯。这个用户满机器都是自己写的脚本。
        rows = [(8001, 1, UID, "python3 /Users/edy/work/claude.py")]
        native = [spec.key for spec in server.SOURCES]
        result = discovery.classify(
            table(rows),
            discovery.DEFAULT_PROFILES,
            discovery.DEFAULT_PROFILES["ignore"],
            set(),
            HOME,
            UID,
            native=native,
        )
        self.assertEqual(result.candidates, [])
        # 不给 native 时（纯模块单跑）才是老行为，非原生家族仍然认脚本名。
        self.assertEqual(families(run(rows)), ["claude"])
        hermes = [(8002, 1, UID, "python3 /Users/edy/work/hermes.py")]
        self.assertEqual(
            families(
                discovery.classify(
                    table(hermes),
                    discovery.DEFAULT_PROFILES,
                    discovery.DEFAULT_PROFILES["ignore"],
                    set(),
                    HOME,
                    UID,
                    native=native,
                )
            ),
            ["hermes"],
        )

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

    def test_crash_reporters_are_never_agents(self):
        # 真机：WorkBuddy 的 crashpad 命中种子的 .app 路径片段，又被 macOS
        # 重挂到 launchd（ppid=1），roll-up 够不着 → 面板多出整整一盏灯。
        result = run([(15397, 1, UID, WORKBUDDY_CRASHPAD)])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.unclassified, [])
        # 黑名单要认的是路径区最后那一段：带空格的 .app 路径切出来的 argv[0]
        # 是 `/Applications/WorkBuddy.app/Contents/Frameworks/Electron`。
        argv = discovery.parse_argv(WORKBUDDY_CRASHPAD)
        self.assertEqual(argv.basename, "Electron")
        self.assertEqual(argv.region_basename, "chrome_crashpad_handler")
        # Chrome / ChatGPT.app 的 crashpad 同样被挡住。
        self.assertEqual(run([(1624, 1, UID, CHROME_CRASHPAD)]).candidates, [])

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

    def test_dotdir_never_forks_a_family_that_already_has_a_seed(self):
        # 真机实测：WorkBuddy 开着的时候，两个插件的 MCP server 和一个采集
        # shell 环境的临时 zsh 只是在参数里**提到**了 ~/.workbuddy/，就各自
        # 点亮了一盏灯。它们不是 agent。
        bystanders = [
            (15900, 1, UID, WORKBUDDY_PLUGIN_MCP),
            (15901, 1, UID, WORKBUDDY_PLUGIN_MCP_TWO),
            (15918, 1, UID, WORKBUDDY_SNAPSHOT_ZSH),
        ]
        # 没有 WorkBuddy 那盏灯时：整条丢掉，一盏都不出。
        self.assertEqual(run(bystanders).candidates, [])
        # WorkBuddy 在跑时：并进那盏灯当成员，仍然只有一盏灯。
        result = run([(15116, 1, UID, WORKBUDDY_MAIN_REAL, 500)] + bystanders)
        self.assertEqual(families(result), ["workbuddy"])
        candidate = result.candidates[0]
        self.assertEqual(candidate.pid, 15116)
        self.assertEqual(candidate.label, "WorkBuddy")
        # 身份完全来自种子画像，不会出现目录名推出来的 `Workbuddy`（小写 b）。
        self.assertEqual(candidate.home_label, "~/.workbuddy")
        self.assertEqual(candidate.open, {"app": "WorkBuddy"})
        for pid in (15900, 15901, 15918):
            self.assertIn(pid, candidate.members)

    def test_a_brand_new_dotdir_family_still_gets_its_own_light(self):
        # 上面那条只针对「种子表里已经有的家族」。现学的家族不受影响。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".fakeagent" / "sessions").mkdir(parents=True)
            result = discovery.classify(
                table([(100, 1, UID, f"python3 {home}/.fakeagent/run.py")]),
                discovery.DEFAULT_PROFILES,
                discovery.DEFAULT_PROFILES["ignore"],
                set(),
                home,
                UID,
            )
        self.assertEqual(families(result), ["fakeagent"])

    def test_gui_app_bundled_interpreter_never_infers_a_dotdir_family(self):
        # 反向的洞：脚本不在包里，身份判据（in_bundle）就是 False，
        # 光看它的话 `~/.fakeagent` 会被推成一个家族。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".fakeagent").mkdir()
            (home / ".fakeagent" / "sessions").mkdir()
            gui = (
                "/Applications/SomeApp.app/Contents/Resources/node "
                f"{home}/.fakeagent/x.mjs"
            )
            plain = f"/opt/homebrew/bin/node {home}/.fakeagent/x.mjs"

            def classify(command, pid):
                return discovery.classify(
                    table([(pid, 1, UID, command)]),
                    discovery.DEFAULT_PROFILES,
                    discovery.DEFAULT_PROFILES["ignore"],
                    set(),
                    home,
                    UID,
                )

            self.assertEqual(classify(gui, 900).candidates, [])
            # 同一个脚本换个正常的 node 跑就该出来，证明拦掉它的是 GUI 包。
            self.assertEqual(families(classify(plain, 901)), ["fakeagent"])

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

    def test_roll_up_only_merges_the_same_family(self):
        # 这台机器的日常主力工作流就是 claude 拉一个 codex exec。
        # 不看家族地并会把 codex 那盏灯整个吃掉，这条路上永远看不到它。
        rows = [
            (100, 1, UID, CLAUDE_LOCAL),
            (200, 100, UID, "/opt/homebrew/bin/codex exec 修一下这个 bug"),
        ]
        result = run(rows)
        self.assertEqual(families(result), ["claude", "codex"])
        by_pid = {c.pid: c for c in result.candidates}
        self.assertEqual(by_pid[100].members, (100,))
        self.assertEqual(by_pid[200].members, (200,))

    def test_roll_up_does_not_reach_across_a_foreign_candidate(self):
        rows = [
            (100, 1, UID, CLAUDE_LOCAL),
            (200, 100, UID, "/opt/homebrew/bin/codex exec x"),
            (300, 200, UID, CLAUDE_LOCAL),
        ]
        result = run(rows)
        # 中间隔着一个异族候选，不再往上认亲：三盏灯。
        self.assertEqual(sorted(c.pid for c in result.candidates), [100, 200, 300])

    def test_same_app_bundle_merges_without_a_parent_chain(self):
        # macOS 把 helper 重挂到 launchd 之后父子链是断的，roll-up 够不着。
        # 同 family + 同 .app 包必须仍然收成一盏。
        rows = [
            (15116, 1, UID, WORKBUDDY_MAIN_REAL, 500),
            (16344, 15116, UID, WORKBUDDY_SANDBOX, 800),
            # 故意让它 ppid=1，且比主进程还老，验证根取最老的那个。
            (15100, 1, UID, WORKBUDDY_CODEBUDDY, 400),
        ]
        result = run(rows)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.pid, 15100)
        self.assertEqual(candidate.members, (15100, 15116, 16344))

    def test_app_bundle_merge_does_not_cross_families_or_apps(self):
        rows = [
            (15116, 1, UID, WORKBUDDY_MAIN_REAL, 500),
            (22118, 1, UID, CLAUDE_APP_MAIN, 500),
            (5001, 1, UID, DSH_NODE, 500),
        ]
        # Claude.app 不是 agent（谁都不认它），dsh 不在任何 .app 包里。
        self.assertEqual(families(run(rows)), ["dsh", "workbuddy"])

    def test_app_bundle_root_ignores_interpreter_shells(self):
        self.assertEqual(
            discovery.app_bundle_root(WORKBUDDY_MAIN_REAL),
            "/Applications/WorkBuddy.app",
        )
        self.assertEqual(
            discovery.app_bundle_root(discovery.parse_argv(WORKBUDDY_CODEBUDDY).region),
            "/Applications/WorkBuddy.app",
        )
        self.assertEqual(discovery.app_bundle_root(FRAMEWORK_PYTHON), "")
        self.assertEqual(discovery.app_bundle_root("/opt/homebrew/bin/node"), "")

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
    def test_workbuddy_watches_traces_not_logs_or_heartbeats(self):
        # 本机空转 120 秒实测：logs/*.log 12 个间隔里变了 8 次（renderer.log
        # 每几秒就写），sessions/*.json 变了 4 次（整 30 秒一次的心跳），
        # traces/*/trace_*.json 一次没变。前两个说的是「应用开着」。
        watch = discovery.DEFAULT_PROFILES["families"]["workbuddy"]["watch"]
        self.assertEqual(watch, ["traces/*/trace_*.json"])

    def test_default_watch_has_no_log_glob(self):
        # 同一个陷阱对任何自动学出来的家族都成立。
        self.assertNotIn("**/*.log", discovery.DEFAULT_WATCH)
        self.assertFalse([p for p in discovery.DEFAULT_WATCH if p.endswith(".log")])

    def test_a_heartbeat_file_would_pin_the_light_blue(self):
        # 为什么必须去掉：只要 watch 里有个每轮都在变的文件，quietSince 每轮
        # 被刷新，now - quietSince 永远 < ACTIVE_MS，busy→idle 那一跳永远不
        # 发生，绿灯在物理上不可能出现。
        server._activity.clear()
        self.addCleanup(server._activity.clear)
        base = 1_700_000_000_000
        statuses = []
        for step in range(12):
            now = base + step * 5_000
            # 模拟一个每半秒就被重写的日志文件。
            quiet = server.note_activity("demo", 0.0, now / 1000.0, now)
            statuses.append(now - quiet < server.DISCOVERY_ACTIVE_MS)
        self.assertTrue(all(statuses), "文件一直在动 → 每轮都算 busy")

    def test_expand_watch_bounds_the_depth(self):
        self.assertEqual(
            discovery.expand_watch("**/*.jsonl"),
            ("*.jsonl", "*/*.jsonl", "*/*/*.jsonl"),
        )
        self.assertEqual(discovery.expand_watch("storages/*.json"), ("storages/*.json",))


# ---------------------------------------------------------------------------
# server 侧：DiscoveredSource 的状态机
# ---------------------------------------------------------------------------


def discovered_light(platform, **overrides):
    """载荷里的一盏发现型的灯（`origin: "process"`）。"""
    light = {
        "id": "7777-1699996400",
        "pid": 7777,
        "platform": platform,
        "name": "x",
        "status": "completed",
        "detail": "自动发现",
        "cwd": "",
        "cwdLabel": "未知目录",
        "updatedAt": 1_000,
        "quietSince": 1_000,
        "completionId": 4,
        "openable": True,
        "origin": "process",
        "openVia": "tty",
        "satellites": [],
    }
    light.update(overrides)
    return light


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

    def test_desktop_bundled_claude_is_labelled_and_opens_the_app(self):
        # 验收项 (e)：登记表没写时，桌面会话也要经捆绑路径规则出现在 Claude
        # 分区并标「桌面 App」。它同时是一颗地雷：这盏灯以前 openVia='tty'，
        # 点下去 terminal_tty 抛错走 500，而 500 发生在 acknowledge_agent
        # 之前，绿灯就再也清不掉了。
        desktop = candidate(
            family="claude",
            label="Claude",
            command=CLAUDE_DESKTOP_BUNDLED,
            exe=CLAUDE_DESKTOP_BUNDLED,
            home_label="~/.claude",
            open={"tty": True},
            bundle_anywhere=True,
        )
        agent = self.sample(1_700_000_000_000, item=desktop)
        self.assertEqual(agent["detail"], "桌面 App")
        self.assertEqual(agent["openVia"], "app:Claude")
        self.assertTrue(agent["openable"])
        self.assertEqual(agent["platform"], "claude")
        self.assertEqual(agent["origin"], "process")
        # 这条 openVia 必须真的能开——`open -a` 的白名单里有 Claude。
        with patch.object(
            server.subprocess, "run", return_value=Mock(returncode=0)
        ) as run_:
            server.open_discovered(agent)
        self.assertEqual(run_.call_args[0][0], ["open", "-a", "Claude"])

    def test_gui_bundle_light_is_never_promised_a_terminal_tab(self):
        item = candidate(open={"tty": True}, bundle_anywhere=True)
        self.assertEqual(server.discovered_open_via(item), "")
        agent = self.sample(1_700_000_000_000, item=item)
        self.assertFalse(agent["openable"])

    def test_a_gui_app_light_ignores_the_cpu_signal(self):
        # 实测 WorkBuddy 空转 24 个 5 秒窗口，11 个越过 CPU_EPSILON、峰值 1.0s。
        # 留着 CPU 信号，这盏灯会每隔半分钟假装完成一次。
        gui = candidate(family="workbuddy", label="WorkBuddy", bundle_anywhere=True)
        base = 1_700_000_000_000
        seen = set()
        for step in range(8):
            # CPU 一路猛涨，但它住在 .app 包里 → 一律当安静。
            seen.add(self.sample(base + step * 5_000, cpu=float(step), item=gui)["status"])
        self.assertEqual(seen, {"idle"})

    def test_a_terminal_agent_still_uses_the_cpu_signal(self):
        cli = candidate(bundle_anywhere=False)
        base = 1_700_000_000_000
        self.sample(base, cpu=0.0, item=cli)
        self.sample(base + 5_000, cpu=1.0, item=cli)
        self.assertEqual(
            self.sample(base + 10_000, cpu=2.0, item=cli)["status"], "thinking"
        )

    def test_a_useless_cwd_does_not_get_pasted_into_the_name(self):
        # 真机：WorkBuddy 主进程的 cwd 就是 `/`，灯叫「WorkBuddy · /」。
        item = candidate(family="workbuddy", label="WorkBuddy", home_label="~/.workbuddy")
        with patch.object(server, "process_cwd", return_value="/"):
            agent = self.sample(1_700_000_000_000, item=item)
        self.assertEqual(agent["name"], "WorkBuddy")

    def test_a_member_cwd_is_used_when_the_root_has_none(self):
        item = candidate(
            family="workbuddy", label="WorkBuddy", pid=15116, members=(15116, 15890)
        )
        cwds = {15116: "/", 15890: "/Users/edy/WorkBuddy/2026-09-07-16-25-42"}
        with patch.object(server, "process_cwd", side_effect=cwds.get):
            agent = self.sample(1_700_000_000_000, item=item)
        self.assertEqual(agent["name"], "WorkBuddy · 2026-09-07-16-25-42")

    def test_bundle_and_temp_cwds_are_not_useful(self):
        self.assertFalse(server.useful_cwd("/"))
        self.assertFalse(server.useful_cwd(""))
        self.assertFalse(
            server.useful_cwd(
                "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/x"
            )
        )
        self.assertFalse(
            server.useful_cwd("/private/var/folders/zn/T/workbuddy-host-cli/x")
        )
        # 家目录仍然算数：dsh 那盏「DeepSeek Harness · ~」不该被这条动到。
        self.assertTrue(server.useful_cwd(str(Path.home())))
        self.assertTrue(server.useful_cwd("/Users/edy/WorkBuddy/2026-09-07-16-25-42"))

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
                candidate(open={"tty": True}, bundle_anywhere=True)
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


def claude_light(session_id, satellites=None):
    """一盏登记表出的前台 Claude 灯（形状取自 load_claude_sessions）。"""
    return {
        "id": session_id,
        "pid": 29796,
        "platform": "claude",
        "name": "edy-b0",
        "status": "thinking",
        "detail": "Terminal",
        "cwd": "/Users/edy/work",
        "cwdLabel": "work",
        "updatedAt": 1_000,
        "quietSince": 1_000,
        "completionId": 0,
        "openable": True,
        "origin": "registry",
        "openVia": "tty",
        "load": {"contextTokens": 1000, "contextWindow": 200000,
                 "contextPct": 1, "stepGapMs": None},
        "satellites": list(satellites or []),
    }


class TeamFlagTests(unittest.TestCase):
    def test_team_flags_reads_the_real_command_line(self):
        name, parent = discovery.team_flags(TEAMMATE)
        self.assertEqual(name, "gh-search")
        self.assertEqual(parent, PARENT_SID)

    def test_team_flags_accepts_the_equals_form(self):
        name, parent = discovery.team_flags(
            "claude --agent-name=exec-runway --parent-session-id=7b582fcb"
        )
        self.assertEqual((name, parent), ("exec-runway", "7b582fcb"))

    def test_team_flags_is_empty_for_an_ordinary_session(self):
        self.assertEqual(discovery.team_flags(CLAUDE_LOCAL), ("", ""))
        self.assertEqual(discovery.team_flags(DSH_NODE), ("", ""))

    def test_a_flag_without_a_value_is_not_a_value(self):
        self.assertEqual(
            discovery.flag_value("claude --agent-name --parent-session-id x", "--agent-name"),
            "",
        )

    def test_candidate_carries_the_team_identity(self):
        result = run([(71086, 71074, UID, TEAMMATE)])
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.family, "claude")
        self.assertEqual(candidate.agent_name, "gh-search")
        self.assertEqual(candidate.parent_session, PARENT_SID)


class TeammateSatelliteTests(unittest.TestCase):
    """天幕低语排出去的 teammate 应该环绕在父灯周围，不是另起一张卡。"""

    def setUp(self):
        server._discovery_state.clear()
        server._discovery_transitions.clear()
        server._discovery_cwd.clear()
        server._activity.clear()
        for target in (
            server._discovery_state,
            server._discovery_transitions,
            server._discovery_cwd,
            server._activity,
        ):
            self.addCleanup(target.clear)
        patches = [
            patch.object(server, "process_cwd", return_value="/Users/edy/memory"),
            patch.object(server, "watch_mtime", return_value=0.0),
            patch.object(server, "claude_claimed_pids", return_value=set()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def load(self, rows, hosts=None):
        context = {"loaded": [("claude", list(hosts or []), {"state": "live"})]}
        return server.load_discovered(1_700_000_000_000, table(rows), context)

    def test_teammate_becomes_a_satellite_of_its_parent_light(self):
        host = claude_light(PARENT_SID)
        agents, health = self.load(
            [
                (71086, 71074, UID, TEAMMATE, 1_000),
                (71174, 71074, UID, TEAMMATE_TWO, 1_000),
            ],
            [host],
        )
        # 一张卡都不该多出来。
        self.assertEqual(agents, [])
        self.assertEqual(
            [item["name"] for item in host["satellites"]], ["gh-search", "hf-search"]
        )
        self.assertTrue(all(s["origin"] == "teammate" for s in host["satellites"]))
        self.assertIn("2 个挂成卫星", health["detail"])

    def test_satellites_carry_no_load(self):
        host = claude_light(PARENT_SID)
        self.load([(71086, 71074, UID, TEAMMATE, 1_000)], [host])
        satellite = host["satellites"][0]
        self.assertNotIn("load", satellite)
        self.assertEqual(
            sorted(satellite), ["completionId", "id", "name", "origin", "status"]
        )

    def test_only_the_claude_family_ever_attaches_to_a_claude_light(self):
        # `--parent-session-id` 是 Claude 自己的会话 id 约定。别的家族哪怕
        # 也用这两个参数名，它的值也不是 Claude 的 sessionId。
        host = claude_light(PARENT_SID)
        foreign = candidate(
            family="dsh",
            label="DeepSeek Harness",
            agent_name="worker-1",
            parent_session=PARENT_SID,
        )
        self.assertFalse(server.attach_teammate({"id": "x"}, foreign, {PARENT_SID: host}))
        self.assertEqual(host["satellites"], [])
        native = candidate(family="claude", agent_name="gh-search", parent_session=PARENT_SID)
        self.assertTrue(
            server.attach_teammate({"id": "y", "name": "gh-search", "status": "idle",
                                    "completionId": 0}, native, {PARENT_SID: host})
        )
        self.assertEqual([s["name"] for s in host["satellites"]], ["gh-search"])

    def test_an_orphan_teammate_still_gets_its_own_light(self):
        # 父会话不在登记表里：找不到爹不是让一个真在跑的进程凭空消失的理由。
        agents, _health = self.load([(71086, 71074, UID, TEAMMATE, 1_000)], [])
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["platform"], "claude")
        self.assertEqual(agents[0]["name"], "gh-search")
        self.assertEqual(agents[0]["origin"], "process")

    def test_no_context_means_no_host_and_no_disappearing_act(self):
        # 老调用方（不传 context）行为不变：照样单独出灯。
        agents, _health = server.load_discovered(
            1_700_000_000_000, table([(71086, 71074, UID, TEAMMATE, 1_000)])
        )
        self.assertEqual([a["name"] for a in agents], ["gh-search"])

    def test_teammate_and_task_subagent_satellites_coexist(self):
        # Phase 2 的 Task 子代理卫星（origin=subagent）与 teammate 卫星
        # 挂在同一盏灯下，互不打架。
        existing = {
            "id": "agent-af7008f09f2924b4e",
            "name": "Explore · 盘点天幕低语公司资料",
            "status": "thinking",
            "completionId": 0,
            "origin": "subagent",
        }
        host = claude_light(PARENT_SID, [existing])
        self.load([(71086, 71074, UID, TEAMMATE, 1_000)], [host])
        self.assertEqual(
            [(s["name"], s["origin"]) for s in host["satellites"]],
            [
                ("Explore · 盘点天幕低语公司资料", "subagent"),
                ("gh-search", "teammate"),
            ],
        )

    def test_the_same_process_is_never_attached_twice(self):
        # 一个 id 一颗卫星：今天两类卫星的 id 空间不重叠，这道去重是把这条
        # 不变量钉死，免得将来某一边改了命名就悄悄出现两颗。
        host = claude_light(PARENT_SID)
        rows = [(71086, 71074, UID, TEAMMATE, 1_000)]
        self.load(rows, [host])
        self.assertEqual(len(host["satellites"]), 1)
        duplicate = dict(host["satellites"][0])
        duplicate["name"] = "别的名字"
        host["satellites"].append(duplicate)
        host["satellites"].pop(0)
        self.load(rows, [host])
        # 同一个 id 已经在了，不再追加第二颗。
        self.assertEqual(len(host["satellites"]), 1)

    def test_status_comes_from_the_activity_signal_not_the_transcript(self):
        host = claude_light(PARENT_SID)
        rows = [(71086, 71074, UID, TEAMMATE, 1_000)]
        with patch.object(server, "claude_load") as transcript:
            for _ in range(3):
                host["satellites"].clear()
                self.load(rows, [host])
        transcript.assert_not_called()
        self.assertEqual(host["satellites"][0]["status"], "idle")


class TeammateCountsTests(unittest.TestCase):
    """counts.satellites 要把 teammate 算进去，counts.agents 相应减少。"""

    def test_counts_move_from_agents_to_satellites(self):
        rows = table(
            [
                (71086, 71074, UID, TEAMMATE, 1_000),
                (71174, 71074, UID, TEAMMATE_TWO, 1_000),
            ]
        )
        host = claude_light(PARENT_SID)
        for target in (
            server._discovery_state,
            server._discovery_transitions,
            server._discovery_cwd,
            server._activity,
        ):
            target.clear()
            self.addCleanup(target.clear)
        with patch.object(server, "scan_processes", return_value=rows), patch.object(
            server, "load_claude_sessions", return_value=([host], {"state": "live", "detail": ""})
        ), patch.object(
            server, "load_codex_threads", return_value=([], {"state": "live", "detail": ""})
        ), patch.object(
            server, "process_cwd", return_value=""
        ), patch.object(
            server, "watch_mtime", return_value=0.0
        ), patch.object(
            server, "claude_claimed_pids", return_value=set()
        ):
            payload = server.snapshot()
        claude = next(p for p in payload["platforms"] if p["key"] == "claude")
        self.assertEqual(len(claude["agents"]), 1)
        self.assertEqual(len(claude["agents"][0]["satellites"]), 2)
        self.assertEqual(payload["counts"]["agents"], 1)
        self.assertEqual(payload["counts"]["satellites"], 2)
        self.assertEqual(payload["counts"]["byPlatform"], {"claude": 1, "codex": 0})


class OpenerRoutingTests(unittest.TestCase):
    """发现型的灯撞上原生 platform key 时，谁来开它。"""

    def test_discovered_light_on_a_native_key_uses_its_own_opener(self):
        # 终端里跑的 codex 就落在 Codex 分区里，但它的 id 是 pid-启动时刻。
        # 交给 Codex 的回调会变成 `open codex://threads/7777-1699996400`。
        light = discovered_light("codex")
        self.assertIs(server.opener_for("codex", light), server.open_discovered)
        light = discovered_light("claude")
        self.assertIs(server.opener_for("claude", light), server.open_discovered)

    def test_registry_light_still_uses_the_native_opener(self):
        # 注册表里的 open 是个 lambda，只能按行为验：谁最终被调到。
        registry = {"id": "sid-1", "pid": 42, "origin": "registry", "openVia": "tty"}
        for platform, name in (("claude", "open_claude"), ("codex", "open_codex")):
            with patch.object(server, name) as native:
                server.opener_for(platform, registry)(registry)
            native.assert_called_once_with(registry)
        # 没有 origin 的老形状按原生走，行为不变。
        legacy = {"id": "t"}
        with patch.object(server, "open_codex") as native:
            server.opener_for("codex", legacy)(legacy)
        native.assert_called_once_with(legacy)

    def test_light_without_an_entrance_has_no_opener(self):
        self.assertIsNone(
            server.opener_for("fakeagent", discovered_light("fakeagent", openVia=""))
        )
        self.assertIsNone(server.opener_for("fakeagent", {"id": "x"}))


class OpenEndpointTests(unittest.TestCase):
    """走真的 HTTP，确认路由没被原生 opener 截胡。"""

    def setUp(self):
        self.payload = {
            "schemaVersion": 2,
            "generatedAt": 1_000_000,
            "version": server.APP_VERSION,
            "sources": {"codex": {"state": "live", "detail": ""}},
            "notifications": {"state": "ok", "detail": ""},
            "platforms": [
                {
                    "key": "codex",
                    "label": "Codex",
                    "order": 1,
                    "kind": "local",
                    "hint": "",
                    "dismissible": True,
                    "lockable": True,
                    "emptyText": "",
                    "health": {"state": "live", "detail": ""},
                    "agents": [discovered_light("codex")],
                }
            ],
            "counts": {"agents": 1, "satellites": 0, "byPlatform": {"codex": 1}},
        }
        patches = [
            patch.object(server, "snapshot", return_value=self.payload),
            patch.object(server, "send_mac_notification"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        server._latest_snapshot = None
        server._latest_revision = ""
        server._locked_ids.clear()

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        state = patch.object(
            server, "STATE_PATH", Path(self.directory.name) / "state.json"
        )
        state.start()
        self.addCleanup(state.stop)
        original = dict(server._acknowledged_completions)
        self.addCleanup(server._acknowledged_completions.update, original)
        server._acknowledged_completions.clear()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        thread = Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/open",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # 本机系统代理会劫持回环，测试必须直连。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read())

    def test_discovered_codex_light_opens_a_terminal_not_a_codex_thread(self):
        with patch.object(server, "open_codex") as codex, patch.object(
            server, "open_terminal_tab"
        ) as terminal:
            body = self.post({"platform": "codex", "id": "7777-1699996400"})
        codex.assert_not_called()
        terminal.assert_called_once_with(7777)
        self.assertEqual(body, {"ok": True, "opened": True, "acknowledged": True})

    def test_an_entranceless_discovered_light_only_acknowledges(self):
        self.payload["platforms"][0]["agents"] = [
            discovered_light("codex", openVia="", openable=False)
        ]
        with patch.object(server, "open_codex") as codex, patch.object(
            server, "open_terminal_tab"
        ) as terminal:
            body = self.post({"platform": "codex", "id": "7777-1699996400"})
        codex.assert_not_called()
        terminal.assert_not_called()
        self.assertEqual(body, {"ok": True, "opened": False, "acknowledged": True})


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

    def test_global_npm_shim_becomes_a_light_with_a_url_entrance(self):
        # 端到端：真机 `dsh web --no-open` 的那条 ps 行 → 一盏挂在 dsh 分区、
        # 点得开 http://127.0.0.1:3080 的灯。验收项 (a) 就是这条。
        rows = table([(84760, 1, UID, "node /opt/homebrew/bin/dsh web --no-open", 1_000)])
        with patch.object(server, "process_cwd", return_value="/Users/edy/work"), (
            patch.object(server, "watch_mtime", return_value=0.0)
        ), patch.object(server, "claude_claimed_pids", return_value=set()), (
            patch.object(server, "port_listening", return_value=True)
        ), patch.object(
            discovery, "resolve_path", side_effect=FAKE_LINKS.get_or_self
        ):
            agents, health = server.load_discovered(1_700_000_000_000, rows)
        self.assertEqual(health["state"], "live")
        self.assertEqual(len(agents), 1)
        light = agents[0]
        self.assertEqual(light["platform"], "dsh")
        self.assertEqual(light["name"], "DeepSeek Harness · work")
        self.assertEqual(light["detail"], "自动发现 · ~/.dsh")
        self.assertEqual(light["openVia"], "url:3080")
        self.assertTrue(light["openable"])
        self.assertEqual(server.platform_meta("dsh")["order"], 10)

    def test_url_entrance_is_withheld_when_nobody_listens(self):
        rows = table([(84760, 1, UID, "node /opt/homebrew/bin/dsh web", 1_000)])
        with patch.object(server, "process_cwd", return_value=""), patch.object(
            server, "watch_mtime", return_value=0.0
        ), patch.object(
            server, "claude_claimed_pids", return_value=set()
        ), patch.object(
            server, "port_listening", return_value=False
        ), patch.object(
            discovery, "resolve_path", side_effect=FAKE_LINKS.get_or_self
        ):
            agents, _health = server.load_discovered(1_700_000_000_000, rows)
        self.assertEqual(agents[0]["openVia"], "")
        self.assertFalse(agents[0]["openable"])

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

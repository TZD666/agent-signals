#!/usr/bin/env python3
"""进程发现引擎：从一份 ps 表里认出「还没有原生数据源的 agent 运行时」。

这个模块**绝不 import server**——它只吃两样东西（一份 ps 表、一份画像字典），
吐出候选家族，所以可以脱离面板单独测。副作用只有两处只读的文件系统探测
（`looks_like_agent_home` 的目录形状判断，带 10 分钟缓存），没有网络、没有写盘。

分类优先级严格按顺序，命中即停：
  1. 硬排除（别人的 uid / 系统路径 / Electron helper / 黑名单）
  2. 已被原生源认领（登记表 pid 树、ChatGPT.app 的 codex 树、面板自己）
  3. 种子表精确匹配（basename 精确相等，或 exe/script 路径含包片段）
  4. 动态 dotdir 规则（未知家族：命令里出现一个看起来像 agent home 的点目录）
  5. Roll-up（候选的祖先也是候选就并进祖先，一个运行时只留一盏灯）
  6. 上限截断（按 start_s 最新优先）
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 结构性排除：这几条不随画像文件变，写死在代码里。
# ---------------------------------------------------------------------------

# 这些前缀下的东西是系统自带的守护进程，永远不是用户的 agent。
SYSTEM_EXE_PREFIXES = (
    "/System/",
    "/usr/libexec/",
    "/usr/sbin/",
    "/Library/Apple/",
)

# Electron/Chromium 的 helper 进程一律带 --type=；`claude --bg-spare` 是预热壳。
HELPER_MARKERS = ("--type=", "--bg-spare")

# 原生源已经认领的 App：这棵树整个跳过（Codex 的灯由 ChatGPT 的 sqlite 出）。
CLAIMED_APP_PATHS = ("/Applications/ChatGPT.app/",)

# argv[0] 是解释器时，真正说明身份的是后面第一个脚本参数。
# 名字一律小写后再比：macOS 上 Homebrew 与系统的 python 可执行文件都叫
# `Python`（大写 P，住在 `…/Python.app/Contents/MacOS/Python`），实测
# `python3 ~/.fakeagent/run.py` 就是在这里被漏掉的。
INTERPRETERS = frozenset({"node", "bun", "deno", "uv", "uvx"})
PYTHON_RE = re.compile(r"^python\d*(\.\d+)?$")
SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".py")
# `-c` / `-e` 后面跟的是内联代码，不是脚本；再往后扫只会把代码片段当成路径。
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "--command"})


def is_interpreter(basename: str) -> bool:
    name = basename.lower()
    return name in INTERPRETERS or bool(PYTHON_RE.match(name))


# 软链解析的结果：一个运行中进程的可执行文件路径不会变，可以永久缓存。
_realpath_cache: dict[str, str] = {}
REALPATH_CACHE_LIMIT = 2000


def resolve_path(path: str) -> str:
    """把一条绝对路径的软链解析开；失败就安静地退回原样。

    ps 显示的是软链本身：npm 全局装的 CLI 是 `/opt/homebrew/bin/dsh`，
    而种子表里写的包路径片段是 `@deepseek-ai/dsh/`——不解析的话，`paths`
    那一列对所有全局 npm 安装都是空转，`amp` / `pi` 这种禁止裸名匹配的家族
    更是 100% 发现不了。

    只解析绝对路径：相对路径会按面板自己的 cwd 解析，那是错的。
    """
    if not path.startswith("/"):
        return path
    cached = _realpath_cache.get(path)
    if cached is not None:
        return cached
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        resolved = path
    if len(_realpath_cache) > REALPATH_CACHE_LIMIT:
        _realpath_cache.clear()
    _realpath_cache[path] = resolved
    return resolved

# GUI 应用 `.app` 包里的可执行文件只允许种子里写死的路径命中：Electron 应用的
# 主程序常常就叫 `Electron`/`Helper`，靠 basename 或 dotdir 猜必然误伤。
#
# 两个坑，都是真机 ps 逼出来的：
#
# 1. 判据**不能用空格切出来的 argv[0]**。ps 把 argv 用空格拼平了，
#    `/Applications/Google Chrome.app/…` 切出来的第一个 token 是
#    `/Applications/Google`，标记永远不匹配——真机 981 行 ps 里有 44 个进程
#    命令含 `.app/Contents/`，照 token 判会有一大半漏网。所以在整段路径文本上
#    用正则找。
# 2. 解释器自己的 bundle **不算** GUI 应用。macOS 上 Homebrew 与系统的 python
#    都住在 `…/Python.framework/…/Resources/Python.app/Contents/MacOS/Python`，
#    把它算进来会把所有 python 写的 agent 一并豁免掉（`python3
#    ~/.fakeagent/run.py` 实测就是这么消失的）。判据：第一个 `.app/Contents/`
#    之前的那段路径里有没有 `.framework/`。
BUNDLE_MARKER = ".app/Contents/"
BUNDLE_RE = re.compile(r"\.app/Contents/")


def in_app_bundle(path: str) -> bool:
    """这段路径是不是住在一个 GUI 应用的 `.app` 包里。"""
    match = BUNDLE_RE.search(path or "")
    if match is None:
        return False
    return ".framework/" not in path[: match.start()]

MAX_ROLL_UP_DEPTH = 8
DEFAULT_MAX_AGENTS = 24
UNCLASSIFIED_LIMIT = 200

# looks_like_agent_home 的判据
AGENT_HOME_DIRS = ("sessions", "threads", "conversations")
AGENT_HOME_FILES = ("history.jsonl",)
AGENT_HOME_GLOBS = ("rollout-*.jsonl",)
AGENT_HOME_SETTINGS = (
    "settings.json",
    "settings.yaml",
    "settings.yml",
    "settings.toml",
)
SESSION_TABLE_WORDS = ("session", "thread", "conversation", "run", "message")
AGENT_HOME_TTL_S = 600.0
SQLITE_PROBE_LIMIT = 12

# watch glob 里的 `**` 展开到的最大深度（深度 ≤ 3，别把整棵树走穿）。
WATCH_MAX_DEPTH = 3
DEFAULT_WATCH = (
    "**/*.jsonl",
    "**/*.sqlite*",
    "**/*.db",
    "**/*.json",
    "**/*.log",
)


# ---------------------------------------------------------------------------
# 种子画像：形状与 Phase 5 要落盘的 runtime-profiles.json 完全一致。
# Task 4 只在代码里给种子，不读写文件——文件是 Phase 5 的事。
# ---------------------------------------------------------------------------


def _family(
    label: str,
    order: int,
    basenames: Iterable[str] = (),
    paths: Iterable[str] = (),
    home: str = "",
    watch: Iterable[str] = (),
    open_with: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "order": order,
        "source": "seed",
        "learnedAt": 0,
        "match": {"basenames": list(basenames), "paths": list(paths)},
        "home": home,
        "watch": list(watch),
        "open": open_with,
        "hint": "自动发现",
    }


DEFAULT_PROFILES: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION,
    "families": {
        "claude": _family(
            "Claude",
            0,
            basenames=["claude"],
            paths=[
                ".local/share/claude/versions/",
                "Application Support/Claude/claude-code/",
                "@anthropic-ai/claude-code/",
            ],
            home="~/.claude",
            watch=["sessions/*.json"],
            open_with={"tty": True},
        ),
        "codex": _family(
            "Codex",
            1,
            basenames=["codex"],
            paths=["@openai/codex/"],
            home="~/.codex",
            watch=["sessions/*/*/*/rollout-*.jsonl"],
            open_with={"tty": True},
        ),
        "dsh": _family(
            "DeepSeek Harness",
            10,
            basenames=["dsh"],
            paths=["@deepseek-ai/dsh/"],
            home="~/.dsh",
            watch=["storages/*.json", "storages/*/*.json"],
            open_with={"url": 3080},
        ),
        "openclaw": _family(
            "OpenClaw",
            11,
            basenames=["openclaw"],
            paths=["node_modules/openclaw/"],
            home="~/.openclaw",
            watch=["tasks/*.sqlite*", "*.jsonl"],
            open_with=None,
        ),
        "gemini": _family(
            "Gemini CLI",
            12,
            basenames=["gemini"],
            paths=["@google/gemini-cli/"],
            home="~/.gemini",
            open_with={"tty": True},
        ),
        "opencode": _family(
            "OpenCode",
            13,
            basenames=["opencode"],
            paths=["opencode-ai/"],
            home="~/.local/share/opencode",
            open_with={"tty": True},
        ),
        "aider": _family(
            "Aider",
            14,
            basenames=["aider"],
            paths=["site-packages/aider/"],
            home="~/.aider",
            open_with={"tty": True},
        ),
        "goose": _family(
            "Goose",
            15,
            basenames=["goose"],
            paths=["block/goose"],
            home="~/.config/goose",
            open_with={"tty": True},
        ),
        # amp / pi 的裸名太常见（`amp`、`pi` 会撞上别的东西），只认包路径。
        "amp": _family(
            "Amp",
            16,
            paths=["@sourcegraph/amp/"],
            home="~/.amp",
            open_with={"tty": True},
        ),
        "qwen": _family(
            "Qwen Code",
            17,
            basenames=["qwen"],
            paths=["@qwen-code/"],
            home="~/.qwen",
            open_with={"tty": True},
        ),
        "kimi": _family(
            "Kimi CLI",
            18,
            basenames=["kimi"],
            paths=["kimi-cli/"],
            home="~/.kimi",
            open_with={"tty": True},
        ),
        "copilot": _family(
            "Copilot CLI",
            19,
            basenames=["copilot"],
            paths=["@github/copilot/"],
            home="~/.copilot",
            open_with={"tty": True},
        ),
        "cursor-agent": _family(
            "Cursor Agent",
            20,
            basenames=["cursor-agent"],
            home="~/.cursor",
            open_with={"tty": True},
        ),
        "hermes": _family(
            "Hermes",
            21,
            basenames=["hermes"],
            paths=["hermes-agent/"],
            home="~/.hermes",
            open_with={"tty": True},
        ),
        "pi": _family(
            "Pi",
            22,
            paths=["pi-coding-agent/"],
            home="~/.pi",
            open_with={"tty": True},
        ),
        "droid": _family(
            "Droid",
            23,
            basenames=["droid"],
            home="~/.factory",
            open_with={"tty": True},
        ),
        "crush": _family(
            "Crush",
            24,
            basenames=["crush"],
            home="~/.crush",
            open_with={"tty": True},
        ),
        # WorkBuddy 的主程序就叫 `Electron`，basename 完全无效，只能靠 .app 路径。
        "workbuddy": _family(
            "WorkBuddy",
            25,
            paths=["/Applications/WorkBuddy.app/"],
            home="~/.workbuddy",
            watch=["sessions/*.json", "logs/*.log", "traces/*/trace_*.json"],
            open_with={"app": "WorkBuddy"},
        ),
    },
    "ignore": {
        "basenames": [
            "AMPDeviceDiscoveryAgent",
            "AMPLibraryAgent",
            "CursorUIViewService",
            "CodexBar",
        ],
        "pathContains": ["Battle.net"],
        # 这些点目录永远不推成家族名（缓存、包管理器、编辑器、面板自己的邻居）。
        # 只作用于第 4 条动态规则：种子表显式登记的家族（如 cursor-agent 的
        # ~/.cursor）走第 3 条，先命中先走人，不受这份名单影响。
        #
        # `.claude` / `.codex` 也在这里：这两个家族有原生数据源，也有种子表的
        # basename 与包路径兜底，动态规则对它们只会带来假阳性——本机快照里
        # `sh ~/.claude/hud/omc-hud-cache.sh` 就被推成了一盏 Claude 灯。凡是
        # 读一下 ~/.claude 的 hook / statusline / MCP 进程都会中招。
        "dotdirs": [
            # 面板自己的运行目录（`~/Library/Application Support/AgentSignals/`）：
            # agent-history.db 里有 sessions 表，完全符合 agent home 的形状。
            # 自己那个进程靠 pid 排除，但同时跑着的第二个面板实例会被认成一个
            # 叫 AgentSignals 的家族——实测见过。
            ".agentsignals",
            ".claude",
            ".codex",
            ".agent-brain",
            ".omc",
            ".claude-mem",
            ".npm",
            ".cache",
            ".nvm",
            ".pyenv",
            ".cargo",
            ".rustup",
            ".docker",
            ".ssh",
            ".gnupg",
            ".oh-my-zsh",
            ".Trash",
            ".git",
            ".vscode",
            ".cursor",
        ],
    },
}


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------


class Argv(NamedTuple):
    """一条 ps 命令行拆出来的、分类器真正要看的几件东西。"""

    exe: str
    basename: str
    script: str
    # 真正说明身份的那个文件：解释器 + 脚本时是脚本，否则就是 exe。
    target: str
    names: tuple[str, ...]
    # names 里「靠剥掉扩展名才凑出来的」那些，属于弱证据：`~/work/claude.py`
    # 剥出来的 `claude` 不许点亮原生 Claude 分区。
    weak_names: frozenset
    region: str
    # 身份文件在不在 GUI 应用包里——种子表的 basename 匹配看这个。
    in_bundle: bool
    # exe 与脚本**任一**在 GUI 应用包里——dotdir 规则与「点得开吗」看这个。
    # 两者分开是必需的：某个 GUI 应用用自带的 node 跑一个 `~/.foo/x.mjs`，
    # 脚本不在包里，但这个进程仍然不该按 `.foo` 推出一个家族来。
    bundle_anywhere: bool


@dataclass
class Candidate:
    """一个待出灯的运行时。`members` 是被 roll-up 并进来的子进程。"""

    pid: int
    family: str
    label: str
    order: int
    hint: str
    home: str
    home_label: str
    watch: tuple[str, ...]
    open: dict[str, Any] | None
    source: str
    exe: str
    command: str
    start_s: int | None
    # exe 或脚本任一住在 GUI 应用包里：这种进程没有可切过去的终端标签页。
    bundle_anywhere: bool = False
    members: tuple[int, ...] = ()


class Unclassified(NamedTuple):
    """过了硬排除与认领、但没被任何规则认出来的进程。

    Phase 5 的探针要低频扫这批（WorkBuddy 那种主程序叫 `Electron`、dotdir
    只出现在 helper 里的应用，永远成不了候选，也就永远探不到）。Task 4 只
    把出口留好，不实现探针。
    """

    pid: int
    exe: str
    reason: str


class ClassifyResult(NamedTuple):
    candidates: list[Candidate]
    unclassified: list[Unclassified]
    truncated: int


# ---------------------------------------------------------------------------
# 命令行解析
# ---------------------------------------------------------------------------


def path_region(command: str) -> str:
    """命令行里「还在讲路径」的那一段：第一个 `-` 开头的参数之前。

    ps 把 argv 用空格拼平了，带空格的路径（`/Applications/Google Chrome.app/…`）
    没法可靠还原。但种子表要匹配的是 exe 与脚本参数的路径片段，这一段前缀
    正好把它们都包住，又把 JSON 参数、prompt 正文挡在外面。
    """
    kept: list[str] = []
    for token in command.split():
        if token.startswith("-"):
            break
        kept.append(token)
    return " ".join(kept)


def parse_argv(command: str) -> Argv:
    """拆出 exe / 脚本 / 用于种子匹配的名字。纯字符串处理，不碰文件系统。"""
    tokens = command.split()
    exe = tokens[0] if tokens else ""
    basename = exe.rsplit("/", 1)[-1]
    region = path_region(command)

    script = ""
    interpreter = is_interpreter(basename)
    if interpreter:
        for token in tokens[1:]:
            if token in INLINE_CODE_FLAGS:
                break
            if token.startswith("-"):
                continue
            # 「像个路径」就够了，不强求扩展名：npm 全局装的 CLI 是一个
            # **没有扩展名**的软链（`/opt/homebrew/bin/dsh`），只认
            # `.js/.mjs/.cjs/.py` 的话它永远不会被当成脚本。
            if "/" in token or token.endswith(SCRIPT_SUFFIXES):
                script = token
                break

    weak: set = set()
    if interpreter and script:
        script_name = script.rsplit("/", 1)[-1]
        found: list = [script_name]
        for suffix in SCRIPT_SUFFIXES:
            if script_name.endswith(suffix):
                stem = script_name[: -len(suffix)]
                if stem and stem not in found:
                    found.append(stem)
                    weak.add(stem)
                break
        names = tuple(found)
    elif interpreter:
        names = ()
    else:
        names = (basename,)

    # exe 那一段要从整段路径文本里取（带空格的 `.app` 路径没法靠 token 还原），
    # 脚本 token 之前的部分就是它。
    if script:
        cut = region.find(script)
        exe_region = region[:cut] if cut > 0 else region
    else:
        exe_region = region
    exe_in_bundle = in_app_bundle(exe_region)
    script_in_bundle = in_app_bundle(script)

    target = script if (interpreter and script) else exe
    return Argv(
        exe=exe,
        basename=basename,
        script=script,
        target=target,
        names=names,
        weak_names=frozenset(weak),
        region=region,
        in_bundle=script_in_bundle if (interpreter and script) else exe_in_bundle,
        bundle_anywhere=exe_in_bundle or script_in_bundle,
    )


def paths_text(argv: Argv, resolve: Callable[[str], str] = resolve_path) -> str:
    """路径片段匹配的对象：命令行的路径区 + 解析过软链的那一条路径。

    种子表的 `paths` 那一列不解析软链就是空转：npm 全局装的 CLI 在 ps 里是
    `/opt/homebrew/bin/dsh`，而种子里写的是 `@deepseek-ai/dsh/`。

    一个进程只解析一条：有脚本时解析脚本（解释器自己的路径不说明身份），
    否则解析 exe。解析结果与原路径相同时连字符串都不用拼。

    **只在这里做文件系统调用**，而不是在 parse_argv 里：调用点是规则 3，
    此时硬排除与「已被认领」都已经筛过一轮了。整机 994 个进程只剩 60 来个
    走到这一步，冷启动少掉近 600 次 realpath。
    """
    origin = argv.script or argv.exe
    resolved = resolve(origin)
    return argv.region if resolved == origin else f"{argv.region} {resolved}"


# ---------------------------------------------------------------------------
# 规则 1 / 2
# ---------------------------------------------------------------------------


def excluded(argv: Argv, command: str, uid: Any, own_uid: int, ignore: dict) -> str:
    """硬排除；返回原因（空串表示没被排除）。"""
    if uid is None or int(uid) != int(own_uid):
        return "别的 uid"
    for prefix in SYSTEM_EXE_PREFIXES:
        if argv.exe.startswith(prefix):
            return "系统路径"
    for marker in HELPER_MARKERS:
        if marker in command:
            return f"helper 进程（{marker}）"
    for name in ignore.get("basenames") or ():
        if name and name == argv.basename:
            return f"黑名单 basename：{name}"
    for fragment in ignore.get("pathContains") or ():
        if fragment and fragment in command:
            return f"黑名单路径：{fragment}"
    return ""


def descendants(table: dict[str, Any], roots: Iterable[int]) -> set[int]:
    """把一组 pid 展开成它们自己 + 全部子孙。"""
    children = table.get("children") or {}
    found: set[int] = set()
    stack = [int(pid) for pid in roots if int(pid) > 0]
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(children.get(pid, ()))
    return found


def claimed_pids(
    table: dict[str, Any],
    seeds: Iterable[int] = (),
    markers: Iterable[str] = CLAIMED_APP_PATHS,
) -> set[int]:
    """原生源已经认领的进程树：调用方给的种子 pid + 命中 App 标记的进程，各带子孙。"""
    roots = {int(pid) for pid in seeds if int(pid) > 0}
    for pid, command in (table.get("commands") or {}).items():
        for marker in markers:
            if marker and marker in command:
                roots.add(int(pid))
                break
    return descendants(table, roots)


def _ancestor_claimed(
    pid: int, parents: dict[int, int], claimed: set[int], depth: int = MAX_ROLL_UP_DEPTH
) -> bool:
    current = parents.get(pid, 0)
    for _ in range(depth):
        if current <= 0:
            return False
        if current in claimed:
            return True
        current = parents.get(current, 0)
    return False


# ---------------------------------------------------------------------------
# 规则 3：种子表
# ---------------------------------------------------------------------------


def match_seed(
    argv: Argv,
    families: dict[str, Any],
    native: Iterable[str] = (),
    resolve: Callable[[str], str] = resolve_path,
) -> str:
    """种子表精确匹配；返回 family key，没命中返回空串。

    先过一轮路径片段（信号强，GUI 应用包里的 exe 只认这一条），再过 basename。

    `native` 是有原生数据源的平台 key。**剥掉扩展名**才凑出来的名字是弱证据：
    `python3 ~/work/claude.py` 会命中 `claude`，在原生 Claude 分区里冒出一盏
    tty 可点的假灯（而 `./claude.py` 直接跑反倒不会，自己都不自洽）。所以弱名
    不许点亮原生平台。没剥过后缀的名字（`node /opt/homebrew/bin/dsh` 里的
    `dsh` 这种 npm 全局软链）是强证据，照常匹配。
    """
    ordered = sorted(families.items())
    text = paths_text(argv, resolve)
    for key, profile in ordered:
        for fragment in ((profile.get("match") or {}).get("paths") or ()):
            if fragment and fragment in text:
                return key
    if argv.in_bundle:
        return ""
    native_keys = {str(key) for key in native}
    for key, profile in ordered:
        for name in ((profile.get("match") or {}).get("basenames") or ()):
            if not name or name not in argv.names:
                continue
            if name in argv.weak_names and key in native_keys:
                continue
            return key
    return ""


# ---------------------------------------------------------------------------
# 规则 4：动态 dotdir
# ---------------------------------------------------------------------------


def _dotdir_patterns(home: Path) -> tuple[re.Pattern, ...]:
    root = re.escape(str(home).rstrip("/"))
    name = r"([A-Za-z0-9][A-Za-z0-9_-]*)"
    return (
        re.compile(root + r"/\." + name + r"(?![A-Za-z0-9_-])"),
        re.compile(root + r"/\.config/" + name + r"(?![A-Za-z0-9_-])"),
        re.compile(
            root + r"/Library/Application Support/([A-Za-z0-9][A-Za-z0-9 _-]*?)/"
        ),
    )


_pattern_cache: dict[str, tuple[re.Pattern, ...]] = {}


def dotdir_candidates(command: str, home: Path) -> list[tuple[str, Path]]:
    """命令里出现过的、可能是 agent 状态目录的位置，按出现顺序去重。"""
    key = str(home)
    patterns = _pattern_cache.get(key)
    if patterns is None:
        patterns = _dotdir_patterns(home)
        if len(_pattern_cache) > 8:
            _pattern_cache.clear()
        _pattern_cache[key] = patterns

    found: list[tuple[int, str, Path]] = []
    for index, pattern in enumerate(patterns):
        for match in pattern.finditer(command):
            name = match.group(1).strip()
            if not name:
                continue
            if index == 0:
                if name == "config":
                    # `~/.config/<name>` 由第二条负责，`.config` 自己不是家族。
                    continue
                found.append((match.start(), f".{name}", home / f".{name}"))
            elif index == 1:
                found.append(
                    (match.start(), name, home / ".config" / name)
                )
            else:
                found.append(
                    (
                        match.start(),
                        name,
                        home / "Library" / "Application Support" / name,
                    )
                )
    found.sort(key=lambda item: item[0])
    seen: set[str] = set()
    ordered: list[tuple[str, Path]] = []
    for _position, name, path in found:
        if str(path) in seen:
            continue
        seen.add(str(path))
        ordered.append((name, path))
    return ordered


def family_key(name: str) -> str:
    return name.lstrip(".").lower()


def family_label(name: str) -> str:
    stripped = name.lstrip(".")
    if not stripped:
        return ""
    return stripped if stripped != stripped.lower() else stripped.capitalize()


_agent_home_cache: dict[str, tuple[float, bool]] = {}


def reset_caches() -> None:
    """测试用：把目录形状缓存清空。"""
    _agent_home_cache.clear()
    _pattern_cache.clear()


def _sqlite_looks_like_sessions(path: Path) -> bool:
    """只读打开一个 sqlite，看表名里有没有会话味道的词；打不开就当没有。"""
    try:
        connection = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.Error:
        return False
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    for row in rows:
        table = str(row[0] or "").lower()
        if any(word in table for word in SESSION_TABLE_WORDS):
            return True
    return False


def _has_session_sqlite(path: Path) -> bool:
    checked = 0
    for pattern in ("*.sqlite", "*.db", "*/*.sqlite", "*/*.db"):
        try:
            matches = path.glob(pattern)
        except OSError:
            continue
        for candidate in matches:
            if checked >= SQLITE_PROBE_LIMIT:
                return False
            checked += 1
            if _sqlite_looks_like_sessions(candidate):
                return True
    return False


def looks_like_agent_home(path: Path, now: float | None = None) -> bool:
    """这个目录像不像某个 agent 的状态目录。每个目录 10 分钟只判一次。"""
    key = str(path)
    stamp = time.monotonic() if now is None else now
    cached = _agent_home_cache.get(key)
    if cached is not None and 0 <= stamp - cached[0] < AGENT_HOME_TTL_S:
        return cached[1]

    verdict = False
    try:
        if path.is_dir():
            verdict = _inspect_agent_home(path)
    except OSError:
        verdict = False
    if len(_agent_home_cache) > 200:
        _agent_home_cache.clear()
    _agent_home_cache[key] = (stamp, verdict)
    return verdict


def _inspect_agent_home(path: Path) -> bool:
    for name in AGENT_HOME_DIRS:
        if (path / name).is_dir():
            return True
    for name in AGENT_HOME_FILES:
        if (path / name).is_file():
            return True
    for pattern in AGENT_HOME_GLOBS:
        try:
            if next(iter(path.glob(pattern)), None) is not None:
                return True
        except OSError:
            continue
    has_settings = any((path / name).is_file() for name in AGENT_HOME_SETTINGS)
    if has_settings and (path / "profiles").is_dir():
        return True
    return _has_session_sqlite(path)


class DotdirMatch(NamedTuple):
    family: str
    label: str
    home: str
    reason: str


NO_DOTDIR = DotdirMatch("", "", "", "")


def match_dotdir(
    argv: Argv,
    command: str,
    families: dict[str, Any],
    ignore: dict[str, Any],
    home: Path,
    now: float | None = None,
) -> DotdirMatch:
    """未知家族的兜底规则。

    命中时连状态目录一起交出来——动态发现的家族在种子表里没有 profile，
    home 只有这里知道；少了它就既没有活动信号（watch mtime），detail 里
    也写不出「自动发现 · ~/.<name>」。

    GUI 应用包里的进程不适用（Electron 应用的 dotdir 只出现在 helper 里，
    照这条推会把浏览器、聊天软件全推成 agent）。这里看的是 `bundle_anywhere`
    而不是身份文件：某个 GUI 应用用自带的 node 跑一个 `~/.foo/x.mjs`，脚本
    本身不在包里，但它照样不该推出一个 `foo` 家族来。
    """
    if argv.bundle_anywhere:
        return NO_DOTDIR
    ignored = {str(name).lower() for name in (ignore.get("dotdirs") or ())}
    rejected: list[str] = []
    for name, path in dotdir_candidates(command, home):
        marker = name if name.startswith(".") else f".{name}"
        if marker.lower() in ignored or name.lower() in ignored:
            continue
        key = family_key(name)
        if key in families:
            # 种子表已经有同名家族：两条路径必须收敛到同一个 key。
            label = str(families[key].get("label") or family_label(name))
            return DotdirMatch(key, label, str(path), "")
        if looks_like_agent_home(path, now):
            return DotdirMatch(key, family_label(name), str(path), "")
        rejected.append(name)
    if rejected:
        return DotdirMatch(
            "", "", "", f"dotdir 不像 agent home：{'、'.join(rejected[:3])}"
        )
    return NO_DOTDIR


# ---------------------------------------------------------------------------
# 规则 5：roll-up
# ---------------------------------------------------------------------------


def roll_up(
    matched: dict[int, str],
    parents: dict[int, int],
    depth: int = MAX_ROLL_UP_DEPTH,
) -> dict[int, list[int]]:
    """候选的祖先（≤ depth 层）也是**同一个家族**的候选就并进祖先。

    `matched` 是 `pid → family`，返回 `根 pid → 成员列表`。

    只并同族是刻意的：`claude` 拉起一个 `codex exec` 是这台机器上的日常主力
    工作流，不看家族地并会把 codex 那盏灯整个吃掉，这条路上永远看不到它。
    WorkBuddy 的验收（「出现且只有一盏灯」）不受影响——它的成员
    `sidecar-entry.js`（`.app` 路径片段）与 `sandbox-center`（dotdir）都解析
    到 `workbuddy`。

    只看**最近的那个**候选祖先：中间隔着一个异族候选时不再往上找，跨过一层
    别人的进程去认亲说不通。
    """
    members: dict[int, list[int]] = {}
    families = {int(pid): str(family) for pid, family in matched.items()}
    for pid in sorted(families):
        root = pid
        seen = {pid}
        while True:
            current = parents.get(root, 0)
            found = 0
            for _ in range(depth):
                if current <= 0:
                    break
                if current in families:
                    found = current
                    break
                current = parents.get(current, 0)
            if not found or found in seen:
                break
            if families[found] != families[pid]:
                break
            seen.add(found)
            root = found
        members.setdefault(root, []).append(pid)
    for pids in members.values():
        pids.sort()
    return members


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def expand_watch(pattern: str, max_depth: int = WATCH_MAX_DEPTH) -> tuple[str, ...]:
    """把 `**/x` 展开成有限层数的普通 glob，别让 rglob 走穿整棵树。"""
    if "**/" not in pattern:
        return (pattern,)
    tail = pattern.split("**/", 1)[1]
    return tuple("/".join(["*"] * level + [tail]) for level in range(max_depth))


def profile_home(profile: dict[str, Any], home: Path) -> tuple[str, str]:
    """画像里的 home 字段 → `(展开后的绝对路径, 给人看的 ~/… 写法)`。"""
    raw = str(profile.get("home") or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("~/"):
        return str(home / raw[2:]), raw
    if raw == "~":
        return str(home), raw
    expanded = Path(raw)
    return str(expanded), raw


def display_home(path: str, home: Path) -> str:
    """绝对路径 → 给人看的 `~/…` 写法；不在家目录下就原样返回。"""
    if not path:
        return ""
    root = str(home).rstrip("/")
    if path == root:
        return "~"
    if path.startswith(root + "/"):
        return "~/" + path[len(root) + 1 :]
    return path


def classify(
    table: dict[str, Any],
    profiles: dict[str, Any],
    ignore: dict[str, Any],
    claimed: set[int],
    home: Path,
    uid: int,
    max_agents: int = DEFAULT_MAX_AGENTS,
    now: float | None = None,
    native: Iterable[str] = (),
) -> ClassifyResult:
    """一份 ps 表 → 候选家族列表 + 「考察过但没认出来」的进程列表。"""
    families = profiles.get("families") or {}
    commands = table.get("commands") or {}
    uids = table.get("uid") or {}
    starts = table.get("start_s") or {}
    children = table.get("children") or {}
    parents = {
        int(kid): int(parent)
        for parent, kids in children.items()
        for kid in kids
    }
    claimed = {int(pid) for pid in claimed}

    matched: dict[int, tuple[str, str, str, str]] = {}
    unclassified: list[Unclassified] = []
    for pid in sorted(int(key) for key in commands):
        command = commands[pid]
        argv = parse_argv(command)
        if excluded(argv, command, uids.get(pid), uid, ignore):
            continue
        if pid in claimed or _ancestor_claimed(pid, parents, claimed):
            continue
        family = match_seed(argv, families, native)
        if family:
            label = str(families[family].get("label") or family_label(family))
            matched[pid] = (family, label, "seed", "")
            continue
        found = match_dotdir(argv, command, families, ignore, home, now)
        if found.family:
            matched[pid] = (found.family, found.label, "dotdir", found.home)
            continue
        unclassified.append(
            Unclassified(pid, argv.exe, found.reason or "没有任何规则命中")
        )

    members = roll_up(
        {pid: entry[0] for pid, entry in matched.items()}, parents
    )
    candidates: list[Candidate] = []
    for root, pids in members.items():
        family, label, source, found_home = matched[root]
        profile = families.get(family) or {}
        expanded, home_label = profile_home(profile, home)
        if not expanded and found_home:
            # 动态发现的家族在种子表里没有 profile：状态目录只有第 4 条规则知道。
            expanded, home_label = found_home, display_home(found_home, home)
        watch = tuple(profile.get("watch") or ()) or DEFAULT_WATCH
        argv = parse_argv(commands[root])
        candidates.append(
            Candidate(
                pid=root,
                family=family,
                label=label,
                order=int(profile.get("order", 90)),
                hint=str(profile.get("hint") or "自动发现"),
                home=expanded,
                home_label=home_label,
                watch=watch,
                open=profile.get("open"),
                source=source,
                exe=argv.exe,
                command=commands[root],
                start_s=_start_of(starts, root),
                bundle_anywhere=argv.bundle_anywhere,
                members=tuple(pids),
            )
        )

    # 上限：按 start_s 最新优先，start_s 未知的排最后。
    candidates.sort(key=lambda item: (-(item.start_s or 0), item.pid))
    truncated = max(0, len(candidates) - max_agents) if max_agents > 0 else 0
    if truncated:
        candidates = candidates[:max_agents]
    candidates.sort(key=lambda item: (item.order, item.family, item.pid))

    unclassified.sort(key=lambda item: (-(_start_of(starts, item.pid) or 0), item.pid))
    return ClassifyResult(candidates, unclassified[:UNCLASSIFIED_LIMIT], truncated)


def _start_of(starts: dict[Any, Any], pid: int) -> int | None:
    value = starts.get(pid)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

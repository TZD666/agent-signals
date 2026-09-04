#!/usr/bin/env python3
"""Local agent status panel for Claude Code and Codex Desktop."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


SCHEMA_VERSION = 1

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CLAUDE_SESSIONS_DIR = Path(
    os.environ.get("CLAUDE_SESSIONS_DIR", "~/.claude/sessions")
).expanduser()
CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
).expanduser()
CODEX_DIR = Path(os.environ.get("CODEX_DIR", "~/.codex")).expanduser()
CODEX_DB_PATH = (
    Path(os.environ["CODEX_DB_PATH"]).expanduser()
    if os.environ.get("CODEX_DB_PATH")
    else None
)
STATE_PATH = Path(
    os.environ.get("AGENT_SIGNALS_STATE_PATH", ROOT / ".agent-signals-state.json")
).expanduser()
SERVER_STARTED_AT_MS = int(time.time() * 1000)

VISIBLE_WINDOW_MS = 48 * 60 * 60 * 1000
RECENT_ERROR_MS = 5 * 60 * 1000
MAX_CODEX_THREADS = 30
# Real rollouts carry single events of tens of KB; 512KB of tail held only ~25
# lines and let every status marker scroll out of view on long turns.
CODEX_TAIL_BYTES = 4_000_000
CLAUDE_TAIL_BYTES = 1_000_000
# The Claude transcript never states the model context window; env-tunable.
CLAUDE_CONTEXT_WINDOW = env_int("AGENT_SIGNALS_CLAUDE_CONTEXT_WINDOW", 200_000)
STEP_GAP_MAX_SAMPLES = 10
STEP_GAP_OUTLIER_MS = 30 * 60 * 1000

STALL_AFTER_MS = env_int("AGENT_SIGNALS_STALL_AFTER_MS", 180_000)
CPU_EPSILON = env_float("AGENT_SIGNALS_CPU_EPSILON", 0.05)
SAMPLE_INTERVAL_MS = env_int("AGENT_SIGNALS_SAMPLE_INTERVAL_MS", 5_000)
LONG_POLL_MAX_S = env_float("AGENT_SIGNALS_LONG_POLL_MAX_S", 8.0)
MAX_WAITERS = env_int("AGENT_SIGNALS_MAX_WAITERS", 8)
NOTIFY_THROTTLE_MS = env_int("AGENT_SIGNALS_NOTIFY_THROTTLE_MS", 60_000)
NOTIFY_MAX_PER_MINUTE = env_int("AGENT_SIGNALS_NOTIFY_MAX_PER_MINUTE", 10)
NOTIFY_ENABLED = os.environ.get("AGENT_SIGNALS_NOTIFY", "1") != "0"
LOCKED_ID_TTL_MS = 10 * 60 * 1000

# ---- 历史埋点与费用估算（history / cost estimation） ----
HISTORY_ENABLED = os.environ.get("AGENT_SIGNALS_HISTORY", "1") != "0"
HISTORY_DB_PATH = Path(
    os.environ.get("AGENT_SIGNALS_HISTORY_DB", ROOT / "agent-history.db")
).expanduser()
CODEX_PRICES_PATH = Path(
    os.environ.get("AGENT_SIGNALS_CODEX_PRICES", ROOT / "codex_prices.json")
).expanduser()
CLAUDE_PRICES_PATH = Path(
    os.environ.get(
        "AGENT_SIGNALS_CLAUDE_PRICES",
        Path.home()
        / "Library/Application Support/tokenusage/data/prices.json",
    )
).expanduser()
HISTORY_SCHEMA_VERSION = 1
HISTORY_INTERVAL_MS = env_int("AGENT_SIGNALS_HISTORY_INTERVAL_MS", 60_000)
HISTORY_BACKFILL_DAYS = env_int("AGENT_SIGNALS_HISTORY_BACKFILL_DAYS", 30)
HISTORY_RETENTION_DAYS = env_int("AGENT_SIGNALS_HISTORY_RETENTION_DAYS", 90)
HISTORY_TURNS_RETENTION_DAYS = env_int(
    "AGENT_SIGNALS_HISTORY_TURNS_RETENTION_DAYS", 30
)
HISTORY_INGEST_BYTE_BUDGET = env_int(
    "AGENT_SIGNALS_HISTORY_BYTE_BUDGET", 32_000_000
)

# Claude 兜底价目表，$/MTok：(input, output, cache_read, cache_write_5m)。
# 正常路径读 tokenusage 的 prices.json（LiteLLM 镜像，每分钟自动刷新）；
# 这张表只在镜像缺失/损坏时使用。同步源：tokenusage ingest.py 的 _FALLBACK_PRICING。
CLAUDE_FALLBACK_PRICES_MTOK = {
    "claude-fable-5": (10.00, 50.00, 1.00, 12.50),
    "claude-mythos-5": (10.00, 50.00, 1.00, 12.50),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-6": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4": (1.00, 5.00, 0.10, 1.25),
}

# Codex/GPT 播种价目表，$/MTok。本机没有权威 GPT 价格源，这里按 OpenAI 公开
# 标价填初始值，属于估算；文件生成后完全由用户编辑，服务只在文件缺失时播种。
CODEX_SEED_PRICES = {
    "_note": "Codex 估算价格（美元/百万 token）。可手工编辑，改完即生效；"
    "未收录的模型金额显示为 —，绝不编造。",
    "_unit": "usd_per_mtok",
    "models": {
        "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
        "gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
        "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
        "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
        "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.4},
    },
}

STATUS_LABELS = {
    "idle": "空闲",
    "thinking": "思考中",
    "completed": "已完成",
    "needs_input": "需要输入",
    "stalled": "疑似卡死",
    "error": "错误",
}
NOTIFY_STATUSES = ("needs_input", "stalled", "completed")

CODEX_CORE_COLUMNS = frozenset({"id", "cwd", "source", "rollout_path"})
CODEX_ACTIVITY_COLUMNS = (
    "recency_at_ms",
    "updated_at_ms",
    "updated_at",
    "created_at_ms",
    "created_at",
)
CODEX_PREFERRED_COLUMNS = frozenset(
    {
        *CODEX_CORE_COLUMNS,
        "title",
        "agent_nickname",
        "name",
        "thread_source",
        "recency_at_ms",
        "updated_at_ms",
        "archived",
    }
)

# Fragments that show up when Codex stores a raw JSON slice as the thread title.
JSON_FRAGMENT_MARKERS = ('"}]}', '"}]', '"},{', '{"type"', '[{"', '"}', '}]', ']},')

_claude_transitions: dict[str, dict[str, int | str]] = {}
_activity: dict[str, dict[str, float]] = {}
_rollout_cache: dict[str, tuple[tuple[float, int], tuple[str, int, dict[str, Any]]]] = {}
_claude_load_cache: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}
_transcript_paths: dict[str, Path] = {}
_state_lock = threading.Lock()

_snapshot_ready = threading.Condition()
_latest_snapshot: dict[str, Any] | None = None
_latest_revision = ""
_waiters = 0
_locked_ids: dict[str, int] = {}
_notified: dict[str, int] = {}
_notify_history: list[int] = []
_notify_primed = False
_notify_health: dict[str, str] = {"state": "unknown", "detail": ""}
_price_cache: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}
_history_health: dict[str, Any] = {"state": "initializing", "detail": ""}


def load_acknowledged_completions() -> dict[str, int]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        acknowledged = data.get("acknowledgedCompletions", {})
        if isinstance(acknowledged, dict):
            return {
                str(key): int(value)
                for key, value in acknowledged.items()
                if str(value).isdigit()
            }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {}


_acknowledged_completions = load_acknowledged_completions()


def completion_key(platform: str, agent_id: str) -> str:
    return f"{platform}:{agent_id}"


def apply_completion_acknowledgement(
    platform: str, agent: dict[str, Any]
) -> None:
    if agent.get("status") != "completed":
        return
    completion_id = int(agent.get("completionId") or 0)
    with _state_lock:
        acknowledged = _acknowledged_completions.get(
            completion_key(platform, str(agent.get("id") or "")), 0
        )
    if completion_id and acknowledged >= completion_id:
        agent["status"] = "idle"
        agent["completionId"] = 0


def save_acknowledged_completions() -> None:
    payload = json.dumps(
        {"acknowledgedCompletions": _acknowledged_completions},
        ensure_ascii=False,
        indent=2,
    )
    temporary = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(STATE_PATH)
    except OSError:
        return


def acknowledge_agent(agent: dict[str, Any]) -> bool:
    platform = str(agent.get("platform") or "")
    changed = False
    with _state_lock:
        for item in [agent, *(agent.get("satellites") or [])]:
            if item.get("status") != "completed":
                continue
            completion_id = int(item.get("completionId") or 0)
            key = completion_key(platform, str(item.get("id") or ""))
            if completion_id > _acknowledged_completions.get(key, 0):
                _acknowledged_completions[key] = completion_id
                changed = True
        if changed:
            save_acknowledged_completions()
    return changed


def is_preexisting_completion(status: str, completion_id: int) -> bool:
    return (
        status == "completed"
        and completion_id > 0
        and completion_id < SERVER_STARTED_AT_MS
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_ms(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number * 1000 if 0 < number < 10_000_000_000 else number


def parse_cpu_time(value: str) -> float:
    """Turn a ps TIME column such as ``12:34.56`` into seconds."""
    total = 0.0
    for part in value.split(":"):
        try:
            total = total * 60 + float(part)
        except ValueError:
            return 0.0
    return total


def scan_processes() -> dict[str, Any]:
    """One ps sweep per sample for CPU accounting; command lines cost extra."""
    table: dict[str, Any] = {"children": {}, "cpu": {}}
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,time="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return table

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table["cpu"][pid] = parse_cpu_time(parts[2])
        table["children"].setdefault(parent, []).append(pid)
    return table


def command_lines(pids: set[int]) -> dict[int, str]:
    """Full command lines, asked for only the handful of pids we care about."""
    wanted = sorted(pid for pid in pids if pid > 0)
    if not wanted:
        return {}
    try:
        result = subprocess.run(
            ["ps", "-p", ",".join(str(pid) for pid in wanted), "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    commands: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            commands[int(parts[0])] = parts[1]
        except ValueError:
            continue
    return commands


def tree_cpu(pid: int, table: dict[str, Any]) -> float:
    """Cumulative CPU seconds for a process and everything it spawned."""
    total, stack, seen = 0.0, [pid], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        total += table["cpu"].get(current, 0.0)
        stack.extend(table["children"].get(current, ()))
    return total


def process_alive(pid: int, commands: dict[int, str] | None = None) -> bool:
    if pid <= 0:
        return False
    commands = commands if commands is not None else command_lines({pid})
    command = commands.get(pid)
    if command is None:
        return False
    command = command.lower()
    return "claude" in command and "--bg-spare" not in command


def transcript_path(session_id: str) -> Path | None:
    """Resolve (and cache) the transcript path for a session."""
    cached = _transcript_paths.get(session_id)
    if cached is not None:
        if cached.exists():
            return cached
        _transcript_paths.pop(session_id, None)
    for path in CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        _transcript_paths[session_id] = path
        return path
    return None


def transcript_mtime(session_id: str) -> float:
    """Last write to the session transcript, our Claude-side activity signal."""
    path = transcript_path(session_id)
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def claude_load(session_id: str) -> dict[str, Any]:
    """Context weight and step cadence from the transcript tail."""
    path = transcript_path(session_id)
    if path is None:
        return empty_load()
    signature = rollout_signature(path)
    if signature is not None:
        cached = _claude_load_cache.get(session_id)
        if cached is not None and cached[0] == signature:
            return cached[1]

    usage: dict[str, Any] | None = None
    step_times: list[int] = []
    last_request_id: Any = object()
    for line in read_tail_json(path, CLAUDE_TAIL_BYTES):
        if str(line.get("type") or "") != "assistant":
            continue
        message = line.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        usage = message["usage"]
        stamp = parse_iso_ms(line.get("timestamp"))
        if stamp:
            request_id = line.get("requestId")
            # Streamed content blocks of one request repeat the same usage;
            # they are one step, so only their newest timestamp counts.
            if request_id is not None and request_id == last_request_id and step_times:
                step_times[-1] = stamp
            else:
                note_step(step_times, stamp)
            last_request_id = request_id

    tokens = None
    if usage is not None:
        try:
            tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
        except (TypeError, ValueError):
            tokens = None
    load = build_load(
        tokens,
        CLAUDE_CONTEXT_WINDOW if CLAUDE_CONTEXT_WINDOW > 0 else None,
        step_times,
    )
    if signature is not None:
        if len(_claude_load_cache) > 200:
            _claude_load_cache.clear()
        _claude_load_cache[session_id] = (signature, load)
    return load


def note_activity(key: str, cpu: float, mtime: float, current_ms: int) -> int:
    """Track when a session last showed any sign of life; returns that moment."""
    previous = _activity.get(key)
    if previous is None:
        _activity[key] = {"cpu": cpu, "mtime": mtime, "quietSince": current_ms}
        return current_ms
    moved = (cpu - previous["cpu"] > CPU_EPSILON) or (mtime > previous["mtime"])
    quiet_since = current_ms if moved else int(previous["quietSince"])
    _activity[key] = {"cpu": cpu, "mtime": mtime, "quietSince": quiet_since}
    return quiet_since


def apply_stall(status: str, busy_for_ms: int, quiet_for_ms: int) -> str:
    """A long-running task only counts as stalled when nothing moved either."""
    if status != "thinking":
        return status
    if busy_for_ms >= STALL_AFTER_MS and quiet_for_ms >= STALL_AFTER_MS:
        return "stalled"
    return status


def clean_label(raw: str, fallback: str = "") -> str:
    """Codex sometimes stores a raw JSON slice as the title; keep the prose."""
    text = str(raw or "")
    for escape in ("\\n", "\\r", "\\t"):
        text = text.replace(escape, " ")
    cut = len(text)
    for marker in JSON_FRAGMENT_MARKERS:
        found = text.find(marker)
        if found != -1:
            cut = min(cut, found)
    text = text[:cut]
    text = "".join(" " if character < " " else character for character in text)
    text = " ".join(text.split()).strip(" \"'{}[],:")
    if not text:
        return fallback
    return text if len(text) <= 40 else f"{text[:39]}…"


def cwd_label(cwd: str) -> str:
    if not cwd:
        return "未知目录"
    path = Path(cwd)
    return "~" if path == Path.home() else path.name or str(path)


def claude_status(
    session_id: str, raw_status: str, alive: bool, updated_at: int, current_ms: int
) -> str:
    if not alive:
        return "error" if current_ms - updated_at <= RECENT_ERROR_MS else "offline"

    if raw_status in {"busy", "shell"}:
        mapped = "thinking"
    elif raw_status == "waiting":
        mapped = "needs_input"
    else:
        mapped = "idle"

    previous = _claude_transitions.get(session_id)
    completed_at = int(previous.get("completed_at", 0)) if previous else 0
    if (
        previous
        and previous.get("raw") in {"busy", "shell"}
        and raw_status == "idle"
    ):
        completed_at = current_ms
    _claude_transitions[session_id] = {
        "raw": raw_status,
        "completed_at": completed_at,
    }
    return "completed" if mapped == "idle" and completed_at else mapped


def load_claude_sessions(
    current_ms: int | None = None, table: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    current_ms = current_ms or now_ms()
    table = table if table is not None else scan_processes()
    foreground: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []

    if not CLAUDE_SESSIONS_DIR.exists():
        return foreground, {
            "state": "unavailable",
            "detail": f"找不到会话目录：{CLAUDE_SESSIONS_DIR}",
        }

    sessions: list[dict[str, Any]] = []
    for path in sorted(CLAUDE_SESSIONS_DIR.glob("*.json")):
        try:
            sessions.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    commands = command_lines({int(data.get("pid") or 0) for data in sessions})

    for data in sessions:
        pid = int(data.get("pid") or 0)
        alive = process_alive(pid, commands)
        updated_at = normalize_ms(data.get("statusUpdatedAt") or data.get("updatedAt"))
        session_id = str(data.get("sessionId") or pid)
        status = claude_status(
            session_id,
            str(data.get("status") or "idle"),
            alive,
            updated_at,
            current_ms,
        )
        completion_id = int(
            _claude_transitions.get(session_id, {}).get("completed_at", 0)
        )
        if status == "offline":
            continue

        quiet_since = note_activity(
            completion_key("claude", session_id),
            tree_cpu(pid, table),
            transcript_mtime(session_id),
            current_ms,
        )
        status = apply_stall(
            status, current_ms - updated_at, current_ms - quiet_since
        )

        agent = {
            "id": session_id,
            "pid": pid,
            "platform": "claude",
            "name": str(data.get("name") or f"terminal-{pid}"),
            "status": status,
            "detail": "Terminal",
            "cwd": str(data.get("cwd") or ""),
            "cwdLabel": cwd_label(str(data.get("cwd") or "")),
            "updatedAt": updated_at,
            "quietSince": quiet_since,
            "completionId": completion_id if status == "completed" else 0,
            "openable": alive and str(data.get("kind") or "interactive") == "interactive",
            "satellites": [],
        }
        apply_completion_acknowledgement("claude", agent)
        if str(data.get("kind") or "interactive") == "interactive":
            agent["load"] = claude_load(session_id)
            foreground.append(agent)
        else:
            agent["name"] = str(data.get("name") or "后台任务")
            background.append(agent)

    foreground.sort(key=lambda item: item["updatedAt"], reverse=True)
    for satellite in background:
        parent = next(
            (item for item in foreground if item["cwd"] == satellite["cwd"]),
            foreground[0] if foreground else None,
        )
        if parent:
            parent["satellites"].append(
                {
                    "id": satellite["id"],
                    "name": satellite["name"],
                    "status": satellite["status"],
                    "completionId": satellite["completionId"],
                }
            )
    return foreground, {"state": "live", "detail": ""}


def read_tail_json(path: Path, max_bytes: int = 512_000) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return []

    if start:
        raw = raw.split(b"\n", 1)[-1]

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return events


def parse_iso_ms(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return int(
            calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
            * 1000
        )
    except ValueError:
        return 0


def empty_load() -> dict[str, Any]:
    return {
        "contextTokens": None,
        "contextWindow": None,
        "contextPct": None,
        "stepGapMs": None,
    }


def quantize_step_gap(ms: int) -> int:
    bucket = 15_000 if ms < 120_000 else 60_000
    return max(bucket, int(round(ms / float(bucket))) * bucket)


def step_gap_ms(timestamps: list[int]) -> int | None:
    """Average of the recent gaps; anything over the outlier cap is a user pause."""
    gaps = [
        later - earlier
        for earlier, later in zip(timestamps, timestamps[1:])
        if 0 < later - earlier <= STEP_GAP_OUTLIER_MS
    ]
    if not gaps:
        return None
    recent = gaps[-STEP_GAP_MAX_SAMPLES:]
    return quantize_step_gap(int(sum(recent) / len(recent)))


def build_load(tokens: Any, window: Any, steps: list[int]) -> dict[str, Any]:
    """Quantized ints or None only: a float or clock-relative value in the
    payload would change every sample and defeat the ETag/304 contract."""
    load = empty_load()
    if isinstance(tokens, (int, float)) and tokens >= 0:
        load["contextTokens"] = int(round(tokens / 1000.0)) * 1000
        # tokens > window means the assumed window is wrong (e.g. a 1M-context
        # session against the default denominator): admit ignorance, don't show
        # a fake 100%.
        if isinstance(window, (int, float)) and 0 < tokens <= window:
            load["contextWindow"] = int(window)
            load["contextPct"] = max(0, int(round(tokens * 100.0 / window)))
    load["stepGapMs"] = step_gap_ms(steps)
    return load


def note_step(step_times: list[int], stamp: int) -> None:
    if stamp and (not step_times or stamp > step_times[-1]):
        step_times.append(stamp)
        if len(step_times) > STEP_GAP_MAX_SAMPLES + 1:
            step_times.pop(0)


def rollout_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def codex_state(rollout_path: str) -> tuple[str, int, dict[str, Any]]:
    path = Path(rollout_path)
    signature = rollout_signature(path)
    if signature is not None:
        cached = _rollout_cache.get(rollout_path)
        if cached is not None and cached[0] == signature:
            return cached[1]

    state = "idle"
    state_at = 0
    token_info: dict[str, Any] | None = None
    step_times: list[int] = []
    status_events = {
        "task_started": "thinking",
        "agent_reasoning": "thinking",
        "agent_message": "thinking",
        # Newer Codex builds fill long turns almost exclusively with these
        # response_item/token_count entries; each only ever appears mid-turn.
        "reasoning": "thinking",
        "custom_tool_call": "thinking",
        "custom_tool_call_output": "thinking",
        "function_call": "thinking",
        "function_call_output": "thinking",
        "token_count": "thinking",
        "task_complete": "completed",
        "turn_aborted": "error",
        "approval_request": "needs_input",
        "exec_approval_request": "needs_input",
        "apply_patch_approval_request": "needs_input",
        "elicitation_request": "needs_input",
    }

    for event in read_tail_json(path, CODEX_TAIL_BYTES):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "")
        if event_type in status_events:
            state = status_events[event_type]
            state_at = parse_iso_ms(event.get("timestamp"))
            note_step(step_times, state_at)
        if event_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
                token_info = info

    usage = token_info.get("last_token_usage", {}) if token_info else {}
    load = build_load(
        usage.get("input_tokens"),
        token_info.get("model_context_window") if token_info else None,
        step_times,
    )
    if signature is not None:
        if len(_rollout_cache) > 200:
            _rollout_cache.clear()
        _rollout_cache[rollout_path] = (signature, (state, state_at, load))
    return state, state_at, load


def codex_status(rollout_path: str, current_ms: int | None = None) -> str:
    del current_ms
    return codex_state(rollout_path)[0]


def satellite_name(source: str) -> str:
    try:
        parsed = json.loads(source)
        subagent = parsed.get("subagent", {})
        if isinstance(subagent, dict):
            value = next(iter(subagent.values()), "子代理")
            if isinstance(value, dict):
                # thread_spawn children carry their identity inside the value.
                nickname = str(value.get("agent_nickname") or "").strip()
                path_name = Path(str(value.get("agent_path") or "")).name
                return nickname or path_name or "子代理"
            return str(value) or "子代理"
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return "子代理"


def resolve_codex_db() -> tuple[Path | None, str]:
    """Explicit override wins, otherwise pick the newest highest-version database."""
    if CODEX_DB_PATH is not None:
        if CODEX_DB_PATH.exists():
            return CODEX_DB_PATH, ""
        return None, f"指定的 CODEX_DB_PATH 不存在：{CODEX_DB_PATH}"
    if not CODEX_DIR.exists():
        return None, f"找不到 Codex 目录：{CODEX_DIR}"

    candidates = {
        *CODEX_DIR.glob("state_*.sqlite"),
        *(CODEX_DIR / "sqlite").glob("state_*.sqlite"),
    }

    def candidate_key(candidate: Path) -> tuple[int, int, int]:
        try:
            version = int(candidate.stem.rsplit("_", 1)[-1])
        except ValueError:
            version = -1
        try:
            modified = candidate.stat().st_mtime_ns
        except OSError:
            modified = 0
        return version, modified, int(candidate.parent == CODEX_DIR)

    if not candidates:
        return None, f"{CODEX_DIR} 及其 sqlite 子目录下没有 state_*.sqlite"
    return max(candidates, key=candidate_key), ""


def codex_connect_ro(database: Path) -> sqlite3.Connection:
    """只读打开 Codex state 库，带 immutable 兜底。

    WAL 库在写端不在线且上次未干净收尾时，纯 mode=ro 打开会因无法创建
    -shm 而报 unable to open database file（2026-08-18 ChatGPT 桌面端升级
    中断写端时实测踩到）。此时写端必然离线，用 immutable=1 读是安全的；
    写端一旦回来 -shm 重建，主路径自然恢复。
    """
    last_error: sqlite3.Error | None = None
    for uri in (
        f"file:{database}?mode=ro",
        f"file:{database}?mode=ro&immutable=1",
    ):
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("SELECT 1").fetchone()
            return connection
        except sqlite3.Error as error:
            last_error = error
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
    raise last_error if last_error else sqlite3.OperationalError(
        "unable to open database file"
    )


def codex_table_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(threads)")
        if len(row) > 1
    }


def codex_schema_issue(columns: set[str]) -> str:
    missing_core = sorted(CODEX_CORE_COLUMNS - columns)
    if missing_core:
        return f"threads 表缺少核心字段：{', '.join(missing_core)}"
    if not any(column in columns for column in CODEX_ACTIVITY_COLUMNS):
        return (
            "threads 表缺少时间字段："
            + ", ".join(CODEX_ACTIVITY_COLUMNS)
        )
    return ""


def codex_schema_ok(connection: sqlite3.Connection) -> bool:
    try:
        columns = codex_table_columns(connection)
    except sqlite3.Error:
        return False
    return not codex_schema_issue(columns)


def codex_activity_expression(columns: set[str]) -> str:
    expressions: list[str] = []
    if "recency_at_ms" in columns:
        expressions.append("NULLIF(recency_at_ms, 0)")
    if "updated_at_ms" in columns:
        expressions.append("NULLIF(updated_at_ms, 0)")
    if "updated_at" in columns:
        expressions.append("updated_at * 1000")
    if "created_at_ms" in columns:
        expressions.append("NULLIF(created_at_ms, 0)")
    if "created_at" in columns:
        expressions.append("created_at * 1000")
    return f"COALESCE({', '.join(expressions)}, 0)"


def codex_text_expression(columns: set[str], candidates: tuple[str, ...]) -> str:
    expressions = [
        f"NULLIF({column}, '')" for column in candidates if column in columns
    ]
    return f"COALESCE({', '.join(expressions)}, '')" if expressions else "''"


def codex_query(columns: set[str], locked_count: int) -> str:
    activity = codex_activity_expression(columns)
    title = codex_text_expression(
        columns, ("title", "first_user_message", "preview")
    )
    nickname = "agent_nickname" if "agent_nickname" in columns else "NULL"
    name = "name" if "name" in columns else "NULL"
    raw_thread_source = "thread_source" if "thread_source" in columns else "NULL"
    thread_source = f"""
        CASE
          WHEN COALESCE({raw_thread_source}, '') <> '' THEN {raw_thread_source}
          WHEN source LIKE '%"subagent"%' THEN 'subagent'
          WHEN source IN ('vscode', 'exec') THEN 'user'
          ELSE ''
        END
    """
    archived = (
        "COALESCE(archived, 0) = 0" if "archived" in columns else "1 = 1"
    )
    locked_clause = (
        f" OR id IN ({','.join('?' for _ in range(locked_count))})"
        if locked_count
        else ""
    )
    return f"""
        SELECT id,
               {title} AS title,
               {nickname} AS agent_nickname,
               {name} AS name,
               cwd,
               source,
               {thread_source} AS thread_source,
               rollout_path,
               {activity} AS activity_ms
        FROM threads
        WHERE {archived}
          AND (
            {activity} >= ?
            {locked_clause}
          )
        ORDER BY activity_ms DESC
        LIMIT ?
    """


def load_codex_threads(
    current_ms: int | None = None, locked_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    current_ms = current_ms or now_ms()
    cutoff = current_ms - VISIBLE_WINDOW_MS
    database, detail = resolve_codex_db()
    if database is None:
        return [], {"state": "unavailable", "detail": detail}

    locked = tuple(sorted(locked_ids or ()))[:50]
    try:
        connection = codex_connect_ro(database)
        connection.row_factory = sqlite3.Row
        columns = codex_table_columns(connection)
        issue = codex_schema_issue(columns)
        if issue:
            connection.close()
            return [], {
                "state": "schema_mismatch",
                "detail": f"{database} 无法兼容：{issue}",
            }
        query = codex_query(columns, len(locked))
        rows = list(
            connection.execute(
                query, (cutoff, *locked, MAX_CODEX_THREADS + len(locked))
            )
        )
        connection.close()
    except sqlite3.Error as error:
        return [], {"state": "unavailable", "detail": f"读取 {database.name} 失败：{error}"}

    foreground: list[dict[str, Any]] = []
    subagents: list[dict[str, Any]] = []
    for row in rows:
        source = str(row["source"] or "")
        thread_source = str(row["thread_source"] or "")
        rollout_path = str(row["rollout_path"] or "")
        status, status_at, load = codex_state(rollout_path)
        cwd = str(row["cwd"] or "")
        label = clean_label(
            row["agent_nickname"] or row["name"] or row["title"] or "",
            fallback=cwd_label(cwd) if cwd else "Codex",
        )
        signature = rollout_signature(Path(rollout_path))
        quiet_since = int(signature[0] * 1000) if signature else current_ms
        status = apply_stall(
            status, current_ms - quiet_since, current_ms - quiet_since
        )
        completion_id = (
            status_at or normalize_ms(row["activity_ms"])
            if status == "completed"
            else 0
        )
        if is_preexisting_completion(status, completion_id):
            continue
        agent = {
            "id": str(row["id"]),
            "platform": "codex",
            "name": label,
            "status": status,
            "detail": "Codex CLI" if source == "exec" else "Codex",
            "cwd": cwd,
            "cwdLabel": cwd_label(cwd),
            "updatedAt": normalize_ms(row["activity_ms"]),
            "quietSince": quiet_since,
            "completionId": completion_id,
            "openable": True,
            "load": load,
            "satellites": [],
        }
        apply_completion_acknowledgement("codex", agent)
        if thread_source == "user" and source in {"vscode", "exec"}:
            foreground.append(agent)
        elif thread_source == "subagent":
            agent["name"] = satellite_name(source)
            subagents.append(agent)

    foreground.sort(key=lambda item: item["updatedAt"], reverse=True)
    for satellite in subagents:
        parent = next(
            (item for item in foreground if item["cwd"] == satellite["cwd"]),
            foreground[0] if foreground else None,
        )
        if parent:
            parent["satellites"].append(
                {
                    "id": satellite["id"],
                    "name": satellite["name"],
                    "status": satellite["status"],
                    "completionId": satellite["completionId"],
                }
            )
    missing_optional = sorted(CODEX_PREFERRED_COLUMNS - columns)
    compatibility = (
        f"兼容读取 {database}；缺少可选字段：{', '.join(missing_optional)}"
        if missing_optional
        else ""
    )
    return foreground, {"state": "live", "detail": compatibility}


def prune_tracking(live_keys: set[str]) -> None:
    """Sessions come and go; do not let the tracking dicts grow forever."""
    for key in [key for key in _activity if key not in live_keys]:
        _activity.pop(key, None)
    live_sessions = {key.split(":", 1)[1] for key in live_keys if key.startswith("claude:")}
    for key in [key for key in _claude_transitions if key not in live_sessions]:
        _claude_transitions.pop(key, None)
    for key in [key for key in _transcript_paths if key not in live_sessions]:
        _transcript_paths.pop(key, None)
    for key in [key for key in _claude_load_cache if key not in live_sessions]:
        _claude_load_cache.pop(key, None)


# ---------------------------------------------------------------------------
# 历史埋点与费用估算
#
# 独立于 5 秒采样循环：一条 60 秒的 ingester 线程按字节偏移增量续读
# Claude transcript / Codex rollout，把「时间戳 + 用量数字」落进 sqlite。
# 绝不落任何对话内容。金额不落库，由端点按当前价格表现算。
# ---------------------------------------------------------------------------


def _mtok_to_per_token(value: Any) -> float | None:
    try:
        return float(value) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def _normalize_claude_price(entry: Any) -> dict[str, float] | None:
    if not isinstance(entry, dict):
        return None
    normalized: dict[str, float] = {}
    for field in ("input", "output", "cache_read", "cache_write"):
        try:
            normalized[field] = float(entry.get(field) or 0.0)
        except (TypeError, ValueError):
            return None
    return normalized if any(normalized.values()) else None


def _load_json_cached(path: Path) -> Any:
    """mtime 缓存的 JSON 读取；失败返回 None。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _price_cache.pop(key, None)
        return None
    cached = _price_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    _price_cache[key] = (mtime, parsed)
    return parsed


def claude_fallback_prices() -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for model, (inp, out, cache_read, cache_write) in (
        CLAUDE_FALLBACK_PRICES_MTOK.items()
    ):
        table[model] = {
            "input": inp / 1_000_000.0,
            "output": out / 1_000_000.0,
            "cache_read": cache_read / 1_000_000.0,
            "cache_write": cache_write / 1_000_000.0,
        }
    return table


def load_claude_prices(
    path: Path | None = None,
) -> tuple[dict[str, dict[str, float]], str]:
    """返回 (模型→每 token 价格, 来源)。来源 live=价格镜像，fallback=兜底表。"""
    parsed = _load_json_cached(path or CLAUDE_PRICES_PATH)
    table: dict[str, dict[str, float]] = {}
    if isinstance(parsed, dict):
        for model, entry in parsed.items():
            normalized = _normalize_claude_price(entry)
            if normalized is not None and str(model).startswith("claude-"):
                table[str(model)] = normalized
    if table:
        return table, "live"
    return claude_fallback_prices(), "fallback"


def ensure_codex_price_file(path: Path | None = None) -> None:
    """缺失才播种，绝不覆盖用户编辑过的文件。"""
    path = path or CODEX_PRICES_PATH
    if path.exists():
        return
    payload = json.dumps(CODEX_SEED_PRICES, ensure_ascii=False, indent=2)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return


def load_codex_prices(
    path: Path | None = None,
) -> tuple[dict[str, dict[str, float]], str]:
    """返回 (模型→每 token 价格, 来源)。来源 file=价格文件，seed=内存播种值。"""
    parsed = _load_json_cached(path or CODEX_PRICES_PATH)
    source = "file"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("models"), dict):
        parsed = CODEX_SEED_PRICES
        source = "seed"
    table: dict[str, dict[str, float]] = {}
    for model, entry in parsed.get("models", {}).items():
        if not isinstance(entry, dict):
            continue
        normalized: dict[str, float] = {}
        broken = False
        for field in ("input", "cached_input", "output"):
            value = _mtok_to_per_token(entry.get(field))
            if value is None:
                broken = True
                break
            normalized[field] = value
        if not broken:
            table[str(model)] = normalized
    return table, source


def match_price(
    model: Any, table: dict[str, dict[str, float]]
) -> dict[str, float] | None:
    """精确命中优先，其次最长前缀；无命中返回 None（金额显示 —）。"""
    name = str(model or "")
    if not name or name == "<synthetic>":
        return None
    if name in table:
        return table[name]
    best_key = ""
    for key in table:
        if name.startswith(key) and len(key) > len(best_key):
            best_key = key
    return table.get(best_key) if best_key else None


def _all_none(tokens: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return all(tokens.get(field) is None for field in fields)


def claude_cost_microusd(
    tokens: dict[str, Any], price: dict[str, float] | None
) -> int | None:
    """四类 token 分开计价；1h 缓存写入是 2× input（prices.json 只给 5m 档）。"""
    if price is None or _all_none(
        tokens,
        ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"),
    ):
        return None

    def count(field: str) -> int:
        return int(tokens.get(field) or 0)

    usd = (
        count("input") * price["input"]
        + count("output") * price["output"]
        + count("cache_read") * price["cache_read"]
        + count("cache_write_5m") * price["cache_write"]
        + count("cache_write_1h") * price["input"] * 2.0
    )
    return int(round(usd * 1_000_000))


def codex_cost_microusd(
    tokens: dict[str, Any], price: dict[str, float] | None
) -> int | None:
    """OpenAI 口径：input 已包含 cached，须先减；reasoning 已在 output 内。"""
    if price is None or _all_none(tokens, ("input", "output")):
        return None
    input_tokens = int(tokens.get("input") or 0)
    cached = min(int(tokens.get("cached_input") or 0), input_tokens)
    output_tokens = int(tokens.get("output") or 0)
    usd = (
        (input_tokens - cached) * price["input"]
        + cached * price["cached_input"]
        + output_tokens * price["output"]
    )
    return int(round(usd * 1_000_000))


TURN_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
)


def history_connect(
    db_path: Path | None = None, readonly: bool = False
) -> sqlite3.Connection:
    path = db_path or HISTORY_DB_PATH
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        connection = sqlite3.connect(str(path), timeout=15)
        connection.execute("PRAGMA journal_mode=WAL")
    connection.row_factory = sqlite3.Row
    return connection


def history_init(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingest_files (
          path TEXT PRIMARY KEY,
          session_key TEXT NOT NULL,
          byte_offset INTEGER NOT NULL DEFAULT 0,
          file_size INTEGER NOT NULL DEFAULT 0,
          cursor_json TEXT NOT NULL DEFAULT '{}',
          last_ingested_at_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
          session_key TEXT PRIMARY KEY,
          platform TEXT NOT NULL,
          native_id TEXT NOT NULL,
          parent_session_key TEXT,
          name TEXT,
          cwd_label TEXT,
          model TEXT,
          models_json TEXT NOT NULL DEFAULT '[]',
          reasoning_effort TEXT,
          started_at_ms INTEGER,
          last_active_at_ms INTEGER,
          ended_at_ms INTEGER,
          last_status TEXT,
          turn_count INTEGER NOT NULL DEFAULT 0,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cache_read_tokens INTEGER,
          cache_write_5m_tokens INTEGER,
          cache_write_1h_tokens INTEGER,
          cached_input_tokens INTEGER,
          reasoning_output_tokens INTEGER,
          total_tokens INTEGER,
          context_peak_tokens INTEGER,
          context_window INTEGER,
          context_peak_pct INTEGER,
          updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_active
          ON sessions(last_active_at_ms);
        CREATE INDEX IF NOT EXISTS idx_sessions_parent
          ON sessions(parent_session_key);
        CREATE TABLE IF NOT EXISTS turns (
          session_key TEXT NOT NULL,
          turn_key TEXT NOT NULL,
          started_at_ms INTEGER,
          ended_at_ms INTEGER,
          model TEXT,
          reasoning_effort TEXT,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cache_read_tokens INTEGER,
          cache_write_5m_tokens INTEGER,
          cache_write_1h_tokens INTEGER,
          cached_input_tokens INTEGER,
          reasoning_output_tokens INTEGER,
          context_tokens INTEGER,
          PRIMARY KEY (session_key, turn_key)
        );
        CREATE INDEX IF NOT EXISTS idx_turns_time
          ON turns(session_key, started_at_ms);
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(HISTORY_SCHEMA_VERSION),),
    )
    connection.commit()


def upsert_session_meta(
    connection: sqlite3.Connection,
    session_key: str,
    platform: str,
    native_id: str,
    **fields: Any,
) -> None:
    """只用新观测到的非空值覆盖；NULL 永远不冲掉已知事实。"""
    known = {
        "parent_session_key",
        "name",
        "cwd_label",
        "model",
        "reasoning_effort",
        "started_at_ms",
        "last_active_at_ms",
        "last_status",
        "context_window",
        "total_tokens",
    }
    payload = {
        key: value
        for key, value in fields.items()
        if key in known and value is not None
    }
    columns = ["session_key", "platform", "native_id", "updated_at_ms"]
    values: list[Any] = [session_key, platform, native_id, now_ms()]
    updates = ["updated_at_ms=excluded.updated_at_ms"]
    for key, value in payload.items():
        columns.append(key)
        values.append(value)
        if key == "total_tokens":
            # threads 表的 tokens_used 只当兜底：轮级明细一旦存在以明细为准。
            updates.append(
                "total_tokens=COALESCE(sessions.total_tokens, excluded.total_tokens)"
            )
        elif key == "started_at_ms":
            updates.append(
                "started_at_ms=COALESCE(excluded.started_at_ms, sessions.started_at_ms)"
            )
        else:
            updates.append(f"{key}=COALESCE(excluded.{key}, sessions.{key})")
    connection.execute(
        f"""
        INSERT INTO sessions ({', '.join(columns)})
        VALUES ({', '.join('?' for _ in columns)})
        ON CONFLICT(session_key) DO UPDATE SET {', '.join(updates)}
        """,
        values,
    )


def note_session_model(
    connection: sqlite3.Connection, session_key: str, model: str
) -> None:
    row = connection.execute(
        "SELECT models_json FROM sessions WHERE session_key=?", (session_key,)
    ).fetchone()
    if row is None:
        return
    try:
        models = json.loads(row["models_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        models = []
    if not isinstance(models, list):
        models = []
    if model not in models:
        models.append(model)
        connection.execute(
            "UPDATE sessions SET models_json=?, model=? WHERE session_key=?",
            (json.dumps(models, ensure_ascii=False), model, session_key),
        )
    else:
        connection.execute(
            "UPDATE sessions SET model=? WHERE session_key=?",
            (model, session_key),
        )


def read_new_lines(
    path: Path, offset: int, budget: int
) -> tuple[list[bytes], int, bool]:
    """从 offset 续读整行；尾部半行不消费。返回 (行, 新偏移, 是否被截断过)。"""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, False
    truncated = size < offset
    if truncated:
        offset = 0
    if size <= offset or budget <= 0:
        return [], offset, truncated
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(min(budget, size - offset))
    except OSError:
        return [], offset, truncated
    end = raw.rfind(b"\n")
    if end == -1:
        return [], offset, truncated
    return raw[:end].split(b"\n"), offset + end + 1, truncated


def _line_json(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def upsert_claude_turn(
    connection: sqlite3.Connection,
    session_key: str,
    turn_key: str,
    started_at_ms: int | None,
    ended_at_ms: int | None,
    model: str | None,
    effort: str | None,
    tokens: dict[str, Any],
    context_tokens: int | None,
) -> None:
    """同一 requestId 的流式行重复累计用量，last-wins 整行替换。"""
    connection.execute(
        """
        INSERT INTO turns (
          session_key, turn_key, started_at_ms, ended_at_ms, model,
          reasoning_effort, input_tokens, output_tokens, cache_read_tokens,
          cache_write_5m_tokens, cache_write_1h_tokens, cached_input_tokens,
          reasoning_output_tokens, context_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_key, turn_key) DO UPDATE SET
          ended_at_ms=excluded.ended_at_ms,
          model=COALESCE(excluded.model, turns.model),
          reasoning_effort=COALESCE(excluded.reasoning_effort, turns.reasoning_effort),
          input_tokens=excluded.input_tokens,
          output_tokens=excluded.output_tokens,
          cache_read_tokens=excluded.cache_read_tokens,
          cache_write_5m_tokens=excluded.cache_write_5m_tokens,
          cache_write_1h_tokens=excluded.cache_write_1h_tokens,
          cached_input_tokens=excluded.cached_input_tokens,
          reasoning_output_tokens=excluded.reasoning_output_tokens,
          context_tokens=MAX(
            COALESCE(turns.context_tokens, 0),
            COALESCE(excluded.context_tokens, 0)
          )
        """,
        (
            session_key,
            turn_key,
            started_at_ms,
            ended_at_ms,
            model,
            effort,
            tokens.get("input_tokens"),
            tokens.get("output_tokens"),
            tokens.get("cache_read_tokens"),
            tokens.get("cache_write_5m_tokens"),
            tokens.get("cache_write_1h_tokens"),
            tokens.get("cached_input_tokens"),
            tokens.get("reasoning_output_tokens"),
            context_tokens,
        ),
    )


def ingest_claude_lines(
    connection: sqlite3.Connection,
    session_key: str,
    lines: list[bytes],
    cursor: dict[str, Any],
) -> bool:
    """只提取时间戳、模型、强度与用量数字；对话内容一个字都不进库。"""
    touched = False
    for raw in lines:
        line = _line_json(raw)
        if line is None:
            continue
        if not cursor.get("cwd_noted") and line.get("cwd"):
            connection.execute(
                "UPDATE sessions SET cwd_label=COALESCE(cwd_label, ?) "
                "WHERE session_key=?",
                (cwd_label(str(line["cwd"])), session_key),
            )
            cursor["cwd_noted"] = True
        line_type = str(line.get("type") or "")
        if line_type == "user":
            stamp = parse_iso_ms(line.get("timestamp"))
            if stamp:
                cursor["last_user_ts"] = stamp
            continue
        if line_type != "assistant" or line.get("isSidechain") is True:
            continue
        message = line.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        model = str(message.get("model") or "") or None
        if model == "<synthetic>":
            continue
        stamp = parse_iso_ms(line.get("timestamp"))
        request_id = str(line.get("requestId") or "") or (
            f"ts{stamp}" if stamp else None
        )
        if request_id is None:
            continue
        effort = str(line.get("effort") or "") or None

        cache_creation = usage.get("cache_creation")
        write_5m = write_1h = None
        if isinstance(cache_creation, dict):
            write_5m = _as_int(cache_creation.get("ephemeral_5m_input_tokens"))
            write_1h = _as_int(cache_creation.get("ephemeral_1h_input_tokens"))
        if write_5m is None and write_1h is None:
            write_5m = _as_int(usage.get("cache_creation_input_tokens"))
        details = usage.get("output_tokens_details")
        thinking = (
            _as_int(details.get("thinking_tokens"))
            if isinstance(details, dict)
            else None
        )
        tokens = {
            "input_tokens": _as_int(usage.get("input_tokens")),
            "output_tokens": _as_int(usage.get("output_tokens")),
            "cache_read_tokens": _as_int(usage.get("cache_read_input_tokens")),
            "cache_write_5m_tokens": write_5m,
            "cache_write_1h_tokens": write_1h,
            "cached_input_tokens": None,
            "reasoning_output_tokens": thinking,
        }
        context_tokens = (
            (tokens["input_tokens"] or 0)
            + (tokens["cache_read_tokens"] or 0)
            + (write_5m or 0)
            + (write_1h or 0)
        ) or None

        started = int(cursor.get("last_user_ts") or 0) or (stamp or None)
        upsert_claude_turn(
            connection,
            session_key,
            request_id,
            started,
            stamp or None,
            model,
            effort,
            tokens,
            context_tokens,
        )
        if model:
            note_session_model(connection, session_key, model)
        if effort:
            connection.execute(
                "UPDATE sessions SET reasoning_effort=? WHERE session_key=?",
                (effort, session_key),
            )
        touched = True
    return touched


def ingest_codex_events(
    connection: sqlite3.Connection,
    session_key: str,
    lines: list[bytes],
    cursor: dict[str, Any],
) -> bool:
    """rollout 的 token_count 是累计值：相邻快照做差得每轮增量。"""
    touched = False
    for raw in lines:
        event = _line_json(raw)
        if event is None:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "")
        stamp = parse_iso_ms(event.get("timestamp"))

        if event_type == "task_started":
            turn_key = f"t{stamp or now_ms()}"
            cursor["open_turn"] = turn_key
            connection.execute(
                """
                INSERT OR IGNORE INTO turns (session_key, turn_key, started_at_ms)
                VALUES (?, ?, ?)
                """,
                (session_key, turn_key, stamp or None),
            )
            touched = True
            continue

        if event_type in {"task_complete", "turn_aborted"}:
            open_turn = cursor.pop("open_turn", None)
            if open_turn and stamp:
                connection.execute(
                    "UPDATE turns SET ended_at_ms=? "
                    "WHERE session_key=? AND turn_key=?",
                    (stamp, session_key, open_turn),
                )
                touched = True
            continue

        if event_type != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        totals = info.get("total_token_usage")
        if not isinstance(totals, dict):
            continue
        window = _as_int(info.get("model_context_window"))
        if window:
            connection.execute(
                "UPDATE sessions SET context_window=? WHERE session_key=?",
                (window, session_key),
            )
        last_usage = info.get("last_token_usage")
        context_tokens = (
            _as_int(last_usage.get("input_tokens"))
            if isinstance(last_usage, dict)
            else None
        )

        current = {
            field: _as_int(totals.get(field)) or 0
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        }
        previous = cursor.get("last_totals")
        if not isinstance(previous, dict):
            # 新文件从 0 起：若该线程已有历史轮次（rollout 轮转/resume），
            # 首个累计快照可能包含旧文件已计过的量——只立基线不记增量，
            # 宁可少记一段也不双算。
            if cursor.get("primed_skip_first"):
                cursor["last_totals"] = current
                cursor.pop("primed_skip_first", None)
                if context_tokens is not None:
                    _bump_codex_context(
                        connection, session_key, cursor, context_tokens, stamp
                    )
                touched = True
                continue
            previous = {field: 0 for field in current}
        deltas = {
            field: current[field] - int(previous.get(field) or 0)
            for field in current
        }
        if any(delta < 0 for delta in deltas.values()):
            # 计数回退（同文件被复用重开）：以新值为基线，绝不写负数。
            cursor["last_totals"] = current
            continue
        cursor["last_totals"] = current

        open_turn = cursor.get("open_turn")
        if not open_turn:
            open_turn = f"t{stamp or now_ms()}"
            cursor["open_turn"] = open_turn
            connection.execute(
                """
                INSERT OR IGNORE INTO turns (session_key, turn_key, started_at_ms)
                VALUES (?, ?, ?)
                """,
                (session_key, open_turn, stamp or None),
            )
        connection.execute(
            """
            UPDATE turns SET
              ended_at_ms=?,
              input_tokens=COALESCE(input_tokens, 0) + ?,
              cached_input_tokens=COALESCE(cached_input_tokens, 0) + ?,
              cache_write_5m_tokens=COALESCE(cache_write_5m_tokens, 0) + ?,
              output_tokens=COALESCE(output_tokens, 0) + ?,
              reasoning_output_tokens=COALESCE(reasoning_output_tokens, 0) + ?,
              context_tokens=MAX(COALESCE(context_tokens, 0), ?)
            WHERE session_key=? AND turn_key=?
            """,
            (
                stamp or None,
                deltas["input_tokens"],
                deltas["cached_input_tokens"],
                deltas["cache_write_input_tokens"],
                deltas["output_tokens"],
                deltas["reasoning_output_tokens"],
                context_tokens or 0,
                session_key,
                open_turn,
            ),
        )
        touched = True
    return touched


def _bump_codex_context(
    connection: sqlite3.Connection,
    session_key: str,
    cursor: dict[str, Any],
    context_tokens: int,
    stamp: int,
) -> None:
    open_turn = cursor.get("open_turn")
    if not open_turn:
        return
    connection.execute(
        """
        UPDATE turns SET
          ended_at_ms=COALESCE(?, ended_at_ms),
          context_tokens=MAX(COALESCE(context_tokens, 0), ?)
        WHERE session_key=? AND turn_key=?
        """,
        (stamp or None, context_tokens, session_key, open_turn),
    )


def recompute_session(connection: sqlite3.Connection, session_key: str) -> None:
    row = connection.execute(
        """
        SELECT COUNT(*) AS turn_count,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_5m_tokens) AS cache_write_5m_tokens,
               SUM(cache_write_1h_tokens) AS cache_write_1h_tokens,
               SUM(cached_input_tokens) AS cached_input_tokens,
               SUM(reasoning_output_tokens) AS reasoning_output_tokens,
               MAX(context_tokens) AS context_peak,
               MIN(started_at_ms) AS first_started,
               MAX(COALESCE(ended_at_ms, started_at_ms)) AS last_ended
        FROM turns WHERE session_key=?
        """,
        (session_key,),
    ).fetchone()
    if row is None:
        return
    session = connection.execute(
        "SELECT platform, context_window, total_tokens FROM sessions "
        "WHERE session_key=?",
        (session_key,),
    ).fetchone()
    if session is None:
        return

    token_parts = [
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_tokens"],
        row["cache_write_5m_tokens"],
        row["cache_write_1h_tokens"],
    ]
    if str(session["platform"]) == "codex":
        # OpenAI 口径 input 已含 cached，总量 = input + output。
        token_parts = [row["input_tokens"], row["output_tokens"]]
    known_parts = [part for part in token_parts if part is not None]
    total = sum(known_parts) if known_parts else session["total_tokens"]

    window = session["context_window"]
    if str(session["platform"]) == "claude" and not window:
        window = CLAUDE_CONTEXT_WINDOW if CLAUDE_CONTEXT_WINDOW > 0 else None
    peak = row["context_peak"]
    pct = None
    if peak and window and 0 < peak <= window:
        pct = int(round(peak * 100.0 / window))

    if int(row["turn_count"] or 0) > 0:
        connection.execute(
            """
            UPDATE sessions SET
              turn_count=?, input_tokens=?, output_tokens=?,
              cache_read_tokens=?, cache_write_5m_tokens=?,
              cache_write_1h_tokens=?, cached_input_tokens=?,
              reasoning_output_tokens=?, total_tokens=?,
              context_peak_tokens=?, context_window=?, context_peak_pct=?,
              started_at_ms=COALESCE(started_at_ms, ?),
              last_active_at_ms=NULLIF(
                MAX(COALESCE(last_active_at_ms, 0), COALESCE(?, 0)), 0
              ),
              updated_at_ms=?
            WHERE session_key=?
            """,
            (
                row["turn_count"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_read_tokens"],
                row["cache_write_5m_tokens"],
                row["cache_write_1h_tokens"],
                row["cached_input_tokens"],
                row["reasoning_output_tokens"],
                total,
                peak,
                window,
                pct,
                row["first_started"],
                row["last_ended"],
                now_ms(),
                session_key,
            ),
        )


def claude_history_targets() -> list[dict[str, Any]]:
    """活跃会话 + 回填窗口内的 transcript + Task 子代理文件。"""
    targets: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    live_meta: dict[str, dict[str, Any]] = {}

    if CLAUDE_SESSIONS_DIR.exists():
        for path in CLAUDE_SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = str(data.get("sessionId") or "")
            if session_id:
                live_meta[session_id] = data

    cutoff = time.time() - HISTORY_BACKFILL_DAYS * 86_400
    candidates: dict[str, Path] = {}
    for session_id in live_meta:
        path = transcript_path(session_id)
        if path is not None:
            candidates[session_id] = path
    if CLAUDE_PROJECTS_DIR.exists():
        for path in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            candidates.setdefault(path.stem, path)

    for session_id, path in candidates.items():
        if str(path) in seen_paths:
            continue
        seen_paths.add(str(path))
        meta = live_meta.get(session_id, {})
        session_key = f"claude:{session_id}"
        cwd = str(meta.get("cwd") or "")
        targets.append(
            {
                "path": path,
                "session_key": session_key,
                "platform": "claude",
                "native_id": session_id,
                "meta": {
                    "name": str(meta.get("name") or "") or None,
                    "cwd_label": cwd_label(cwd) if cwd else None,
                    "started_at_ms": normalize_ms(meta.get("startedAt")) or None,
                },
            }
        )
        subagents_dir = path.parent / session_id / "subagents"
        if not subagents_dir.is_dir():
            continue
        for agent_file in sorted(subagents_dir.glob("agent-*.jsonl")):
            agent_stem = agent_file.stem
            agent_name = None
            meta_file = agent_file.with_name(f"{agent_stem}.meta.json")
            try:
                agent_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(agent_meta, dict):
                    agent_name = (
                        str(
                            agent_meta.get("agentType")
                            or agent_meta.get("name")
                            or ""
                        )
                        or None
                    )
            except (OSError, json.JSONDecodeError):
                pass
            targets.append(
                {
                    "path": agent_file,
                    "session_key": f"{session_key}/{agent_stem}",
                    "platform": "claude",
                    "native_id": agent_stem,
                    "meta": {
                        "parent_session_key": session_key,
                        "name": agent_name or agent_stem,
                    },
                }
            )
    return targets


def codex_history_targets() -> list[dict[str, Any]]:
    database, _detail = resolve_codex_db()
    if database is None:
        return []
    try:
        connection = codex_connect_ro(database)
        connection.row_factory = sqlite3.Row
        columns = codex_table_columns(connection)
        if codex_schema_issue(columns):
            connection.close()
            return []
        activity = codex_activity_expression(columns)
        title = codex_text_expression(
            columns, ("title", "first_user_message", "preview")
        )
        optional = {
            name: (name if name in columns else "NULL")
            for name in (
                "agent_nickname",
                "name",
                "thread_source",
                "model",
                "reasoning_effort",
                "tokens_used",
            )
        }
        cutoff = now_ms() - HISTORY_BACKFILL_DAYS * 86_400_000
        rows = list(
            connection.execute(
                f"""
                SELECT id, cwd, source, rollout_path,
                       {title} AS title,
                       {optional['agent_nickname']} AS agent_nickname,
                       {optional['name']} AS name,
                       {optional['thread_source']} AS thread_source,
                       {optional['model']} AS model,
                       {optional['reasoning_effort']} AS reasoning_effort,
                       {optional['tokens_used']} AS tokens_used,
                       {activity} AS activity_ms
                FROM threads
                WHERE {activity} >= ?
                ORDER BY activity_ms DESC
                LIMIT 200
                """,
                (cutoff,),
            )
        )
        connection.close()
    except sqlite3.Error:
        return []

    user_by_cwd: dict[str, str] = {}
    for row in rows:
        source = str(row["source"] or "")
        thread_source = str(row["thread_source"] or "")
        if thread_source == "user" or (
            not thread_source and source in {"vscode", "exec"}
        ):
            user_by_cwd.setdefault(str(row["cwd"] or ""), str(row["id"]))

    targets: list[dict[str, Any]] = []
    for row in rows:
        thread_id = str(row["id"])
        rollout = str(row["rollout_path"] or "")
        if not rollout:
            continue
        cwd = str(row["cwd"] or "")
        source = str(row["source"] or "")
        thread_source = str(row["thread_source"] or "")
        parent_key = None
        label = clean_label(
            row["agent_nickname"] or row["name"] or row["title"] or "",
            fallback=cwd_label(cwd) if cwd else "Codex",
        )
        if thread_source == "subagent" or (
            not thread_source and '"subagent"' in source
        ):
            label = satellite_name(source)
            parent_id = user_by_cwd.get(cwd)
            if parent_id and parent_id != thread_id:
                parent_key = f"codex:{parent_id}"
        targets.append(
            {
                "path": Path(rollout),
                "session_key": f"codex:{thread_id}",
                "platform": "codex",
                "native_id": thread_id,
                "meta": {
                    "parent_session_key": parent_key,
                    "name": label,
                    "cwd_label": cwd_label(cwd) if cwd else None,
                    "model": str(row["model"] or "") or None,
                    "reasoning_effort": str(row["reasoning_effort"] or "") or None,
                    "total_tokens": _as_int(row["tokens_used"]),
                    "last_active_at_ms": normalize_ms(row["activity_ms"]) or None,
                },
            }
        )
    return targets


def ingest_target(
    connection: sqlite3.Connection, target: dict[str, Any], budget: int
) -> tuple[int, bool]:
    """续读单个文件；offset、cursor 与数据行同一事务提交。返回 (消费字节, 是否有新数据)。"""
    path: Path = target["path"]
    session_key: str = target["session_key"]
    try:
        size = path.stat().st_size
    except OSError:
        return 0, False

    state = connection.execute(
        "SELECT byte_offset, file_size, cursor_json FROM ingest_files WHERE path=?",
        (str(path),),
    ).fetchone()
    offset = int(state["byte_offset"]) if state else 0
    if state and int(state["file_size"]) == size and offset >= size:
        return 0, False
    try:
        cursor = json.loads(state["cursor_json"]) if state else {}
    except (json.JSONDecodeError, TypeError):
        cursor = {}
    if not isinstance(cursor, dict):
        cursor = {}

    if state is None and target["platform"] == "codex":
        prior = connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_key=? "
            "AND input_tokens IS NOT NULL",
            (session_key,),
        ).fetchone()
        if prior and int(prior[0]) > 0:
            cursor["primed_skip_first"] = True

    lines, new_offset, truncated = read_new_lines(path, offset, budget)
    if truncated:
        cursor = {}
    upsert_session_meta(
        connection,
        session_key,
        target["platform"],
        target["native_id"],
        **target.get("meta", {}),
    )
    touched = False
    if lines:
        if target["platform"] == "claude":
            touched = ingest_claude_lines(connection, session_key, lines, cursor)
        else:
            touched = ingest_codex_events(connection, session_key, lines, cursor)
    if touched:
        recompute_session(connection, session_key)
    connection.execute(
        """
        INSERT INTO ingest_files (
          path, session_key, byte_offset, file_size, cursor_json,
          last_ingested_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          session_key=excluded.session_key,
          byte_offset=excluded.byte_offset,
          file_size=excluded.file_size,
          cursor_json=excluded.cursor_json,
          last_ingested_at_ms=excluded.last_ingested_at_ms
        """,
        (
            str(path),
            session_key,
            new_offset,
            size,
            json.dumps(cursor, ensure_ascii=False),
            now_ms(),
        ),
    )
    connection.commit()
    return max(0, new_offset - offset), touched


def mark_ended_sessions(connection: sqlite3.Connection) -> None:
    """用最近一次快照对账：从面板上消失的会话盖上结束时间。"""
    with _snapshot_ready:
        snap = _latest_snapshot
    if snap is None:
        return
    live: dict[str, str] = {}
    for platform in ("claude", "codex"):
        for agent in snap.get(platform, []):
            live[f"{platform}:{agent['id']}"] = str(agent.get("status") or "")
            for satellite in agent.get("satellites", []):
                live[f"{platform}:{satellite['id']}"] = str(
                    satellite.get("status") or ""
                )
    rows = connection.execute(
        "SELECT session_key, last_active_at_ms, ended_at_ms FROM sessions "
        "WHERE parent_session_key IS NULL OR platform='codex'"
    ).fetchall()
    for row in rows:
        key = str(row["session_key"])
        if key in live:
            connection.execute(
                "UPDATE sessions SET last_status=?, ended_at_ms=NULL "
                "WHERE session_key=?",
                (live[key], key),
            )
        elif row["ended_at_ms"] is None and row["last_active_at_ms"]:
            connection.execute(
                "UPDATE sessions SET ended_at_ms=last_active_at_ms "
                "WHERE session_key=?",
                (key,),
            )
    connection.commit()


def prune_history(connection: sqlite3.Connection) -> None:
    current = now_ms()
    connection.execute(
        "DELETE FROM turns WHERE COALESCE(ended_at_ms, started_at_ms, 0) < ?",
        (current - HISTORY_TURNS_RETENTION_DAYS * 86_400_000,),
    )
    connection.execute(
        "DELETE FROM sessions WHERE COALESCE(last_active_at_ms, updated_at_ms) < ?",
        (current - HISTORY_RETENTION_DAYS * 86_400_000,),
    )
    connection.execute(
        "DELETE FROM ingest_files WHERE last_ingested_at_ms < ?",
        (current - HISTORY_RETENTION_DAYS * 86_400_000,),
    )
    connection.commit()


_history_pass_count = 0


def history_pass(db_path: Path | None = None) -> None:
    global _history_pass_count
    connection = history_connect(db_path)
    try:
        history_init(connection)
        targets = claude_history_targets() + codex_history_targets()

        def target_mtime(target: dict[str, Any]) -> float:
            try:
                return target["path"].stat().st_mtime
            except OSError:
                return 0.0

        targets.sort(key=target_mtime, reverse=True)
        budget = HISTORY_INGEST_BYTE_BUDGET
        for target in targets:
            if budget <= 0:
                break
            consumed, _touched = ingest_target(connection, target, budget)
            budget -= consumed
        mark_ended_sessions(connection)
        _history_pass_count += 1
        if _history_pass_count % 60 == 1:
            prune_history(connection)
        _history_health.update(
            {"state": "ok", "detail": "", "lastPassAtMs": now_ms()}
        )
    finally:
        connection.close()


def history_loop(stop: threading.Event) -> None:
    ensure_codex_price_file()
    while not stop.is_set():
        try:
            history_pass()
        except Exception as error:  # noqa: BLE001 - 面板必须活过一次坏数据
            _history_health.update({"state": "error", "detail": str(error)})
        stop.wait(HISTORY_INTERVAL_MS / 1000)


def _session_tokens(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "input": row["input_tokens"],
        "output": row["output_tokens"],
        "cache_read": row["cache_read_tokens"],
        "cache_write_5m": row["cache_write_5m_tokens"],
        "cache_write_1h": row["cache_write_1h_tokens"],
        "cached_input": row["cached_input_tokens"],
        "reasoning_output": row["reasoning_output_tokens"],
        "total": row["total_tokens"],
    }


def _cost_for(
    platform: str,
    model: Any,
    tokens: dict[str, Any],
    claude_prices: dict[str, dict[str, float]],
    codex_prices: dict[str, dict[str, float]],
) -> int | None:
    if platform == "codex":
        return codex_cost_microusd(
            {
                "input": tokens.get("input"),
                "cached_input": tokens.get("cached_input"),
                "output": tokens.get("output"),
            },
            match_price(model, codex_prices),
        )
    return claude_cost_microusd(
        {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "cache_read": tokens.get("cache_read"),
            "cache_write_5m": tokens.get("cache_write_5m"),
            "cache_write_1h": tokens.get("cache_write_1h"),
        },
        match_price(model, claude_prices),
    )


def _live_status_map() -> dict[str, str]:
    with _snapshot_ready:
        snap = _latest_snapshot
    live: dict[str, str] = {}
    if snap is None:
        return live
    for platform in ("claude", "codex"):
        for agent in snap.get(platform, []):
            live[f"{platform}:{agent['id']}"] = str(agent.get("status") or "")
            for satellite in agent.get("satellites", []):
                live[f"{platform}:{satellite['id']}"] = str(
                    satellite.get("status") or ""
                )
    return live


def _session_object(
    row: sqlite3.Row,
    live: dict[str, str],
    claude_prices: dict[str, dict[str, float]],
    codex_prices: dict[str, dict[str, float]],
) -> dict[str, Any]:
    platform = str(row["platform"])
    key = str(row["session_key"])
    tokens = _session_tokens(row)
    try:
        models = json.loads(row["models_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        models = []
    return {
        "sessionKey": key,
        "platform": platform,
        "id": str(row["native_id"]),
        "name": row["name"],
        "cwdLabel": row["cwd_label"],
        "model": row["model"],
        "effort": row["reasoning_effort"],
        "mixedModels": isinstance(models, list) and len(models) > 1,
        "startedAtMs": row["started_at_ms"],
        "lastActiveAtMs": row["last_active_at_ms"],
        "endedAtMs": row["ended_at_ms"],
        "live": key in live,
        "status": live.get(key) or row["last_status"],
        "turnCount": row["turn_count"],
        "tokens": {
            "input": tokens["input"],
            "output": tokens["output"],
            "cacheRead": tokens["cache_read"],
            "cacheWrite5m": tokens["cache_write_5m"],
            "cacheWrite1h": tokens["cache_write_1h"],
            "cachedInput": tokens["cached_input"],
            "reasoningOutput": tokens["reasoning_output"],
            "total": tokens["total"],
        },
        "contextPeakTokens": row["context_peak_tokens"],
        "contextWindow": row["context_window"],
        "contextPeakPct": row["context_peak_pct"],
        "costMicroUsd": _cost_for(
            platform, row["model"], tokens, claude_prices, codex_prices
        ),
        "costLabel": "估算" if platform == "codex" else "等效API标价",
    }


def history_summary_payload(days: int, limit: int) -> dict[str, Any]:
    days = max(1, min(90, days))
    limit = max(1, min(200, limit))
    base: dict[str, Any] = {
        "schemaVersion": HISTORY_SCHEMA_VERSION,
        "generatedAt": now_ms(),
        "state": "ok",
        "detail": "",
        "sessions": [],
    }
    claude_prices, claude_source = load_claude_prices()
    codex_prices, codex_source = load_codex_prices()
    base["pricing"] = {"claude": claude_source, "codex": codex_source}
    if not HISTORY_DB_PATH.exists():
        base["state"] = "initializing"
        base["detail"] = str(_history_health.get("detail") or "正在建立历史库")
        return base
    if _history_health.get("state") == "error":
        base["detail"] = str(_history_health.get("detail") or "")

    try:
        connection = history_connect(readonly=True)
    except sqlite3.Error as error:
        base["state"] = "initializing"
        base["detail"] = str(error)
        return base
    try:
        cutoff = now_ms() - days * 86_400_000
        rows = connection.execute(
            """
            SELECT * FROM sessions
            WHERE parent_session_key IS NULL
              AND COALESCE(last_active_at_ms, updated_at_ms) >= ?
            ORDER BY COALESCE(last_active_at_ms, updated_at_ms) DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        children = connection.execute(
            "SELECT * FROM sessions WHERE parent_session_key IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as error:
        base["state"] = "initializing"
        base["detail"] = str(error)
        return base
    finally:
        connection.close()

    child_map: dict[str, list[sqlite3.Row]] = {}
    for child in children:
        child_map.setdefault(str(child["parent_session_key"]), []).append(child)

    live = _live_status_map()
    sessions = []
    for row in rows:
        obj = _session_object(row, live, claude_prices, codex_prices)
        related = child_map.get(str(row["session_key"]), [])
        if related:
            child_costs = [
                _cost_for(
                    str(child["platform"]),
                    child["model"],
                    _session_tokens(child),
                    claude_prices,
                    codex_prices,
                )
                for child in related
            ]
            known_costs = [cost for cost in child_costs if cost is not None]
            child_tokens = [
                child["total_tokens"]
                for child in related
                if child["total_tokens"] is not None
            ]
            obj["subagents"] = {
                "count": len(related),
                "totalTokens": sum(child_tokens) if child_tokens else None,
                "costMicroUsd": sum(known_costs) if known_costs else None,
            }
        else:
            obj["subagents"] = {"count": 0, "totalTokens": None, "costMicroUsd": None}
        sessions.append(obj)
    base["sessions"] = sessions
    return base


def history_session_payload(session_key: str, turns_limit: int) -> dict[str, Any]:
    turns_limit = max(1, min(500, turns_limit))
    claude_prices, _ = load_claude_prices()
    codex_prices, _ = load_codex_prices()
    if not HISTORY_DB_PATH.exists():
        return {"error": "历史库尚未建立"}
    try:
        connection = history_connect(readonly=True)
    except sqlite3.Error as error:
        return {"error": str(error)}
    try:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_key=?", (session_key,)
        ).fetchone()
        if row is None:
            return {"error": "没有这个会话的记录"}
        turn_rows = connection.execute(
            """
            SELECT * FROM turns WHERE session_key=?
            ORDER BY COALESCE(started_at_ms, 0) DESC LIMIT ?
            """,
            (session_key, turns_limit),
        ).fetchall()
    except sqlite3.Error as error:
        return {"error": str(error)}
    finally:
        connection.close()

    live = _live_status_map()
    payload = _session_object(row, live, claude_prices, codex_prices)
    platform = str(row["platform"])
    session_model = row["model"]
    turns = []
    for turn in turn_rows:
        tokens = {
            "input": turn["input_tokens"],
            "output": turn["output_tokens"],
            "cache_read": turn["cache_read_tokens"],
            "cache_write_5m": turn["cache_write_5m_tokens"],
            "cache_write_1h": turn["cache_write_1h_tokens"],
            "cached_input": turn["cached_input_tokens"],
            "reasoning_output": turn["reasoning_output_tokens"],
        }
        model = turn["model"] or session_model
        turns.append(
            {
                "turnKey": str(turn["turn_key"]),
                "startedAtMs": turn["started_at_ms"],
                "endedAtMs": turn["ended_at_ms"],
                "model": model,
                "effort": turn["reasoning_effort"],
                "tokens": {
                    "input": tokens["input"],
                    "output": tokens["output"],
                    "cacheRead": tokens["cache_read"],
                    "cacheWrite5m": tokens["cache_write_5m"],
                    "cacheWrite1h": tokens["cache_write_1h"],
                    "cachedInput": tokens["cached_input"],
                    "reasoningOutput": tokens["reasoning_output"],
                },
                "contextTokens": turn["context_tokens"],
                "costMicroUsd": _cost_for(
                    platform, model, tokens, claude_prices, codex_prices
                ),
            }
        )
    payload["turns"] = turns
    return payload


def snapshot(locked_codex_ids: set[str] | None = None) -> dict[str, Any]:
    current_ms = now_ms()
    table = scan_processes()
    claude, claude_health = load_claude_sessions(current_ms, table)
    codex, codex_health = load_codex_threads(current_ms, locked_codex_ids)
    prune_tracking(
        {completion_key("claude", agent["id"]) for agent in claude}
        | {completion_key("codex", agent["id"]) for agent in codex}
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": current_ms,
        "sources": {"claude": claude_health, "codex": codex_health},
        "notifications": dict(_notify_health),
        "claude": claude,
        "codex": codex,
        "counts": {
            "claude": len(claude),
            "codex": len(codex),
            "satellites": sum(
                len(agent["satellites"]) for agent in [*claude, *codex]
            ),
        },
    }


def snapshot_revision(payload: dict[str, Any]) -> str:
    material = json.dumps(
        {key: value for key, value in payload.items() if key != "generatedAt"},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def send_mac_notification(title: str, subtitle: str, message: str) -> None:
    """Best effort: osascript reports launch failures, silent drops it cannot."""
    script = """
on run argv
  display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, "--", title, subtitle, message],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _notify_health.update({"state": "error", "detail": str(error)})
        return
    if result.returncode:
        _notify_health.update(
            {"state": "error", "detail": result.stderr.strip() or "osascript 调用失败"}
        )
        return
    _notify_health.update({"state": "ok", "detail": ""})


def notify_key(platform: str, agent: dict[str, Any]) -> str:
    return f"{platform}:{agent['id']}:{agent['status']}:{agent.get('completionId') or 0}"


def dispatch_notifications(payload: dict[str, Any]) -> list[str]:
    """Fire one notification per new interesting state; returns the keys sent."""
    global _notify_primed
    current_ms = int(payload.get("generatedAt") or now_ms())
    agents = [
        (platform, agent)
        for platform in ("claude", "codex")
        for agent in payload.get(platform, [])
    ]
    interesting = [
        (platform, agent)
        for platform, agent in agents
        if agent.get("status") in NOTIFY_STATUSES
    ]

    for key in [
        key for key, stamp in _notified.items() if current_ms - stamp > 6 * 60 * 60 * 1000
    ]:
        _notified.pop(key, None)

    if not _notify_primed:
        # First sample only records what is already on screen, it does not shout.
        _notify_primed = True
        for platform, agent in interesting:
            _notified[notify_key(platform, agent)] = current_ms
        return []

    sent: list[str] = []
    for platform, agent in interesting:
        key = notify_key(platform, agent)
        if key in _notified:
            continue
        agent_prefix = f"{platform}:{agent['id']}:"
        recent = [
            stamp
            for existing, stamp in _notified.items()
            if existing.startswith(agent_prefix)
            and current_ms - stamp < NOTIFY_THROTTLE_MS
        ]
        _notified[key] = current_ms
        if recent:
            continue
        _notify_history[:] = [
            stamp for stamp in _notify_history if current_ms - stamp < 60_000
        ]
        if len(_notify_history) >= NOTIFY_MAX_PER_MINUTE:
            continue
        _notify_history.append(current_ms)
        sent.append(key)
        if NOTIFY_ENABLED:
            send_mac_notification(
                "Agent Signals",
                str(agent.get("name") or ""),
                f"{STATUS_LABELS.get(agent['status'], agent['status'])} · {agent.get('cwdLabel') or ''}",
            )
    return sent


def register_locked(ids: set[str]) -> bool:
    """Remember what any client pinned so the sampler keeps loading it."""
    current_ms = now_ms()
    changed = False
    with _snapshot_ready:
        for stale in [
            key
            for key, stamp in _locked_ids.items()
            if current_ms - stamp > LOCKED_ID_TTL_MS
        ]:
            _locked_ids.pop(stale, None)
            changed = True
        for agent_id in ids:
            if agent_id not in _locked_ids:
                changed = True
            _locked_ids[agent_id] = current_ms
    return changed


def refresh_snapshot() -> tuple[dict[str, Any], str]:
    global _latest_snapshot, _latest_revision
    with _snapshot_ready:
        locked = set(_locked_ids)
    payload = snapshot(locked)
    revision = snapshot_revision(payload)
    with _snapshot_ready:
        _latest_snapshot = payload
        if revision != _latest_revision:
            _latest_revision = revision
            _snapshot_ready.notify_all()
    dispatch_notifications(payload)
    return payload, revision


def current_snapshot() -> tuple[dict[str, Any], str]:
    with _snapshot_ready:
        if _latest_snapshot is not None:
            return _latest_snapshot, _latest_revision
    return refresh_snapshot()


def wait_for_revision(previous: str, timeout_s: float) -> tuple[dict[str, Any], str]:
    global _waiters
    deadline = time.monotonic() + timeout_s
    with _snapshot_ready:
        if _latest_snapshot is None or _waiters >= MAX_WAITERS:
            payload, revision = _latest_snapshot, _latest_revision
            if payload is not None:
                return payload, revision
        else:
            _waiters += 1
            try:
                while _latest_revision == previous:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _snapshot_ready.wait(remaining)
            finally:
                _waiters -= 1
            if _latest_snapshot is not None:
                return _latest_snapshot, _latest_revision
    return refresh_snapshot()


def sampler_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            refresh_snapshot()
        except Exception:  # noqa: BLE001 - the panel must outlive one bad sample
            pass
        stop.wait(SAMPLE_INTERVAL_MS / 1000)


def terminal_tty(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "tty=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    tty = result.stdout.strip()
    if not tty or tty == "??":
        raise RuntimeError("找不到这个 Claude 会话对应的 Terminal 标签页")
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def open_claude(agent: dict[str, Any]) -> None:
    tty = terminal_tty(int(agent["pid"]))
    script = """
on run argv
  set targetTTY to item 1 of argv
  set matchedTab to false
  tell application "Terminal"
    repeat with terminalWindow in windows
      repeat with terminalTab in tabs of terminalWindow
        if tty of terminalTab is targetTTY then
          set selected tab of terminalWindow to terminalTab
          set miniaturized of terminalWindow to false
          set visible of terminalWindow to true
          set index of terminalWindow to 1
          set matchedTab to true
          exit repeat
        end if
      end repeat
      if matchedTab then exit repeat
    end repeat
    if not matchedTab then error "Terminal tab not found"
    activate
  end tell

  delay 0.35
  tell application "Terminal"
    set targetVisible to visible of window 1
    set targetMiniaturized to miniaturized of window 1
    set isFrontmost to frontmost
  end tell
  if isFrontmost and targetVisible and not targetMiniaturized then return "ok"

  do shell script "/usr/bin/open -a Terminal"
  delay 0.35
  tell application "Terminal"
    set targetVisible to visible of window 1
    set targetMiniaturized to miniaturized of window 1
    set isFrontmost to frontmost
  end tell
  if isFrontmost and targetVisible and not targetMiniaturized then return "ok"

  error "Terminal tab selected, but its window is not visible in the foreground"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, "--", tty],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "无法打开 Terminal 标签页")
    if result.stdout.strip() != "ok":
        raise RuntimeError("已找到 Terminal 标签页，但未能切换到前台")


def open_codex(agent: dict[str, Any]) -> None:
    result = subprocess.run(
        ["open", f"codex://threads/{agent['id']}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "无法打开 Codex 任务")


def find_agent(platform: str, agent_id: str) -> dict[str, Any] | None:
    locked = {agent_id} if platform == "codex" else None
    agents = snapshot(locked).get(platform, [])
    return next((item for item in agents if item["id"] == agent_id), None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/icon-180.png": ("icon-180.png", "image/png"),
        "/manifest.webmanifest": (
            "manifest.webmanifest",
            "application/manifest+json; charset=utf-8",
        ),
    }

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        etag: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if etag:
            self.send_header("ETag", f'"{etag}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(
        self, payload: dict[str, Any], status: int = 200, etag: str = ""
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status, etag)

    def send_not_modified(self, etag: str) -> None:
        self.send_response(304)
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", f'"{etag}"')
        self.end_headers()

    def serve_agents(self, query: dict[str, list[str]]) -> None:
        locked = {agent_id for agent_id in query.get("locked", []) if agent_id}
        previous = self.headers.get("If-None-Match", "").strip('"')
        try:
            wait = max(0.0, min(LONG_POLL_MAX_S, float(query.get("wait", ["0"])[0])))
        except ValueError:
            wait = 0.0

        if register_locked(locked):
            payload, revision = refresh_snapshot()
        elif wait > 0 and previous:
            payload, revision = wait_for_revision(previous, wait)
        else:
            payload, revision = current_snapshot()

        if previous and previous == revision:
            self.send_not_modified(revision)
            return
        self.send_json(payload, etag=revision)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/agents":
            self.serve_agents(parse_qs(parsed.query))
            return
        if path == "/health":
            self.send_json({"ok": True})
            return
        if path == "/api/history":
            query = parse_qs(parsed.query)
            try:
                days = int(query.get("days", ["7"])[0])
            except ValueError:
                days = 7
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self.send_json(history_summary_payload(days, limit))
            return
        if path == "/api/history/session":
            query = parse_qs(parsed.query)
            key = str(query.get("key", [""])[0])
            try:
                turns = int(query.get("turns", ["200"])[0])
            except ValueError:
                turns = 200
            payload = history_session_payload(key, turns)
            self.send_json(payload, 404 if payload.get("error") else 200)
            return
        if path in self.static_files:
            filename, content_type = self.static_files[path]
            try:
                body = (STATIC_DIR / filename).read_bytes()
            except OSError:
                self.send_json({"error": "静态文件缺失"}, 500)
                return
            self.send_bytes(body, content_type)
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/open":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length))
            platform = str(payload.get("platform") or "")
            agent_id = str(payload.get("id") or "")
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "请求格式错误"}, 400)
            return

        if platform not in {"claude", "codex"}:
            self.send_json({"error": "未知平台"}, 400)
            return
        agent = find_agent(platform, agent_id)
        if not agent or not agent.get("openable"):
            self.send_json({"error": "会话已经离线或不可打开"}, 404)
            return
        try:
            open_claude(agent) if platform == "claude" else open_codex(agent)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.send_json({"error": str(error)}, 500)
            return
        acknowledged = acknowledge_agent(agent)
        if acknowledged:
            refresh_snapshot()
        self.send_json({"ok": True, "acknowledged": acknowledged})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 状态呼吸灯")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8812)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    stop = threading.Event()
    sampler = threading.Thread(target=sampler_loop, args=(stop,), daemon=True)
    sampler.start()
    if HISTORY_ENABLED:
        history = threading.Thread(target=history_loop, args=(stop,), daemon=True)
        history.start()
    print(f"Agent 状态呼吸灯已启动: http://127.0.0.1:{args.port}", flush=True)
    print("iPad 请访问 Mac 的局域网 IP，并使用相同端口。按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()

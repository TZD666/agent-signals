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

VISIBLE_WINDOW_MS = 48 * 60 * 60 * 1000
RECENT_ERROR_MS = 5 * 60 * 1000
MAX_CODEX_THREADS = 30

STALL_AFTER_MS = env_int("AGENT_SIGNALS_STALL_AFTER_MS", 180_000)
CPU_EPSILON = env_float("AGENT_SIGNALS_CPU_EPSILON", 0.05)
SAMPLE_INTERVAL_MS = env_int("AGENT_SIGNALS_SAMPLE_INTERVAL_MS", 5_000)
LONG_POLL_MAX_S = env_float("AGENT_SIGNALS_LONG_POLL_MAX_S", 8.0)
MAX_WAITERS = env_int("AGENT_SIGNALS_MAX_WAITERS", 8)
NOTIFY_THROTTLE_MS = env_int("AGENT_SIGNALS_NOTIFY_THROTTLE_MS", 60_000)
NOTIFY_MAX_PER_MINUTE = env_int("AGENT_SIGNALS_NOTIFY_MAX_PER_MINUTE", 10)
NOTIFY_ENABLED = os.environ.get("AGENT_SIGNALS_NOTIFY", "1") != "0"
LOCKED_ID_TTL_MS = 10 * 60 * 1000

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
_rollout_cache: dict[str, tuple[tuple[float, int], tuple[str, int]]] = {}
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


def transcript_mtime(session_id: str) -> float:
    """Last write to the session transcript, our Claude-side activity signal."""
    cached = _transcript_paths.get(session_id)
    if cached is not None:
        try:
            return cached.stat().st_mtime
        except OSError:
            _transcript_paths.pop(session_id, None)
    for path in CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        _transcript_paths[session_id] = path
        return mtime
    return 0.0


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


def rollout_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def codex_state(rollout_path: str) -> tuple[str, int]:
    path = Path(rollout_path)
    signature = rollout_signature(path)
    if signature is not None:
        cached = _rollout_cache.get(rollout_path)
        if cached is not None and cached[0] == signature:
            return cached[1]

    state = "idle"
    state_at = 0
    status_events = {
        "task_started": "thinking",
        "agent_reasoning": "thinking",
        "agent_message": "thinking",
        "task_complete": "completed",
        "turn_aborted": "error",
        "approval_request": "needs_input",
        "exec_approval_request": "needs_input",
        "apply_patch_approval_request": "needs_input",
        "elicitation_request": "needs_input",
    }

    for event in read_tail_json(path):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "")
        if event_type in status_events:
            state = status_events[event_type]
            state_at = parse_iso_ms(event.get("timestamp"))

    if signature is not None:
        if len(_rollout_cache) > 200:
            _rollout_cache.clear()
        _rollout_cache[rollout_path] = (signature, (state, state_at))
    return state, state_at


def codex_status(rollout_path: str, current_ms: int | None = None) -> str:
    del current_ms
    return codex_state(rollout_path)[0]


def satellite_name(source: str) -> str:
    try:
        parsed = json.loads(source)
        subagent = parsed.get("subagent", {})
        if isinstance(subagent, dict):
            return str(next(iter(subagent.values()), "子代理"))
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
          WHEN source = 'vscode' THEN 'user'
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
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
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
        status, status_at = codex_state(rollout_path)
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
        agent = {
            "id": str(row["id"]),
            "platform": "codex",
            "name": label,
            "status": status,
            "detail": "Codex",
            "cwd": cwd,
            "cwdLabel": cwd_label(cwd),
            "updatedAt": normalize_ms(row["activity_ms"]),
            "quietSince": quiet_since,
            "completionId": (
                status_at or normalize_ms(row["activity_ms"])
                if status == "completed"
                else 0
            ),
            "openable": True,
            "satellites": [],
        }
        apply_completion_acknowledgement("codex", agent)
        if thread_source == "user" and source == "vscode":
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

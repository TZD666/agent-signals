import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "agent_status_server", Path(__file__).parents[1] / "server.py"
)
server = importlib.util.module_from_spec(SPEC)
# dataclasses 解析注解时要回查 sys.modules[__module__]，先登记再执行。
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


def make_threads_db(path, rows, columns=None):
    columns = columns or (
        "id TEXT, title TEXT, agent_nickname TEXT, name TEXT, "
        "cwd TEXT, source TEXT, thread_source TEXT, "
        "rollout_path TEXT, recency_at_ms INTEGER, "
        "updated_at_ms INTEGER, archived INTEGER"
    )
    connection = sqlite3.connect(path)
    connection.executescript(f"CREATE TABLE threads ({columns});")
    if rows:
        placeholders = ",".join("?" for _ in rows[0])
        connection.executemany(f"INSERT INTO threads VALUES ({placeholders})", rows)
    connection.commit()
    connection.close()


def write_rollout(path, event_type, timestamp="2026-07-30T08:00:00Z"):
    path.write_text(
        json.dumps(
            {"timestamp": timestamp, "type": "event_msg", "payload": {"type": event_type}}
        )
        + "\n",
        encoding="utf-8",
    )


class StatusMappingTests(unittest.TestCase):
    def setUp(self):
        server._claude_transitions.clear()

    def test_claude_busy_then_idle_gets_completed_glow(self):
        current = 1_000_000
        self.assertEqual(
            server.claude_status("session", "busy", True, current, current),
            "thinking",
        )
        self.assertEqual(
            server.claude_status("session", "idle", True, current, current + 1),
            "completed",
        )

    def test_claude_waiting_needs_input(self):
        self.assertEqual(
            server.claude_status("session", "waiting", True, 1, 1),
            "needs_input",
        )

    def test_recent_dead_claude_session_is_error(self):
        self.assertEqual(
            server.claude_status("session", "idle", False, 900_000, 1_000_000),
            "error",
        )

    def test_codex_completed_event_stays_completed_until_ui_acknowledges_it(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout(rollout, "task_complete", "2026-01-01T00:00:00Z")
            self.assertEqual(
                server.codex_status(str(rollout), current_ms=2_000_000_000_000),
                "completed",
            )

    def test_codex_reasoning_event_is_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout(rollout, "agent_reasoning")
            self.assertEqual(
                server.codex_status(str(rollout), current_ms=1_785_400_000_000),
                "thinking",
            )

    def test_codex_mid_turn_response_items_are_thinking(self):
        event_types = (
            "reasoning",
            "custom_tool_call",
            "custom_tool_call_output",
            "function_call",
            "function_call_output",
            "token_count",
        )
        for event_type in event_types:
            with tempfile.TemporaryDirectory() as directory:
                rollout = Path(directory) / "rollout.jsonl"
                write_rollout(rollout, event_type)
                self.assertEqual(
                    server.codex_status(str(rollout)), "thinking", event_type
                )

    def test_status_marker_survives_huge_mid_turn_events(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            filler = json.dumps(
                {
                    "timestamp": "2026-08-17T02:00:00Z",
                    "type": "response_item",
                    "payload": {"type": "unknown_future_type", "data": "x" * 60_000},
                }
            )
            marker = json.dumps(
                {
                    "timestamp": "2026-08-17T01:47:05Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            rollout.write_text(
                "\n".join([marker, *[filler] * 20]) + "\n", encoding="utf-8"
            )
            self.assertEqual(server.codex_status(str(rollout)), "thinking")

    def test_completion_acknowledgement_changes_completed_agent_to_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            agent = {
                "id": "thread",
                "platform": "codex",
                "status": "completed",
                "completionId": 123,
                "satellites": [],
            }
            original = dict(server._acknowledged_completions)
            try:
                server._acknowledged_completions.clear()
                with patch.object(server, "STATE_PATH", state_path):
                    self.assertTrue(server.acknowledge_agent(agent))
                server.apply_completion_acknowledgement("codex", agent)
                self.assertEqual(agent["status"], "idle")
                self.assertEqual(agent["completionId"], 0)
            finally:
                server._acknowledged_completions.clear()
                server._acknowledged_completions.update(original)

    def test_claude_bg_spare_is_not_an_agent(self):
        commands = {123: "/Users/edy/.local/share/claude/versions/2.1 --bg-spare socket"}
        self.assertFalse(server.process_alive(123, commands))

    def test_claude_interactive_process_is_an_agent(self):
        commands = {123: "/Users/edy/.local/share/claude/versions/2.1 --title panel"}
        self.assertTrue(server.process_alive(123, commands))


def write_rollout_events(path, events):
    path.write_text(
        "\n".join(
            json.dumps({"timestamp": ts, "type": "event_msg", "payload": payload})
            for ts, payload in events
        )
        + "\n",
        encoding="utf-8",
    )


def token_count_payload(input_tokens, window=258_400):
    return {
        "type": "token_count",
        "info": {
            "last_token_usage": {"input_tokens": input_tokens},
            "model_context_window": window,
        },
    }


def write_transcript(path, lines):
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def assistant_line(timestamp, request_id, usage):
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "message": {"usage": usage},
    }


class LoadIndicatorTests(unittest.TestCase):
    def setUp(self):
        server._rollout_cache.clear()
        server._claude_load_cache.clear()
        server._transcript_paths.clear()

    def codex_load(self, rollout):
        return server.codex_state(str(rollout))[2]

    def test_codex_load_captures_last_token_count(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout_events(
                rollout,
                [
                    ("2026-08-17T02:00:00Z", token_count_payload(50_000)),
                    ("2026-08-17T02:05:00Z", token_count_payload(133_848)),
                ],
            )
            load = self.codex_load(rollout)
        self.assertEqual(load["contextTokens"], 134_000)
        self.assertEqual(load["contextWindow"], 258_400)
        self.assertEqual(load["contextPct"], 52)
        self.assertEqual(load["stepGapMs"], 300_000)

    def test_codex_load_without_token_count_is_null_context(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout_events(
                rollout,
                [
                    ("2026-08-17T02:00:00Z", {"type": "agent_reasoning"}),
                    ("2026-08-17T02:01:00Z", {"type": "agent_reasoning"}),
                ],
            )
            load = self.codex_load(rollout)
        self.assertIsNone(load["contextTokens"])
        self.assertIsNone(load["contextPct"])
        self.assertEqual(load["stepGapMs"], 60_000)

    def test_codex_token_count_with_null_info_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout_events(
                rollout,
                [
                    ("2026-08-17T02:00:00Z", token_count_payload(50_000)),
                    ("2026-08-17T02:05:00Z", {"type": "token_count", "info": None}),
                ],
            )
            load = self.codex_load(rollout)
        self.assertEqual(load["contextTokens"], 50_000)

    def test_empty_rollout_yields_all_null_load(self):
        self.assertEqual(
            server.codex_state("/nonexistent/rollout.jsonl")[2], server.empty_load()
        )

    def test_step_gap_averages_recent_gaps(self):
        base = 1_000_000
        self.assertEqual(
            server.step_gap_ms([base, base + 60_000, base + 120_000]), 60_000
        )

    def test_step_gap_requires_two_usable_events(self):
        self.assertIsNone(server.step_gap_ms([]))
        self.assertIsNone(server.step_gap_ms([1_000_000]))

    def test_step_gap_drops_pauses_over_outlier(self):
        base = 1_000_000
        pause = server.STEP_GAP_OUTLIER_MS + 60_000
        self.assertEqual(
            server.step_gap_ms([base, base + 60_000, base + 60_000 + pause]), 60_000
        )

    def test_step_gap_quantizes_to_buckets(self):
        self.assertEqual(server.step_gap_ms([0, 100_000]), 105_000)
        self.assertEqual(server.step_gap_ms([0, 400_000]), 420_000)

    def test_tokens_beyond_window_hide_percentage_not_fake_100(self):
        load = server.build_load(546_000, 200_000, [])
        self.assertEqual(load["contextTokens"], 546_000)
        self.assertIsNone(load["contextWindow"])
        self.assertIsNone(load["contextPct"])

    def test_load_contains_only_ints_or_none(self):
        load = server.build_load(133_848, 258_400, [1_000, 61_000])
        for value in json.loads(json.dumps(load)).values():
            self.assertTrue(value is None or isinstance(value, int), load)

    def claude_fixture(self, directory, lines):
        root = Path(directory)
        (root / "proj").mkdir()
        write_transcript(root / "proj" / "session.jsonl", lines)
        return patch.object(server, "CLAUDE_PROJECTS_DIR", root)

    def test_claude_load_uses_newest_assistant_usage(self):
        lines = [
            assistant_line(
                "2026-08-17T02:00:00Z",
                "req_1",
                {"input_tokens": 2, "cache_read_input_tokens": 100_000,
                 "cache_creation_input_tokens": 5_000},
            ),
            assistant_line(
                "2026-08-17T02:01:00Z",
                "req_2",
                {"input_tokens": 3, "cache_read_input_tokens": 169_743,
                 "cache_creation_input_tokens": 4_406},
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.claude_fixture(directory, lines), patch.object(
                server, "CLAUDE_CONTEXT_WINDOW", 200_000
            ):
                load = server.claude_load("session")
        self.assertEqual(load["contextTokens"], 174_000)
        self.assertEqual(load["contextWindow"], 200_000)
        self.assertEqual(load["contextPct"], 87)
        self.assertEqual(load["stepGapMs"], 60_000)

    def test_claude_streamed_lines_share_one_step(self):
        usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        lines = [
            assistant_line("2026-08-17T02:00:00Z", "req_1", usage),
            assistant_line("2026-08-17T02:00:01Z", "req_1", usage),
            assistant_line("2026-08-17T02:00:02Z", "req_1", usage),
            assistant_line("2026-08-17T02:01:02Z", "req_2", usage),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.claude_fixture(directory, lines):
                load = server.claude_load("session")
        self.assertEqual(load["stepGapMs"], 60_000)

    def test_claude_missing_transcript_yields_null_load(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "CLAUDE_PROJECTS_DIR", Path(directory)):
                self.assertEqual(server.claude_load("ghost"), server.empty_load())

    def test_unchanged_transcript_is_parsed_once(self):
        lines = [assistant_line("2026-08-17T02:00:00Z", "req_1", {"input_tokens": 10})]
        with tempfile.TemporaryDirectory() as directory:
            with self.claude_fixture(directory, lines):
                reader = Mock(return_value=[])
                with patch.object(server, "read_tail_json", reader):
                    server.claude_load("session")
                    server.claude_load("session")
        self.assertEqual(reader.call_count, 1)

    def test_unchanged_rollout_returns_cached_load(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout_events(
                rollout, [("2026-08-17T02:00:00Z", token_count_payload(50_000))]
            )
            first = server.codex_state(str(rollout))[2]
            second = server.codex_state(str(rollout))[2]
        self.assertIs(first, second)

    def test_claude_interactive_agent_carries_load_and_bg_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            current = 2_000_000_000_000
            for pid, kind in ((123, "interactive"), (124, "bg")):
                (sessions / f"{pid}.json").write_text(
                    json.dumps(
                        {"pid": pid, "sessionId": f"s{pid}", "cwd": "/tmp/project",
                         "kind": kind, "status": "busy", "updatedAt": current}
                    ),
                    encoding="utf-8",
                )
            commands = {
                123: "claude --title one",
                124: "claude --title two",
            }
            with patch.object(server, "CLAUDE_SESSIONS_DIR", sessions), patch.object(
                server, "CLAUDE_PROJECTS_DIR", root
            ), patch.object(server, "command_lines", return_value=commands):
                agents, health = server.load_claude_sessions(
                    current, {"children": {}, "cpu": {}}
                )
        self.assertEqual(health["state"], "live")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["load"], server.empty_load())
        self.assertEqual(len(agents[0]["satellites"]), 1)
        self.assertNotIn("load", agents[0]["satellites"][0])


class StallDetectionTests(unittest.TestCase):
    def setUp(self):
        server._activity.clear()

    def test_thinking_without_any_activity_becomes_stalled(self):
        start = 1_000_000
        self.assertEqual(server.note_activity("claude:s", 10.0, 100.0, start), start)
        later = start + server.STALL_AFTER_MS + 1
        quiet_since = server.note_activity("claude:s", 10.0, 100.0, later)
        self.assertEqual(quiet_since, start)
        self.assertEqual(
            server.apply_stall("thinking", later - start, later - quiet_since),
            "stalled",
        )

    def test_long_task_burning_cpu_is_not_stalled(self):
        start = 1_000_000
        server.note_activity("claude:s", 10.0, 100.0, start)
        later = start + server.STALL_AFTER_MS + 1
        quiet_since = server.note_activity("claude:s", 15.0, 100.0, later)
        self.assertEqual(quiet_since, later)
        self.assertEqual(
            server.apply_stall("thinking", later - start, later - quiet_since),
            "thinking",
        )

    def test_growing_transcript_counts_as_activity(self):
        start = 1_000_000
        server.note_activity("claude:s", 10.0, 100.0, start)
        later = start + server.STALL_AFTER_MS + 1
        quiet_since = server.note_activity("claude:s", 10.0, 140.0, later)
        self.assertEqual(quiet_since, later)

    def test_only_thinking_can_stall(self):
        long_quiet = server.STALL_AFTER_MS * 10
        for status in ("idle", "completed", "needs_input", "error"):
            self.assertEqual(
                server.apply_stall(status, long_quiet, long_quiet), status
            )


class LabelTests(unittest.TestCase):
    def test_json_fragment_is_stripped_from_codex_title(self):
        self.assertEqual(
            server.clean_label('\\n转换成png格式\\n"}]},{"type"'), "转换成png格式"
        )

    def test_plain_title_survives(self):
        self.assertEqual(
            server.clean_label("接管Claude服务器故障处理"), "接管Claude服务器故障处理"
        )

    def test_empty_title_uses_fallback(self):
        self.assertEqual(server.clean_label('  "}]}  ', fallback="Codex"), "Codex")

    def test_long_title_is_truncated(self):
        self.assertLessEqual(len(server.clean_label("很长的标题" * 20)), 40)

    def test_thread_spawn_satellite_uses_its_nickname(self):
        source = json.dumps(
            {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent",
                        "depth": 1,
                        "agent_path": "/root/slide2_copy",
                        "agent_nickname": "Huygens",
                        "agent_role": None,
                    }
                }
            }
        )
        self.assertEqual(server.satellite_name(source), "Huygens")

    def test_thread_spawn_satellite_falls_back_to_agent_path(self):
        source = json.dumps(
            {"subagent": {"thread_spawn": {"agent_path": "/root/copy_audit"}}}
        )
        self.assertEqual(server.satellite_name(source), "copy_audit")

    def test_guardian_satellite_keeps_plain_name(self):
        self.assertEqual(
            server.satellite_name('{"subagent":{"other":"guardian"}}'), "guardian"
        )


class TerminalOpenTests(unittest.TestCase):
    def test_open_claude_waits_for_verified_frontmost_terminal(self):
        result = Mock(returncode=0, stdout="ok\n", stderr="")
        with patch.object(server, "terminal_tty", return_value="/dev/ttys003"), patch.object(
            server.subprocess, "run", return_value=result
        ) as run:
            server.open_claude({"pid": 123})

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertIn("set miniaturized of terminalWindow to false", command[2])
        self.assertIn("set visible of terminalWindow to true", command[2])
        self.assertIn("delay 0.35", command[2])
        self.assertIn("/usr/bin/open -a Terminal", command[2])
        self.assertIn(
            "if isFrontmost and targetVisible and not targetMiniaturized then return \"ok\"",
            command[2],
        )

    def test_open_claude_rejects_silent_activation_drop(self):
        result = Mock(returncode=0, stdout="", stderr="")
        with patch.object(server, "terminal_tty", return_value="/dev/ttys003"), patch.object(
            server.subprocess, "run", return_value=result
        ):
            with self.assertRaisesRegex(RuntimeError, "未能切换到前台"):
                server.open_claude({"pid": 123})

    def test_open_claude_sends_desktop_sessions_to_the_app(self):
        result = Mock(returncode=0, stdout="", stderr="")
        with patch.object(server, "terminal_tty") as tty, patch.object(
            server.subprocess, "run", return_value=result
        ) as run:
            server.open_claude({"pid": 123, "openVia": "app:Claude"})
        # 桌面 App 会话没有 Terminal 标签页可切，绝不能去查 tty。
        tty.assert_not_called()
        self.assertEqual(run.call_args.args[0], ["open", "-a", "Claude"])

    def test_open_claude_with_tty_open_via_still_uses_the_terminal(self):
        result = Mock(returncode=0, stdout="ok\n", stderr="")
        with patch.object(
            server, "terminal_tty", return_value="/dev/ttys003"
        ), patch.object(server.subprocess, "run", return_value=result) as run:
            server.open_claude({"pid": 123, "openVia": "tty"})
        self.assertEqual(run.call_args.args[0][0], "osascript")


class SourceHealthTests(unittest.TestCase):
    def test_missing_database_reports_unavailable_not_empty(self):
        with patch.object(server, "CODEX_DB_PATH", Path("/nonexistent/state_5.sqlite")):
            agents, health = server.load_codex_threads(2_000_000_000_000)
        self.assertEqual(agents, [])
        self.assertEqual(health["state"], "unavailable")
        self.assertIn("不存在", health["detail"])

    def test_unexpected_schema_reports_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state_9.sqlite"
            make_threads_db(database, [], columns="id TEXT, title TEXT")
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(2_000_000_000_000)
        self.assertEqual(agents, [])
        self.assertEqual(health["state"], "schema_mismatch")

    def test_highest_state_version_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in (5, 6, 11):
                (root / f"state_{version}.sqlite").write_bytes(b"")
            (root / "goals_1.sqlite").write_bytes(b"")
            with patch.object(server, "CODEX_DB_PATH", None), patch.object(
                server, "CODEX_DIR", root
            ):
                database, detail = server.resolve_codex_db()
        self.assertEqual(database.name, "state_11.sqlite")
        self.assertEqual(detail, "")

    def test_newer_duplicate_database_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "sqlite"
            nested.mkdir()
            old = nested / "state_5.sqlite"
            current = root / "state_5.sqlite"
            old.write_bytes(b"old")
            current.write_bytes(b"current")
            old.touch()
            current.touch()
            with patch.object(server, "CODEX_DB_PATH", None), patch.object(
                server, "CODEX_DIR", root
            ), patch.object(
                server.Path, "stat", autospec=True, side_effect=Path.stat
            ):
                database, detail = server.resolve_codex_db()
        self.assertEqual(database, current)
        self.assertEqual(detail, "")

    def test_missing_claude_directory_reports_unavailable(self):
        with patch.object(server, "CLAUDE_SESSIONS_DIR", Path("/nonexistent/sessions")):
            agents, health = server.load_claude_sessions(2_000_000_000_000, {})
        self.assertEqual(agents, [])
        self.assertEqual(health["state"], "unavailable")


class SourceCollectionTests(unittest.TestCase):
    def test_codex_foreground_and_subagent_are_grouped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            activity = 2_000_000_000_000
            make_threads_db(
                database,
                [
                    (
                        "main", "主任务", None, None, "/tmp/project",
                        "vscode", "user", str(rollout), activity, activity, 0,
                    ),
                    (
                        "child", "审核任务", None, None, "/tmp/project",
                        '{"subagent":{"other":"guardian"}}', "subagent",
                        str(rollout), activity, activity, 0,
                    ),
                ],
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(activity)

        self.assertEqual(health["state"], "live")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "主任务")
        self.assertEqual(agents[0]["satellites"][0]["name"], "guardian")
        self.assertIn("load", agents[0])
        self.assertNotIn("load", agents[0]["satellites"][0])

    def test_completion_from_before_server_start_is_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            write_rollout(rollout, "task_complete", "2026-01-01T00:00:00Z")
            activity = 2_000_000_000_000
            make_threads_db(
                database,
                [
                    (
                        "done", "旧完成", None, None, "/tmp/project",
                        "vscode", "user", str(rollout), activity, activity, 0,
                    ),
                ],
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(activity)

        self.assertEqual(health["state"], "live")
        self.assertEqual(agents, [])

    def test_codex_exec_cli_thread_is_foreground(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            activity = 2_000_000_000_000
            make_threads_db(
                database,
                [
                    (
                        "cli", "CLI 任务", None, None, "/tmp/project",
                        "exec", None, str(rollout), activity, activity, 0,
                    ),
                ],
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(activity)

        self.assertEqual(health["state"], "live")
        self.assertEqual([agent["name"] for agent in agents], ["CLI 任务"])
        self.assertEqual(agents[0]["detail"], "Codex CLI")

    def test_locked_codex_thread_is_loaded_after_recent_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            rollout.write_text("", encoding="utf-8")
            current = 2_000_000_000_000
            old_activity = current - server.VISIBLE_WINDOW_MS - 1
            make_threads_db(
                database,
                [
                    (
                        "locked", "锁定任务", None, None, "/tmp/project",
                        "vscode", "user", str(rollout), old_activity, old_activity, 0,
                    )
                ],
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                self.assertEqual(server.load_codex_threads(current)[0], [])
                agents, _ = server.load_codex_threads(current, {"locked"})

        self.assertEqual([agent["id"] for agent in agents], ["locked"])

    def test_legacy_schema_without_optional_columns_stays_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            activity = 2_000_000_000_000
            columns = (
                "id TEXT, title TEXT, agent_nickname TEXT, "
                "cwd TEXT, source TEXT, thread_source TEXT, "
                "rollout_path TEXT, updated_at_ms INTEGER, archived INTEGER"
            )
            make_threads_db(
                database,
                [
                    (
                        "main",
                        "兼容任务",
                        None,
                        "/tmp/project",
                        "vscode",
                        None,
                        str(rollout),
                        activity,
                        0,
                    )
                ],
                columns=columns,
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(activity)

        self.assertEqual(health["state"], "live")
        self.assertIn("缺少可选字段", health["detail"])
        self.assertEqual([agent["name"] for agent in agents], ["兼容任务"])

    def test_seconds_activity_column_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            rollout = root / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            current = 2_000_000_000_000
            columns = (
                "id TEXT, title TEXT, cwd TEXT, source TEXT, "
                "rollout_path TEXT, updated_at INTEGER"
            )
            make_threads_db(
                database,
                [
                    (
                        "main",
                        "秒级时间",
                        "/tmp/project",
                        "vscode",
                        str(rollout),
                        current // 1000,
                    )
                ],
                columns=columns,
            )
            with patch.object(server, "CODEX_DB_PATH", database):
                agents, health = server.load_codex_threads(current)

        self.assertEqual(health["state"], "live")
        self.assertEqual(agents[0]["updatedAt"], current)


class RolloutCacheTests(unittest.TestCase):
    def test_unchanged_rollout_is_parsed_once(self):
        server._rollout_cache.clear()
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            reader = Mock(return_value=[])
            with patch.object(server, "read_tail_json", reader):
                server.codex_state(str(rollout))
                server.codex_state(str(rollout))
                server.codex_state(str(rollout))
            self.assertEqual(reader.call_count, 1)

    def test_changed_rollout_is_parsed_again(self):
        server._rollout_cache.clear()
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            write_rollout(rollout, "task_started")
            reader = Mock(return_value=[])
            with patch.object(server, "read_tail_json", reader):
                server.codex_state(str(rollout))
                write_rollout(rollout, "task_complete", "2026-07-30T09:00:00Z")
                server.codex_state(str(rollout))
            self.assertEqual(reader.call_count, 2)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        server._notified.clear()
        server._notify_history.clear()
        server._notify_primed = True

    def payload(self, status="needs_input", agent_id="a1"):
        return {
            "generatedAt": 1_000_000,
            "claude": [
                {
                    "id": agent_id,
                    "name": "edy-d6",
                    "status": status,
                    "cwdLabel": "项目",
                    "completionId": 0,
                }
            ],
            "codex": [],
        }

    def test_same_state_notifies_once(self):
        with patch.object(server, "send_mac_notification") as sender:
            first = server.dispatch_notifications(self.payload())
            second = server.dispatch_notifications(self.payload())
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(sender.call_count, 1)

    def test_first_sample_primes_without_shouting(self):
        server._notified.clear()
        server._notify_primed = False
        with patch.object(server, "send_mac_notification") as sender:
            sent = server.dispatch_notifications(self.payload())
        self.assertEqual(sent, [])
        self.assertEqual(sender.call_count, 0)

    def test_rapid_state_flapping_is_throttled(self):
        with patch.object(server, "send_mac_notification") as sender:
            server.dispatch_notifications(self.payload("needs_input"))
            server.dispatch_notifications(self.payload("stalled"))
        self.assertEqual(sender.call_count, 1)

    def test_global_ceiling_caps_a_notification_storm(self):
        with patch.object(server, "send_mac_notification") as sender:
            for index in range(server.NOTIFY_MAX_PER_MINUTE + 5):
                server.dispatch_notifications(
                    self.payload("needs_input", agent_id=f"agent-{index}")
                )
        self.assertEqual(sender.call_count, server.NOTIFY_MAX_PER_MINUTE)


PS_SWEEP = (
    "  501     1   501   12:34.56    01:02:03 /usr/bin/python3 server.py --port 8812\n"
    "  777   501     0    0:00.10       05:00 claude --bg-spare\n"
    "  888   501   501    1:00.00  2-03:04:05 codex app\n"
    "  902   501   501       0:00          17 /bin/sh\n"
    "垃圾行\n"
)
EMPTY_TABLE = {"children": {}, "cpu": {}, "commands": {}, "start_s": {}, "uid": {}}


class ProcessTableTests(unittest.TestCase):
    def setUp(self):
        # scan_processes 会写模块级 _last_table，别把它漏给后面的用例。
        self.addCleanup(setattr, server, "_last_table", server._last_table)

    def test_parse_etime_handles_every_ps_shape(self):
        self.assertEqual(server.parse_etime("17"), 17)
        self.assertEqual(server.parse_etime("05:00"), 300)
        self.assertEqual(server.parse_etime("01:02:03"), 3723)
        self.assertEqual(server.parse_etime("2-03:04:05"), 183_845)
        self.assertEqual(server.parse_etime(""), 0)
        self.assertEqual(server.parse_etime("垃圾"), 0)

    def test_scan_parses_etime_and_commands(self):
        with patch.object(server, "now_ms", return_value=1_000_000_000_000), patch.object(
            server.subprocess, "run", return_value=Mock(stdout=PS_SWEEP)
        ) as run:
            table = server.scan_processes()

        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args[0][0],
            ["ps", "-axo", "pid=,ppid=,uid=,time=,etime=,command="],
        )
        self.assertEqual(
            table["commands"],
            {
                501: "/usr/bin/python3 server.py --port 8812",
                777: "claude --bg-spare",
                888: "codex app",
                902: "/bin/sh",
            },
        )
        self.assertEqual(
            table["start_s"],
            {
                501: 999_996_277,
                777: 999_999_700,
                888: 999_816_155,
                902: 999_999_983,
            },
        )
        self.assertEqual(table["uid"], {501: 501, 777: 0, 888: 501, 902: 501})
        # 老字段一个不动：其他代码依赖 children / cpu。
        self.assertEqual(table["children"], {1: [501], 501: [777, 888, 902]})
        self.assertAlmostEqual(table["cpu"][501], 754.56)
        self.assertAlmostEqual(table["cpu"][888], 60.0)

    def test_command_lines_reads_from_table_first(self):
        table = dict(EMPTY_TABLE, commands={101: "claude --resume abc"})
        with patch.object(server, "_last_table", table), patch.object(
            server.subprocess, "run"
        ) as run:
            self.assertEqual(
                server.command_lines({101}), {101: "claude --resume abc"}
            )
        run.assert_not_called()

    def test_command_lines_falls_back_for_pids_outside_the_table(self):
        table = dict(EMPTY_TABLE, commands={101: "claude --resume abc"})
        with patch.object(server, "_last_table", table), patch.object(
            server.subprocess,
            "run",
            return_value=Mock(stdout="  202 codex --headless\n"),
        ) as run:
            commands = server.command_lines({101, 202})
        self.assertEqual(
            commands, {101: "claude --resume abc", 202: "codex --headless"}
        )
        # 只为缺失的 pid 付一次 ps 的钱。
        self.assertEqual(run.call_args[0][0][2], "202")


CLAUDE_NOW = 2_000_000_000_000
DESKTOP_COMMAND = (
    "/Users/edy/Library/Application Support/Claude/claude-code/"
    "2.0.76/claude.app/Contents/MacOS/claude --title 桌面"
)


def registry(pid, **extra):
    """一条 ~/.claude/sessions/<pid>.json 的最小形状；传 None 表示这个键不存在。"""
    data = {
        "pid": pid,
        "sessionId": f"s{pid}",
        "cwd": "/tmp/project",
        "kind": "interactive",
        "status": "busy",
        "updatedAt": CLAUDE_NOW,
    }
    for key, value in extra.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class ClaudeFamilyTests(unittest.TestCase):
    """入口标签、桌面 App 索引、headless 会话、PPID 挂靠、Task 子代理卫星。"""

    def setUp(self):
        for cache in (
            server._claude_transitions,
            server._activity,
            server._transcript_paths,
            server._claude_load_cache,
            server._subagent_cache,
            server._desktop_index_cache,
        ):
            cache.clear()

    def sample(self, root, sessions, commands=None, children=None, current=CLAUDE_NOW):
        """登记表落盘 → 跑一轮 load_claude_sessions，全部目录都在临时目录里。"""
        sessions_dir = root / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        (root / "projects").mkdir(exist_ok=True)
        for data in sessions:
            (sessions_dir / f"{data['pid']}.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
        if commands is None:
            commands = {int(data["pid"]): "claude --title x" for data in sessions}
        table = dict(EMPTY_TABLE, commands=commands, children=children or {})
        with patch.object(server, "CLAUDE_SESSIONS_DIR", sessions_dir), patch.object(
            server, "CLAUDE_PROJECTS_DIR", root / "projects"
        ), patch.object(
            server, "CLAUDE_DESKTOP_SUPPORT_DIR", root / "desktop"
        ), patch.object(server, "command_lines", return_value=commands):
            return server.load_claude_sessions(current, table)

    def desktop_entry(self, root, cli_session_id, title, last_activity=CLAUDE_NOW):
        folder = root / "desktop" / "claude-code-sessions" / "acct" / "org"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"local_{cli_session_id}.json").write_text(
            json.dumps(
                {
                    "sessionId": f"local_{cli_session_id}",
                    "cliSessionId": cli_session_id,
                    "cwd": "/tmp/project",
                    "title": title,
                    "titleSource": "generated",
                    "lastActivityAt": last_activity,
                    "isArchived": False,
                }
            ),
            encoding="utf-8",
        )

    def subagent_file(self, folder, stem, age_ms, meta):
        jsonl = folder / f"{stem}.jsonl"
        jsonl.write_text("{}\n", encoding="utf-8")
        stamp = (CLAUDE_NOW - age_ms) / 1000.0
        os.utime(jsonl, (stamp, stamp))
        (folder / f"{stem}.meta.json").write_text(
            "不是 json" if meta is None else json.dumps(meta), encoding="utf-8"
        )

    def test_entrypoint_detail_labels(self):
        sessions = [
            registry(100, entrypoint="cli"),
            registry(101, entrypoint="claude-vscode"),
            registry(102, entrypoint="mcp"),
            registry(103, entrypoint="local_agent"),
            registry(104, entrypoint="sdk-cli"),
            registry(105),  # 老登记表没有这个字段
            registry(106),  # 也没有，但命令行是桌面 App 自带的那个二进制
            registry(107, entrypoint="remote-future"),
        ]
        commands = {data["pid"]: "claude --title x" for data in sessions}
        commands[106] = DESKTOP_COMMAND
        with tempfile.TemporaryDirectory() as directory:
            agents, health = self.sample(Path(directory), sessions, commands)
        self.assertEqual(health["state"], "live")
        self.assertEqual(
            {agent["pid"]: agent["detail"] for agent in agents},
            {
                100: "Terminal",
                101: "VS Code",
                102: "MCP",
                103: "桌面 Cowork",
                104: "claude -p 后台",
                105: "Terminal",
                106: "桌面 App",
                107: "remote-future",  # 认不出的入口原样显示，不假装是终端
            },
        )
        by_pid = {agent["pid"]: agent for agent in agents}
        self.assertEqual(by_pid[100]["openVia"], "tty")
        self.assertEqual(by_pid[106]["openVia"], "app:Claude")

    def test_desktop_index_overrides_name_and_open_via(self):
        sessions = [
            registry(200, name="terminal-200", nameSource="derived"),
            registry(201, name="我起的名字"),
            registry(202, name="没进索引"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.desktop_entry(root, "s200", "重构对账脚本")
            self.desktop_entry(root, "s201", "索引里的标题")
            (
                root
                / "desktop"
                / "claude-code-sessions"
                / "acct"
                / "org"
                / "local_broken.json"
            ).write_text("{坏文件", encoding="utf-8")
            agents, _ = self.sample(root, sessions)
        by_pid = {agent["pid"]: agent for agent in agents}
        self.assertEqual(by_pid[200]["name"], "重构对账脚本")
        self.assertEqual(by_pid[200]["detail"], "桌面 App")
        self.assertEqual(by_pid[200]["openVia"], "app:Claude")
        self.assertEqual(by_pid[200]["origin"], "registry")
        # 自己起的名字不许被索引标题盖掉。
        self.assertEqual(by_pid[201]["name"], "我起的名字")
        self.assertEqual(by_pid[201]["detail"], "桌面 App")
        # 索引里没有的会话一切照旧。
        self.assertEqual(by_pid[202]["name"], "没进索引")
        self.assertEqual(by_pid[202]["openVia"], "tty")

    def test_headless_session_without_status_is_thinking_while_cpu_moves(self):
        sessions = [
            registry(
                300,
                status=None,
                updatedAt=None,
                startedAt=CLAUDE_NOW,
                entrypoint="sdk-cli",
            )
        ]
        statuses, openable = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for offset, mtime in ((0, 1_000.0), (10_000, 1_005.0), (50_000, 1_005.0)):
                with patch.object(server, "transcript_mtime", return_value=mtime):
                    agents, _ = self.sample(
                        root, sessions, current=CLAUDE_NOW + offset
                    )
                statuses.append(agents[0]["status"])
                openable.append(agents[0]["openable"])
        self.assertEqual(statuses, ["thinking", "thinking", "completed"])
        self.assertEqual(openable, [False, False, False])
        self.assertEqual(agents[0]["detail"], "claude -p 后台")
        # 没有 statusUpdatedAt/updatedAt 时退回 startedAt，而不是 0。
        self.assertEqual(agents[0]["updatedAt"], CLAUDE_NOW)

    def test_desktop_session_without_status_is_still_openable(self):
        # 桌面 App 的登记表和 `claude -p` 一样不写 status，但它点得开：
        # 状态要自己推 ≠ 打不开，这两件事必须分开。
        sessions = [
            registry(
                210,
                status=None,
                updatedAt=None,
                startedAt=CLAUDE_NOW - 60_000,
                entrypoint="claude-desktop",
                name="terminal-210",
                nameSource="derived",
                bridgeSessionId="bridge-1",
            ),
            registry(
                211,
                status=None,
                updatedAt=None,
                startedAt=CLAUDE_NOW - 60_000,
                entrypoint="sdk-cli",
            ),
        ]
        commands = {
            210: (
                "/Users/edy/Library/Application Support/Claude/claude-code/"
                "2.0.76/claude.app/Contents/MacOS/claude"
            ),
            211: "claude -p 巡检",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.desktop_entry(
                root, "s210", "5分钟空任务测试", last_activity=CLAUDE_NOW - 5_000
            )
            agents, _ = self.sample(root, sessions, commands)
        by_pid = {agent["pid"]: agent for agent in agents}
        desktop = by_pid[210]
        self.assertTrue(desktop["openable"])
        self.assertEqual(desktop["openVia"], "app:Claude")
        self.assertEqual(desktop["detail"], "桌面 App")
        self.assertEqual(desktop["name"], "5分钟空任务测试")
        # 没有 status 字段，状态照样按活动推出来。
        self.assertEqual(desktop["status"], "thinking")
        # 没有状态时间戳时，索引的 lastActivityAt 比 startedAt 更贴近真实活动。
        self.assertEqual(desktop["updatedAt"], CLAUDE_NOW - 5_000)
        # 真正的 `claude -p` 依旧点不开，时间戳只能退回 startedAt。
        self.assertFalse(by_pid[211]["openable"])
        self.assertEqual(by_pid[211]["updatedAt"], CLAUDE_NOW - 60_000)

    def test_background_session_attaches_to_ppid_ancestor(self):
        sessions = [
            registry(400, cwd="/tmp/host"),
            registry(500, cwd="/tmp/elsewhere"),
            registry(402, kind="bg", cwd="/tmp/elsewhere", name="后台构建"),
            registry(
                403,
                entrypoint="sdk-cli",
                status=None,
                updatedAt=None,
                startedAt=CLAUDE_NOW,
                cwd="/tmp/host",
                name="claude -p 巡检",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            agents, _ = self.sample(
                Path(directory), sessions, children={400: [401], 401: [402, 403]}
            )
        by_pid = {agent["pid"]: agent for agent in agents}
        self.assertEqual(sorted(by_pid), [400, 500])
        # cwd 相同的 500 是老规则会挑中的宿主；PPID 链把它挂回真正的父会话。
        self.assertEqual(by_pid[500]["satellites"], [])
        self.assertEqual(
            {
                (item["id"], item["name"], item["origin"])
                for item in by_pid[400]["satellites"]
            },
            {
                ("s402", "后台构建", "registry"),
                ("s403", "claude -p 巡检", "registry"),
            },
        )

    def test_subagent_meta_becomes_satellite_and_lingers_then_drops(self):
        sessions = [registry(600, sessionId="s600")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "projects" / "proj").mkdir(parents=True)
            write_transcript(root / "projects" / "proj" / "s600.jsonl", [{"type": "x"}])
            folder = root / "projects" / "proj" / "s600" / "subagents"
            folder.mkdir(parents=True)
            self.subagent_file(
                folder, "agent-live", 0, {"agentType": "Explore", "description": "找入口"}
            )
            self.subagent_file(
                folder, "agent-done", 120_000, {"agentType": "Plan", "description": "定方案"}
            )
            self.subagent_file(
                folder, "agent-gone", 900_000, {"agentType": "Plan", "description": "上小时的"}
            )
            self.subagent_file(folder, "agent-bad", 0, None)
            agents, _ = self.sample(root, sessions)
        satellites = {item["id"]: item for item in agents[0]["satellites"]}
        # 完成超过 SUBAGENT_LINGER_MS 的那个不再显示。
        self.assertEqual(
            sorted(satellites), ["agent-bad", "agent-done", "agent-live"]
        )
        self.assertEqual(satellites["agent-live"]["name"], "Explore · 找入口")
        self.assertEqual(satellites["agent-live"]["status"], "thinking")
        self.assertEqual(satellites["agent-live"]["completionId"], 0)
        self.assertEqual(satellites["agent-live"]["origin"], "subagent")
        self.assertEqual(satellites["agent-done"]["status"], "completed")
        self.assertEqual(
            satellites["agent-done"]["completionId"], CLAUDE_NOW - 120_000
        )
        # meta 读不出来就退回文件名，不编造名字。
        self.assertEqual(satellites["agent-bad"]["name"], "agent-bad")

    def test_satellites_still_carry_no_load(self):
        sessions = [registry(700), registry(702, kind="bg", name="后台任务")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "projects" / "proj").mkdir(parents=True)
            write_transcript(root / "projects" / "proj" / "s700.jsonl", [{"type": "x"}])
            folder = root / "projects" / "proj" / "s700" / "subagents"
            folder.mkdir(parents=True)
            self.subagent_file(
                folder, "agent-x", 0, {"agentType": "Explore", "description": "看一眼"}
            )
            agents, _ = self.sample(root, sessions, children={700: [702]})
        self.assertEqual(len(agents), 1)
        self.assertEqual(len(agents[0]["satellites"]), 2)
        self.assertEqual(
            {frozenset(item) for item in agents[0]["satellites"]},
            {frozenset({"id", "name", "status", "completionId", "origin"})},
        )

    def test_claimed_pids_cover_registry_pids_and_their_children(self):
        sessions = [registry(700), registry(800)]
        table = dict(
            EMPTY_TABLE, children={700: [701, 702], 702: [703], 900: [901]}
        )
        with tempfile.TemporaryDirectory() as directory:
            sessions_dir = Path(directory) / "sessions"
            sessions_dir.mkdir()
            for data in sessions:
                (sessions_dir / f"{data['pid']}.json").write_text(
                    json.dumps(data), encoding="utf-8"
                )
            with patch.object(server, "CLAUDE_SESSIONS_DIR", sessions_dir):
                claimed = server.claude_claimed_pids(table)
        self.assertEqual(claimed, {700, 701, 702, 703, 800})


GOLDEN_CLAUDE = [
    {
        "id": "sid-1",
        "platform": "claude",
        "name": "edy-d6",
        "status": "thinking",
        "pid": 4242,
        "cwdLabel": "项目",
        "openable": True,
        "completionId": 0,
        "satellites": [
            {"id": "sid-1/agent-a", "name": "Explore", "status": "thinking"},
            {"id": "sid-1/agent-b", "name": "Plan", "status": "completed"},
        ],
    },
    {
        "id": "sid-2",
        "platform": "claude",
        "name": "edy-d7",
        "status": "needs_input",
        "pid": 4243,
        "cwdLabel": "另一个项目",
        "openable": True,
        "completionId": 3,
        "satellites": [],
    },
]
GOLDEN_CODEX = [
    {
        "id": "thread-1",
        "platform": "codex",
        "name": "Codex",
        "status": "completed",
        "cwdLabel": "项目",
        "openable": True,
        "completionId": 1,
        "satellites": [
            {"id": "thread-2", "name": "子代理", "status": "thinking"},
        ],
    },
]
GOLDEN_CLAUDE_HEALTH = {"state": "live", "detail": ""}
GOLDEN_CODEX_HEALTH = {"state": "live", "detail": "兼容读取"}
GOLDEN_NOTIFY_HEALTH = {"state": "ok", "detail": ""}


class SnapshotPayloadTests(unittest.TestCase):
    """载荷是 iPad 前端的契约：重构可以动内部，不许动这张表。"""

    def take(self, locked=None):
        with patch.object(
            server, "scan_processes", return_value=dict(EMPTY_TABLE)
        ), patch.object(
            server,
            "load_claude_sessions",
            return_value=(GOLDEN_CLAUDE, GOLDEN_CLAUDE_HEALTH),
        ), patch.object(
            server,
            "load_codex_threads",
            return_value=(GOLDEN_CODEX, GOLDEN_CODEX_HEALTH),
        ), patch.object(
            server, "_notify_health", dict(GOLDEN_NOTIFY_HEALTH)
        ):
            return server.snapshot(locked)

    def test_snapshot_payload_is_schema1_golden(self):
        payload = self.take({"thread-1"})
        self.assertIsInstance(payload.pop("generatedAt"), int)
        self.assertEqual(
            payload,
            {
                "schemaVersion": 1,
                "sources": {
                    "claude": {"state": "live", "detail": ""},
                    "codex": {"state": "live", "detail": "兼容读取"},
                },
                "notifications": {"state": "ok", "detail": ""},
                "claude": [
                    {
                        "id": "sid-1",
                        "platform": "claude",
                        "name": "edy-d6",
                        "status": "thinking",
                        "pid": 4242,
                        "cwdLabel": "项目",
                        "openable": True,
                        "completionId": 0,
                        "satellites": [
                            {
                                "id": "sid-1/agent-a",
                                "name": "Explore",
                                "status": "thinking",
                            },
                            {
                                "id": "sid-1/agent-b",
                                "name": "Plan",
                                "status": "completed",
                            },
                        ],
                    },
                    {
                        "id": "sid-2",
                        "platform": "claude",
                        "name": "edy-d7",
                        "status": "needs_input",
                        "pid": 4243,
                        "cwdLabel": "另一个项目",
                        "openable": True,
                        "completionId": 3,
                        "satellites": [],
                    },
                ],
                "codex": [
                    {
                        "id": "thread-1",
                        "platform": "codex",
                        "name": "Codex",
                        "status": "completed",
                        "cwdLabel": "项目",
                        "openable": True,
                        "completionId": 1,
                        "satellites": [
                            {
                                "id": "thread-2",
                                "name": "子代理",
                                "status": "thinking",
                            },
                        ],
                    },
                ],
                "counts": {"claude": 2, "codex": 1, "satellites": 3},
            },
        )

    def test_revision_stable_across_identical_samples(self):
        first = self.take()
        second = self.take()
        second["generatedAt"] = first["generatedAt"] + 5_000
        self.assertEqual(
            server.snapshot_revision(first), server.snapshot_revision(second)
        )


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "schemaVersion": 1,
            "generatedAt": 1_000_000,
            "sources": {
                "claude": {"state": "live", "detail": ""},
                "codex": {"state": "live", "detail": ""},
            },
            "notifications": {"state": "ok", "detail": ""},
            "claude": [],
            "codex": [],
            "counts": {"claude": 0, "codex": 0, "satellites": 0},
        }
        self.snapshot_patch = patch.object(
            server, "snapshot", return_value=self.payload
        )
        self.notify_patch = patch.object(server, "send_mac_notification")
        self.snapshot_patch.start()
        self.notify_patch.start()
        server._latest_snapshot = None
        server._latest_revision = ""
        server._locked_ids.clear()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.snapshot_patch.stop()
        self.notify_patch.stop()

    def get(self, path, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=headers or {}
        )
        return urllib.request.urlopen(request, timeout=10)

    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_unchanged_revision_answers_304(self):
        with self.get("/api/agents") as response:
            self.assertEqual(response.status, 200)
            etag = response.headers["ETag"]
            body = json.loads(response.read())
        self.assertIsNotNone(etag)
        self.assertEqual(body["schemaVersion"], 1)

        try:
            with self.get("/api/agents", {"If-None-Match": etag}) as response:
                self.fail(f"预期 304，实际 {response.status}")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 304)
            self.assertEqual(error.read(), b"")
            error.close()

    def test_long_poll_returns_304_after_the_wait(self):
        with self.get("/api/agents") as response:
            etag = response.headers["ETag"]
        try:
            with self.get("/api/agents?wait=1", {"If-None-Match": etag}):
                self.fail("预期 304")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 304)
            error.close()

    def test_payload_carries_source_health(self):
        with self.get("/api/agents") as response:
            body = json.loads(response.read())
        self.assertEqual(body["sources"]["codex"]["state"], "live")

    def test_open_acknowledges_a_light_that_cannot_be_opened(self):
        # headless 的 `claude -p` 打不开，但它转绿之后必须有办法确认掉。
        self.payload["claude"] = [
            {
                "id": "headless-1",
                "platform": "claude",
                "name": "claude -p 巡检",
                "status": "completed",
                "completionId": 4242,
                "openable": False,
                "satellites": [],
            }
        ]
        original = dict(server._acknowledged_completions)
        with tempfile.TemporaryDirectory() as directory:
            try:
                server._acknowledged_completions.clear()
                with patch.object(
                    server, "STATE_PATH", Path(directory) / "state.json"
                ), patch.object(server, "open_claude") as opener:
                    body = self.post(
                        "/api/open", {"platform": "claude", "id": "headless-1"}
                    )
                opener.assert_not_called()
                self.assertEqual(
                    body, {"ok": True, "opened": False, "acknowledged": True}
                )
                self.assertEqual(
                    server._acknowledged_completions["claude:headless-1"], 4242
                )
            finally:
                server._acknowledged_completions.clear()
                server._acknowledged_completions.update(original)

    def test_open_unknown_agent_is_still_404(self):
        try:
            self.post("/api/open", {"platform": "claude", "id": "ghost"})
            self.fail("预期 404")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 404)
            error.close()

    def test_health_carries_version_and_platforms(self):
        with self.get("/health") as response:
            body = json.loads(response.read())
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["version"], server.APP_VERSION)
        self.assertEqual(body["schemaVersion"], server.SCHEMA_VERSION)
        self.assertEqual(body["platforms"], ["claude", "codex"])
        self.assertEqual(body["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()

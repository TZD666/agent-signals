import importlib.util
import json
import sqlite3
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


if __name__ == "__main__":
    unittest.main()

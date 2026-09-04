"""历史埋点与费用估算的测试。

约定与 test_server.py 相同：importlib 直接加载 server.py，不启动线程、
不在 import 时建任何文件。
"""

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "agent_status_server_history", Path(__file__).parents[1] / "server.py"
)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

SENTINEL = "SENTINEL_对话内容_MUST_NOT_BE_STORED"

FABLE_PRICE = {
    "input": 1e-05,
    "output": 5e-05,
    "cache_read": 1e-06,
    "cache_write": 1.25e-05,
}
GPT5_PRICE = {"input": 1.25e-06, "cached_input": 1.25e-07, "output": 1e-05}


def claude_line(timestamp, request_id, usage, model="claude-fable-5", **extra):
    line = {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "effort": extra.pop("effort", "xhigh"),
        "message": {
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": SENTINEL}],
        },
    }
    line.update(extra)
    return line


def user_line(timestamp):
    return {"type": "user", "timestamp": timestamp, "message": {"content": SENTINEL}}


def write_jsonl(path, lines):
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def codex_event(timestamp, payload):
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def token_count_event(timestamp, totals, window=258_400, last_input=None):
    return codex_event(
        timestamp,
        {
            "type": "token_count",
            "info": {
                "total_token_usage": totals,
                "last_token_usage": {"input_tokens": last_input or 0},
                "model_context_window": window,
            },
        },
    )


def claude_target(path, session_id, meta=None):
    return {
        "path": path,
        "session_key": f"claude:{session_id}",
        "platform": "claude",
        "native_id": session_id,
        "meta": meta or {},
    }


def codex_target(path, thread_id, meta=None):
    return {
        "path": path,
        "session_key": f"codex:{thread_id}",
        "platform": "codex",
        "native_id": thread_id,
        "meta": meta or {},
    }


class CostFormulaTests(unittest.TestCase):
    def test_claude_cost_prices_all_four_classes_and_1h_at_double_input(self):
        tokens = {
            "input": 1_000,
            "output": 2_000,
            "cache_read": 1_000_000,
            "cache_write_5m": 10_000,
            "cache_write_1h": 20_000,
        }
        # 0.01 + 0.1 + 1.0 + 0.125 + 0.4 = 1.635 USD
        self.assertEqual(
            server.claude_cost_microusd(tokens, FABLE_PRICE), 1_635_000
        )

    def test_codex_cost_subtracts_cached_from_input(self):
        tokens = {"input": 100_000, "cached_input": 90_000, "output": 10_000}
        # 10k*1.25 + 90k*0.125 + 10k*10 (每百万) = 0.12375 USD
        self.assertEqual(server.codex_cost_microusd(tokens, GPT5_PRICE), 123_750)

    def test_cached_never_exceeds_input(self):
        tokens = {"input": 100, "cached_input": 500, "output": 0}
        self.assertEqual(
            server.codex_cost_microusd(tokens, GPT5_PRICE),
            int(round(100 * 1.25e-07 * 1_000_000)),
        )

    def test_zero_tokens_is_zero_cost_not_none(self):
        tokens = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write_5m": 0,
            "cache_write_1h": 0,
        }
        self.assertEqual(server.claude_cost_microusd(tokens, FABLE_PRICE), 0)

    def test_all_none_tokens_is_none(self):
        empty = {key: None for key in (
            "input", "output", "cache_read", "cache_write_5m", "cache_write_1h"
        )}
        self.assertIsNone(server.claude_cost_microusd(empty, FABLE_PRICE))
        self.assertIsNone(
            server.codex_cost_microusd({"input": None, "output": None}, GPT5_PRICE)
        )

    def test_unknown_price_is_none(self):
        self.assertIsNone(server.claude_cost_microusd({"input": 10}, None))


class PrefixMatchTests(unittest.TestCase):
    def test_exact_beats_prefix(self):
        table = {"gpt-5": {"input": 1.0}, "gpt-5-mini": {"input": 2.0}}
        self.assertEqual(server.match_price("gpt-5-mini", table)["input"], 2.0)

    def test_longest_prefix_wins(self):
        table = {
            "claude-opus-4": {"input": 15.0},
            "claude-opus-4-5": {"input": 5.0},
        }
        self.assertEqual(
            server.match_price("claude-opus-4-5-20251101", table)["input"], 5.0
        )

    def test_gpt_56_sol_resolves_to_gpt5(self):
        table, _ = server.load_codex_prices(Path("/nonexistent/codex.json"))
        price = server.match_price("gpt-5.6-sol", table)
        self.assertIsNotNone(price)
        self.assertAlmostEqual(price["input"], 1.25e-06)

    def test_unmatched_model_is_none(self):
        table, _ = server.load_codex_prices(Path("/nonexistent/codex.json"))
        self.assertIsNone(server.match_price("codex-auto-review", table))

    def test_synthetic_model_is_none(self):
        self.assertIsNone(
            server.match_price("<synthetic>", {"claude-fable-5": FABLE_PRICE})
        )


class PriceLoadingTests(unittest.TestCase):
    def setUp(self):
        server._price_cache.clear()

    def test_missing_prices_file_falls_back(self):
        table, source = server.load_claude_prices(Path("/nonexistent/prices.json"))
        self.assertEqual(source, "fallback")
        self.assertAlmostEqual(table["claude-fable-5"]["input"], 1e-05)
        self.assertAlmostEqual(table["claude-fable-5"]["cache_write"], 1.25e-05)

    def test_live_prices_file_wins_and_cache_invalidates_on_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            path.write_text(
                json.dumps({"claude-test-1": {
                    "input": 1e-06, "output": 2e-06,
                    "cache_read": 1e-07, "cache_write": 1.25e-06,
                }}),
                encoding="utf-8",
            )
            os.utime(path, (1_000_000, 1_000_000))
            table, source = server.load_claude_prices(path)
            self.assertEqual(source, "live")
            self.assertIn("claude-test-1", table)

            path.write_text(
                json.dumps({"claude-test-2": {
                    "input": 1e-06, "output": 2e-06,
                    "cache_read": 1e-07, "cache_write": 1.25e-06,
                }}),
                encoding="utf-8",
            )
            os.utime(path, (2_000_000, 2_000_000))
            table, _ = server.load_claude_prices(path)
            self.assertIn("claude-test-2", table)
            self.assertNotIn("claude-test-1", table)

    def test_seed_codex_prices_and_never_overwrite_user_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex_prices.json"
            server.ensure_codex_price_file(path)
            self.assertTrue(path.exists())
            table, source = server.load_codex_prices(path)
            self.assertEqual(source, "file")
            self.assertAlmostEqual(table["gpt-5"]["output"], 1e-05)

            edited = {"_unit": "usd_per_mtok", "models": {
                "gpt-5": {"input": 9.0, "cached_input": 0.9, "output": 90.0}
            }}
            path.write_text(json.dumps(edited), encoding="utf-8")
            os.utime(path, (3_000_000, 3_000_000))
            server.ensure_codex_price_file(path)
            server._price_cache.clear()
            table, _ = server.load_codex_prices(path)
            self.assertAlmostEqual(table["gpt-5"]["input"], 9e-06)


class ReadNewLinesTests(unittest.TestCase):
    def test_partial_trailing_line_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.jsonl"
            path.write_bytes(b'{"a":1}\n{"half"')
            lines, offset, truncated = server.read_new_lines(path, 0, 1 << 20)
            self.assertEqual(lines, [b'{"a":1}'])
            self.assertEqual(offset, 8)
            self.assertFalse(truncated)
            with path.open("ab") as handle:
                handle.write(b':2}\n')
            lines, offset, _ = server.read_new_lines(path, offset, 1 << 20)
            self.assertEqual(lines, [b'{"half":2}'])
            self.assertEqual(offset, path.stat().st_size)

    def test_truncated_file_resets_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.jsonl"
            path.write_bytes(b'{"a":1}\n{"b":2}\n')
            _, offset, _ = server.read_new_lines(path, 0, 1 << 20)
            path.write_bytes(b'{"c":3}\n')
            lines, offset, truncated = server.read_new_lines(path, offset, 1 << 20)
            self.assertTrue(truncated)
            self.assertEqual(lines, [b'{"c":3}'])


class HistoryDbTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_path = self.root / "history.db"
        self.connection = server.history_connect(self.db_path)
        server.history_init(self.connection)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def rows(self, sql, args=()):
        return self.connection.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.connection.execute(sql, args).fetchone()


class ClaudeIngestTests(HistoryDbTestCase):
    def test_ingest_resume_upsert_and_no_content_leak(self):
        transcript = self.root / "session.jsonl"
        write_jsonl(
            transcript,
            [
                user_line("2026-08-18T10:00:00.000Z"),
                claude_line(
                    "2026-08-18T10:00:05.000Z",
                    "req-1",
                    {
                        "input_tokens": 2,
                        "output_tokens": 100,
                        "cache_read_input_tokens": 1_000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 50,
                            "ephemeral_1h_input_tokens": 200,
                        },
                        "output_tokens_details": {"thinking_tokens": 30},
                    },
                ),
                claude_line(
                    "2026-08-18T10:00:09.000Z",
                    "req-1",
                    {
                        "input_tokens": 2,
                        "output_tokens": 430,
                        "cache_read_input_tokens": 1_000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 50,
                            "ephemeral_1h_input_tokens": 200,
                        },
                        "output_tokens_details": {"thinking_tokens": 310},
                    },
                ),
            ],
        )
        target = claude_target(transcript, "sid-1", {"name": "edy-9b"})
        consumed, touched = server.ingest_target(
            self.connection, target, 1 << 24
        )
        self.assertTrue(touched)
        self.assertEqual(consumed, transcript.stat().st_size)

        turns = self.rows("SELECT * FROM turns")
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn["turn_key"], "req-1")
        self.assertEqual(turn["output_tokens"], 430)
        self.assertEqual(turn["reasoning_output_tokens"], 310)
        self.assertEqual(turn["cache_write_1h_tokens"], 200)
        self.assertEqual(
            turn["started_at_ms"],
            server.parse_iso_ms("2026-08-18T10:00:00.000Z"),
        )
        self.assertEqual(
            turn["ended_at_ms"],
            server.parse_iso_ms("2026-08-18T10:00:09.000Z"),
        )

        session = self.one(
            "SELECT * FROM sessions WHERE session_key='claude:sid-1'"
        )
        self.assertEqual(session["turn_count"], 1)
        self.assertEqual(session["total_tokens"], 2 + 430 + 1_000 + 50 + 200)
        self.assertEqual(session["context_peak_tokens"], 2 + 1_000 + 250)
        self.assertEqual(session["model"], "claude-fable-5")
        self.assertEqual(session["reasoning_effort"], "xhigh")
        self.assertEqual(session["name"], "edy-9b")

        # 续读：追加一轮换模型，offset 只前进增量。
        offset_before = self.one(
            "SELECT byte_offset FROM ingest_files"
        )["byte_offset"]
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user_line("2026-08-18T10:05:00.000Z")) + "\n")
            handle.write(
                json.dumps(
                    claude_line(
                        "2026-08-18T10:05:20.000Z",
                        "req-2",
                        {"input_tokens": 5, "output_tokens": 7},
                        model="claude-haiku-4-5",
                    )
                )
                + "\n"
            )
        consumed, _ = server.ingest_target(self.connection, target, 1 << 24)
        self.assertEqual(consumed, transcript.stat().st_size - offset_before)
        session = self.one(
            "SELECT * FROM sessions WHERE session_key='claude:sid-1'"
        )
        self.assertEqual(session["turn_count"], 2)
        self.assertEqual(json.loads(session["models_json"]),
                         ["claude-fable-5", "claude-haiku-4-5"])

        # 内容零泄漏：全库任何一行都不得出现哨兵字符串。
        for table in ("meta", "ingest_files", "sessions", "turns"):
            for row in self.rows(f"SELECT * FROM {table}"):
                self.assertNotIn(SENTINEL, str(tuple(row)))

    def test_sidechain_and_synthetic_lines_are_skipped(self):
        transcript = self.root / "session.jsonl"
        write_jsonl(
            transcript,
            [
                claude_line(
                    "2026-08-18T10:00:05.000Z",
                    "req-side",
                    {"input_tokens": 9},
                    isSidechain=True,
                ),
                claude_line(
                    "2026-08-18T10:00:06.000Z",
                    "req-syn",
                    {"input_tokens": 9},
                    model="<synthetic>",
                ),
            ],
        )
        server.ingest_target(
            self.connection, claude_target(transcript, "sid-2"), 1 << 24
        )
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 0)

    def test_truncated_transcript_resets_without_negative_totals(self):
        transcript = self.root / "session.jsonl"
        write_jsonl(
            transcript,
            [claude_line("2026-08-18T10:00:05.000Z", "r1", {"input_tokens": 10})],
        )
        target = claude_target(transcript, "sid-3")
        server.ingest_target(self.connection, target, 1 << 24)
        write_jsonl(
            transcript,
            [claude_line("2026-08-18T11:00:05.000Z", "r2", {"input_tokens": 3})],
        )
        server.ingest_target(self.connection, target, 1 << 24)
        session = self.one(
            "SELECT * FROM sessions WHERE session_key='claude:sid-3'"
        )
        self.assertGreaterEqual(session["input_tokens"], 0)
        self.assertEqual(
            self.one("SELECT byte_offset FROM ingest_files")["byte_offset"],
            transcript.stat().st_size,
        )


class CodexIngestTests(HistoryDbTestCase):
    def test_turn_deltas_from_cumulative_totals(self):
        rollout = self.root / "rollout.jsonl"
        write_jsonl(
            rollout,
            [
                codex_event("2026-08-18T02:00:00.000Z", {"type": "task_started"}),
                token_count_event(
                    "2026-08-18T02:00:30.000Z",
                    {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 800,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                    },
                    last_input=1_000,
                ),
                token_count_event(
                    "2026-08-18T02:01:00.000Z",
                    {
                        "input_tokens": 2_500,
                        "cached_input_tokens": 2_000,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 150,
                        "reasoning_output_tokens": 40,
                    },
                    last_input=2_500,
                ),
                codex_event("2026-08-18T02:01:10.000Z", {"type": "task_complete"}),
            ],
        )
        target = codex_target(
            rollout, "thread-1", {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}
        )
        server.ingest_target(self.connection, target, 1 << 24)

        turns = self.rows("SELECT * FROM turns")
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn["input_tokens"], 2_500)
        self.assertEqual(turn["cached_input_tokens"], 2_000)
        self.assertEqual(turn["output_tokens"], 150)
        self.assertEqual(turn["reasoning_output_tokens"], 40)
        self.assertEqual(turn["context_tokens"], 2_500)
        self.assertEqual(
            turn["ended_at_ms"],
            server.parse_iso_ms("2026-08-18T02:01:10.000Z"),
        )

        session = self.one(
            "SELECT * FROM sessions WHERE session_key='codex:thread-1'"
        )
        self.assertEqual(session["total_tokens"], 2_650)
        self.assertEqual(session["context_window"], 258_400)
        self.assertEqual(session["model"], "gpt-5.6-sol")

    def test_counter_regression_resets_baseline_never_negative(self):
        rollout = self.root / "rollout.jsonl"
        write_jsonl(
            rollout,
            [
                token_count_event(
                    "2026-08-18T02:00:30.000Z",
                    {"input_tokens": 5_000, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0, "output_tokens": 100,
                     "reasoning_output_tokens": 0},
                ),
                token_count_event(
                    "2026-08-18T02:01:30.000Z",
                    {"input_tokens": 200, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0, "output_tokens": 5,
                     "reasoning_output_tokens": 0},
                ),
            ],
        )
        server.ingest_target(
            self.connection, codex_target(rollout, "thread-2"), 1 << 24
        )
        turn = self.one("SELECT * FROM turns")
        self.assertEqual(turn["input_tokens"], 5_000)
        self.assertEqual(turn["output_tokens"], 100)

    def test_first_snapshot_of_new_file_skipped_when_session_has_history(self):
        self.connection.execute(
            "INSERT INTO sessions (session_key, platform, native_id, updated_at_ms)"
            " VALUES ('codex:thread-3', 'codex', 'thread-3', 1)"
        )
        self.connection.execute(
            """
            INSERT INTO turns (session_key, turn_key, input_tokens, output_tokens)
            VALUES ('codex:thread-3', 'old', 9_000, 400)
            """
        )
        self.connection.commit()
        rollout = self.root / "rollout2.jsonl"
        write_jsonl(
            rollout,
            [
                token_count_event(
                    "2026-08-18T03:00:00.000Z",
                    {"input_tokens": 9_500, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0, "output_tokens": 420,
                     "reasoning_output_tokens": 0},
                ),
                token_count_event(
                    "2026-08-18T03:01:00.000Z",
                    {"input_tokens": 9_800, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0, "output_tokens": 450,
                     "reasoning_output_tokens": 0},
                ),
            ],
        )
        server.ingest_target(
            self.connection, codex_target(rollout, "thread-3"), 1 << 24
        )
        session = self.one(
            "SELECT * FROM sessions WHERE session_key='codex:thread-3'"
        )
        # 旧轮 9_000/400 + 新文件只记第二个快照的增量 300/30。
        self.assertEqual(session["input_tokens"], 9_300)
        self.assertEqual(session["output_tokens"], 430)


class CodexConnectFallbackTests(unittest.TestCase):
    def make_db(self, path):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE threads (id TEXT)")
        connection.commit()
        connection.close()

    def test_healthy_db_uses_plain_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            self.make_db(db)
            connection = server.codex_connect_ro(db)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            connection.close()

    def test_falls_back_to_immutable_when_plain_ro_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            self.make_db(db)
            real_connect = sqlite3.connect
            attempts = []

            def flaky_connect(database, **kwargs):
                attempts.append(str(database))
                if "immutable" not in str(database):
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(database, **kwargs)

            with patch.object(server.sqlite3, "connect", side_effect=flaky_connect):
                connection = server.codex_connect_ro(db)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            connection.close()
            self.assertIn("immutable=1", attempts[-1])

    def test_raises_when_both_paths_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent.sqlite"
            with self.assertRaises(sqlite3.Error):
                server.codex_connect_ro(missing)


class CodexTargetSchemaTests(unittest.TestCase):
    def make_db(self, path, with_new_columns):
        columns = (
            "id TEXT, title TEXT, cwd TEXT, source TEXT, thread_source TEXT, "
            "rollout_path TEXT, updated_at_ms INTEGER"
        )
        if with_new_columns:
            columns += ", model TEXT, reasoning_effort TEXT, tokens_used INTEGER"
        connection = sqlite3.connect(path)
        connection.executescript(f"CREATE TABLE threads ({columns});")
        row = [
            "thread-x", "title", "/tmp/dir", "vscode", "user",
            "/tmp/rollout.jsonl", server.now_ms(),
        ]
        if with_new_columns:
            row += ["gpt-5.6-sol", "xhigh", 12_345]
        placeholders = ",".join("?" for _ in row)
        connection.execute(f"INSERT INTO threads VALUES ({placeholders})", row)
        connection.commit()
        connection.close()

    def test_missing_optional_columns_degrade_to_null(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state_5.sqlite"
            self.make_db(db, with_new_columns=False)
            with patch.object(server, "CODEX_DB_PATH", db):
                targets = server.codex_history_targets()
        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0]["meta"]["model"])
        self.assertIsNone(targets[0]["meta"]["total_tokens"])

    def test_new_columns_are_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state_5.sqlite"
            self.make_db(db, with_new_columns=True)
            with patch.object(server, "CODEX_DB_PATH", db):
                targets = server.codex_history_targets()
        self.assertEqual(targets[0]["meta"]["model"], "gpt-5.6-sol")
        self.assertEqual(targets[0]["meta"]["reasoning_effort"], "xhigh")
        self.assertEqual(targets[0]["meta"]["total_tokens"], 12_345)


class SubagentRollupTests(HistoryDbTestCase):
    def test_child_sessions_roll_up_into_parent_payload(self):
        parent = self.root / "parent.jsonl"
        write_jsonl(
            parent,
            [claude_line("2026-08-18T10:00:05.000Z", "r1",
                         {"input_tokens": 10, "output_tokens": 20})],
        )
        child = self.root / "agent-abc.jsonl"
        write_jsonl(
            child,
            [claude_line("2026-08-18T10:01:00.000Z", "r-child",
                         {"input_tokens": 100, "output_tokens": 200},
                         isSidechain=True)],
        )
        server.ingest_target(
            self.connection, claude_target(parent, "sid-p"), 1 << 24
        )
        # 子代理文件里的行即使标 isSidechain 也必须计入子会话本身。
        # （防双算的跳过只对主 transcript 生效——这里模拟主文件里的
        # sidechain 行已在上一个用例验证被跳过。）
        write_jsonl(
            child,
            [claude_line("2026-08-18T10:01:00.000Z", "r-child",
                         {"input_tokens": 100, "output_tokens": 200})],
        )
        server.ingest_target(
            self.connection,
            {
                "path": child,
                "session_key": "claude:sid-p/agent-abc",
                "platform": "claude",
                "native_id": "agent-abc",
                "meta": {"parent_session_key": "claude:sid-p", "name": "Explore"},
            },
            1 << 24,
        )

        with patch.object(server, "HISTORY_DB_PATH", self.db_path), patch.object(
            server, "CLAUDE_PRICES_PATH", self.root / "missing.json"
        ), patch.object(
            server, "CODEX_PRICES_PATH", self.root / "codex_prices.json"
        ):
            server._price_cache.clear()
            payload = server.history_summary_payload(days=7, limit=50)

        keys = [item["sessionKey"] for item in payload["sessions"]]
        self.assertIn("claude:sid-p", keys)
        self.assertNotIn("claude:sid-p/agent-abc", keys)
        parent_obj = next(
            item for item in payload["sessions"]
            if item["sessionKey"] == "claude:sid-p"
        )
        self.assertEqual(parent_obj["subagents"]["count"], 1)
        self.assertEqual(parent_obj["subagents"]["totalTokens"], 300)
        self.assertIsInstance(parent_obj["subagents"]["costMicroUsd"], int)


def walk_numbers(value, path=""):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        raise AssertionError(f"payload 出现 float：{path}={value}")
    if isinstance(value, dict):
        for key, item in value.items():
            walk_numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk_numbers(item, f"{path}[{index}]")


class HistoryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_path = self.root / "history.db"
        connection = server.history_connect(self.db_path)
        server.history_init(connection)
        current = server.now_ms()
        connection.execute(
            """
            INSERT INTO sessions (
              session_key, platform, native_id, name, model, reasoning_effort,
              started_at_ms, last_active_at_ms, turn_count,
              input_tokens, output_tokens, cache_read_tokens,
              cache_write_5m_tokens, cache_write_1h_tokens,
              total_tokens, context_peak_tokens, context_window,
              context_peak_pct, updated_at_ms, models_json
            ) VALUES (
              'claude:s1', 'claude', 's1', 'edy-9b', 'claude-fable-5', 'xhigh',
              ?, ?, 2, 1000, 2000, 1000000, 10000, 20000,
              1033000, 120000, 200000, 60, ?, '["claude-fable-5"]'
            )
            """,
            (current - 3_600_000, current - 60_000, current),
        )
        connection.execute(
            """
            INSERT INTO sessions (
              session_key, platform, native_id, name, model,
              last_active_at_ms, total_tokens, updated_at_ms, models_json
            ) VALUES (
              'codex:t1', 'codex', 't1', 'guardian', 'codex-auto-review',
              ?, 6397560, ?, '["codex-auto-review"]'
            )
            """,
            (current - 120_000, current),
        )
        connection.execute(
            """
            INSERT INTO turns (
              session_key, turn_key, started_at_ms, ended_at_ms, model,
              input_tokens, output_tokens, context_tokens
            ) VALUES ('claude:s1', 'req-1', ?, ?, 'claude-fable-5', 500, 900, 90000)
            """,
            (current - 3_600_000, current - 3_500_000),
        )
        connection.commit()
        connection.close()

        self.patches = [
            patch.object(server, "HISTORY_DB_PATH", self.db_path),
            patch.object(server, "CLAUDE_PRICES_PATH", self.root / "missing.json"),
            patch.object(
                server, "CODEX_PRICES_PATH", self.root / "codex_prices.json"
            ),
            patch.object(server, "snapshot", return_value={
                "schemaVersion": 1,
                "generatedAt": 1,
                "sources": {}, "notifications": {},
                "claude": [], "codex": [],
                "counts": {"claude": 0, "codex": 0, "satellites": 0},
            }),
            patch.object(server, "send_mac_notification"),
        ]
        for item in self.patches:
            item.start()
        server._price_cache.clear()
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
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def get(self, path, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=headers or {}
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_history_summary_shape_costs_and_int_discipline(self):
        with self.get("/api/history?days=99999&limit=99999") as response:
            body = json.loads(response.read())
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["pricing"]["claude"], "fallback")
        walk_numbers(body)

        by_key = {item["sessionKey"]: item for item in body["sessions"]}
        claude = by_key["claude:s1"]
        # 1000*10 + 2000*50 + 1e6*1 + 1e4*12.5 + 2e4*20 ($/MTok) = 1.635 USD
        self.assertEqual(claude["costMicroUsd"], 1_635_000)
        self.assertEqual(claude["costLabel"], "等效API标价")
        self.assertEqual(claude["contextPeakPct"], 60)
        codex = by_key["codex:t1"]
        self.assertIsNone(codex["costMicroUsd"])
        self.assertEqual(codex["costLabel"], "估算")
        self.assertEqual(codex["tokens"]["total"], 6_397_560)

    def test_history_session_detail_prices_turns(self):
        with self.get("/api/history/session?key=claude:s1&turns=10") as response:
            body = json.loads(response.read())
        self.assertEqual(len(body["turns"]), 1)
        turn = body["turns"][0]
        # 500*10 + 900*50 ($/MTok) = 0.05 USD
        self.assertEqual(turn["costMicroUsd"], 50_000)
        walk_numbers(body)

    def test_unknown_session_is_404(self):
        try:
            with self.get("/api/history/session?key=claude:nope"):
                self.fail("预期 404")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 404)
            error.close()

    def test_missing_db_reports_initializing(self):
        with patch.object(server, "HISTORY_DB_PATH", self.root / "absent.db"):
            with self.get("/api/history") as response:
                body = json.loads(response.read())
        self.assertEqual(body["state"], "initializing")
        self.assertEqual(body["sessions"], [])

    def test_agents_endpoint_unaffected_by_history(self):
        with self.get("/api/agents") as response:
            etag = response.headers["ETag"]
            body = json.loads(response.read())
        self.assertEqual(
            set(body.keys()),
            {
                "schemaVersion", "generatedAt", "sources", "notifications",
                "claude", "codex", "counts",
            },
        )
        self.assertNotIn("cost", json.dumps(body))
        try:
            with self.get("/api/agents", {"If-None-Match": etag}):
                self.fail("预期 304")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 304)
            error.close()


if __name__ == "__main__":
    unittest.main()

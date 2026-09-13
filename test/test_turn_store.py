"""验证 Turn 生命周期、事件记录和重启恢复。"""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from context_builder import ContextBuilder
from compaction import Compactor
from gateway import create_app
from runtime import AgentRuntime
from session_store import SessionStore
from tool_protocol import parse_model_action
from tools import Tool, ToolRegistry
from turn_store import TurnStore
from turn_schema import TURN_SCHEMA_VERSION


class TurnStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_turn_and_event_journal_survives_reopen(self):
        store = TurnStore(self.root)
        store.start("turn_1", "session_1", "chat", started_at="2026-07-14T10:00:00+00:00")
        store.update("turn_1", phase="tool_call", message="正在调用工具")
        store.append_event("turn_1", 1, "tool_call", {"tool": "current_time"})
        store.append_event("turn_1", 2, "done", {"status": "completed"})
        store.finish("turn_1", "completed")

        reopened = TurnStore(self.root)
        record = reopened.get("turn_1")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["last_seq"], 2)
        events = reopened.events_after("turn_1")
        self.assertEqual([item["eventName"] for item in events], ["tool_call", "done"])

        connection = sqlite3.connect(reopened.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                TURN_SCHEMA_VERSION,
            )
        finally:
            connection.close()

    def test_restart_marks_only_incomplete_turns(self):
        store = TurnStore(self.root)
        store.start("running", "session_1", "chat")
        store.start("done", "session_1", "chat")
        store.finish("done", "completed")
        self.assertEqual(store.mark_interrupted(), 1)
        self.assertEqual(store.get("running")["status"], "interrupted")
        self.assertEqual(store.get("done")["status"], "completed")

    def test_restart_does_not_reclassify_approval_required_turn(self):
        store = TurnStore(self.root)
        store.start("awaiting_approval", "session_1", "chat")
        store.transition("awaiting_approval", "awaiting_approval", active_approval_id="approval_1")
        self.assertEqual(store.mark_interrupted(), 0)
        self.assertEqual(store.get("awaiting_approval")["status"], "awaiting_approval")

    def test_duplicate_sequence_is_idempotent(self):
        store = TurnStore(self.root)
        store.start("turn_1", "session_1", "chat")
        store.append_event("turn_1", 1, "status", {"message": "first"})
        store.append_event("turn_1", 1, "status", {"message": "replayed"})
        events = store.events_after("turn_1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["message"], "first")

    def test_trace_parent_child_tree_and_schema_upgrade(self):
        store = TurnStore(self.root)
        store.start("turn_trace", "session_1", "chat")
        store.append_event(
            "turn_trace", 1, "status", {"phase": "model_call"},
            trace_id="turn_trace:model:1", trace_type="model_call",
        )
        store.append_event(
            "turn_trace", 2, "tool_call", {"callId": "call_1"},
            parent_seq=1, trace_id="call_1", trace_type="tool",
        )
        store.append_event(
            "turn_trace", 3, "tool_result", {"callId": "call_1"},
            parent_seq=2, trace_id="call_1", trace_type="tool_result",
        )
        events = store.events_after("turn_trace")
        self.assertIsNone(events[0]["parentSeq"])
        self.assertEqual(events[1]["parentSeq"], 1)
        self.assertEqual(events[2]["traceType"], "tool_result")
        tree = store.trace_tree("turn_trace")
        self.assertEqual(tree["eventCount"], 3)
        self.assertEqual(tree["roots"][0]["seq"], 1)
        self.assertEqual(tree["roots"][0]["children"][0]["seq"], 2)
        self.assertEqual(
            tree["roots"][0]["children"][0]["children"][0]["seq"], 3
        )

        # Re-opening exercises the migration path used by an existing
        # turns.sqlite3 created before trace columns were introduced.
        reopened = TurnStore(self.root)
        self.assertEqual(reopened.events_after("turn_trace")[1]["traceId"], "call_1")

    def test_existing_event_schema_is_upgraded_in_place(self):
        path = self.root / "turns.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE turns (
                    turn_id TEXT PRIMARY KEY, run_id TEXT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, phase TEXT,
                    message TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE TABLE turn_events (
                    turn_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    event_name TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (turn_id, seq)
                );
                """
            )
            connection.commit()
        finally:
            connection.close()
        store = TurnStore(self.root)
        connection = sqlite3.connect(path)
        try:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(turn_events)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue({"parent_seq", "trace_id", "trace_type"}.issubset(columns))
        self.assertIsNotNone(store.migration.backup_path)
        self.assertTrue(store.migration.backup_path.exists())

    def test_terminal_finish_is_idempotent(self):
        store = TurnStore(self.root)
        store.start("turn_1", "session_1", "chat")
        store.finish("turn_1", "completed")
        # A late worker cleanup must not overwrite a successful terminal
        # state with ``error``.
        store.finish("turn_1", "error", error="late cleanup")
        record = store.get("turn_1")
        self.assertEqual(record["status"], "completed")
        self.assertIsNone(record["error"])

    def test_stale_run_cannot_mutate_resumed_turn(self):
        store = TurnStore(self.root)
        store.start(
            "turn_fenced", "session_1", "chat",
            run_id="run_old",
        )
        resumed = store.transition(
            "turn_fenced", "running", run_id="run_new",
            expected_run_id="run_old", expected_statuses={"starting"},
        )
        self.assertEqual(resumed["run_id"], "run_new")

        self.assertFalse(store.update(
            "turn_fenced", phase="late_old_update", expected_run_id="run_old"
        ))
        self.assertFalse(store.append_event(
            "turn_fenced", 1, "assistant_final", {"content": "stale"},
            expected_run_id="run_old",
        ))
        stale_transition = store.transition(
            "turn_fenced", "awaiting_approval", active_approval_id="approval_old",
            expected_run_id="run_old", expected_statuses={"running"},
        )
        self.assertEqual(stale_transition["status"], "running")
        self.assertFalse(store.finish(
            "turn_fenced", "completed", expected_run_id="run_old"
        ))

        self.assertTrue(store.append_event(
            "turn_fenced", 1, "assistant_final", {"content": "current"},
            expected_run_id="run_new",
        ))
        self.assertTrue(store.finish(
            "turn_fenced", "completed", expected_run_id="run_new"
        ))
        record = store.get("turn_fenced")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["phase"], "completed")
        self.assertEqual(
            store.events_after("turn_fenced")[0]["payload"]["content"],
            "current",
        )

    def test_terminal_events_replay_without_in_memory_worker(self):
        data_dir = self.root / "gateway-replay-data"
        runtime = AgentRuntime(
            type("Model", (), {"complete": lambda self, messages: '{"type":"final","content":"ok"}'})(),
            SessionStore(data_dir), ContextBuilder(),
        )
        app = create_app(
            runtime, web_dir=Path(__file__).parents[1] / "web",
            start_background_services=False,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"sessionId": "default", "message": "hello", "turnId": "turn_replay"},
            )
            self.assertIn("event: done", response.text)
            # The worker has already left the in-memory active map. A refresh
            # must still replay the durable journal and close immediately.
            replay = client.get("/api/turns/turn_replay/stream?after=0")
            self.assertEqual(replay.status_code, 200)
            self.assertIn("event: assistant_final", replay.text)
            self.assertIn("event: done", replay.text)

    def test_gateway_persists_completed_stream_turn(self):
        class Model:
            def complete(self, _messages):
                return '{"type":"final","content":"ok"}'

        data_dir = self.root / "gateway-data"
        runtime = AgentRuntime(Model(), SessionStore(data_dir), ContextBuilder())
        app = create_app(runtime, web_dir=Path(__file__).parents[1] / "web", start_background_services=False)
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"sessionId": "default", "message": "hello", "turnId": "turn_gateway"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('"agentEvent"', response.text)
            checkpoint = client.get("/api/turns/turn_gateway")
            self.assertEqual(checkpoint.status_code, 200)
            self.assertEqual(checkpoint.json()["status"], "completed")
            self.assertGreaterEqual(checkpoint.json()["last_seq"], 1)
            replay = client.get("/api/turns/turn_gateway/events")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json()["events"])
            trace = client.get("/api/turns/turn_gateway/trace")
            self.assertEqual(trace.status_code, 200)
            # Token deltas stay on the live transport path and are not
            # persisted one-by-one. Durable sequence numbers may therefore
            # contain gaps, while final/lifecycle events remain replayable.
            self.assertLessEqual(trace.json()["eventCount"], checkpoint.json()["last_seq"])
            durable_names = [
                item["eventName"]
                for item in client.get("/api/turns/turn_gateway/events").json()["events"]
            ]
            self.assertNotIn("assistant_delta", durable_names)
            self.assertIn("assistant_final", durable_names)
            self.assertIn("done", durable_names)
            self.assertIn("turn_gateway", [item["turn_id"] for item in client.get("/api/turns/history").json()["turns"]])

    def test_final_answer_checkpoint_survives_post_turn_compaction(self):
        class Model:
            def __init__(self):
                self.replies = iter(["normal reply", "summary after answer"])

            def complete(self, _messages):
                return next(self.replies)

        data_dir = self.root / "gateway-compaction-data"
        store = SessionStore(data_dir)
        session = store.current
        session.messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(10)
        ]
        store.save(session)
        model = Model()
        compactor = Compactor(
            model,
            store,
            Path(__file__).parents[1] / "prompts" / "compact_prompt.md",
            max_messages=6,
            keep_recent=4,
        )
        runtime = AgentRuntime(model, store, ContextBuilder(), compactor)
        app = create_app(
            runtime,
            web_dir=Path(__file__).parents[1] / "web",
            start_background_services=False,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"sessionId": "default", "message": "hello", "turnId": "turn_compact_checkpoint"},
            )
            self.assertEqual(response.status_code, 200)
            checkpoint = client.get("/api/turns/turn_compact_checkpoint")
            self.assertEqual(checkpoint.json()["status"], "completed")
            events = client.get("/api/turns/turn_compact_checkpoint/events").json()["events"]
            event_names = [event["eventName"] for event in events]
            self.assertLess(event_names.index("assistant_final"), event_names.index("compaction_started"))

    def test_tool_call_id_is_preserved_and_event_envelope_is_canonical(self):
        action = parse_model_action(
            '{"type":"tool_call","tool":"echo","id":"native_42","args":{}}'
        )
        self.assertEqual(action.calls[0].call_id, "native_42")

        class Model:
            def __init__(self):
                self.replies = iter([
                    '{"type":"tool_call","tool":"echo","args":{}}',
                    '{"type":"final","content":"完成"}',
                ])

            def complete(self, _messages):
                return next(self.replies)

        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo", {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"value": "ok"},
        ))
        data_dir = self.root / "event-data"
        runtime = AgentRuntime(
            Model(), SessionStore(data_dir),
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        events = []
        runtime.run("调用 echo", event_callback=events.append, turn_id="turn_events")
        tool_call = next(item for item in events if item["type"] == "tool_call")
        tool_result = next(item for item in events if item["type"] == "tool_result")
        self.assertEqual(tool_call["callId"], tool_result["callId"])
        self.assertEqual(tool_call["step"], tool_result["step"])


if __name__ == "__main__":
    unittest.main()

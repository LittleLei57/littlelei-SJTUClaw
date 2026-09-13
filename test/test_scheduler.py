"""TaskStore、Scheduler 与 Gateway Task API 测试。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from context_builder import ContextBuilder
from gateway import create_app
from runtime import AgentRuntime
from scheduler import Scheduler, TaskStore, cron_next, iso_utc, parse_cron
from scheduler_tools import SchedulerTools, register_scheduler_tools
from session_store import SessionStore
from tools import ToolExecutionContext, ToolRegistry


class EchoModel:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("scheduled model offline")
        return f"scheduled: {messages[-1]['content']}"


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = SessionStore(self.root / "data")
        self.model = EchoModel()
        self.runtime = AgentRuntime(self.model, self.sessions, ContextBuilder())
        self.tasks = TaskStore(self.root / "data", self.sessions)
        self.scheduler = Scheduler(self.tasks, self.runtime)

    def tearDown(self):
        self.scheduler.stop()
        self.temp.cleanup()

    def future(self, seconds=60):
        return iso_utc(datetime.now(timezone.utc) + timedelta(seconds=seconds))

    def test_once_task_executes_through_runtime_and_completes(self):
        task = self.tasks.create_once("生成一次总结", "default", self.future())
        count = self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        restored = self.tasks.get(task.task_id)
        self.assertEqual(count, 1)
        self.assertEqual(restored.status, "completed")
        self.assertIsNone(restored.next_run_at)
        self.assertEqual(restored.history[0]["assistantReply"], "scheduled: 生成一次总结")
        self.assertEqual(len(self.sessions.get("default").messages), 2)

    def test_concurrent_due_scans_claim_a_task_only_once(self):
        from threading import Event, Thread

        started = Event()
        release = Event()

        class BlockingModel(EchoModel):
            def complete(inner_self, messages):
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return super().complete(messages)

        runtime = AgentRuntime(BlockingModel(), self.sessions, ContextBuilder())
        scheduler = Scheduler(self.tasks, runtime)
        task = self.tasks.create_once("concurrent trigger", "default", self.future())
        now = datetime.now(timezone.utc) + timedelta(minutes=2)
        first = Thread(target=scheduler.run_due_once, args=(now,))
        second = Thread(target=scheduler.run_due_once, args=(now,))
        first.start()
        self.assertTrue(started.wait(timeout=2))
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        restored = self.tasks.get(task.task_id)
        self.assertEqual(restored.run_count, 1)
        self.assertEqual(len(restored.history), 1)
        self.assertEqual(len(runtime.model.calls), 1)
        scheduler.stop()

    def test_scheduler_pushes_success_to_notifier(self):
        class Notifier:
            def __init__(self): self.events = []
            def notify(self, session_id, event):
                self.events.append((session_id, event)); return True

        notifier = Notifier()
        scheduler = Scheduler(self.tasks, self.runtime, notifier=notifier)
        self.tasks.create_once("push result", "default", self.future())
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(notifier.events[-1][0], "default")
        self.assertEqual(notifier.events[-1][1].event_type, "final")
        self.assertIn("scheduled: push result", notifier.events[-1][1].text)
        self.assertTrue(notifier.events[-1][1].data["deliveryId"].startswith("scheduler:"))

    def test_scheduler_turn_has_transient_trigger_context(self):
        task = self.tasks.create_once("后台整理提醒", "default", self.future())
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=10))
        system_prompt = self.model.calls[-1][0]["content"]
        self.assertIn("# Scheduler Trigger Context", system_prompt)
        self.assertIn(f"taskId: {task.task_id}", system_prompt)
        self.assertIn("不是用户刚刚发送的普通聊天消息", system_prompt)
        # The task content remains auditable as the actual user message; the
        # trigger explanation is not persisted into the visible transcript.
        messages = self.sessions.get("default").messages
        self.assertEqual(messages[0]["content"], "后台整理提醒")
        self.assertNotIn("Scheduler Trigger Context", messages[0]["content"])

    def test_scheduler_approval_pause_does_not_also_push_failure(self):
        class PendingRuntime:
            store = self.sessions

            def run(inner_self, content, session_id, make_current=False):
                return SimpleNamespace(
                    reply="",
                    pending_approvals=[{"approvalId": "approval_1"}],
                )

        class Notifier:
            def __init__(self): self.events = []
            def notify(self, session_id, event):
                self.events.append(event); return True

        notifier = Notifier()
        scheduler = Scheduler(self.tasks, PendingRuntime(), notifier=notifier)
        task = self.tasks.create_once("needs approval", "default", self.future())
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual([event.event_type for event in notifier.events], ["approval_required"])
        self.assertNotIn("scheduler_task_failed", str(self.sessions.get("default").messages))
        waiting = self.tasks.get(task.task_id)
        self.assertEqual(waiting.status, "waiting_approval")
        self.assertFalse(waiting.enabled)
        self.assertEqual(waiting.pending_approval_ids, ["approval_1"])
        scheduler.on_approval_result(
            "approval_1", SimpleNamespace(reply="审批后完成", pending_approvals=[])
        )
        resumed = self.tasks.get(task.task_id)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.run_count, 1)
        self.assertEqual(resumed.history[0]["assistantReply"], "审批后完成")

    def test_waiting_approval_survives_scheduler_restart_and_resolves_once(self):
        class PendingRuntime:
            store = self.sessions

            def run(inner_self, content, session_id, make_current=False):
                return SimpleNamespace(
                    reply="", pending_approvals=[{"approvalId": "approval_restart"}],
                )

        first = Scheduler(self.tasks, PendingRuntime())
        task = self.tasks.create_once("approval restart", "default", self.future())
        first.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.tasks.get(task.task_id).status, "waiting_approval")

        restored_store = TaskStore(self.root / "data", self.sessions)
        restored_task = restored_store.find_waiting_by_approval("approval_restart")
        self.assertIsNotNone(restored_task)
        second = Scheduler(restored_store, PendingRuntime())
        second.on_approval_result(
            "approval_restart", SimpleNamespace(reply="resumed", pending_approvals=[])
        )
        finished = restored_store.get(task.task_id)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.run_count, 1)
        self.assertEqual(len(finished.history), 1)
        # A retried callback after completion is a harmless no-op.
        second.on_approval_result(
            "approval_restart", SimpleNamespace(reply="duplicate", pending_approvals=[])
        )
        self.assertEqual(len(restored_store.get(task.task_id).history), 1)
        first.stop()
        second.stop()

    def test_scheduler_does_not_steal_current_session(self):
        other = self.sessions.create("background")
        self.sessions.set_current_id("default")
        self.tasks.create_once("后台检查", other.session_id, self.future())
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.sessions.current_id, "default")
        self.assertEqual(len(self.sessions.get(other.session_id).messages), 2)

    def test_interval_task_repeats_and_keeps_all_history(self):
        task = self.tasks.create_interval("周期检查", "default", 30, self.future(10))
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(seconds=20))
        first = self.tasks.get(task.task_id)
        self.assertEqual(first.status, "pending")
        self.assertIsNotNone(first.next_run_at)
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        second = self.tasks.get(task.task_id)
        self.assertEqual(len(second.history), 2)
        self.assertEqual(len(self.sessions.get("default").messages), 4)

    def test_interval_task_supports_end_time_and_max_runs(self):
        task = self.tasks.create_interval(
            "限次检查", "default", 30, self.future(10), self.future(120), max_runs=2
        )
        self.assertEqual(task.run_count, 0)
        self.assertEqual(task.max_runs, 2)
        self.assertIsNotNone(task.starts_at)
        self.assertIsNotNone(task.ends_at)
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(seconds=20))
        self.assertEqual(self.tasks.get(task.task_id).status, "pending")
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        finished = self.tasks.get(task.task_id)
        self.assertEqual(finished.run_count, 2)
        self.assertEqual(finished.status, "completed")
        self.assertIsNone(finished.next_run_at)

    def test_cron_expression_and_timezone_schedule_next_occurrence(self):
        fields = parse_cron("0 9 * * 1-5")
        self.assertIn(0, fields[0])
        next_run = cron_next(
            "0 9 * * 1-5",
            "Asia/Shanghai",
            datetime(2026, 7, 16, 1, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(next_run, datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "5 段"):
            parse_cron("0 9 * *")

    def test_cron_task_repeats_and_honors_max_runs(self):
        task = self.tasks.create_cron(
            "Cron 检查", "default", "*/5 * * * *", self.future(10), max_runs=2
        )
        self.assertEqual(task.task_type, "cron")
        self.assertEqual(task.timezone_name, "Asia/Shanghai")
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=10))
        first = self.tasks.get(task.task_id)
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.run_count, 1)
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=20))
        finished = self.tasks.get(task.task_id)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.run_count, 2)

    def test_interval_task_can_pause_and_resume(self):
        task = self.tasks.create_interval("可暂停", "default", 30, self.future(10))
        paused = self.tasks.pause(task.task_id)
        self.assertEqual(paused.status, "paused")
        self.assertEqual(self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2)), 0)
        resumed = self.tasks.resume(task.task_id)
        self.assertEqual(resumed.status, "pending")
        self.assertTrue(resumed.enabled)

    def test_manual_run_completes_once_task(self):
        task = self.tasks.create_once("立即总结", "default", self.future())
        running = self.scheduler.run_task_now(task.task_id)
        self.assertEqual(running.status, "running")
        for _ in range(50):
            if self.tasks.get(task.task_id).status == "completed":
                break
            time.sleep(0.01)
        finished = self.tasks.get(task.task_id)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.run_count, 1)
        self.assertTrue(finished.history[0]["manual"])
        self.assertIsNone(finished.next_run_at)

    def test_manual_interval_run_does_not_advance_schedule(self):
        task = self.tasks.create_interval("手动试跑", "default", 300, self.future(120))
        next_run = task.next_run_at
        self.scheduler.run_task_now(task.task_id)
        for _ in range(50):
            if self.tasks.get(task.task_id).status == "pending":
                break
            time.sleep(0.01)
        finished = self.tasks.get(task.task_id)
        self.assertEqual(finished.status, "pending")
        self.assertEqual(finished.next_run_at, next_run)
        self.assertEqual(finished.run_count, 0)
        self.assertTrue(finished.history[0]["manual"])

    def test_periodic_failure_is_recorded_and_future_run_remains(self):
        failing_runtime = AgentRuntime(EchoModel(fail=True), self.sessions, ContextBuilder())
        scheduler = Scheduler(self.tasks, failing_runtime)
        task = self.tasks.create_interval("失败也继续", "default", 60, self.future())
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        restored = self.tasks.get(task.task_id)
        self.assertEqual(restored.status, "failed")
        self.assertIsNotNone(restored.next_run_at)
        self.assertFalse(restored.history[0]["success"])
        self.assertIn("scheduled model offline", restored.history[0]["error"])
        self.assertIn("scheduler_task_failed", str(self.sessions.current.messages))

    def test_periodic_failure_can_be_retried_on_a_later_due_tick(self):
        model = EchoModel(fail=True)
        runtime = AgentRuntime(model, self.sessions, ContextBuilder())
        scheduler = Scheduler(self.tasks, runtime)
        task = self.tasks.create_interval("retry later", "default", 60, self.future())
        first_now = datetime.now(timezone.utc) + timedelta(minutes=2)
        scheduler.run_due_once(first_now)
        failed = self.tasks.get(task.task_id)
        self.assertEqual(failed.status, "failed")
        self.assertIsNotNone(failed.next_run_at)

        model.fail = False
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=4))
        recovered = self.tasks.get(task.task_id)
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.run_count, 2)
        self.assertEqual(len(recovered.history), 2)
        self.assertTrue(recovered.history[-1]["success"])
        scheduler.stop()

    def test_interval_end_time_expires_task_without_running_past_boundary(self):
        start = datetime.now(timezone.utc) + timedelta(seconds=10)
        end = start + timedelta(seconds=35)
        task = self.tasks.create_interval(
            "end boundary", "default", 30, iso_utc(start), iso_utc(end)
        )
        self.scheduler.run_due_once(start + timedelta(seconds=1))
        pending = self.tasks.get(task.task_id)
        self.assertEqual(pending.status, "pending")
        self.assertIsNotNone(pending.next_run_at)
        self.scheduler.run_due_once(end + timedelta(seconds=1))
        expired = self.tasks.get(task.task_id)
        self.assertEqual(expired.status, "expired")
        self.assertFalse(expired.enabled)
        self.assertIsNone(expired.next_run_at)

    def test_cancel_prevents_future_trigger(self):
        task = self.tasks.create_interval("不要再运行", "default", 60, self.future())
        self.tasks.cancel(task.task_id)
        self.assertEqual(self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(days=1)), 0)
        self.assertEqual(self.tasks.get(task.task_id).status, "cancelled")

    def test_restart_restores_tasks_and_recovers_running_status(self):
        task = self.tasks.create_once("重启恢复", "default", self.future())
        self.tasks.mark_running(task.task_id)
        restored_store = TaskStore(self.root / "data", self.sessions)
        restored = restored_store.get(task.task_id)
        self.assertEqual(restored.status, "pending")
        self.assertEqual(restored.next_run_at, task.next_run_at)

    def test_invalid_time_rule_and_session_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "时区"):
            self.tasks.create_once("x", "default", "2026-07-07T10:00:00")
        with self.assertRaisesRegex(ValueError, "未来"):
            self.tasks.create_once("x", "default", "2020-01-01T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "intervalSeconds"):
            self.tasks.create_interval("x", "default", 0)
        with self.assertRaises(KeyError):
            self.tasks.create_interval("x", "missing", 10)

    def test_task_json_survives_store_restart(self):
        task = self.tasks.create_interval("persist", "default", 300)
        restored = TaskStore(self.root / "data", self.sessions).get(task.task_id)
        self.assertEqual(restored.content, "persist")
        self.assertEqual(restored.interval_seconds, 300)

    def test_task_modes_are_persisted_and_validated(self):
        task = self.tasks.create_once(
            "isolated task", "default", self.future(),
            execution_context="isolated", delivery_mode="none",
        )
        restored = TaskStore(self.root / "data", self.sessions).get(task.task_id)
        self.assertEqual(restored.execution_context, "isolated")
        self.assertEqual(restored.delivery_mode, "none")
        channel_task = self.tasks.create_once(
            "channel task", "default", self.future(),
            delivery_mode="channel", delivery_channel="feishu",
        )
        channel_restored = TaskStore(self.root / "data", self.sessions).get(channel_task.task_id)
        self.assertEqual(channel_restored.delivery_channel, "feishu")
        broadcast_task = self.tasks.create_once(
            "broadcast task", "default", self.future(),
            delivery_mode="channel", delivery_channels=["feishu", "qqbot"],
        )
        broadcast_restored = TaskStore(self.root / "data", self.sessions).get(broadcast_task.task_id)
        self.assertIsNone(broadcast_restored.delivery_channel)
        self.assertEqual(broadcast_restored.delivery_channels, ["feishu", "qqbot"])
        with self.assertRaisesRegex(ValueError, "executionContext"):
            self.tasks.create_once(
                "bad", "default", self.future(), execution_context="invalid",
            )
        with self.assertRaisesRegex(ValueError, "deliveryMode"):
            self.tasks.create_once(
                "bad", "default", self.future(), delivery_mode="invalid",
            )
        with self.assertRaisesRegex(ValueError, "deliveryChannel"):
            self.tasks.create_once(
                "bad", "default", self.future(),
                delivery_mode="channel", delivery_channel="telegram",
            )
        with self.assertRaisesRegex(ValueError, "只能与 channel"):
            self.tasks.create_once(
                "bad", "default", self.future(),
                delivery_mode="session", delivery_channel="feishu",
            )
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.tasks.create_once(
                "bad", "default", self.future(),
                delivery_mode="channel", delivery_channel="feishu",
                delivery_channels=["qqbot"],
            )

    def test_scheduler_rejects_unimplemented_webhook_mode(self):
        task = self.tasks.create_once(
            "webhook task", "default", self.future(),
            delivery_mode="webhook",
        )
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        restored = self.tasks.get(task.task_id)
        self.assertEqual(restored.status, "failed")
        self.assertFalse(self.model.calls)
        self.assertIn("尚未配置", restored.history[0]["error"])

    def test_current_context_uses_active_session_at_trigger_time(self):
        active = self.sessions.create("active")
        self.sessions.set_current_id(active.session_id)
        task = self.tasks.create_once(
            "current context", "default", self.future(),
            execution_context="current",
        )
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.tasks.get(task.task_id).status, "completed")
        self.assertEqual(len(self.sessions.get(active.session_id).messages), 2)
        self.assertEqual(len(self.sessions.get("default").messages), 0)

    def test_isolated_context_gets_reused_execution_session(self):
        task = self.tasks.create_interval(
            "isolated context", "default", 30, self.future(10),
            execution_context="isolated", delivery_mode="none",
        )
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(seconds=20))
        first = self.tasks.get(task.task_id)
        self.assertEqual(first.status, "pending")
        self.assertIsNotNone(first.execution_session_id)
        isolated_id = first.execution_session_id
        self.assertEqual(len(self.sessions.get(isolated_id).messages), 2)
        self.assertEqual(len(self.sessions.get("default").messages), 0)
        self.scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        second = self.tasks.get(task.task_id)
        self.assertEqual(second.execution_session_id, isolated_id)
        self.assertEqual(len(self.sessions.get(isolated_id).messages), 4)

    def test_none_delivery_keeps_history_without_notifying(self):
        class Notifier:
            def __init__(self): self.events = []
            def notify(self, session_id, event):
                self.events.append(event); return True

        notifier = Notifier()
        scheduler = Scheduler(self.tasks, self.runtime, notifier=notifier)
        task = self.tasks.create_once(
            "silent task", "default", self.future(), delivery_mode="none",
        )
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.tasks.get(task.task_id).status, "completed")
        self.assertEqual(notifier.events, [])

    def test_webhook_delivery_uses_configured_sender(self):
        events = []

        def sender(session_id, event):
            events.append((session_id, event.event_type, event.text, event.data))
            return True

        scheduler = Scheduler(self.tasks, self.runtime, webhook_sender=sender)
        task = self.tasks.create_once(
            "webhook task", "default", self.future(), delivery_mode="webhook",
        )
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.tasks.get(task.task_id).status, "completed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "default")
        self.assertEqual(events[0][1], "final")
        self.assertEqual(events[0][3]["deliveryMode"], "webhook")
        self.assertEqual(events[0][3]["notification"]["status"], "completed")

    def test_channel_delivery_carries_explicit_channel_target(self):
        events = []

        class Notifier:
            def notify(self, session_id, event):
                events.append((session_id, event.data))
                return True

        scheduler = Scheduler(self.tasks, self.runtime, notifier=Notifier())
        task = self.tasks.create_once(
            "channel task", "default", self.future(),
            delivery_mode="channel", delivery_channel="qqbot",
        )
        scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.tasks.get(task.task_id).status, "completed")
        self.assertEqual(events[0][1]["deliveryMode"], "channel")
        self.assertEqual(events[0][1]["deliveryChannel"], "qqbot")

    def test_agent_scheduler_tools_share_task_store(self):
        registry = ToolRegistry()
        register_scheduler_tools(registry, SchedulerTools(self.tasks))
        self.assertIn("schedule_run", {item["name"] for item in registry.definitions()})
        result = registry.execute(
            "schedule_create",
            {"content": "agent reminder", "task_type": "once", "run_at": self.future()},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        listed = registry.execute(
            "schedule_list", {}, ToolExecutionContext("default")
        )
        self.assertTrue(listed.success)
        self.assertEqual(listed.output["tasks"][0]["content"], "agent reminder")

    def test_agent_scheduler_tool_can_create_timezone_aware_cron_task(self):
        registry = ToolRegistry()
        register_scheduler_tools(registry, SchedulerTools(self.tasks))
        result = registry.execute(
            "schedule_create",
            {
                "content": "daily briefing",
                "task_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "timezone": "Asia/Shanghai",
                "start_at": self.future(3600),
                # Keep the test independent of the weekday on which it runs:
                # late Friday's next 09:00 weekday can be more than 24 hours
                # after ``start_at``.
                "end_at": self.future(7 * 86400),
                "max_runs": 3,
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        task = result.output["task"]
        self.assertEqual(task["taskType"], "cron")
        self.assertEqual(task["timezone"], "Asia/Shanghai")
        self.assertEqual(task["maxRuns"], 3)
        self.assertIsNotNone(task["startsAt"])
        self.assertIsNotNone(task["endsAt"])

    def test_scheduler_tool_schema_avoids_unsupported_grammar_keywords(self):
        registry = ToolRegistry()
        register_scheduler_tools(registry, SchedulerTools(self.tasks))
        schedule_create = next(item for item in registry.definitions() if item["name"] == "schedule_create")
        encoded = json.dumps(schedule_create, ensure_ascii=False)
        self.assertNotIn("uniqueItems", encoded)


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sessions = SessionStore(root / "data")
        runtime = AgentRuntime(EchoModel(), self.sessions, ContextBuilder())
        task_store = TaskStore(root / "data", self.sessions)
        scheduler = Scheduler(task_store, runtime, poll_seconds=60)
        self.client = TestClient(
            create_app(runtime, web_dir=Path(__file__).parents[1] / "web", scheduler=scheduler)
        )

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_create_list_detail_and_cancel_interval_task(self):
        response = self.client.post(
            "/api/tasks",
            json={
                "content": "API periodic",
                "sessionId": "default",
                "taskType": "interval",
                "intervalSeconds": 60,
            },
        )
        self.assertEqual(response.status_code, 201)
        task_id = response.json()["taskId"]
        listed = self.client.get("/api/tasks").json()["tasks"]
        self.assertEqual(listed[0]["taskId"], task_id)
        for field in {
            "content", "taskType", "sessionId", "nextRunAt", "status",
            "intervalSeconds", "startsAt", "endsAt", "maxRuns", "runCount",
            "enabled", "pendingApprovalIds", "waitingStartedAt", "history",
            "createdAt", "updatedAt", "cronExpression", "timezone",
            "activeRunMode",
            "executionContext", "deliveryMode",
        }:
            self.assertIn(field, listed[0])
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").status_code, 200)
        cancelled = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_create_interval_with_boundaries_and_pause_resume_api(self):
        start = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        response = self.client.post(
            "/api/tasks",
            json={
                "content": "API bounded periodic",
                "sessionId": "default",
                "taskType": "interval",
                "intervalSeconds": 60,
                "startAt": start,
                "endAt": end,
                "maxRuns": 3,
            },
        )
        self.assertEqual(response.status_code, 201)
        task_id = response.json()["taskId"]
        self.assertEqual(response.json()["maxRuns"], 3)
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/pause").json()["status"], "paused")
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/resume").json()["status"], "pending")

    def test_create_cron_task_api(self):
        response = self.client.post(
            "/api/tasks",
            json={
                "content": "API cron",
                "sessionId": "default",
                "taskType": "cron",
                "cronExpression": "0 9 * * 1-5",
                "timezone": "Asia/Shanghai",
                "maxRuns": 2,
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["taskType"], "cron")
        self.assertEqual(body["cronExpression"], "0 9 * * 1-5")
        self.assertEqual(body["timezone"], "Asia/Shanghai")

    def test_run_task_now_api_queues_manual_execution(self):
        response = self.client.post(
            "/api/tasks",
            json={"content": "API manual", "sessionId": "default", "taskType": "once", "runAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()},
        )
        task_id = response.json()["taskId"]
        queued = self.client.post(f"/api/tasks/{task_id}/run")
        self.assertEqual(queued.status_code, 202)
        for _ in range(50):
            if self.client.get(f"/api/tasks/{task_id}").json()["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").json()["status"], "completed")

    def test_create_once_validation_and_session_filter(self):
        invalid = self.client.post(
            "/api/tasks",
            json={"content": "x", "sessionId": "default", "taskType": "once"},
        )
        self.assertEqual(invalid.status_code, 400)
        missing = self.client.get("/api/tasks?sessionId=missing")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()

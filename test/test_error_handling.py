"""CLI 失败后继续输入与 Session 不污染验收测试。"""

from pathlib import Path
import tempfile
import unittest

from context_builder import ContextBuilder
from main import run_cli
from runtime import AgentRuntime, EmptyAssistantResponse
from session_store import SessionStore


class SequenceModel:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp.name) / "data")

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_input_does_not_call_model_or_write_session(self):
        model = SequenceModel([])
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        inputs = iter(["   ", "/exit"])
        outputs = []
        self.assertEqual(
            run_cli(runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append), 0
        )
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.store.current.messages, [])

    def test_exit_prints_bye_without_calling_model(self):
        model = SequenceModel([])
        outputs = []
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        run_cli(runtime, input_fn=lambda _: "/exit", output_fn=outputs.append)
        self.assertEqual(model.calls, 0)
        self.assertEqual(outputs[-1], "bye.")

    def test_llm_failure_does_not_write_messages_and_next_turn_succeeds(self):
        model = SequenceModel([RuntimeError("network down"), "recovered"])
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        inputs = iter(["first", "second", "/exit"])
        outputs = []
        run_cli(runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append)
        self.assertTrue(any("network down" in item for item in outputs))
        self.assertTrue(any("Assistant> recovered" in item for item in outputs))
        self.assertEqual(
            self.store.current.messages,
            [
                {"role": "user", "content": "second", "metadata": {"source": "cli"}},
                {"role": "assistant", "content": "recovered"},
            ],
        )

    def test_empty_assistant_is_rejected_and_next_turn_succeeds(self):
        model = SequenceModel(["   \n", "valid answer"])
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        inputs = iter(["first", "retry", "/exit"])
        outputs = []
        run_cli(runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append)
        self.assertTrue(any("空 assistant 回复" in item for item in outputs))
        self.assertTrue(any("Assistant> valid answer" in item for item in outputs))
        self.assertEqual(len(self.store.current.messages), 2)
        self.assertEqual(self.store.current.messages[0]["content"], "retry")
        self.assertTrue(any(item["type"] == "turn_failed" for item in self.store.current.activity))

    def test_ctrl_c_during_model_returns_to_input_loop(self):
        model = SequenceModel([KeyboardInterrupt()])
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        inputs = iter(["interrupt me", "/exit"])
        outputs = []
        run_cli(runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append)
        self.assertTrue(any("本次请求已中断" in item for item in outputs))
        self.assertEqual(outputs[-1], "bye.")
        self.assertEqual(self.store.current.messages, [])

    def test_runtime_raises_specific_error_for_whitespace_reply(self):
        runtime = AgentRuntime(SequenceModel(["\t"]), self.store, ContextBuilder())
        with self.assertRaises(EmptyAssistantResponse):
            runtime.run("hello")
        self.assertEqual(self.store.current.messages, [])


if __name__ == "__main__":
    unittest.main()

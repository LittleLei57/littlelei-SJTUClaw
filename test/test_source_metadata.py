"""验证网页与附件来源元数据的保存和引用映射。"""

from pathlib import Path
import tempfile
import unittest

from context_builder import ContextBuilder
from runtime import AgentRuntime
from session_store import SessionStore


class RecordingModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return "ok"


class SourceMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp.name) / "data")
        self.model = RecordingModel()
        self.runtime = AgentRuntime(self.model, self.store, ContextBuilder())

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_records_source_but_strips_metadata_from_model_context(self):
        self.runtime.run("hello", source="feishu")
        stored_user = self.store.current.messages[0]
        self.assertEqual(stored_user["metadata"]["source"], "feishu")
        self.assertNotIn("metadata", self.model.calls[0][-1])

        self.runtime.run("again", source="web")
        self.assertEqual(self.store.current.messages[-2]["metadata"]["source"], "web")
        for message in self.model.calls[-1]:
            self.assertNotIn("metadata", message)


if __name__ == "__main__":
    unittest.main()

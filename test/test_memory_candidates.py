"""验证长期记忆候选的提取、确认和拒绝流程。"""

from pathlib import Path
import tempfile
import unittest

from memory_candidates import MemoryCandidateStore
from memory_store import MemoryStore


class MemoryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memories = MemoryStore(self.root)
        self.candidates = MemoryCandidateStore(self.root, self.memories)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_preference_becomes_reviewable_candidate(self):
        result = self.candidates.propose_from_message("我比较喜欢偏橘红一点的风格", "session_1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_type, "preference")
        self.assertEqual(self.memories.list(), [])

    def test_future_tool_workflow_preference_becomes_candidate(self):
        result = self.candidates.propose_from_message(
            "这样吧，你下次记住，list_dir 和 new_shell 都要用相对于 Workspace 的相对路径，不要错了再修正",
            "session_1",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_type, "preference")
        self.assertIn("list_dir", result[0].content)

    def test_accept_writes_memory_with_provenance(self):
        candidate = self.candidates.propose_from_message("我正在做一个长期的 Agent 项目", "session_2")[0]
        resolved, memory = self.candidates.resolve(candidate.candidate_id, True)
        self.assertEqual(resolved.status, "accepted")
        self.assertIsNotNone(memory)
        self.assertEqual(memory.source, "agent_candidate:session_2")

    def test_reject_and_deduplicate(self):
        candidate = self.candidates.propose_from_message("以后请一直用中文回答", "session_1")[0]
        self.candidates.resolve(candidate.candidate_id, False)
        self.assertEqual(self.memories.list(), [])
        # Rejected candidates may be proposed again if the user explicitly repeats them.
        self.assertEqual(len(self.candidates.propose_from_message("以后请一直用中文回答", "session_1")), 1)

    def test_sensitive_or_temporary_information_is_ignored(self):
        self.assertEqual(self.candidates.propose_from_message("我的 API key 是 sk-secret", "session_1"), [])
        self.assertEqual(self.candidates.propose_from_message("我今天比较喜欢喝咖啡", "session_1"), [])

    def test_existing_memory_is_not_proposed_again(self):
        self.memories.add("我喜欢简洁回答", memory_type="preference")
        self.assertEqual(self.candidates.propose_from_message("我喜欢简洁回答", "session_1"), [])

    def test_conflicting_preference_replaces_old_memory_after_confirmation(self):
        old = self.memories.add("我喜欢橘红色风格", memory_type="preference")
        candidate = self.candidates.propose_from_message(
            "我不喜欢橘红色风格", "session_1"
        )[0]
        self.assertIn(old.memory_id, candidate.conflict_memory_ids)
        _, updated = self.candidates.resolve(candidate.candidate_id, True)
        self.assertEqual(updated.memory_id, old.memory_id)
        self.assertEqual(updated.content, "我不喜欢橘红色风格")
        self.assertEqual(updated.source, "agent_candidate:session_1")
        self.assertEqual(len(self.memories.list()), 1)

    def test_candidate_can_be_edited_before_accepting(self):
        candidate = self.candidates.propose_from_message(
            "我正在做一个长期的 Agent 项目", "session_2"
        )[0]
        edited = self.candidates.update(candidate.candidate_id, "用户长期开发 SJTUClaw")
        self.assertEqual(edited.content, "用户长期开发 SJTUClaw")
        _, memory = self.candidates.resolve(candidate.candidate_id, True)
        self.assertEqual(memory.content, "用户长期开发 SJTUClaw")

    def test_model_can_refine_candidate_without_changing_source(self):
        class Refiner:
            def complete(self, _messages):
                return '{"content":"用户偏好简洁的中文回答","type":"preference","reason":"稳定的回复风格偏好"}'

        store = MemoryCandidateStore(self.root, self.memories, Refiner())
        # Disable the background branch for deterministic inspection, then run
        # the same refiner synchronously.
        store.model = None
        candidate = store.propose_from_message("以后请一直用中文简洁回答", "session_3")[0]
        store.model = Refiner()
        store._refine(candidate.candidate_id)
        refined = store.list("pending")[0]
        self.assertEqual(refined.content, "用户偏好简洁的中文回答")
        self.assertEqual(refined.source_text, "以后请一直用中文简洁回答")
        self.assertTrue(refined.model_assisted)
        self.assertEqual(refined.reason, "稳定的回复风格偏好")


if __name__ == "__main__":
    unittest.main()

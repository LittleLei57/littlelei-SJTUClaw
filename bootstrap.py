"""SJTUClaw 的统一装配入口。

CLI、Gateway、Scheduler 和各消息渠道都调用 ``build_runtime`` 构造同一组
SessionStore、ContextBuilder、LLMClient、ToolRegistry、Compactor、Memory、
Workspace、Approval 与 Skill 服务。入口层因此只负责交互，核心 Agent Loop 和
持久化语义不会因运行方式不同而分叉。
"""

from pathlib import Path
import os

from advanced_tools import AdvancedTools, register_advanced_tools
from attachment_store import AttachmentStore
from approval_store import ApprovalStore
from compaction import Compactor
from context_builder import ContextBuilder
from llm_client import LLMClient
from memory_store import MemoryStore
from memory_candidates import MemoryCandidateStore
from model_capabilities import ModelCapabilityStore
from model_selection import ModelSelectionStore
from download_store import DownloadStore
from runtime import AgentRuntime
from session_store import SessionStore
from shell_manager import ShellManager
from skill_system import SkillRegistry, SkillService, register_skill_tool
from tools import create_read_only_registry
from tavily_search import TavilySearch
from weather_service import OpenMeteoWeather
from wolfram_alpha import WolframAlpha
from github_service import GitHubReader
from scheduler import TaskStore
from scheduler_tools import SchedulerTools, register_scheduler_tools
from workspace import WorkspaceManager
from config import (
    compaction_chunk_tokens,
    compaction_max_tokens,
    tool_timeout_seconds,
    wolfram_app_id,
)


BASE_DIR = Path(__file__).resolve().parent


def build_runtime(data_dir: str | Path | None = None) -> tuple[AgentRuntime, MemoryStore]:
    """按依赖顺序创建共享服务，并返回可立即运行的 AgentRuntime。"""

    configured_data_dir = os.getenv("SJTUCLAW_DATA_DIR", "").strip()
    actual_data_dir = (
        Path(data_dir)
        if data_dir is not None
        else Path(configured_data_dir)
        if configured_data_dir
        else BASE_DIR / "data"
    )
    model_capability_store = ModelCapabilityStore(actual_data_dir)
    model_selection_store = ModelSelectionStore(actual_data_dir)
    client = LLMClient(
        model=model_selection_store.current(),
        capability_store=model_capability_store,
    )
    session_store = SessionStore(actual_data_dir)
    memory_store = MemoryStore(actual_data_dir)
    memory_candidate_store = MemoryCandidateStore(actual_data_dir, memory_store, client)
    workspace_manager = WorkspaceManager(session_store, BASE_DIR)
    download_store = DownloadStore(actual_data_dir)
    shell_manager = ShellManager(workspace_manager)
    task_store = TaskStore(actual_data_dir, session_store)
    scheduler_tools = SchedulerTools(task_store)
    advanced_tools = AdvancedTools(
        session_store, workspace_manager, shell_manager, download_store
    )
    tavily_search = TavilySearch()
    weather_service = OpenMeteoWeather()
    github_reader = GitHubReader()
    configured_wolfram_app_id = wolfram_app_id()
    wolfram_alpha = (
        WolframAlpha(configured_wolfram_app_id)
        if configured_wolfram_app_id
        else None
    )
    tool_registry = create_read_only_registry(
        advanced_tools,
        tavily_search.search,
        weather_service.forecast,
        max_execution_seconds=tool_timeout_seconds(),
        github_read_handler=github_reader.read,
        wolfram_handler=wolfram_alpha.query if wolfram_alpha else None,
    )
    register_advanced_tools(tool_registry, advanced_tools)
    register_scheduler_tools(tool_registry, scheduler_tools)
    skill_registry = SkillRegistry(BASE_DIR / "skills")
    skill_service = SkillService(skill_registry, session_store)
    register_skill_tool(tool_registry, skill_service)
    approval_store = ApprovalStore(actual_data_dir)
    context_builder = ContextBuilder.from_files(
        memory_store,
        BASE_DIR / "prompts" / "system_prompt.md",
        BASE_DIR / "prompts" / "soul.md",
        tool_registry.definitions(),
        skill_registry.index(),
    )
    # Refresh the lightweight skill index immediately after an approved
    # install; users should not have to restart Gateway for the new Skill.
    skill_service.add_refresh_callback(
        lambda: setattr(context_builder, "skill_index", skill_registry.index())
    )
    compactor = Compactor(
        client,
        session_store,
        BASE_DIR / "prompts" / "compact_prompt.md",
        max_tokens=compaction_max_tokens(),
        chunk_tokens=compaction_chunk_tokens(),
    )
    runtime = AgentRuntime(
            client,
            session_store,
            context_builder,
            compactor,
            tool_registry,
            approval_store,
            skill_service,
            strict_tool_protocol=False,
        )
    runtime.workspace_manager = workspace_manager
    runtime.task_store = task_store
    runtime.scheduler_tools = scheduler_tools
    runtime.download_store = download_store
    runtime.shell_manager = shell_manager
    runtime.skill_registry = skill_registry
    runtime.skill_service = skill_service
    runtime.tavily_search = tavily_search
    runtime.weather_service = weather_service
    runtime.github_reader = github_reader
    runtime.wolfram_alpha = wolfram_alpha

    runtime.memory_candidate_store = memory_candidate_store
    runtime.model_capability_store = model_capability_store
    runtime.model_selection_store = model_selection_store
    runtime.attachment_store = AttachmentStore(session_store)
    return runtime, memory_store

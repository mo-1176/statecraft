"""
StateCraft — 記憶を構造として扱うマルチエージェントのフレームワーク。

    from statecraft import Agent, GeminiLLM, Network, SharedMemory, Tool
    from statecraft.agent import Agent          # 直接指定でも同じ

並べているのは利用側が組み立てに使う名前だけ。内部の補助
（プロンプトの断片、差分の適用など）は各モジュールから直接importする。
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .agent import (
    Agent,
    AgentResponse,
    GenerationAborted,
    GenerationConfig,
    ReflexAgent,
    RunInput,
)
from .auditor import AuditorSettings
from .builder import NetworkBuilder
from .config import (
    AgentConfig,
    GenerationOverride,
    KnowledgeConfig,
    NetworkConfig,
    ToolConfig,
)
from .events import (
    AgentState,
    BeforeExecuteEvent,
    BeforeGenerateEvent,
    BeforeMemoryDiffEvent,
    ErrorEvent,
    ExecuteBlockedEvent,
    ExecutedEvent,
    ExecuteEndEvent,
    ExecuteStartEvent,
    FailureKind,
    GeneratedEvent,
    GenerationFailedEvent,
    MemoryDiffEvent,
    MemoryUpdatedEvent,
    NetworkState,
    RespondEndEvent,
    RespondStartEvent,
    StepTime,
)
from .interceptor import (
    ExecuteDecision,
    GenerateDecision,
    Interceptor,
    MemoryDiffDecision,
    RetryDecision,
)
from .invokable import Invokable
from .llm import (
    AzureOpenAILLM,
    BaseLLM,
    ClaudeLLM,
    GeminiLLM,
    OpenAILLM,
    ThoughtLevel,
)
from .mcp import McpTools
from .memory import MemoryEntry, PrivateMemory, SharedMemory, TaskEntry, TaskStatus
from .network import Network
from .prompts import Phase
from .tool_sources import LocalTools, ToolSource
from .tools import SchemaTool, Tool, ToolResult

try:
    # 配布物のメタデータを唯一の出典にする（pyprojectとの二重管理を避ける）。
    __version__ = _version("statecraft")
except PackageNotFoundError:
    # インストールせずにsrc/を直接importした場合。動作はするので落とさない。
    __version__ = "0.0.0+unknown"

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentResponse",
    "AgentState",
    "AuditorSettings",
    "AzureOpenAILLM",
    "BaseLLM",
    "BeforeExecuteEvent",
    "BeforeGenerateEvent",
    "BeforeMemoryDiffEvent",
    "ClaudeLLM",
    "ErrorEvent",
    "ExecuteBlockedEvent",
    "ExecuteDecision",
    "ExecuteEndEvent",
    "ExecuteStartEvent",
    "ExecutedEvent",
    "FailureKind",
    "GeminiLLM",
    "GenerateDecision",
    "GeneratedEvent",
    "GenerationAborted",
    "GenerationConfig",
    "GenerationFailedEvent",
    "GenerationOverride",
    "Interceptor",
    "Invokable",
    "KnowledgeConfig",
    "LocalTools",
    "McpTools",
    "MemoryDiffDecision",
    "MemoryDiffEvent",
    "MemoryEntry",
    "MemoryUpdatedEvent",
    "Network",
    "NetworkBuilder",
    "NetworkConfig",
    "NetworkState",
    "OpenAILLM",
    "Phase",
    "PrivateMemory",
    "ReflexAgent",
    "RespondEndEvent",
    "RespondStartEvent",
    "RetryDecision",
    "RunInput",
    "SchemaTool",
    "SharedMemory",
    "StepTime",
    "TaskEntry",
    "TaskStatus",
    "ThoughtLevel",
    "Tool",
    "ToolConfig",
    "ToolResult",
    "ToolSource",
    "__version__",
]



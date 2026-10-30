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
from .builder import NetworkBuilder
from .config import (
    AgentConfig,
    GenerationOverride,
    KnowledgeConfig,
    NetworkConfig,
    ToolConfig,
)
from .events import (
    BeforeExecuteEvent,
    BeforeGenerateEvent,
    BeforeMemoryDiffEvent,
    ErrorEvent,
    ExecuteBlockedEvent,
    ExecutedEvent,
    ExecuteEndEvent,
    ExecuteStartEvent,
    GeneratedEvent,
    GenerationFailedEvent,
    MemoryDiffEvent,
    MemoryUpdatedEvent,
    RespondEndEvent,
    RespondStartEvent,
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
from .memory import MemoryEntry, PrivateMemory, SharedMemory, TaskEntry, TaskStatus
from .network import Network
from .prompts import Phase
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
    "MemoryDiffDecision",
    "MemoryDiffEvent",
    "MemoryEntry",
    "MemoryUpdatedEvent",
    "Network",
    "NetworkBuilder",
    "NetworkConfig",
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
    "TaskEntry",
    "TaskStatus",
    "ThoughtLevel",
    "Tool",
    "ToolConfig",
    "ToolResult",
    "__version__",
]



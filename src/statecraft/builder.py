"""
設定（NetworkConfig）からNetworkを組み立てる。

ツールは使う側のものなので、出どころ（ToolSource）を並べて渡す。tools.yamlの
名前ごとに、それを持っている出どころがツールを作る（tool_sources.py）。

    net = NetworkBuilder.from_dir(
        "path/to/root",
        tools=[
            LocalTools({"web_search": web_search}),        # 自作のツール
            McpTools("inventory", url="http://...:8000/mcp"),  # MCPサーバーのツール
        ],
    ).build()

LLMは組み込みのプロバイダ（gemini / openai / claude / azure_openai）を最初から
登録してある。APIキーは環境変数から読む。それ以外のプロバイダを使うときや、
繋ぎ方を変えるとき（キーではなくEntra IDで認証する等）は、足すか上書きする。

    LLM_FACTORIES = {**NetworkBuilder.LLM_FACTORIES, "azure_openai": make_azure}

YAMLに関数の場所（モジュール名）を書かせず、出どころに名前で尋ねる形にしているのは、
設定を書き換えられる人が、渡されていない関数を組み立て時に呼べないようにするため。
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, Self, TypeVar

from .agent import Agent, GenerationConfig, ReflexAgent
from .auditor import AuditorSettings
from .config import AgentConfig, GenerationOverride, NetworkConfig, ThoughtLevelName
from .interceptor import Interceptor
from .llm import (
    AzureOpenAILLM,
    BaseLLM,
    ClaudeLLM,
    GeminiLLM,
    OpenAILLM,
    ThoughtLevel,
)
from .network import Network
from .tool_sources import ToolSource
from .tools import Tool

T = TypeVar("T")


class NetworkBuilder:
    # api_keyを渡さなければ、SDKが環境変数（GEMINI_API_KEY / OPENAI_API_KEY /
    # ANTHROPIC_API_KEY）から読む。azure_openaiは接続先も要るので、
    # AzureOpenAILLMがAZURE_OPENAI_ENDPOINTとAZURE_OPENAI_API_KEYから読む。
    # SDKはproviderを使うときに初めて読み込まれる。
    LLM_FACTORIES: ClassVar[dict[str, Callable[[], BaseLLM]]] = {
        "gemini": GeminiLLM,
        "openai": OpenAILLM,
        "claude": ClaudeLLM,
        "azure_openai": AzureOpenAILLM,
    }
    AGENT_CLASSES: ClassVar[dict[str, type[Agent]]] = {
        "agent": Agent,
        "reflex": ReflexAgent,
    }

    # AgentConfigのうち、そのままでは渡せず変換が要る項目
    _CONVERTED: ClassVar[set[str]] = {
        "type",
        "provider",
        "thought_level",
        "phase_overrides",
        "tools",
        "initial_tools",
        "sub_agents",
        "knowledge",
        "initial_knowledge",
        "answer_knowledge",
    }

    def __init__(
        self,
        config: NetworkConfig,
        interceptor: Interceptor | None = None,
        *,
        tools: Sequence[ToolSource] = (),
    ) -> None:
        self.config = config
        self.interceptor = interceptor
        self.tools = list(tools)
        # providerごとに1つ作り、同じproviderのエージェントで共有する
        self._llms: dict[str, BaseLLM] = {}

    @classmethod
    def from_dir(
        cls,
        root: Path | str,
        interceptor: Interceptor | None = None,
        *,
        tools: Sequence[ToolSource] = (),
    ) -> Self:
        """root の下の設定を読み込んだビルダーを返す。toolsはツールの出どころ。"""
        return cls(NetworkConfig.load(root), interceptor, tools=tools)

    def build(self, enable_auditor: AuditorSettings | None = None) -> Network:
        """enable_auditorは、そのままNetworkへ渡す（監査役の設定。省けば監査しない）。"""
        agents = [self._agent(config) for config in self.config.agents]
        return Network(
            agents=agents, interceptor=self.interceptor, enable_auditor=enable_auditor
        )

    def _agent(self, config: AgentConfig) -> Agent:
        # Noneは書かなかった項目。渡さずにAgentの既定値を使う
        kwargs = config.model_dump(exclude_none=True, exclude=self._CONVERTED)
        kwargs["llm"] = self._llm(config.provider)
        kwargs["tools"] = [self._tool(name) for name in config.tools]
        kwargs["initial_tools"] = [self._tool(name) for name in config.initial_tools]
        kwargs["sub_agent_names"] = config.sub_agents
        if config.knowledge:
            kwargs["knowledge"] = self._knowledge(config.knowledge)
        if config.initial_knowledge:
            kwargs["initial_knowledge"] = self._knowledge(config.initial_knowledge)
        if config.answer_knowledge:
            kwargs["answer_knowledge"] = self._knowledge(config.answer_knowledge)
        if config.thought_level is not None:
            kwargs["thought_level"] = self._thought_level(config.thought_level)
        if config.phase_overrides:
            kwargs["phase_overrides"] = {
                phase: self._generation(override)
                for phase, override in config.phase_overrides.items()
            }
        return self.AGENT_CLASSES[config.type](**kwargs)

    def _tool(self, name: str) -> Tool:
        config = self.config.tools_by_name[name]
        owners = [source for source in self.tools if source.has(name)]
        if not owners:
            given = ", ".join(map(repr, self.tools)) or "なし"
            raise ValueError(
                f"tools/{name} を作れる出どころがありません（渡された出どころ: {given}）。"
                "自作のツールなら LocalTools に、MCPのツールなら McpTools のサーバーに"
                "あるか確かめてください"
            )
        if len(owners) > 1:
            raise ValueError(
                f"tools/{name} が、複数の出どころにあります: {', '.join(map(repr, owners))}。"
                "どちらのツールを使うか決められないので、名前を分けてください"
            )
        return owners[0].make(config)

    def _llm(self, provider: str) -> BaseLLM:
        if provider not in self._llms:
            factory = self._lookup(self.LLM_FACTORIES, "LLM_FACTORIES", provider, "provider")
            self._llms[provider] = factory()
        return self._llms[provider]

    def _knowledge(self, names: list[str]) -> str:
        return "\n\n".join(self.config.knowledges_by_name[name].text for name in names)

    def _generation(self, override: GenerationOverride) -> GenerationConfig:
        return GenerationConfig(
            llm=self._llm(override.provider) if override.provider else None,
            model=override.model,
            temperature=override.temperature,
            max_tokens=override.max_tokens,
            thought_level=self._thought_level(override.thought_level),
        )

    @staticmethod
    def _thought_level(name: ThoughtLevelName | None) -> ThoughtLevel | None:
        return ThoughtLevel[name.upper()] if name else None

    @staticmethod
    def _lookup(table: dict[str, T], table_name: str, key: str, where: str) -> T:
        if key not in table:
            registered = ", ".join(table) or "なし"
            raise ValueError(
                f"{where} {key} が {table_name} にありません（登録済み: {registered}）。"
                f"子クラスの {table_name} に登録してください"
            )
        return table[key]

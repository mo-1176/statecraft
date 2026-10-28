"""
設定（NetworkConfig）からNetworkを組み立てる。

ツールの関数は使う側のものなので、typeから実装を作る対応表は子クラスで埋める。

    class MyBuilder(NetworkBuilder):
        # type → settingsを受け取り、ツールの関数を返すもの
        TOOL_FACTORIES = {"web_search": make_search}

    net = MyBuilder.from_dir("path/to/root").build()

LLMは組み込みのプロバイダ（gemini / openai / claude / azure_openai）を最初から
登録してある。APIキーは環境変数から読む。それ以外のプロバイダを使うときや、
繋ぎ方を変えるとき（キーではなくEntra IDで認証する等）は、足すか上書きする。

    LLM_FACTORIES = {**NetworkBuilder.LLM_FACTORIES, "azure_openai": make_azure}

YAMLに関数の場所（モジュール名）を書かせず対応表にしているのは、設定を
書き換えられる人が、登録されていない関数を組み立て時に呼べないようにするため。
"""

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Self, TypeVar

from .agent import Agent, GenerationConfig, ReflexAgent
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
from .tools import SchemaTool

T = TypeVar("T")


class NetworkBuilder:
    TOOL_FACTORIES: ClassVar[dict[str, Callable[..., Callable]]] = {}
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
    }
    _TOOL_TEXTS: ClassVar[set[str]] = {"summary", "usage", "evaluation", "execution_message"}

    def __init__(self, config: NetworkConfig, interceptor: Interceptor | None = None) -> None:
        self.config = config
        self.interceptor = interceptor
        # providerごとに1つ作り、同じproviderのエージェントで共有する
        self._llms: dict[str, BaseLLM] = {}

    @classmethod
    def from_dir(cls, root: Path | str, interceptor: Interceptor | None = None) -> Self:
        """root の下の設定を読み込んだビルダーを返す。"""
        return cls(NetworkConfig.load(root), interceptor)

    def build(self) -> Network:
        agents = [self._agent(config) for config in self.config.agents]
        return Network(agents=agents, interceptor=self.interceptor)

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
        if config.thought_level is not None:
            kwargs["thought_level"] = self._thought_level(config.thought_level)
        if config.phase_overrides:
            kwargs["phase_overrides"] = {
                phase: self._generation(override)
                for phase, override in config.phase_overrides.items()
            }
        return self.AGENT_CLASSES[config.type](**kwargs)

    def _tool(self, name: str) -> SchemaTool:
        config = self.config.tools_by_name[name]
        factory = self._lookup(
            self.TOOL_FACTORIES, "TOOL_FACTORIES", config.type, f"tools/{config.name} の type"
        )
        try:
            func = factory(**config.settings)
        except TypeError as e:
            raise ValueError(
                f"tools/{config.name} の settings が、{config.type} の引数と合いません: {e}"
            ) from e
        texts = config.model_dump(include=self._TOOL_TEXTS, exclude_none=True)
        return SchemaTool(func=func, tool_name=config.name, parameters=config.parameters, **texts)

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

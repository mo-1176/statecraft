"""
NetworkBuilder（設定 → Network の組み立て）の検証。

ここの要点は2つ。
  ・設定の値が、Agent / Tool の同名の引数へ渡る（書かなかった項目はAgentの既定値）
  ・名前から実装を引く対応表は使う側が埋め、引けなければ組み立ての時点でエラーになる
"""

import textwrap
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import ClassVar

import pytest
from conftest import FakeLLM

from statecraft import (
    Agent,
    AgentConfig,
    AzureOpenAILLM,
    BaseLLM,
    ClaudeLLM,
    GeminiLLM,
    GenerationConfig,
    Interceptor,
    Network,
    NetworkBuilder,
    NetworkConfig,
    OpenAILLM,
    Phase,
    ReflexAgent,
    ThoughtLevel,
    ToolConfig,
)


def make_echo(*, prefix: str):
    """settingsを受け取って、ツールの関数を返す（対応表に入れるもの）。"""

    def echo(text: str) -> dict:
        return {"value": prefix + text}

    return echo


class Builder(NetworkBuilder):
    # 使う側が埋める対応表。type / provider の名前 → 実体を作るもの
    TOOL_FACTORIES: ClassVar[dict[str, Callable[..., Callable]]] = {"echo": make_echo}
    LLM_FACTORIES: ClassVar[dict[str, Callable[[], BaseLLM]]] = {
        "fake": FakeLLM,
        "other": FakeLLM,
    }


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "返す文字列"}},
    "required": ["text"],
}


def agent(name: str, **kw) -> AgentConfig:
    return AgentConfig(name=name, summary="s", provider="fake", model="m", **kw)


def echo_tool(name: str, **kw) -> ToolConfig:
    kw.setdefault("settings", {"prefix": "!"})
    return ToolConfig(name=name, type="echo", parameters=ECHO_SCHEMA, **kw)


def build(*agents: AgentConfig, tools=(), interceptor=None) -> Network:
    config = NetworkConfig(agents=list(agents), tools=list(tools))
    return Builder(config, interceptor).build()


# ---- 設定の値が渡る ----
def test_設定どおりにエージェントがそろう():
    net = build(
        agent("front", sub_agents=["scout"]),
        agent(
            "scout",
            tools=["echo"],
            initial_tools=["echo"],
            system_instruction="調べる係",
            # 初期ツールの引数は、委譲の入力から渡る
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        tools=[echo_tool("echo")],
    )

    scout = net["scout"]
    assert [t.name for t in scout.tools] == ["echo"]
    assert [t.name for t in scout.initial_tools] == ["echo"]
    assert scout.system_instruction == "調べる係"
    assert net["front"].sub_agent_names == ["scout"]



def test_思考の継承は設定で有効にできる():
    net = build(agent("a", carry_thoughts=True), agent("b"))

    assert net["a"].carry_thoughts is True
    # 書かなければAgentの既定値（継承しない）
    assert net["b"].carry_thoughts is False

def test_ツール名は設定のnameになる():
    """同じtypeから、settingsだけ変えた別のツールを作れる。"""
    net = build(
        agent("a", tools=["loud", "quiet"]),
        tools=[
            echo_tool("loud", settings={"prefix": "!"}),
            echo_tool("quiet", settings={"prefix": "…"}),
        ],
    )

    loud, quiet = net["a"].tools
    assert [loud.name, quiet.name] == ["loud", "quiet"]
    assert loud.func(text="x") == {"value": "!x"}
    assert quiet.func(text="x") == {"value": "…x"}


def test_ツールの文章とスキーマが設定から入る():
    net = build(
        agent("a", tools=["echo"]),
        tools=[echo_tool("echo", summary="繰り返す", usage="使い方")],
    )

    tool = net["a"].tools[0]
    assert tool.summary == "繰り返す"
    assert tool.usage == "使い方"
    # 書かなかった文章は空（Toolの既定値）
    assert tool.evaluation == ""
    assert tool.to_declaration()["parameters"] == ECHO_SCHEMA


def test_LLMはproviderごとに1つだけ作る():
    net = build(
        agent("front", sub_agents=["scout"]),
        agent("scout"),
        AgentConfig(name="x", summary="s", provider="other", model="m"),
    )

    assert net["front"].llm is net["scout"].llm
    assert net["front"].llm is not net["x"].llm


def test_書かなかった項目はAgentの既定値になる():
    defaults = {f.name: f.default for f in fields(Agent)}

    a = build(agent("a"))["a"]

    assert a.max_steps == defaults["max_steps"]
    assert a.max_tokens == defaults["max_tokens"]
    assert a.usage == defaults["usage"]
    assert a.thought_level is None


def test_reflexとthought_levelが変換される():
    r = build(agent("r", type="reflex", thought_level="low"))["r"]

    assert isinstance(r, ReflexAgent)
    assert r.thought_level is ThoughtLevel.LOW


def test_phase_overridesがGenerationConfigになる():
    a = build(
        agent(
            "a",
            phase_overrides={
                "initial_memory": {"provider": "other", "model": "m2", "thought_level": "low"}
            },
        )
    )["a"]

    override = a.phase_overrides[Phase.INITIAL_MEMORY]
    assert isinstance(override, GenerationConfig)
    assert override.llm is not None
    assert override.llm is not a.llm
    assert override.model == "m2"
    assert override.thought_level is ThoughtLevel.LOW
    # 書かなかった項目は上書きしない（エージェント本体の設定のまま）
    assert override.temperature is None


def test_ナレッジは書いた順につながる(tmp_path: Path):
    def write(relative: str, text: str) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    write(
        "config/agents.yaml",
        """
        - name: a
          summary: s
          provider: fake
          model: m
          knowledge: [b, a]
          initial_knowledge: [a]
        """,
    )
    write(
        "config/knowledges.yaml",
        """
        - {name: a, file: knowledge/a.md}
        - {name: b, file: knowledge/b.md}
        """,
    )
    write("knowledge/a.md", "Aの中身")
    write("knowledge/b.md", "Bの中身")

    a = Builder.from_dir(tmp_path).build()["a"]

    assert a.knowledge == "Bの中身\n\nAの中身"
    assert a.initial_knowledge == "Aの中身"


def test_組み込みのproviderは登録しなくても使える():
    """子クラスがツールの対応表だけを埋めても、LLMの対応表は既定のまま残る。"""

    class ToolsOnly(NetworkBuilder):
        TOOL_FACTORIES: ClassVar[dict[str, Callable[..., Callable]]] = {"echo": make_echo}

    assert ToolsOnly.LLM_FACTORIES == {
        "gemini": GeminiLLM,
        "openai": OpenAILLM,
        "claude": ClaudeLLM,
        "azure_openai": AzureOpenAILLM,
    }


def test_interceptorがNetworkに渡る():
    interceptor = Interceptor()

    net = build(agent("a"), interceptor=interceptor)

    assert net.interceptor is interceptor


# ---- 対応表から引けない ----
def test_知らないtypeはエラーになる():
    with pytest.raises(ValueError, match="no_such_type"):
        build(
            agent("a", tools=["x"]),
            tools=[ToolConfig(name="x", type="no_such_type", parameters={})],
        )


def test_知らないproviderはエラーになる():
    with pytest.raises(ValueError, match="no_such_provider"):
        build(AgentConfig(name="a", summary="s", provider="no_such_provider", model="m"))


def test_settingsが実装の引数と合わないとエラーになる():
    """どのツールの設定が悪いのかを、エラーの文に出す。"""
    with pytest.raises(ValueError, match="loud"):
        build(
            agent("a", tools=["loud"]),
            tools=[echo_tool("loud", settings={"prefx": "!"})],
        )

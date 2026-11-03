"""
NetworkBuilder（設定 → Network の組み立て）の検証。

ここの要点は2つ。
  ・設定の値が、Agent / Tool の同名の引数へ渡る（書かなかった項目はAgentの既定値）
  ・ツールは渡された出どころ（LocalTools / McpTools）が名前で作り、作れなければ組み立ての時点でエラーになる
"""

import textwrap
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
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
    LocalTools,
    McpTools,
    Network,
    NetworkBuilder,
    NetworkConfig,
    OpenAILLM,
    Phase,
    ReflexAgent,
    ThoughtLevel,
    ToolConfig,
)
from statecraft.mcp import McpServer


def echo(text: str) -> dict:
    return {"value": "!" + text}


class Builder(NetworkBuilder):
    # provider の名前 → LLMを作るもの
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
    kw.setdefault("parameters", ECHO_SCHEMA)
    return ToolConfig(name=name, **kw)


def build(*agents: AgentConfig, tools=(), interceptor=None, sources=None) -> Network:
    config = NetworkConfig(agents=list(agents), tools=list(tools))
    if sources is None:
        sources = [LocalTools({t.name: echo for t in tools})]
    return Builder(config, interceptor, tools=sources).build()


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


def test_思考の継承は設定で止められる():
    net = build(agent("a", carry_thoughts=False), agent("b"))

    assert net["a"].carry_thoughts is False
    # 書かなければAgentの既定値（継承する）
    assert net["b"].carry_thoughts is True


def test_ツール名は設定のnameになる():
    """同じ関数でも、tools.yamlの名前ごとに別のツールになる。"""
    net = build(agent("a", tools=["loud", "quiet"]), tools=[echo_tool("loud"), echo_tool("quiet")])

    loud, quiet = net["a"].tools
    assert [loud.name, quiet.name] == ["loud", "quiet"]
    assert loud.func(text="x") == {"value": "!x"}


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
          answer_knowledge: [b]
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
    assert a.answer_knowledge == "Bの中身"


def test_組み込みのproviderは登録しなくても使える():
    assert NetworkBuilder.LLM_FACTORIES == {
        "gemini": GeminiLLM,
        "openai": OpenAILLM,
        "claude": ClaudeLLM,
        "azure_openai": AzureOpenAILLM,
    }


def test_interceptorがNetworkに渡る():
    interceptor = Interceptor()

    net = build(agent("a"), interceptor=interceptor)

    assert net.interceptor is interceptor


# ---- ツールの出どころ ----
def test_どの出どころにも無い名前はエラーになる():
    with pytest.raises(ValueError, match="出どころがありません"):
        build(agent("a", tools=["x"]), tools=[echo_tool("x")], sources=[LocalTools({})])


def test_複数の出どころにある名前はエラーになる():
    with pytest.raises(ValueError, match="複数の出どころ"):
        build(
            agent("a", tools=["x"]),
            tools=[echo_tool("x")],
            sources=[LocalTools({"x": echo}), LocalTools({"x": echo})],
        )


def test_自作のツールはparametersが要る():
    """description はそのままプロンプトになるので、型ヒントからは作らない。"""
    with pytest.raises(ValueError, match="parameters"):
        build(agent("a", tools=["x"]), tools=[ToolConfig(name="x")])


class FakeServer:
    """McpServerの代わり。ツールの一覧と、呼ばれた引数を持つ。"""

    def __init__(self) -> None:
        self.called: list[tuple[str, dict]] = []

    def tools(self) -> dict:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        return {
            "search_items": SimpleNamespace(
                name="search_items",
                description="在庫を検索します。\n品番でも書名でも引けます。",
                input_schema=schema,
            )
        }

    def call(self, tool: str, arguments: dict):
        self.called.append((tool, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="3件")],
            structured_content=None,
            is_error=tool == "broken",
        )


@pytest.fixture
def mcp_server(monkeypatch) -> FakeServer:
    server = FakeServer()
    monkeypatch.setattr(McpServer, "get", classmethod(lambda cls, url, headers: server))
    return server


def test_MCPのツールは名前だけ書けばサーバーから埋まる(mcp_server):
    net = build(
        agent("a", tools=["search_items"]),
        tools=[ToolConfig(name="search_items")],
        sources=[McpTools("inventory", url="http://inventory/mcp")],
    )

    tool = net["a"].tools[0]
    assert tool.summary == "在庫を検索します。"
    assert tool.usage == "品番でも書名でも引けます。"
    assert tool.to_declaration()["parameters"]["properties"] == {"q": {"type": "string"}}
    assert tool.func(q="x") == {"value": "3件"}
    assert mcp_server.called == [("search_items", {"q": "x"})]


def test_MCPのツールも書いた項目はtools_yamlが優先される(mcp_server):
    net = build(
        agent("a", tools=["search_items"]),
        tools=[ToolConfig(name="search_items", summary="在庫を引く", evaluation="件数だけ見る")],
        sources=[McpTools("inventory", url="http://inventory/mcp")],
    )

    tool = net["a"].tools[0]
    assert tool.summary == "在庫を引く"
    assert tool.evaluation == "件数だけ見る"
    # 書かなかったusageはサーバーから
    assert tool.usage == "品番でも書名でも引けます。"


def test_自作とMCPのツールを並べて使える(mcp_server):
    net = build(
        agent("a", tools=["echo", "search_items"]),
        tools=[echo_tool("echo"), ToolConfig(name="search_items")],
        sources=[LocalTools({"echo": echo}), McpTools("inventory", url="http://inventory/mcp")],
    )

    assert [t.name for t in net["a"].tools] == ["echo", "search_items"]


def test_知らないproviderはエラーになる():
    with pytest.raises(ValueError, match="no_such_provider"):
        build(AgentConfig(name="a", summary="s", provider="no_such_provider", model="m"))



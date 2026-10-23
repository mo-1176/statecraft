"""
設定ファイル（config.py）の検証。

ここの要点は2つ。
  ・YAMLとmdから、Agent / Tool へ渡せる値がそろう（文章の置き場所、差し込み口）
  ・書き間違いは、読み込みの時点でエラーになる（静かに読めてしまわない）
"""

import textwrap
from pathlib import Path
from typing import get_args

import pytest

from statecraft import AgentConfig, NetworkConfig, ThoughtLevel, ToolConfig
from statecraft.config import ThoughtLevelName

AGENTS = """
- name: front
  summary: 窓口
  provider: fake
  model: m
  sub_agents: [scout]
- name: scout
  summary: 調査
  provider: fake
  model: m
  tools: [search]
  knowledge: [basics]
"""

TOOLS = """
- name: search
  type: search
  summary: 最大{limit}件まで検索します
  settings: {limit: 3}
  parameters:
    type: object
    properties:
      q:
        type: string
        description: 検索語。最大{limit}件
        pattern: "^[a-z]{2,}$"
    required: [q]
"""

KNOWLEDGES = """
- name: basics
  file: knowledge/basics.md
"""


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write(tmp_path, "config/agents.yaml", AGENTS)
    write(tmp_path, "config/tools.yaml", TOOLS)
    write(tmp_path, "config/knowledges.yaml", KNOWLEDGES)
    write(tmp_path, "prompts/agents/scout/system_instruction.md", "調べる係")
    write(tmp_path, "prompts/tools/search/evaluation.md", "上位{limit}件だけを見る")
    write(tmp_path, "knowledge/basics.md", "基本")
    return tmp_path


def agent(name: str, **kw) -> AgentConfig:
    return AgentConfig(name=name, summary="s", provider="fake", model="m", **kw)


def tool(name: str, **kw) -> ToolConfig:
    return ToolConfig(name=name, type=name, parameters={}, **kw)


# ---- 読み込み ----
def test_rootの下の設定を読み込める(root):
    config = NetworkConfig.load(root)

    assert config.agent_names == {"front", "scout"}
    assert config.tool_names == {"search"}
    assert config.knowledge_names == {"basics"}


def test_mdの文章が項目に入る(root):
    config = NetworkConfig.load(root)

    scout = next(a for a in config.agents if a.name == "scout")
    assert scout.system_instruction == "調べる係"
    # mdが無い項目は書かなかった扱い（Agentの既定値に任せる）
    assert scout.usage is None


def test_同じ項目がYAMLとmdの両方にあるとエラーになる(root):
    """どちらが正か分からなくなるため。"""
    write(root, "config/agents.yaml", AGENTS + "  system_instruction: YAMLにも書いた\n")

    with pytest.raises(ValueError, match="両方"):
        NetworkConfig.load(root)


def test_ナレッジはファイルの中身が入る(root):
    config = NetworkConfig.load(root)

    assert config.knowledges_by_name["basics"].text == "基本"


def test_ナレッジのファイルが無いとエラーになる(root):
    (root / "knowledge/basics.md").unlink()

    with pytest.raises(ValueError, match="basics"):
        NetworkConfig.load(root)


def test_使わない種類のファイルは無くてよい(tmp_path):
    """ツールやナレッジを持たないネットワークに、空のファイルを置かせない。"""
    write(tmp_path, "config/agents.yaml", "- {name: a, summary: s, provider: fake, model: m}")

    config = NetworkConfig.load(tmp_path)

    assert config.agent_names == {"a"}
    assert config.tools == []
    assert config.knowledges == []


def test_agents_yamlが無いとエラーになる(tmp_path):
    with pytest.raises(FileNotFoundError):
        NetworkConfig.load(tmp_path)


def test_中身が空のファイルは空として扱う(root):
    write(root, "config/knowledges.yaml", "")
    write(root, "config/agents.yaml", AGENTS.replace("  knowledge: [basics]\n", ""))

    assert NetworkConfig.load(root).knowledges == []


# ---- 差し込み口 ----
def test_差し込み口がsettingsの値で埋まる(root):
    search = NetworkConfig.load(root).tools_by_name["search"]

    assert search.summary == "最大3件まで検索します"
    # mdから入った文章の差し込み口も埋まる
    assert search.evaluation == "上位3件だけを見る"
    assert search.parameters["properties"]["q"]["description"] == "検索語。最大3件"


def test_スキーマのdescription以外の波かっこは残る(root):
    """patternの {2,} のように、波かっこが差し込み口ではない場所がある。"""
    search = NetworkConfig.load(root).tools_by_name["search"]

    assert search.parameters["properties"]["q"]["pattern"] == "^[a-z]{2,}$"


def test_差し込み口に入れる値が無いとエラーになる():
    broken = tool("x", usage="最大{max_reeds}件")

    with pytest.raises(ValueError, match="max_reeds"):
        broken.fill_placeholders()


# ---- 書き間違い ----
def test_名前の食い違いはまとめて出る():
    """1つずつ出すと、直して動かすたびに次のエラーが出てくる。"""
    with pytest.raises(ValueError) as e:
        NetworkConfig(
            agents=[
                agent("front", sub_agents=["scuot"]),
                agent("scout", tools=["web_serch"], knowledge=["basic"]),
            ],
            tools=[tool("web_search"), tool("web_search")],
        )

    message = str(e.value)
    assert "scuot" in message
    assert "web_serch" in message
    assert "basic" in message
    assert "2つ以上" in message


def test_知らない項目名はエラーになる():
    with pytest.raises(ValueError, match="summery"):
        AgentConfig(name="a", summery="s", provider="fake", model="m")  # type: ignore[call-arg]


def test_ナレッジの中身はYAMLから書けない(root):
    """中身はfileから読むものなので、YAMLに直接書いたら綴り違いと同じ扱いにする。"""
    write(root, "config/knowledges.yaml", KNOWLEDGES + "  text: 直接書いた\n")

    with pytest.raises(ValueError, match="text"):
        NetworkConfig.load(root)


def test_thought_levelに書ける値はThoughtLevelとそろっている():
    """ThoughtLevelに段階を足したら、ここも足す（YAMLに書けない段階ができる）。"""
    assert get_args(ThoughtLevelName) == tuple(m.name.lower() for m in ThoughtLevel)

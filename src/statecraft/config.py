"""
エージェント・ツール・ナレッジの設定を、YAMLとMarkdownから読み込んで検証する。

    <root>/
      config/agents.yaml        エージェントと、使うツール・ナレッジ・エージェントの名前
      config/tools.yaml         ツール
      config/knowledges.yaml    ナレッジの名前とファイル
      prompts/<agents|tools>/<名前>/<項目>.md   長い文章の項目

組み立て（builder.py）と分けているのは、設定の出どころ（YAML、DBなど）を
替えても、組み立てる側を変えずに済むようにするため。

YAMLは書き間違えても読めてしまうので、知らない項目名・名前の食い違い・
同じ文章の二重定義は、読み込みの時点でエラーにする。
"""

from pathlib import Path
from typing import Any, ClassVar, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, PrivateAttr, TypeAdapter, model_validator

from .prompts import Phase

# ThoughtLevelのメンバー名の小文字。組み立てる側でThoughtLevelへ変える。
ThoughtLevelName = Literal["none", "minimal", "low", "medium", "high"]


class ConfigEntry(BaseModel):
    """
    1つのYAMLファイルに並ぶ設定1件の共通部分。

    子クラスは、読むファイル（FILE）、mdを探すフォルダ（KIND）、mdから埋める
    項目（TEXT_FIELDS）、読み込み後の仕上げ（_after_load）を決める。
    """

    model_config = ConfigDict(extra="forbid")

    # mdの置き場所は決め打ちにする。YAMLからパスを指させると打ち間違いが起きる。
    CONFIG_DIR: ClassVar[str] = "config"
    PROMPT_DIR: ClassVar[str] = "prompts"

    FILE: ClassVar[str]
    KIND: ClassVar[str]
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ()
    # Falseなら、ファイルが無くても空として扱う（使わない種類のために空ファイルを置かせない）。
    REQUIRED: ClassVar[bool] = False

    name: str

    @classmethod
    def load_all(cls, root: Path) -> list[Self]:
        path = root / cls.CONFIG_DIR / cls.FILE
        if not path.exists() and not cls.REQUIRED:
            return []
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        # リストごと検証すると、エラーに何件目か（0.usage）が載る。
        items = TypeAdapter(list[cls]).validate_python(raw)  # pyright: ignore[reportInvalidTypeForm]
        for item in items:
            item.fill_text_from_md(root / cls.PROMPT_DIR)
            item._after_load(root)
        return items

    def fill_text_from_md(self, prompt_dir: Path) -> None:
        """<prompt_dir>/<KIND>/<名前>/<項目>.md があれば、その中身を項目へ入れる。"""
        for field in self.TEXT_FIELDS:
            md = prompt_dir / self.KIND / self.name / f"{field}.md"
            if not md.exists():
                continue
            if getattr(self, field) is not None:
                raise ValueError(
                    f"{self.KIND}/{self.name} の {field} が、YAML と {md} の両方にあります。"
                    "どちらか片方にしてください"
                )
            setattr(self, field, md.read_text(encoding="utf-8"))

    def _after_load(self, root: Path) -> None:
        """読み込みの最後に呼ばれる。"""


class ToolConfig(ConfigEntry):
    """
    tools.yamlの1件。

    nameはLLMに見せる名前、typeは実装の種類（NetworkBuilder.TOOL_FACTORIESのキー）。
    分けてあるので、同じ実装からsettingsだけ違うツールを何本でも作れる。

    文章とスキーマのdescriptionにある {名前} は、settingsの同じ名前の値で埋める。
    数字をsettingsの1か所にだけ書けば、説明文と実装の上限が食い違わない。
    波かっこをそのまま書くときは {{ }} にする。
    """

    FILE = "tools.yaml"
    KIND = "tools"
    TEXT_FIELDS = ("usage", "evaluation", "execution_message")

    type: str
    summary: str = ""
    usage: str | None = None
    evaluation: str | None = None
    execution_message: str | None = None
    settings: dict[str, Any] = {}
    parameters: dict[str, Any]

    def _after_load(self, root: Path) -> None:
        # mdの文章にも差し込み口があるので、mdを読んだ後に埋める
        self.fill_placeholders()

    def fill_placeholders(self) -> None:
        try:
            for field in ("summary", *self.TEXT_FIELDS):
                text = getattr(self, field)
                if text is not None:
                    setattr(self, field, self._fill(text))
            self.parameters = self._fill_schema(self.parameters)
        except KeyError as e:
            raise ValueError(
                f"tools/{self.name} の差し込み口 {{{e.args[0]}}} に入れる値が、settings にありません"
            ) from e

    def _fill(self, text: str) -> str:
        return text.format(**self.settings)

    def _fill_schema(self, node: Any) -> Any:
        """descriptionだけを埋める。patternなどは波かっこを文法として使うので触らない。"""
        if isinstance(node, dict):
            return {
                key: self._fill(value)
                if key == "description" and isinstance(value, str)
                else self._fill_schema(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [self._fill_schema(item) for item in node]
        return node


class KnowledgeConfig(ConfigEntry):
    """
    knowledges.yamlの1件。名前とファイルの対応だけを持つ。

    ナレッジは複数のエージェントで共有する（多対多）ので、1対1の文章のように
    置き場所を決め打ちにせず、名前で登録してagents.yamlから指す。
    """

    FILE = "knowledges.yaml"
    KIND = "knowledges"

    # rootからのパス
    file: str
    # 中身はfileから読む。YAMLからは書かせない。
    _text: str = PrivateAttr(default="")

    @property
    def text(self) -> str:
        return self._text

    def _after_load(self, root: Path) -> None:
        path = root / self.file
        if not path.exists():
            raise ValueError(f"knowledges/{self.name} の file {path} がありません")
        self._text = path.read_text(encoding="utf-8")


class GenerationOverride(BaseModel):
    """phase_overridesの1件。書かなかった項目は、エージェント本体の設定のまま。"""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thought_level: ThoughtLevelName | None = None


class AgentConfig(ConfigEntry):
    """
    agents.yamlの1件。項目はAgentの同名の引数にあたる。

    つながり（tools / initial_tools / sub_agents / knowledge / initial_knowledge）は
    名前で書き、使う側であるエージェントにだけ書く。同じ関係を2か所に書くと、
    片方だけ直したときに食い違う。typeはクラスを選ぶだけで、設定の形は変わらない。

    書かなかった項目はNoneのままにし、Agentへ渡さない。既定値をここへ写すと、
    Agent側の既定値を変えたときに食い違う。
    """

    FILE = "agents.yaml"
    KIND = "agents"
    TEXT_FIELDS = ("usage", "evaluation", "execution_message", "system_instruction")
    REQUIRED = True

    summary: str
    type: Literal["agent", "reflex"] = "agent"
    usage: str | None = None
    evaluation: str | None = None
    execution_message: str | None = None
    system_instruction: str | None = None
    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    thought_level: ThoughtLevelName | None = None
    max_steps: int | None = None
    max_memory_retries: int | None = None
    max_stalled_steps: int | None = None
    phase_overrides: dict[Phase, GenerationOverride] = {}
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    tools: list[str] = []
    initial_tools: list[str] = []
    sub_agents: list[str] = []
    knowledge: list[str] = []
    initial_knowledge: list[str] = []


class NetworkConfig(BaseModel):
    """設定の全体。作った時点で、ファイルをまたいだ名前の食い違いを検証する。"""

    agents: list[AgentConfig]
    tools: list[ToolConfig] = []
    knowledges: list[KnowledgeConfig] = []

    @classmethod
    def load(cls, root: Path | str) -> Self:
        """root の下の config/ と prompts/ から読む。"""
        root = Path(root)
        return cls(
            agents=AgentConfig.load_all(root),
            tools=ToolConfig.load_all(root),
            knowledges=KnowledgeConfig.load_all(root),
        )

    @property
    def agent_names(self) -> set[str]:
        return {a.name for a in self.agents}

    @property
    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}

    @property
    def knowledge_names(self) -> set[str]:
        return {k.name for k in self.knowledges}

    @property
    def tools_by_name(self) -> dict[str, ToolConfig]:
        return {t.name: t for t in self.tools}

    @property
    def knowledges_by_name(self) -> dict[str, KnowledgeConfig]:
        return {k.name: k for k in self.knowledges}

    @model_validator(mode="after")
    def check_references(self) -> Self:
        """
        名前の重複と、存在しない名前への参照を検証する。

        1つずつ出すと直すたびに次が出てくるので、全部集めてからまとめて出す。
        """
        errors: list[str] = []
        groups = (
            ("agents.yaml", "エージェント", self.agents),
            ("tools.yaml", "ツール", self.tools),
            ("knowledges.yaml", "ナレッジ", self.knowledges),
        )
        for file_name, label, items in groups:
            for name in self._duplicates([item.name for item in items]):
                errors.append(f"{file_name} に、{name} という名前の{label}が2つ以上あります")

        references = (
            ("tools", self.tool_names, "tools.yaml"),
            ("initial_tools", self.tool_names, "tools.yaml"),
            ("sub_agents", self.agent_names, "agents.yaml"),
            ("knowledge", self.knowledge_names, "knowledges.yaml"),
            ("initial_knowledge", self.knowledge_names, "knowledges.yaml"),
        )
        for agent in self.agents:
            for field, known, file_name in references:
                for name in getattr(agent, field):
                    if name not in known:
                        errors.append(
                            f"{agent.name} の {field} にある {name} は、{file_name} にありません"
                        )

        if errors:
            raise ValueError("\n".join(errors))
        return self

    @staticmethod
    def _duplicates(names: list[str]) -> list[str]:
        """2回以上出てくる名前を、最初に出てきた順で返す。"""
        seen: set[str] = set()
        duplicated: list[str] = []
        for name in names:
            if name in seen and name not in duplicated:
                duplicated.append(name)
            seen.add(name)
        return duplicated

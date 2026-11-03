"""
ツールの出どころ。tools.yamlに書いた名前から、実際のツールを作る。

    NetworkBuilder.from_dir(root, tools=[
        LocalTools({"web_search": web_search}),
        McpTools("inventory", url="http://inventory:8000/mcp"),
    ])

組み立てる側（NetworkBuilder）は、tools.yamlの名前を出どころに順に尋ねるだけで、
そのツールが手元の関数なのか、MCPサーバーの向こうにあるのかを知らない。
出どころを増やす時は、ToolSourceの形（has / make）を満たすクラスを書けばよい。

MCPの出どころ（McpTools）は mcp.py にある。
"""

from collections.abc import Callable
from typing import Protocol

from .config import ToolConfig
from .tools import SchemaTool, Tool


class ToolSource(Protocol):
    """
    ツールの出どころが満たす形（Protocol: このメソッドを持っていれば、継承しなくても
    ToolSourceとして渡せる）。
    """

    def has(self, name: str) -> bool:
        """この名前のツールを作れるか。"""
        ...

    def make(self, config: ToolConfig) -> Tool:
        """tools.yamlの1件から、ツールを作る。hasがTrueの名前でだけ呼ばれる。"""
        ...


def texts(config: ToolConfig) -> dict[str, str]:
    """ToolConfigのうち、書かれた文章の項目だけ。書かなかった項目はToolの既定値に任せる。"""
    return config.model_dump(
        include={"summary", "usage", "evaluation", "execution_message"},
        exclude_none=True,
    )


class LocalTools:
    """
    自作のツール。名前と、ツールの関数の対応表を受け取る。

        LocalTools({"web_search": make_search(TavilySearch())})

    上限などの値は、関数（を作る側）が持つ。tools.yamlに書くのは、LLMに見せる
    文章と引数のスキーマ（parameters）だけ。
    """

    def __init__(self, functions: dict[str, Callable[..., dict]]) -> None:
        self._functions = dict(functions)

    def __repr__(self) -> str:
        return f"LocalTools({sorted(self._functions)})"

    def has(self, name: str) -> bool:
        return name in self._functions

    def make(self, config: ToolConfig) -> Tool:
        # 引数の説明（description）はそのままプロンプトになるので、型ヒントからは
        # 作らずに、tools.yamlへ書かせる。
        if config.parameters is None:
            raise ValueError(
                f"tools/{config.name} に parameters がありません。自作のツールは、"
                "引数のスキーマを tools.yaml に書いてください。"
            )
        return SchemaTool(
            func=self._functions[config.name],
            tool_name=config.name,
            parameters=config.parameters,
            **{"summary": "", **texts(config)},
        )

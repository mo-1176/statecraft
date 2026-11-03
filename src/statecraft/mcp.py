"""
MCPサーバーのツールを、statecraftのツールとして使う（ツールの出どころ McpTools）。

    McpTools("inventory", url="http://inventory:8000/mcp", headers={"Authorization": "Bearer ..."})

サーバーとは Streamable HTTP（URLへのPOST）で話す。ツールの一覧と引数のスキーマは
サーバーから取るので、tools.yaml には名前だけを書けばよい。summary と usage は
サーバーの description から入り、tools.yaml に書けばそちらを使う。結果の読み方
（evaluation）は、自作のツールと同じく prompts/tools/<名前>/evaluation.md に書く。

mcpパッケージは、使う時に初めて読み込む（pip install "statecraft[mcp]"）。
"""

import asyncio
import atexit
import contextlib
import importlib
import json
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any, ClassVar, TypeVar

from .config import ToolConfig
from .tool_sources import texts
from .tools import SchemaTool, Tool

T = TypeVar("T")


def _mcp(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise ImportError(
            'MCPのツールを使うには mcp が要ります。`pip install "statecraft[mcp]"` を'
            "実行してください。"
        ) from e


class McpServer:
    """
    1つのMCPサーバーへの接続。

    mcpパッケージは非同期で書かれているが、statecraftのツールの関数は同期で呼ばれる。
    そこで専用のスレッドでイベントループを回し、同期の側からそこへ仕事を投げて待つ。

    接続とツールの一覧は、最初に使う時に取り、プロセスが終わるまで使い回す。
    Networkはリクエストごとに組み立て直されることが多いので、そのたびに繋ぎ直さない
    よう、接続先（URLとヘッダー）ごとに1つだけ持つ。
    """

    _servers: ClassVar[dict[tuple, "McpServer"]] = {}
    _servers_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def get(cls, url: str, headers: dict[str, str]) -> "McpServer":
        key = (url, tuple(sorted(headers.items())))
        with cls._servers_lock:
            if key not in cls._servers:
                cls._servers[key] = cls(url, headers)
            return cls._servers[key]

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self.client: Any = None
        self._serving: Future[None] | None = None
        self._closing: asyncio.Event | None = None
        self._tools: dict[str, Any] | None = None
        # statecraftはツールを並列に呼ぶので、最初の接続と一覧の取得が重ならないようにする
        self._connect_lock = threading.Lock()
        atexit.register(self.close)

    # 接続（_connect）は、必ず同期の側で済ませてからループへ投げる。ループの上の
    # コルーチンから _connect を呼ぶと、ループが自分自身の完了を待って止まる。
    def tools(self) -> dict[str, Any]:
        """サーバーのツール（mcp.types.Tool）を名前で引ける形で。最初の1回だけ取る。"""
        with self._connect_lock:
            if self._tools is None:
                client = self._connect_locked()
                listed = self._run(self._list_tools(client))
                self._tools = {t.name: t for t in listed}
            return self._tools

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        with self._connect_lock:
            client = self._connect_locked()
        return self._run(client.call_tool(tool, arguments))

    def close(self) -> None:
        if self._closing is not None:
            self.loop.call_soon_threadsafe(self._closing.set)
        if self._serving is not None:
            # 終了処理の失敗で、プロセスの終了を止めない
            with contextlib.suppress(Exception):
                self._serving.result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _connect_locked(self) -> Any:
        # 接続が切れていたら（サーバーのコンテナが再起動した等）、開き直す
        if self.client is None or (self._serving and self._serving.done()):
            ready: Future[None] = Future()
            self._serving = asyncio.run_coroutine_threadsafe(self._serve(ready), self.loop)
            ready.result(timeout=30)
        return self.client

    async def _serve(self, ready: Future[None]) -> None:
        """
        接続を開き、閉じるよう言われるまで持ち続ける。

        開くのと閉じるのを同じタスクで行う必要がある（mcpが内部で使うanyioは、
        別のタスクから閉じるとエラーになる）。そのため、呼び出しごとのタスクとは別に、
        接続を持つためだけのタスクを1本立てる。
        """
        client_module = _mcp("mcp.client")
        http = _mcp("mcp.client.streamable_http")
        self._closing = asyncio.Event()
        try:
            async with (
                http.create_mcp_http_client(headers=self.headers) as http_client,
                client_module.Client(
                    http.streamable_http_client(self.url, http_client=http_client)
                ) as client,
            ):
                self.client = client
                ready.set_result(None)
                await self._closing.wait()
        except Exception as e:
            # 接続の失敗は、待っている同期の側へ渡す
            if not ready.done():
                ready.set_exception(e)
                return
            raise
        finally:
            self.client = None

    @staticmethod
    async def _list_tools(client: Any) -> list[Any]:
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools


class McpTools:
    """
    MCPサーバー1台分のツールの出どころ。

    nameはこの出どころの名前で、エラーの文に出るだけ（tools.yamlには書かない）。
    どのツールがあるかは、サーバーに尋ねる（最初の1回だけ。プロセスの間は使い回す）。
    そのため、このサーバーのツールを tools.yaml に書いた Network を組み立てる時は、
    サーバーに繋がる必要がある。
    """

    def __init__(
        self, name: str, *, url: str, headers: dict[str, str] | None = None
    ) -> None:
        self.name = name
        self._server = McpServer.get(url, dict(headers or {}))

    def __repr__(self) -> str:
        return f"McpTools({self.name!r})"

    def has(self, name: str) -> bool:
        return name in self._server.tools()

    def make(self, config: ToolConfig) -> Tool:
        listed = self._server.tools()[config.name]
        description = (listed.description or listed.name).strip()
        summary, _, rest = description.partition("\n")
        # 書かれた項目はtools.yaml（とmd）を優先し、書かれていない項目をサーバーから埋める
        written = texts(config)
        return SchemaTool(
            func=self._caller(config.name),
            tool_name=config.name,
            parameters=config.parameters or listed.input_schema,
            **{"summary": summary, "usage": rest.strip(), **written},
        )

    def _caller(self, tool: str) -> Callable[..., dict]:
        server = self._server

        def call(**kwargs: Any) -> dict:
            result = server.call(tool, kwargs)
            text = _text_of(result)
            if result.is_error:
                # ツールの側のエラー（引数の誤り、見つからない等）は、LLMが読んで
                # 直せるように結果として返す。例外にすると、statecraftは汎用の失敗の
                # 文しか見せない。接続できない等の失敗は、例外のまま任せる。
                return {"value": f"【ツールがエラーを返した】{text}"}
            return {"value": text}

        call.__name__ = tool
        return call


def _text_of(result: Any) -> str:
    """ツールの結果を、LLMへ渡す文字列にする。文字以外の中身は、形式の名前だけを残す。"""
    parts = []
    for content in result.content:
        if getattr(content, "type", None) == "text":
            parts.append(content.text)
        else:
            parts.append(f"[{getattr(content, 'type', '?')} は表示できない形式]")
    # 文字の結果が無く、構造化された結果だけが返るツールもある
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)

"""
実行の中断（suspend）の合図。

    def present_candidates(query: str) -> dict:
        candidates = search(query)
        suspend({"candidates": candidates})   # ここで依頼全体が止まる

toolの本体と、before_executeのコールバックから呼べる。どちらで呼んだかで、
再開時に渡した答えの扱いが変わる（Network.resume() を参照）。

  tool の本体       … 答えがそのtoolの結果になる。toolは実行し直さない
  before_execute   … 同じ呼び出しを判定し直し、そこで呼んだsuspend()が答えを返す

他のモジュールを読み込まないのは、interceptor.pyとruntime.pyの両方から
読み込むため（どちらかに置くと循環する）。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

# どこで中断したか。再開時の答えの扱いがこれで決まる。
SuspendedAt = Literal["tool", "before_execute"]


class Suspend(BaseException):
    """
    suspend()が投げる合図。

    BaseExceptionなのは、toolの本体やコールバックを包む except Exception
    （フレームワークの境界）に握られずに、実行の外まで届かせるため。
    名前をErrorで終えないのは、誤りではなく意図した中断だから。
    """

    def __init__(self, payload: Any):
        super().__init__("suspend")
        self.payload = payload
        # before_executeの判定の中で投げられたら、interceptorが書き換える。
        self.at: SuspendedAt = "tool"


# 判定し直す呼び出しへ渡す答え。(答えがあるか, 答え)。
# 実行する側（runtime）が、その呼び出しを実行する間だけ入れる。
_answer: ContextVar[tuple[bool, Any]] = ContextVar("statecraft_answer", default=(False, None))


def suspend(payload: Any = None) -> Any:
    """
    依頼全体をここで止める。Network.respond() / resume() は Suspended を返す。

    payloadは、利用者へ見せるもの（候補・確認したい内容など）。Pending.payload
    として届き、保存する状態にも入るので、JSONにできる値にする。

    before_executeのコールバックで呼んだ場合だけ、再開時に答えを返す
    （判定し直す時に、同じ所で呼ばれたsuspend()が答えを受け取る）。
    toolの本体で呼んだ場合は戻らない（答えがそのtoolの結果になる）。
    """
    has, value = _answer.get()
    if has:
        # 1つの答えは1回だけ返す。判定を通った後のtool本体が、もう一度
        # suspend()を呼んだ場合は、新しい中断として扱う。
        _answer.set((False, None))
        return value
    raise Suspend(payload)


@contextmanager
def answering(value: Any) -> Iterator[None]:
    """
    その中で呼ばれた最初のsuspend()が、valueを返すようにする（runtimeが使う）。

    抜ける時に元へ戻す。スレッドプールのスレッドは使い回されるので、
    戻さないと次の仕事のsuspend()まで答えを受け取ってしまう。
    """
    token = _answer.set((True, value))
    try:
        yield
    finally:
        _answer.reset(token)

"""
実行の観測と、実行前の判定。

    interceptor.notice.execute_start(callback)        # 利用側が登録する（受け取るだけ）
    interceptor.guard.before_execute(callback)        # 利用側が登録する（止められる）
    interceptor.recover.generation_failed(callback)   # 利用側が登録する（失敗の後を決める）

    interceptor.notify.execute_start(event)           # フレームワークが通知する
    interceptor.check.before_execute(event)           # フレームワークが判定を仰ぐ

登録の窓口は、コールバックが何を返すかで分けてある。noticeは何も返さず、
guardは止めるかどうかを、recoverは失敗した後にどうするかを返す。窓口が1つだと、
観測のつもりで登録した関数が拒否権を持ち、返し忘れで実行を止めてしまう。

コールバックが受け取るのは、どの窓口でもイベントの型1つ（events.py）。
guard / recoverは判定の型（ExecuteDecision など）で返す。真偽値や文字列で返させない
のは、同じ文字列が判定ごとに「拒否の理由」「モデル名」と別の意味になっていたため。
イベントの一覧と使い方はREADMEを参照。

イベント名は各メソッドが文字列で明示し、_verify_events()がimport時に
名前の一致と窓口の対称性を確かめる（ズレると静かに呼ばれなくなるため）。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .events import (
    BeforeExecuteEvent,
    BeforeGenerateEvent,
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

logger = logging.getLogger(__name__)


# ---- 判定の結果 ----
# guard / recoverのコールバックが返し、フレームワークもこの形で受け取る。
@dataclass
class ExecuteDecision:
    """
    実行前の判定の結果（before_execute）。

    理由を返せるのは、無いと止められた側が原因を探して追加の実行を始めるため。
    返すかどうかは拒否する側が選べる（審査基準を伝えると回避される）。
    """

    allowed: bool
    # 拒否した理由。空文字なら理由を伝えない拒否。LLMへそのまま渡る。
    # 許可した場合は読まない。
    reason: str = ""


@dataclass
class GenerateDecision:
    """生成の前の判定の結果（before_generate）。RetryDecisionと形は同じだが意味が違う。"""

    proceed: bool
    # 生成に使うモデル名。空文字なら元のモデルのまま。
    model: str = ""


@dataclass
class RetryDecision:
    """
    生成が失敗した時の判定の結果（generation_failed）。

    枠を使い切ったモデルは待っても同じエラーが返るため、モデル名を返せる。
    """

    retry: bool
    # 再試行に使うモデル名。空文字なら元のモデルのまま。
    model: str = ""


def _log_wrong_type(event: str, verdict: object, expected: type, fallback: str) -> None:
    """
    判定の型以外が返った時に、何を返すべきだったかをログへ出す。

    True や文字列で返す書き方からの移行で起きやすい。黙って安全側へ倒すだけだと、
    「なぜ全部止まるのか」が追えない。
    """
    logger.error(
        "%sのコールバックが%s以外を返しました（%r）。%sとして扱います。",
        event,
        expected.__name__,
        verdict,
        fallback,
    )


# ---- 登録の窓口（利用側が使う） ----
# 文字列キーの窓口（on("tool_strat", cb)）にしないのは、タイポが静かに何も
# しない状態になるため。メソッドなら補完が効き、誤りが実行前に分かる。
class _NoticeRegistry:
    """
    通知の登録。interceptor.notice.xxx(callback) の形で使う。

    起きたことを受け取るだけで、戻り値は見ない。観測はすべてここへ登録する。
    """

    def __init__(self, interceptor: "Interceptor"):
        self._interceptor = interceptor

    # ---- 応答（respondの境界） ----
    def respond_start(self, callback: Callable[[RespondStartEvent], None]) -> None:
        self._interceptor._register("respond_start", callback)

    def respond_end(self, callback: Callable[[RespondEndEvent], None]) -> None:
        self._interceptor._register("respond_end", callback)

    # ---- 実行（tool / agent 共通） ----
    def execute_start(self, callback: Callable[[ExecuteStartEvent], None]) -> None:
        self._interceptor._register("execute_start", callback)

    def execute_end(self, callback: Callable[[ExecuteEndEvent], None]) -> None:
        self._interceptor._register("execute_end", callback)

    def execute_blocked(self, callback: Callable[[ExecuteBlockedEvent], None]) -> None:
        self._interceptor._register("execute_blocked", callback)

    def executed(self, callback: Callable[[ExecutedEvent], None]) -> None:
        """
        実行1回ごとに、引数と戻り値を構造のまま受け取る。

        呼び出した側から発火するため、ループを経由しない直接のexecute()
        呼び出しでは流れない。
        """
        self._interceptor._register("executed", callback)

    # ---- 生成 ----
    def generated(self, callback: Callable[[GeneratedEvent], None]) -> None:
        """LLMの生成1回ごとに、かかった時間とトークン数を受け取る。"""
        self._interceptor._register("generated", callback)

    # ---- 記憶 ----
    def memory_updated(self, callback: Callable[[MemoryUpdatedEvent], None]) -> None:
        self._interceptor._register("memory_updated", callback)

    def memory_diff(self, callback: Callable[[MemoryDiffEvent], None]) -> None:
        """記憶の差分を1回の生成ごとに受け取る。検証・評価のための経路。"""
        self._interceptor._register("memory_diff", callback)

    # ---- 異常 ----
    def error(self, callback: Callable[[ErrorEvent], None]) -> None:
        self._interceptor._register("error", callback)


class _GuardRegistry:
    """
    止められる判定の登録。interceptor.guard.xxx(callback) の形で使う。

    判定の型以外を返すと止まる（安全側）。観測だけをしたい関数はnoticeへ
    登録する。拒否された呼び出しの引数も、execute_blockedで受け取れる。
    """

    def __init__(self, interceptor: "Interceptor"):
        self._interceptor = interceptor

    def before_execute(
        self, callback: Callable[[BeforeExecuteEvent], ExecuteDecision]
    ) -> None:
        """
        実行してよいかを判定する。

        登録された全てが ExecuteDecision(allowed=True) を返した時だけ実行される。
        それ以外（返し忘れ・型の違う値・例外）はすべて拒否になる。
        """
        self._interceptor._register("before_execute", callback)

    def before_generate(
        self, callback: Callable[[BeforeGenerateEvent], GenerateDecision]
    ) -> None:
        """
        生成を行う前に、行うかどうかを判定する。登録が無ければ生成する。

        用途は走行中の状態を見て決めること（phase_overridesは構築時の静的な
        割り当てなので「10万トークン使ったから落とす」は書けない）。門を生成に
        置くのは、費用の大きいMEMORY_UPDATEがbefore_executeの外にあるため。
        generation_failedと同じく直列化されない。
        """
        self._interceptor._register("before_generate", callback)


class _RecoverRegistry:
    """
    失敗した後にどうするかの判定の登録。interceptor.recover.xxx(callback) の形で使う。

    guardと違い、止めるかどうかではなく次の手（やり直すか、何で）を返す。
    """

    def __init__(self, interceptor: "Interceptor"):
        self._interceptor = interceptor

    def generation_failed(
        self, callback: Callable[[GenerationFailedEvent], RetryDecision]
    ) -> None:
        """
        生成が例外で失敗した時に、再試行するかどうかを判定する。

        登録が無い場合は再試行しない。待つ処理を書けるよう、直列化されない。
        """
        self._interceptor._register("generation_failed", callback)


# ---- 発火の窓口（フレームワークが使う） ----
class _CheckDispatcher:
    """
    判定を仰ぐ。interceptor.check.xxx(event) の形でフレームワークが呼ぶ。

    guardとrecoverに登録されたものが、ここから呼ばれる。
    """

    def __init__(self, interceptor: "Interceptor"):
        self._interceptor = interceptor

    def before_execute(self, event: BeforeExecuteEvent) -> ExecuteDecision:
        """tool / agent の実行前判定。Invokableとして同一に扱うため共通。"""
        return self._interceptor._check("before_execute", event)

    def before_generate(self, event: BeforeGenerateEvent) -> GenerateDecision:
        """生成の前の判定。登録が無ければ生成する。"""
        return self._interceptor._decide_generate("before_generate", event)

    def generation_failed(self, event: GenerationFailedEvent) -> RetryDecision:
        """生成が失敗した時の再試行判定。登録が無ければ再試行しない。"""
        return self._interceptor._decide_retry("generation_failed", event)


class _NotifyDispatcher:
    """
    通知。interceptor.notify.xxx(event) の形でフレームワークが呼ぶ。戻り値は見ない。

    noticeに登録されたものが、ここから呼ばれる。
    """

    def __init__(self, interceptor: "Interceptor"):
        self._interceptor = interceptor

    def respond_start(self, event: RespondStartEvent) -> None:
        self._interceptor._notify("respond_start", event)

    def respond_end(self, event: RespondEndEvent) -> None:
        self._interceptor._notify("respond_end", event)

    def execute_start(self, event: ExecuteStartEvent) -> None:
        self._interceptor._notify("execute_start", event)

    def execute_end(self, event: ExecuteEndEvent) -> None:
        self._interceptor._notify("execute_end", event)

    def execute_blocked(self, event: ExecuteBlockedEvent) -> None:
        self._interceptor._notify("execute_blocked", event)

    def executed(self, event: ExecutedEvent) -> None:
        self._interceptor._notify("executed", event)

    def generated(self, event: GeneratedEvent) -> None:
        self._interceptor._notify("generated", event)

    def memory_updated(self, event: MemoryUpdatedEvent) -> None:
        self._interceptor._notify("memory_updated", event)

    def memory_diff(self, event: MemoryDiffEvent) -> None:
        self._interceptor._notify("memory_diff", event)

    def error(self, event: ErrorEvent) -> None:
        self._interceptor._notify("error", event)


def _verify_events() -> None:
    """
    登録と発火の窓口の整合をimport時に確かめる。メソッドが自分の名前を渡して
    いるか、同じ名前が2つの登録の窓口に無いか、noticeとnotify、guard・recoverと
    checkで名前が揃っているか。

    どれもズレても実行時エラーにはならず「登録したのに呼ばれない」という形で
    静かに壊れるため、ここで落とす。
    """
    faces: dict[str, set[str]] = {}
    for face, cls in (
        ("notice", _NoticeRegistry),
        ("guard", _GuardRegistry),
        ("recover", _RecoverRegistry),
        ("check", _CheckDispatcher),
        ("notify", _NotifyDispatcher),
    ):
        names = set()
        for name, attr in vars(cls).items():
            code = getattr(attr, "__code__", None)
            if name.startswith("_") or code is None:
                continue
            if name not in code.co_consts:
                raise ImportError(
                    f"{cls.__name__}.{name} がイベント名 '{name}' を渡していません。"
                    f"メソッド名と渡す文字列を一致させてください。"
                )
            names.add(name)
        faces[face] = names

    # 同じイベントが2つの登録の窓口にあると、どちらの既定値で判定するかが決まらない。
    registries = ("notice", "guard", "recover")
    for i, a in enumerate(registries):
        for b in registries[i + 1 :]:
            if both := faces[a] & faces[b]:
                raise ImportError(f"{a}と{b}に同じイベント名があります: {sorted(both)}")

    for fire, registered in (
        ("notify", faces["notice"]),
        ("check", faces["guard"] | faces["recover"]),
    ):
        if faces[fire] != registered:
            raise ImportError(
                f"{fire}と登録の窓口でイベント名が揃っていません。"
                f"登録だけあるもの: {sorted(registered - faces[fire])} / "
                f"発火だけあるもの: {sorted(faces[fire] - registered)}"
            )


_verify_events()


class Interceptor:
    """
    イベントの登録・発火を一元管理する。文言は持たず、Tool/Agentが
    組み立てたものを配るだけ。

    並列実行では複数のスレッドから呼ばれる。同じ種別のコールバックは1件ずつに
    直列化してあるが、順序は実行の終了順なので「前の通知の内容」には依存できない。
    通知とbefore_executeは別のロックで、互いを待たない。before_generateと
    generation_failedはロックを取らない（中で待つ処理を書けるようにするため）。
    """

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}
        # 別のロックにするのは、before_executeが人の承認を待つ場合に無関係な実行の
        # 通知まで止めないため。コールバックの中からさらに通知が出るためRLock。
        self._notify_lock = threading.RLock()
        self._check_lock = threading.RLock()
        self.notice = _NoticeRegistry(self)
        self.guard = _GuardRegistry(self)
        self.recover = _RecoverRegistry(self)
        self.check = _CheckDispatcher(self)
        self.notify = _NotifyDispatcher(self)

    @property
    def has_listeners(self) -> bool:
        """コールバックが1つでも登録されているか。Networkが配線時に参照する。"""
        return any(self._listeners.values())

    # ---- 窓口からのみ呼ばれる ----
    def _register(self, event: str, callback: Callable) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def _notify(self, event: str, payload: object) -> None:
        """
        通知を発火する。

        コールバック内の例外は握りつぶす（観測側の不具合で観測対象の実行を
        止めない）。黙って消すと原因が追えないため、ログには残す。
        """
        with self._notify_lock:
            for callback in self._listeners.get(event, []):
                try:
                    callback(payload)
                except Exception:
                    logger.exception(
                        "通知コールバックが例外を送出しました: event=%s", event
                    )

    def _decide_retry(
        self, event: str, payload: GenerationFailedEvent
    ) -> RetryDecision:
        """
        失敗した後にどうするかの判定。登録順に尋ね、RetryDecision(retry=True)を
        返したものがあればそこで決まる。それ以外は次の登録へ回し、例外と
        登録が無い場合は再試行しない。

        直列化しない（待つ処理がコールバックの中に書かれるため、直列化すると
        他の実行が無関係な待ち時間まで待たされる）。
        """
        for callback in self._listeners.get(event, []):
            try:
                verdict = callback(payload)
            except Exception:
                logger.exception(
                    "判定コールバックが例外を送出しました: event=%s", event
                )
                # 再試行として扱うと、コールバックの不具合が無限の再試行になる。
                return RetryDecision(retry=False)
            if not isinstance(verdict, RetryDecision):
                _log_wrong_type(
                    event, verdict, RetryDecision, "再試行しない（次の登録へ回す）"
                )
                continue
            if verdict.retry is True:
                return RetryDecision(retry=True, model=verdict.model or "")
        return RetryDecision(retry=False)

    def _decide_generate(
        self, event: str, payload: BeforeGenerateEvent
    ) -> GenerateDecision:
        """
        生成の前の判定。登録順に尋ね、GenerateDecision(proceed=True)なら次の登録へ
        回す。モデル名が付いていればそこで決まる。中止・型の違う値・例外は
        そこで中止する。

        _decide_retryと形は同じだが既定が逆で、登録が無ければ生成する
        （未登録の利用側が動かなくなるのを避ける）。ロックは取らない。
        """
        for callback in self._listeners.get(event, []):
            try:
                verdict = callback(payload)
            except Exception:
                logger.exception(
                    "判定コールバックが例外を送出しました: event=%s", event
                )
                # 「生成してよい」と解釈すると、予算の門が不具合で開く。
                return GenerateDecision(proceed=False)
            if not isinstance(verdict, GenerateDecision):
                _log_wrong_type(event, verdict, GenerateDecision, "中止")
                return GenerateDecision(proceed=False)
            if verdict.proceed is not True:
                return GenerateDecision(proceed=False)
            if verdict.model:
                return GenerateDecision(proceed=True, model=verdict.model)
        return GenerateDecision(proceed=True)

    def _check(self, event: str, payload: BeforeExecuteEvent) -> ExecuteDecision:
        """
        拒否権のある判定。ExecuteDecision(allowed=True)を返したものだけ許可し、
        返し忘れ・型の違う値・例外はすべて拒否（許可と解釈すると権限確認の
        失敗が通る）。拒否の理由は、返された判定からそのまま持ち帰る。

        判定だけのロックを取るので、人の承認を待つあいだも通知は流れ続ける。
        """
        with self._check_lock:
            for callback in self._listeners.get(event, []):
                try:
                    verdict = callback(payload)
                except Exception:
                    logger.exception(
                        "判定コールバックが例外を送出しました: event=%s", event
                    )
                    # 例外の内容は理由にしない（実装の不具合が外へ出る）。
                    return ExecuteDecision(allowed=False)
                if not isinstance(verdict, ExecuteDecision):
                    _log_wrong_type(event, verdict, ExecuteDecision, "拒否")
                    return ExecuteDecision(allowed=False)
                if verdict.allowed is not True:
                    return ExecuteDecision(allowed=False, reason=verdict.reason or "")
            return ExecuteDecision(allowed=True)

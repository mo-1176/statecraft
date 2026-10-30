"""
Interceptor の検証。

guard / recover は判定を返す窓口なので、既定値の向きが安全側かどうかが全て。
ここが逆を向くと、権限確認の失敗や予算の門が静かに開く。
noticeは受け取るだけの窓口なので、何を返しても実行の可否に関わらないことを確かめる。
"""

import threading

import pytest

from statecraft import (
    BeforeExecuteEvent,
    BeforeGenerateEvent,
    BeforeMemoryDiffEvent,
    ErrorEvent,
    ExecuteDecision,
    GenerateDecision,
    GenerationFailedEvent,
    Interceptor,
    MemoryDiffDecision,
    RespondStartEvent,
    RetryDecision,
)
from statecraft.prompts import Phase


def before_execute(name: str = "t", kwargs: dict | None = None) -> BeforeExecuteEvent:
    return BeforeExecuteEvent(name=name, kwargs=kwargs or {})


def before_generate(model: str = "m") -> BeforeGenerateEvent:
    return BeforeGenerateEvent(
        agent="a", phase=Phase.ANSWER, model=model, attempt=1, step_no=1
    )


def generation_failed(
    model: str = "m", error: Exception | None = None
) -> GenerationFailedEvent:
    return GenerationFailedEvent(
        agent="a",
        phase=Phase.ANSWER,
        model=model,
        attempt=1,
        error=error or RuntimeError("x"),
    )


def before_memory_diff() -> BeforeMemoryDiffEvent:
    return BeforeMemoryDiffEvent(
        agent="a",
        phase=Phase.MEMORY_UPDATE,
        attempt=1,
        review_round=1,
        step_no=1,
        run_id="r",
        conversation="利用者（今回の発言）: x",
        request="x",
        shared_memory="",
        private_memory="",
        rows=[],
        tool_results="",
    )


ALLOW = ExecuteDecision(allowed=True)


# ---- guard.before_execute（既定は拒否） ----
def test_登録が無ければ許可する():
    verdict = Interceptor().check.before_execute(before_execute())

    assert verdict.allowed


def test_許可の判定を返した時だけ許可する():
    i = Interceptor()
    i.guard.before_execute(lambda e: ALLOW)

    assert i.check.before_execute(before_execute()).allowed


def test_Noneを返すと拒否になる():
    """returnを書き忘れた場合。許可と解釈すると権限確認の失敗が通る。"""
    i = Interceptor()
    i.guard.before_execute(lambda e: None)

    assert not i.check.before_execute(before_execute()).allowed


def test_判定の型でなければTrueでも拒否になる():
    """真偽値で返す書き方からの移行で起きる。許可と解釈すると、型の取り違えが門を開ける。"""
    i = Interceptor()
    i.guard.before_execute(lambda e: True)

    assert not i.check.before_execute(before_execute()).allowed


def test_拒否の理由は持ち帰られる():
    i = Interceptor()
    i.guard.before_execute(
        lambda e: ExecuteDecision(allowed=False, reason="館長の承認が要ります")
    )

    verdict = i.check.before_execute(before_execute())

    assert not verdict.allowed
    assert verdict.reason == "館長の承認が要ります"


def test_コールバックの例外は拒否になる():
    i = Interceptor()

    def broken(event):
        raise RuntimeError("判定の実装ミス")

    i.guard.before_execute(broken)
    verdict = i.check.before_execute(before_execute())

    assert not verdict.allowed
    # 例外の内容を理由にしない（実装の不具合が外へ出る）
    assert verdict.reason == ""


def test_全員が許可しなければ拒否になる():
    i = Interceptor()
    i.guard.before_execute(lambda e: ALLOW)
    i.guard.before_execute(lambda e: ExecuteDecision(allowed=False))

    assert not i.check.before_execute(before_execute()).allowed


def test_拒否が決まった後は後続を呼ばない():
    i = Interceptor()
    reached = []
    i.guard.before_execute(lambda e: ExecuteDecision(allowed=False))
    i.guard.before_execute(lambda e: reached.append(1) or ALLOW)

    i.check.before_execute(before_execute())

    assert reached == []


def test_判定には名前と引数がイベントで渡る():
    seen = []
    i = Interceptor()

    def gate(event):
        seen.append(event)
        return ALLOW

    i.guard.before_execute(gate)
    i.check.before_execute(before_execute("issue_reservation", {"title": "深夜特急"}))

    assert seen[0].name == "issue_reservation"
    assert seen[0].kwargs == {"title": "深夜特急"}


# ---- guard.before_generate（既定は生成する） ----
def test_登録が無ければ生成する():
    """未登録の利用側が動かなくなるのを避けるため、こちらの既定は通す。"""
    decision = Interceptor().check.before_generate(before_generate())

    assert decision.proceed


def test_中止の判定を返すと生成を中止する():
    i = Interceptor()
    i.guard.before_generate(lambda e: GenerateDecision(proceed=False))

    assert not i.check.before_generate(before_generate()).proceed


def test_モデル名を付けるとそのモデルへ差し替わる():
    i = Interceptor()
    i.guard.before_generate(
        lambda e: GenerateDecision(proceed=True, model="gemini-3.1-flash-lite")
    )

    decision = i.check.before_generate(before_generate("pro"))

    assert decision.proceed
    assert decision.model == "gemini-3.1-flash-lite"


def test_モデル名の無い許可は次の判定へ回る():
    """予算の門と時間帯の門を別々に登録した時、前の門が通しても後ろの門が止められる。"""
    i = Interceptor()
    i.guard.before_generate(lambda e: GenerateDecision(proceed=True))
    i.guard.before_generate(lambda e: GenerateDecision(proceed=False))

    assert not i.check.before_generate(before_generate()).proceed


def test_生成前の判定が型でなければ中止する():
    i = Interceptor()
    i.guard.before_generate(lambda e: True)

    assert not i.check.before_generate(before_generate()).proceed


def test_生成前の判定で例外が出たら中止する():
    """「生成してよい」と解釈すると、予算の門が不具合で開いてしまう。"""
    i = Interceptor()

    def broken(event):
        raise RuntimeError("x")

    i.guard.before_generate(broken)

    assert not i.check.before_generate(before_generate()).proceed


# ---- guard.before_memory_diff（既定は当てる。壊れた監査は飛ばす） ----
def test_監査の登録が無ければ当てる():
    assert Interceptor().check.before_memory_diff(before_memory_diff()).approved


def test_監査の差し戻しは理由ごと持ち帰られる():
    i = Interceptor()
    i.guard.before_memory_diff(
        lambda e: MemoryDiffDecision(approved=False, reason="依頼から外れている")
    )

    decision = i.check.before_memory_diff(before_memory_diff())

    assert not decision.approved
    assert decision.reason == "依頼から外れている"


def test_監査が1つでも差し戻せば差し戻す():
    i = Interceptor()
    i.guard.before_memory_diff(lambda e: MemoryDiffDecision(approved=True))
    i.guard.before_memory_diff(lambda e: MemoryDiffDecision(approved=False))

    assert not i.check.before_memory_diff(before_memory_diff()).approved


def test_監査の例外と型の違う値は飛ばして通す():
    """
    差し戻しの上限は監査する側が持つ。ここで差し戻しに倒すと、壊れた監査が
    同じ更新を作り直させ続ける（before_executeの「拒否に倒す」とは向きが逆）。
    """
    i = Interceptor()

    def broken(event):
        raise RuntimeError("監査の実装ミス")

    i.guard.before_memory_diff(broken)
    i.guard.before_memory_diff(lambda e: False)

    assert i.check.before_memory_diff(before_memory_diff()).approved


# ---- recover.generation_failed（既定は再試行しない） ----
def test_登録が無ければ再試行しない():
    """何回試すか・どれだけ待つかは運用の方針なので、フレームワークは決めない。"""
    decision = Interceptor().check.generation_failed(generation_failed())

    assert not decision.retry


def test_再試行の判定で同じモデルのまま再試行する():
    i = Interceptor()
    i.recover.generation_failed(lambda e: RetryDecision(retry=True))

    decision = i.check.generation_failed(generation_failed())

    assert decision.retry
    assert decision.model == ""


def test_モデル名を付けると別モデルでの再試行になる():
    """枠を使い切ったモデルは待っても同じエラーが返るため。"""
    i = Interceptor()
    i.recover.generation_failed(lambda e: RetryDecision(retry=True, model="flash-lite"))

    decision = i.check.generation_failed(generation_failed("pro"))

    assert decision.retry
    assert decision.model == "flash-lite"


def test_再試行しない判定は次の登録へ回る():
    """待って直る失敗と、モデルを変えれば直る失敗を、別々の関数で扱えるようにするため。"""
    i = Interceptor()
    i.recover.generation_failed(lambda e: RetryDecision(retry=False))
    i.recover.generation_failed(lambda e: RetryDecision(retry=True, model="flash-lite"))

    decision = i.check.generation_failed(generation_failed())

    assert decision.retry
    assert decision.model == "flash-lite"


def test_再試行の判定が型でなければ再試行しない():
    i = Interceptor()
    i.recover.generation_failed(lambda e: True)

    assert not i.check.generation_failed(generation_failed()).retry


def test_再試行の判定で例外が出たら諦める():
    """再試行として扱うと、コールバックの不具合が無限の再試行になる。"""
    i = Interceptor()

    def broken(event):
        raise RuntimeError("x")

    i.recover.generation_failed(broken)

    assert not i.check.generation_failed(generation_failed()).retry


def test_再試行の判定には元の例外が渡る():
    seen = []
    i = Interceptor()

    def decide(event):
        seen.append(event.error)
        return RetryDecision(retry=False)

    i.recover.generation_failed(decide)
    original = RuntimeError("429 Too Many Requests")
    i.check.generation_failed(generation_failed(error=original))

    assert seen[0] is original


# ---- notice（観測側の不具合で実行を止めない） ----
def test_通知は登録された全員へ届く():
    seen = []
    i = Interceptor()
    i.notice.error(seen.append)
    i.notice.error(seen.append)

    event = ErrorEvent(source="librarian", message="何かが起きた")
    i.notify.error(event)

    assert seen == [event, event]


def test_通知コールバックの例外は握りつぶす():
    """観測側の不具合で観測対象の実行を止めない。"""
    reached = []
    i = Interceptor()

    def broken(event):
        raise RuntimeError("観測側のバグ")

    i.notice.error(broken)
    i.notice.error(reached.append)

    event = ErrorEvent(source="librarian", message="x")
    i.notify.error(event)  # 例外が外へ出ないこと

    # 前が落ちても後続は呼ばれる
    assert reached == [event]


def test_通知へ登録した関数は実行の可否に関わらない():
    """
    窓口を分けた理由そのもの。観測のつもりの関数が何を返しても（返し忘れても）、
    実行は止まらない。
    """
    i = Interceptor()
    i.notice.execute_blocked(lambda e: False)
    i.notice.execute_start(lambda e: None)

    assert i.check.before_execute(before_execute()).allowed


def test_登録が無い通知は何もしない():
    # 例外が出ないこと
    Interceptor().notify.respond_start(RespondStartEvent(agent="a", message="x"))


def test_has_listenersは登録の有無を返す():
    """Networkが「個別に登録済みなら尊重する」判定に使う。"""
    i = Interceptor()
    assert not i.has_listeners

    i.notice.error(lambda e: None)
    assert i.has_listeners


# ---- 直列化 ----
def test_同じ種別の通知は1件ずつに直列化される():
    """並列実行で複数スレッドから呼ばれるため。"""
    i = Interceptor()
    overlaps = []
    active = []
    lock = threading.Lock()

    def slow(event):
        with lock:
            active.append(1)
            if len(active) > 1:
                overlaps.append(1)
        threading.Event().wait(0.01)
        with lock:
            active.pop()

    i.notice.error(slow)
    threads = [
        threading.Thread(
            target=i.notify.error, args=(ErrorEvent(source="a", message=f"m{n}"),)
        )
        for n in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == []


# ---- イベント名の対称性（import時に検証される） ----
def test_登録と発火のイベント名が揃っている():
    """
    ズレても実行時エラーにはならず「登録したのに呼ばれない」形で静かに壊れる。
    _verify_events()がimport時に落とすので、importできている時点で揃っている。
    """
    from statecraft.interceptor import _verify_events

    _verify_events()  # 例外が出ないこと


def test_同じイベント名が2つの登録の窓口にあると落とす(monkeypatch):
    """どちらの既定値（拒否か、受け取るだけか）で扱うかが決まらなくなるため。"""
    from statecraft import interceptor as module

    def error(self, callback):
        self._interceptor._register("error", callback)

    monkeypatch.setattr(module._GuardRegistry, "error", error, raising=False)

    with pytest.raises(ImportError):
        module._verify_events()

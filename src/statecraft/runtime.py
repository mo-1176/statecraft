"""
1回の依頼の中で、どのエージェントの起動がどこまで進んでいるかを持ち、進める。

エージェントは互いを関数として呼ばない。呼び出しの要求（バッチ）を返して
いったん手を離し、委譲先の起動は「起動の表」に子の行として足される。
親が誰の結果を待っているかは、バッチの何番目の枠かという1行のデータになる。

    run_id   agent       親（run_id, 枠）   状態
    a1b2…    front       -                 待ち（枠0: 子の結果待ち）
    c3d4…    librarian   (a1b2…, 0)        待ち（枠1: 中断中）

関数の呼び出しで親子を表すと、親が何周目で、どの呼び出しの結果を待っているかが
コールスタックにしか残らず、中断しても保存できない。表にしておけば、中断は
「進められる行が無くなったら止まる」だけになり、保存と復元もこの表と記憶を
書き出す／読み込むだけで済む。普通の実行と再開は、同じ表を回す同じ経路を通る。

並列の粒度は表にする前と同じ。

  ・同じバッチの呼び出しは並列に実行する
  ・同じエージェントの起動は1件ずつ（2件目は、1件目が終わるまで表で待つ）

起動の途中の状態（実行履歴・周回数など）はAgentの属性に持ったままで、
同時に1つの起動しか属性を使わないことを、2つ目の約束で保証している。
"""

import contextlib
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from .invokable import Invokable
from .llm import FunctionCall
from .state import decode, dump_result, encode, load_result
from .suspension import Suspend, SuspendedAt, answering
from .tools import ToolResult

if TYPE_CHECKING:
    from .agent import Agent

# 同時に動かす仕事の上限。仕事は互いを待たない（親は子を待つ間、スレッドを
# 持たない）ので、上限に達しても止まりはせず、順番を待つだけになる。
_MAX_WORKERS = 32

# バッチがどこで出されたか。結果を受け取った後の続きが変わる。
#   initial … ループに入る前の初期tool
#   loop    … ループの各周の呼び出し
Stage = Literal["initial", "loop"]


# ---- エージェントと表の間で受け渡すもの ----
@dataclass
class _PlannedCall:
    """
    実行の1件分。「実行するか」を決めた後、「実行して反映する」側へ渡す器。

    decidedが入っているものは実行しない。結果を先に決めてあり、そのまま
    履歴とイベントへ乗る（実行しなかった事実も観測できる）。
    """

    call: FunctionCall
    # 呼び出す相手。解決できなかった場合と、実行しないと決めた場合はNone。
    target: Invokable | None = None
    # 実行せずに確定させた結果。重複拒否・名前の解決失敗・引数不足。
    decided: ToolResult | None = None
    # 重複として拒否したもの。結果を既に持っている扱いなので、次のステップでも
    # 「直前に成功した」側へ引き継ぐ（落とすと拒否と実行が交互に繰り返される）。
    duplicate: bool = False
    # 相手がエージェントか。エージェントは関数として呼ばず、表に子の起動を足す。
    delegate: bool = False


@dataclass
class _Batch:
    """起動がここで手を離す。planを実行して、結果を持って戻ってきてほしい。"""

    stage: Stage
    plan: list[_PlannedCall]


@dataclass
class _Finished:
    """起動が回答を出し終えた。"""

    text: str
    steps: int


# ---- 中断の公開の形 ----
@dataclass
class Pending:
    """中断している地点1つ。Suspended.pending と Network.pending で受け取る。"""

    # resume(answers={point: 答え}) のキー。中身に意味は無い（解釈しない）。
    point: str
    caller: str  # 呼んだエージェント
    name: str  # 止まったtool / agent
    kwargs: dict  # 渡された引数
    # どこで止まったか。答えの扱いが変わる。
    #   tool           … 答えが、そのtoolの結果になる
    #   before_execute … 同じ呼び出しを判定し直し、そこのsuspend()が答えを返す
    at: SuspendedAt
    payload: Any  # suspend()に渡された値。利用者へ見せるもの


@dataclass
class Suspended:
    """
    Network.respond() / resume() が、依頼を最後まで進められずに止まった時の戻り値。

    状態はNetworkが持っている。プロセスをまたぐなら save_state() で取り出して
    保存し、load_state() で戻してから resume() する。
    """

    pending: list[Pending]


# ---- 起動の表 ----
@dataclass
class _Slot:
    """バッチの1枠。呼び出し1件の進み具合。"""

    planned: _PlannedCall
    # pending   … まだ実行していない（再開で実行し直すものもここへ戻る）
    # running   … 実行中
    # child     … 委譲先の起動の結果を待っている
    # suspended … 中断した
    # done      … 結果がある
    state: str = "pending"
    result: ToolResult | None = None
    child: str = ""  # 委譲先の起動のrun_id
    at: SuspendedAt = "tool"
    payload: Any = None
    # 判定し直す時に、suspend()へ返す答え。(答えがあるか, 答え)
    answer: tuple[bool, Any] = (False, None)


@dataclass
class _Run:
    """起動1件（respond / execute 1回）。"""

    run_id: str
    agent: "Agent"
    kind: Literal["respond", "execute"]
    # respondなら message / history / blobs、executeなら委譲された引数
    request: dict
    # 委譲された起動なら、親の (run_id, 枠の番号)
    parent: tuple[str, int] | None = None
    # queued  … まだ始めていない（同じエージェントの別の起動が終わるのを待つ）
    # busy    … このエージェントのコードが走っている
    # waiting … バッチの結果を待っている
    state: str = "queued"
    stage: Stage = "loop"
    slots: list[_Slot] = field(default_factory=list)
    # 実行にかかった時間の積み上げ。中断していた時間は含めない。
    elapsed: float = 0.0
    resumed: float | None = None
    # respondの起動を始めた時点の、そのエージェントのトークン累計
    tokens_before: tuple[int, int] = (0, 0)


class Runtime:
    """
    起動の表を回す。Networkが1依頼につき1つ持ち、単体で動かすAgentも
    respond() / execute() の中で1つ作る。

    表を触るのは、drive()を呼んだスレッドだけ。スレッドプールの仕事は
    エージェントのコードとtoolを実行して値を返すだけで、表には書かない。
    """

    def __init__(self) -> None:
        self.runs: dict[str, _Run] = {}  # 足した順（＝始める順）
        self.root = ""
        self.finished = False
        # 根の起動の結果。respondならAgentResponse、executeならToolResult
        self.outcome: Any = None
        self._submit: Callable[[Callable[[], Any], Callable[[Future], None]], None]

    # ---- 入口 ----
    def begin_respond(
        self, agent: "Agent", *, message: str, history: list[dict], blobs: list[dict]
    ) -> None:
        request = {"message": message, "history": list(history), "blobs": list(blobs)}
        self.root = self._add(agent, "respond", request, None).run_id

    def begin_execute(self, agent: "Agent", kwargs: dict) -> None:
        self.root = self._add(agent, "execute", dict(kwargs), None).run_id

    def drive(self) -> Any:
        """
        進められる限り進める。根の起動が終われば結果を、どこかで中断して
        進められる行が無くなれば Suspended を返す。
        """
        now = time.perf_counter()
        for run in self.runs.values():
            run.resumed = now

        with ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="statecraft"
        ) as pool:
            jobs: dict[Future, Callable[[Future], None]] = {}

            def submit(work: Callable[[], Any], done: Callable[[Future], None]) -> None:
                jobs[pool.submit(work)] = done

            self._submit = submit
            self._dispatch()
            while jobs:
                finished, _ = wait(jobs, return_when=FIRST_COMPLETED)
                for future in finished:
                    jobs.pop(future)(future)
                self._dispatch()

        if self.finished:
            return self.outcome

        pending = self.pending()
        if not pending:
            # 中断が無いのに進めないのは、表の回し方の誤り。
            raise RuntimeError("進められる起動が無いまま、依頼が終わっていません。")
        now = time.perf_counter()
        for run in self.runs.values():
            run.elapsed += now - (run.resumed or now)
            run.resumed = None
        return Suspended(pending=pending)

    def pending(self) -> list[Pending]:
        return [
            Pending(
                point=f"{run.run_id}/{index}",
                caller=run.agent.name,
                name=slot.planned.call.name,
                kwargs=dict(slot.planned.call.args),
                at=slot.at,
                payload=slot.payload,
            )
            for run in self.runs.values()
            for index, slot in enumerate(run.slots)
            if slot.state == "suspended"
        ]

    def answer(self, answers: dict[str, Any]) -> None:
        """
        中断している地点へ答えを渡す。答えなかった地点は、実行し直す。

          tool で止まった           … 答えがそのまま結果になる（ToolResultならそのもの）
          before_execute で止まった … 判定し直し、そこのsuspend()が答えを返す
        """
        points = {p.point for p in self.pending()}
        if unknown := set(answers) - points:
            raise ValueError(
                f"中断していない地点への答えです: {sorted(unknown)}"
                f"（中断している地点: {sorted(points)}）"
            )
        for run in self.runs.values():
            for index, slot in enumerate(run.slots):
                if slot.state != "suspended":
                    continue
                point = f"{run.run_id}/{index}"
                slot.payload = None
                if point not in answers:
                    slot.state = "pending"
                elif slot.at == "tool":
                    value = answers[point]
                    slot.result = (
                        value if isinstance(value, ToolResult) else ToolResult(value=value)
                    )
                    slot.state = "done"
                else:
                    slot.answer = (True, answers[point])
                    slot.state = "pending"

    # ---- 表を回す ----
    def _add(
        self,
        agent: "Agent",
        kind: Literal["respond", "execute"],
        request: dict,
        parent: tuple[str, int] | None,
    ) -> _Run:
        run = _Run(
            run_id=uuid4().hex[:12],
            agent=agent,
            kind=kind,
            request=request,
            parent=parent,
            resumed=time.perf_counter(),
        )
        self.runs[run.run_id] = run
        return run

    def _occupied(self, agent: "Agent") -> bool:
        return any(
            r.agent is agent and r.state in ("busy", "waiting") for r in self.runs.values()
        )

    def _dispatch(self) -> None:
        """進められる行を、全部進め始める。"""
        for run in list(self.runs.values()):
            if run.state == "queued":
                if not self._occupied(run.agent):
                    self._start(run)
            elif run.state == "waiting":
                for index, slot in enumerate(run.slots):
                    if slot.state == "pending":
                        self._execute(run, index)
                if all(slot.state == "done" for slot in run.slots):
                    self._continue(run)

    def _start(self, run: _Run) -> None:
        run.state = "busy"
        agent, request = run.agent, run.request
        if run.kind == "respond":
            run.tokens_before = (agent.total_input_tokens, agent.total_output_tokens)
            self._advance(
                run,
                lambda: agent._start_respond(
                    run.run_id,
                    message=request["message"],
                    history=request["history"],
                    blobs=request["blobs"],
                ),
            )
        else:
            self._advance(run, lambda: agent._start_execute(run.run_id, request))

    def _continue(self, run: _Run) -> None:
        """バッチの結果が揃った起動を、その続きから進める。"""
        run.state = "busy"
        stage = run.stage
        plan = [slot.planned for slot in run.slots]
        results = {i: slot.result for i, slot in enumerate(run.slots)}
        children = {i: slot.child for i, slot in enumerate(run.slots) if slot.child}
        run.slots = []
        self._advance(
            run, lambda: run.agent._continue_after_batch(stage, plan, results, children)
        )

    def _advance(self, run: _Run, work: Callable[[], "_Batch | _Finished"]) -> None:
        """エージェントのコードを、次に手を離すところ（バッチか回答）まで走らせる。"""

        def done(future: Future) -> None:
            try:
                outcome = future.result()
            except Suspend:
                raise RuntimeError(
                    "suspend()は、toolの本体とbefore_executeのコールバックからだけ呼べます"
                    "（記憶の更新や生成の途中では、再開できる場所が無いため）。"
                ) from None
            except Exception as e:
                if run.kind != "execute":
                    raise
                # 委譲された起動の失敗は、親へ失敗の結果として返す（Invokableの契約）。
                self._finish(run, run.agent._fail_execute(e))
                return

            if isinstance(outcome, _Batch):
                run.stage = outcome.stage
                run.slots = [_Slot(planned=p) for p in outcome.plan]
                for slot in run.slots:
                    if slot.planned.decided is not None:
                        slot.result = slot.planned.decided
                        slot.state = "done"
                run.state = "waiting"
            elif run.kind == "respond":
                self._finish(
                    run,
                    run.agent._finish_respond(
                        outcome.text, outcome.steps, run.tokens_before
                    ),
                )
            else:
                self._finish(run, run.agent._finish_execute(outcome.text))

        self._submit(work, done)

    def _finish(self, run: _Run, result: Any) -> None:
        now = time.perf_counter()
        run.elapsed += now - (run.resumed or now)
        del self.runs[run.run_id]
        if run.parent is None:
            self.finished = True
            self.outcome = result
            return
        result.execution_time = round(run.elapsed, 3)
        parent_id, index = run.parent
        slot = self.runs[parent_id].slots[index]
        slot.result = result
        slot.state = "done"

    def _execute(self, run: _Run, index: int) -> None:
        """
        枠1つを実行し始める。toolは実行し、エージェントは判定を通ったら
        子の起動として表に足す（中身は、表を回す側が進める）。
        """
        slot = run.slots[index]
        slot.state = "running"
        call, target = slot.planned.call, slot.planned.target
        has_answer, answer = slot.answer
        slot.answer = (False, None)
        interceptor = run.agent.interceptor

        def work() -> Any:
            assert target is not None  # 実行しない枠（decided）はここへ来ない
            try:
                with answering(answer) if has_answer else contextlib.nullcontext():
                    if slot.planned.delegate:
                        return target._open_execute(call.args, interceptor)  # type: ignore[attr-defined]
                    return target.execute(kwargs=call.args, interceptor=interceptor)
            except Suspend as s:
                return s

        def done(future: Future) -> None:
            try:
                value = future.result()
            except Exception as e:  # noqa: BLE001
                # Invokableは例外を投げない契約だが、1件の失敗で依頼全体を止めない。
                run.agent._notify_error(f"{call.name}の実行が例外で終了しました: {e}")
                value = ToolResult(error=f"実行中にエラーが発生しました: {e}")

            if isinstance(value, Suspend):
                slot.state = "suspended"
                slot.at = value.at
                slot.payload = value.payload
            elif isinstance(value, ToolResult):
                slot.result = value
                slot.state = "done"
            else:
                # 判定を通った委譲。子の起動を表に足して、その結果を待つ。
                child = self._add(target, "execute", dict(call.args), (run.run_id, index))  # type: ignore[arg-type]
                slot.child = child.run_id
                slot.state = "child"

        self._submit(work, done)

    # ---- 保存と復元 ----
    def dump(self) -> dict:
        def slot_state(run: _Run, slot: _Slot) -> dict:
            where = f"{run.agent.name}の呼び出し '{slot.planned.call.name}'"
            return {
                "name": slot.planned.call.name,
                "args": encode(slot.planned.call.args, f"{where}の引数"),
                "decided": dump_result(slot.planned.decided, where),
                "duplicate": slot.planned.duplicate,
                "state": slot.state,
                "result": dump_result(slot.result, f"{where}の結果"),
                "child": slot.child,
                "at": slot.at,
                "payload": encode(slot.payload, f"{where}がsuspend()に渡した値"),
                "answer": [slot.answer[0], encode(slot.answer[1], f"{where}への答え")],
            }

        return {
            "root": self.root,
            "runs": [
                {
                    "run_id": run.run_id,
                    "agent": run.agent.name,
                    "kind": run.kind,
                    "request": encode(run.request, f"{run.agent.name}への依頼"),
                    "parent": list(run.parent) if run.parent else None,
                    "state": run.state,
                    "stage": run.stage,
                    "elapsed": run.elapsed,
                    "tokens_before": list(run.tokens_before),
                    "slots": [slot_state(run, s) for s in run.slots],
                }
                for run in self.runs.values()
            ],
        }

    @classmethod
    def load(cls, data: dict, agents: dict[str, "Agent"]) -> "Runtime":
        runtime = cls()
        runtime.root = data["root"]
        for raw in data["runs"]:
            agent = agents.get(raw["agent"])
            if agent is None:
                raise ValueError(
                    f"保存した状態の起動が、このNetworkに無いエージェント"
                    f" '{raw['agent']}' を指しています。"
                )
            run = _Run(
                run_id=raw["run_id"],
                agent=agent,
                kind=raw["kind"],
                request=decode(raw["request"]),
                parent=tuple(raw["parent"]) if raw["parent"] else None,  # type: ignore[arg-type]
                state=raw["state"],
                stage=raw["stage"],
                elapsed=raw["elapsed"],
                tokens_before=tuple(raw["tokens_before"]),  # type: ignore[arg-type]
            )
            for s in raw["slots"]:
                planned = agent._rebuild_planned(
                    stage=run.stage,
                    call=FunctionCall(name=s["name"], args=decode(s["args"])),
                    decided=load_result(s["decided"]),
                    duplicate=s["duplicate"],
                )
                run.slots.append(
                    _Slot(
                        planned=planned,
                        state=s["state"],
                        result=load_result(s["result"]),
                        child=s["child"],
                        at=s["at"],
                        payload=decode(s["payload"]),
                        answer=(s["answer"][0], decode(s["answer"][1])),
                    )
                )
            runtime.runs[run.run_id] = run
        return runtime

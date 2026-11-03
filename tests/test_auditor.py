"""
組み込みの監査役（Network(enable_auditor=...)）の検証。

守ることは5つ。記憶を持つエージェントの差分が監査役へ渡り、差し戻したら
当てずに作り直させること。材料が揃うこと（ナレッジと手段は入り、toolの結果は
入らない）。前に差し戻した理由が、同じ更新の次の監査にだけ載ること。上限・
監査役の失敗・読めない判定では通すこと。利用者が足したプロンプトが、既定の
プロンプトの後ろに付くこと。
"""

import json
from collections.abc import Callable
from typing import ClassVar

import pytest
from conftest import FakeLLM, call_response, diff_response, task_row, text_response

from statecraft import (
    Agent,
    AgentConfig,
    BaseLLM,
    GeneratedEvent,
    Interceptor,
    Network,
    NetworkBuilder,
    NetworkConfig,
    ThoughtLevel,
    Tool,
)
from statecraft.auditor import AUDITOR_INSTRUCTION, MAX_REJECTIONS
from statecraft.llm import LLMResponse


def verdict(approved: bool, reason: str = "", **kw) -> LLMResponse:
    """監査役の判定（output_schemaの形のJSON）を返す応答。"""
    return text_response(
        json.dumps({"approved": approved, "reason": reason}, ensure_ascii=False), **kw
    )


def goal(text: str) -> dict:
    return {"field": "goals", "id": "g-1", "text": text}


def search_rules(topic: str) -> dict:
    return {"value": f"{topic}の規程です"}


def librarian(llm, **kw) -> Agent:
    return Agent(
        name="librarian",
        summary="調べる",
        llm=llm,
        model="m",
        tools=[
            Tool(
                func=search_rules,
                summary="規程を調べる",
                usage="館の名前を渡す",
                evaluation="抜粋なので、全部とは限らない",
            )
        ],
        **kw,
    )


def network_with(agent: Agent, judge: FakeLLM, **settings) -> Network:
    return Network(
        agents=[agent], enable_auditor={"llm": judge, "model": "judge", **settings}
    )


# ---- 監査と差し戻し ----
def test_差し戻した差分は当てずに作り直させる():
    judge = FakeLLM([verdict(False, "依頼から外れている"), verdict(True)])
    llm = FakeLLM(
        [
            diff_response([goal("脱線した目標")]),
            diff_response([goal("直した目標")]),
            text_response("回答"),
        ]
    )
    agent = librarian(llm)
    network_with(agent, judge)

    agent.respond(message="貸出規程を教えて")

    assert [g.text for g in agent.private_memory.goals] == ["直した目標"]
    assert "依頼から外れている" in llm.calls[1]["prompt"]
    assert len(judge.calls) == 2


def test_監査役には原文と記憶と差分とナレッジと手段が渡り_toolの結果は渡らない():
    judge = FakeLLM([verdict(True), verdict(True)])
    llm = FakeLLM(
        [
            diff_response(
                [
                    goal("貸出規程を答える"),
                    task_row("t-1", "規程を調べる", "next", ["search_rules"]),
                ]
            ),
            call_response("search_rules", topic="貸出"),
            diff_response([task_row("t-1", "規程を調べる", "done", ["search_rules"])]),
            text_response("回答"),
        ]
    )
    agent = librarian(
        llm,
        knowledge="館内の規程は本館が決める",
        initial_knowledge="タスクの立て方の例",
    )
    network_with(agent, judge)

    agent.respond(message="貸出規程を教えて")

    initial, update = (call["prompt"] for call in judge.calls)
    # 原文
    assert "貸出規程を教えて" in update
    # 今の記憶（この差分を当てる前）と、差分
    assert "貸出規程を答える" in update
    assert '"status": "done"' in update
    # ナレッジと、使える手段（結果の読み方まで）
    assert "館内の規程は本館が決める" in update
    assert "規程を調べる" in update
    assert "館の名前を渡す" in update
    assert "抜粋なので、全部とは限らない" in update
    # toolの結果そのものは渡さない（監査するのは向きで、事実の照合ではない）
    assert "貸出の規程です" not in update
    # initial_knowledgeは初期の記憶を作る時にしか載らないので、その時だけ渡す
    assert "タスクの立て方の例" in initial
    assert "タスクの立て方の例" not in update


def test_差し戻した理由は同じ更新の次の監査にだけ載る():
    """直された差分は、前の指摘と並べて判定する。通った後の更新には持ち越さない。"""
    judge = FakeLLM([verdict(False, "他の館も確かめる計画にする"), verdict(True), verdict(True)])
    llm = FakeLLM(
        [
            diff_response(
                [goal("目標"), task_row("t-1", "規程を調べる", "next", ["search_rules"])]
            ),
            diff_response([goal("直した目標")]),
            call_response("search_rules", topic="貸出"),
            diff_response([task_row("t-1", "規程を調べる", "done", ["search_rules"])]),
            text_response("回答"),
        ]
    )
    agent = librarian(llm)
    network_with(agent, judge)

    agent.respond(message="x")

    first, retried, update = (call["prompt"] for call in judge.calls)
    assert "他の館も確かめる計画にする" not in first
    assert "他の館も確かめる計画にする" in retried
    assert "他の館も確かめる計画にする" not in update


def test_上限まで差し戻したら次は監査せずに通す():
    judge = FakeLLM([verdict(False, "まだずれている")] * MAX_REJECTIONS)
    llm = FakeLLM(
        [diff_response([goal(f"{i}回目")]) for i in range(1, MAX_REJECTIONS + 2)]
        + [text_response("回答")]
    )
    agent = librarian(llm)
    network_with(agent, judge)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == [f"{MAX_REJECTIONS + 1}回目"]
    assert len(judge.calls) == MAX_REJECTIONS


@pytest.mark.parametrize(
    "responses",
    [
        [RuntimeError("APIが落ちた")],
        # JSONとして読めない回答は、ReflexAgentが1度だけ作り直させる
        [text_response("判定: 差し戻し")] * 2,
        [text_response('{"ok": false}')],
    ],
    ids=["監査役の失敗", "JSONでない判定", "approvedの無い判定"],
)
def test_監査できなければ通す(responses):
    """差し戻しに倒すと、同じ記憶の更新が作り直され続ける。"""
    judge = FakeLLM(responses)
    llm = FakeLLM([diff_response([goal("目標")]), text_response("回答")])
    agent = librarian(llm)
    network_with(agent, judge)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == ["目標"]
    # 台本が尽きて失敗したのではなく、台本どおりの応答を受けて通している
    assert len(judge.calls) == len(responses)


# ---- 監査役の設定 ----
def test_足したプロンプトは既定のプロンプトの後ろに付く():
    judge = FakeLLM([verdict(True)])
    llm = FakeLLM([diff_response([goal("目標")]), text_response("回答")])
    agent = librarian(llm)
    network_with(
        agent,
        judge,
        thought_level=ThoughtLevel.LOW,
        system_instruction="社内では「案件」は商談のこと",
    )

    agent.respond(message="x")

    system = "\n".join(judge.calls[0]["system_instruction"])
    assert f"{AUDITOR_INSTRUCTION}\n\n社内では「案件」は商談のこと" in system
    assert judge.calls[0]["model"] == "judge"


def test_監査役の生成もNetworkのinterceptorを通り_消費はauditorとして数える():
    """費用の上限や観測が、監査の分にもそのまま効くように。"""
    generated: list[GeneratedEvent] = []
    interceptor = Interceptor()
    interceptor.notice.generated(generated.append)
    judge = FakeLLM([verdict(True, input_tokens=100, output_tokens=10)])
    llm = FakeLLM([diff_response([goal("目標")]), text_response("回答")])
    agent = librarian(llm)
    network = Network(
        agents=[agent],
        interceptor=interceptor,
        enable_auditor={"llm": judge, "model": "judge"},
    )

    agent.respond(message="x")

    assert "auditor" in [e.agent for e in generated]
    assert network.token_usage()["auditor"] == (100, 10)
    network.reset()
    assert network.token_usage()["auditor"] == (0, 0)


class NoCarryLLM(FakeLLM):
    """ClaudeLLMと同じく、思考の継承（exchanges）を渡されたら落ちる。"""

    def generate(self, **kwargs) -> LLMResponse:
        if kwargs.get("exchanges") is not None:
            raise NotImplementedError("思考の継承に対応していません")
        return super().generate(**kwargs)


def test_思考の継承に対応していないLLMでも監査する():
    """監査役は1回きりの判定なので継承しない。継承すると毎回落ちて、全部通ってしまう。"""
    judge = NoCarryLLM([verdict(False, "ずれている"), verdict(True)])
    llm = FakeLLM(
        [
            diff_response([goal("脱線した目標")]),
            diff_response([goal("直した目標")]),
            text_response("回答"),
        ]
    )
    agent = librarian(llm)
    network_with(agent, judge)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == ["直した目標"]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"llm": FakeLLM(), "model": "m", "modle": "m"}, "知らない項目"),
        ({"llm": FakeLLM()}, "'model'"),
        (True, "dict"),
    ],
    ids=["知らない項目", "必須の項目が無い", "dictでない"],
)
def test_設定の誤りは構築時に落とす(settings, message):
    with pytest.raises(ValueError, match=message):
        Network(agents=[librarian(FakeLLM())], enable_auditor=settings)


def test_auditorという名前のエージェントとは並べられない():
    agent = Agent(name="auditor", summary="s", llm=FakeLLM(), model="m")

    with pytest.raises(ValueError, match="組み込みの監査役の名前"):
        Network(agents=[agent], enable_auditor={"llm": FakeLLM(), "model": "m"})


def test_渡さなければ監査しない():
    network = Network(agents=[librarian(FakeLLM())])

    assert "auditor" not in network.token_usage()


def test_NetworkBuilderからも渡せる():
    class Builder(NetworkBuilder):
        LLM_FACTORIES: ClassVar[dict[str, Callable[[], BaseLLM]]] = {"fake": FakeLLM}

    config = NetworkConfig(
        agents=[AgentConfig(name="a", summary="s", provider="fake", model="m")]
    )

    network = Builder(config).build(enable_auditor={"llm": FakeLLM(), "model": "m"})

    assert network.token_usage()["auditor"] == (0, 0)

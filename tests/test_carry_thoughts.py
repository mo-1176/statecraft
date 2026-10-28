"""
思考の継承（carry_thoughts）。

Agentが前の生成を縮めた発話・出力・実行したという事実だけで送り返し、
toolの生の戻り値を会話に残さないことを確かめる。replyの中身は解釈しないので、
FakeLLMの応答には目印の文字列を入れておく。
"""

from types import SimpleNamespace

import pytest
from conftest import FakeLLM, call_response, diff_response, task_row, text_response

from statecraft import (
    Agent,
    GenerationConfig,
    Interceptor,
    Phase,
    ReflexAgent,
    Tool,
)
from statecraft import llm as llm_module
from statecraft.llm import CallOutcome, ClaudeLLM, Exchange, LLMResponse

RAW = "一般は5冊まで、2週間（生の戻り値）"


def search_rules(topic: str) -> dict:
    return {"value": RAW}


def tool() -> Tool:
    return Tool(func=search_rules, summary="規程を調べる")


def replied(response: LLMResponse, reply: str, thoughts: str = "") -> LLMResponse:
    """送り返す出力の目印を付ける（本物ではプロバイダのContentなどが入る）。"""
    response.reply = reply
    response.thoughts = thoughts
    return response


def one_round_trip() -> list[LLMResponse]:
    """初期記憶 → 呼び出し → 記憶の更新 → 回答 の4回分。"""
    return [
        replied(
            diff_response(
                [
                    {"field": "goals", "id": "g-1", "text": "貸出規程を答える"},
                    task_row("t-1", "規程を調べる", "next", ["search_rules"]),
                ]
            ),
            "r-initial",
        ),
        replied(call_response("search_rules", topic="貸出"), "r-call"),
        replied(
            diff_response(
                [
                    {"field": "facts", "id": "f-1", "text": "一般は5冊まで"},
                    task_row("t-1", "規程を調べる", "done", ["search_rules"]),
                ]
            ),
            "r-update",
        ),
        replied(text_response("5冊までです"), "r-answer"),
    ]


def build_agent(llm, **kw) -> Agent:
    kw.setdefault("carry_thoughts", True)
    return Agent(name="librarian", summary="調べる", llm=llm, model="m", tools=[tool()], **kw)


def sent_text(call: dict) -> str:
    """その生成で送り返したもの（縮めた発話と、対にした結果）を1つの文字列にする。"""
    parts = []
    for ex in call.get("exchanges") or []:
        parts.append(ex.prompt)
        parts.extend(o.text for o in ex.outcomes)
    return "\n".join(parts)


# ---- 既定では何も変えない ----
def test_既定では過去の生成を送り返さない():
    llm = FakeLLM(one_round_trip())
    agent = build_agent(llm, carry_thoughts=False)

    agent.respond(message="貸出規程を教えて")

    # 引数自体を渡さない（exchangesを知らない自作のLLMも、そのまま動く）
    assert all("exchanges" not in call for call in llm.calls)


# ---- 何を送り返すか ----
def test_前の生成を縮めた発話と出力で送り返す():
    llm = FakeLLM(one_round_trip())
    agent = build_agent(llm)

    agent.execute(kwargs={"message": "貸出規程を教えて"})

    initial = Exchange(prompt="【依頼内容】\n貸出規程を教えて\n\n[initial_memory]", reply="r-initial")
    call = Exchange(
        prompt="[function_call]", reply="r-call", outcomes=[CallOutcome(text="実行済み")]
    )
    update = Exchange(prompt="[memory_update]", reply="r-update")
    assert [c["exchanges"] for c in llm.calls] == [
        [],
        [initial],
        [initial, call],
        [initial, call, update],
    ]


def test_利用者からの依頼は縮めた発話に残さない():
    """利用者の発言は shared_memory.conversation に残り続けるので、二重に載せない。"""
    llm = FakeLLM(one_round_trip())
    agent = build_agent(llm)

    agent.respond(message="貸出規程を教えて")

    assert llm.calls[1]["exchanges"][0].prompt == "[initial_memory]"


def test_生の戻り値は評価する1回の生成にだけ載る():
    llm = FakeLLM(one_round_trip())
    agent = build_agent(llm)

    agent.respond(message="貸出規程を教えて")

    update_call = llm.calls[2]
    assert RAW in update_call["prompt"]
    # 会話として送り返す側には、どの生成でも入らない
    assert all(RAW not in sent_text(c) for c in llm.calls)
    # 以降の生成の発話にも載らない（今までどおり要約済みとして隠れる）
    assert RAW not in llm.calls[3]["prompt"]


def test_失敗した呼び出しはエラーとして対にする():
    responses = one_round_trip()
    responses[1] = replied(call_response("missing_tool"), "r-call")
    llm = FakeLLM(responses)
    agent = build_agent(llm)

    agent.respond(message="貸出規程を教えて")

    [outcome] = llm.calls[2]["exchanges"][-1].outcomes
    assert outcome.is_error
    assert "missing_tool" in outcome.text


def test_思考の要約を生成のイベントで観測できる():
    responses = one_round_trip()
    responses[0] = replied(responses[0], "r-initial", thoughts="まず規程を調べる")
    events = []
    interceptor = Interceptor()
    interceptor.on.generated(events.append)
    agent = build_agent(FakeLLM(responses), interceptor=interceptor)

    agent.respond(message="貸出規程を教えて")

    assert events[0].thoughts == "まず規程を調べる"


# ---- いつまで持ち続けるか ----
def answer_only() -> list[LLMResponse]:
    """タスクを立てずに答える、2回分の生成。"""
    return [
        replied(diff_response([{"field": "goals", "id": "g-1", "text": "答える"}]), "r-initial"),
        replied(text_response("回答"), "r-answer"),
    ]


def test_次の起動へ持ち越しreset_で捨てる():
    llm = FakeLLM(answer_only() * 3)
    agent = build_agent(llm)

    agent.execute(kwargs={"message": "1件目"})
    agent.execute(kwargs={"message": "2件目"})

    # 2回目の起動の最初の生成が、1回目の起動の2回分を受け取る
    second = llm.calls[2]["exchanges"]
    assert [ex.reply for ex in second] == ["r-initial", "r-answer"]
    # 2回目の起動の最初の生成には、2回目の依頼内容が残る
    assert "2件目" in llm.calls[3]["exchanges"][2].prompt

    agent.reset()
    agent.execute(kwargs={"message": "3件目"})

    assert llm.calls[4]["exchanges"] == []


def test_別の接続が作った生成は送らない():
    """phase_overridesで別のllmを使うphaseの出力は、元のllmへ送り返さない（逆も同じ）。"""
    responses = one_round_trip()
    main = FakeLLM([responses[0], responses[1], responses[3]])
    other = FakeLLM([responses[2]])
    agent = build_agent(
        main, phase_overrides={Phase.MEMORY_UPDATE: GenerationConfig(llm=other)}
    )

    agent.respond(message="貸出規程を教えて")

    assert other.calls[0]["exchanges"] == []
    # 回答の生成には、mainが作った2回分だけが届く
    assert [ex.reply for ex in main.calls[2]["exchanges"]] == ["r-initial", "r-call"]


def test_ReflexAgentも結果を対にして送り返す():
    llm = FakeLLM(
        [
            replied(call_response("search_rules", topic="貸出"), "r-call"),
            replied(text_response("5冊までです"), "r-answer"),
        ]
    )
    agent = ReflexAgent(
        name="front", summary="窓口", llm=llm, model="m", tools=[tool()], carry_thoughts=True
    )

    agent.respond(message="貸出規程を教えて")

    [exchange] = llm.calls[1]["exchanges"]
    assert exchange.outcomes == [CallOutcome(text="実行済み")]
    # ReflexAgentは今の発話に結果を載せ続ける（設計どおり）が、会話には残さない
    assert RAW in llm.calls[1]["prompt"]
    assert RAW not in sent_text(llm.calls[1])


# ---- 対応していないプロバイダ ----
def test_対応していないプロバイダは継承を断る(monkeypatch):
    """黙って1問1答にすると、継承したつもりで動いていることに気付けない。"""
    sdk = SimpleNamespace(Anthropic=lambda **_: SimpleNamespace())
    monkeypatch.setattr(llm_module, "_require", lambda *_: sdk)

    with pytest.raises(NotImplementedError, match="ClaudeLLM"):
        ClaudeLLM().generate(
            model="m", prompt="今回", temperature=None, max_tokens=10, exchanges=[]
        )

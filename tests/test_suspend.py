"""
中断と再開（suspend / save_state / load_state / resume）。

確かめたいのは「文字通りの中断と再開」になっていること。

  ・中断までに実行したtoolは、再開しても実行し直さない
  ・誰が誰を呼び、何周目のどの呼び出しの結果を待っていたかが、別のNetworkへ
    読み込んでも続きから進む
  ・保存する状態は json.dumps がそのまま通る

別のNetworkへ読み込む時は、中断までの台本と再開後の台本を別のFakeLLMへ分ける。
再開後のFakeLLMに中断前の分を入れないことで、「最初からやり直していない」ことも
台本の数で確かめられる（やり直すと台本が尽きて落ちる）。
"""

import json

import pytest
from conftest import (
    FakeLLM,
    call_response,
    calls_response,
    diff_response,
    task_row,
    text_response,
)

from statecraft import (
    Agent,
    ExecuteDecision,
    GenerateDecision,
    Interceptor,
    Network,
    Suspended,
    Tool,
    ToolResult,
    suspend,
)
from statecraft.llm import CallOutcome, Exchange, LLMResponse


def candidates_tool(calls: list) -> Tool:
    """候補を作って利用者に選んでもらうtool。選ばれたものが、このtoolの結果になる。"""

    def present_candidates(query: str) -> dict:
        calls.append(query)
        suspend({"candidates": [f"{query}-A", f"{query}-B"]})
        return {"value": "ここへは来ない"}

    return Tool(func=present_candidates, summary="候補を利用者に見せて選んでもらう")


def search_tool(calls: list) -> Tool:
    def search_rules(topic: str) -> dict:
        calls.append(topic)
        return {"value": f"{topic}の規程です"}

    return Tool(func=search_rules, summary="規程を調べる")


def plan(*targets: str, status: str = "next") -> LLMResponse:
    """初期記憶。targetsを呼ぶタスクを立てる。"""
    return diff_response(
        [{"field": "goals", "id": "g-1", "text": "選んでもらう"}]
        + [task_row(f"t-{t}", f"{t}を使うため", status, [t]) for t in targets]
    )


def done(*targets: str) -> LLMResponse:
    """記憶の更新。targetsのタスクを終える（残すと差し戻しで生成が増える）。"""
    return diff_response([task_row(f"t-{t}", f"{t}を使うため", "done", [t]) for t in targets])


def roundtrip(net: Network) -> dict:
    """保存した状態がJSONを往復できることも、ついでに確かめる。"""
    return json.loads(json.dumps(net.save_state(), ensure_ascii=False))


# ---- toolの本体で中断する（答えがtoolの結果になる） ----
def front_with_candidates(llm, calls: list, **kw) -> Network:
    front = Agent(
        name="front", summary="窓口", llm=llm, model="m", tools=[candidates_tool(calls)], **kw
    )
    return Network(agents=[front])


def test_toolで中断した依頼を別のNetworkで再開する():
    calls: list = []
    net = front_with_candidates(
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )

    result = net.respond(agent="front", message="EQを選びたい")

    assert isinstance(result, Suspended)
    [pending] = result.pending
    assert pending.caller == "front"
    assert pending.name == "present_candidates"
    assert pending.kwargs == {"query": "EQ"}
    assert pending.at == "tool"
    assert pending.payload == {"candidates": ["EQ-A", "EQ-B"]}

    saved = roundtrip(net)
    after = FakeLLM([done("present_candidates"), text_response("EQ-Bにしました")])
    restored = front_with_candidates(after, calls)
    restored.load_state(saved)

    # 戻すだけで動かさない。中断している地点は、もう一度見せられる
    assert after.calls == []
    assert restored.pending == result.pending

    response = restored.resume(answers={pending.point: "EQ-B"})

    assert response.text == "EQ-Bにしました"
    # 中断したtoolは実行し直さず、答えがそのまま結果になる
    assert calls == ["EQ"]
    assert "present_candidates(query='EQ')\n    EQ-B" in after.calls[0]["prompt"]
    # 記憶も戻っている
    front = restored.get("front")
    assert [g.text for g in front.private_memory.goals] == ["選んでもらう"]
    assert "EQを選びたい" in restored.shared_memory.render()


def test_同じプロセスならそのまま再開できる():
    calls: list = []
    llm = FakeLLM(
        [
            plan("present_candidates"),
            call_response("present_candidates", query="EQ"),
            done("present_candidates"),
            text_response("EQ-Aにしました"),
        ]
    )
    net = front_with_candidates(llm, calls)

    [pending] = net.respond(agent="front", message="EQを選びたい").pending
    response = net.resume(answers={pending.point: "EQ-A"})

    assert response.text == "EQ-Aにしました"
    assert calls == ["EQ"]


def test_答えにToolResultを渡せば失敗も結果にできる():
    calls: list = []
    llm = FakeLLM(
        [
            plan("present_candidates"),
            call_response("present_candidates", query="EQ"),
            done("present_candidates"),
            text_response("選ばれませんでした"),
        ]
    )
    net = front_with_candidates(llm, calls)
    [pending] = net.respond(agent="front", message="EQを選びたい").pending

    net.resume(answers={pending.point: ToolResult(error="利用者が選ばずに閉じました")})

    assert "利用者が選ばずに閉じました" in llm.calls[2]["prompt"]


def test_答えなかった地点は実行し直す():
    calls: list = []
    net = front_with_candidates(
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )

    first = net.respond(agent="front", message="EQを選びたい")
    second = net.resume()

    # 実行し直したtoolが、同じ所でまた止まる
    assert isinstance(second, Suspended)
    assert calls == ["EQ", "EQ"]
    assert [p.point for p in second.pending] == [p.point for p in first.pending]


def test_並列の呼び出しは止まらなかった方を実行し直さない():
    candidates: list = []
    searched: list = []

    def build(llm) -> Network:
        front = Agent(
            name="front",
            summary="窓口",
            llm=llm,
            model="m",
            tools=[candidates_tool(candidates), search_tool(searched)],
        )
        return Network(agents=[front])

    net = build(
        FakeLLM(
            [
                plan("present_candidates", "search_rules", status="next_parallel"),
                calls_response(
                    ("present_candidates", {"query": "EQ"}), ("search_rules", {"topic": "EQ"})
                ),
            ]
        )
    )
    [pending] = net.respond(agent="front", message="EQを選びたい").pending

    after = FakeLLM([done("present_candidates", "search_rules"), text_response("完了")])
    restored = build(after)
    restored.load_state(roundtrip(net))
    restored.resume(answers={pending.point: "EQ-A"})

    assert searched == ["EQ"]
    # 両方の結果が、呼び出しの順のまま記憶の更新へ渡る
    prompt = after.calls[0]["prompt"]
    assert prompt.index("EQ-A") < prompt.index("EQの規程です")


# ---- 委譲の先で中断する（親子の関係が戻る） ----
def front_and_scout(front_llm, scout_llm, calls: list, **scout_kw) -> Network:
    scout = Agent(
        name="scout",
        summary="候補を探す",
        llm=scout_llm,
        model="m",
        tools=[candidates_tool(calls)],
        **scout_kw,
    )
    front = Agent(
        name="front", summary="窓口", llm=front_llm, model="m", sub_agent_names=["scout"]
    )
    return Network(agents=[front, scout])


def test_委譲先で中断しても親の続きから再開する():
    calls: list = []
    net = front_and_scout(
        FakeLLM([plan("scout"), call_response("scout", message="EQの候補を出して")]),
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )

    result = net.respond(agent="front", message="EQを選びたい")
    [pending] = result.pending
    assert pending.caller == "scout"

    front_after = FakeLLM([done("scout"), text_response("scoutによるとEQ-B")])
    scout_after = FakeLLM([done("present_candidates"), text_response("EQ-Bを選んだ")])
    restored = front_and_scout(front_after, scout_after, calls)
    restored.load_state(roundtrip(net))
    executed: list = []
    restored.interceptor.notice.executed(executed.append)

    response = restored.resume(answers={pending.point: "EQ-B"})

    assert response.text == "scoutによるとEQ-B"
    assert calls == ["EQ"]
    # scoutは中断した起動の続きを終え、その答えがfrontの待っていた枠へ入る
    assert [a.text for a in restored.shared_memory.agent_answers] == ["EQ-Bを選んだ"]
    assert "EQ-Bを選んだ" in front_after.calls[0]["prompt"]
    [to_scout] = [e for e in executed if e.callee == "scout"]
    assert to_scout.callee_run_id == pending.point.split("/")[0]


def test_同じエージェントへの2件目は1件目が終わるまで待ち保存をまたいでも続く():
    calls: list = []
    net = front_and_scout(
        FakeLLM(
            [
                plan("scout"),
                calls_response(("scout", {"message": "EQ"}), ("scout", {"message": "Comp"})),
            ]
        ),
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )
    [first] = net.respond(agent="front", message="EQとCompを選びたい").pending
    assert first.kwargs == {"query": "EQ"}

    front_after = FakeLLM([done("scout"), text_response("まとめました")])
    scout_after = FakeLLM(
        [
            done("present_candidates"),
            text_response("EQはEQ-B"),
            # 1件目が終わってから、2件目の起動が始まる
            plan("present_candidates"),
            call_response("present_candidates", query="Comp"),
            done("present_candidates"),
            text_response("CompはComp-A"),
        ]
    )
    restored = front_and_scout(front_after, scout_after, calls)
    restored.load_state(roundtrip(net))

    second = restored.resume(answers={first.point: "EQ-B"})
    [pending] = second.pending
    assert pending.kwargs == {"query": "Comp"}
    response = restored.resume(answers={pending.point: "Comp-A"})

    assert response.text == "まとめました"
    assert calls == ["EQ", "Comp"]
    assert [a.text for a in restored.shared_memory.agent_answers] == [
        "EQはEQ-B",
        "CompはComp-A",
    ]


def test_サブエージェントの手前で止めた状態から設定を変えて動かせる():
    """エージェント単体の挙動を、本物の流れの途中から何度も試すための使い方。"""

    def stop_before_advisor(e):
        if e.name == "advisor":
            suspend("advisorの手前")
        return ExecuteDecision(ok=True)

    def build(front_llm, advisor_llm, *, instruction: str, guard=None) -> Network:
        interceptor = Interceptor()
        if guard:
            interceptor.guard.before_execute(guard)
        advisor = Agent(
            name="advisor",
            summary="候補を比べる",
            llm=advisor_llm,
            model="m",
            system_instruction=instruction,
        )
        front = Agent(
            name="front",
            summary="窓口",
            llm=front_llm,
            model="m",
            sub_agent_names=["advisor"],
        )
        return Network(agents=[front, advisor], interceptor=interceptor)

    net = build(
        FakeLLM([plan("advisor"), call_response("advisor", message="EQを比べて")]),
        FakeLLM(),
        instruction="古い人格",
        guard=stop_before_advisor,
    )
    result = net.respond(agent="front", message="EQを選びたい")
    assert result.pending[0].at == "before_execute"
    fixture = roundtrip(net)

    advisor_llm = FakeLLM([diff_response([]), text_response("EQ-Aがおすすめ")])
    restored = build(
        FakeLLM([done("advisor"), text_response("advisorによるとEQ-A")]),
        advisor_llm,
        instruction="新しい人格",
    )
    restored.load_state(fixture)
    response = restored.resume()

    assert response.text == "advisorによるとEQ-A"
    # advisorは、変えた後の設定で、戻した共有記憶を読んで動く
    assert "新しい人格" in "\n".join(advisor_llm.calls[0]["system_instruction"])
    assert "EQを選びたい" in advisor_llm.calls[0]["prompt"]


# ---- before_executeで中断する（判定し直し、suspend()が答えを返す） ----
def purchase_network(llm, bought: list) -> Network:
    def purchase(item: str) -> dict:
        bought.append(item)
        return {"value": f"{item}を購入しました"}

    def confirm(e):
        if e.name != "purchase":
            return ExecuteDecision(ok=True)
        answer = suspend({"confirm": e.kwargs["item"]})
        return ExecuteDecision(ok=answer == "yes", reason="利用者が購入を取りやめました")

    interceptor = Interceptor()
    interceptor.guard.before_execute(confirm)
    front = Agent(
        name="front",
        summary="窓口",
        llm=llm,
        model="m",
        tools=[Tool(func=purchase, summary="購入する")],
    )
    return Network(agents=[front], interceptor=interceptor)


def purchase_script() -> list:
    return [
        plan("purchase"),
        call_response("purchase", item="EQ"),
        done("purchase"),
        text_response("おしまい"),
    ]


def test_承認されたら実行する():
    bought: list = []
    net = purchase_network(FakeLLM(purchase_script()), bought)

    result = net.respond(agent="front", message="EQを買って")
    [pending] = result.pending
    assert pending.at == "before_execute"
    assert pending.payload == {"confirm": "EQ"}
    assert bought == []

    net.resume(answers={pending.point: "yes"})

    assert bought == ["EQ"]


def test_断られたら実行せず理由がエージェントへ届く():
    bought: list = []
    llm = FakeLLM(purchase_script())
    net = purchase_network(llm, bought)

    [pending] = net.respond(agent="front", message="EQを買って").pending
    net.resume(answers={pending.point: "no"})

    assert bought == []
    assert "利用者が購入を取りやめました" in llm.calls[2]["prompt"]


# ---- 中断をまたいでも、1つの依頼として扱う ----
def with_tokens(response: LLMResponse, n: int) -> LLMResponse:
    response.input_tokens = n
    return response


def test_トークンは中断の前の分も含めて依頼全体を返す():
    calls: list = []
    net = front_with_candidates(
        FakeLLM(
            [
                with_tokens(plan("present_candidates"), 10),
                with_tokens(call_response("present_candidates", query="EQ"), 20),
            ]
        ),
        calls,
    )
    [pending] = net.respond(agent="front", message="EQを選びたい").pending

    restored = front_with_candidates(
        FakeLLM(
            [
                with_tokens(done("present_candidates"), 30),
                text_response("完了", input_tokens=40),
            ]
        ),
        calls,
    )
    restored.load_state(roundtrip(net))
    response = restored.resume(answers={pending.point: "EQ-B"})

    assert response.input_tokens == 100


def test_応答の開始と終了は中断をまたいで1回ずつ流れる():
    calls: list = []
    llm = FakeLLM(
        [
            plan("present_candidates"),
            call_response("present_candidates", query="EQ"),
            done("present_candidates"),
            text_response("完了"),
        ]
    )
    net = front_with_candidates(llm, calls)
    seen: list = []
    net.interceptor.notice.respond_start(lambda e: seen.append("start"))
    net.interceptor.notice.respond_end(lambda e: seen.append("end"))

    [pending] = net.respond(agent="front", message="x").pending
    assert seen == ["start"]
    net.resume(answers={pending.point: "EQ-A"})

    assert seen == ["start", "end"]


def replied(response: LLMResponse, reply: str) -> LLMResponse:
    response.reply = reply
    return response


def test_継承する思考も保存して送り返せる():
    calls: list = []
    net = front_with_candidates(
        FakeLLM(
            [
                replied(plan("present_candidates"), "r-initial"),
                replied(call_response("present_candidates", query="EQ"), "r-call"),
            ]
        ),
        calls,
    )
    [pending] = net.respond(agent="front", message="EQを選びたい").pending

    after = FakeLLM([replied(done("present_candidates"), "r-update"), text_response("完了")])
    restored = front_with_candidates(after, calls)
    restored.load_state(roundtrip(net))
    restored.resume(answers={pending.point: "EQ-B"})

    # 結果待ちだった呼び出し要求に、再開後の結果が対になって送り返される
    assert after.calls[0]["exchanges"] == [
        Exchange(prompt="[initial_memory]", reply="r-initial"),
        Exchange(
            prompt="[function_call]", reply="r-call", outcomes=[CallOutcome(text="実行済み")]
        ),
    ]


# ---- 使い方の誤り ----
def test_中断していなければ保存できない():
    net = Network(
        agents=[
            Agent(
                name="front",
                summary="窓口",
                llm=FakeLLM([diff_response([]), text_response("回答")]),
                model="m",
            )
        ]
    )
    net.respond(agent="front", message="x")

    with pytest.raises(RuntimeError, match="中断している依頼がありません"):
        net.save_state()
    with pytest.raises(RuntimeError, match="再開する依頼がありません"):
        net.resume()


def test_中断していない地点への答えは断る():
    calls: list = []
    net = front_with_candidates(
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )
    net.respond(agent="front", message="x")

    with pytest.raises(ValueError, match="中断していない地点"):
        net.resume(answers={"typo": "EQ-A"})


def test_版の違う状態は読み込まない():
    calls: list = []
    net = front_with_candidates(
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )
    net.respond(agent="front", message="x")
    saved = roundtrip(net)
    saved["version"] = 999

    with pytest.raises(ValueError, match="版"):
        front_with_candidates(FakeLLM(), calls).load_state(saved)


def test_無いエージェントの状態は読み込まない():
    calls: list = []
    net = front_with_candidates(
        FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        calls,
    )
    net.respond(agent="front", message="x")
    saved = roundtrip(net)

    other = Network(agents=[Agent(name="desk", summary="別の窓口", llm=FakeLLM(), model="m")])
    with pytest.raises(ValueError, match="front"):
        other.load_state(saved)


def test_JSONにできない値は保存の時に知らせる():
    def present(query: str) -> dict:
        suspend(object())
        return {"value": ""}

    front = Agent(
        name="front",
        summary="窓口",
        llm=FakeLLM([plan("present"), call_response("present", query="EQ")]),
        model="m",
        tools=[Tool(func=present, summary="候補を見せる")],
    )
    net = Network(agents=[front])
    net.respond(agent="front", message="x")

    with pytest.raises(TypeError, match="suspend"):
        net.save_state()


def test_単体のAgentは中断できない():
    """保存して再開する入口がNetworkにしか無いので、黙って止まらずに知らせる。"""
    calls: list = []
    agent = Agent(
        name="front",
        summary="窓口",
        llm=FakeLLM([plan("present_candidates"), call_response("present_candidates", query="EQ")]),
        model="m",
        tools=[candidates_tool(calls)],
    )

    with pytest.raises(RuntimeError, match="Network"):
        agent.respond(message="x")


def test_toolとbefore_execute以外では中断できない():
    """生成の途中では、再開できる場所が無い。"""

    def stop(e):
        suspend("ここでは止まれない")
        return GenerateDecision(ok=True)

    front = Agent(
        name="front", summary="窓口", llm=FakeLLM([diff_response([])]), model="m"
    )
    net = Network(agents=[front])
    net.interceptor.guard.before_generate(stop)

    with pytest.raises(RuntimeError, match="before_execute"):
        net.respond(agent="front", message="x")

"""
記憶の更新の監査（guard.before_memory_diff）の検証。

監査は、差分を当てる前に見せ、差し戻したら当てずに作り直させる仕組み。
守ることは4つ。差し戻した差分が記憶に入らないこと、監査に要る材料
（原文・今の記憶・差分）が揃って渡ること、上限は監査する側が持つこと、
作り直しは差し戻した差分を直す形で行い、実行結果を読み直させないこと。
"""

from conftest import FakeLLM, call_response, diff_response, task_row, text_response

from statecraft import (
    Agent,
    BeforeMemoryDiffEvent,
    Interceptor,
    MemoryDiffDecision,
    Phase,
    Tool,
)


def search_rules(topic: str) -> dict:
    return {"value": f"{topic}の規程です"}


def build_agent(llm, interceptor: Interceptor, **kw) -> Agent:
    return Agent(
        name="librarian",
        summary="調べる",
        llm=llm,
        model="m",
        tools=[Tool(func=search_rules, summary="規程を調べる")],
        interceptor=interceptor,
        **kw,
    )


def goal(text: str) -> dict:
    return {"field": "goals", "id": "g-1", "text": text}


def reject_first(reason: str = "依頼から外れている"):
    """1回目だけ差し戻し、2回目からは通す監査。上限を監査する側が持つ形の最小例。"""

    def review(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        if event.review_round == 1:
            return MemoryDiffDecision(approved=False, reason=reason)
        return MemoryDiffDecision(approved=True)

    return review


def test_差し戻した差分は当てずに作り直させる():
    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(reject_first())
    llm = FakeLLM(
        [
            diff_response([goal("脱線した目標")]),
            diff_response([goal("直した目標")]),
            text_response("回答"),
        ]
    )
    agent = build_agent(llm, interceptor)

    agent.respond(message="貸出規程を教えて")

    assert [g.text for g in agent.private_memory.goals] == ["直した目標"]
    # 作り直しの生成には、監査の指摘が理由ごと載る
    assert "監査の指摘" in llm.calls[1]["prompt"]
    assert "依頼から外れている" in llm.calls[1]["prompt"]


def test_監査には原文と今の記憶と差分が揃って渡る():
    seen: list[BeforeMemoryDiffEvent] = []

    def review(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        seen.append(event)
        return MemoryDiffDecision(approved=True)

    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(review)
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
    agent = build_agent(llm, interceptor)

    agent.respond(message="貸出規程を教えて")

    initial, update = seen
    assert initial.phase is Phase.INITIAL_MEMORY
    assert update.phase is Phase.MEMORY_UPDATE
    # 原文
    assert "貸出規程を教えて" in update.conversation
    assert update.request == "貸出規程を教えて"
    # 今の記憶は、この差分を当てる前のもの（初期の記憶で立てた目標とタスクが入っている）
    assert "貸出規程を答える" in update.private_memory
    assert "(next)" in update.private_memory
    # 差分と、その材料
    assert update.rows[0]["status"] == "done"
    assert "貸出の規程です" in update.tool_results


def test_監査の差し戻しは形式の差し戻しの回数を使わない():
    """上限は監査する側が持つ。max_memory_retriesは形式の誤りのためのもの。"""

    def reject_twice(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        return MemoryDiffDecision(
            approved=event.review_round >= 3, reason="まだずれている"
        )

    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(reject_twice)
    llm = FakeLLM(
        [
            diff_response([goal("1回目")]),
            diff_response([goal("2回目")]),
            diff_response([goal("3回目")]),
            text_response("回答"),
        ]
    )
    agent = build_agent(llm, interceptor, max_memory_retries=1)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == ["3回目"]


def test_差し戻した差分も記憶の差分イベントで流れる():
    """何を指摘され、どう直したかを後から追えるように。"""
    diffs = []
    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(reject_first("脱線している"))
    interceptor.notice.memory_diff(diffs.append)
    llm = FakeLLM(
        [
            diff_response([goal("脱線した目標")]),
            diff_response([goal("直した目標")]),
            text_response("回答"),
        ]
    )
    agent = build_agent(llm, interceptor)

    agent.respond(message="x")

    rejected, applied = diffs
    assert not rejected.applied
    assert rejected.errors[0].field == "(監査)"
    assert rejected.errors[0].message == "脱線している"
    assert rejected.review_round == 1
    assert applied.applied
    assert applied.review_round == 2
    # 監査の差し戻しでは、形式の試行回数は増えない
    assert rejected.attempt == applied.attempt == 1


def test_監査が壊れていても記憶の更新は止めない():
    """差し戻しに倒すと、壊れた監査が同じ更新を作り直させ続ける。"""

    def broken(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        raise RuntimeError("監査の実装ミス")

    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(broken)
    llm = FakeLLM([diff_response([goal("目標")]), text_response("回答")])
    agent = build_agent(llm, interceptor)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == ["目標"]


def test_作り直しで出さなかった行は差し戻した差分のまま当たる():
    """直す行だけを出させる。全体を出し直させると、指摘と関係ない行まで作り直しになる。"""

    def reject_initial_once(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        if event.phase is Phase.INITIAL_MEMORY and event.review_round == 1:
            return MemoryDiffDecision(approved=False, reason="依頼から外れている")
        return MemoryDiffDecision(approved=True)

    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(reject_initial_once)
    llm = FakeLLM(
        [
            diff_response(
                [
                    goal("脱線した目標"),
                    task_row("t-1", "規程を調べる", "next", ["search_rules"]),
                ]
            ),
            # 作り直しでは、目標だけを直す
            diff_response([goal("直した目標")]),
            call_response("search_rules", topic="貸出"),
            diff_response([task_row("t-1", "規程を調べる", "done", ["search_rules"])]),
            text_response("回答"),
        ]
    )
    agent = build_agent(llm, interceptor)

    agent.respond(message="x")

    assert [g.text for g in agent.private_memory.goals] == ["直した目標"]
    # 出さなかったタスクも当たっていて、実行まで進んでいる
    assert llm.calls[2]["tools"]


def test_作り直しには実行結果を渡さず差し戻した差分を渡す():
    def reject_update_once(event: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        if event.phase is Phase.MEMORY_UPDATE and event.review_round == 1:
            return MemoryDiffDecision(approved=False, reason="調べすぎている")
        return MemoryDiffDecision(approved=True)

    interceptor = Interceptor()
    interceptor.guard.before_memory_diff(reject_update_once)
    llm = FakeLLM(
        [
            diff_response([task_row("t-1", "規程を調べる", "next", ["search_rules"])]),
            call_response("search_rules", topic="貸出"),
            diff_response(
                [
                    task_row("t-1", "規程を調べる", "done", ["search_rules"]),
                    task_row("t-2", "全館の規程も調べる", "next", ["search_rules"]),
                ]
            ),
            diff_response(
                [task_row("t-2", "全館の規程も調べる", "unnecessary", ["search_rules"])]
            ),
            text_response("回答"),
        ]
    )
    agent = build_agent(llm, interceptor)

    agent.respond(message="x")

    update_prompt, retry_prompt = llm.calls[2]["prompt"], llm.calls[3]["prompt"]
    assert "貸出の規程です" in update_prompt
    assert "貸出の規程です" not in retry_prompt
    # 差し戻した差分と指摘は、作り直しの材料として載る
    assert "全館の規程も調べる" in retry_prompt
    assert "調べすぎている" in retry_prompt

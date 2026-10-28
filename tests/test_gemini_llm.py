"""
GeminiLLMの思考の継承。APIは呼ばない。

google-genaiの型（ただのデータ）で応答を組み、送ったリクエストを確かめる。
CIはSDKを入れないので、google-genaiが無い環境では飛ばす。
"""

from types import SimpleNamespace

import pytest

types = pytest.importorskip("google.genai.types")

from statecraft.llm import CallOutcome, Exchange, GeminiLLM


class FakeModels:
    """client.models の代わり。台本の応答を順に返し、リクエストを残す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.requests.append({"contents": contents, "config": config})
        return self.responses.pop(0)


def gemini_response(*parts):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=types.FinishReason.STOP,
            )
        ]
    )


def search_call(signature: bytes = b"sig"):
    return types.Part(
        function_call=types.FunctionCall(id="c1", name="search", args={"q": "x"}),
        thought_signature=signature,
    )


@pytest.fixture
def make_llm():
    def make(*responses):
        llm = GeminiLLM(api_key="test")
        models = FakeModels(responses)
        llm.client = SimpleNamespace(models=models)
        return llm, models

    return make


def generate(llm, **kwargs):
    return llm.generate(model="m", prompt="今回", temperature=None, max_tokens=100, **kwargs)


def test_思考の要約は回答に混ぜず別に返す(make_llm):
    answer = types.Part(text="回答", thought_signature=b"sig")
    llm, models = make_llm(gemini_response(types.Part(text="考え中", thought=True), answer))

    response = generate(llm, exchanges=[])

    assert response.text == "回答"
    assert response.thoughts == "考え中"
    # 送り返すのは署名の付いたパートだけ（要約まで送ると同じ思考を二重に読ませる）
    assert response.reply.parts == [answer]
    assert models.requests[0]["config"].thinking_config.include_thoughts is True


def test_継承しない時は思考の要約を求めない(make_llm):
    llm, models = make_llm(gemini_response(types.Part(text="回答")))

    generate(llm)

    assert models.requests[0]["config"].thinking_config is None


def test_過去の生成を会話として送り返し結果を対にする(make_llm):
    reply = types.Content(role="model", parts=[search_call()])
    exchange = Exchange(
        prompt="[function_call]", reply=reply, outcomes=[CallOutcome(text="実行済み")]
    )
    llm, models = make_llm(gemini_response(types.Part(text="差分")))

    generate(llm, exchanges=[exchange])

    first, second, current = models.requests[0]["contents"]
    assert first.role == "user" and first.parts[0].text == "[function_call]"
    # 前の出力は署名ごとそのまま送る
    assert second is reply
    result, text = current.parts
    assert result.function_response.id == "c1"
    assert result.function_response.name == "search"
    assert result.function_response.response == {"output": "実行済み"}
    assert text.text == "今回"


def test_失敗した呼び出しはerrorとして返す(make_llm):
    reply = types.Content(role="model", parts=[search_call()])
    exchange = Exchange(
        prompt="[function_call]",
        reply=reply,
        outcomes=[CallOutcome(text="存在しません", is_error=True)],
    )
    llm, models = make_llm(gemini_response(types.Part(text="差分")))

    generate(llm, exchanges=[exchange])

    result = models.requests[0]["contents"][-1].parts[0]
    assert result.function_response.response == {"error": "存在しません"}


def test_結果の数が合わなければ送らない(make_llm):
    """どの呼び出しの結果なのかが入れ違ったまま送らない。"""
    reply = types.Content(role="model", parts=[search_call()])
    llm, models = make_llm()

    with pytest.raises(ValueError):
        generate(llm, exchanges=[Exchange(prompt="[function_call]", reply=reply)])

    assert models.requests == []

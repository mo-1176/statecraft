"""
OpenAILLMのstrictモード（出力の構造と、toolの引数）。

APIは呼ばない。openaiの代わりに偽のクライアントを差し込み、送ったリクエストと、
受け取った応答を元の形へ戻せているかを確かめる。
"""

import json
from types import SimpleNamespace

import pytest

from statecraft import llm as llm_module
from statecraft.llm import OpenAILLM, _drop_absent, _strict_schema
from statecraft.memory import PrivateMemory, SharedMemory, build_diff_schema

DIFF_SCHEMA = build_diff_schema(SharedMemory, PrivateMemory)


class FakeBadRequest(Exception):
    """openai.BadRequestErrorの代わり。どの項目を断ったかをparamに持つ。"""

    def __init__(self, param: str):
        super().__init__(param)
        self.param = param


class FakeResponses:
    """client.responses の代わり。台本の応答（または例外）を順に返し、リクエストを残す。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _response(*items):
    return SimpleNamespace(
        output=list(items), usage=None, status="completed", incomplete_details=None
    )


def message(text: str):
    part = SimpleNamespace(type="output_text", text=text)
    return _response(SimpleNamespace(type="message", content=[part]))


def function_call(name: str, args: dict):
    return _response(
        SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args))
    )


@pytest.fixture
def make_llm(monkeypatch):
    def make(*outputs):
        responses = FakeResponses(outputs)
        fake_openai = SimpleNamespace(
            OpenAI=lambda api_key=None: SimpleNamespace(responses=responses),
            BadRequestError=FakeBadRequest,
        )
        monkeypatch.setattr(llm_module, "_require", lambda *_: fake_openai)
        return OpenAILLM(), responses

    return make


def generate(llm, **kwargs):
    return llm.generate(
        model="gpt-test",
        prompt="依頼",
        system_instruction=["共通", "固有", "phase"],
        temperature=None,
        max_tokens=1000,
        **kwargs,
    )


def search_tool(parameters: dict):
    return SimpleNamespace(
        to_declaration=lambda: {
            "name": "search",
            "description": "検索する",
            "parameters": parameters,
        }
    )


SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "q": {"type": "string"},
        "recency": {"type": "string", "enum": ["day", "week"]},
    },
    "required": ["q"],
}


# ---- スキーマの変換 ----
def test_差分のスキーマは全項目必須で任意の項目がnullを取れる形になる():
    schema = _strict_schema(
        {
            "type": "object",
            "properties": {"result": DIFF_SCHEMA},
            "required": ["result"],
        }
    )

    row = schema["properties"]["result"]["items"]
    assert row["additionalProperties"] is False
    assert set(row["required"]) == set(row["properties"])
    # 元から必須の項目はnullを取らない
    assert row["properties"]["field"]["type"] == "string"
    assert None not in row["properties"]["field"]["enum"]
    # 任意だった項目はnullを取れる
    assert row["properties"]["status"]["type"] == ["string", "null"]
    assert None in row["properties"]["status"]["enum"]
    assert row["properties"]["target_names"]["type"] == ["array", "null"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": True,
        },
        {"type": "object", "properties": {"a": {"type": "string", "minLength": 1}}},
        {"type": "array"},
    ],
    ids=[
        "中身を決めていないオブジェクト",
        "自由なキーを許す",
        "使えないキーワード",
        "itemsの無い配列",
    ],
)
def test_strictにできないスキーマはNoneになる(schema):
    assert _strict_schema(schema) is None


def test_意味を変えない注釈は落として送る():
    schema = _strict_schema(
        {
            "type": "object",
            "title": "引数",
            "properties": {"q": {"type": "string", "default": "x"}},
            "required": ["q"],
        }
    )

    assert "title" not in schema
    assert schema["properties"]["q"] == {"type": "string"}


def test_足したnullだけを取り除く():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": ["string", "null"]},
            "rows": {
                "type": "array",
                "items": {"type": "object", "properties": {"c": {"type": "string"}}},
            },
        },
        "required": ["b"],
    }

    value = {"a": None, "b": None, "rows": [{"c": None}, {"c": "x"}]}

    # 元から必須のbのnullは残す
    assert _drop_absent(value, schema) == {"b": None, "rows": [{}, {"c": "x"}]}


# ---- 出力の構造 ----
def test_配列の出力はstrictで送り元の形で受け取る(make_llm):
    returned = {
        "result": [
            {
                "field": "facts",
                "id": "fact-1",
                "text": "内容",
                "basis": "general",
                "source": None,
                "status": None,
                "target_names": None,
                "run_when": None,
                "skip_when": None,
                "resolved_by": None,
                "outcome": None,
            }
        ]
    }
    llm, responses = make_llm(message(json.dumps(returned)))

    res = generate(llm, response_schema=DIFF_SCHEMA)

    assert json.loads(res.text) == [
        {"field": "facts", "id": "fact-1", "text": "内容", "basis": "general"}
    ]
    request = responses.requests[0]
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    # 構造を強制できる時は、スキーマを指示文として送らない
    assert "JSON Schema" not in json.dumps(request["input"], ensure_ascii=False)


def test_オブジェクトの出力は包まずに送る(make_llm):
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["done", "fail"]}},
        "required": ["status"],
    }
    llm, responses = make_llm(message('{"status": "done"}'))

    res = generate(llm, response_schema=schema)

    assert json.loads(res.text) == {"status": "done"}
    sent = responses.requests[0]["text"]["format"]["schema"]
    assert "status" in sent["properties"]


def test_途中で切れたJSONはそのまま返す(make_llm):
    llm, _ = make_llm(message('{"result": [{"field": "fa'))

    res = generate(llm, response_schema=DIFF_SCHEMA)

    assert res.text == '{"result": [{"field": "fa'


def test_strictを断ったモデルは指示文に切り替えて覚える(make_llm):
    llm, responses = make_llm(
        FakeBadRequest("text.format.schema"), message("[]"), message("[]")
    )

    generate(llm, response_schema=DIFF_SCHEMA)
    generate(llm, response_schema=DIFF_SCHEMA)

    first, retry, later = responses.requests
    assert "text" in first
    assert "text" not in retry
    assert "JSON Schema" in json.dumps(retry["input"], ensure_ascii=False)
    # 2回目の生成は、最初から指示文で送る
    assert "text" not in later


def test_strictと関係ない400はそのまま投げる(make_llm):
    llm, _ = make_llm(FakeBadRequest("model"))

    with pytest.raises(FakeBadRequest):
        generate(llm, response_schema=DIFF_SCHEMA)


# ---- toolの引数 ----
def test_toolの任意の引数はstrictで必須にしnullを取り除いて返す(make_llm):
    llm, responses = make_llm(function_call("search", {"q": "x", "recency": None}))

    res = generate(llm, tools=[search_tool(SEARCH_PARAMS)])

    assert res.function_calls[0].args == {"q": "x"}
    sent = responses.requests[0]["tools"][0]
    assert sent["strict"] is True
    assert sent["parameters"]["required"] == ["q", "recency"]
    assert sent["parameters"]["additionalProperties"] is False


def test_strictにできないtoolはそのまま送る(make_llm):
    params = {"type": "object", "properties": {"options": {"type": "object"}}}
    llm, responses = make_llm(function_call("search", {"options": {"a": 1}}))

    res = generate(llm, tools=[search_tool(params)])

    sent = responses.requests[0]["tools"][0]
    assert sent["strict"] is False
    assert sent["parameters"] == params
    assert res.function_calls[0].args == {"options": {"a": 1}}


def test_toolのstrictを断ったモデルはstrict無しで送り直す(make_llm):
    llm, responses = make_llm(
        FakeBadRequest("tools[0].parameters"),
        function_call("search", {"q": "x"}),
    )

    res = generate(llm, tools=[search_tool(SEARCH_PARAMS)])

    first, retry = responses.requests
    assert first["tools"][0]["strict"] is True
    assert retry["tools"][0]["strict"] is False
    assert retry["tools"][0]["parameters"] == SEARCH_PARAMS
    assert res.function_calls[0].args == {"q": "x"}

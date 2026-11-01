# テストを書く

statecraft のテストは、**LLM の API を一切呼びません。** 生成の中身は偽の LLM へ台本として渡し、テストが確かめるのは「その応答を受けて、フレームワークがどう振る舞ったか」だけです。API キーも要らず、課金も発生せず、すぐに終わります。

---

## 動かす

```bash
pip install -e ".[dev]"
pytest                                    # 全部
pytest tests/test_memory_diff.py          # ファイル単位
pytest -k 停滞                             # 名前に「停滞」を含むものだけ
ruff check .                              # lint
```

CI（`.github/workflows/test.yml`）は、push と PR のたびに Python 3.11〜3.14 で pytest と ruff を回します。commit の前に、手元で両方を通しておいてください。

---

## 偽の LLM

`tests/conftest.py` にあります。

| 道具 | 使う時 |
|---|---|
| `FakeLLM` | 応答の順番が決まっている時。台本に入れた順に1つずつ返す。例外を入れると送出する。渡された引数は全部 `calls` に残る |
| `RuleLLM` | 記憶の差し戻しなどで、生成の回数が変わる時。渡された引数の形から phase を見分けて応答する |
| `text_response(text)` | 本文だけの応答 |
| `call_response(name, **args)` | 呼び出しを1件要求する応答 |
| `calls_response(*pairs)` | 呼び出しを複数要求する応答 |
| `diff_response(rows)` | 記憶の差分（JSON の配列）を返す応答 |
| `task_row(id, text, status, targets)` | tasks の差分の1行 |

`FakeLLM` の台本が尽きると、「想定より多く生成が走っている」というエラーで落ちます。

---

## 例: 1往復を台本で書く

`Agent` の1往復は、「初期の記憶 → 呼び出し → 記憶の更新 → 回答」の4回の生成です。台本もその順に並べます。

```python
from statecraft import Agent, Network, Tool

from conftest import FakeLLM, call_response, diff_response, task_row, text_response


def test_one_round():
    calls = []

    def search_rules(topic: str) -> dict:
        calls.append(topic)
        return {"value": f"【{topic}】一般利用者は5冊まで。"}

    llm = FakeLLM(
        [
            # INITIAL_MEMORY: 規程を調べるタスクを立てる
            diff_response([task_row("task-1", "貸出の上限を確かめるため", "next", ["search_rules"])]),
            # FUNCTION_CALL: 引数を決める
            call_response("search_rules", topic="貸出"),
            # MEMORY_UPDATE: 結果を書き起こし、タスクを閉じる
            diff_response(
                [
                    {"field": "facts", "id": "fact-1", "text": "一般利用者の貸出上限は5冊。"},
                    task_row("task-1", "貸出の上限を確かめるため", "done", ["search_rules"]),
                ]
            ),
            # ANSWER
            text_response("5冊までです。"),
        ]
    )
    agent = Agent(
        name="librarian",
        summary="規程を調べる",
        llm=llm,
        model="fake",
        tools=[Tool(func=search_rules, summary="規程を検索する")],
    )
    net = Network(agents=[agent])

    response = agent.respond(message="何冊まで借りられる？")

    assert response.text == "5冊までです。"
    assert calls == ["貸出"]
    assert net.shared_memory.facts[0].text == "一般利用者の貸出上限は5冊。"
    assert llm.scripted == []   # 台本を全部使い切った（余計な生成も、足りない生成も無い）
```

タスクを `done` にし忘れた差分を台本に入れると、システムが差し戻して生成が1回増えるので、台本が尽きて落ちます。こうした「生成の回数が変わる」振る舞いを確かめる時は、`RuleLLM` を使います。

---

## 何を確かめるか

テストが守るのは、**フレームワークの約束**です。LLM の答えの良し悪しは、テストでは確かめません。

| 確かめたいこと | 見る所 |
|---|---|
| プロンプトに何が載ったか | `llm.calls[i]["prompt"]`、`llm.calls[i]["system_instruction"]` |
| どのツールが API へ渡ったか | `llm.calls[i]["tools"]`、`llm.calls[i]["tool_choice"]` |
| 出力の形が強制されたか | `llm.calls[i]["response_schema"]` |
| 記憶に何が入ったか | `net.shared_memory`、`agent.private_memory` |
| イベントが流れたか | Interceptor の `notice` に、記録する関数を登録する |

既存のテストファイルと、それぞれが守っている約束の一覧は、README の「テスト」にあります。

---

## statecraft を使う側のアプリで

使う側のアプリでも、同じ考え方でテストを書けます。`tests/conftest.py` は statecraft のリポジトリの中にあり、配布物には入っていないので、`BaseLLM` を継承した偽の LLM を自分で書きます。

```python
from statecraft import BaseLLM
from statecraft.llm import LLMResponse


class ScriptedLLM(BaseLLM):
    """台本どおりに返す。届いたプロンプトは prompts に残す。"""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.prompts: list[str] = []

    def generate(self, **kwargs) -> LLMResponse:
        self.prompts.append(kwargs["prompt"])
        return LLMResponse(text=self.texts.pop(0))
```

本物の LLM の Adapter を書く前に、この偽物と差し替えて動かせる形にしておくと、Adapter を書いた時に「接続の問題か、組み立ての問題か」を切り分けやすくなります。

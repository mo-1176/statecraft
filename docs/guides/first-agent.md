# 最初の30分 — 1エージェント・1ツールを動かす

statecraft を初めて使う人向けに、エージェント1体とツール1つを、ゼロから動かすところまでを案内します。最後に、中で何が起きたかを観測します。

サンプル（`examples/library/`）を写して始める方法は、README の「はじめ方」にあります。こちらは、最小の部品から組み立てて、仕組みを掴むための手順です。

---

## 0. 準備

- Python 3.11 以上
- 仮想環境（`.venv`）を作って有効にしておく
- LLM の API キー（ここでは Gemini を使います。Google AI Studio で取れます）

```bash
pip install "statecraft[gemini] @ git+https://github.com/mo-1176/statecraft.git"
```

API キーは環境変数 `GEMINI_API_KEY` に置きます。コードや YAML には書かないでください。

---

## 1. フォルダを作る

```
my_app/
  __init__.py
  config/
    agents.yaml
    tools.yaml
  prompts/
    agents/assistant/system_instruction.md
  tools.py
  network.py
main.py
```

## 2. エージェントを書く

```yaml
# my_app/config/agents.yaml
- name: assistant
  summary: 社内規程を調べて答える
  provider: gemini
  model: gemini-3.5-flash-lite
  tools: [search_rules]
```

```markdown
<!-- my_app/prompts/agents/assistant/system_instruction.md -->
あなたは総務の担当者です。社内規程を調べ、根拠を示して答えてください。
```

人格定義は YAML に書かず、決まった場所の md に置きます。YAML から指す必要はありません。

## 3. ツールを書く

LLM に見せる定義（YAML）と、実装（Python）に分けて書きます。

```yaml
# my_app/config/tools.yaml
- name: search_rules
  type: search_rules          # network.py の TOOL_FACTORIES のキー
  summary: 社内規程を検索する
  parameters:
    type: object
    properties:
      topic: {type: string, description: 何について調べるか}
    required: [topic]
```

```python
# my_app/tools.py
def search_rules(topic: str) -> dict:
    return {"value": "【有給休暇】申請は取得日の3営業日前までに行う。..."}
```

ツールは、必ず `{"value": ...}` の形の辞書を返します（→ [reference/tool.md](../reference/tool.md#戻り値の約束)）。

## 4. 組み立てる

```python
# my_app/network.py
from pathlib import Path

from statecraft import Interceptor, Network, NetworkBuilder

from . import tools

ROOT = Path(__file__).resolve().parent


class MyBuilder(NetworkBuilder):
    TOOL_FACTORIES = {"search_rules": lambda: tools.search_rules}


def build_network(interceptor: Interceptor | None = None) -> Network:
    return MyBuilder.from_dir(ROOT, interceptor).build()
```

`type` と実装の対応だけをコードに書きます。

## 5. 動かす

```python
# main.py
from my_app.network import build_network

net = build_network()
response = net["assistant"].respond(message="有給の申請はいつまでに出せばいい？")

print(response.text)
print(net.total_tokens())  # (入力, 出力)
```

```bash
python main.py
```

**API の利用料がかかります。** 1往復で数円未満が目安です。

---

## 6. 中で何が起きたかを見る

答えが返ってきたら、Interceptor を付けて、中の動きを観測してみます。

```python
# main.py
from statecraft import ExecutedEvent, GeneratedEvent, Interceptor, MemoryDiffEvent

from my_app.network import build_network


def show_generated(event: GeneratedEvent) -> None:
    print(f"[生成] {event.agent}/{event.phase} {event.model} "
          f"{event.elapsed:.1f}秒 in={event.input_tokens} out={event.output_tokens}")


def show_executed(event: ExecutedEvent) -> None:
    print(f"[実行] {event.caller} → {event.callee}({event.kwargs})")


def show_memory(event: MemoryDiffEvent) -> None:
    for row in event.rows:
        print(f"[記憶] {row.get('field')}/{row.get('id')}: {row.get('text')}")


ic = Interceptor()
ic.notice.generated(show_generated)
ic.notice.executed(show_executed)
ic.notice.memory_diff(show_memory)

net = build_network(ic)
response = net["assistant"].respond(message="有給の申請はいつまでに出せばいい？")
print(response.text)
```

だいたい、次の順で行が出るはずです。

```
[生成] assistant/initial_memory ...   ← 依頼を読み、goals と最初の tasks を書く
[記憶] goals/... / tasks/task-1: ...   ← search_rules を next にしたタスク
[生成] assistant/function_call ...    ← search_rules の引数（topic）だけを決める
[実行] assistant → search_rules({'topic': '有給休暇'})
[生成] assistant/memory_update ...    ← 結果を書き起こし、task-1 を done にする
[記憶] facts/... / tasks/task-1: ...
[生成] assistant/answer ...           ← 記憶をもとに答える
```

確かめてほしいのは、次の3点です。

1. **生成が phase に分かれていること。** 見立て → 引数 → 書き起こし → 回答が、別々の生成になっています（→ [concepts/phases-and-prompts.md](../concepts/phases-and-prompts.md)）。
2. **ツールを呼ぶ生成が、引数しか決めていないこと。** 何を呼ぶかは、その前の記憶のタスクで決まっています。
3. **ツールの結果が、記憶へ書き起こされていること。** 次のステップで見えるのは、書き起こされた記憶だけです（→ [concepts/memory.md](../concepts/memory.md)）。

---

## 次の一歩

| 足したいもの | 見る所 |
|---|---|
| 会話の窓口と、調べる役を分ける | サンプルの `config/agents.yaml` の `front`（`type: reflex`、`sub_agents`、`input_schema`）、[concepts/network-placement.md](../concepts/network-placement.md) |
| 結果の読み方をツールに持たせる | `prompts/tools/<名前>/evaluation.md`、[concepts/knowledge.md](../concepts/knowledge.md) |
| 危ない実行を止める | [reference/interceptor.md](../reference/interceptor.md) |
| 会話を続ける、記憶を次のターンへ引き継ぐ | サンプルの `session.py`、`carry_over.py` |
| Web API にする | サンプルの `api.py` |
| Python のコードだけで組む | README の「設定ファイルを使わずに組む」 |
| テストを書く | [testing.md](testing.md) |

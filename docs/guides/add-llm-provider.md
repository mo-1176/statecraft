# 新しい LLM の提供元を足す

statecraft が最初から持っていない LLM の提供元を使いたい時の手順です。`BaseLLM` を継承したクラスを1つ書き、設定から使えるように登録します。

GoF でいえば **Adapter** パターンです。エージェントが期待する形（`BaseLLM.generate()` と `LLMResponse`）に、提供元の SDK の形を合わせます。エージェントの側は、どの提供元かを知りません。

各項目の意味は [reference/llm-providers.md](../reference/llm-providers.md) にあります。既存の実装（`src/statecraft/llm.py` の `GeminiLLM`・`OpenAILLM`・`ClaudeLLM`）が、一番の見本です。

> statecraft 本体は改造せず、使う側のリポジトリにクラスを置いて登録できます。

---

## 1. generate() を実装する

```python
from statecraft import BaseLLM
from statecraft.llm import FunctionCall, LLMResponse


class MyLLM(BaseLLM):
    def __init__(self, *, api_key: str | None = None):
        self.client = ...  # 提供元の SDK のクライアント

    def generate(
        self,
        *,
        model,
        prompt=None,
        messages=None,
        system_instruction=None,
        blobs=None,
        tools=None,
        tool_choice="auto",
        response_schema=None,
        temperature,
        max_tokens,
        thought_level=None,
        exchanges=None,
    ) -> LLMResponse:
        ...
```

受け取ったものを、提供元の形へ移します。

| 受け取るもの | やること |
|---|---|
| `model` | そのまま提供元へ渡す。接続のクラスはモデル名を持たない |
| `system_instruction` | 文字列か、段ごとの配列で来る。提供元にキャッシュの区切りがあれば段の境界に打ち、無ければ `"\n\n"` でつなぐ |
| `messages` | 共通の形 `[{"role": "user" \| "assistant", "content": ...}]` から、提供元の形へ変える |
| `prompt` | 最後の user のメッセージとして足す |
| `blobs` | `[{"data": bytes, "mime_type": str}]` を、提供元の添付の形へ変える |
| `tools` | 各ツールの `to_declaration()`（`name` / `description` / `parameters`）を、提供元のツール定義へ変える |
| `tool_choice` | `"any"` は「どれかを必ず呼ばせる」、`"auto"` は「呼ぶかも任せる」に対応させる |
| `response_schema` | 提供元に出力の形を強制する仕組みがあれば使う。無ければ指示文で伝える |
| `temperature` | **`None` ならパラメータ自体を送らない。** 受け付けないモデルがあるため |
| `thought_level` | 提供元の思考の深さの指定へ対応させる。`None` なら送らない |
| `exchanges` | 思考の継承に対応しないなら、**`None` 以外を受け取ったら例外にする**（下の 4 を参照） |

## 2. LLMResponse に詰める

| 項目 | やること |
|---|---|
| `text` | 生成された本文。呼び出し要求だけなら空文字 |
| `function_calls` | 呼び出し要求を `FunctionCall(name=..., args={...})` の配列にする |
| `input_tokens` | **キャッシュから読んだ分を含めた**入力の合計 |
| `output_tokens` | 出力 |
| `cached_tokens` / `cache_write_tokens` | 提供元が報告するなら入れる |
| `stop_reason` | 提供元の語彙を、`"end"` / `"tool_use"` / `"max_tokens"` / `"refusal"` へ移す。打ち切りと拒否は例外ではなく正常な応答として届くので、ここで区別できるようにする |

## 3. エラーは例外のまま投げる

API のエラーは、握りつぶさずに投げます。再試行するか、別のモデルへ逃がすかは、利用側が `recover.generation_failed` で決めます（→ [reference/interceptor.md](../reference/interceptor.md)）。接続のクラスの中で勝手に再試行しないでください。

ただし、「このモデルはこのパラメータを受け付けない」と分かっている断られ方は、`OpenAILLM` のように、断られたモデルを覚えて以降は送らない、という扱いにできます。

## 4. 思考の継承に対応するか決める

思考の継承（`carry_thoughts`）は、エージェントの既定でオンです。

- **対応しない場合:** `exchanges` が `None` 以外で来たら `NotImplementedError` を投げます。黙って無視すると、継承したつもりで1問1答になり、誰も気付けないためです。この提供元で動かすエージェントは、`carry_thoughts: false` にします。
- **対応する場合:** 過去の生成（`Exchange`）を、思考の署名ごと会話として送り返します。応答の `reply` に提供元の出力そのものを入れ、エージェントに預けさせます。`GeminiLLM` と `OpenAILLM` の実装を見てください。

## 5. 設定から使えるように登録する

```python
from statecraft import NetworkBuilder


class MyBuilder(NetworkBuilder):
    LLM_FACTORIES = {**NetworkBuilder.LLM_FACTORIES, "my_llm": MyLLM}
```

```yaml
# config/agents.yaml
- name: assistant
  provider: my_llm
  model: some-model-name
  carry_thoughts: false   # 思考の継承に対応しないなら
```

接続は `provider` ごとに1つ作られ、同じ `provider` のエージェントで共有されます。API キーは YAML に書かず、環境変数から読みます。

---

## 6. 確かめる

いきなり本物の API で確かめると、失敗した時に原因が切り分けられません。次の順で確かめます。

1. **提供元の SDK だけで、素の呼び出しを通す。** 接続先・キー・モデル名の誤りは、ここで分かります。
2. **`MyLLM.generate()` を直接呼ぶ。** 本文だけの応答、ツールの呼び出し、`response_schema` の3つを試します。
3. **エージェント1体・ツール1つで1往復させる。** [first-agent.md](first-agent.md) の観測のコードを付けると、どの phase で失敗したかが分かります。

## チェックリスト

- [ ] `temperature=None` の時、パラメータを送っていない
- [ ] `tool_choice="any"` の時、必ずどれかのツールを呼ばせている
- [ ] `input_tokens` にキャッシュ分を含めている
- [ ] 打ち切り（`max_tokens`）と拒否（`refusal`）を `stop_reason` で区別できる
- [ ] API のエラーを例外のまま投げている
- [ ] 思考の継承に対応しないなら、`exchanges` を受け取った時に例外にしている
- [ ] API キーをコードや YAML に書いていない

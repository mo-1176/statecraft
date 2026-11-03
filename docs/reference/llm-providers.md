# LLM の提供元

LLM の提供元（プロバイダ）ごとの違いは、`BaseLLM` を継承したクラスが吸収します。実装は `src/statecraft/llm.py` にあります。新しい提供元を足す手順は [guides/add-llm-provider.md](../guides/add-llm-provider.md) にあります。

---

## BaseLLM の約束

`BaseLLM` は、エージェントから見た「LLM への接続」の共通の形です（GoF でいう Adapter の、合わせる先の型）。

- **モデル名を持ちません。** 生成のたびに `model` を受け取ります。どのモデルを使うかは、エージェントと `phase_overrides` が決めます。そのため、接続は提供元の数だけあれば足り、同じ提供元のエージェントで共有できます。
- **実装するのは `generate()` だけです。**

### generate() が受け取るもの

| 引数 | 中身 |
|---|---|
| `model` | 使うモデル名（Azure ではデプロイ名） |
| `prompt` | 毎回変わる部分（記憶の中身、実行履歴、実行結果、現在時刻） |
| `messages` | 会話の履歴。共通の形 `[{"role": "user" \| "assistant", "content": "..."}]` |
| `system_instruction` | 変わらない部分。文字列か、段ごとの配列（キャッシュの区切りに使う） |
| `blobs` | 添付。`[{"data": bytes, "mime_type": str}]` |
| `tools` | 呼び出し候補。各ツールの `to_declaration()` を、提供元の形へ変換して渡す |
| `tool_choice` | `"any"`（どれかを必ず呼ばせる）か `"auto"`（呼ぶかも任せる）。tool を使わせない phase では、`tools` 自体が渡らない |
| `response_schema` | 出力の形（記憶の差分のスキーマ、`output_schema`） |
| `temperature` | `None` ならパラメータ自体を送らない（受け付けないモデルがあるため） |
| `max_tokens` | 出力の上限 |
| `thought_level` | 思考の深さ。`None` ならモデルの既定に任せる |
| `exchanges` | 思考の継承のための過去の生成。`None` なら継承しない。対応していない提供元は、`None` 以外を受け取ったら例外にする |

### generate() が返すもの（LLMResponse）

| 項目 | 中身 |
|---|---|
| `text` | 生成された本文。呼び出し要求だけの応答では空 |
| `function_calls` | 呼び出し要求（`FunctionCall(name, args)` の配列） |
| `input_tokens` / `output_tokens` | 入力（キャッシュ分を含む合計）と出力のトークン数 |
| `cached_tokens` / `cache_write_tokens` | キャッシュから読んだ分 / 書き込んだ分 |
| `stop_reason` | 終わった理由。`"end"` / `"tool_use"` / `"max_tokens"` / `"refusal"` など、共通の語彙に移したもの |
| `reply` / `thoughts` | 思考の継承のための、提供元の出力そのもの / 思考の要約（観測用） |

API のエラーは、例外のまま投げます。再試行するかは、利用側が `recover.generation_failed` で決めます。

---

## 提供元ごとの違い

| | Gemini | OpenAI | Azure OpenAI | Claude |
|---|---|---|---|---|
| クラス | `GeminiLLM` | `OpenAILLM` | `AzureOpenAILLM`（`OpenAILLM` を継承） | `ClaudeLLM` |
| 設定の `provider` | `gemini` | `openai` | `azure_openai` | `claude` |
| 記憶の差分・`output_schema` の強制 | `response_schema` で強制 | strict な `json_schema` で強制 | OpenAI と同じ | 指示文で伝える |
| tool の引数 | 宣言どおり | strict にできるものは strict | OpenAI と同じ | 宣言どおり |
| キャッシュ | 暗黙のキャッシュ（先頭の一致） | GPT-5.6 以降は区切りを打つ | GPT-5.6 以降の Standard のデプロイだけ区切りを受け付ける | 区切りを打つ |
| 思考の継承 | 対応 | 対応 | `OpenAILLM` と同じ形で送る | 未対応（`carry_thoughts=False` にする） |
| 鍵 | `GEMINI_API_KEY` | `OPENAI_API_KEY` | `AZURE_OPENAI_ENDPOINT` と `AZURE_OPENAI_API_KEY`（または Entra ID のトークン） | `ANTHROPIC_API_KEY` |

### OpenAI の strict と、断られた時の扱い

strict な `json_schema` は、ルートがオブジェクトで、全項目が必須のスキーマしか受け付けません。`OpenAILLM` は送る時に形を直し、受け取った時に元へ戻します。

- 記憶の差分はルートが配列なので、オブジェクトで包んで送り、受け取ったら剥がします。
- 任意の項目は「null も取れる必須の項目」にして送り、返ってきた null は取り除きます。
- strict にできないスキーマは、指示文で伝えます。

モデルが**キャッシュの区切り**や **strict** を断った場合は、断られた時点でそのモデルを覚えて、以降は区切り無し・strict 無しで送ります。そのため、古いモデルでは最初の1回だけ 400 が返り、すぐに送り直されます。覚えるのは接続のインスタンスごとです。

### Azure OpenAI

- **v1 API を使います。** OpenAI と同じ形のリクエストを受け付けるので、変換は `OpenAILLM` のものをそのまま使い、接続先と認証だけを差し替えています。SDK の `AzureOpenAI` クラス（日付つきの `api_version` を要求する旧方式）は使いません。
- **`model` には、モデル名ではなく Azure で付けたデプロイ名を渡します。** 違うと 404 になります。
- **エンドポイントは `https://<リソース名>.openai.azure.com`** の形で渡します。`/openai/v1` まで写した場合も二重には付けません。ただし、ポータルの「ターゲット URI」を `/responses` まで写すと、パスが二重になって 400 になります。
- **認証は、キーか Entra ID のトークンのどちらか1つです。** トークンには期限があるので、文字列ではなく、トークンを返す関数（`token_provider`）を渡します（README の「組み立てる」に例があります）。
- **入力がコンテンツフィルタに掛かると、400（code が `content_filter`）が返ります。** 例外のまま外へ出します。同じ入力で再試行しても同じ結果になります。

### 動作の確認状況

README の「実装状況」が正です。この文書を書いた時点で分かっていることを補足します。

- plugin-scout（statecraft を使う側のアプリ）から、Azure の gpt-5-mini（Global Standard のデプロイ）で、委譲・tool・記憶の更新・監査を含む1往復が通りました（2026-11-01）。キャッシュの区切りは最初の1回で断られ、区切り無しへ切り替わりました。

---

## 接続の共有

設定ファイルでは、`provider` ごとに接続を1つ作り、同じ `provider` のエージェントで共有します。コードで組む場合も、提供元を混ぜる時だけインスタンスを分けてください。

`phase_overrides` で別の接続を使う phase の出力は、互いに思考を送り返しません（エラーにはなりません）。

SDK のクライアントは、最初に生成する時に作ります。設定を組み立てるだけ（テストや設定の確認）なら、API キーや接続先は要りません。キーや接続先の誤りに気づくのは、最初の生成の時です。渡した引数だけで分かる誤り（Azure で `api_key` と `token_provider` を両方渡した、Vertex AI で `project_id` が無い）は、作った時点でエラーになります。

自作の LLM で SDK のクライアントを使うなら、`_connect()` でクライアントを作って返すと、同じように最初の生成まで遅らせられます。`self.client = ...` と直接入れても動きます。

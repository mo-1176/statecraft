# 用語集

statecraft の文書とコードに出てくる、独自の言葉と、意味を絞って使っている言葉です。五十音順ではなく、関係するものを近くに並べています。

---

## 全体の構成

| 用語 | 意味 |
|---|---|
| **Network** | 1リクエスト分のエージェントの入れ物。共有の記憶を1つ作って全員に配り、委譲先の名前を実体へ解決し、配線の誤りを構築時に落とす（→ [reference/network.md](reference/network.md)） |
| **Agent** | 記憶を持つエージェント。記憶を書きながら「実行 → 書き起こし → 次を決める」を繰り返す |
| **ReflexAgent** | 記憶を持たないエージェント。全ツールを提示して自分で選ばせる、古典的な ReAct ループ。生成回数が少ない。窓口や審査役に向く |
| **Tool** | エージェントから呼べる Python 関数。決まった形の辞書を返す（→ [reference/tool.md](reference/tool.md)） |
| **SchemaTool** | 引数の JSON Schema を、型ヒントから作らずに手で渡す Tool。設定ファイルや MCP から作る時に使う |
| **Invokable** | 「LLM から呼べるもの」の型（Protocol）。Tool と Agent が同じ形（`execute(kwargs=...)` → `ToolResult`）で呼べる |
| **ToolResult** | Tool や委譲の実行結果。成功でも失敗でも必ずこの形で返り、例外は外へ出さない |
| **Interceptor** | ループの外から観測し、実行・生成・記憶の更新を止め、失敗した後の扱いを決める仕組み（→ [reference/interceptor.md](reference/interceptor.md)） |
| **BaseLLM** | LLM の提供元への接続の基底クラス。モデル名は持たず、生成のたびに受け取る |
| **NetworkBuilder** | 設定ファイル（YAML / Markdown）から Network を組み立てるもの |

## 配置（ハブとスポーク）

| 用語 | 意味 |
|---|---|
| **窓口（front）** | 利用者と話し、依頼を構造化して統括へ渡す。調べない |
| **統括（orchestrator）** | 記憶を持ち、知識の取得と全体の判断を担う |
| **専門家（specialist）** | 渡された材料で、専門性や創造性が要る作業だけをする。知識は持たない |
| **委譲** | エージェントが別のエージェントを呼ぶこと。呼ばれた側から見ると `execute()` での起動になる |
| **審査役・監査役** | Interceptor から呼ぶ、外に置いたエージェント。監視される側の Network には入れない（→ [concepts/network-placement.md](concepts/network-placement.md)） |

## 1回の応答の中

| 用語 | 意味 |
|---|---|
| **phase** | 1回の生成が、LLM に何をさせている瞬間か。`INITIAL_MEMORY` / `FUNCTION_CALL` / `MEMORY_UPDATE` / `ANSWER` / `CUTOFF_ANSWER` / `STALLED_ANSWER` の6つ（→ [concepts/phases-and-prompts.md](concepts/phases-and-prompts.md)） |
| **ステップ** | ループの1周。「実行 → 記憶の更新」が1ステップ。上限は `max_steps` |
| **初期ツール（initial_tools）** | ループの前に、LLM に選ばせずシステムが実行する tool。毎回必ず要る手順だけを置く |
| **実行履歴** | その起動の中で呼んだ tool / agent の一覧。Agent では、結果の本文は書き起こした後に隠れる |
| **停滞** | 直前のステップと全く同じ実行（名前・引数・エラー）が繰り返されること。1回目は立て直しを促し、続けば打ち切る |
| **打ち切り** | `max_steps` を使い切ること。そこまでの情報で回答する（`CUTOFF_ANSWER`） |
| **思考の継承（carry_thoughts）** | 前の生成の思考を、次の生成へ引き継ぐこと。過去の生成を思考の署名ごと会話として送り返す |
| **run_id** | `respond()` / `execute()` 1回ごとに発行される起動の ID。並列の委譲のイベントを紐づけるのに使う |

## 記憶

| 用語 | 意味 |
|---|---|
| **memory（記憶）** | エージェントの思考そのもの。ステップをまたいで引き継がれる唯一のもの（→ [concepts/memory.md](concepts/memory.md)） |
| **SharedMemory** | 同じ Network の全エージェントが、同じ実体を読み書きする記憶。ホワイトボード |
| **PrivateMemory** | エージェントごとの記憶。`goals` / `tasks` / `task_notes` |
| **フィールド（プロパティ）** | 記憶の項目。`facts`、`ontology`、`tasks` など |
| **差分（diff）** | LLM が記憶へ書く時に返す、行の配列。`[{"field": ..., "id": ..., "text": ...}]` |
| **upsert** | 同じ id の項目があれば差し替え、無ければ追記すること。記憶を消す経路は無い |
| **システム所有のフィールド** | システムだけが書く `requests` / `conversation` / `agent_answers`。LLM がここへ書いた行は捨てられる |
| **description / how_to** | 各フィールドの説明。description は「何を入れる場所か」（全phaseに載る）、how_to は「どう書くか」（記憶を書く phase だけに載る） |
| **書き起こし** | 実行結果の中身を、要約ではなく記憶へ写すこと。結果はその生成が終わると消えるため |
| **tasks** | 次に何を実行するかの、システムへの指示。`target_names` に書いた tool / agent だけが実行される |
| **status** | タスクの状態。`next` / `next_parallel` / `conditional` / `done` / `unnecessary` |
| **target_names** | そのタスクで呼ぶ tool / agent の名前 |
| **DiffError** | 差分の1行の失敗。例外にせず集め、失敗した行だけを差し戻す |
| **errors / ignored** | 記憶に入らなかった行のうち、差し戻して直させるもの（errors）と、直せないので次のステップで1回だけ伝えるもの（ignored） |
| **引き継ぎ（carry over）** | 共有の記憶の一部を、次のターンへ持ち越すこと。何を持ち越すかは利用側が決める |
| **preload** | Network を作る時に、共有の記憶へ最初から入れておく内容 |

## 依頼と会話

| 用語 | 意味 |
|---|---|
| **requests** | 最初の委譲で構造化された依頼。全エージェントの解釈の基準。以降の委譲では上書きされない |
| **conversation** | 利用者との会話の原文。`respond()` が記録する。`turn-1` から古い順に並び、最後に利用者の発言だけを抜いた `user-only` がある |
| **user-only** | 利用者の発言だけを古い順に並べたもの。窓口は依頼の中身をここを軸に読む |
| **agent_answers** | 委譲先が返した回答の記録 |

## 渡す文章

| 用語 | 意味 |
|---|---|
| **system_instruction** | エージェントの人格・役割の定義 |
| **knowledge** | 全 phase・全ステップに載る文書（→ [concepts/knowledge.md](concepts/knowledge.md)） |
| **initial_knowledge** | 初期の記憶を作る生成にだけ載る文書。タスクの立て方の例など |
| **answer_knowledge** | 最終回答を作る生成にだけ載る文書。答え方の方針など |
| **summary / usage / evaluation** | tool / agent の説明の3段。summary は常時、usage は使う本人が計画・実行する時、evaluation は結果が返った時に1回だけ載る |
| **input_schema / output_schema** | エージェントが受け取る依頼の形 / 返す最終回答の形（JSON Schema） |
| **POURING** | 全 phase に流し込む共通の文言。`common`（仕組みと進め方）と `thinking`（思考の方向づけ） |
| **TRANSCRIPTION_RULES** | 記憶を書く phase に載る、書き起こしの規則 |
| **COMPLETION_CRITERIA** | 記憶を書く phase の最後に載る、調べるのを止めるか続けるかの判断の形 |
| **STALL_NOTICE** | 停滞を検出した時に、記憶の更新へ差し込む注意文 |

## Interceptor

| 用語 | 意味 |
|---|---|
| **notice** | 起きたことを受け取るだけの窓口。何を返しても実行は止まらない |
| **guard** | 実行・生成・記憶の更新を止められる窓口 |
| **recover** | 生成が失敗した後に、再試行するかを決める窓口 |
| **判定の型** | `ExecuteDecision` / `GenerateDecision` / `MemoryDiffDecision` / `RetryDecision`。guard と recover はこの型で返す |
| **review_round** | 記憶の更新を監査に見せた何回目か。監査で差し戻すたびに増える |
| **attempt** | 何回目の試行か。形式の誤りで差し戻した回を含む |

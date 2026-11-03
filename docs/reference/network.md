# Network

1リクエスト分のエージェントの入れ物です。実装は `src/statecraft/network.py` にあります。エージェントをどう並べるかの考え方は [concepts/network-placement.md](../concepts/network-placement.md) にあります。

```python
net = Network(agents=[front, librarian], interceptor=ic)   # コードで組む
net = MyBuilder.from_dir(ROOT, ic).build()                   # 設定ファイルから

response = net.respond(agent="front", message="...")
```

---

## 担うこと

Network が持つのは、エージェント単体では決められない**配線**だけです。

1. `SharedMemory` を1つ作り、全エージェントへ同じ実体を渡す
2. `sub_agent_names`（名前）を、実体へ解決する
3. Interceptor を全エージェントへ配る（エージェントが自分の Interceptor を持っていても、Network のものに差し替える。残すと、そのエージェントにだけ観測や費用の上限が効かなくなるため）
4. Tool を複製する（あるエージェントの無効化が、他のエージェントや次のリクエストへ波及しないように）
5. 配線の誤りを、構築した時点で落とす
6. `enable_auditor` を渡せば、組み込みの監査役を全エージェントの記憶の更新へ繋ぐ

LLM の接続は配りません。接続先はエージェントごとに違うのが前提です。

Network の寿命が、共有の記憶の寿命（セッション）になります。

---

## 引数

| 引数 | 中身 |
|---|---|
| `agents` | 入れるエージェント。1つ以上。名前の重複はエラー |
| `interceptor` | 全エージェントへ配る Interceptor。省略すると空のもの |
| `preload` | 共有の記憶へ最初から入れておく内容。`{"facts": ["...", ...]}` の形 |
| `enable_auditor` | 組み込みの監査役の設定。省略すると監査しない（下） |

---

## 監査役を付ける — enable_auditor

```python
net = Network(
    agents=[front, librarian],
    enable_auditor={
        "llm": OpenAILLM(),                   # 必須
        "model": "gpt-6-luna",                # 必須
        "thought_level": ThoughtLevel.LOW,    # 任意
        "system_instruction": "…",            # 任意。既定のプロンプトの後ろにそのまま付く
    },
)
net = MyBuilder.from_dir(ROOT, ic).build(enable_auditor={...})   # 設定ファイルからでも同じ
```

記憶を持つ全エージェントの記憶の更新を、当てる前に監査役へ見せます（仕組みは `guard.before_memory_diff`。→ [interceptor.md](interceptor.md)）。実装と既定のプロンプトは `src/statecraft/auditor.py` にあります。

- 2回差し戻したら、次の差分は監査せずに通します。監査役が失敗した時、判定を読めなかった時も通します。
- 監査役は、監査のたびに作り直します。並列に動くエージェントの監査を、順番待ちさせないためです。
- 監査役の生成も、この Network の Interceptor を通ります。消費は `token_usage()` に `auditor` として入ります。
- 監査役は `agents` には入りません（共有の記憶は配られず、委譲先にもなりません）。

---

## 構築時に落とすもの

実行してから気付くのではなく、Network を作った時点でエラーにします。

- 委譲先の名前が解決できない、自分自身を指している
- `sub_agent_names` と `sub_agents`（実体の直接指定）を同時に指定している
- `sub_agents` や `initial_tools` に置いた実体が、`agents` に入っていない（共有の記憶が繋がらない）
- 委譲が循環している（初期ツールに置いたエージェントも辿る）
- `initial_tools` に同じ対象が重複している
- `initial_tools` の対象が必要とする引数が、自分の `input_schema` に無い
- `phase_overrides` で接続（`llm`）だけを上書きして、`model` を書き忘れている
- `phase_overrides` に `CUTOFF_ANSWER` / `STALLED_ANSWER` を書いている（答える時はいつも `ANSWER` の設定を使う）
- `ReflexAgent` の `phase_overrides` が、通らない phase（`INITIAL_MEMORY` / `MEMORY_UPDATE`、tool を持つ場合の `ANSWER`）を指している
- `preload` のフィールドが無い、または配列でない
- `enable_auditor` が dict でない、知らない項目がある、`llm` か `model` が無い
- `enable_auditor` を渡したのに、`auditor` という名前のエージェントがいる（組み込みの監査役の名前）

---

## 使い回す — reset()

エージェントをモジュールの階層で作ってプロセスを生かし続ける構成（サーバーなど）では、前のリクエストの記憶とトークンの累計が残ります。リクエストの手前で `reset()` を呼びます。**自動では走りません。** リクエストの区切りを知っているのは利用側だけだからです。

```python
net.reset()
net.shared_memory.ontology = carried   # 引き継ぎを入れるのは reset() の後
net.respond(agent="front", message=...)
```

| 消えるもの | 残るもの |
|---|---|
| 共有の記憶（作り直して全員へ配り直す） | 配線と、構築時の検証の結果 |
| 各エージェントの個別の記憶 | エージェントの設定（`model`、`system_instruction`、登録した tools など） |
| トークンの累計 | 設定としての `disabled` |
| セッション中に行われた無効化 | |
| 起動ごとの状態（直前の依頼、実行履歴、周回数、抱えた添付、継承する思考） | |

`reset()` は共有の記憶を**差し替える**ので、呼ぶ前に取っておいた参照は古い実体を指したままになります。呼んだ後に `net.shared_memory` から取り直してください。

リクエストごとに Network を作り直す構成なら、`reset()` は要りません。サンプルは作り直す側で、設定も毎回読み直すので、プロンプトを直すと次のリクエストから効きます。

---

## 使う

| | 中身 |
|---|---|
| `net.respond(agent="name", message=..., history=..., blobs=...)` | 利用者からの依頼を、名前で指したエージェントへ渡す。返るトークン数は、委譲先と監査役を含むこの依頼の全体（`agent.respond()` は、そのエージェント自身の分だけ。単体で動かす時やテスト向け） |
| `net["name"]` / `net.get("name")` | エージェントを名前で取る。無ければ `KeyError` |
| `net.shared_memory` | 共有の記憶の実体 |
| `net.total_tokens()` | Network 全体の `(入力, 出力)` |
| `net.token_usage()` | エージェントごとの `{"name": (入力, 出力)}`。監査役を付けていれば `auditor` も入る |

---

## 渡すと共有の記憶が入る

`agents` に渡したエージェントには、共有の記憶と Interceptor が注入されます。逆に、共有の記憶を持たせたくないエージェント（審査役・自分で作る監査役）は渡しません。組み込みの監査役（`enable_auditor`）は、Network が `agents` の外に置きます。

設定ファイルでは、1つの `agents.yaml` に書いたエージェントが全員同じ Network に入ります。共有の記憶を分けたいエージェントは、別のディレクトリに設定を置いて、別の Network として組み立てます。

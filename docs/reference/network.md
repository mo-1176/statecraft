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
7. 依頼を起動の表（`runtime.py`）で進め、中断した依頼の保存・復元・再開の入口を持つ（下の「中断と再開」）

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
| `net.respond(agent="name", message=..., history=..., blobs=...)` | 利用者からの依頼を、名前で指したエージェントへ渡す。返るトークン数は、委譲先と監査役を含むこの依頼の全体（`agent.respond()` は、そのエージェント自身の分だけ。単体で動かす時やテスト向け）。途中で中断したら `Suspended` が返る |
| `net.resume(answers={point: 答え})` | 中断した依頼を続きから進める。戻り値は `respond()` と同じ |
| `net.save_state()` / `net.load_state(state)` | 中断した依頼の状態を、JSON にできる dict で取り出す／戻す（戻すだけで動かさない） |
| `net.pending` | 中断している地点（`Pending`）の一覧。中断していなければ空 |
| `net["name"]` / `net.get("name")` | エージェントを名前で取る。無ければ `KeyError` |
| `net.shared_memory` | 共有の記憶の実体 |
| `net.total_tokens()` | Network 全体の `(入力, 出力)` |
| `net.token_usage()` | エージェントごとの `{"name": (入力, 出力)}`。監査役を付けていれば `auditor` も入る |

---

## 中断と再開 — suspend / save_state / load_state / resume

tool の本体か、`before_execute` のコールバックで `suspend(payload)` を呼ぶと、依頼全体がそこで止まり、`respond()` は `Suspended` を返します。利用者の返事を待つ（ヒューマンインザループ）、承認を取る、途中の状態から特定のエージェントだけを試す、といった用途です。

```python
from statecraft import Suspended, suspend

def identify_title(keyword: str) -> dict:
    found = search_titles(keyword)
    suspend({"question": "どれのことですか？", "options": found})   # 利用者に選んでもらう
    return {"value": ""}                                           # ここへは来ない
```

```python
result = net.respond(agent="front", message="『銀河鉄道』を予約したい")
if isinstance(result, Suspended):
    [pending] = result.pending               # pending.payload を画面へ出す
    store.put(key, net.save_state())         # 保存先は利用側が決める（DB・Blob など）

# 返事が来たら（別のプロセスでもよい）
net = build_network()                        # 同じ名前のエージェントと tool で組む
net.load_state(store.get(key))               # 戻すだけ。まだ動かない
result = net.resume(answers={pending.point: "銀河鉄道の父"})
```

同じプロセスで続けるなら、`save_state()` / `load_state()` を挟まずに `resume()` を呼べます。

### 答えの扱いは、どこで止まったかで変わる

| `Pending.at` | 止まった所 | `resume()` で答えを渡すと | 答えなかったら |
|---|---|---|---|
| `"tool"` | tool の本体 | 答えがそのまま tool の結果になる。tool は実行し直さない（`ToolResult` を渡せば、失敗や `memory` も渡せる） | tool を実行し直す |
| `"before_execute"` | `before_execute` のコールバック | 同じ呼び出しを判定し直し、そこで呼んだ `suspend()` が答えを返す | 判定し直す |

承認なら `before_execute` で止めます。

```python
def confirm(e):
    if e.name != "issue_reservation":
        return ExecuteDecision(ok=True)
    answer = suspend({"question": "予約してよいですか？", "options": ["はい", "いいえ"]})   # 再開すると、ここが答えを返す
    return ExecuteDecision(ok=answer == "はい", reason="利用者が取りやめました")
```

サンプル（`examples/`）では、候補を選んでもらう・足りない情報を尋ねる・実行前の承認の3つを、CLI・対話・Web API で動かしています（→ [sampleの読み方.md](../sampleの読み方.md#人に尋ねる中断と再開)）。

### 何が保存されるか

保存するのは、実行して変わったものだけです。設定（人格定義・プロンプト・tool・LLM の接続）は含めません。

- 共有の記憶、各エージェントの個別の記憶
- 各エージェントの起動の途中の状態（実行履歴、周回数、停滞の数え、無効化、抱えた添付）
- 継承する思考（`carry_thoughts`。プロバイダの出力は `dump_reply()` で保存できる形にする）
- 起動の表（どの起動が、どのバッチの何番目の結果を待っているか。中断までに終わった呼び出しの結果）
- トークンの累計と、イベントに付ける集計

読み込む側の Network は、**エージェントと tool の名前を揃えて**組みます。人格定義やプロンプトは変えてかまいません（変えた後の設定で続きが動きます）。途中の状態から1体だけを試すのに使えます。

### 決まりごと

- 中断できるのは、tool の本体と `before_execute` のコールバックだけです。生成や記憶の更新の途中（`before_generate` など）で呼ぶとエラーになります。通知（`notice`）のコールバックで呼んでも止まりません（ログに出るだけ）。
- 並列に走っていた他の呼び出しは、終わるのを待ってから止まります。終わった呼び出しは、再開しても実行し直しません。
- 2か所以上で同時に止まったら、`pending` に全部入ります。答えなかった地点は実行し直すので、また止まることがあります。
- tool の戻り値と `suspend()` に渡す値は、JSON にできる形にします（`bytes` は自動で base64 にします）。できない値が入っていると、`save_state()` が `TypeError` になります。
- エージェントを単体で動かした時（`agent.respond()` / `agent.execute()`）は中断できません。保存して再開する入口が Network にしか無いためです。
- 仕組みと、なぜこの形にしたかは [ADR 0004](../adr/0004-runs-as-table.md) にあります。

---

## 渡すと共有の記憶が入る

`agents` に渡したエージェントには、共有の記憶と Interceptor が注入されます。逆に、共有の記憶を持たせたくないエージェント（審査役・自分で作る監査役）は渡しません。組み込みの監査役（`enable_auditor`）は、Network が `agents` の外に置きます。

設定ファイルでは、1つの `agents.yaml` に書いたエージェントが全員同じ Network に入ります。共有の記憶を分けたいエージェントは、別のディレクトリに設定を置いて、別の Network として組み立てます。

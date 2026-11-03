# Interceptor

ループの外から観測し、実行・生成・記憶の更新を止め、生成が失敗した後の扱いを決める仕組みです。実装は `src/statecraft/interceptor.py`、イベントの型は `src/statecraft/events.py` にあります。

各イベントの項目の一覧と、書き方の例は、README の「Interceptor」にまとまっています。ここでは、全体の形と、既定の向きをまとめます。

```python
from statecraft import Interceptor

ic = Interceptor()
ic.notice.execute_start(lambda event: print(event.message))
ic.guard.before_execute(guard)
ic.recover.generation_failed(retry_on_overload)

net = MyBuilder.from_dir(ROOT, ic).build()
```

何も登録しなければ、何も起きません。Interceptor を渡さなくても動きます。

---

## 窓口は3つ

コールバックが**何を返すか**で、登録する窓口が分かれています。

| 窓口 | 何をするか | 返すもの | 登録が無い時 |
|---|---|---|---|
| `notice` | 起きたことを受け取るだけ | なし（返しても見ない） | 何も起きない |
| `guard` | 実行・生成・記憶の更新を止められる | 判定の型 | 通す |
| `recover` | 失敗した後にどうするかを決める | 判定の型 | 再試行しない |

**観測だけをしたい関数は、必ず `notice` に登録してください。** `guard` に置くと、返し方を誤った時に実行が止まります。止められた呼び出しも、`notice.execute_blocked` で受け取れます。

---

## イベント

どのコールバックも、イベントの型の値を1つだけ受け取ります。型の名前はイベントの名前に揃えてあります（`execute_start` → `ExecuteStartEvent`）。

| イベント | 窓口 | いつ |
|---|---|---|
| `before_execute` | guard | tool・エージェントを実行する直前（委譲も対象） |
| `before_generate` | guard | LLM へリクエストを投げる直前 |
| `before_memory_diff` | guard | LLM が作った記憶の差分を、記憶へ当てる直前 |
| `generation_failed` | recover | 生成がエラーで終わった時 |
| `execute_start` / `execute_end` | notice | 実行を始めた時 / 終えた瞬間（呼ばれた側から） |
| `execute_blocked` | notice | 実行が止められた時 |
| `executed` | notice | 実行1件の記録（同じバッチが全部終わった後に、呼んだ側から、呼び出しの順に） |
| `generated` | notice | 生成が1回終わった時（失敗した試行も） |
| `memory_diff` | notice | 記憶へ書き込んだ時（失敗した時、監査で差し戻した時も） |
| `memory_updated` | notice | 記憶の更新が終わった時 |
| `respond_start` / `respond_end` | notice | `respond()` の開始・終了 |
| `error` | notice | 処理は続くが、記録すべき異常が起きた時 |

`message` は人が読むための1行です。機械で扱う時は、`message` 以外の項目（名前・引数・成否・トークン数）を読んでください。

---

## どのイベントにも付く、その時点の状態 — event.network

Network に入れて動かすと、どのイベントにも `network`（`NetworkState`）が付きます。観測する側が Network の中を読みに行かなくて済むようにするためのものです。イベントが起きた瞬間の写しなので、並列に動く他のエージェントが後から書き換えても、受け取った値は変わりません。Agent を単体で動かした時は `None` です。

| 項目 | 中身 |
|---|---|
| `memory` | 共有の記憶。エージェントが読んでいるのと同じ文の形 |
| `elapsed` | `net.respond()` を始めてからの秒数 |
| `input_tokens` / `output_tokens` / `cached_tokens` / `cache_write_tokens` | 全体の合計（監査役を含む） |
| `agents["名前"]` | エージェントごとの `AgentState`（監査役は `auditor`） |

`AgentState` には、そのエージェント自身の記憶（`memory`。記憶を持たない ReflexAgent と監査役は空）、トークン、生成にかかった時間の合計（`seconds`）、ステップごとの時間（`steps`）が入ります。`steps` は起動とステップの順に並ぶ `StepTime` で、`seconds` に phase ごとの秒数を持ちます（作り直しと失敗した試行の時間も含みます）。

```python
def on_generated(e: GeneratedEvent) -> None:
    for step in e.network.agents["scout"].steps:
        print(step.step_no, {phase.value: round(s, 1) for phase, s in step.seconds.items()})
        # 1 {'initial_memory': 3.2, 'function_call': 1.1}
        # 2 {'memory_update': 4.0, 'function_call': 0.9}
```

## 判定の型と、既定の向き

guard と recover は、**決まった型で返します。** `True` や文字列では返せません（返すと下の表の向きに倒れ、正しい返し方がログに出ます）。

| イベント | 返す型 | 通す時 | 止める時 | 型の違う値・例外 |
|---|---|---|---|---|
| `before_execute` | `ExecuteDecision` | `ok=True` | `ok=False`（`reason` は LLM へ渡る） | **拒否**（理由は渡らない） |
| `before_generate` | `GenerateDecision` | `ok=True`（`model` で切り替えも） | `ok=False` → `GenerationAborted` | **中止** |
| `before_memory_diff` | `MemoryDiffDecision` | `ok=True` | `ok=False`（`reason` を添えて直させる） | **その監査を飛ばす** |
| `generation_failed` | `RetryDecision` | `ok=True`（`model` で切り替えも） | `ok=False` | 型の違う値は「再試行しない」、例外は「諦める」 |

向きが窓口ごとに違う理由は、[concepts/failure-handling.md](../concepts/failure-handling.md#判定できなかった時の倒し方) にあります。

### 複数登録した時

| イベント | 扱い |
|---|---|
| `before_execute` | 全員が許可した時だけ実行する。登録順に呼び、1つが拒否した時点で後続は呼ばない |
| `before_generate` | `ok=True` だけなら後ろの登録にも尋ねる。`model` を返した時点でそのモデルに決まる |
| `before_memory_diff` | 通したら後ろの監査にも尋ねる |
| `generation_failed` | `ok=False` なら後ろの登録に尋ねる。例外なら、後ろにも尋ねずに諦める |

### 同時に呼ばれることがある

`before_generate` と `generation_failed` は、中で待つ処理を書けるように、他のコールバックと足並みを揃えていません。並列に動くエージェントから同時に呼ばれることがあるので、カウンタのような値を触る場合は、利用側で保護してください。監査（`before_memory_diff`）も直列化されません。

---

## 何をどこで止めるか

| やりたいこと | 使うもの |
|---|---|
| 危ない実行を止める | `guard.before_execute` |
| 予算の上限を効かせる、軽いモデルへ落とす | `guard.before_generate` |
| 一時的な過負荷（503）で待って再試行する、枠切れ（429）で別のモデルへ逃がす | `recover.generation_failed`（`event.kind` が `"unavailable"` / `"rate_limit"` / `"other"` で見分ける） |
| 記憶の更新の向き（やりすぎ・早すぎる打ち切り・脱線）を監査する | `guard.before_memory_diff`（組み込みの監査役で足りるなら、Network の `enable_auditor`。→ [network.md](network.md#監査役を付ける--enable_auditor)） |
| 進捗を画面へ出す | `notice.execute_start` |
| コストと時間を測る | `notice.generated` |
| 誰が何を呼んで何が返ったかを記録する | `notice.executed` |
| 記憶の作られ方を検証する | `notice.memory_diff` |
| 並列の委譲の、呼び出しの木を組む | `executed.callee_run_id` と各イベントの `run_id` |

### 機械的に塞げるものは、機械的に塞ぐ

LLM に審査させる方法だけに頼るのは危険です。審査役も LLM なので、言い方を変えれば通ることがあります。

| 塞ぎたいもの | やり方 |
|---|---|
| そのツールを使わせない | `disabled` にする。選択肢として提示されない |
| 引数の形式 | `before_execute` の中で判定する（正規表現、範囲、許可リスト） |
| 実在するか | ツール本体で確かめる。形式のチェックは、形を真似た値を通す |
| 権限・所有者 | ツール本体で確かめる。LLM の判断に委ねない |
| 実行できる時間帯・回数 | `before_execute` の中で外部の状態を見る |
| 取り返しがつかない操作 | 人の承認を挟む |

LLM の審査が効くのは、機械的な条件では書けないものだけです。上の表で塞げるものを塞いだ後、残りに重ねる層として使ってください。審査役・監査役は、監視される Network に入れません（→ [concepts/network-placement.md](../concepts/network-placement.md#network-の外に置くもの)）。

---

## 0.5.0 からの移行

0.6.0 で、登録の窓口を `ic.on` から `notice` / `guard` / `recover` に分け、コールバックがイベントの型を1つ受け取る形に変えました。対応表は README の「0.6.0 での書き換え」にあります。

## 判定の型の項目名の変更

判定の型の「はい」の項目を、すべて `ok` にそろえました。

| 型 | 以前 | 今 |
|---|---|---|
| `ExecuteDecision` | `allowed` | `ok` |
| `GenerateDecision` | `proceed` | `ok` |
| `RetryDecision` | `retry` | `ok` |
| `MemoryDiffDecision` | `approved` | `ok` |

`ok=False` の意味は窓口ごとに違うままです（`before_execute` は止める、`before_generate` は中止、`before_memory_diff` は差し戻す、`generation_failed` は「この関数は再試行しない」で後ろの登録へ回す）。

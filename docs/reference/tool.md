# Tool

エージェントから呼べる Python 関数です。実装は `src/statecraft/tools.py` にあります。

---

## 項目

`Agent` と同じく、3つに分かれます。

**① このツール自身の振る舞い**

| 項目 | 中身 |
|---|---|
| `func` | **必須。** ツールの本体。普通の Python 関数 |
| `expose_error_details` | 例外の文面をそのまま LLM へ返すか。既定の `False` では汎用の失敗の文だけを返す（例外には接続先やパスが混ざりうるため） |
| `disabled` | `True` の間は、選択肢として提示されない |

**② 他の AI が読むもの**

| 項目 | 中身 |
|---|---|
| `summary` | **必須。** 何をするものか（1行） |
| `param_descriptions` | 引数ごとの説明。`{引数名: 説明}`。スキーマの `description` に入る |
| `usage` | 使い方。スキーマで表せない制約 |
| `evaluation` | 結果の読み方 |

**③ 実行を観測するためのもの**

| 項目 | 中身 |
|---|---|
| `execution_message` | 実行中に `execute_start` へ流す文言。`{引数名}` で差し込める |
| `describe_execution` | 引数からその文言を組み立てる関数。`execution_message` より優先 |

ツールの名前は、関数名です（`SchemaTool` では `tool_name` で変えられます）。

---

## summary / usage / evaluation

3つとも文言ですが、**誰に、いつ渡るかが違います。**

| | 誰に | いつ |
|---|---|---|
| `summary` | 全員（親のエージェントにも） | 常時 |
| `usage` | そのツールを持つエージェントだけ | 計画する時（`INITIAL_MEMORY` / `MEMORY_UPDATE` の一覧）と、呼び出し候補として API へ渡す時 |
| `evaluation` | 結果を受け取ったエージェント | 結果が返った時に、結果と一緒に1回だけ |

```
librarian（tool を持っている側）が見るもの
  - check_stock: 書名の所在と貸出状況を調べる
    使い方: 書名は完全一致で渡すこと。部分一致では見つからない   ← usage

front（librarian へ依頼を送る側）が見るもの
  - librarian: 蔵書と規程を調べる
      - check_stock: 書名の所在と貸出状況を調べる               ← summary だけ
```

`summary` は全員に常時載るので短く、`usage` は使う本人にだけ渡るので長く書けます。結果の読み方を人格定義へ書かず、そのツールの `evaluation` に置いてください（→ [ADR 0002](../adr/0002-own-tool-loop.md)）。

---

## 引数のスキーマ

| 作り方 | どうなるか |
|---|---|
| `Tool(func=...)` | 関数の型ヒントから自動で作る。既定値のある引数は `required` から外れる |
| `SchemaTool(func=..., parameters={...})` | 渡した JSON Schema をそのまま使う |

型ヒントからは表せない形（`TypedDict` の配列、`Literal` の enum など）がある場合と、設定ファイルや MCP のようにスキーマが外から来る場合は、`SchemaTool` を使います。スキーマと関数の引数を食い違わせないのは、渡す側の責任です。`SchemaTool` の `tool_name` を使うと、同じ関数から名前の違うツールを何本でも作れます。

---

## 戻り値の約束

**すべてのツールで、この形の辞書を返します。**

| キー | 型 | 中身 |
|---|---|---|
| `value` | `str` | **必須。** LLM へ返す結果 |
| `blobs` | `list[dict]` | LLM へ見せる添付。`[{"data": bytes, "mime_type": str}, ...]` |
| `memory` | `dict[str, list[str]]` | 記憶へ直接書く内容。`{"vars": ["受付番号: ..."]}` |

形が違う場合（辞書でない、`value` が無い、キーの綴り違い）は、そのツールの実行が失敗したものとして扱い、何がおかしいかを LLM へ返します。

- **`blobs`** は、結果を評価する生成で1回だけ LLM へ渡ります。記憶へ書き起こされた後は載りません（`ReflexAgent` では渡り続けます）。
- **`memory`** は、LLM の要約を通さずに記憶へ書きます。id はシステムが `tool名-フィールド名-連番` で採番します。書き込み先のフィールドが無くても実行は止まらず、`error` イベントで知らせます。記憶へ書いた結果は、実行履歴では「※結果はmemoryへ記録済み」になり、中身が二重に載りません。

---

## 実行の流れ

`Tool.execute(kwargs=, interceptor=)` は、例外を投げず、必ず `ToolResult` を返します。

1. `disabled` なら、すぐに拒否する
2. `before_execute` に尋ね、拒否されたら理由を結果にする（`execute_blocked` を流す）
3. `execute_start` を流す（文言の組み立てに失敗しても、実行は止めない）
4. 関数を呼び、戻り値の約束を確かめる
5. 例外が出たら、観測側（`error` イベント）には実際の例外を、LLM には `expose_error_details` に従った文を渡す

ループの側では、さらに次のことをします（→ [agent-loop.md](agent-loop.md)）。

- 必須の引数が空なら、呼ばずに失敗の結果を記録する
- 直前のステップで成功した呼び出しと全く同じなら、呼ばずに拒否する
- 1回の生成で複数の呼び出しが返ったら、関数を同時に呼ぶ。外部の状態を書き換えるツールを並列で呼ばせる場合は、利用側で保護する

---

## 粒度

判断の基準は、**間に LLM の判断が要るかどうか**です。

| | 例 |
|---|---|
| まとめる | SQL を実行して結果をシートへ書く（間に判断が入らない） |
| 分ける | 検索して候補を出す → 選んだ候補で本処理（結果を見て次が変わる） |

判断が要らないなら、1つにまとめた方が生成の回数が減ります。

---

## Invokable

`Tool` と `Agent` は、同じ形で呼べます。

```python
tool.execute(kwargs={...}, interceptor=ic)    # → ToolResult
agent.execute(kwargs={...}, interceptor=ic)   # → ToolResult
```

この形を `Invokable`（Protocol）として定義しています。`class Agent(Tool)` にしていないのは、「エージェントはツールの一種」という成り立たない関係を宣言することになるためです。呼ぶ側から見ると、委譲先をツールに差し替えても、書き方は変わりません。

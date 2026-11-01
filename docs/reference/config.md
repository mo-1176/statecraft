# 設定ファイル（早見表）

エージェントとツールの定義を、コードの外の YAML と Markdown に書けます。詳しい書き方と例は README の「設定ファイルから組み立てる」にあります。ここは早見表です。実装は `src/statecraft/config.py`（読み込みと検証）と `src/statecraft/builder.py`（組み立て）にあります。

---

## フォルダの形

```
<root>/
  config/
    agents.yaml        # エージェント（必須）
    tools.yaml         # ツール（使わなければ無くてよい）
    knowledges.yaml    # ナレッジの名前とファイル（使わなければ無くてよい）
  prompts/
    agents/<エージェント名>/<項目>.md
    tools/<ツール名>/<項目>.md
  knowledge/
    <名前>.md
```

コードに残るのは、ツールの実装と、`type` とその実装の対応だけです。

---

## agents.yaml

項目は `Agent` の同じ名前の引数にあたり、書かなかった項目は `Agent` の既定値になります。違うのは次の項目です。

| 項目 | 中身 |
|---|---|
| `type` | `agent`（既定）か `reflex` |
| `provider` | LLM の提供元の名前（`gemini` / `openai` / `azure_openai` / `claude`）。実体は `LLM_FACTORIES` で作る |
| `thought_level` | `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` |
| `phase_overrides` | キーは phase の小文字（`initial_memory` など）。中身は `model` / `provider` / `temperature` / `max_tokens` / `thought_level` |
| `tools` / `initial_tools` | tools.yaml のツール名 |
| `sub_agents` | 委譲できるエージェントの名前 |
| `knowledge` / `initial_knowledge` / `answer_knowledge` | knowledges.yaml のナレッジ名。書いた順につなげて渡す |
| `carry_thoughts` | 思考の継承。Claude で動かすなら `false` |

設定ファイルでは書けないもの: `disabled`、`describe_execution`、`initial_tools` に置くエージェント。これらが要る場合は、コードで組みます。

**つながりは、使う側のエージェントにだけ書きます。** tools.yaml や knowledges.yaml には、どのエージェントが使うかを書きません。同じ関係を2か所に書くと、片方だけ直した時に食い違うためです。

## tools.yaml

| 項目 | 中身 |
|---|---|
| `name` | LLM に見せるツール名 |
| `type` | 実装の種類。`TOOL_FACTORIES` のキー |
| `settings` | 実装に渡す値。説明文の `{名前}` も、ここの値で埋まる |
| `parameters` | 引数の JSON Schema。型ヒントからは作らず、書いたものをそのまま使う（`SchemaTool` になる） |
| `summary` / `usage` / `evaluation` / `execution_message` | Tool の同じ名前の項目 |

`name` と `type` が分かれているので、同じ実装から `settings` だけ違うツールを何本でも作れます。`execution_message` の `{名前}` は埋めずに残し、実行時にツールの引数で埋まります。

## knowledges.yaml

```yaml
- name: terms
  file: knowledge/terms.md      # root からのパス
```

## 長い文章は Markdown に

`system_instruction`（エージェントだけ）、`usage`、`evaluation`、`execution_message` は、`prompts/<agents か tools>/<名前>/<項目>.md` に置けます。置き場所が決まっているので、YAML から指す必要はありません。同じ項目が YAML と md の両方にあるとエラーになります。

---

## 組み立て

```python
from statecraft import NetworkBuilder


class MyBuilder(NetworkBuilder):
    # type → ツールの関数を返すもの
    TOOL_FACTORIES = {
        "search_rules": lambda: tools.search_rules,      # settings を持たないもの
        "sample_tool": tools.make_sample_tool,           # settings をキーワード引数で受け取るもの
    }
    # 提供元を足す・繋ぎ方を変える時だけ（同じ名前なら上書き）
    LLM_FACTORIES = {**NetworkBuilder.LLM_FACTORIES, "azure_openai": make_azure_llm}


net = MyBuilder.from_dir(ROOT, interceptor).build()
```

- `from_dir` は呼ぶたびに設定を読み直します。
- YAML に関数の場所（モジュール名）を書く形にしていないのは、設定を書き換えられる人が、登録されていない関数を組み立て時に呼べないようにするためです。
- API キーは YAML に書かず、環境変数に置きます。

---

## 読み込み時に落とすもの

YAML は書き間違えても読めてしまうので、次のものは読み込みか組み立ての時点でエラーにします。名前の食い違いは、まとめて出ます。

- 知らない項目名（綴り違い）
- 同じ名前のエージェント・ツール・ナレッジが2つ以上ある
- `tools` / `sub_agents` / `knowledge` などに書いた名前が、指す先に無い
- 同じ文章が YAML と md の両方にある
- `{名前}` に入れる値が `settings` に無い
- ナレッジの `file` が無い
- `type` / `provider` が対応表に無い、`settings` が実装の引数と合わない

これに加えて、Network を作る時の配線の検証が走ります（→ [network.md](network.md#構築時に落とすもの)）。

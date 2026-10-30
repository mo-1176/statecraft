# StateCraft

記憶を構造として扱うマルチエージェントフレームワーク（Python）。
本人がGAS + Gemini APIで構築したAIエージェントの設計思想を、Pythonのフレームワークとして作り直したもの。

## このプロジェクトの位置づけ
- 転職（2026/11/1入社）前に、自分の設計思想を形にしたフレームワークとしてGitHub上で仕上げておくのが目的。
- `src/statecraft/` は **完成扱い**（2026/10/20、ontology の概念導入まで）。新機能の追加は提案しない。バグ修正や、使う側アプリから見て必要になった最小限の変更のみ。
  - 例外: 設定ファイル（YAML / Markdown）からネットワークを組み立てる機能は、本人の判断で入れる（2026/10/24）。転職先で statecraft をフォークして使う可能性があり、フォークの前に入れておきたいため。plugin-scout で作ったものを移す。
- 現在は、statecraft を「使う側」のアプリを別リポジトリで作り、学習に使っている。
- セッションまわり（セッションをまたぐ会話履歴・記憶の引き継ぎなど）は一旦スコープ外。提案や修正をそちらへ広げない。

## 構成
- `src/statecraft/` — 本体（src-layout）
  - `agent.py` 思考ループの中核 / `memory.py` 共有記憶・個別記憶 / `prompts.py` phaseごとのプロンプト
  - `network.py` 1リクエスト分の配線 / `tools.py` ツール定義 / `interceptor.py` 観測と実行前判定
  - `llm.py` プロバイダ実装（Gemini / OpenAI / Codex） / `invokable.py` / `utils.py`
  - `config.py` 設定ファイルの読み込みと検証（pydantic / pyyaml） / `builder.py` 設定からNetworkを組み立てる
- `tests/` — pytest。`conftest.py` の FakeLLM を使い、LLMのAPIは呼ばない
- `examples/` — 動作サンプル（図書館）。`library/` が設定のディレクトリと実装（フォーク先が写す雛形）、入口は `cli.py` / `chat.py` / `api.py`（api は fastapi と uvicorn が要る）。解説は `docs/sampleの読み方.md`
- 公開APIは `src/statecraft/__init__.py` の `__all__` に並べたものだけ

## 開発コマンド（Windows / PowerShell、`.venv` 有効化後）
```powershell
pip install -e ".[dev,gemini]"   # 編集可能インストール
pytest                            # テスト（APIキー不要）
ruff check .                      # Lint
```
CI（`.github/workflows/test.yml`）は push / PR ごとに Python 3.11〜3.14 で pytest と ruff を回す。

## コードの約束事
- コメント・docstring・READMEは日本語。「何をするか」より「なぜそうするか」を書く既存のスタイルに合わせる。
- 依存は増やさない。本体の `dependencies` は pydantic と pyyaml だけ（設定ファイルの読み込み用。2026/10/24 に本人の判断で、依存ゼロの前提を外した）。プロバイダSDKはoptionalのまま。
- `except Exception` はフレームワークの境界（利用者のtool本体やコールバックを呼ぶ箇所）でのみ使い、行ごとに `# noqa: BLE001` を付ける。
- Python 3.11 互換を保つ。対応バージョンを変えるときは pyproject と CI の matrix を揃える。
- 挙動を変えたらテストを足す／直す。READMEの「実装状況」も更新する。

## 進め方
- 設計判断（特にエージェントの思考・記憶の設計）は本人が決める。Codexは選択肢と根拠を出す。
- コードは本人が書きたい部分がある。大きな実装の前に方針を確認する。
- commit / push は頼まれたときだけ行う。

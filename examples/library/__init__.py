"""
図書館の問い合わせ対応のサンプル。そのまま写して、自分のアプリの雛形にできる形にしてある。

    library/
      config/            エージェント・ツール・ナレッジの設定（YAML）
      prompts/           長い文章（システムプロンプト、evaluation）
      knowledge/         ナレッジの中身
      guard/             審査役の設定。図書館のネットワークとは記憶を分ける
      tools.py           ツールの実装（ダミーのデータ）
      network.py         組み立て。tools.yaml の type と、tools.py の関数を対応させる
      observers.py       interceptor に登録するもの（審査、表示、計測、再試行）
      carry_over.py      次のターンへ引き継ぐ記憶
      session.py         1つの会話（ターンごとにNetworkを作り、履歴と記憶を持ち越す）
      api_key.py         APIキーの用意

入口は examples/ 直下の cli.py（1問で終わる）/ chat.py（対話）/ api.py（Web API）。
"""

この結果に含まれるのは status と blocking_reason だけで、文章は無い。調べた内容は共有記憶へ書かれているので、回答はそれを読んで組み立てる。

COMPLETE なら、記憶を根拠に聞かれたことへ答える。
それ以外なら、blocking_reason に応じて次を利用者へ伝える。
  MISSING_REQUIRED_INPUT … 何が必要なのかを伝えて、利用者に尋ねる
  AMBIGUOUS_REQUEST … 考えられる範囲を示して、どれについてかを尋ねる。確定できていない主題を勝手に解説しない
  NOT_PERMITTED … できないことと、その理由を伝える
  INTERNAL_ERROR … 完了できなかったことを伝える

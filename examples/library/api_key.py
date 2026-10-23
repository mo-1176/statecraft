"""
APIキーを用意する。サンプルの題材はエージェントの組み方なので、ここは読み飛ばしてよい。

探す順は 環境変数 → .env → 入力欄。見つけたキーは環境変数 GEMINI_API_KEY に入れる。
NetworkBuilder に最初から登録されている gemini の provider は、そこから読む
（GeminiLLM に api_key を渡さないと、SDKが環境変数を読む）。
"""

import contextlib
import getpass
import os
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[1]


class ApiKey:
    ENV_VAR = "GEMINI_API_KEY"

    # .env を探す場所。カレントディレクトリに依存させない（入口は
    # リポジトリ直下からも examples/ からも起動されるため、相対パスだと
    # 起動した場所によって見つかったり見つからなかったりする）。
    ENV_FILES = (
        EXAMPLES_DIR / ".env",
        EXAMPLES_DIR.parent / ".env",  # リポジトリ直下
    )

    def ensure(self, *, ask: bool = True) -> None:
        """
        キーを環境変数へ入れる。ask=False なら入力欄を出さない（サーバー向け）。

        シェルに `GEMINI_API_KEY="..."` と打つ方法を勧めていないのは、
        多くのシェルがコマンド行を履歴ファイルへ平文で保存するため。
        """
        if os.environ.get(self.ENV_VAR):
            return

        for path in self.ENV_FILES:
            if value := self.read_env_file(path):
                os.environ[self.ENV_VAR] = value
                return

        if not ask:
            raise SystemExit(f"{self.ENV_VAR} が環境変数にも .env にもありません。")

        # 入力欄から受け取ったキーは、保存を選んだ場合だけ .env へ書く。
        # 選ばなければこのプロセスのメモリにしか残らず、終了すれば消える。
        key, save = self.ask()
        if not key:
            raise SystemExit("APIキーが入力されませんでした。")
        if save:
            # 書く場所は examples/.env の1つに決める（読むのは2箇所だが、
            # 書く場所が状況で変わると「さっき保存したのにまた聞かれる」が起きる）。
            self.save_to_env_file(self.ENV_FILES[0], key)
            print(f"{self.ENV_FILES[0]} へ保存しました。次回から聞かれません。")
        os.environ[self.ENV_VAR] = key

    def read_env_file(self, path: Path) -> str:
        """
        .env から値を1つ読む。無ければ空文字。

        python-dotenvへ依存しないための最小の実装。.env の書式を網羅はしない
        （複数行の値や変数展開は扱わない）。
        """
        if not path.exists():
            return ""

        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # `export KEY=値` と書かれていても読めるようにする。
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == self.ENV_VAR:
                # 値を囲む引用符は外す（キーの一部ではない）。
                return value.strip().strip("\"'")
        return ""

    def save_to_env_file(self, path: Path, value: str) -> None:
        """
        入力されたキーを .env へ書き足す。

        上書きではなく追記なのは、既に書かれている他の値を消さないため。
        権限を絞るのは新規作成した時だけ（既にある .env の設定は変えない）。
        """
        is_new = not path.exists()

        # 既存の最終行が改行で終わっていない場合、続けて書くと同じ行に繋がる。
        prefix = ""
        if not is_new and not path.read_text(encoding="utf-8").endswith("\n"):
            prefix = "\n"

        with path.open("a", encoding="utf-8") as f:
            f.write(f"{prefix}{self.ENV_VAR}={value}\n")

        if is_new:
            # 秘密を書いたファイルなので本人だけが読めるようにする。
            # Windowsでは意味を持たないが、失敗しても実行は止めない。
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)

    def ask(self) -> tuple[str, bool]:
        """
        その場でキーの入力を求める。戻り値は (キー, .envへ保存するか)。

        画面があればダイアログ、無ければ端末で聞く。どちらも伏せ字にするのは、
        貼り付けたキーを画面にも端末の履歴にも残さないため。
        """
        try:
            return self.ask_with_dialog()
        except Exception:  # noqa: BLE001
            # 画面が使えない環境（SSH越しなど）ではダイアログを開けない。
            # 開けない理由は環境によって違う（ImportError / TclError / OSError）が、
            # ここで必要な判断は「使えたかどうか」だけなので種類を区別しない。
            key = getpass.getpass("APIキーを入力してください（表示されません）: ").strip()
            # ダイアログのチェックボックスと同じ既定（そのままEnterで保存）。
            answer = input(".env へ保存しますか？ 次回から聞かれません [Y/n]: ").strip().lower()
            return key, answer in ("", "y", "yes")

    def ask_with_dialog(self) -> tuple[str, bool]:
        """
        tkinterの入力欄でキーを受け取る。

        tkinterが無い環境・画面が無い環境では例外が出る。
        呼び出し側（ask）が端末での入力へ切り替える。
        """
        import tkinter as tk

        # クロージャから書き戻すため、値ではなく入れ物を持つ。
        result = {"key": "", "save": True}

        root = tk.Tk()
        root.title(self.ENV_VAR)
        root.attributes("-topmost", True)
        tk.Label(root, text="APIキーを貼り付けてください（Ctrl+V）", padx=16, pady=8).pack()
        entry = tk.Entry(root, show="*", width=54)  # 伏せ字で表示する
        entry.pack(padx=16)
        entry.focus_set()

        # 既定で保存する。2回目以降は聞かれないのが普通の使い方なので、
        # 「保存しない」の方を明示的な操作にする。
        save = tk.BooleanVar(value=True)
        tk.Checkbutton(root, text="このキーを .env へ保存する", variable=save).pack(pady=(8, 0))

        def submit(_event: object = None) -> None:
            result["key"] = entry.get().strip()
            result["save"] = bool(save.get())
            root.destroy()

        entry.bind("<Return>", submit)
        tk.Button(root, text="OK", command=submit, width=12).pack(pady=12)
        # Escapeはsubmitを通らずに閉じる。keyは空のままになり、呼び出し側が落とす。
        root.bind("<Escape>", lambda _e: root.destroy())
        root.mainloop()

        return result["key"], result["save"]

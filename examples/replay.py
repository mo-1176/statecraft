"""
保存した状態から、同じ所を何度でも試し直す。人に尋ねるためではない、save_state の使い方。

cli.py で止めた依頼（sample_suspended.json）を読み込み、答えを渡して最後まで進める。
ファイルは消さず、書き換えもしない。保存した状態に設定は入っていないので、
prompts/ や config/ を直してから流し直すと、同じ所から直した設定で動く。
止まった所より後のエージェントの振る舞いを、毎回最初から流さずに確かめられる。

    python cli.py "『銀河鉄道』を予約したい。会員番号はM-001"   # 止めて保存する
    python replay.py --answer 2 --answer はい                  # そこから最後まで（ファイルは残る）
    （prompts/agents/librarian/ を直す）
    python replay.py --answer 2 --answer はい                  # 同じ所から、直した設定で

--answer は止まった順に使う。足りなければ、その時点で止まったまま終わる（保存はしない）。
残しておきたい地点は、ファイルをコピーしておき --state で指す。
"""

import argparse
import json
import sys
from pathlib import Path

from library.api_key import ApiKey
from library.session import LibrarySession

# cli.py が止めた依頼を保存する所
SUSPENDED_FILE = Path(__file__).resolve().parent / "sample_suspended.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="保存した状態から、同じ所を何度でも試し直す")
    parser.add_argument("--answer", action="append", default=[], help="止まった順に渡す答え。何度でも書ける")
    parser.add_argument("--state", type=Path, default=SUSPENDED_FILE, help="読み込む状態のファイル")
    args = parser.parse_args()

    if not args.state.exists():
        raise SystemExit(f"{args.state.name} がありません。先に cli.py で依頼を止めてください。")

    ApiKey().ensure()
    # 読むだけで、書き戻さない。answer() は読み込んだ会話を進めるだけで、ファイルには触れない
    session = LibrarySession.load(json.loads(args.state.read_text(encoding="utf-8")))

    for answer in args.answer:
        suspension = session.suspension
        if suspension is None:
            print(f"\n（依頼は終わったので、残りの答えは使わない: {answer}）")
            break
        print(f"\n? {suspension.question}\n→ {suspension.resolve(answer)}")
        # 新しいNetworkへ状態を戻し（load_state）、止まった所から進める（resume）
        session.answer(answer)

    print("\n" + "=" * 64)
    if session.suspension is not None:
        print(f"答えが足りず、止まったまま終わりました: {session.suspension.question}")
        for number, option in enumerate(session.suspension.options, 1):
            print(f"  {number}. {option}")
        print("続けるには、--answer を足して流し直す")
    else:
        assert session.response is not None
        print(session.response.text)

    session.cost.report()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # cli.py と同じく、例外の種類と内容だけを短く出す
        print(f"\n失敗しました: {type(e).__name__}\n{e}", file=sys.stderr)
        raise SystemExit(1) from e

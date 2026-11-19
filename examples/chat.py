"""
対話モード。1つの会話の中で、会話履歴と記憶（ontology / vars）を次のターンへ引き継ぐ。

    python chat.py
    python chat.py --fresh    # 前回の記憶を引き継がずに始める

利用者に候補を選んでもらう所で依頼が止まったら、候補を出して次の入力を待つ。
次の入力（番号か返答の文）がその答えになり、止まった所から続く。

exit で終わる（Ctrl+C / Ctrl+D でも終わる）。終えた時点の記憶を sample_carry_over.json へ
保存し、次に起動したときに引き継ぐ（cli.py と同じファイル）。
"""

import sys
from pathlib import Path

from library.api_key import ApiKey
from library.carry_over import CarryOver
from library.session import LibrarySession

CARRY_OVER_FILE = Path(__file__).resolve().parent / "sample_carry_over.json"
EXIT_WORDS = {"exit", "quit"}


def main() -> None:
    fresh = "--fresh" in sys.argv[1:]

    ApiKey().ensure()
    session = LibrarySession(CarryOver() if fresh else CarryOver.load(CARRY_OVER_FILE))

    print("図書館の受付です。exit で終わります。")
    if fresh:
        print("前回の記憶は引き継がない（--fresh）")
    elif carried := session.carry_over.summary():
        print(f"前回から引き継いだ記憶: {carried}")

    while True:
        try:
            message = input("\nあなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message.lower() in EXIT_WORDS:
            break

        try:
            # 依頼が途中で止まっていれば、この発言はその答え
            reply = session.answer(message) if session.suspension else session.ask(message)
        except Exception as e:  # noqa: BLE001
            # 1ターンの失敗で会話を終わらせない。履歴と記憶は失敗する前のまま残る
            # （止まっていた依頼も残るので、もう一度答えられる）。
            print(f"\n失敗しました: {type(e).__name__}: {e}")
            continue

        print(f"\n受付> {reply.message}")
        for number, option in enumerate(reply.options, 1):
            print(f"  {number}. {option}")

    session.cost.report()

    if fresh:
        print("\n引き継ぎは保存していない（--fresh）")
    else:
        session.carry_over.save(CARRY_OVER_FILE)
        print(f"\n引き継ぎ用に保存しました: {CARRY_OVER_FILE.name}")


if __name__ == "__main__":
    main()

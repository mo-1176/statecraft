"""
1つの質問に答えて終わる。題材は図書館の問い合わせ対応（設定は library/ にある）。

    python cli.py
    python cli.py "貸出は何冊まで？"
    python cli.py "貸出は何冊まで？" --fresh    # 前回の記憶を引き継がない

2回目以降は、前回の記憶のうち ontology と vars を引き継ぐ（sample_carry_over.json）。
何を実演しているか・どう読むかは docs/sampleの読み方.md を参照。
"""

import sys
import time
from pathlib import Path

from statecraft import Network

from library.api_key import ApiKey
from library.carry_over import CarryOver
from library.session import LibrarySession

# chat.py と同じファイル。どちらで話しても、次の起動へ引き継がれる
CARRY_OVER_FILE = Path(__file__).resolve().parent / "sample_carry_over.json"


def dump_memory(net: Network) -> None:
    shared = net.shared_memory
    print("\n" + "=" * 64)
    print("共有記憶（全Agentが同じ実体を読み書きする）")
    print("=" * 64)

    if shared.requests:
        # 利用者の原文ではなく、front が構造化した依頼が入る。
        # 原文が共有記憶へ入らないことが、後続Agentへの指示混入を防ぐ境界になる。
        print(f"■ requests（システムが書き込む）\n    {shared.requests.text}")

    for name in (
        "state_briefing",
        "vars",
        "facts",
        "ontology",
        "actions",
        "decisions",
        "hypotheses",
        "questions_to_research",
        "questions_to_ask",
        "agent_answers",
    ):
        entries = getattr(shared, name)
        if entries:
            print(f"■ {name}")
            for e in entries:
                print(f"    [{e.id}] {e.text}")

    # 個別記憶はエージェントごとに別の実体。持つのは作業の進行管理だけ。
    # front は ReflexAgent なので出てこない（更新するphaseを通らない）。
    print("\n" + "=" * 64)
    print("個別記憶（librarian だけが持つ）")
    print("=" * 64)
    librarian = net["librarian"]
    for field in ("goals", "task_notes"):
        entries = getattr(librarian.private_memory, field)
        if entries:
            print(f"■ {field}")
            for e in entries:
                print(f"    [{e.id}] {e.text}")
    if librarian.private_memory.tasks:
        print("■ tasks（status と実行対象を構造として持つ）")
        for t in librarian.private_memory.tasks:
            print(f"    [{t.id}] ({t.status.value}) {t.text} → {t.target_names}")
    print("\n（front は ReflexAgent のため個別記憶を持たない。tasks も無い）")

    print("\n■ tool の有効状態（セッション中に無効化されたものが分かる）")
    for tool in librarian.tools:
        print(f"    {tool.name}: {'無効' if tool.disabled else '有効'}")


def main() -> None:
    # --fresh を付けると、前回の記憶を引き継がずに実行する。
    # 挙動を試す時は、前回の内容が混ざらない方が原因を切り分けやすい。
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    fresh = "--fresh" in sys.argv
    question = args[0] if args else "学生証があれば何冊まで借りられる？"

    ApiKey().ensure()
    session = LibrarySession(CarryOver() if fresh else CarryOver.load(CARRY_OVER_FILE))

    print("=" * 64)
    print(f"質問: {question}")
    # 何を持ち越したかは表示しておく。黙って引き継ぐと、前回の内容が
    # 回答に出てきたときに原因を追えなくなる。
    if fresh:
        print("前回の記憶は引き継がない（--fresh）")
    elif carried := session.carry_over.summary():
        print(f"前回から引き継いだ記憶: {carried}")
    print("=" * 64)

    started = time.time()
    session.ask(question)
    elapsed = time.time() - started
    net, response = session.network, session.response
    assert net is not None and response is not None

    print("\n" + "=" * 64)
    print("回答")
    print("=" * 64)
    # そのまま出す。message だけを表示すると、output_schema が守られたのか
    # どうかが見えなくなる（chat.py / api.py は message と awaiting_reply を使う）。
    print(response.text)

    dump_memory(net)

    print("\n" + "=" * 64)
    print(f"所要 {elapsed:.1f}秒 / front のステップ数 {response.steps}")
    # Agentごとに分けて数える。委譲先の消費は呼び出し元へ合算しないため、
    # どのAgentがコストを使っているかが分かる。
    print(f"Agentごとのトークン: {net.token_usage()}")
    print(f"合計: {net.total_tokens()}")

    session.cost.report()

    # --fresh の時は保存もしない（次回へ残ると、引き継がない指定の意味が無くなる）。
    if fresh:
        print("\n引き継ぎは保存していない（--fresh）")
    else:
        session.carry_over.save(CARRY_OVER_FILE)
        fields = " / ".join(CarryOver.FIELDS)
        print(f"\n引き継ぎ用に保存しました: {CARRY_OVER_FILE.name}（{fields} のみ）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 例外の種類と内容だけを短く出す。
        # トレースバック全文は利用者に見せるものではないため、ここで止める。
        print(f"\n失敗しました: {type(e).__name__}\n{e}", file=sys.stderr)
        # SystemExitはPythonが特別扱いする例外で、終了コード1（＝失敗）を残してプロセスを終える。
        raise SystemExit(1) from e

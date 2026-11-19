"""
1つの質問に答えて終わる。題材は図書館の問い合わせ対応（設定は library/ にある）。

    python cli.py
    python cli.py "貸出は何冊まで？"
    python cli.py "貸出は何冊まで？" --fresh    # 前回の記憶を引き継がない

利用者に尋ねる所（候補を選ぶ、足りない情報を答える、予約を承認する）で依頼が止まったら、
状態を sample_suspended.json に保存して終わる。答えを渡すと、保存した状態を読み込んで
止まった所から続ける。1つの依頼の中で、何度か止まることもある。

    python cli.py "『銀河鉄道』を予約したい。会員番号はM-001"    # 候補を出して止まる
    python cli.py --answer 2                                    # 2番目を選んで続ける
    python cli.py --answer はい                                 # 予約の承認で止まったら答える

2回目以降は、前回の記憶のうち ontology と vars を引き継ぐ（sample_carry_over.json）。
何を実演しているか・どう読むかは docs/sampleの読み方.md を参照。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from statecraft import Network

from library.api_key import ApiKey
from library.carry_over import CarryOver
from library.session import LibrarySession

# chat.py と同じファイル。どちらで話しても、次の起動へ引き継がれる
CARRY_OVER_FILE = Path(__file__).resolve().parent / "sample_carry_over.json"
# 止まった依頼の置き場。サーバーなら Redis や DB に置くもの
SUSPENDED_FILE = Path(__file__).resolve().parent / "sample_suspended.json"


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
        "user_back_grounds",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="図書館のサンプル。1つの質問に答えて終わる")
    parser.add_argument("question", nargs="?", default="学生証があれば何冊まで借りられる？")
    # 挙動を試す時は、前回の内容が混ざらない方が原因を切り分けやすい。
    parser.add_argument("--fresh", action="store_true", help="前回の記憶を引き継がない")
    parser.add_argument("--answer", help="止まっている依頼への答え（候補の番号か、返答の文）")
    return parser.parse_args()


def start(question: str, fresh: bool) -> LibrarySession:
    """新しい質問を始める。"""
    if SUSPENDED_FILE.exists():
        # 1問で終わる入口なので、新しい質問は新しい会話。止まっていた依頼は続けない
        SUSPENDED_FILE.unlink()
        print(f"前回止まっていた依頼は破棄しました（{SUSPENDED_FILE.name}）")

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

    session.ask(question)
    return session


def resume(answer: str) -> LibrarySession:
    """保存してある、止まった依頼を読み込み、答えを渡して続ける。"""
    if not SUSPENDED_FILE.exists():
        raise SystemExit(f"止まっている依頼がありません（{SUSPENDED_FILE.name} が無い）。先に質問してください。")
    session = LibrarySession.load(json.loads(SUSPENDED_FILE.read_text(encoding="utf-8")))
    assert session.suspension is not None

    print("=" * 64)
    print(f"続き: {session.suspension.question}")
    print(f"答え: {session.suspension.resolve(answer)}")
    print("=" * 64)

    # ここで新しいNetworkへ状態を戻し（load_state）、止まった所から進める（resume）
    session.answer(answer)
    return session


def wait_for_answer(session: LibrarySession) -> None:
    """止まった依頼を保存して終わる。答えは次の起動で受け取る。"""
    suspension = session.suspension
    assert suspension is not None

    print("\n" + "=" * 64)
    print("利用者に尋ねる（依頼はここで止まっている）")
    print("=" * 64)
    print(suspension.question)
    for number, option in enumerate(suspension.options, 1):
        print(f"  {number}. {option}")

    # 答えを待たずに、状態を保存してプロセスを終える。サーバーで動かす時も同じで、
    # 答えは別のリクエストで届く。
    SUSPENDED_FILE.write_text(json.dumps(session.dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n状態を保存しました: {SUSPENDED_FILE.name}")
    if suspension.options:
        print("続けるには: python cli.py --answer 番号（候補に無い返答は文で渡す）")
    else:
        print('続けるには: python cli.py --answer "返答"')

    session.cost.report()


def finish(session: LibrarySession, elapsed: float, fresh: bool) -> None:
    """終わった依頼の回答と、記憶・コストの内訳を出す。"""
    SUSPENDED_FILE.unlink(missing_ok=True)
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


def main() -> None:
    args = parse_args()
    ApiKey().ensure()

    started = time.time()
    session = start(args.question, args.fresh) if args.answer is None else resume(args.answer)
    elapsed = time.time() - started

    if session.suspension is not None:
        wait_for_answer(session)
    else:
        finish(session, elapsed, args.fresh)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 例外の種類と内容だけを短く出す。
        # トレースバック全文は利用者に見せるものではないため、ここで止める。
        print(f"\n失敗しました: {type(e).__name__}\n{e}", file=sys.stderr)
        # SystemExitはPythonが特別扱いする例外で、終了コード1（＝失敗）を残してプロセスを終える。
        raise SystemExit(1) from e

"""
ツールの実装。ただのPython関数として書く。

LLMに見せる説明（summary / usage / evaluation）と引数のスキーマは config/tools.yaml に
ある。ここに置くのは、実際に動く処理だけ。データはサンプル用のダミー。
"""

import time

from statecraft import suspend

RULES = """
【貸出】
一般利用者は1人あたり同時に5冊まで、貸出期間は14日間。
学生証を提示した場合は10冊まで、貸出期間は30日間。
延長は1回のみ可能で、予約が入っている資料は延長できない。

【返却】
閉館時は入口横の返却ポストを利用する。
延滞は1日あたり1冊10円の延滞料が発生する。

【予約】
貸出中の資料は予約できる。到着後7日以内に受け取らないと予約は取り消される。

【禁帯出】
辞書・年鑑・新聞の原紙は館内閲覧のみで貸出できない。
"""

# 会員の登録簿。予約時に実在を確認するために使う。
# 形式チェック（observers.py の Guard の②）だけでは、形を真似た値を止められない。
MEMBERS = {
    "M-001": "一般",
    "M-002": "学生",
}

STOCK: dict[str, dict[str, str | None]] = {
    "深夜特急": {"著者": "沢木耕太郎", "所在": "3階 旅行記", "状態": "貸出中", "返却予定": "2026-09-10"},
    "銀河鉄道の夜": {"著者": "宮沢賢治", "所在": "2階 日本文学", "状態": "在架", "返却予定": None},
    # 「銀河鉄道」で探すと上と2冊になる。利用者に選んでもらう（identify_title）確認に使う
    "銀河鉄道の父": {"著者": "門井慶喜", "所在": "2階 日本文学", "状態": "貸出中", "返却予定": "2026-12-10"},
    # 禁帯出資料。予約もできないため、審査で止まることの確認に使う
    "広辞苑": {"著者": "新村出 編", "所在": "1階 参考図書", "状態": "館内閲覧のみ", "返却予定": None},
}


def search_rules(topic: str) -> dict:
    """
    図書館の利用規程を返す。

    サンプルなのでベクトル検索などの絞り込みは行わず、規程の全文を返す。
    """
    return {"value": RULES}


def fetch_member_status(member_id: str) -> dict:
    """
    会員区分を引く。貸出可能冊数と貸出期間の条件がこれで決まる。

    initial_toolsに置いているが、会員番号が渡らない相談も普通にある。
    その場合このtoolは実行されず、「取得できなかった」が記録される。
    """
    category = MEMBERS.get(member_id)
    if category is None:
        return {"value": f"会員番号 {member_id} は登録簿に見つかりません。"}
    # memoryへは入れない。区分は「一般」「学生」のどちらかで、
    # 受付番号のように一字一句を保たないと無意味になる値ではないため。
    return {"value": f"会員番号 {member_id} の区分: {category}"}


def identify_title(keyword: str) -> dict:
    """
    書名の一部や著者名から蔵書を探し、書名を1つに確定する。

    候補が複数あれば、どれのことかを利用者に選んでもらう。LLMに推測させると、
    利用者の求めていない方で話が進む。suspend() を呼ぶと依頼全体がここで止まり、
    利用者の答えがそのまま、このtoolの結果になる（再開しても、この関数は呼び直されない）。
    """
    found = [
        title
        for title, info in STOCK.items()
        if keyword in title or keyword in (info["著者"] or "")
    ]
    if not found:
        return {"value": f"「{keyword}」に当たる蔵書は見つかりませんでした。"}
    if len(found) == 1:
        return {"value": f"書名は『{found[0]}』です（著者: {STOCK[found[0]]['著者']}）。"}

    # 渡す値は、入口が利用者へ見せるもの。状態ごと保存されるので、JSONにできる形にする。
    # 候補は書名だけにする。選ばれた候補がそのまま結果になり、check_stock へ渡るため。
    suspend(
        {
            "question": f"「{keyword}」に当たる本が{len(found)}冊あります。どれのことですか？",
            "options": found,
        }
    )
    return {"value": ""}  # ここには来ない。suspend() は止まったまま戻らない


def ask_member(question: str) -> dict:
    """
    利用者にしか分からないこと（会員番号など）を、調べている途中のまま利用者に尋ねる。

    受付に聞き返してもらうと、答えは次のターンの新しい依頼になり、司書は最初から
    調べ直す。ここで止めれば、それまでに調べたことを持ったまま、答えだけを受け取って続ける。
    利用者の答えが、そのままこのtoolの結果になる。
    """
    # 候補を出さない（自由入力）ので options は無い
    suspend({"question": question})
    return {"value": ""}  # ここには来ない。suspend() は止まったまま戻らない


def check_stock(title: str) -> dict:
    """指定した書名の所在と貸出状況を調べる"""
    found = STOCK.get(title)
    if not found:
        return {"value": f"『{title}』は当館の蔵書に見つかりませんでした。"}
    return {
        "value": (
            f"『{title}』 所在={found['所在']} 状態={found['状態']} 返却予定={found['返却予定']}"
        )
    }


def issue_reservation(title: str, member_id: str) -> dict:
    """
    予約を登録し、受付番号を発行する。

    受付番号は memory へ入れる（1文字変わるだけで無意味になる値はLLMを
    経由させない）。会員の実在はここで確かめる——実行前の判定（Guard）が
    見ているのは形式だけ。
    """
    if member_id not in MEMBERS:
        return {
            "value": f"会員番号{member_id}は登録されていないため、予約できませんでした。",
            "memory": {"facts": [f"会員番号{member_id}は登録されていないため、予約できなかった。"]},
        }

    ticket = f"RSV-{abs(hash(title + member_id)) % 100000:05d}"
    return {
        "value": f"『{title}』の予約を登録しました。受付番号は {ticket} です。",
        "memory": {
            "vars": [f"予約受付番号: {ticket}"],
            "facts": [f"『{title}』の予約を会員{member_id}名義で登録した。"],
        },
    }


def send_overdue_notice(member_id: str) -> dict:
    """延滞している利用者へ督促メールを送信する"""
    return {"value": f"会員{member_id}へ督促メールを送信しました。"}


def is_open_hours() -> bool:
    """開館時間内か。実行の可否を外部の状態で判断する例（Guardの③）。"""
    return 9 <= time.localtime().tm_hour < 20


# 見本のツールが一度に返す件数。上限のような値は、ツールの実装が持つ。
SAMPLE_MAX_RESULTS = 3


def sample_tool(topic: str) -> dict:
    """config/tools.yaml の sample_tool（全項目の見本）の実装。"""
    return {"value": f"「{topic}」についての見本の結果（最大{SAMPLE_MAX_RESULTS}件）"}

"""
ツールの実装。ただのPython関数として書く。

LLMに見せる説明（summary / usage / evaluation）と引数のスキーマは config/tools.yaml に
ある。ここに置くのは、実際に動く処理だけ。データはサンプル用のダミー。
"""

import time

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
    "深夜特急": {"所在": "3階 旅行記", "状態": "貸出中", "返却予定": "2026-09-10"},
    "銀河鉄道の夜": {"所在": "2階 日本文学", "状態": "在架", "返却予定": None},
    # 禁帯出資料。予約もできないため、審査で止まることの確認に使う
    "広辞苑": {"所在": "1階 参考図書", "状態": "館内閲覧のみ", "返却予定": None},
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

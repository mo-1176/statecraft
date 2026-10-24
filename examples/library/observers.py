"""
interceptor に登録するもの。通知（notify）は観測のみ、判定（check）は拒否権を持つ。

    Guard             実行の手前で可否を決める（審査役への問い合わせを含む）
    ArgumentWatcher   呼ばれた対象と引数を出す
    CostMeter         生成ごとのトークンと概算コストを積む
    その他             画面への表示、記憶の変化の表示、生成失敗時の再試行

Interceptor は文言を持たない。誰に何を見せるかは購読する側で決める。
"""

import json
import time
from typing import ClassVar

from statecraft import Agent, Interceptor

from . import tools
from .network import build_compliance

# 枠切れ（429）のときに逃がす先。config/agents.yaml の librarian の既定モデルと同じ
FALLBACK_MODEL = "gemini-3.5-flash-lite"


class Guard:
    """
    実行の直前に呼ばれ、拒否できる（before_execute）。

    True を返した場合のみ許可される（None・返し忘れ・例外はすべて拒否）。
    文字列を返すと拒否のうえ理由として実行側へ返る。理由そのものが審査基準に
    なるものは返さない。ずっと使わせないなら Tool.disabled を使う。
    """

    # 操作の審査を通す対象。全toolに掛けると、そのたびに生成が1回増えて遅くなる。
    # 取り消せない操作、外部へ影響する操作だけを対象にする。
    NEEDS_COMPLIANCE: ClassVar[set[str]] = {"issue_reservation", "send_overdue_notice"}
    # 依頼の審査を通す対象（委譲先の名前）。
    # 利用者の入力が最初に構造化されて渡る境界だけを見る。
    NEEDS_REQUEST_AUDIT: ClassVar[set[str]] = {"librarian"}

    def __init__(self) -> None:
        self.compliance: Agent = build_compliance()

    def __call__(self, *, name: str, kwargs: dict) -> bool | str:
        # ①②③は文字列を返す（拒否と同時に理由がLLMへ渡る）。理由が無いと、
        # 止められた側は原因を探して追加の実行を始める。
        # 引数を直せば通るものは、直せる形で伝える。

        # ① ツール名で止める — 取り消せない操作を、このデモでは実行しない
        if name == "send_overdue_notice":
            # 「なぜ使えないか」を返す。返さないと、LLMは条件を変えて
            # 何度も呼び直そうとする（このtoolは何をしても通らない）。
            return (
                "外部への送信を伴うため、この環境では実行できません。別の手段を検討してください。"
            )

        # ② 引数の内容で止める — 会員番号の形式が不正なら予約させない
        if name == "issue_reservation":
            member_id = str(kwargs.get("member_id", ""))
            if not member_id.startswith("M-"):
                # 何が問題かは返すが、正しい形式は返さない。
                # 形式を教えると、その形式に合わせて値を作られる
                # （「M- で始まる」と返したら「12345」を「M-12345」にされた）。
                return (
                    f"受け取った会員番号（{member_id!r}）は正しい形式ではありません。"
                    "利用者から正しい会員番号を取得してください。"
                    "値を推測したり、形式に合わせて書き換えたりしてはいけません。"
                )

        # ③ 外部の状態で止める — 開館時間外は予約の登録を受け付けない
        if name == "issue_reservation" and not tools.is_open_hours():
            return "開館時間外のため、予約の登録を受け付けられません。開館時間は9時から20時です。"

        # ④ 内容を審査して止める — 機械的な条件では書けない場合
        #
        # 判定が必要なものだけに絞る。全toolに掛けると実行のたびに生成が1回増える。
        # ここは False を返す（理由が審査基準そのものなので伝えない）。
        if name in self.NEEDS_COMPLIANCE:
            return self.judge_execution(name, kwargs)

        # ⑤ 依頼の内容を審査して止める — 委譲の引数そのものを見る。
        # before_execute は tool と agent の両方で発火するので、「委譲してよいか」
        # も同じ機構で審査できる。ここも理由は返さない（審査基準に当たる）。
        if name in self.NEEDS_REQUEST_AUDIT:
            return self.judge_request(name, kwargs)

        return True

    def judge_execution(self, name: str, kwargs: dict) -> bool:
        """操作の審査。規程に反しないかを見る。"""
        # 引数だけでは規程に照らせないため、対象の状態も添えて渡す。
        # 審査に必要な材料を揃えるのは、呼び出し側の責任になる。
        # toolは辞書を返すので value を取り出す。そのまま埋めると
        # 審査役へ渡る文が {'value': '...'} という表記になる。
        stock = (
            tools.check_stock(kwargs["title"])["value"]
            if "title" in kwargs
            else "（対象の資料なし）"
        )
        return self.ask_compliance(
            name, f"次の操作を審査せよ。\n操作: {name}\n引数: {kwargs}\n対象の状態: {stock}"
        )

    def judge_request(self, name: str, kwargs: dict) -> bool:
        """
        依頼の審査。委譲の引数そのものを見る（プロンプトインジェクションへの
        3層目。1層目はfrontによる構造化、2層目はinput_schema）。

        見るのは指示の上書きと、無害な作業に紛れ込ませた入れ子の要求。
        「明確に危険なものだけ止める」に寄せている——通すべきものを止めると
        「エラーで進まない」としか見えず、原因が審査だと分からない。
        """
        return self.ask_compliance(
            f"{name}への依頼", f"次の依頼を審査せよ。\n委譲先: {name}\n依頼内容: {kwargs}"
        )

    def ask_compliance(self, label: str, body: str) -> bool:
        """審査役を起動し、その判定で可否を決める。"""
        result = self.compliance.execute(kwargs={"message": body})

        if not result.success:
            print(f"    [拒否] {label}: 審査を完了できませんでした（{result.error}）")
            return False

        # output_schema で形式を強制しているが、受け取り側でも確認する。
        # Geminiはスキーマで保証されるが、他のプロバイダでは指示文へ変換されるため
        # 守られないことがある。審査で最も避けたいのは
        # 「形式が守られなかったときに許可が通る」ことなので、二重にしている。
        try:
            verdict = json.loads(result.value)
            allowed = verdict["allow"] is True
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"    [拒否] {label}: 審査結果を解釈できませんでした（{e}）")
            return False

        print(f"    [審査/{'許可' if allowed else '拒否'}] {label}: {verdict.get('reason', '')}")
        return allowed


class ArgumentWatcher:
    """
    呼ばれた対象と引数を出す。観測だけで、可否の判断はしない。

    before_execute は拒否権を持つ判定。観測するだけのつもりでも
    True を返し忘れると、その時点で全ての実行が拒否される。
    """

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {}

    def __call__(self, *, name: str, kwargs: dict) -> bool:
        shown = []
        for key, value in kwargs.items():
            text = str(value).replace("\n", " ")
            if len(text) > 70:
                text = f"{text[:70]}…（全{len(str(value))}字）"
            elif text == "":
                # 空文字は「渡されなかった」ではなく「空と決めて渡された」。
                # 素のまま出すと key= だけになり、どちらか判別できない。
                text = "''"
            shown.append(f"{key}={text}")

        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        count = self.call_counts[name]
        repeat = f"  ※{count}回目" if count > 1 else ""
        print(f"  [引数] {name}({', '.join(shown)}){repeat}")

        # 同じ対象が何度も呼ばれるのは、前回の結果が記憶へ反映されていない、
        # あるいはtasksが完了扱いになっていない兆候として観測できる。
        # ここで止めることもできるが、正当な再実行（引数を変えた再試行）も
        # あるため、このデモでは数えて見せるだけにしている。
        return True


class CostMeter:
    """
    生成1回ごとの実測を積み、モデルごとの概算コストを出す。

    単価はハードコード。目的は「どのphaseがいくら使ったか」を桁で把握する
    ことで、請求額を当てることではない。値は公式の価格表で確認して更新する。
    """

    USD_JPY: ClassVar[int] = 155  # 実運用では為替APIか、固定の社内レートを使う

    # USD / 100万トークン。
    PRICING: ClassVar[dict[str, dict[str, float]]] = {
        "gemini-3.5-flash-lite": {"in": 0.3, "out": 2.5},
        "gemini-3.7-flash": {"in": 0.75, "out": 3.75},
    }

    # キャッシュから読まれた入力トークンの単価比（暗黙キャッシュは通常より安い）。
    CACHED_INPUT_RATIOS: ClassVar[dict[str, float]] = {
        "ai_studio": 0.25,  # Developer API。約75%割引 → 通常単価の25%を請求
        "vertex": 0.10,  # Vertex AI。約90%割引 → 通常単価の10%を請求
    }
    # GeminiLLM を api_key で接続しているので AI Studio 側。
    # GeminiLLM(use_vertex=True, project_id=...) へ変えたら "vertex" にする。
    PLATFORM: ClassVar[str] = "ai_studio"

    def __init__(self) -> None:
        self.rows: list[dict] = []

    @property
    def cached_input_ratio(self) -> float:
        return self.CACHED_INPUT_RATIOS[self.PLATFORM]

    def add(self, event) -> float | None:
        """生成1回分を積み、その回の概算（円）を返す。単価表に無いモデルはNone。"""
        jpy = self.estimate_jpy(
            event.model, event.input_tokens, event.output_tokens, event.cached_tokens
        )
        self.rows.append(
            {
                "model": event.model,
                "in": event.input_tokens,
                "out": event.output_tokens,
                "cached": event.cached_tokens,
                "jpy": jpy,
                "saved": self.cached_saving_jpy(event.model, event.cached_tokens),
            }
        )
        return jpy

    def estimate_jpy(
        self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float | None:
        price = self.PRICING.get(model)
        if price is None:
            return None

        # キャッシュから読まれた分を単価の安い側で数える。
        fresh_in = max(0, input_tokens - cached_tokens)
        usd = (
            fresh_in * price["in"]
            + cached_tokens * price["in"] * self.cached_input_ratio
            + output_tokens * price["out"]
        ) / 1_000_000
        return usd * self.USD_JPY

    def cached_saving_jpy(self, model: str, cached_tokens: int) -> float:
        """キャッシュが効いたことで払わずに済んだ額。"""
        price = self.PRICING.get(model)
        if price is None or not cached_tokens:
            return 0.0
        usd = cached_tokens * price["in"] * (1 - self.cached_input_ratio) / 1_000_000
        return usd * self.USD_JPY

    def report(self) -> None:
        """モデルごとの内訳と合計を出す。"""
        if not self.rows:
            return

        by_model: dict[str, dict[str, float]] = {}
        for row in self.rows:
            acc = by_model.setdefault(
                row["model"], {"calls": 0, "in": 0, "out": 0, "cached": 0, "jpy": 0.0, "saved": 0.0}
            )
            acc["calls"] += 1
            acc["in"] += row["in"]
            acc["out"] += row["out"]
            acc["cached"] += row["cached"]
            acc["jpy"] += row["jpy"] or 0.0
            acc["saved"] += row["saved"]

        print()
        print(
            f"概算コスト（単価はハードコード。1USD={self.USD_JPY}円 / "
            f"{self.PLATFORM}のキャッシュ割引で計算）"
        )
        print(
            "  {:24s} {:>4s} {:>10s} {:>9s} {:>10s} {:>9s}".format(
                "モデル", "回数", "in", "out", "cached", "円"
            )
        )
        print("  " + "-" * 70)
        total_jpy = 0.0
        total_saved = 0.0
        unknown = []
        for model, acc in sorted(by_model.items(), key=lambda kv: -kv[1]["jpy"]):
            if model not in self.PRICING:
                unknown.append(model)
            print(
                "  {:24s} {:>4d} {:>10,d} {:>9,d} {:>10,d} {:>9.3f}".format(
                    model,
                    int(acc["calls"]),
                    int(acc["in"]),
                    int(acc["out"]),
                    int(acc["cached"]),
                    acc["jpy"],
                )
            )
            total_jpy += acc["jpy"]
            total_saved += acc["saved"]

        print("  " + "-" * 70)
        print("  {:24s} {:>4d} {:>41.3f}".format("合計", len(self.rows), total_jpy))
        if total_saved:
            print(f"  プロンプトキャッシュで払わずに済んだ分: 約{total_saved:.3f}円")
        if unknown:
            print(f"  ※ 単価表に無いため未計上: {' / '.join(sorted(set(unknown)))}")


def print_memory_diff(event) -> None:
    """
    記憶の作られ方を構造のまま出す（検証用）。

    材料（tool_results）・出力（rows）・結果（errors）が同時に要る。
    memory_updated の文字列では text も失敗も落ちている。
    """
    origin = event.source_tool or (event.phase.value if event.phase else "?")
    status = "適用" if event.applied else f"失敗{len(event.errors)}件"
    print(f"  [memory_diff] {event.agent} / {origin} / {event.attempt}回目 / {status}")
    for row in event.rows:
        if not isinstance(row, dict):
            continue
        # tasksはstatusと実行対象まで出す。ここが見えないと
        # 「なぜこのタスクが実行されなかったのか」を追えない
        # （nextとnext_parallel以外は実行対象にならない）。
        extra = ""
        if row.get("status"):
            extra += f" [{row['status']}]"
        if row.get("target_names"):
            extra += f" → {row['target_names']}"
        print(f"      {row.get('field')}[{row.get('id')}]{extra} {row.get('text')}")
    for e in event.errors:
        print(f"      × {e.render()}")


class GenerationPrinter:
    """
    生成1回ごとの時間とトークンを出し、CostMeterへ積む（計測用）。

    合計だけでは「遅いのが生成回数のせいか、1回の重さのせいか」が
    分からない。phaseとモデル名が一緒に見えるので、
    phase_overridesの割り当てが妥当かもここで判断できる。
    """

    def __init__(self, cost: CostMeter) -> None:
        self.cost = cost

    def __call__(self, event) -> None:
        head = (
            f"  [生成] {event.agent} / {event.phase.value} / {event.model} / {event.elapsed:.1f}秒"
        )
        if event.attempt > 1:
            head += f" / {event.attempt}回目"

        # 失敗した試行もここへ流れてくる。トークンは分からないので出さない。
        # 落とすと、再試行に費やした時間が計測から消える。
        if event.error:
            print(f"{head} / 失敗: {event.error.splitlines()[0][:80]}")
            return

        # キャッシュの内訳は、効いている時だけ出す。
        # 0を毎回並べても読み取れる情報が増えない。
        detail = ""
        if event.cached_tokens:
            rate = event.cached_tokens / event.input_tokens * 100
            detail += f" (cached {event.cached_tokens:,} / {rate:.0f}%)"
        if event.cache_write_tokens:
            detail += f" (cache書込 {event.cache_write_tokens:,})"

        # 概算コスト。1回あたりは小さいので桁を多めに出す。
        # 単価表に無いモデルは何も出さない（0円と書くと無料だと読める）。
        jpy = self.cost.add(event)
        money = f" / 約{jpy:.3f}円" if jpy is not None else " / 単価未登録"
        print(f"{head} / in {event.input_tokens:,}{detail} out {event.output_tokens:,}{money}")


def retry_on_overload(*, agent: str, phase, model: str, attempt: int, error: Exception) -> bool | str:
    """
    生成が失敗した時の再試行。Trueなら同じモデルで、モデル名を返すとそのモデルでやり直す。

    登録しなければ例外がそのまま外へ出る。「何回待つか」「どこへ逃がすか」は
    フレームワークが決めない。
    """
    text = str(error)

    # 枠を使い切った場合は、待っても同じエラーが返る。同じ条件での
    # やり直しでは越えられないので、既定のモデルへ逃がす。
    if ("429" in text or "RESOURCE_EXHAUSTED" in text) and model != FALLBACK_MODEL:
        print(f"  [再試行] {agent} / {phase.value} / 枠切れのため {FALLBACK_MODEL} でやり直します。")
        return FALLBACK_MODEL

    # 一時的な過負荷なら、待てば同じモデルで通ることが多い。
    # 引数の不正やスキーマの誤りは何度やっても同じ結果になるので対象外。
    temporary = "503" in text or "UNAVAILABLE" in text
    if not temporary or attempt >= 3:
        return False

    wait = 2.0 * attempt  # 待つのはこちらの仕事。回を追うごとに延ばす
    print(f"  [再試行] {agent} / {phase.value} / {attempt}回目が失敗。{wait:.0f}秒待って再試行します。")
    time.sleep(wait)
    return True


def build_interceptor(cost: CostMeter) -> Interceptor:
    """1リクエスト分のinterceptorを作る。costはリクエストをまたいで積み上げる。"""
    interceptor = Interceptor()

    # 観測を判定より先に登録する。判定は登録順に呼ばれ、1つが False を返した
    # 時点で後続は呼ばれない。観測を後ろに置くと、拒否された実行の引数だけが
    # ログに出ないことになり、「なぜ止まったのか」を追えなくなる。
    interceptor.on.before_execute(ArgumentWatcher())
    interceptor.on.before_execute(Guard())

    # 利用者の画面へ — 実行の開始だけを流す。
    # 各 Tool / Agent の execution_message が展開された文言が届く。
    # ここに「どのtoolなら何を出すか」の分岐は書かない。
    # 分岐を書くと、toolを足すたびに定義とUIの2箇所を直すことになる。
    interceptor.on.execute_start(lambda message: print(f"  ▷ {message}"))

    interceptor.on.memory_diff(print_memory_diff)
    interceptor.on.generated(GenerationPrinter(cost))
    interceptor.on.generation_failed(retry_on_overload)

    # 開発者向けのログへ — 内部の文言なので画面へは出さない。
    # execute_blocked や error は利用者に見せる文面ではなく、
    # 見せるなら「ただいま受付できません」等へ翻訳してから出す。
    for event in (
        "respond_start",
        "respond_end",
        "execute_end",
        "execute_blocked",
        "memory_updated",
        "error",
    ):
        getattr(interceptor.on, event)(lambda message, event=event: print(f"  [{event}] {message}"))

    return interceptor

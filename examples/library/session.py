"""
1つの会話。cli.py / chat.py / api.py はどれも、これを通してエージェントを呼ぶ。
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Self

from statecraft import AgentResponse, Network, Suspended

from .carry_over import CarryOver
from .network import build_network
from .observers import CostMeter, build_interceptor


@dataclass
class Reply:
    """front の回答。front の output_schema（message / awaiting_reply）の形。"""

    message: str
    # 利用者からの返答を待っているか。画面では入力欄の出し方に使う
    awaiting_reply: bool
    # 利用者に選んでもらう候補。依頼が途中で止まっている時だけ入る（→ Suspension）
    options: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Reply":
        # ReflexAgent の output_schema は強制されず、指示文として伝わるだけ。
        # 形が守られなかった場合は、本文をそのまま message として扱う。
        body = text.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            data = json.loads(body)
            return cls(message=str(data["message"]), awaiting_reply=data.get("awaiting_reply") is True)
        except (json.JSONDecodeError, KeyError, TypeError):
            return cls(message=text, awaiting_reply=False)


@dataclass
class Suspension:
    """
    途中で止まった依頼。利用者の答えが届くまで、ここに置いておく。

    Networkは持たず、net.save_state() の JSON だけを持つ。答えは別のリクエストで
    届く（サーバーなら別のプロセスのこともある）ので、その時に新しいNetworkへ戻して続ける。
    """

    # net.save_state() の戻り値
    state: dict
    # net.resume(answers={point: 答え}) のキー
    point: str
    # 利用者へ見せるもの。identify_title が suspend() に渡した形
    question: str
    options: list[str]
    # このターンの発言（依頼、尋ねたこと、答え）。依頼が終わってから会話履歴へ足す
    turn: list[dict]

    def resolve(self, text: str) -> str:
        """
        番号で答えたら、その候補の文字列にする。候補に無い返答はそのまま渡す。

        答えはそのまま tool の結果になる。番号のままだと、受け取った側には
        「2」としか見えない。
        """
        if text.isdigit() and 1 <= int(text) <= len(self.options):
            return self.options[int(text) - 1]
        return text


class LibrarySession:
    """
    1つの会話。ターンごとにNetworkを作り直し、会話履歴と引き継ぐ記憶だけを次へ持ち越す。

    Networkを使い回さないのは、1つのNetworkが1リクエスト分の入れ物だから。
    使い回すと、前のターンの facts や decisions まで次のターンに残る。

    依頼が途中で止まったら（利用者に候補を選んでもらう時）、状態を保存して答えを待つ。
    止まっている間の発言は ask ではなく answer に渡す。
    """

    def __init__(self, carry_over: CarryOver | None = None, cost: CostMeter | None = None) -> None:
        self.history: list[dict] = []
        self.carry_over = carry_over or CarryOver()
        self.cost = cost or CostMeter()
        # 依頼が途中で止まっている間だけ入る
        self.suspension: Suspension | None = None
        # 直前のターン。記憶やトークンの内訳を表示するときに使う
        self.network: Network | None = None
        self.response: AgentResponse | None = None

    def ask(self, message: str) -> Reply:
        """新しい依頼を始める。"""
        net = build_network(build_interceptor(self.cost))
        self.carry_over.apply(net)

        # respond が人間からの入口。会話履歴や添付もここで受け取る。
        #   net.respond(agent="front", message=..., history=[...], blobs=[...])
        result = net.respond(agent="front", message=message, history=self.history)
        return self._settle(net, result, [{"role": "user", "content": message}])

    def answer(self, text: str) -> Reply:
        """止まっている依頼へ利用者の答えを渡し、止まった所から続ける。"""
        if self.suspension is None:
            raise RuntimeError("止まっている依頼がありません")
        suspension = self.suspension
        choice = suspension.resolve(text)

        # 止まった時のNetworkは使わず、保存した状態を新しいNetworkへ戻す。
        # 戻るのは記憶と実行の途中経過で、設定は今の config/ と prompts/ から組んだもの。
        # load_state は戻すだけで動かさない。動かすのは resume。
        net = build_network(build_interceptor(self.cost))
        net.load_state(suspension.state)
        result = net.resume(answers={suspension.point: choice})
        return self._settle(net, result, [*suspension.turn, {"role": "user", "content": choice}])

    def _settle(self, net: Network, result: AgentResponse | Suspended, turn: list[dict]) -> Reply:
        """止まっていれば状態を保存して答えを待ち、終わっていればターンを閉じる。"""
        self.network = net
        if isinstance(result, Suspended):
            # 同時に複数の所で止まることもある（並列の呼び出し）。1つずつ聞く。
            # 答えなかった所は、再開した時に実行し直されて、また止まる。
            pending = result.pending[0]
            question, options = pending.payload["question"], pending.payload["options"]
            self.suspension = Suspension(
                state=net.save_state(),
                point=pending.point,
                question=question,
                options=options,
                turn=[*turn, {"role": "assistant", "content": question}],
            )
            self.response = None
            return Reply(message=question, awaiting_reply=True, options=options)

        self.suspension = None
        reply = Reply.parse(result.text)
        # 会話履歴には、利用者に見せた本文だけを残す
        self.history += [*turn, {"role": "assistant", "content": reply.message}]
        self.carry_over = CarryOver.capture(net)
        self.response = result
        return reply

    def dump(self) -> dict:
        """
        会話を JSON にできる形で取り出す。プロセスをまたいで続ける時に保存する。

        止まった依頼の状態もこの中に入る。Networkは入らない（load の後、
        answer が組み直す）。
        """
        return {
            "history": self.history,
            "carry_over": self.carry_over.rows,
            "suspension": asdict(self.suspension) if self.suspension else None,
        }

    @classmethod
    def load(cls, data: dict, cost: CostMeter | None = None) -> Self:
        """dump() で取り出したものから、会話を戻す。"""
        session = cls(CarryOver(data["carry_over"]), cost)
        session.history = data["history"]
        if data["suspension"] is not None:
            session.suspension = Suspension(**data["suspension"])
        return session

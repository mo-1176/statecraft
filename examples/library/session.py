"""
1つの会話。cli.py / chat.py / api.py はどれも、これを通してエージェントを呼ぶ。
"""

import json
from dataclasses import dataclass

from statecraft import AgentResponse, Network

from .carry_over import CarryOver
from .network import build_network
from .observers import CostMeter, build_interceptor


@dataclass
class Reply:
    """front の回答。front の output_schema（message / awaiting_reply）の形。"""

    message: str
    # 利用者からの返答を待っているか。画面では入力欄の出し方に使う
    awaiting_reply: bool

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


class LibrarySession:
    """
    1つの会話。ターンごとにNetworkを作り直し、会話履歴と引き継ぐ記憶だけを次へ持ち越す。

    Networkを使い回さないのは、1つのNetworkが1リクエスト分の入れ物だから。
    使い回すと、前のターンの facts や decisions まで次のターンに残る。
    """

    def __init__(self, carry_over: CarryOver | None = None, cost: CostMeter | None = None) -> None:
        self.history: list[dict] = []
        self.carry_over = carry_over or CarryOver()
        self.cost = cost or CostMeter()
        # 直前のターン。記憶やトークンの内訳を表示するときに使う
        self.network: Network | None = None
        self.response: AgentResponse | None = None

    def ask(self, message: str) -> Reply:
        net = build_network(build_interceptor(self.cost))
        self.carry_over.apply(net)

        # respond が人間からの入口。会話履歴や添付もここで受け取る。
        #   net["front"].respond(message=..., history=[...], blobs=[...])
        response = net["front"].respond(message=message, history=self.history)
        reply = Reply.parse(response.text)

        # 会話履歴には、利用者に見せた本文だけを残す
        self.history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply.message},
        ]
        self.carry_over = CarryOver.capture(net)
        self.network, self.response = net, response
        return reply

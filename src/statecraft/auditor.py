"""
組み込みの監査役。Networkを作るときに enable_auditor を渡すと有効になる。

    network = Network(
        agents=[front, scout],
        enable_auditor={"llm": OpenAILLM(), "model": "gpt-6-luna"},
    )

記憶を持つエージェントは、それぞれ自分の受けた指示やナレッジの範囲では正しく
動いても、全体として依頼からずれていくことがある（調べすぎる、早く終わる、
別の方向へ進む、要件に無い規則を当てはめる）。どれも記憶の更新（次に何を
するかと今の理解）に現れるので、当てる前の差分を監査役に見せ、ずれていれば
理由を添えて直させる。

仕組みは guard.before_memory_diff そのもので、ここにあるのはその標準の実装。
監査の中身を変えたい時は、これを使わずに自分の監査を before_memory_diff へ
登録する（組み込みの監査役と並べてもよい）。
"""

import json
import logging
import threading
from typing import NotRequired, TypedDict

from .agent import Agent, ReflexAgent
from .events import BeforeMemoryDiffEvent
from .interceptor import Interceptor, MemoryDiffDecision
from .llm import BaseLLM, ThoughtLevel
from .prompts import Phase, join_sections

logger = logging.getLogger(__name__)

# 監査役の名前。監査役の生成のイベントと、token_usage()の内訳にこの名前で現れる。
AUDITOR_NAME = "auditor"

# この回数まで差し戻したら、次の差分は監査せずに通す。差し戻すたびに記憶の
# 更新が作り直されるので、上限が無いと、監査役と食い違ったまま終わらない。
MAX_REJECTIONS = 2


class AuditorSettings(TypedDict):
    """
    enable_auditor に渡すdictの形。利用者はdictを書くだけでよく、これをimport
    する必要は無い（エディタがキーを補完し、型を確かめるためのもの）。
    """

    llm: BaseLLM
    model: str
    thought_level: NotRequired[ThoughtLevel | None]
    # 既定の監査役のプロンプト（AUDITOR_INSTRUCTION）の後ろに、そのまま付く
    system_instruction: NotRequired[str]


# 監査役の人格定義。enable_auditor の system_instruction は、この後ろに付く。
AUDITOR_INSTRUCTION = """\
# 役割

あなたは、対話型マルチエージェントAIシステムの監査役です。

システムの中では、複数のエージェントが、それぞれ自分の記憶を更新しながら動いています。記憶は、そのエージェントの思考そのものです。依頼をどう読んだか、今どう見ているか、何を判断したか、どう答えるつもりか、次に何をするかが、記憶の更新に現れます。

エージェントは、自分のプロンプトやナレッジを守ろうとして動きます。その律儀さのせいで、利用者が求めていないことにまで規則を当てはめたり、問いから離れていったりします。あなたは、その枷の外にいる人です。規則を守らせる人ではありません。外から見て、「それ、こうじゃない？」と声をかけるのが仕事です。

あなたには、記憶へ反映される前の更新が渡されます。見るのは、二つだけです。

**この更新に現れた思考は、利用者の今の問いに答えることへ向かっているか。**
**気を利かせるの度を超えた不要な先回りをしていないか。**

# 材料

- 利用者とのやり取り：問いの基準です。利用者自身の言葉で読みます。
- そのエージェントが受けた依頼：利用者の言葉そのものか、依頼してきたエージェントが利用者の言葉から組み立てたものです。利用者の言葉と食い違っているなら、利用者の言葉を基準にします。
- そのエージェントが持っているナレッジ：エージェントに前もって渡してある文書です。エージェントはこれを前提に考えます。
- そのエージェントが使えるtoolと委譲先：エージェントが計画に使える手段です。あなたが使うものではありません。
- 今の記憶：この更新を反映する前の、エージェントの思考です。
- 今回の更新：何を知って、考えをどう変えようとしているかです。
- 前回この更新を差し戻した理由：同じ更新を、あなたが前に差し戻した時の理由です。今回の更新は、それを受けてエージェントが直したものです。

ナレッジやtoolの使い方は、エージェントが自分で従うものです。それに沿っているかは、あなたが見ることではありません。あなたが見るのは、それに縛られて、利用者の求めていることから離れていないかです。

# 判定

- 気になる所が無ければ、通してください。迷ったら、通してください。差し戻すのは、このまま進むと答えがずれると思う時だけです。
- 差し戻す時は、「〜を考えた方がいいんじゃない？」くらいの温度で、利用者の問いとエージェントの思考がどうずれていそうかを理由に書きます。
- あなたも外から見ているだけで、エージェントが持っている手がかりのすべてを知っているわけではありません。どう直すかは、エージェントが決めます。"""

# 判定は文面ではなく構造で受け取る。文面を解釈して可否を決めると、
# そこが新しい失敗の入り口になる。
AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {
            "type": "boolean",
            "description": "この更新を通してよいか",
        },
        "reason": {
            "type": "string",
            "description": "差し戻す時、利用者の問いから見てどうずれていそうかを、助言として書く。通す時は空でよい",
        },
    },
    "required": ["approved", "reason"],
}


def _knowledge(agent: Agent, phase: Phase) -> str:
    """
    差分を作ったエージェントが、この生成で読んでいたナレッジ。

    エージェントはナレッジを前提に考えるので、監査役に渡さないと、ナレッジに
    書いてあることが根拠のない思い込みに見える。initial_knowledge は初期の
    記憶を作る時にだけ載るので、その時だけ渡す。
    """
    parts = [agent.knowledge]
    if phase == Phase.INITIAL_MEMORY:
        parts.append(agent.initial_knowledge)
    return "\n\n".join(part for part in parts if part)


def _capabilities(agent: Agent) -> str:
    """
    差分を作ったエージェントが使えるtoolと委譲先の、summary・usage・evaluation。

    計画の良し悪しは、そのエージェントがどんな手段を持っているかと並べないと
    判断できない。結果そのものは渡さないので、結果をどう読むものか（evaluation）を
    渡す。監査役自身の手段ではないことは、プロンプトの材料の節に書いてある。
    """
    lines = []
    for kind, members in (("tool", agent.tools), ("agent", agent.sub_agents)):
        for member in members:
            lines.append(f"- {member.name}（{kind}）: {member.summary}")
            if member.usage:
                lines.append(f"  使い方: {member.usage}")
            if member.evaluation:
                lines.append(f"  結果の読み方: {member.evaluation}")
    return "\n".join(lines)


def _material(e: BeforeMemoryDiffEvent, knowledge: str, capabilities: str) -> str:
    """
    監査役へ渡す材料。原文・ナレッジ・手段・今の記憶・差分と、同じ更新を前に
    差し戻した理由を、見出しで分けて並べる。

    toolの結果そのものは渡さない。監査するのは向きであって、事実の照合ではない。
    結果を渡すと、監査役が結果の中身を読み込んで、差分と突き合わせ始める。
    """
    rows = "\n".join(json.dumps(row, ensure_ascii=False) for row in e.rows)
    previous = "\n".join(
        f"{i}回目: {reason}" for i, reason in enumerate(e.rejections, start=1)
    )
    return "\n\n".join(
        [
            f"【監査する差分】{e.agent} の {e.phase}（{e.step_no}周目）",
            f"【利用者とのやり取り】\n{e.conversation or '（なし）'}",
            f"【{e.agent}が受けた依頼】\n{e.request or '（なし）'}",
            f"【{e.agent}が持っているナレッジ】\n{knowledge or '（なし）'}",
            f"【{e.agent}が使えるtoolと委譲先】\n{capabilities or '（なし）'}",
            (
                f"【今の記憶（この差分を当てる前）】\n共有:\n{e.shared_memory or '（空）'}\n"
                f"{e.agent}自身:\n{e.private_memory or '（空）'}"
            ),
            f"【今回の差分】\n{rows or '（空）'}",
            f"【前回この更新を差し戻した理由】\n{previous or '（なし）'}",
        ]
    )


class Auditor:
    """
    記憶の更新を監査する。Networkが作り、before_memory_diffへ登録する。

    監査のたびに監査役（ReflexAgent）を作り直す。使い回すと、前の監査の依頼や
    判定を消してから使うために1件ずつ順番に監査することになり、並列に動く
    エージェントの記憶の更新を待たせる。

    監査できなかった時（監査役の失敗、判定の読み違い）は通す。差し戻しに倒すと、
    同じ記憶の更新が作り直され続ける。
    """

    def __init__(
        self,
        settings: AuditorSettings,
        agents: dict[str, Agent],
        interceptor: Interceptor,
    ) -> None:
        _check_settings(settings)
        # 監査役の生成のイベントは、この名前で流れる。同じ名前のエージェントが
        # いると、観測も費用の内訳も、どちらのものか分からなくなる。
        if AUDITOR_NAME in agents:
            raise ValueError(
                f"enable_auditorを渡すときは、'{AUDITOR_NAME}' という名前のagentは"
                f"使えません（組み込みの監査役の名前です）。"
            )
        self._settings = settings
        # 監査する相手のエージェント。ナレッジと手段をここから引く。
        self._agents = agents
        # 監査役の生成も、ネットワークと同じinterceptorを通す（費用の上限や
        # 観測が、監査の分にもそのまま効く）。
        self._interceptor = interceptor
        # 監査役は毎回作り直すので、消費したトークンはここに積む。
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # 監査は並列に走るので、累計への足し込みだけは1件ずつにする。
        self._tokens_lock = threading.Lock()

    def review(self, e: BeforeMemoryDiffEvent) -> MemoryDiffDecision:
        if len(e.rejections) >= MAX_REJECTIONS:
            logger.info(
                "[監査] %s/%s step%d %d回差し戻したので、監査せずに通します",
                e.agent,
                e.phase,
                e.step_no,
                len(e.rejections),
            )
            return MemoryDiffDecision(ok=True)

        agent = self._agents.get(e.agent)
        material = _material(
            e,
            _knowledge(agent, e.phase) if agent else "",
            _capabilities(agent) if agent else "",
        )
        judge = self._judge()
        result = judge.execute(kwargs={"message": material})
        with self._tokens_lock:
            self.total_input_tokens += judge.total_input_tokens
            self.total_output_tokens += judge.total_output_tokens

        if not result.success:
            logger.warning(
                "[監査] %s/%s step%d を監査できなかったので通します: %s",
                e.agent,
                e.phase,
                e.step_no,
                result.error,
            )
            return MemoryDiffDecision(ok=True)

        try:
            verdict = json.loads(result.value)
            approved = verdict["approved"] is True
            reason = str(verdict.get("reason") or "")
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            logger.warning(
                "[監査] %s/%s step%d の判定を読めなかったので通します: %s",
                e.agent,
                e.phase,
                e.step_no,
                error,
            )
            return MemoryDiffDecision(ok=True)

        logger.log(
            logging.INFO if approved else logging.WARNING,
            "[監査] %s/%s step%d 差分%d回目 %s%s",
            e.agent,
            e.phase,
            e.step_no,
            e.review_round,
            "通す" if approved else "差し戻し",
            f": {reason}" if reason else "",
        )
        return MemoryDiffDecision(ok=approved, reason=reason)

    def reset(self) -> None:
        """Network.reset()から呼ばれる。リクエストをまたいで残るのは累計だけ。"""
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _judge(self) -> ReflexAgent:
        settings = self._settings
        return ReflexAgent(
            name=AUDITOR_NAME,
            summary="記憶の更新が、利用者の依頼に向かっているかを監査します。",
            llm=settings["llm"],
            model=settings["model"],
            thought_level=settings.get("thought_level"),
            system_instruction=join_sections(
                AUDITOR_INSTRUCTION, settings.get("system_instruction", "")
            ),
            output_schema=AUDIT_SCHEMA,
            # 材料を読んで判定するだけで、調べる必要が無い
            max_steps=1,
            # 監査のたびに作り直す1回きりの判定なので、継承する思考が無い。
            # オンのままだと、継承に対応していないLLM（ClaudeLLMなど）で毎回落ち、
            # 監査できずに全部通ってしまう。
            carry_thoughts=False,
            interceptor=self._interceptor,
        )


def _check_settings(settings: object) -> None:
    """
    enable_auditor の中身を、構築時に確かめる。

    dictは打ち間違えても作れてしまい、知らないキーは黙って無視される
    （設定ファイルの読み込みで、知らない項目名をエラーにしているのと同じ理由）。
    """
    # 型の不正ではなく設定の誤りなので、他の構築時検証と揃えてValueErrorにする。
    if not isinstance(settings, dict):
        raise ValueError(  # noqa: TRY004
            "enable_auditorには、監査役の設定をdictで渡してください"
            '（例: {"llm": OpenAILLM(), "model": "..."}）。'
        )
    required = AuditorSettings.__required_keys__
    optional = AuditorSettings.__optional_keys__
    errors = []
    if unknown := set(settings) - required - optional:
        errors.append(f"知らない項目があります: {sorted(unknown)}")
    if missing := required - set(settings):
        errors.append(f"{sorted(missing)} がありません")
    if errors:
        raise ValueError(
            f"enable_auditorの設定が正しくありません。{' / '.join(errors)}"
            f"（必須: {sorted(required)}、任意: {sorted(optional)}）"
        )

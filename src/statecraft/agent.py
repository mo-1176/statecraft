"""
Agentの本体。ReActループ、プロンプトの組み立て、他Agentへの委譲を担う。

ReflexAgentはAgentを継承し、記憶に関わるhookを空にするだけ。
使い分けと各フィールドの意味はREADMEを参照。

【1回の応答で何が起きるか】
    respond() または execute()
      → _begin_run()      前回の実行の痕跡を捨てる
      → 初期ツールを実行し、最初の記憶を作る（_after_initial_tools。Agentのみ）
      → ループ開始
          _select_invokables()  次に呼ぶ対象を決める
          ・対象がある → 生成してツールを呼ぶ → 結果を記憶へ（_after_tools）
          ・対象がない → ループを抜けて回答へ
      → _final_answer()   回答を生成する

【ツールの呼び出しで、いったん手を離す】
    ループは、ツールや他のエージェントを自分で呼ばない。呼び出しの計画（バッチ）を
    返して手を離し、結果が揃ったら _continue_after_batch() から続きを進める。
    実行するのは起動の表（runtime.py）で、委譲先のエージェントもそこで子の起動として
    進む。親子をコールスタックではなく表のデータで持つので、途中で中断して状態を
    保存し、後で再開できる。

【外から呼ばれる入口は2つ】
    respond()  人間からの入口。会話履歴や添付を受け取る
    execute()  他のAgentから委譲された時の入口。Toolと同じ形で呼べる
               （呼び出し側がtoolとagentを区別せずに扱える。invokable.py参照）
"""

import copy
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Optional

from .events import (
    BeforeExecuteEvent,
    BeforeGenerateEvent,
    BeforeMemoryDiffEvent,
    ErrorEvent,
    ExecuteBlockedEvent,
    ExecutedEvent,
    ExecuteEndEvent,
    ExecuteStartEvent,
    FailureKind,
    GeneratedEvent,
    GenerationFailedEvent,
    MemoryDiffEvent,
    MemoryUpdatedEvent,
    RespondEndEvent,
    RespondStartEvent,
)
from .interceptor import Interceptor, MemoryDiffDecision
from .invokable import Invokable
from .llm import (
    BaseLLM,
    CallOutcome,
    Exchange,
    FunctionCall,
    LLMResponse,
    ThoughtLevel,
)
from .memory import (
    DIFF_EXAMPLE,
    DISABLE_FIELD,
    ROUTING_GUIDE,
    DiffError,
    MemoryEntry,
    PrivateMemory,
    SharedMemory,
    TaskStatus,
    apply_diff,
    build_diff_schema,
)
from .prompts import (
    PHASE_SPECS,
    POURING,
    STALL_NOTICE,
    Phase,
    PhaseSpec,
    join_sections,
    section,
)
from .runtime import Runtime, Stage, Suspended, _Batch, _Finished, _PlannedCall
from .state import (
    decode,
    dump_diff_error,
    dump_memory,
    dump_result,
    encode,
    load_diff_error,
    load_result,
    restore_memory,
)
from .suspension import Suspend
from .tools import Tool, ToolResult
from .utils import format_message, with_timing

# 単体で動かしたAgent（Networkに入れずにrespond / execute）が中断した時の文言。
# 中断した状態を保存して戻す入口（save_state / load_state / resume）はNetworkにある。
_SUSPEND_OUTSIDE_NETWORK = (
    "中断（suspend）は、Networkに入れて動かした時だけ使えます。"
    "Agentを単体で動かすと、中断した状態を保存して再開する入口がありません。"
)


def _extract_rows(text: str) -> list | None:
    """
    LLMの応答からmemory差分の配列を取り出す。

    配列を構造的に強制できないプロバイダでは、前後に文章が付いたり
    オブジェクトで包まれたりするため、ここで吸収する。
    取れなければNone（呼び出し側がリトライで直させる）。
    """
    if not text:
        return None

    def as_rows(parsed):
        if isinstance(parsed, list):
            return parsed
        # {"updates": [...]} のように1つの配列で包まれていた場合は中身を採用する
        if isinstance(parsed, dict):
            arrays = [v for v in parsed.values() if isinstance(v, list)]
            if len(arrays) == 1:
                return arrays[0]
        return None

    try:
        rows = as_rows(json.loads(text))
        if rows is not None:
            return rows
    except json.JSONDecodeError:
        pass

    # 前後に説明文が付いている場合に備えて、最初の[から最後の]までを試す
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            return as_rows(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            return None
    return None


def _merge_rows(held: list, fixes: list) -> list:
    """
    差し戻した差分に、作り直しで出た行を重ねる。同じfieldとidの行は置き換え、
    新しい行は後ろへ足す。

    作り直しでは直す行だけを出させるので、出なかった行は差し戻した差分のまま
    残す。形の崩れた行（dictでない）は、そのまま足して適用側の検査に任せる。
    """

    def key(row: object) -> tuple | None:
        if isinstance(row, dict):
            return (row.get("field"), row.get("id"))
        return None

    fixed = {key(row): row for row in fixes if key(row) is not None}
    merged = [fixed.pop(key(row), row) if key(row) else row for row in held]
    # 置き換えに使わなかった行は、新しく足す行
    merged.extend(row for row in fixes if key(row) is None or key(row) in fixed)
    return merged


class GenerationAborted(Exception):
    """
    before_generateが生成を中止した。

    例外にしているのは、空の応答を返すとループが同じ場所を回り続け、
    理由も見えないままmax_stepsを消費するため。
    委譲先ではexecuteがToolResult(error=...)へ変え、respondでは外へ出る。
    """


@dataclass
class RunInput:
    """
    1回の起動（respond() / execute() 1回）で受け取った入力。

    ループの各メソッドが必要とするため、引数で引き回さずAgentが
    current_inputとして保持する。次の起動時に_begin_run()が差し替える。
    """

    # 依頼の本文。委譲ではプロンプトの【依頼内容】へ載せる。
    # 利用者から直接受けた場合は、shared_memory.conversationの方で読ませる。
    message: str

    # 呼び出し元から受け取った構造化引数（input_schemaに従う形）。
    # 初期ツールへ渡す引数は、ここから名前が一致するものだけを使う。
    kwargs: dict = field(default_factory=dict)
    # 過去のチャット履歴。[{"role": "user"|"assistant", "content": str}, ...]
    # respond経由だけが受け取り、shared_memory.conversationへ記録する
    # （LLMへはmessagesとして渡さない。窓口も委譲先もconversationで読む）。
    history: list[dict] = field(default_factory=list)
    # 添付ファイル。[{"data": bytes, "mime_type": str}, ...]
    # 画像やPDFをLLMへ渡す場合に使う。
    blobs: list[dict] = field(default_factory=list)
    # respond()（利用者からの直接の依頼）で起動したか。
    from_user: bool = False


@dataclass
class AgentResponse:
    """Agent.respond() と Network.respond() の戻り値。"""

    text: str
    steps: int = 0  # 実際に回ったループ回数

    # 消費したトークン。Network.respond()では、委譲先と監査役を含むこの依頼の全体。
    # Agent.respond()では、このAgent自身の分だけ（単体で動かす時やテスト向け）。
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class GenerationConfig:
    """
    phaseごとの生成設定の「上書き指示」。Noneは「指定なし＝既定値を使う」。

        phase_overrides={Phase.ANSWER: GenerationConfig(model="gemini-3.5-pro")}

    確定値はResolvedGenerationが持つ。
    """

    model: str | None = None
    llm: BaseLLM | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thought_level: ThoughtLevel | None = None


@dataclass
class ResolvedGeneration:
    """
    そのphaseで実際に使う生成設定。resolve_generation()の戻り値。

    GenerationConfigと項目は同じだが、必須にすることで「解決済み」を型で
    宣言している（生成へ渡す時点でNoneを考えなくて済む）。

    temperatureとthought_levelのNoneだけは「そのパラメータを送らない」
    という有効な値。
    """

    model: str
    llm: BaseLLM
    temperature: float | None
    max_tokens: int
    thought_level: ThoughtLevel | None


@dataclass
class ToolHistoryEntry:
    """
    ReActループ内で1回実行したtool/sub_agentの記録。

    結果本文は常に完全な形で保持し、隠すのはrender()側の責務。
    sub_agentの結果も、回答文をvalueに入れたToolResultとして記録する。
    """

    name: str  # 実行したtool名またはagent名
    kwargs: dict  # 渡した引数
    result: ToolResult  # 実行結果（成功/失敗と戻り値）

    # 呼ばれた側が持つ「この結果をどう解釈すべきか」の指示。
    # 結果を隠す時は一緒に隠す（要約済みなら解釈指示も役目を終えている）。
    evaluation: str = ""

    # 呼ばれた側のoutput_schema。値の意味を結果に添えるために持つ。
    # 結果を評価するphaseには軽い一覧しか載らないため、これが無いと
    # 「status=PARTIALが返った」ことは見えても意味が見えない。
    output_schema: dict | None = None

    # toolが返したmemoryによって、結果が既に記憶へ記録済みかどうか。
    # 記録済みなら履歴側では中身を繰り返さない（同じ内容を二重に載せない）。
    # tasksだけを書いた場合はFalse。tasksは結果の中身ではなく次の指示なので、
    # 書いても結果を記録したことにはならない（隠すと、評価する生成に値が届かない）。
    written_to_memory: bool = False

    # toolが返したmemoryで、このtoolが書いたtasksのid。実行済みのタスクの放置を
    # 差し戻す検査（_executed_task_ids）から外すために持つ。toolが「同じtoolを
    # もう一度」と立てたタスクを、実行済みのまま放置したと誤って差し戻さない。
    written_task_ids: list[str] = field(default_factory=list)

    def render(self, *, include_result: bool = True) -> str:
        """
        1件分をプロンプト用のテキストにする。

        成功時は戻り値だけを載せる（success:true を見せると「動いた」と
        「求めた答えが得られた」を混同する）。異常時だけ status: error を出す。

        include_result: Falseなら戻り値を載せず、実行した事実だけを残す
            （要約済みの結果を再度載せない）。エラーは常に見せる。
        """
        args = ", ".join(f"{k}={v!r}" for k, v in self.kwargs.items())
        head = f"  - {self.name}({args})"

        if not self.result.success:
            return f"{head}\n    status: error\n    {self.result.error}"
        # 既にmemoryへ記録済みなら、記憶を持つかどうかに関わらず中身を繰り返さない。
        if self.written_to_memory:
            return f"{head}\n    ※結果はmemoryへ記録済み"
        if not include_result:
            return f"{head}\n    ※結果はmemoryへ要約済み"

        body = f"{head}\n    {self.result.value}"
        # 値の意味を先に添える。何を意味する値なのかが分からないまま
        # 「どう解釈するか」を読んでも噛み合わない。
        if schema_note := self._render_schema_note():
            body += f"\n    【返ってきた値の意味】\n{schema_note}"
        if self.evaluation:
            body += f"\n    【この結果の解釈】{self.evaluation}"
        return body

    def outcome(self) -> CallOutcome:
        """
        思考を継承する時、過去の呼び出し要求に対にして送り返す結果。

        生の戻り値は入れず、実行したかどうかだけにする。戻り値は評価する1回の
        生成にだけ見せ、以降はmemoryと実行履歴が担う。会話に残すと、以降の
        すべての生成に生データが積もる。エラーは実行履歴と同じく常に見せる。
        """
        if not self.result.success:
            return CallOutcome(text=self.result.error or "", is_error=True)
        return CallOutcome(text="実行済み")

    def _render_schema_note(self) -> str:
        """
        output_schemaの各項目の説明を、結果に添える形にする。

        説明を持たないスキーマ（型だけ指定したもの）は何も返さない。
        載せる意味があるのは、値の意味がdescriptionに書かれている場合だけ。
        """
        props = (self.output_schema or {}).get("properties", {})
        lines = [
            f"      {name}: {spec['description']}"
            for name, spec in props.items()
            if isinstance(spec, dict) and spec.get("description")
        ]
        return "\n".join(lines)


def _is_blank(value: Any) -> bool:
    """
    必須の引数が実質的に空か。

    input_schemaのrequiredを満たしていても中身は空でありうる。LLMが値を
    埋められず""を入れる場合と、respond()経由でそのキー自体が存在しない場合
    （あちらのkwargsはmessageだけ）が該当する。

    真偽値では判定しない。0 / False / [] は正当な値であり、
    「件数0を指定する」のような呼び出しが空扱いになる。
    """
    return value is None or (isinstance(value, str) and not value.strip())


# 委譲される依頼を「自然言語のメッセージ1つ」で受け取るのが大半なので既定にする。
# 構造化された引数が必要なAgentだけ明示的に上書きする。
DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
}


@dataclass(repr=False)
class Agent:
    # tool実行結果をプロンプトに載せるか。このクラスは結果をmemoryへ要約する
    # ため隠す（ReflexAgentはTrueで上書き）。設定値ではないのでClassVar。
    _show_tool_results: ClassVar[bool] = False

    # 最終回答をループの中で出すか。Falseなら_final_answerでoutput_schemaを
    # response_schemaとして強制できる（ReflexAgentは強制できない）。
    _answers_in_loop: ClassVar[bool] = False

    # ---- 必須項目（デフォルト無し） ----
    name: str

    # 委譲先候補としてLLMに提示する概要。「何に詳しいか」ではなく
    # 「何を返すか」を書く（前者だと質問を投げる先として選ばれる）。
    summary: str

    # llmは接続、modelはモデル名。同じ接続を複数のAgentで使い回し、
    # モデルだけAgentごとに変えられる。
    llm: BaseLLM
    model: str

    # ---- Networkが配線する項目 ----
    # Networkが1つの実体を全Agentへ注入する（既定値は単体実行のため）。
    shared_memory: SharedMemory = field(default_factory=SharedMemory)
    # 委譲先の宣言。相互参照があるため実体では書けず、Networkが解決する。
    sub_agent_names: list[str] = field(default_factory=list)

    # ---- 他のエージェントに見せるもの（部下に持つ側のプロンプトに載る） ----
    # 委譲すると決まった時だけ渡す詳細。スキーマで表現できない制約を書く。
    usage: str = ""

    # 委譲時に受け取れる引数の形（JSON Schema）。deepcopyなのは、浅いコピーでは
    # ネストした"properties"が全インスタンスで共有されるため。
    input_schema: dict = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_INPUT_SCHEMA)
    )

    # 最終回答に強制する形式。Noneなら自然言語で回答する。
    output_schema: dict | None = None

    # Trueの間は委譲候補として提示しない。
    disabled: bool = False

    # このAgentの回答を受け取った側が、それをどう解釈すべきかの指示。
    evaluation: str = ""

    # ---- このAgent自身の振る舞い ----
    # 人格定義。llm.generate(system_instruction=...)へそのまま渡す。
    system_instruction: str = ""

    # 知識として常時プロンプトに載せる本文。
    knowledge: str = ""

    # このAgentが使えるツール。名前引き用の辞書は持たせず、リストで受け取る。
    tools: list[Tool] = field(default_factory=list)

    # sub_agent_namesからNetworkが解決して入れる。直接指定してもよい（単体実行時）。
    sub_agents: list["Agent"] = field(default_factory=list)

    private_memory: PrivateMemory = field(default_factory=PrivateMemory)

    # ReActループの上限回数。Noneは禁止（無限ループを防ぐため必ず数値を持つ）。
    max_steps: int = 10

    # 記憶の差分が不正だった時、再生成を試みる回数。
    max_memory_retries: int = 3

    # 同じ実行が何ステップ続いたら停滞とみなして打ち切るか。
    # 2なら「1度立て直しを促し、それでも変わらなければ止める」になる。
    max_stalled_steps: int = 2
    # Noneならtemperature自体を送らず、モデルの既定に任せる
    # （thought_levelと同じ扱い）。既定がNoneなのは、受け付けないモデルが
    # あるため（OpenAIの推論モデル等は値に関わらず400）。
    temperature: float | None = None
    max_tokens: int = 65535
    # どれだけ考えさせるか。Noneならパラメータ自体を送らず、モデルの動的思考に
    # 任せる。明示的にOFFにしたい場合だけThoughtLevel.NONEを指定する。
    thought_level: ThoughtLevel | None = None
    # phaseごとの生成設定の上書き。指定しなかったphaseは上の既定値を使う。
    #   phase_overrides={Phase.INITIAL_MEMORY: GenerationConfig(llm=flash_lite)}
    phase_overrides: dict[Phase, GenerationConfig] = field(default_factory=dict)
    # 前の生成の思考を、次の生成へ継承するか。reset()まで持ち続ける。
    #
    # 各社のAPIは、前の出力を思考の署名ごと会話として送り返す前提で作られて
    # いて、memoryでは運びきれない文脈を思考が運ぶ。送り返すのは、縮めた発話・
    # 出力（memoryの差分や引数つきの呼び出し）・実行したという事実だけで、
    # toolの生の戻り値は会話に残さない（_keep_exchange / ToolHistoryEntry.outcome）。
    #
    # 戻った思考は毎回入力トークンとして課金されるので、生成のたびに伸びる。
    # 違う接続（phase_overridesのllm）が作った分は送らず、同じ接続でも
    # モデルが違えばプロバイダ側で捨てられる。対応するのはGeminiLLMとOpenAILLM。
    #
    # 既定でオンにしているのは、APIがこの形を前提に作られているため。ClaudeLLMや、
    # generate()がexchangesを受け取らない自作のLLMで動かす場合はFalseにする
    # （オンのままだと最初の生成で落ちる）。
    carry_thoughts: bool = True
    # ループ開始前に機械的に1回だけ実行するtool / agent（判断させないので生成が
    # 1回分減る）。引数はinput_schemaの同名プロパティから渡される。
    #
    # 互いに依存できない——引数はinput_schemaからしか来ないので、ある要素の
    # 結果が別の要素の引数になる経路が無い。そのため並列で実行してよく、
    # 順序に意味は無い（プロンプトへ載る順序だけはこの並び順になる）。
    #
    # tools / sub_agentsへ登録するかは任意。登録しなければカタログにも候補にも
    # 出ないので、LLMからは呼べない準備ステップになる。登録すれば後から呼べる。
    #
    # agentを置く場合、この時点では記憶がまだ作られていない（INITIAL_MEMORYは
    # この後）。届いた依頼だけを見て仕事が完結するものに限る。
    initial_tools: list[Invokable] = field(default_factory=list)

    # 初期memory構築の時だけ、材料としてpromptへ載るナレッジ（任意）。
    # 主な用途はタスクの組み立て方のfew-shot。何を書くかはREADME参照。
    # knowledgeと分けているのは載る回数が違うため（ReflexAgentでは載らない）。
    initial_knowledge: str = ""

    # 最終回答を作る時だけ、promptへ載るナレッジ（任意）。
    # 主な用途は答え方の方針。memoryに何を残すかと、答えで何を話すかを
    # 分けるため、memoryを書くphaseには載せない（ReflexAgentでは載らない）。
    answer_knowledge: str = ""

    # 委譲された時に観測側へ流す文言。Tool.execution_messageと同じ扱い。
    #   execution_message="司書が調べています..."
    execution_message: str = ""
    # 条件で出し分けたい場合だけ関数を渡す。execution_messageより優先される。
    describe_execution: Callable[[dict], str] | None = None
    # 未登録でも何もしないだけなので、常に実体を持たせる
    # （呼び出し側で if self.interceptor: を書かなくて済む）。
    interceptor: Interceptor = field(default_factory=Interceptor)

    # ---- 実行中に変化する状態 ----
    tool_history: list[ToolHistoryEntry] = field(default_factory=list)
    # respond() / execute() が受け取った入力。ループ中の各メソッドがここから読む。
    current_input: RunInput | None = None
    # Networkが最初に配線した時点のTool一覧。2回目以降の配線で
    # ミューテート済みのコピーを複製元にしないために保持する。
    _source_tools: list | None = None
    # 最初に配線した時点のsub_agents（実体で直接指定された分）。Networkは
    # 解決した実体をsub_agentsへ書き込むため、こちらが無いと2回目の構築で
    # 名前と実体の併用を誤判定する。
    _source_sub_agents: list | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # total_input_tokensのうち、キャッシュから読まれた分の累計。
    total_cached_tokens: int = 0
    # 直前のステップで成功した呼び出しの署名。同一呼び出しの再実行を弾くために使う。
    last_successful_calls: set = field(default_factory=set)
    # 直近の_select_invokablesで解決できなかった (名前, 理由) の一覧。
    _unresolved_targets: list = field(default_factory=list)
    # このAgentがセッション中に無効化した対象の名前。起動ごとに戻す。
    _runtime_disabled: set = field(default_factory=set)
    # 直前のステップの指紋と、同じ指紋が続いた回数。停滞の検出に使う。
    _last_fingerprint: frozenset | None = None
    _stalled_count: int = 0
    # 前回のmemory更新で反映されなかった行（差し戻しても直らないもの、
    # リトライを使い切ったもの）。次のmemory更新で1回だけ提示して空になる。
    # 記憶へ書かないのは、直せば消えるべき情報が記憶には残り続けるため。
    _unapplied_rows: list = field(default_factory=list)
    # ReActループの何周目か。イベントへ載せるためだけに保持する。
    # ループの前に走るINITIAL_MEMORYもstep 1として扱うため1で始める。
    _step_no: int = 1

    # この起動（respond / execute 1回）を指すID。起動の表（runtime.py）が発行し、
    # _begin_run()で受け取る。中断した地点（Pending.point）にも使われる。
    current_run_id: str = ""

    # 単体で動かしたexecute()が、同じ実体へ同時に入るのを防ぐ錠。Networkの中では
    # 起動の表が同じエージェントの起動を1件ずつにするので、こちらは単体で複数の
    # スレッドから呼ばれた場合に効く。RLockは万一の再入でハングしないため。
    _lock: Any = field(default_factory=threading.RLock, compare=False)
    # toolが返した添付の蓄積。文字起こしする先を持たないReflexAgentが
    # 以降も見せ続けるために使う（記憶を持つAgentは1回渡して終わり）。
    _carried_blobs: list = field(default_factory=list)
    # carry_thoughtsで送り返す過去の生成。(作った接続, やり取り) の並び。
    # 起動をまたいでreset()まで持ち続ける。
    _exchanges: list = field(default_factory=list)
    # 呼び出し要求を返した生成。実行結果が揃うまで_exchangesへ入れない
    # （結果を対にしていない呼び出し要求は、送り返すとプロバイダが断る）。
    _awaiting_outcomes: tuple | None = None
    # この起動で何回生成したか。最初の生成の発話にだけ依頼内容を残すために使う。
    _run_generations: int = 0

    def get_tool(self, name: str) -> Tool | None:
        """名前からToolを引く。辞書はここで都度、実体（func.__name__）から導出する。"""
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def get_sub_agent(self, name: str) -> Optional["Agent"]:
        """名前からsub_agentを引く。"""
        for a in self.sub_agents:
            if a.name == name:
                return a
        return None

    def render_tool_history(self) -> str:
        """
        実行履歴をプロンプト用のテキストにする。
        結果本文を載せるかどうかはクラスの性質（_show_tool_results）で決まるため、
        呼び出し側が毎回判断する必要はない。
        """
        if not self.tool_history:
            return ""
        return "\n".join(
            e.render(include_result=self._show_tool_results) for e in self.tool_history
        )

    def to_catalog_line(self, *, include_usage: bool = False) -> str:
        """Tool.to_catalog_line()と同じ形。委譲先候補として存在を伝える行。"""
        line = f"- {self.name}: {self.summary}"
        if include_usage and self.usage:
            line += f"\n  使い方: {self.usage}"
        return line

    def resolve_generation(self, phase: Phase) -> ResolvedGeneration:
        """
        そのphaseで使う生成設定を確定させる。指定のある項目だけを差し替える。
        ここが「指定なしのNone」が消える唯一の場所。

        `o.temperature or self.temperature` と書くと temperature=0 が偽と
        判定されて既定値に差し替わるため、必ず is None で判定する。
        """
        # 打ち切り・停滞で答える時も「答える生成」なので、ANSWERの上書きを使う
        # （3つの違いはループの終わり方と指示文だけで、利用者が分けて書く理由が無い）。
        if phase in (Phase.CUTOFF_ANSWER, Phase.STALLED_ANSWER):
            phase = Phase.ANSWER
        o = self.phase_overrides.get(phase) or GenerationConfig()
        return ResolvedGeneration(
            model=o.model if o.model is not None else self.model,
            llm=o.llm if o.llm is not None else self.llm,
            temperature=o.temperature
            if o.temperature is not None
            else self.temperature,
            max_tokens=o.max_tokens if o.max_tokens is not None else self.max_tokens,
            thought_level=o.thought_level
            if o.thought_level is not None
            else self.thought_level,
        )

    def to_declaration(self) -> dict:
        """
        このAgentを部下に持つ側へ見せる宣言情報。Tool.to_declaration()と同じ形。

        descriptionは summary + usage + 配下の能力一覧（1段だけ）。
        孫以降は、トークンが増える割に選定の判断材料にならないため見せない。
        """
        parts = [self.summary]
        if self.usage:
            parts.append(self.usage)

        catalog = self._render_capability_catalog()
        if catalog:
            parts.append("このエージェントが使用できるtool / agent:\n" + catalog)

        # 項目の名前だけを見せる。委譲先を選ぶ段階で必要なのは「何が返るか」の
        # 形だけで、値の意味は結果を受け取った後にToolHistoryEntryが添える。
        # スキーマ全体だとdescriptionまで入り、委譲先の全員分が毎ステップ載る。
        if props := (self.output_schema or {}).get("properties"):
            parts.append("このエージェントは次の項目で回答を返す: " + ", ".join(props))

        return {
            "name": self.name,
            "description": "\n".join(parts),
            "parameters": self.input_schema,
        }

    def reset(self) -> None:
        """
        次のリクエストへ持ち込む痕跡が無い状態へ戻す。

        _begin_run()は同じリクエスト内の再呼び出し用でgoalsと累計を残すが、
        こちらはリクエストの区切りなので残すものが無い。
        自動では走らない（区切りを知っているのは利用側だけ）。
        全員へ配るならNetwork.reset()。
        """
        # 空にするのではなく作り直す（項目が増えた時の消し忘れを防ぐ）。
        self.private_memory = type(self.private_memory)()

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens = 0

        # このAgentが無効化した対象を戻す。_runtime_disabledを空にするだけでは
        # toolの実体に立てたdisabledが残る。
        self._restore_disabled()

        # 起動ごとの状態も消す。_begin_run()と重複するが、reset()の後に
        # 前のリクエストの値が読める状態を残さないため。
        self.current_input = None
        self.tool_history = []
        self.last_successful_calls = set()
        self._unresolved_targets = []
        self._last_fingerprint = None
        self._stalled_count = 0
        self._unapplied_rows = []
        self._step_no = 1
        self._carried_blobs = []
        self.current_run_id = ""
        # 継承する思考も、次のリクエストへは持ち込まない。
        self._exchanges = []
        self._awaiting_outcomes = None
        self._run_generations = 0

    # ---- 中断した状態の保存と復元（Network.save_state / load_state） ----
    def _dump_state(self) -> dict:
        """
        記憶と、起動の途中の状態。JSONにできる形で返す。

        設定（人格定義・tool・llm）は含めない。同じ設定で組み直したNetworkへ
        読み込む前提で、入れるのは実行して変わったものだけ。
        """
        run = self.current_input
        return {
            "private_memory": dump_memory(self.private_memory),
            "tokens": [
                self.total_input_tokens,
                self.total_output_tokens,
                self.total_cached_tokens,
            ],
            "current_input": None
            if run is None
            else {
                "message": run.message,
                "kwargs": encode(run.kwargs, f"{self.name}への依頼の引数"),
                "history": encode(run.history, f"{self.name}への依頼の会話履歴"),
                "blobs": encode(run.blobs, f"{self.name}への依頼の添付"),
                "from_user": run.from_user,
            },
            "current_run_id": self.current_run_id,
            "step_no": self._step_no,
            "tool_history": [
                {
                    "name": e.name,
                    "kwargs": encode(e.kwargs, f"{self.name}の実行履歴の引数"),
                    "result": dump_result(e.result, f"{self.name}の実行履歴の '{e.name}'"),
                    "evaluation": e.evaluation,
                    "output_schema": e.output_schema,
                    "written_to_memory": e.written_to_memory,
                    "written_task_ids": list(e.written_task_ids),
                }
                for e in self.tool_history
            ],
            "last_successful_calls": sorted(self.last_successful_calls),
            "unresolved_targets": [list(t) for t in self._unresolved_targets],
            "runtime_disabled": sorted(self._runtime_disabled),
            "last_fingerprint": None
            if self._last_fingerprint is None
            else [list(t) for t in self._last_fingerprint],
            "stalled_count": self._stalled_count,
            "unapplied_rows": [
                dump_diff_error(e, f"{self.name}の反映されなかった行")
                for e in self._unapplied_rows
            ],
            "carried_blobs": encode(self._carried_blobs, f"{self.name}が抱えている添付"),
            "exchanges": [self._dump_exchange(llm, ex) for llm, ex in self._exchanges],
            "awaiting_outcomes": None
            if self._awaiting_outcomes is None
            else self._dump_exchange(*self._awaiting_outcomes),
            "run_generations": self._run_generations,
        }

    def _load_state(self, data: dict) -> None:
        """_dump_state()の逆。reset()した直後の実体へ書き戻す。"""
        restore_memory(self.private_memory, data["private_memory"])
        (
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_cached_tokens,
        ) = data["tokens"]
        raw = data["current_input"]
        self.current_input = (
            None
            if raw is None
            else RunInput(
                message=raw["message"],
                kwargs=decode(raw["kwargs"]),
                history=decode(raw["history"]),
                blobs=decode(raw["blobs"]),
                from_user=raw["from_user"],
            )
        )
        self.current_run_id = data["current_run_id"]
        self._step_no = data["step_no"]
        self.tool_history = [
            ToolHistoryEntry(
                name=e["name"],
                kwargs=decode(e["kwargs"]),
                result=load_result(e["result"]) or ToolResult(),
                evaluation=e["evaluation"],
                output_schema=e["output_schema"],
                written_to_memory=e["written_to_memory"],
                # この項目より前に保存した状態には無い（無ければ書いていない）。
                written_task_ids=list(e.get("written_task_ids", [])),
            )
            for e in data["tool_history"]
        ]
        self.last_successful_calls = set(data["last_successful_calls"])
        self._unresolved_targets = [tuple(t) for t in data["unresolved_targets"]]
        # 無効化は名前と、toolの実体のdisabledの両方で持つ（_apply_disable_rowsと同じ）。
        for name in data["runtime_disabled"]:
            target = self._resolve_invokable(name)
            if target is not None and not isinstance(target, Agent):
                target.disabled = True
            self._runtime_disabled.add(name)
        fingerprint = data["last_fingerprint"]
        self._last_fingerprint = (
            None if fingerprint is None else frozenset(tuple(t) for t in fingerprint)
        )
        self._stalled_count = data["stalled_count"]
        self._unapplied_rows = [load_diff_error(e) for e in data["unapplied_rows"]]
        self._carried_blobs = decode(data["carried_blobs"])
        self._exchanges = [
            pair for e in data["exchanges"] if (pair := self._load_exchange(e))
        ]
        awaiting = data["awaiting_outcomes"]
        self._awaiting_outcomes = None if awaiting is None else self._load_exchange(awaiting)
        self._run_generations = data["run_generations"]

    def _llm_key(self, llm: BaseLLM) -> str:
        """
        継承する思考を作った接続を、保存できる名前で指す。実体は保存できないので、
        Agentのどこに設定された接続か（llm / phase_overridesのどのphase）で持つ。
        """
        if llm is self.llm:
            return "llm"
        for phase, override in self.phase_overrides.items():
            if override.llm is llm:
                return f"phase:{phase.name}"
        return ""

    def _dump_exchange(self, llm: BaseLLM, exchange: Exchange) -> dict:
        return {
            "llm": self._llm_key(llm),
            "prompt": exchange.prompt,
            # 中身を解釈できるのは作ったプロバイダだけなので、変換も任せる。
            "reply": encode(llm.dump_reply(exchange.reply), f"{self.name}の継承する思考"),
            "outcomes": [[o.text, o.is_error] for o in exchange.outcomes],
        }

    def _load_exchange(self, data: dict) -> tuple[BaseLLM, Exchange] | None:
        """
        保存した思考を、今の設定の接続へ結び直す。その接続が設定から消えていれば
        捨てる（送り返す先が無い）。
        """
        key = data["llm"]
        llm: BaseLLM | None = None
        if key == "llm":
            llm = self.llm
        elif phase := Phase.__members__.get(key.removeprefix("phase:")):
            override = self.phase_overrides.get(phase)
            llm = override.llm if override else None
        if llm is None:
            return None
        exchange = Exchange(
            prompt=data["prompt"],
            reply=llm.load_reply(decode(data["reply"])),
            outcomes=[CallOutcome(text=t, is_error=e) for t, e in data["outcomes"]],
        )
        return llm, exchange

    # ---- 起動の入り口 ----
    def _begin_run(self, run_input: RunInput, run_id: str) -> None:
        """
        起動ごとに、前回の実行の痕跡を捨てる（同一セッション内で同じAgentが
        複数回呼ばれることがある）。

        tasksを捨てるのは必須。status=nextのまま残ると_select_invokablesが
        拾い、前回の依頼のためのtool実行が起きる。goalsとshared_memoryは残す。
        継承する思考（carry_thoughts）も残す。

        run_idは起動の表が発行したもの。親が子の起動を表で指すのにも使うので、
        ここで作らずに受け取る。
        """
        self.current_input = run_input
        self.tool_history = []
        # 前回の依頼にもとづく無効化を持ち越さない。
        # 無効化は依頼内容から導かれる判断であって、恒久的な設定ではない。
        self._restore_disabled()
        # 依頼が変われば、同じ呼び出しでも正当な実行になりうる。
        self.last_successful_calls = set()
        self.private_memory.tasks = []
        self._last_fingerprint = None
        self._stalled_count = 0
        # 前回の依頼で反映できなかった行を、次の依頼のプロンプトへ持ち込まない。
        self._unapplied_rows = []
        # 前回の周回数を持ち越さない。INITIAL_MEMORYはループへ入る前に
        # 走るため、ここで1に戻しておく必要がある。
        self._step_no = 1
        self.current_run_id = run_id
        # 前回の依頼で取得した添付を持ち越さない。
        self._carried_blobs = []
        # 前の起動が実行の手前で終わった場合、結果の無い呼び出し要求が残る。
        # 送り返せない（プロバイダが断る）ので、ここで捨てる。
        self._awaiting_outcomes = None
        self._run_generations = 0

    def respond(
        self,
        *,
        message: str,
        history: list[dict] | None = None,
        blobs: list[dict] | None = None,
    ) -> AgentResponse:
        """
        人間からの入り口。原文・チャット履歴・添付を受け取る。

        原文と履歴はshared_memory.conversationへ記録し、窓口自身も委譲先も
        そこから読む（窓口へ【依頼内容】やmessagesとして別に渡すと二重になる）。
        委譲先にとっての要求の解釈の基準はrequests（委譲された時点で構造化された
        依頼）のままで、conversationは解釈を補う材料として読ませる。

        単体で動かす時の入口。Networkに入れた場合はNetwork.respond()から
        起動の表が進めるので、ここは通らない。単体では中断できない
        （保存して再開する入口がNetworkにしか無い）。
        """
        runtime = Runtime()
        runtime.begin_respond(
            self, message=message, history=list(history or []), blobs=list(blobs or [])
        )
        outcome = runtime.drive()
        if isinstance(outcome, Suspended):
            # 型の不正ではなく使い方の誤り（単体では再開できない）。
            raise RuntimeError(_SUSPEND_OUTSIDE_NETWORK)  # noqa: TRY004
        return outcome

    def _start_respond(
        self, run_id: str, *, message: str, history: list[dict], blobs: list[dict]
    ) -> _Batch | _Finished:
        """起動の表が、respondの起動を始める時に呼ぶ。"""
        turns = list(history)
        self._begin_run(
            RunInput(
                message=message,
                kwargs={"message": message},
                history=turns,
                blobs=list(blobs),
                from_user=True,
            ),
            run_id,
        )
        self._record_conversation(message, turns)
        self.interceptor.notify.respond_start(
            RespondStartEvent(
                agent=self.name, message=f"{self.name}が応答を開始しました。"
            )
        )
        return self._enter_loop()

    def _finish_respond(
        self, text: str, steps: int, tokens_before: tuple[int, int]
    ) -> AgentResponse:
        """
        起動の表が、respondの起動の回答が出た時に呼ぶ。

        トークンは累計を保持したまま、今回分だけを差分で返す。
        累計はNetwork.total_tokens()がセッション全体の集計に使う。
        """
        self.interceptor.notify.respond_end(
            RespondEndEvent(
                agent=self.name, message=f"{self.name}が応答を完了しました。"
            )
        )
        before_in, before_out = tokens_before
        return AgentResponse(
            text=text,
            steps=steps,
            input_tokens=self.total_input_tokens - before_in,
            output_tokens=self.total_output_tokens - before_out,
        )

    def _record_conversation(self, message: str, history: list[dict]) -> None:
        """
        利用者の原文と会話履歴をshared_memory.conversationへ記録する。

        追記ではなく置き換える。respondごとに履歴は全体が渡ってくるため、
        追記すると同じやり取りが重なる。conversationはシステム所有で、
        LLMの差分では書き換えられない。

        idは古い順の連番（turn-1, turn-2, ...）で、最後が今回の発言。
        今回の発言だけ番号が無いと、時系列がidから読めない。

        末尾のuser-onlyは、利用者の発言だけを抜き出した一覧。依頼の中身は
        ここを軸に読ませる（_direct_request_stance）。会話をそのまま読ませると、
        「言い直された解釈に異議が無ければ同意した」と読む会話の規範が働き、
        過去の回答の解釈が、利用者の訂正をすり抜けて依頼の中身へ入り込む。
        話者のラベルを明確にするだけでは、この読み方は変わらない。
        """
        speaker = {"user": "利用者", "assistant": "あなた（過去の回答）"}

        def label(turn: dict) -> str:
            role = str(turn.get("role"))
            return speaker.get(role, role)

        entries = [
            MemoryEntry(id=f"turn-{i}", text=f"{label(turn)}: {turn.get('content')}")
            for i, turn in enumerate(history, start=1)
        ]
        entries.append(
            MemoryEntry(
                id=f"turn-{len(history) + 1}", text=f"利用者（今回の発言）: {message}"
            )
        )
        said = [turn.get("content") for turn in history if turn.get("role") == "user"]
        said.append(message)
        entries.append(
            MemoryEntry(
                id="user-only",
                text="利用者の発言だけ（古い順、最後が今回）: "
                + " → ".join(f"「{s}」" for s in said),
            )
        )
        self.shared_memory.conversation = entries

    def _default_describe_execution(self, kwargs: dict) -> str:
        """
        execution_messageも describe_execution も無い場合の既定文言。

        Toolと違い引数の中身は載せない。委譲の引数は依頼文そのもの
        （長文になりうる）で、toolの引数のように短い識別子とは限らないため。
        """
        if self.execution_message:
            return format_message(self.execution_message, kwargs)
        return f"{self.name}へ委譲しました。"

    @staticmethod
    def _classify_failure(llm: BaseLLM, error: Exception) -> FailureKind:
        """
        失敗の種類を接続に尋ねる。自作のLLMが上書きした分類が壊れていても、
        再試行の判定までは止めない（分からない失敗として扱う）。
        """
        try:
            return llm.classify_error(error)
        except Exception:  # noqa: BLE001
            return "other"

    def _notify_error(self, message: str) -> None:
        """errorイベントを流す。異常を見つけたのは常に自分なので、呼ぶ側は文言だけ渡す。"""
        self.interceptor.notify.error(ErrorEvent(source=self.name, message=message))

    @with_timing
    def execute(
        self, *, kwargs: dict, interceptor: Interceptor | None = None
    ) -> ToolResult:
        """
        他のエージェントからの入り口。Tool.execute()と同じ形で呼べる。

        チャット履歴はmessagesとしては受け取らない。原文と経緯は、窓口の
        respond()が記録したshared_memory.conversationを読む。

        単体で動かす時の入口（監査役など）。Networkの中の委譲はここを通らず、
        起動の表が_open_execute() → _start_execute() の順に進める。
        """
        try:
            blocked = self._open_execute(kwargs, interceptor)
        except Suspend:
            return ToolResult(error=_SUSPEND_OUTSIDE_NETWORK)
        if blocked is not None:
            return blocked

        # ここから先は自分の状態を書き換えながら進むため1件ずつに直列化する。
        # 門より後で取るのは、before_executeが人の承認を待つ構成でも
        # 他の呼び出し元を待たせないため。
        with self._lock:
            runtime = Runtime()
            runtime.begin_execute(self, kwargs)
            outcome = runtime.drive()
        if isinstance(outcome, Suspended):
            return self._fail_execute(RuntimeError(_SUSPEND_OUTSIDE_NETWORK))
        return outcome

    def _open_execute(
        self, kwargs: dict, interceptor: Interceptor | None
    ) -> ToolResult | None:
        """
        委譲を受けてよいかの判定。断るならその結果を、受けるならNoneを返す。

        before_executeのコールバックが中断（suspend）した場合は、そのまま外へ出る。
        """
        if self.disabled:
            # Tool.executeと同じく、呼ばれなかった理由を観測側へ伝える。
            if interceptor:
                interceptor.notify.execute_blocked(
                    ExecuteBlockedEvent(
                        name=self.name,
                        kwargs=kwargs,
                        reason="",
                        message=f"{self.name}は無効化されています。",
                    )
                )
            return ToolResult(error="このエージェントは現在無効化されています。")

        if interceptor:
            verdict = interceptor.check.before_execute(
                BeforeExecuteEvent(name=self.name, kwargs=kwargs)
            )
            if not verdict.ok:
                # Tool.executeと同じ扱い。理由があれば依頼元へそのまま返す。
                detail = f" 理由: {verdict.reason}" if verdict.reason else ""
                interceptor.notify.execute_blocked(
                    ExecuteBlockedEvent(
                        name=self.name,
                        kwargs=kwargs,
                        reason=verdict.reason,
                        message=f"{self.name}への委譲がブロックされました。{detail}",
                    )
                )
                return ToolResult(error=verdict.reason or "委譲がブロックされました。")
        return None

    def _start_execute(self, run_id: str, kwargs: dict) -> _Batch | _Finished:
        """起動の表が、委譲された起動を始める時に呼ぶ（判定は通った後）。"""
        # 依頼本文。input_schemaが既定（messageのみ）ならその中身を、
        # 構造化スキーマならJSONのまま見せる。
        if set(kwargs) == {"message"}:
            message = str(kwargs["message"])
        else:
            # 直列化できない値はdefault=strで文字列化する。ここで例外を出すと
            # Invokableの「例外を外へ出さない」契約が破れる。
            message = json.dumps(kwargs, ensure_ascii=False, indent=2, default=str)

        self._begin_run(RunInput(message=message, kwargs=dict(kwargs)), run_id)

        # 最初に呼ばれたエージェントの依頼だけがセッション全体の方向を定義する
        # （以降の委譲では上書きしない）。入るのは構造化された引数だけ。
        if self.shared_memory.requests is None:
            self.shared_memory.requests = MemoryEntry(id="request_1", text=message)

        # toolと同じイベントで通知する。文言の組み立ては失敗しうるため、
        # 通知の失敗で委譲を止めないよう保護する（Tool.executeと同じ）。
        try:
            describe = self.describe_execution or self._default_describe_execution
            self.interceptor.notify.execute_start(
                ExecuteStartEvent(name=self.name, kwargs=kwargs, message=describe(kwargs))
            )
        except Exception as e:  # noqa: BLE001
            self._notify_error(f"{self.name}の実行中メッセージの生成に失敗しました: {e}")

        return self._enter_loop()

    def _finish_execute(self, text: str) -> ToolResult:
        """起動の表が、委譲された起動の回答が出た時に呼ぶ。"""
        # agent_answersはシステム所有。LLMの差分では書けないため直接記録する。
        self.shared_memory.agent_answers.append(
            MemoryEntry(
                id=f"{self.name}-answer-{len(self.shared_memory.agent_answers) + 1}",
                text=text,
            )
        )
        self.interceptor.notify.execute_end(
            ExecuteEndEvent(
                name=self.name, success=True, message=f"{self.name}の委譲が完了しました。"
            )
        )
        return ToolResult(value=text)

    def _fail_execute(self, error: Exception) -> ToolResult:
        """
        委譲された起動が例外で終わった時。Invokableの契約として例外を外へ出さない。
        ただし握りつぶすと原因が追えなくなるため、観測者へは通知する。
        """
        self._notify_error(f"{self.name}の実行が失敗しました: {error}")
        self.interceptor.notify.execute_end(
            ExecuteEndEvent(
                name=self.name,
                success=False,
                message=f"{self.name}の委譲がエラーで終了しました。",
            )
        )
        return ToolResult(error=f"{self.name}の実行中にエラーが発生しました: {error}")

    # ---- ReActループ ----
    # ループは「実行 → 評価 → 次を決める」の繰り返し。AgentとReflexAgentの違いは
    # hookの中身だけなので、ここに分岐は無い。
    #
    # tool / agent を呼ぶ所でだけ、計画（_Batch）を返して手を離す。結果が揃うと
    # 起動の表が _continue_after_batch() を呼び、そこから続ける。
    #
    # 抜ける条件は5つ。どの終わり方でも必ず回答を返す（_Finished）。
    #
    #     ・実行すべき対象が無くなった      → ANSWER
    #     ・同じ実行を繰り返して進まない    → STALLED_ANSWER
    #     ・max_stepsに達した               → CUTOFF_ANSWER
    #     ・ReflexAgentがテキストで答えた   → その場で返す
    #     ・呼び出しを強制したのにテキストが返った → ANSWER（やり直し）
    def _enter_loop(self) -> _Batch | _Finished:
        """ループに入る。初期toolがあれば、先にその実行を頼む。"""
        plan = self._plan_initial_tools()
        if plan:
            return _Batch(stage="initial", plan=plan)
        return self._after_initial([])

    def _after_initial(self, entries: list[ToolHistoryEntry]) -> _Batch | _Finished:
        # 記憶を持つAgentなら、初期ツールの結果も材料にして最初の記憶を作る。
        # ReflexAgentでは添付を抱えるだけ。
        self._after_initial_tools(entries)
        return self._loop(1)

    def _continue_after_batch(
        self,
        stage: Stage,
        plan: list[_PlannedCall],
        results: dict[int, ToolResult | None],
        children: dict[int, str],
    ) -> _Batch | _Finished:
        """
        起動の表が、バッチの結果が揃った時に呼ぶ。結果を反映して、続きを進める。

        children: 委譲先の起動のrun_id（枠の番号ごと）。executedイベントに載せる。
        """
        entries = self._reflect(plan, results, children)
        if stage == "initial":
            return self._after_initial(entries)

        # 結果を評価する生成より前に、呼び出し要求と結果を対にして残す。
        self._close_exchange(entries)

        # 結果を評価して記憶へ書く。同時に「進んでいるか」も判定する。
        # 進んでいないと判断されたらTrueが返り、打ち切る。
        if self._process_step(entries):
            return _Finished(
                self._final_answer(
                    Phase.STALLED_ANSWER, notice=self._pending_task_notice()
                ),
                self._step_no,
            )
        return self._loop(self._step_no + 1)

    def _loop(self, first_step: int) -> _Batch | _Finished:
        """first_step周目から、次に手を離すところ（呼び出しか回答）まで回す。"""
        for step in range(first_step, self.max_steps + 1):
            # この周のイベントに載る番号。_generate / _update_memory /
            # _reflect が self._step_no から読む。
            self._step_no = step

            # 記憶を持つAgentはtasksの対象だけを提示して呼び出しを強制し、
            # ReflexAgentは全ツールを提示して呼ぶかどうかも委ねる。
            targets, tool_choice = self._select_invokables()

            # 名前が1つも解決できなかった場合。「実行すべきものが無い」のではなく
            # タスクの記述が誤っているので、失敗として記録しmemory更新で直させる
            # （素通りさせると未実行のタスクを残したまま回答へ飛ぶ）。
            if not targets and self._unresolved_targets:
                entries = [
                    ToolHistoryEntry(
                        name=name, kwargs={}, result=ToolResult(error=reason)
                    )
                    for name, reason in self._unresolved_targets
                ]
                self.tool_history.extend(entries)
                if self._process_step(entries):
                    return _Finished(
                        self._final_answer(
                            Phase.STALLED_ANSWER, notice=self._pending_task_notice()
                        ),
                        step,
                    )
                continue

            # 実行すべきものが無い ＝ 追加実行なしで答えられる状態。
            # toolsを渡さないのではなく、そもそも呼び出しフェーズへ入らない。
            if not targets:
                return _Finished(
                    self._final_answer(Phase.ANSWER, notice=self._pending_task_notice()),
                    step,
                )

            # 呼び出しフェーズ。ここでLLMに決めさせるのは「引数」だけで、
            # 何を呼ぶかは既にtargetsで絞られている。
            response = self._generate(
                Phase.FUNCTION_CALL, tools=targets, tool_choice=tool_choice
            )

            # --- 呼び出し要求が返ってこなかった場合の3つの分岐 ---
            if not response.has_calls:
                if not response.text:
                    # テキストも呼び出し要求も無い異常な応答。
                    # ステップを1つ消費して、次の周で再試行する。
                    continue

                if self._answers_in_loop:
                    # ReflexAgentはテキストで答えてきた時点で終了する
                    # （「調べる必要はなかった」という判断の表明）。output_schemaが
                    # あっても形式はsystem_instructionで伝えてあるので作り直さない。
                    return _Finished(response.text, step)

                # 呼び出しを強制した（"any"）のに答えが返ってきた場合。この
                # テキストには回答フェーズの指示も出力形式の強制も効いていないため、
                # 改めて回答フェーズで答えさせる。
                return _Finished(
                    self._final_answer(Phase.ANSWER, notice=self._pending_task_notice()),
                    step,
                )

            # --- 実際に呼ぶ ---
            # 重複の拒否と名前の解決はここで決め、実行（無効化の確認・実行前判定を
            # 含む）は起動の表に任せる。結果は_continue_after_batchで受け取る。
            return _Batch(stage="loop", plan=self._plan_calls(response.function_calls))

        # max_stepsを使い切った。ここまでに分かったことで回答する。
        return _Finished(
            self._final_answer(Phase.CUTOFF_ANSWER, notice=self._pending_task_notice()),
            self.max_steps,
        )

    @staticmethod
    def _step_fingerprint(entries: list[ToolHistoryEntry]) -> frozenset:
        """
        そのステップで何が起きたかの指紋。名前・引数・エラー内容の集合。

        前ステップと一致するなら新しい情報が入っていない（成功した呼び出しは
        重複拒否で弾かれるため、一致するのは失敗か拒否の繰り返しの時だけ）。
        """
        return frozenset(
            (
                e.name,
                json.dumps(e.kwargs, sort_keys=True, ensure_ascii=False, default=str),
                e.result.error,
            )
            for e in entries
        )

    def _process_step(self, entries: list[ToolHistoryEntry]) -> bool:
        """
        1ステップ分の結果を処理し、停滞で打ち切るべきならTrueを返す。

        同じ指紋が続いた場合、まずmemory更新へ立て直しを促し、それでも
        変わらなければ打ち切る（進捗ゼロのまま推論を払い続けない）。
        """
        fingerprint = self._step_fingerprint(entries)
        if fingerprint and fingerprint == self._last_fingerprint:
            self._stalled_count += 1
        else:
            self._stalled_count = 0
        self._last_fingerprint = fingerprint

        if self._stalled_count >= self.max_stalled_steps:
            self._notify_error(
                f"{self.name}が同じ実行を{self._stalled_count + 1}回繰り返し、"
                f"進捗しなくなったため打ち切ります。"
            )
            return True

        # 1度目の繰り返しでは、状況を伝えたうえでmemory更新をやり直させる。
        self._after_tools(entries, notice=STALL_NOTICE if self._stalled_count else "")
        return False

    def _pending_task_notice(self) -> str:
        """
        実行されないまま残ったタスクを、最終回答の生成へ知らせる文言。

        そのまま回答させると、実行していないことを実行したかのように書く。
        止めずに知らせるだけなのは、残っているのが正常な場合もあるため
        （誤りかどうかは記憶を読まないと決まらない）。
        """
        unfinished = (TaskStatus.NEXT, TaskStatus.NEXT_PARALLEL, TaskStatus.CONDITIONAL)
        pending = [t for t in self.private_memory.tasks if t.status in unfinished]
        if not pending:
            return ""

        listing = "\n".join(f"- [{t.id}] ({t.status.value}) {t.text}" for t in pending)
        return section(
            "実行されていないタスク",
            f"""次のタスクは完了しておらず、実行もされていない。
{listing}

これらが実行済みであるかのように回答してはならない。実行すべきだったものが実行できていない場合は、その事実と、何が足りなかったのかを回答に明示する。""",
        )

    def _final_answer(self, phase: Phase, notice: str = "") -> str:
        """
        toolを渡さずに最終回答を生成する。

        output_schemaはresponse_schemaとして渡すが、強制しきれないプロバイダ
        があるため、生成後に解釈できるかを確かめて1度だけ作り直す。

        notice: 回答の前に伝えておく必要がある事情（未実行のタスクなど）。
        """
        spec = PHASE_SPECS[phase]
        schema = self.output_schema if spec.respects_output_schema else None
        # 答え方の方針は、事情（notice）より前に置く。事情は今回だけのもの。
        # 作り直しの時もnoticeごと渡るので、方針も落ちない。
        notice = join_sections(
            section("この回答のためのナレッジ", self.answer_knowledge), notice
        )

        response = self._generate(phase, response_schema=schema, extra_prompt=notice)

        if schema and response.text and not self._matches_output_schema(response.text):
            self._notify_error(
                f"{self.name}の回答がoutput_schemaに従っていないため、作り直します。"
            )
            response = self._generate(
                phase,
                response_schema=schema,
                # noticeも一緒に渡す。作り直しで落とすと、
                # 2回目だけ未実行タスクを知らない状態で回答が作られる。
                extra_prompt=join_sections(
                    notice,
                    section(
                        "前回の応答の問題",
                        """指定された形式のJSONとして解釈できなかった。説明文やコードブロックの記号を付けず、JSONのみを出力すること。""",
                    ),
                ),
            )

        if response.text:
            return response.text
        return "【システム通知】回答の生成に失敗しました。memoryに残っている調査結果を確認してください。"

    def _matches_output_schema(self, text: str) -> bool:
        """
        回答がoutput_schemaの形として解釈できるか。

        全項目は検証しない。防ぎたいのは「JSONですらない」「オブジェクトを
        求めたのに配列」のような、パースした時点で落ちる不一致だけ。
        """
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return False

        expected = (self.output_schema or {}).get("type")
        if expected == "object":
            return isinstance(parsed, dict)
        if expected == "array":
            return isinstance(parsed, list)
        return True

    # ---- ここから下がhook。ReflexAgentが上書きする ----
    @staticmethod
    def _collect_blobs(entries: list[ToolHistoryEntry]) -> list[dict]:
        """
        今回実行したtoolが返した添付を、呼び出し順のまま集める。

        評価するphaseへ1回だけ渡すもので、起動の間ずっと有効な
        current_input.blobs（利用者の添付）とは混ぜない。
        """
        return [b for e in entries for b in e.result.blobs]

    def _after_initial_tools(self, entries: list[ToolHistoryEntry]) -> None:
        """
        ループ開始前。初期toolの結果も材料にして初期memoryを作る
        （初期toolが無ければentriesは空）。

        結果と添付は履歴経由ではなく明示的に渡す（_show_tool_results=Falseで
        履歴からは隠れるが、このステップはその中身を材料にする）。
        """
        tool_results = "\n".join(e.render(include_result=True) for e in entries)
        self._update_memory(
            phase=Phase.INITIAL_MEMORY,
            tool_results=tool_results,
            tool_blobs=self._collect_blobs(entries) or None,
            knowledge=self.initial_knowledge,
        )

    def _after_tools(
        self, entries: list[ToolHistoryEntry], *, notice: str = ""
    ) -> None:
        """
        実行直後。今回の結果を評価してmemoryへ反映する。

        notice: 停滞を検出した時に差し込む注意文。memory更新はtasksのstatusを
            変える唯一の場所なので、立て直しはここから促す。
        """
        tool_results = "\n".join(e.render(include_result=True) for e in entries)
        self._update_memory(
            phase=Phase.MEMORY_UPDATE,
            tool_results=tool_results,
            # toolが返した添付は、それを評価するこの生成にだけ渡す
            # （記憶へ文字起こしされた後は以降のプロンプトへ載らない）。
            tool_blobs=self._collect_blobs(entries),
            notice=notice,
            executed=entries,
        )

    def _select_invokables(self) -> tuple[list[Invokable], str | None]:
        """
        このステップで提示するtool/agentと、tool_choiceを決める。

        next / next_parallel のものだけを提示し、"any"で強制する（選ばせない）。
        提示するものが無ければ空リストを返し、ループは最終回答へ向かう。
        解決できなかった名前は_unresolved_targetsへ記録する（黙って除外すると、
        nextのまま残っているのに実行対象が空になる）。
        """
        targets = []
        seen = set()
        self._unresolved_targets = []
        for task in self.private_memory.actionable_tasks():
            for name in task.target_names:
                if name in seen:
                    continue
                seen.add(name)
                found = self._resolve_invokable(name)
                if found is None:
                    self._unresolved_targets.append(
                        (name, "存在しないtool/agentです。")
                    )
                elif self._is_disabled(found):
                    self._unresolved_targets.append((name, "現在無効化されています。"))
                else:
                    targets.append(found)
        return targets, "any"

    # ---- 生成呼び出し ----
    def _generate(
        self,
        phase: Phase,
        *,
        tools: list | None = None,
        tool_choice: str | None = None,
        tool_results: str = "",
        tool_blobs: list[dict] | None = None,
        extra_prompt: str = "",
        response_schema: Any | None = None,
    ) -> LLMResponse:
        """
        1回の生成。phaseに応じた設定・プロンプト・添付をまとめて組み立てる。

        ここを通した呼び出しだけがトークンを積算するので、
        使用量の集計漏れが起きない。
        """
        spec = PHASE_SPECS[phase]
        cfg = self.resolve_generation(phase)

        # 会話履歴はmessagesとして渡さない。shared_memory.conversationに載せ、
        # 全員が同じ形で読む（窓口だけがマルチターンで受け取ると、委譲先と
        # 見えている経緯の形が食い違い、窓口には二重に載る）。
        # プロンプトの組み立ては再試行のたびに同じ結果になるので1回だけ行う。
        prompt = self._build_prompt(
            tool_results=tool_results, extra=extra_prompt, phase=phase
        )
        system_instruction = self._build_system_instruction(phase)

        # 実際に使うモデル。再試行で差し替わりうるので、cfgとは別に持つ。
        model = cfg.model

        # 継承しないなら引数自体を渡さない（exchangesを知らない自作のLLMも動く）。
        # 送るのは同じ接続が作った分だけ。別のプロバイダの出力は読めない。
        carried = {}
        if self.carry_thoughts:
            carried["exchanges"] = [ex for llm, ex in self._exchanges if llm is cfg.llm]

        attempt = 0
        while True:
            attempt += 1

            # 試行ごとに尋ねる（1回目だけだと再試行で予算を超えても止まらない）。
            # 計測より前に置くのは、判定が待つ場合にその時間を秒数へ混ぜないため。
            gate = self.interceptor.check.before_generate(
                BeforeGenerateEvent(
                    agent=self.name,
                    phase=phase,
                    model=model,
                    attempt=attempt,
                    step_no=self._step_no,
                )
            )
            if not gate.ok:
                raise GenerationAborted(
                    f"{self.name}の{phase.value}の生成が中止されました。"
                )
            # モデル名が返った場合はそれに差し替える。走行中の状態を見て
            # 軽いモデルへ落とす用途（予算が残り少ない等）のため。
            if gate.model:
                model = gate.model

            started = time.perf_counter()
            try:
                response = cfg.llm.generate(
                    model=model,
                    prompt=prompt,
                    system_instruction=system_instruction,
                    blobs=self._blobs_for(spec) + list(tool_blobs or []),
                    tools=tools,
                    tool_choice=tool_choice,
                    response_schema=response_schema,
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                    thought_level=cfg.thought_level,
                    **carried,
                )
            except Exception as e:
                # 失敗した試行も計測として流す（落とすと再試行に費やした時間が
                # 消える）。判定より先に流すのは、待ち時間を秒数へ混ぜないため。
                self.interceptor.notify.generated(
                    GeneratedEvent(
                        agent=self.name,
                        phase=phase,
                        model=model,
                        elapsed=time.perf_counter() - started,
                        input_tokens=0,
                        output_tokens=0,
                        attempt=attempt,
                        step_no=self._step_no,
                        error=str(e),
                        run_id=self.current_run_id,
                    )
                )

                # 再試行するかはフレームワークが決めない（どれだけ待つか・何回
                # 試すかは運用の方針で、対話UIとバッチで正解が違う）。
                # 登録が無ければ再試行せず、例外をそのまま外へ出す。
                decision = self.interceptor.check.generation_failed(
                    GenerationFailedEvent(
                        agent=self.name,
                        phase=phase,
                        model=model,
                        attempt=attempt,
                        error=e,
                        kind=self._classify_failure(cfg.llm, e),
                        message=f"{type(e).__name__}: {e}",
                    )
                )
                if not decision.ok:
                    raise
                # モデル名が返った場合はそれに差し替える（枠を使い切ったモデルは
                # 待っても同じエラーが返る）。
                if decision.model:
                    model = decision.model
                continue
            break

        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        self.total_cached_tokens += response.cached_tokens

        self.interceptor.notify.generated(
            GeneratedEvent(
                agent=self.name,
                phase=phase,
                model=model,
                elapsed=time.perf_counter() - started,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_tokens=response.cached_tokens,
                cache_write_tokens=response.cache_write_tokens,
                attempt=attempt,
                step_no=self._step_no,
                run_id=self.current_run_id,
                stop_reason=response.stop_reason,
                thoughts=response.thoughts,
            )
        )
        if self.carry_thoughts:
            self._keep_exchange(phase, cfg.llm, response)
        self._run_generations += 1
        return response

    def _keep_exchange(self, phase: Phase, llm: BaseLLM, response: LLMResponse) -> None:
        """
        この生成を、次の生成へ送り返すやり取りとして残す（carry_thoughts）。

        発話は縮めて残す。memoryの全体・実行履歴・toolの生の戻り値は、毎回の
        発話が最新のものを載せるので、過去の発話には残さない。思考はそれを
        読み取った結果を運ぶもので、読み取った元まで積み直す必要は無い。

        依頼内容だけは、委譲された起動の最初の生成に残す。次の起動では今回の
        発話に載らなくなり、前の起動の思考が何の依頼についてのものか分からなく
        なるため（利用者からの依頼は、shared_memory.conversationに残り続ける）。
        """
        if response.reply is None:
            return
        run = self.current_input or RunInput(message="")
        first = self._run_generations == 0
        prompt = join_sections(
            section("依頼内容", run.message if first and not run.from_user else ""),
            f"[{phase.value}]",
        )
        exchange = Exchange(prompt=prompt, reply=response.reply)
        if response.has_calls:
            self._awaiting_outcomes = (llm, exchange)
        else:
            self._exchanges.append((llm, exchange))

    def _close_exchange(self, entries: list[ToolHistoryEntry]) -> None:
        """
        呼び出し要求を返した生成に、呼び出し順の結果を対にして残す。

        _reflectは要求1件につき1件の記録を、要求の順に返す。
        """
        if self._awaiting_outcomes is None:
            return
        llm, exchange = self._awaiting_outcomes
        self._awaiting_outcomes = None
        exchange.outcomes = [e.outcome() for e in entries]
        self._exchanges.append((llm, exchange))

    # ---- memoryの更新 ----
    def _writable_memories(self) -> tuple:
        """
        差分の書き込み先。sharedとprivateの両方。

        ReflexAgentも_write_tool_result_to_memory経由でここを通るため、
        あちらはsharedのみへ絞るようオーバーライドしている。
        """
        return (self.shared_memory, self.private_memory)

    def _executed_task_ids(self, executed: list[ToolHistoryEntry]) -> set[str]:
        """
        今回の実行に対応するタスクのid。記憶を更新する前に控える。

        更新の後に「成功した対象を指すnextのタスク」を探すと、結果を見て新しく
        立てたタスク（同じtoolを別の引数でもう一度、など）まで放置と見なして
        差し戻してしまう。toolが1つしか無いエージェントは、結果を見て掘り直す
        ことができなくなる。失敗した実行は対象にしない（再試行が正しい）。

        今回のtoolの結果が書いたタスクも対象にしない。記憶の更新より前に書かれる
        ので、toolが「同じtoolをもう一度」と立てたタスクが、放置に見えてしまう。
        """
        succeeded = {e.name for e in executed if e.result.success}
        if not succeeded:
            return set()

        written = {task_id for e in executed for task_id in e.written_task_ids}
        pending = (TaskStatus.NEXT, TaskStatus.NEXT_PARALLEL)
        return {
            task.id
            for task in self.private_memory.tasks
            if task.status in pending
            and succeeded & set(task.target_names)
            and task.id not in written
        }

    def _stale_task_errors(self, executed_ids: set[str]) -> list[DiffError]:
        """
        実行済みのタスクがstatusに反映されているかを検証する。

        nextのまま残ると次のステップで同じ対象が選ばれ、重複拒否で弾かれる
        ぶんmax_stepsを使い切るまで無駄が続く。記憶の書き方の誤りとして
        DiffErrorで差し戻す。

        見るのは実行前から控えていたidだけ。このステップで新しく作った
        タスクは、同じ対象を指していても通す（全く同じ呼び出しの繰り返しは
        重複拒否が止める）。
        """
        pending = (TaskStatus.NEXT, TaskStatus.NEXT_PARALLEL)
        return [
            DiffError(
                "tasks",
                {"field": "tasks", "id": task.id, "status": task.status.value},
                f"このタスクの対象（{'、'.join(task.target_names)}）は今回呼び出されたが、"
                f"statusが{task.status.value}のまま残っている。"
                "今回の呼び出しの引数と結果を見て、このタスクの目的が満たされたかを確かめ、"
                "statusを更新すること。満たされたならdone。"
                "今回の呼び出しに含まれていなかった内容や、結果が目的に届かなかった内容は、"
                "依頼の中身を変えた新しいidのタスクとして作り、このタスクはunnecessaryにする。",
            )
            for task in self.private_memory.tasks
            if task.id in executed_ids and task.status in pending
        ]

    def _review_diff(
        self,
        *,
        phase: Phase,
        attempt: int,
        review_round: int,
        rows: list,
        tool_results: str,
        rejections: list[str],
    ) -> MemoryDiffDecision:
        """
        差分を当てる前に監査（before_memory_diff）へ見せる。

        材料はこの時点の写しで揃えて渡す。監査が後から読みに行くと、並列に
        動く他のエージェントが共有の記憶を書き換えた後の中身を見てしまう。
        """
        conversation = "\n".join(
            f"[{e.id}] {e.text}" for e in self.shared_memory.conversation
        )
        return self.interceptor.check.before_memory_diff(
            BeforeMemoryDiffEvent(
                agent=self.name,
                phase=phase,
                attempt=attempt,
                review_round=review_round,
                step_no=self._step_no,
                run_id=self.current_run_id,
                conversation=conversation,
                request=self.current_input.message if self.current_input else "",
                shared_memory=self.shared_memory.render(),
                private_memory=self.private_memory.render(),
                # 監査が書き換えても、当てる差分は変わらないようにする。
                rows=copy.deepcopy(rows),
                tool_results=tool_results,
                rejections=list(rejections),
            )
        )

    def _update_memory(
        self,
        *,
        phase: Phase,
        tool_results: str = "",
        tool_blobs: list[dict] | None = None,
        notice: str = "",
        knowledge: str = "",
        executed: list[ToolHistoryEntry] | None = None,
    ) -> list[DiffError]:
        """
        LLMに記憶の差分（JSON配列）を作らせて反映する。初期構築と更新の両方が通る。

        失敗した行があればその行だけを差し戻して再生成させる（全体をやり直すと
        既に正しく書けた項目まで作り直させる）。リトライを使い切っても中断せず、
        通った分だけを残して進む（欠けた事実はerrorイベントで通知される）。
        """
        memories = self._writable_memories()
        spec = PHASE_SPECS[phase]
        schema = build_diff_schema(*memories, allow_disable=spec.allows_disable)

        # スキーマが形式を保証していても、粒度は例で示した方が安定する。
        # 「fieldに書き込み先を指定する」という構造も例で伝わる。
        base_extra = join_sections(
            section("停滞の検出", notice),
            # 前回の更新で消えた行。ここで出し切って空にする（同じ書き方を
            # 繰り返さなければ次は出ない）。
            section("前回、反映されなかった項目", self._take_unapplied_notice()),
            # この構築だけで使うナレッジ（判断の例など）。出力例より前に置くのは、
            # 「何をどう判断するか」を読んでから「どの形で書くか」を読む方が
            # 噛み合うため。
            section("この構築のためのナレッジ", knowledge),
            section("出力例", DIFF_EXAMPLE),
        )
        extra = base_extra

        errors: list[DiffError] = []
        # 試行をまたいで積む。1回目で消えた行は2回目の差し戻しには乗らない
        # （直せないため）ので、ここで持っておかないと最後の試行の分しか残らない。
        ignored_all: list[DiffError] = []
        # ループ内で必ず代入されるが（max(1, ...)により最低1回は回る）、
        # 型チェッカーはそこまで推論しないため明示的に初期化する。
        rows: list | None = None
        # 差分の適用は試行ごとに繰り返されるので、実行したタスクは試行に入る前に
        # 控える（1回目の適用の後に控えると、書き換えた後のtasksを見てしまう）。
        executed_ids = self._executed_task_ids(executed) if executed else set()
        # 0以下だとループが一度も回らず、memoryを作らないまま「更新しました」と
        # 通知してしまう。最低1回は必ず生成する。
        max_attempts = max(1, self.max_memory_retries)
        attempt = 0
        # 監査（before_memory_diff）に見せた回数。監査の差し戻しはattemptを
        # 消費しない。何回で通すかは監査する側が決める（上限を持つのは運用の方針）。
        review_round = 0
        # 監査が差し戻した理由。作り直した差分を監査へ見せる時に添える。
        # 通ったら空に戻す（通った後の差分は、前の指摘を受けて直したものではない）。
        rejections: list[str] = []
        # 形式の誤りで差し戻す時の節。監査で差し戻した後の作り直しにも載せる
        # （直すべき行が、まだ反映されないまま残っているため）。
        retry_section = ""
        # 監査で差し戻され、当てずに持っている差分。作り直しでは、ここへ直した行
        # だけを重ねる。全体を作り直させると、実行結果を読み直して、指摘と関係の
        # ない書き起こしまでやり直すことになる。
        held: list | None = None
        # 差分を1度でも当てたか。当てた後の形式の差し戻しは、失敗した行だけを
        # 出し直させるので、実行結果を読み直す必要がない。1行も当たっていない
        # （配列として読めなかった）間だけ、書き起こしからやり直させる。
        applied_once = False
        while attempt < max_attempts:
            # 作り直しには実行結果を渡さない。書き起こしは、持っている差分か、
            # 当てた記憶に済んでいる。
            reread = held is None and not applied_once
            response = self._generate(
                phase,
                tool_results=tool_results if reread else "",
                tool_blobs=tool_blobs if reread else None,
                extra_prompt=extra,
                response_schema=schema,
            )

            rows = _extract_rows(response.text)
            if rows is not None and held is not None:
                rows = _merge_rows(held, rows)
            if rows is not None:
                # 当てる前に監査へ見せる。当てた後では取り消せない（他の
                # エージェントが既に読んでいるかもしれない）。
                review_round += 1
                verdict = self._review_diff(
                    phase=phase,
                    attempt=attempt + 1,
                    review_round=review_round,
                    rows=rows,
                    tool_results=tool_results,
                    rejections=rejections,
                )
                if not verdict.ok:
                    reason = verdict.reason or "（理由は示されていない）"
                    rejections.append(reason)
                    # 当てなかった差分も、何を指摘されたかと一緒に流す。
                    # 「どう脱線し、指摘を受けてどう直したか」を後から追うため。
                    self.interceptor.notify.memory_diff(
                        MemoryDiffEvent(
                            agent=self.name,
                            phase=phase,
                            attempt=attempt + 1,
                            rows=list(rows),
                            errors=[DiffError("(監査)", "(差分全体)", reason)],
                            tool_results=tool_results,
                            step_no=self._step_no,
                            run_id=self.current_run_id,
                            review_round=review_round,
                        )
                    )
                    held = rows
                    extra = join_sections(
                        base_extra,
                        retry_section,
                        section(
                            "監査の指摘",
                            "次の指摘を受けて、下の差分はまだ記憶に反映されていない。\n"
                            "指摘を踏まえて、直す行だけを同じfieldとidで出すこと。"
                            "出さなかった行は、下の差分のまま反映される。"
                            "実行結果の書き起こしは下の差分に済んでいるので、"
                            "このステップでは実行結果は渡されない。\n\n"
                            f"【指摘】\n{reason}\n\n"
                            "【差し戻された差分】\n"
                            + json.dumps(rows, ensure_ascii=False, indent=1),
                        ),
                    )
                    continue
                held = None
                rejections.clear()

            attempt += 1
            if rows is None:
                errors = [
                    DiffError(
                        "(全体)", response.text[:200], "JSONの配列として解釈できません"
                    )
                ]
                ignored = []
                # 配列として読めなかった場合は適用そのものが起きていない。
                applied_seq = 0
            else:
                # 無効化の指示はmemoryへの書き込みではないので、先に取り出して処理する。
                if spec.allows_disable:
                    self._apply_disable_rows(rows)
                # LLM由来の差分はidが必須なので、idの採番は起きない。
                # apply_seq（適用順の番号）は通知へ載せるので受け取る。
                diff = apply_diff(rows, *memories, handled_disable=spec.allows_disable)
                applied_once = True
                errors = diff.errors
                ignored = diff.ignored
                applied_seq = diff.apply_seq
                # 書き込みが通ったうえで、実行済みのタスクが放置されていないかを見る。
                # apply_diffは1行ずつしか見ないので「書かれなかったこと」は拾えない。
                if executed_ids:
                    errors.extend(self._stale_task_errors(executed_ids))

            ignored_all.extend(ignored)

            # 適用の直後に、材料・出力・結果を揃えて流す。ループの内側なのは、
            # 再生成の「1回目に何を間違え、2回目にどう直したか」を残すため。
            self.interceptor.notify.memory_diff(
                MemoryDiffEvent(
                    agent=self.name,
                    phase=phase,
                    attempt=attempt,
                    # 観測側が書き換えても本体へ影響しないよう浅く複製する。
                    # 通知は観測のための仕組みであり、観測が対象を変えてよい理由がない。
                    rows=list(rows or []),
                    errors=list(errors),
                    # この試行で消えた分だけ。積み上げた全体はループの外で扱う。
                    ignored=list(ignored),
                    tool_results=tool_results,
                    raw_text="" if rows is not None else response.text,
                    step_no=self._step_no,
                    apply_seq=applied_seq,
                    run_id=self.current_run_id,
                    # 配列として読めなかった差分は監査に見せていない。
                    review_round=review_round if rows is not None else 0,
                )
            )

            if not errors:
                break

            # 失敗内容を添えて再生成させる。通った分は適用済みなので、やり直すのは
            # 失敗した行だけ。毎回ベースから作り直すのは、前回のextraへ積み増すと
            # 修正済みの古い失敗まで提示され続けるため。
            retry_section = section(
                "前回の更新で失敗した項目",
                "以下は反映されなかった。原因を確認し、修正した分のみを出力すること。\n"
                + "\n".join(e.render() for e in errors),
            )
            extra = join_sections(base_extra, retry_section)

        # リトライを使い切って残った分と、差し戻さずに消した分。どちらも
        # このステップでは記憶に入っていないので、次の更新で1回だけ提示する。
        self._unapplied_rows = [*errors, *ignored_all]

        if ignored_all:
            detail = "\n".join(e.render() for e in ignored_all)
            self._notify_error(
                f"{self.name}のmemory更新で{len(ignored_all)}件を反映しませんでした"
                f"（差し戻しても直らないため再生成はしていません）。\n{detail}"
            )

        if errors:
            # リトライを使い切っても中断しないため、通知しなければ
            # 記憶が欠けたまま進んだことに誰も気付けない。
            detail = "\n".join(e.render() for e in errors)
            self._notify_error(
                f"{self.name}のmemory更新で{len(errors)}件が反映されませんでした。\n{detail}"
            )
        else:
            # 何を更新したかを添える（「更新しました」だけでは記憶が育っているか
            # 判断できない）。ここは全行が適用された時なのでrowsがそのまま適用内容。
            applied = [
                f"{r.get('field')}:{r.get('id')}"
                for r in (rows or [])
                if isinstance(r, dict) and r.get("field") != DISABLE_FIELD
            ]
            detail = f"（{' / '.join(applied)}）" if applied else "（変更なし）"
            self.interceptor.notify.memory_updated(
                MemoryUpdatedEvent(
                    agent=self.name,
                    message=f"{self.name}のmemoryを更新しました。{detail}",
                )
            )
        return errors

    def _take_unapplied_notice(self) -> str:
        """
        前回の更新で記憶へ入らなかった行を伝える文言。読み出したら空にする。

        差し戻し（"前回の更新で失敗した項目"）とは別の経路になる。あちらは
        同じ生成の中でやり直させるもので、ここは「やり直しても直らなかった／
        直せない」ものを次のステップへ1回だけ運ぶ。
        記憶へ書かないのは、直れば消えるべき情報が記憶には残り続けるため。
        """
        rows, self._unapplied_rows = self._unapplied_rows, []
        if not rows:
            return ""
        return (
            "以下は記憶に入っていない。同じ書き方を繰り返さないこと。\n"
            "内容が必要なら、書き込めるプロパティを選び直して書く。\n"
            + "\n".join(r.render() for r in rows)
        )

    def _apply_disable_rows(self, rows: list) -> None:
        """
        差分に含まれる無効化指示を適用する。

        無効化された対象は候補の選択とカタログの両方から外れ、以降のステップ
        では存在自体が見えなくなる（依頼ではなく選択肢から消す）。
        名前は記録して次の起動時に戻す（依頼にもとづく判断で、恒久設定ではない）。
        """
        for row in rows:
            if not isinstance(row, dict) or row.get("field") != DISABLE_FIELD:
                continue

            name = str(row.get("id") or "")
            target = self._resolve_invokable(name)
            if target is None:
                # 提示していない名前を無効化しようとした場合。処理は続ける。
                self._notify_error(
                    f"{self.name}が存在しない対象の無効化を指示しました: '{name}'"
                )
                continue
            if self._is_disabled(target):
                continue  # 元から無効。戻す対象にもしない

            # toolは_wireがAgentごとに複製するため実体へ書き込んでよい。agentは
            # 複製されない（集計の宛先を兼ねており、複製するとトークン累計と
            # private_memoryが分かれる）ため、_runtime_disabled側で持つ。
            if not isinstance(target, Agent):
                target.disabled = True
            self._runtime_disabled.add(name)
            self.interceptor.notify.memory_updated(
                MemoryUpdatedEvent(
                    agent=self.name,
                    message=f"{self.name}が{name}を無効化しました。"
                    f"理由: {row.get('text') or '(記載なし)'}",
                )
            )

    def _is_disabled(self, target: Invokable) -> bool:
        """
        その対象が、このAgentから見て無効化されているか。

            target.disabled   … 利用側の設定、およびtoolの実行時無効
            _runtime_disabled … このAgentが今回のセッション中に無効化した名前

        agentの無効化は実体へ書き込まないため、実体だけを見ると取りこぼす。
        """
        return target.disabled or target.name in self._runtime_disabled

    def _restore_disabled(self) -> None:
        """このAgentがセッション中に無効化したものを元に戻す。"""
        for name in self._runtime_disabled:
            target = self._resolve_invokable(name)
            # agentへは書き込んでいないので戻す対象もtoolだけ（Falseにすると
            # 利用側が設定した無効まで解除してしまう）。
            if target is not None and not isinstance(target, Agent):
                target.disabled = False
        self._runtime_disabled = set()

    # ---- tool / agent の呼び出し ----
    def _resolve_invokable(self, name: str) -> Invokable | None:
        """
        名前からtoolまたはsub_agentを引く（呼び出し側はどちらか知らなくてよい）。
        toolを先に探すのは同名時に単機能処理を優先するため（実質は順序の明示）。
        """
        tool = self.get_tool(name)
        if tool is not None:
            return tool
        return self.get_sub_agent(name)

    @staticmethod
    def _call_signature(call: FunctionCall) -> str:
        """
        呼び出しの同一性を判定するための文字列。
        引数の辞書は順序が変わりうるのでsort_keysで正規化する。
        JSONにできない値（bytes等）が来てもdefault=strで落ちないようにしておく。
        """
        args = json.dumps(call.args, sort_keys=True, ensure_ascii=False, default=str)
        return f"{call.name}({args})"

    def _plan_calls(self, calls: list[FunctionCall]) -> list[_PlannedCall]:
        """
        LLMが要求した呼び出しを、実行する／しないに振り分ける。

        重複拒否と名前の解決はここでしか起きない。逐次で回すのは、並列にすると
        同じ引数の呼び出しが両方走ってしまうため。
        """
        planned = set()
        plan: list[_PlannedCall] = []

        for call in calls:
            signature = self._call_signature(call)

            # 同じ結果しか返さない呼び出しは二重に実行しない（依頼ではなく止める）。
            # ステップをまたぐ判定を成功したものに限るのは、失敗のやり直しは正当な
            # リトライで、止めると復帰できなくなるため。同じバッチの中は成否が
            # 分からないので、実行すると決めた時点で止める。
            if signature in self.last_successful_calls or signature in planned:
                plan.append(
                    _PlannedCall(
                        call=call,
                        decided=ToolResult(
                            error="""直前と完全に同じ呼び出しのため実行しませんでした。同じ結果しか得られません。すでに得られている結果をもとに、次に必要な行動を判断してください。"""
                        ),
                        duplicate=True,
                    )
                )
                continue

            planned.add(signature)

            target = self._resolve_invokable(call.name)
            if target is None:
                # 提示していないものを呼んできた場合。ここへ来る時点でLLMの逸脱か
                # tasksの記述ミスなので、事実として記録して次の判断材料にする。
                plan.append(
                    _PlannedCall(
                        call=call,
                        decided=ToolResult(
                            error=f"'{call.name}' というtool/agentは存在しません。"
                        ),
                    )
                )
                continue

            plan.append(self._planned(call, target))

        return plan

    @staticmethod
    def _planned(call: FunctionCall, target: Invokable) -> _PlannedCall:
        """実行する1件。相手がエージェントなら、起動の表で子の起動として進める。"""
        return _PlannedCall(call=call, target=target, delegate=isinstance(target, Agent))

    def _rebuild_planned(
        self,
        *,
        stage: Stage,
        call: FunctionCall,
        decided: ToolResult | None,
        duplicate: bool,
    ) -> _PlannedCall:
        """
        保存した状態から、バッチの1件を組み直す（runtime.Runtime.loadが呼ぶ）。

        相手は名前で引き直す。初期toolはtools / sub_agentsに登録されていないことが
        あるので、初期のバッチならinitial_toolsから探す。
        """
        if decided is not None:
            return _PlannedCall(call=call, decided=decided, duplicate=duplicate)
        if stage == "initial":
            target = next((t for t in self.initial_tools if t.name == call.name), None)
        else:
            target = self._resolve_invokable(call.name)
        if target is None:
            raise ValueError(
                f"保存した状態の{self.name}の呼び出し '{call.name}' が、"
                f"このエージェントのtool / agentに見つかりません。"
            )
        return self._planned(call, target)

    def _reflect(
        self,
        plan: list[_PlannedCall],
        results: dict[int, ToolResult | None],
        children: dict[int, str],
    ) -> list[ToolHistoryEntry]:
        """
        実行の終わったバッチを、記憶と履歴へ反映して返す。

        LLMが要求した分（_plan_calls）と、システムが機械的に走らせる分
        （_plan_initial_tools）の両方がここを通る。結果の扱い——記憶への
        書き込み、履歴、イベント——を1箇所に持つため、どちらの経路でも
        観測される形が同じになる。実行（並列）は起動の表が済ませている。

        children: 委譲先の起動のrun_id（枠の番号ごと）。
        """
        succeeded_now = {
            self._call_signature(item.call) for item in plan if item.duplicate
        }

        # 呼び出し順のまま逐次で反映する。並列でも完了順ではなくplanの順で回す。
        # プロンプトへ載る順序が実行のタイミングで変わると、同じ依頼でも
        # 読み方が変わってしまう。記憶への書き込みと履歴への追加を同時に行うと
        # 片方が消えるので、ここは並列にしない。
        entries = []
        for index, item in enumerate(plan):
            call, target = item.call, item.target
            result = results[index]
            # 結果の無い枠は、表を回す側の誤り。
            assert result is not None

            entry = ToolHistoryEntry(
                name=call.name,
                kwargs=call.args,
                result=result,
                # 結果の解釈指示と値の意味は、呼ばれた側が持つ。
                # 呼んだ側の人格定義へ書き写さない。
                evaluation=getattr(target, "evaluation", "") if target else "",
                output_schema=getattr(target, "output_schema", None)
                if target
                else None,
            )

            # memoryを返したtoolは、その内容をLLMに要約させず直接記録する。
            # ここで行うのは、評価フェーズを持たないReflexAgentでも効かせるため。
            # 登録側の設定ではなく戻り値で決まるので、同じtoolが状況に応じて
            # 記憶へ書くか選べる。
            if result.memory and result.success:
                entry.written_to_memory, entry.written_task_ids = (
                    self._write_tool_result_to_memory(
                        tool_name=call.name, memory=result.memory
                    )
                )

            if target is not None and result.success:
                succeeded_now.add(self._call_signature(call))

            # 呼び出し1件ごとに、引数と戻り値を構造のまま流す。execute()の中から
            # 出せないのは、callerと周回数を知っているのが呼び出した側だけだから。
            # 実行されなかったもの（存在しない名前・重複で弾いたもの）も流す。
            self.interceptor.notify.executed(
                ExecutedEvent(
                    caller=self.name,
                    callee=call.name,
                    call_type=("agent" if isinstance(target, Agent) else "tool")
                    if target is not None
                    else "",
                    kwargs=call.args,
                    step_no=self._step_no,
                    value=result.value,
                    error=result.error or "",
                    elapsed=result.execution_time,
                    written_to_memory=entry.written_to_memory,
                    caller_run_id=self.current_run_id,
                    # 委譲ならその呼び出しで始まった起動のID。toolは起動を持たない。
                    callee_run_id=children.get(index, ""),
                )
            )

            self.tool_history.append(entry)
            entries.append(entry)

        # 次のステップでの「直前」はこのバッチになる。
        self.last_successful_calls = succeeded_now
        return entries

    def _write_tool_result_to_memory(
        self, *, tool_name: str, memory: dict
    ) -> tuple[bool, list[str]]:
        """
        toolが返したmemoryを記憶へ直接書き込む。

        受け取るのは {プロパティ名: [項目, ...]}。項目は本文の文字列か、LLMの差分の
        行と同じ形のオブジェクト（idを省けばapply_diffのid_prefixで採番する。toolは
        既存の記憶を知らない）。存在しないプロパティ名は通知だけ出して処理を続ける
        （toolの実装ミスでセッションは止めない）。

        戻り値は (結果の中身を記録したか, 書けたtasksのid)。tasksは結果の中身では
        なく次の指示なので、前者には数えない。
        """
        rows = [
            # プロパティ名はキーの方で決まる。項目の中のfieldは使わない。
            {**item, "field": field_name}
            if isinstance(item, dict)
            else {"field": field_name, "text": item}
            for field_name, items in memory.items()
            for item in items
        ]
        if not rows:
            return False, []

        result = apply_diff(rows, *self._writable_memories(), id_prefix=tool_name)
        errors = result.errors

        # LLMを介さない書き込みも同じ経路で観測できるようにする（追う側が主体ごとに
        # 別の窓を見に行かずに済む）。渡すのは採番済みidを持つresult.rows。
        # toolの戻り値（value）はidを持たないため、どこへ入ったかが見えない。
        self.interceptor.notify.memory_diff(
            MemoryDiffEvent(
                agent=self.name,
                phase=None,
                attempt=1,
                rows=list(result.rows),
                errors=list(errors),
                ignored=list(result.ignored),
                source_tool=tool_name,
                step_no=self._step_no,
                apply_seq=result.apply_seq,
                run_id=self.current_run_id,
            )
        )

        # 書けなかった分はLLMへ差し戻さない。原因はtoolの実装であって、
        # LLMが書き方を変えても直らないため（利用側へ通知して知らせる）。
        unwritten = [*errors, *result.ignored]
        if unwritten:
            self._notify_error(
                f"{tool_name}の結果のうち{len(unwritten)}件をmemoryへ書き込めませんでした。\n"
                + "\n".join(e.render() for e in unwritten)
            )

        # 通った行は、result.rowsのうちerrorsに入らなかったもの。分母がresult.rows
        # なのは、渡した行数で判定すると（ignoredの行はrowsへ入らない）1行も適用
        # されていないのに記録済みになり、記憶にも履歴にも何も残らないため。
        # errorsが持つ行は、result.rowsの同じ実体（apply_diffが採番した後の行）。
        failed = {id(e.entry) for e in errors}
        applied = [row for row in result.rows if id(row) not in failed]
        task_ids = [str(row["id"]) for row in applied if row["field"] == "tasks"]
        # 1件でも中身が通っていれば記録済みとして扱う。
        return len(applied) > len(task_ids), task_ids

    # ---- 初期tool ----
    def _plan_initial_tools(self) -> list[_PlannedCall]:
        """
        ループ開始前に、システムが機械的に実行するtoolの計画（確定している手順を
        判断させない分、1ステップ分の推論と失敗可能性が消える）。

        反映の経路はLLMが要求した呼び出しと同じ（_reflect）。記憶への書き込み、
        履歴、イベントが1箇所なので、システムが走らせた分も同じ形で観測できる。

        初期toolへ渡す引数を決め、揃っていないものは実行しないと決める。

        引数は受け取った構造化引数から、呼ばれる側が受け取れるものだけを抜き出す。
        受け取れるものはto_declaration()が答える（toolは関数シグネチャ由来の
        JSON Schema、agentはinput_schema）ので、どちらかを区別せずに済む。

        必須の引数が空なら実行しない。input_schemaのrequiredは「キーがある」
        しか保証しないため、値が""のまま渡って空の条件で検索してしまう
        （呼ばれた側は空文字を正当な指定と区別できない）。
        """
        if not self.initial_tools or not self.current_input:
            return []
        kwargs = self.current_input.kwargs
        plan: list[_PlannedCall] = []

        for target in self.initial_tools:
            schema = target.to_declaration().get("parameters") or {}
            args = {
                k: v for k, v in kwargs.items() if k in schema.get("properties", {})
            }
            call = FunctionCall(name=target.name, args=args)

            missing = [k for k in schema.get("required", []) if _is_blank(args.get(k))]
            if missing:
                # 依頼した側がその値を持っていなかったということなので、
                # このセッションの中では誰も直せない。記憶の材料としては
                # 「取得できなかった」を残し、配線の問題として通知もする。
                reason = (
                    f"必須の引数 {', '.join(missing)} が空のため実行しませんでした。"
                    f"この情報は取得できていません。"
                )
                self._notify_error(
                    f"{self.name}の初期ステップ '{target.name}': {reason}"
                )
                plan.append(_PlannedCall(call=call, decided=ToolResult(error=reason)))
                continue

            plan.append(self._planned(call, target))

        return plan

    # ---- プロンプト組み立て ----
    def _render_capability_catalog(self, *, include_usage: bool = False) -> str:
        """
        自分が使えるtool / agentの一覧。disabledなものは存在自体を見せない。

        include_usage=Trueは自分がtasksを立てる・評価する立場（使い方まで
        見えないと正しいtasksが作れない）。Falseは他のAgentから「孫」として
        覗かれる立場で、そこまで見せる必要がない。
        """
        lines = [
            t.to_catalog_line(include_usage=include_usage)
            for t in self.tools
            if not self._is_disabled(t)
        ]
        lines += [
            a.to_catalog_line(include_usage=include_usage)
            for a in self.sub_agents
            if not self._is_disabled(a)
        ]
        return "\n".join(lines)

    def _blobs_for(self, spec: PhaseSpec) -> list[dict]:
        """
        そのphaseでLLMへ渡す添付。

        渡すかどうかの判断はPhaseSpecが持つ（初期memory構築時と最終回答時のみ）。
        ReflexAgentはmemoryを持たず文字起こしができないため、
        このメソッドをオーバーライドして常に渡す。
        """
        if not self.current_input or not spec.include_blobs:
            return []
        return self.current_input.blobs

    def _render_memory(self) -> str:
        """
        プロンプトに載せるmemoryの中身。sharedとprivateの両方。

        ガイド文は含めない（_render_memory_reading_guide /
        _render_memory_writing_guide がsystem_instruction側へ載せる）。
        交互に並べる方が遵守されやすいが、固定文のガイドを変わる中身と混ぜると
        キャッシュに乗らないため、遵守率よりトークンコストを取っている。
        """
        return join_sections(
            self.shared_memory.render(),
            self.private_memory.render(),
        )

    def _render_memory_reading_guide(self) -> str:
        """memoryの読み方。全phaseで不変なので、system_instructionの共通部へ置く。"""
        return join_sections(
            self.shared_memory.reading_guide(),
            self.private_memory.reading_guide(),
        )

    def _render_memory_writing_guide(self) -> str:
        """
        memoryの書き方。書き込みが発生するphaseだけで使う。

        書き分けの判断はsharedとprivateをまたぐため、ROUTING_GUIDEは
        両方を見せた後に1回だけ置く（片方のガイドに置くと不完全になる）。
        """
        return join_sections(
            self.shared_memory.writing_guide(),
            self.private_memory.writing_guide(),
            section("書き込み先の選び方", ROUTING_GUIDE),
            # 依頼は【依頼内容】かconversationで毎ステップ渡るため、memoryへ複写させない。
            "依頼（依頼内容、またはconversationの原文）は毎ステップ渡されるため、依頼そのものをmemoryへ書き写す必要はない。",
        )

    def _render_input_schema(self) -> str:
        """
        自分が受け取る引数の意味。

        委譲された側には値だけがJSONで届き、宣言（to_declaration）は呼ぶ側に
        しか渡らないため、これが無いとキー名から意味を推測して読むことになる。
        既定のスキーマ（messageだけ、説明なし）の場合は何も返さない。
        """
        props = self.input_schema.get("properties", {})
        if not props:
            return ""
        if set(props) == {"message"} and not props["message"].get("description"):
            return ""

        required = set(self.input_schema.get("required", []))
        lines = [
            "委譲された場合、依頼は次の項目で届く。それぞれの意味は以下のとおり。",
            "",
        ]
        for name, spec in props.items():
            mark = "必須" if name in required else "任意"
            desc = spec.get("description", "（説明なし）")
            lines.append(f"■ {name}（{mark}）")
            lines.append(f"{desc}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _render_output_schema(self) -> str:
        """
        自分が返す最終回答の形。output_schemaを指示文へ変換する。

        必要になるのは_answers_in_loopのクラスだけ（呼び出しフェーズで答える
        ためresponse_schemaで強制できない）。守られる保証は無い。
        """
        if not self._answers_in_loop or not self.output_schema:
            return ""

        props = self.output_schema.get("properties", {})
        if not props:
            # propertiesを持たないスキーマ（ルートが配列、$refだけ等）は項目ごとに
            # 説明できないため、スキーマ自体を載せる（落とすと形式の指定が消える）。
            return join_sections(
                """最終回答の形式が指定されている。文章で最終回答を返すときは、次のJSON Schemaに従うJSONだけを出力すること。説明文やコードブロックの記号は付けない。""",
                json.dumps(self.output_schema, ensure_ascii=False, indent=2),
            )

        required = set(self.output_schema.get("required", []))
        lines = [
            """最終回答の形式が指定されている。文章で最終回答を返すときは、次の形式のJSONだけを出力すること。説明文やコードブロックの記号は付けない。""",
            "",
        ]
        for name, spec in props.items():
            mark = "必須" if name in required else "任意"
            head = f"■ {name}（{mark}"
            if spec.get("type"):
                head += f" / {spec['type']}"
            if spec.get("enum"):
                head += " / " + " | ".join(map(str, spec["enum"]))
            lines.append(head + "）")
            lines.append(spec.get("description", "（説明なし）"))
            lines.append("")
        return "\n".join(lines).rstrip()

    def __repr__(self) -> str:
        """
        dataclassが生成する__repr__を使わない（repr=False）。

        全フィールドを出すと2万字近くになり、見せない前提のもの（審査基準など）
        が混ざる。richのshow_localsや利用側のf"{agent}"で漏れるため、出さない
        ものは最初から出せないようにしておく。
        """
        state = ", disabled=True" if self.disabled else ""
        return (
            f"{type(self).__name__}(name={self.name!r}, model={self.model!r}, "
            f"tools={len(self.tools)}, sub_agents={len(self.sub_agents)}{state})"
        )

    def _build_system_instruction(self, phase: Phase) -> list[str]:
        """
        phase内で変化しない部分。llm.generate(system_instruction=...)へ渡す。
        毎ステップ変わるもの（memoryの中身 / 実行履歴 / 依頼内容）は含めない。

        キャッシュは先頭からの一致で効くので、変わる頻度で3段に分ける。
        ②と③が逆だと、phaseが変わるたびにナレッジ全体が再送になる。

          1. 記憶構成が同じエージェントの全phaseで同一
          2. エージェントごと（ナレッジ / 人格定義 / 各スキーマ / 直接の依頼の扱い）
          3. phaseごと（memoryの書き方 / tool一覧 / 思考 / ステップの指示）

        戻り値は段ごとの配列。区切りを明示する必要があるClaudeがこの境界を
        そのまま使い、Gemini / OpenAIではllm.py側で連結される。
        詳細はREADMEの「プロンプトキャッシュ」を参照。
        """
        spec = PHASE_SPECS[phase]

        # toolsをAPIへ渡すphaseでは文章で説明すると二重になる。渡さないphase
        # （memory構築・更新）は何が使えるか分からないとタスクを立てられないため
        # 文章で見せ、usageも含める。ANSWER系はsummaryのみ。
        if spec.allows_tools:
            catalog = ""
        else:
            include_usage = phase in (Phase.INITIAL_MEMORY, Phase.MEMORY_UPDATE)
            catalog = self._render_capability_catalog(include_usage=include_usage)

        universal = join_sections(
            # ---- ① 記憶構成が同じエージェントの全phaseで同一 ----
            section("全ステップ共通の姿勢", POURING.common),
            section("memoryの読み方", self._render_memory_reading_guide()),
        )
        per_agent = join_sections(
            # ---- ② このエージェントに固有。phaseが変わっても不変 ----
            # ナレッジを先頭へ置く理由と、その条件はdocstringに書いた。
            section("ナレッジ", self.knowledge),
            section("あなたに適用された人格定義", self.system_instruction),
            section("あなたが受け取る依頼の形", self._render_input_schema()),
            section("あなたが返す最終回答の形", self._render_output_schema()),
            section("利用者から直接受けた依頼の扱い", self._direct_request_stance()),
        )
        per_phase = join_sections(
            # ---- ③ phaseで変わる ----
            section(
                "memoryの書き方",
                self._render_memory_writing_guide()
                if spec.include_memory_how_to
                else "",
            ),
            section("実行可能なtool / agent", catalog),
            section(
                "思考の方向づけ", POURING.thinking if spec.include_thinking else ""
            ),
            section("現在のステップでやるべきこと", spec.instruction),
        )
        return [b for b in (universal, per_agent, per_phase) if b]

    def _build_prompt(
        self, *, tool_results: str = "", extra: str = "", phase: Phase | None = None
    ) -> str:
        """
        毎ステップ変化する部分。llm.generate(prompt=...)へ渡す。

        依頼内容はセッション中不変だが、ルールではなく「答えるべき対象」なので
        system_instruction側には置かない（形式説明はあちらが済ませている）。
        現在時刻も同じ扱いで、ルールではなくその時点の状態として渡す。

        tool_results: 今回の実行結果を評価対象として渡す。
        extra: phaseごとの追加指示（出力例、立て直しの注意文など）。
            連結をここで行うのは、現在時刻を必ず最後に置くため。
        phase: 残りの実行回数を見せるかどうかの判断に使う。
        """
        run = self.current_input or RunInput(message="")

        # 回答形式はここで文章として指示しない（_final_answerがresponse_schemaで
        # 強制するため、両方に書くと二重提示になる）。ReflexAgentだけは②段に
        # 指示文を常時載せている（FUNCTION_CALLで答えるため強制が効かない）。
        return join_sections(
            # 利用者から直接受けた依頼は、memoryのconversationに原文として載っている。
            section("依頼内容", "" if run.from_user else run.message),
            section("現在のmemory", self._render_memory()),
            section("あなたの実行履歴", self.render_tool_history()),
            section("今回の実行結果", tool_results),
            extra,
            section("残りの実行回数", self._render_remaining_steps(phase)),
            # 秒まで出すのは、開館時間や締め切りのように時刻で可否が変わる判断を
            # させるため。生成ごとに必ず変わる唯一の値なので、いちばん最後に置く。
            # 前に置くと、後ろに続くものがキャッシュの一致範囲から外れる
            # （system_instruction側へ置けば先頭が毎回変わり、一度も効かなくなる）。
            # astimezone()で明示するのは、UTCで動くサーバーとローカルで
            # 日付がずれるのを気付かずに通さないため。
            section(
                "現在時刻", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            ),
        )

    def _render_remaining_steps(self, phase: Phase | None) -> str:
        """
        tasksを決めるphase（初期構築と記憶の更新）に、あと何回実行できるかを示す。

        上限を知らないまま調べ続けると、答えをまとめる前にmax_stepsで打ち切られる。
        残りが分かれば、答えを大きく変える不足から先に取りに行ける。
        初期構築はループの前なので、全ステップが残っている。
        """
        if phase is Phase.INITIAL_MEMORY:
            remaining = self.max_steps
        elif phase is Phase.MEMORY_UPDATE:
            remaining = self.max_steps - self._step_no
        else:
            return ""
        if remaining <= 0:
            return "これ以上は実行できない。次は、ここまでの情報で回答する。"
        return (
            f"あと{remaining}回まで、tool / agentを実行できる。これは許される上限であり、"
            "使い切る目安ではない。実行するのは、依頼者へ最終的に返す答えに直結するものだけにする。"
            "上限に達すると、そこまでの情報で回答する。"
        )

    def _direct_request_stance(self) -> str:
        """
        利用者から直接受けた依頼の読み方。respond()で起動した時だけ提示する。

        【依頼内容】を載せない代わりに、依頼がconversationのどこにあるかを示す。
        conversationの説明は「requestsを基準に」と委譲先向けに書かれているため、
        窓口にとっては原文そのものが依頼であることを、ここで上書きする。

        依頼の中身は「今回の発言」ではなく「利用者の発言の積み重ね」とする。
        今回の発言が訂正や受け入れだけのとき、それ単体には依頼が無く、
        「省略を履歴で補え」とだけ言うと、補う材料に過去の回答が使われる。
        過去の回答は、利用者が受け入れたかどうかで扱いを分ける。一律に
        「利用者が使った語だけ」と縛ると、「じゃあそれで」のような受け入れを落とす。

        過去の回答に触れずに聞き直された時の扱いは、ここではなくconversationの説明
        （memory.py）に置く。委譲先も会話を読み、前の回答で挙げた候補を「名指しされた
        製品」として調べるため（GPT-6 Solで実測。ここに置いた版では委譲先に見えず、
        委譲先がその候補に固定した）。
        """
        if not self.current_input or not self.current_input.from_user:
            return ""
        return """あなたは利用者から直接依頼を受けている。
依頼の中身は、利用者の発言の積み重ねで決まる。conversation の user-only（利用者の発言だけを古い順に並べたもの。最後が今回の発言）を軸に読む。
「あなた（過去の回答）」は、利用者が何に答え、何を受け入れ、何を言い直しているのかを知るために読む。
- 利用者が過去の回答を受け入れている（その語を使う、「それで」「そう」と同意する）なら、受け入れた内容は依頼の一部になる。
- 利用者が言い直したり、別の言葉で答えたりしているなら、その部分についての過去の解釈は捨てる。
- どちらとも言えないなら、過去の回答の解釈を、利用者の意図として確定させない。
conversationの説明にある「requestsを基準に解釈する」は、委譲を受けた側のための規則である。あなたは原文そのものから、利用者が何を求めているのかを読み取る側にいる。"""


# ---- ReflexAgent ----
class ReflexAgent(Agent):
    # memoryへ要約しないため、tool結果は隠さず履歴に残し続ける。
    _show_tool_results: ClassVar[bool] = True
    # テキストを返した時点でそれが最終回答になる。ANSWERフェーズを通らないため、
    # output_schemaを構造で強制する経路が無い。形式は指示文で伝える。
    _answers_in_loop: ClassVar[bool] = True

    def _writable_memories(self) -> tuple:
        """
        private_memoryは使わない。プロンプトにも載せないため、
        memoryを返したtoolがgoalsやtasksへ書き込めてしまうと、
        誰にも読まれない場所に情報が消える。書き込み先をsharedだけに限る。
        """
        return (self.shared_memory,)

    def _after_initial_tools(self, entries: list[ToolHistoryEntry]) -> None:
        """
        初期toolは実行するが、初期memoryは作らない。

        返ってきた添付は_carried_blobsへ積む。このクラスには文字起こしする先が
        無く、渡す先の生成も無いので、ここで抱えなければ二度と参照できない。
        """
        self._carried_blobs.extend(self._collect_blobs(entries))

    def _after_tools(
        self, entries: list[ToolHistoryEntry], *, notice: str = ""
    ) -> None:
        """
        評価フェーズを持たない。結果は履歴に残るだけ。

        memory更新の仕組みが無いため、停滞時の立て直し（notice）も効かず、
        繰り返しを検出したらそのまま打ち切られる。
        toolが返した添付は、文字起こしする先が無いので蓄積して渡し続ける。
        """
        self._carried_blobs.extend(self._collect_blobs(entries))

    def _select_invokables(self) -> tuple[list[Invokable], str | None]:
        """
        tasksを持たないため、有効なtool/agentを全て提示して"auto"で選ばせる。
        古典的なReActループそのもの。テキストを返した時点でループが終わる。
        """
        # 型注釈が無いと右辺から list[Tool] と推論され、戻り値の list[Invokable]
        # と食い違う（listは不変なので、要素がInvokableを満たしていても
        # list[Tool] を list[Invokable] として扱うことは許されない）。
        targets: list[Invokable] = [t for t in self.tools if not self._is_disabled(t)]
        targets += [a for a in self.sub_agents if not self._is_disabled(a)]
        return targets, "auto"

    def _render_memory(self) -> str:
        """shared_memoryは読む。private_memoryは持たない（更新する仕組みが無い）。"""
        return self.shared_memory.render()

    def _render_memory_reading_guide(self) -> str:
        """private_memoryを持たないため、sharedの読み方だけ。"""
        return self.shared_memory.reading_guide()

    def _render_memory_writing_guide(self) -> str:
        """
        LLMの差分でmemoryを書くphase（INITIAL_MEMORY / MEMORY_UPDATE）を通らないため、
        書き方は一切載せない。toolのmemoryによる直接書き込みは
        LLMが差分を書くわけではないので、ここの指示とは無関係。
        """
        return ""

    def _blobs_for(self, spec: PhaseSpec) -> list[dict]:
        """
        添付は常に渡す（文字起こしする仕組みが無く、渡さないと二度と参照
        できない）。そのぶん添付を返すtoolを何度も呼ぶと毎生成へ積まれ続ける。
        1回読んで畳みたい場合は、記憶を持つAgentへ担当させる。
        """
        if not self.current_input:
            return list(self._carried_blobs)
        return self.current_input.blobs + self._carried_blobs

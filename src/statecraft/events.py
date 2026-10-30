"""
Interceptorへ渡るイベント。タイミング1つにつき型1つで、名前はタイミングの
名前に揃える（execute_start → ExecuteStartEvent）。コールバックが受け取るのは、
どの窓口でもこの型の値1つだけなので、何が載っているかはここを見れば分かる。

    notice（受け取るだけ。戻り値は見ない）
        respond_start       RespondStartEvent       respond()を始めた
        respond_end         RespondEndEvent         respond()を終えた
        execute_start       ExecuteStartEvent       tool / agentの実行を始めた
        execute_end         ExecuteEndEvent         tool / agentの実行を終えた
        execute_blocked     ExecuteBlockedEvent     実行が止められた
        executed            ExecutedEvent           呼んだ側から見た実行1件の記録
        generated           GeneratedEvent          生成1回の実測値
        memory_diff         MemoryDiffEvent         記憶の差分1回分
        memory_updated      MemoryUpdatedEvent      記憶の更新を終えた
        error               ErrorEvent              処理は続くが記録すべき異常
    guard（止められる。判定の型で返す）
        before_execute      BeforeExecuteEvent      → ExecuteDecision
        before_generate     BeforeGenerateEvent     → GenerateDecision
    recover（失敗した後を決める。判定の型で返す）
        generation_failed   GenerationFailedEvent   → RetryDecision

判定の型（ExecuteDecision など）はinterceptor.pyにある。

messageを持つものは、人が読む1行をそこへ入れる（文言はtool / agentが組み立てる）。
機械で扱う値は、message以外の項目から読む。
"""

from dataclasses import dataclass, field
from typing import Any

from .prompts import Phase


# ---- notice: 応答（respondの境界） ----
@dataclass
class RespondStartEvent:
    """respond()を始めた。委譲（execute）では流れない（そちらはexecute_start）。"""

    agent: str  # 応答するAgentの名前
    message: str  # 表示用の1行


@dataclass
class RespondEndEvent:
    """respond()を終えた。例外で抜けた場合は流れない。"""

    agent: str  # 応答したAgentの名前
    message: str  # 表示用の1行


# ---- notice: 実行（tool / agent 共通） ----
@dataclass
class ExecuteStartEvent:
    """
    tool / agentの実行を始めた（before_executeを通った後）。

    呼ばれた側（Tool.execute / Agent.execute）が出すので、呼び出し元や周回数は
    載らない。それらが要るならexecutedを使う。
    文言の組み立てに失敗した場合は流れず、代わりにerrorが流れる。
    """

    name: str  # 実行するtool / agentの名前
    kwargs: dict  # 渡された引数
    # execution_message / describe_executionが組み立てた表示用の1行。
    # 利用者の画面へ出すのは、message系のうちこれだけを想定している。
    message: str


@dataclass
class ExecuteEndEvent:
    """
    tool / agentの実行を終えた。終わった瞬間に、呼ばれた側から流れる。

    executedとはタイミングが違う。あちらは同じバッチの実行がすべて終わった後に、
    呼んだ側から流れる（並列実行では、終わった順とバッチの順が食い違う）。
    """

    name: str  # 実行したtool / agentの名前
    success: bool  # 成功したか。失敗の中身はerrorイベントで流れる
    message: str  # 表示用の1行


@dataclass
class ExecuteBlockedEvent:
    """
    実行が止められた。before_executeが拒否した時と、無効化されたagentを
    呼んだ時に流れる（toolの無効化では流れない）。
    """

    name: str  # 止められたtool / agentの名前
    # 渡されるはずだった引数。観測のためにguardへ登録しなくても、
    # 止められた呼び出しの引数をここで見られる。
    kwargs: dict
    # before_executeが返した拒否の理由。理由を伝えない拒否と、
    # 無効化による停止では空文字。
    reason: str
    message: str  # 表示用の1行


@dataclass
class ExecutedEvent:
    """
    tool / agentの実行1回分。各項目の意味はREADMEを参照。

    発火するのは呼び出した側（_run_plan）。
    Tool.execute / Agent.executeは呼び出し元も周回数も知らない。
    """

    caller: str  # 呼び出した側のAgent名
    callee: str  # 呼ばれたtool / agent名
    call_type: str  # "tool" | "agent"。解決できなかった名前は空文字
    kwargs: dict  # 渡した引数
    step_no: int  # ReActループの何周目か（1始まり）

    # 戻り値そのもの。整形は観測側が決める（MemoryDiffEvent.rowsと同じ扱い）。
    # 失敗・拒否された場合はNone。
    value: Any = None
    # 失敗した理由、または拒否の理由。成功した場合は空文字。
    # before_executeによる拒否とtool自身の失敗はどちらもここへ入るので、
    # 拒否だけを見たいならexecute_blockedを使う。
    error: str = ""
    # 呼び出しにかかった秒数。before_executeの判定時間も含む
    # （人の承認を待つ構成では、その待ち時間がここへ乗る）。
    elapsed: float = 0.0
    # memoryを返したtoolで、実際に記憶へ書き込めたか。
    written_to_memory: bool = False

    # 呼び出した側の起動ID（caller の current_run_id）。
    caller_run_id: str = ""
    # 呼ばれた側の起動ID。calleeがagentの場合だけ入り、toolでは空文字。
    # callee（名前）だけでは同じエージェントが並列に呼ばれた時に区別できない。
    # callee側のGeneratedEvent.run_idと一致する。
    callee_run_id: str = ""

    @property
    def success(self) -> bool:
        """成功したか。errorの有無から導出する（ToolResult.successと同じ考え方）。"""
        return not self.error


# ---- notice: 生成 ----
@dataclass
class GeneratedEvent:
    """
    生成1回分の実測値。時間とトークンを構造のまま観測側へ渡す。
    各項目の意味はREADMEを参照。
    """

    agent: str  # どのAgentの生成か
    phase: Phase  # どのphaseの生成か
    model: str  # 実際に使われたモデル名（phase_overridesの結果が見える）
    elapsed: float  # この試行にかかった秒数
    input_tokens: int  # キャッシュから読まれた分を含む合計
    output_tokens: int

    # input_tokensのうちキャッシュから読まれた分。プロンプトキャッシュが
    # 実際に効いているかは、ここを見ないと分からない（合計だけでは判断できない）。
    cached_tokens: int = 0
    # キャッシュへ書き込んだ分。Claudeのみ報告される。
    # 毎回これが立つなら、キャッシュが当たっておらず割高になっている。
    cache_write_tokens: int = 0

    # 何回目の試行か（1始まり）。再試行が起きた場合、同じphaseから
    # このイベントが複数回流れる。
    attempt: int = 1
    # 失敗した試行では例外の内容が入る。成功した試行では空文字。
    # 失敗した回も流す（落とすと再試行に費やした時間が計測から消える）。
    # 再試行の待ち時間は含まない（待つのはgeneration_failedのコールバック側）。
    error: str = ""

    # ReActループの何周目の生成か（1始まり）。MemoryDiffEventと同じ定義。
    step_no: int = 0

    # この生成が、どの起動（respond / execute 1回）の中で走ったか。
    # agentは名前なので、同じエージェントが並列に動くと区別が付かない。
    # ExecutedEvent.callee_run_idと同じ値で結ばれ、呼び出しの木が辿れる。
    run_id: str = ""

    # なぜ生成が終わったか。"end" / "tool_use" / "max_tokens" / "refusal" 等。
    # 例外で失敗した試行では空文字（応答自体が無い）。
    #
    # max_tokens と refusal はHTTP 200で返るためerrorには現れない。ここを見ないと
    # 「同じphaseの生成が何度も並んでいる」理由が内訳から読めない。
    stop_reason: str = ""

    # 思考の要約。carry_thoughtsの時だけ入る（それ以外は要求しないので空）。
    # 継承した思考は次の判断に効くのに中身が見えないため、ここで観測する。
    thoughts: str = ""


# ---- notice: 記憶 ----
@dataclass
class MemoryDiffEvent:
    """
    記憶の差分を1回の生成につき1つ、構造のまま観測側へ渡す。

    材料・出力・結果を1つのイベントに揃えてあり、失敗した時も発火する
    （検証で見たいのはむしろ失敗した回）。各項目の意味はREADMEを参照。
    """

    agent: str  # どのAgentが出したか
    phase: Phase | None  # どのphaseの生成か。Noneはtoolの直接書き込み
    attempt: int  # 何回目の生成か（1始まり。再生成の経過が見える）
    rows: list  # 差分そのもの。textを含む
    errors: list  # 適用に失敗した行。差し戻して再生成させる

    # 反映しなかったが差し戻さない行（システム所有プロパティへの書き込み等）。
    # errorsと違い再生成では直らないため、次のステップで1回だけ伝える。
    ignored: list = field(default_factory=list)
    tool_results: str = ""  # この差分を作らせた材料
    raw_text: str = ""  # 配列として解釈できなかった場合の生出力

    # memoryを返したtoolが直接書き込んだ場合、そのtool名。
    # LLMが作った差分とtoolが返した値は検証の意味が違うため区別する。
    source_tool: str = ""

    # ReActループの何周目で起きたか（1始まり）。1周は
    # 「FUNCTION_CALL → 実行 → MEMORY_UPDATE」で、INITIAL_MEMORYはstep 1に属する。
    step_no: int = 0

    # 記憶へ何番目に適用されたか（apply_diffがロックの中で採番した番号）。
    # 通知はロックの外なので、届く順序は適用順ではない。後から再適用するなら
    # 受け取った順ではなくこの昇順で並べ直す。
    apply_seq: int = 0

    # この差分が、どの起動（respond / execute 1回）の中で作られたか。
    # GeneratedEvent.run_id と同じ定義（適用の順序はapply_seqが持つ）。
    run_id: str = ""

    @property
    def applied(self) -> bool:
        """全行が反映されたか。errorsの有無から導出する（ToolResult.successと同じ考え方）。"""
        return not self.errors


@dataclass
class MemoryUpdatedEvent:
    """
    記憶の更新を終えた（全行が反映された時）。LLMの指示で対象を無効化した
    時にも流れる。反映されない行が残った場合は、こちらではなくerrorが流れる。
    差分を構造のまま見たいならmemory_diffを使う。
    """

    agent: str  # 記憶を更新したAgentの名前
    message: str  # 表示用の1行。何を更新・無効化したかが入る


# ---- notice: 異常 ----
@dataclass
class ErrorEvent:
    """
    処理は継続するが記録すべき異常。

    LLMへ返すエラー文（expose_error_detailsで隠れる場合がある）とは別に、
    観測者へは実際に起きたことを伝える。
    """

    source: str  # 異常を見つけたtool / agentの名前
    message: str  # 何が起きたか。人が読む文


# ---- guard ----
@dataclass
class BeforeExecuteEvent:
    """
    tool / agentを実行する直前。toolとagentの両方で流れるので、
    委譲そのものも審査できる。
    """

    name: str  # 呼ばれようとしているtool / agentの名前
    kwargs: dict  # それへ渡される引数。LLMが埋めた値


@dataclass
class BeforeGenerateEvent:
    """
    LLMへリクエストを投げる直前。試行ごとに流れる（1回目だけだと、
    再試行で予算を超えても止まらない）。
    """

    agent: str  # 生成しようとしているAgentの名前
    phase: Phase  # どのphaseの生成か
    model: str  # そのとき使うモデル名。切り替えた後は切り替え先が入る
    attempt: int  # 何回目の試行か（1始まり）
    step_no: int  # ReActループの何周目か（1始まり）


# ---- recover ----
@dataclass
class GenerationFailedEvent:
    """生成が例外で終わった時。HTTP 200で返るmax_tokensやrefusalでは流れない。"""

    agent: str  # 生成していたAgentの名前
    phase: Phase  # どのphaseの生成か
    model: str  # その回に実際に使ったモデル名。切り替えた後は切り替え先が入る
    attempt: int  # 何回目の試行か（1始まり）
    error: Exception  # 起きた例外そのもの

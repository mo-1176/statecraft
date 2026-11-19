import copy
import threading
import time
from typing import Any

from .agent import Agent, AgentResponse, ReflexAgent
from .auditor import AUDITOR_NAME, Auditor, AuditorSettings
from .events import AgentState, GeneratedEvent, NetworkState, StepTime
from .interceptor import Interceptor
from .memory import MemoryEntry, SharedMemory
from .prompts import Phase
from .runtime import Pending, Runtime, Suspended
from .state import STATE_VERSION, dump_memory, restore_memory


class Network:
    """
    1リクエスト分のAgent群の入れ物。

        net = Network(agents=[front, librarian])
        response = net.respond(agent="front", message="...")

    構築時にSharedMemoryを1つ作って全Agentへ配るため、インスタンスの寿命が
    セッションの寿命になる。Agent実体を使い回す場合はreset()を呼ぶ。

    担うのはAgent単体では決められない配線だけ。

      1. SharedMemoryを1つ作り、全Agentへ同じ実体を渡す
      2. sub_agent_names（文字列）をsub_agents（実体）へ解決する
      3. interceptorの既定値を配る
      4. Toolインスタンスを複製する（無効化が他のAgentへ波及しないように）
      5. 配線の誤りを構築時に落とす（存在しない委譲先、循環、初期toolの引数不整合）
      6. enable_auditorを渡せば、監査役を全Agentの記憶の更新へ繋ぐ（auditor.py）
      7. どのイベントにも、その時点のネットワークの状態（NetworkState）を付ける
      8. 依頼を起動の表（runtime.py）で進め、中断したら保存・復元・再開の入口を持つ

    llmは配らない。接続先はAgentごとに違うのが前提のため。

    【中断と再開】
    toolの本体かbefore_executeのコールバックがsuspend()を呼ぶと、respond()は
    Suspendedを返す。同じプロセスならそのままresume()で続けられる。プロセスを
    またぐなら、状態を取り出して保存し、同じ設定で組んだNetworkへ戻す。

        result = net.respond(agent="front", message="...")
        if isinstance(result, Suspended):
            store.put(key, net.save_state())          # 保存先は利用側が決める

        net = build_network()                          # 別のプロセスでもよい
        net.load_state(store.get(key))                 # 戻すだけで、動かさない
        result = net.resume(answers={result.pending[0].point: "候補B"})

    保存するのは実行して変わったもの（記憶・起動の途中の状態・起動の表）だけで、
    設定は含めない。読み込む側のNetworkは、エージェントとtoolの名前を揃えて組む。
    """

    def __init__(
        self,
        *,
        agents: list[Agent],
        interceptor: Interceptor | None = None,
        preload: dict[str, list[str]] | None = None,
        enable_auditor: AuditorSettings | None = None,
    ):
        if not agents:
            raise ValueError("agentsを1つ以上指定してください")

        # 記憶は常に空で始まる。引き継ぎは構築後にshared_memoryへ代入する
        # （全Agentが同じ実体を参照するため、代入は全員へ反映される）。
        self.shared_memory = SharedMemory()
        self.interceptor = interceptor or Interceptor()
        self.agents: dict[str, Agent] = {}

        for agent in agents:
            if agent.name in self.agents:
                raise ValueError(f"Agent名が重複しています: {agent.name}")
            self.agents[agent.name] = agent

        # 生成ごとの消費と時間。generatedのイベントから数える（監査役の分も
        # 同じinterceptorを通るので、ここで一緒に数えられる）。
        self._usage: dict[str, AgentState] = {}
        self._usage_lock = threading.Lock()
        # net.respond()を始めた時刻。状態のelapsedはここから数える。
        self._started: float | None = None
        self.interceptor._attach_state(self._state)

        # 進めている依頼の起動の表。依頼が終われば捨て、中断している間は残る。
        self._runtime: Runtime | None = None
        # 依頼を始めた時点のトークン累計（中断をまたいでも、依頼全体の消費を返すため）
        self._tokens_at_start: tuple[int, int] = (0, 0)
        # 中断した時点の経過秒数。再開したらここから数え直す。
        self._elapsed = 0.0

        self._wire()
        self._resolve_sub_agents()
        self._detect_cycles()
        self._preload(preload or {})

        self._auditor: Auditor | None = None
        if enable_auditor is not None:
            self._auditor = Auditor(enable_auditor, self.agents, self.interceptor)
            # 全Agentが同じinterceptorを使うので、1回繋げば全員の記憶の更新に効く
            # （記憶を持たないReflexAgentでは呼ばれない）。
            self.interceptor.guard.before_memory_diff(self._auditor.review)

    # ---- 配線 ----
    def _wire(self) -> None:
        for agent in self.agents.values():
            agent.shared_memory = self.shared_memory

            # Toolを複製する。共有したままだと、あるAgentが立てたdisabledが
            # 他のAgentにも、次のリクエストにも残る（浅いコピーで足りる）。
            # 複製元は最初に渡されたTool。agent.toolsは下で置き換えるので、
            # 2回目の配線でそこから複製すると無効化済みのコピーが元になる。
            if agent._source_tools is None:
                agent._source_tools = agent.tools
            agent.tools = [copy.copy(t) for t in agent._source_tools]

            # interceptorはNetworkのもの。Agentが持っているのは、単体で動かす時と
            # 配線の都合のため。個別の登録を残すと、そのAgentにだけNetworkの
            # 観測や費用の上限が黙って効かなくなるので、必ず差し替える。
            agent.interceptor = self.interceptor

            # llmだけ差し替えてmodelを書き忘れると、別プロバイダのモデル名を
            # 送ることになる。APIは「そんなモデルは無い」しか返さず原因が
            # 見えないため、構築時に確かめる。
            for phase, override in agent.phase_overrides.items():
                # 打ち切り・停滞で答える時は、ANSWERの上書きが使われる
                # （resolve_generation）。別に書けると、答えの設定が2か所に割れる。
                if phase in (Phase.CUTOFF_ANSWER, Phase.STALLED_ANSWER):
                    raise ValueError(
                        f"{agent.name}のphase_overrides[{phase.name}]は書けません。"
                        f"打ち切りや停滞で答える時も、ANSWERの設定が使われます。"
                    )
                if override.llm is not None and override.model is None:
                    raise ValueError(
                        f"{agent.name}のphase_overrides[{phase.name}]がllmだけを"
                        f"上書きしています。接続を変えるならmodelも指定してください"
                        f"（モデル名はプロバイダごとに異なるため、"
                        f"既定の '{agent.model}' がそのまま使われると食い違います）。"
                    )

            # ReflexAgentが通らないphaseを上書きしていないか。
            # 記憶を作るphaseは持たない。ANSWERも、呼ぶ対象があればテキストを
            # 返した時点でループを抜けるため通らない。逆に対象を持たない
            # ReflexAgent（単発の判定・分類）ではANSWERが唯一通るphaseになる。
            if isinstance(agent, ReflexAgent):
                dead = {Phase.INITIAL_MEMORY, Phase.MEMORY_UPDATE}
                if agent.tools or agent.sub_agent_names or agent.sub_agents:
                    dead.add(Phase.ANSWER)
                for phase in agent.phase_overrides:
                    if phase not in dead:
                        continue
                    if phase is Phase.ANSWER:
                        reason = (
                            "ReflexAgentはテキストを返した時点でループを抜けるため、"
                            "利用者が読む回答文はFUNCTION_CALLフェーズが生成します"
                            "（output_schemaはsystem_instructionの指示文として伝わります）。"
                            "ANSWERへ入るのは、提示できる対象が実行時に全て無効化された場合だけです。"
                            "回答文のモデルを変えたいならAgent自身のmodelを指定してください。"
                        )
                    else:
                        reason = (
                            "ReflexAgentは記憶を作るphaseを持たないため、"
                            "このphaseの生成は一度も発生しません。"
                        )
                    raise ValueError(
                        f"{agent.name}のphase_overrides[{phase.name}]は効きません。{reason}"
                    )

            # 実行時に落とすとexecute()の例外処理に飲まれ、「なぜか初期toolが
            # 動かない」形になるため、構築時に確かめる。
            seen: set[str] = set()
            available = set((agent.input_schema or {}).get("properties", {}))
            for target in agent.initial_tools:
                # 同じものを2度並べても、同一の名前と引数で2回呼ぶだけになる
                # （2回目は重複として拒否される）。設定ミス以外に理由が無い。
                if target.name in seen:
                    raise ValueError(
                        f"{agent.name}のinitial_toolsに '{target.name}' が重複しています。"
                    )
                seen.add(target.name)

                # agentを置く場合、配線を受けていない実体だと自分だけの空の
                # 共有記憶で動く。sub_agentsと同じ理由で同じチェックをする。
                if (
                    isinstance(target, Agent)
                    and self.agents.get(target.name) is not target
                ):
                    raise ValueError(
                        f"{agent.name}のinitial_tools内の'{target.name}'がagentsに"
                        f"含まれていません。Networkの配線を受けないため、"
                        f"shared_memoryが共有されません。"
                    )

                # 初期ステップの引数はinput_schemaの同名プロパティから渡される。
                # 噛み合っていないと引数ゼロで呼ばれ、全てに既定値があると
                # 依頼と無関係な結果が事実としてmemoryへ書かれる。
                schema = target.to_declaration().get("parameters") or {}
                if missing := set(schema.get("required", [])) - available:
                    raise ValueError(
                        f"{agent.name}のinitial_tools '{target.name}' が必要とする引数"
                        f" {sorted(missing)} が、このAgentのinput_schemaに存在しません。"
                        f"input_schemaを合わせるか、別の対象を指定してください。"
                    )

    def _resolve_sub_agents(self) -> None:
        for agent in self.agents.values():
            # 利用側が実体で直接指定した分。agent.sub_agentsは下で解決済みの
            # 実体に置き換わるため、毎回そこから読むと2回目の構築で
            # 「名前と実体の併用」と誤判定される。
            if agent._source_sub_agents is None:
                agent._source_sub_agents = agent.sub_agents
            direct = agent._source_sub_agents

            resolved = []
            for name in agent.sub_agent_names:
                if name not in self.agents:
                    raise ValueError(
                        f"{agent.name}が存在しないagentを参照しています: {name}"
                    )
                if name == agent.name:
                    raise ValueError(f"{agent.name}が自分自身を参照しています")
                resolved.append(self.agents[name])
            # 併用されると、どちらを採ってももう一方が黙って消える。
            if resolved and direct:
                raise ValueError(
                    f"{agent.name}がsub_agent_namesとsub_agentsを同時に指定しています。"
                    f"どちらか一方にしてください。"
                )
            # 配線を受けていない実体が委譲先に混ざると、shared_memoryが
            # 別インスタンスのまま実行される。
            for sub in direct:
                if self.agents.get(sub.name) is not sub:
                    raise ValueError(
                        f"{agent.name}のsub_agents内の'{sub.name}'がagentsに含まれていません。"
                        f"Networkの配線を受けないため、shared_memoryが共有されません。"
                    )
            agent.sub_agents = resolved or direct

    def _detect_cycles(self) -> None:
        """
        委譲の循環（A→B→A）を構築時に検出する。

        循環があると各Agentはmax_stepsで止まるが、委譲の深さが際限なく増える。
        実行時には「終わらない」という形でしか現れないため、ここで落とす。

        深さ優先で辿り、集合を2つ使う。

            visiting … 今辿っている経路上にいるAgent
            done     … 探索が終わったAgent（循環が無いと確定済み）

        訪問済みかどうかだけで判定すると、A→B→D / A→C→D のような合流を
        循環と誤判定する。
        """
        visiting, done = set(), set()

        def walk(agent: Agent, path: list[str]) -> None:
            # 既に調べ終わっているなら、辿り直す必要はない。
            if agent.name in done:
                return
            # 今辿っている経路上に再び現れたら、それが循環。
            if agent.name in visiting:
                loop = " → ".join([*path, agent.name])
                raise ValueError(f"委譲が循環しています: {loop}")

            visiting.add(agent.name)
            # 初期ステップの委譲先も委譲の辺として辿る。sub_agentsだけを見ると
            # A.initial_tools=[B] / B.sub_agents=[A] の循環が残り、実行時には
            # 再帰が止まらない形で現れる。
            for sub in (*agent.sub_agents, *agent.initial_tools):
                if not isinstance(sub, Agent):
                    continue
                # pathは、循環を見つけた時に経路をエラーへ出すために渡す。
                walk(sub, [*path, agent.name])
            # この枝を抜けるので、経路上からは外す。
            visiting.discard(agent.name)
            done.add(agent.name)

        # どのAgentからも辿れない孤立した部分が残らないよう、全員を起点にする。
        for agent in self.agents.values():
            walk(agent, [])

    def _preload(self, preload: dict[str, list[str]]) -> None:
        """
        セッション開始時点でSharedMemoryへ入れておく内容。

        privateは各Agentがセッション内で構築するため対象はsharedだけ。
        システムからの書き込みなので、システム所有プロパティの制限は受けない。
        """
        for field_name, texts in preload.items():
            bucket = getattr(self.shared_memory, field_name, None)
            # 型の不正ではなく設定の誤り（プロパティが無い、または配列でない）
            # なので、他の構築時検証と揃えてValueErrorにする。
            if not isinstance(bucket, list):
                raise ValueError(f"preloadできないプロパティです: {field_name}")  # noqa: TRY004
            for i, text in enumerate(texts, start=1):
                bucket.append(MemoryEntry(id=f"preload-{field_name}-{i}", text=text))

    # ---- 利用 ----
    def reset(self) -> None:
        """
        同じAgent実体で次のリクエストを始められる状態へ戻す。

        消える: 共有記憶 / 各Agentのprivate_memory / トークン累計 /
                セッション中の無効化 / 起動ごとの状態（依頼・履歴・周回数・添付）
        残る:   配線と構築時の検証結果 / Agentの設定

        Agentをモジュールレベルで定義して使い回す構成で、次のリクエストの
        手前で呼ぶ。自動では走らない——リクエストの区切りを知っているのは
        利用側だけで、構築時に消すと「構築後に引き継ぎを入れる」使い方と
        噛み合わないため。引き継ぎを入れるのはこのメソッドの後。

            network.reset()
            network.shared_memory.ontology = carried
        """
        # 空にするのではなく差し替える（項目が増えた時の消し忘れを防ぐ）。
        # reset()の前に取った参照は古い実体を指したままになる。
        self.shared_memory = SharedMemory()

        for agent in self.agents.values():
            agent.reset()
        if self._auditor is not None:
            self._auditor.reset()
        with self._usage_lock:
            self._usage = {}
        self._started = None
        self._runtime = None
        self._tokens_at_start = (0, 0)
        self._elapsed = 0.0

        # 新しい共有記憶と、_source_toolsから複製し直したtoolを配る
        # （設定としてのdisabledだけが残る）。
        self._wire()

    def respond(
        self,
        *,
        agent: str,
        message: str,
        history: list[dict] | None = None,
        blobs: list[dict] | None = None,
    ) -> AgentResponse | Suspended:
        """
        利用者からの依頼を、名前で指したエージェント（窓口）へ渡す。

            response = net.respond(agent="front", message="...", history=[...])

        Agent.respond()と違い、トークン数は委譲先と監査役を含む、この1回の依頼の
        全体を返す。利用者が知りたいのは「この依頼でいくら使ったか」で、各Agentが
        自分の分だけを数えているのは、二重に数えないための集計の都合だから。

        全体の増えた分で数えるので、同じNetworkで同時に2つの依頼を走らせると
        混ざる（共有の記憶も混ざるので、そもそも同時には使わない）。

        途中でsuspend()が呼ばれたら、そこで止まってSuspendedを返す
        （中断している地点の一覧を持つ）。続きはresume()で進める。
        """
        self._started = time.perf_counter()
        self._tokens_at_start = self.total_tokens()
        self._runtime = Runtime()
        self._runtime.begin_respond(
            self.get(agent),
            message=message,
            history=list(history or []),
            blobs=list(blobs or []),
        )
        return self._drive()

    def resume(self, answers: dict[str, Any] | None = None) -> AgentResponse | Suspended:
        """
        中断した依頼を続きから進める。戻り値はrespond()と同じ（また止まることもある）。

            answers={pending.point: 答え}

        答えの扱いは、どこで止まったか（Pending.at）で変わる。

          tool           … 答えがそのtoolの結果になる（実行し直さない）。
                           ToolResultを渡せば、失敗や記憶への書き込みも渡せる
          before_execute … 同じ呼び出しを判定し直し、そこのsuspend()が答えを返す

        答えなかった地点は、実行し直す（同じ所でまた止まることもある）。
        依頼全体のトークン数は、中断する前の分も含めて返す。
        """
        if self._runtime is None:
            raise RuntimeError(
                "再開する依頼がありません。resume()は、respond() / resume() が"
                "Suspendedを返した後か、load_state()の後に呼びます。"
            )
        self._runtime.answer(answers or {})
        self._started = time.perf_counter() - self._elapsed
        return self._drive()

    def _drive(self) -> AgentResponse | Suspended:
        """起動の表を進め、終われば依頼全体の消費を添えて返す。"""
        assert self._runtime is not None
        try:
            outcome = self._runtime.drive()
        except BaseException:
            # 途中で例外が出た依頼は再開できない（表と各Agentの状態が揃っていない）。
            self._runtime = None
            raise
        if isinstance(outcome, Suspended):
            self._elapsed = time.perf_counter() - (self._started or time.perf_counter())
            return outcome

        self._runtime = None
        after_in, after_out = self.total_tokens()
        before_in, before_out = self._tokens_at_start
        return AgentResponse(
            text=outcome.text,
            steps=outcome.steps,
            input_tokens=after_in - before_in,
            output_tokens=after_out - before_out,
        )

    @property
    def pending(self) -> list[Pending]:
        """
        中断している地点。load_state()で戻した後に、利用者へもう一度見せる時に使う。
        中断していなければ空。
        """
        return self._runtime.pending() if self._runtime else []

    # ---- 中断した依頼の保存と復元 ----
    def save_state(self) -> dict:
        """
        中断している依頼の状態を、JSONにできる形で取り出す。保存先は利用側が決める。

        入るのは実行して変わったものだけ（共有の記憶、各Agentの記憶と起動の途中の
        状態、継承する思考、起動の表、消費の集計）。toolの戻り値とsuspend()に渡した
        値も入るので、JSONにできない値が入っているとTypeErrorになる。
        """
        if self._runtime is None:
            raise RuntimeError(
                "中断している依頼がありません。save_state()は、respond() / resume() が"
                "Suspendedを返した後に呼びます。"
            )
        with self._usage_lock:
            usage = {name: _dump_usage(used) for name, used in self._usage.items()}
        return {
            "version": STATE_VERSION,
            "shared_memory": dump_memory(self.shared_memory),
            "agents": {name: agent._dump_state() for name, agent in self.agents.items()},
            "runtime": self._runtime.dump(),
            "usage": usage,
            "auditor": None
            if self._auditor is None
            else [self._auditor.total_input_tokens, self._auditor.total_output_tokens],
            "tokens_at_start": list(self._tokens_at_start),
            "elapsed": self._elapsed,
        }

    def load_state(self, state: dict) -> None:
        """
        save_state()で取り出した状態へ戻す。戻すだけで、動かさない（resume()で動く）。

        このNetworkの今の状態は捨てる（reset()してから書き戻す）。エージェントと
        toolは名前で結び直すので、同じ名前で組んだNetworkへ読み込む。人格定義や
        プロンプトを変えて組み直したNetworkへ戻すこともできる（変えた後の設定で
        続きが動く）。読み込みに失敗した場合は、reset()した状態で残る。
        """
        if state.get("version") != STATE_VERSION:
            raise ValueError(
                f"保存した状態の版（{state.get('version')}）が、このstatecraftの版"
                f"（{STATE_VERSION}）と違うため読み込めません。"
            )
        if unknown := set(state["agents"]) - set(self.agents):
            raise ValueError(
                f"保存した状態にあるエージェントが、このNetworkにありません: {sorted(unknown)}"
            )

        self.reset()
        restore_memory(self.shared_memory, state["shared_memory"])
        for name, data in state["agents"].items():
            self.agents[name]._load_state(data)
        with self._usage_lock:
            self._usage = {name: _load_usage(used) for name, used in state["usage"].items()}
        if self._auditor is not None and state["auditor"] is not None:
            (
                self._auditor.total_input_tokens,
                self._auditor.total_output_tokens,
            ) = state["auditor"]
        self._tokens_at_start = (state["tokens_at_start"][0], state["tokens_at_start"][1])
        self._elapsed = state["elapsed"]
        self._runtime = Runtime.load(state["runtime"], self.agents)

    # ---- イベントに付ける状態 ----
    def _state(self, event: object) -> NetworkState:
        """
        interceptorがイベントを配る直前に呼ぶ。generatedなら先に数えてから、
        その時点の写しを返す（その生成の分まで入った状態を、そのイベントで見せる）。
        """
        with self._usage_lock:
            if isinstance(event, GeneratedEvent):
                self._count(event)
            agents = {}
            names = [*self.agents, *([AUDITOR_NAME] if self._auditor else [])]
            for name in [*names, *(n for n in self._usage if n not in names)]:
                used = self._usage.get(name) or AgentState()
                agent = self.agents.get(name)
                agents[name] = AgentState(
                    # ReflexAgentは記憶を使わないので、空の見出しを見せない
                    memory=""
                    if agent is None or isinstance(agent, ReflexAgent)
                    else agent.private_memory.render(),
                    input_tokens=used.input_tokens,
                    output_tokens=used.output_tokens,
                    cached_tokens=used.cached_tokens,
                    cache_write_tokens=used.cache_write_tokens,
                    seconds=used.seconds,
                    steps=[
                        StepTime(s.run_id, s.step_no, dict(s.seconds)) for s in used.steps
                    ],
                )
        return NetworkState(
            memory=self.shared_memory.render(),
            elapsed=time.perf_counter() - self._started if self._started else 0.0,
            input_tokens=sum(a.input_tokens for a in agents.values()),
            output_tokens=sum(a.output_tokens for a in agents.values()),
            cached_tokens=sum(a.cached_tokens for a in agents.values()),
            cache_write_tokens=sum(a.cache_write_tokens for a in agents.values()),
            agents=agents,
        )

    def _count(self, e: GeneratedEvent) -> None:
        """生成1回分を、そのエージェントの消費とステップの時間へ積む。"""
        used = self._usage.setdefault(e.agent, AgentState())
        used.input_tokens += e.input_tokens
        used.output_tokens += e.output_tokens
        used.cached_tokens += e.cached_tokens
        used.cache_write_tokens += e.cache_write_tokens
        used.seconds += e.elapsed
        # 同じ起動の同じステップなら同じ行へ積む。委譲で2回起動されると
        # ステップの番号が1から振り直されるので、起動（run_id）でも分ける。
        if not used.steps or (used.steps[-1].run_id, used.steps[-1].step_no) != (
            e.run_id,
            e.step_no,
        ):
            used.steps.append(StepTime(run_id=e.run_id, step_no=e.step_no))
        step = used.steps[-1]
        step.seconds[e.phase] = step.seconds.get(e.phase, 0.0) + e.elapsed

    def get(self, name: str) -> Agent:
        if name not in self.agents:
            raise KeyError(f"存在しないagentです: {name}")
        return self.agents[name]

    def __getitem__(self, name: str) -> Agent:
        return self.get(name)

    def total_tokens(self) -> tuple[int, int]:
        """
        セッション全体の入力・出力トークン数。

        各Agentは自分の消費分だけを数えるため、単純な合計で二重計上が起きない。
        監査役を有効にしていれば、その消費も含む。
        """
        usage = self.token_usage().values()
        return (
            sum(input_tokens for input_tokens, _ in usage),
            sum(output_tokens for _, output_tokens in usage),
        )

    def token_usage(self) -> dict[str, tuple[int, int]]:
        """
        Agentごとのトークン消費の内訳。どのAgentが重いかを見るためのもの。
        監査役を有効にしていれば、その消費は auditor として入る。
        """
        usage = {
            name: (a.total_input_tokens, a.total_output_tokens)
            for name, a in self.agents.items()
        }
        if self._auditor is not None:
            usage[AUDITOR_NAME] = (
                self._auditor.total_input_tokens,
                self._auditor.total_output_tokens,
            )
        return usage


# ---- イベントに付ける状態の積み上げを、保存できる形にする ----
# 記憶（AgentState.memory）は保存しない。イベントを配る時に、その時点の記憶から作り直す。
def _dump_usage(used: AgentState) -> dict:
    return {
        "input_tokens": used.input_tokens,
        "output_tokens": used.output_tokens,
        "cached_tokens": used.cached_tokens,
        "cache_write_tokens": used.cache_write_tokens,
        "seconds": used.seconds,
        "steps": [
            {
                "run_id": s.run_id,
                "step_no": s.step_no,
                "seconds": {phase.value: sec for phase, sec in s.seconds.items()},
            }
            for s in used.steps
        ],
    }


def _load_usage(data: dict) -> AgentState:
    return AgentState(
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        cached_tokens=data["cached_tokens"],
        cache_write_tokens=data["cache_write_tokens"],
        seconds=data["seconds"],
        steps=[
            StepTime(
                run_id=s["run_id"],
                step_no=s["step_no"],
                seconds={Phase(p): sec for p, sec in s["seconds"].items()},
            )
            for s in data["steps"]
        ],
    )

""" / L2b：把批次接到**会话那条唯一的写口**上（纯模型，注入式）。

`live_test_batch` 管"状态该长什么样"，这个模块管"为了让状态往前走，该写下哪条命令、
收到的那一行算谁的、什么时候该发取消"。三件事都不自己做，全部注入：

* `write` —— **唯一的写口**（L2b 的装配处给的是 `ObserveSession._write_tx` 那条路；
  本模块自己不持有 transport、不 import 观察会话 ⇒ "不创建第二个 owner" 是结构性的）；
* `protocol` —— 项目包的测试协议，**复用既有 `project_package.TestProtocolContract` 的形状**
  （`run_command(身份, case_id, version, parameters)` / `cancel_command(身份)` / `parse_line`），
  把 `execution_id` 当"身份"传进去。**没有第二套协议接口**；
* `emit` —— 发快照的出口（事件只从一处发布；本模块不写证据文件，落盘是 L3 的事）。

两条与 run 那条路**故意不同**的地方（都是需求决定的，不是偷懒）：

1. **不问设备要清单**。run 那条路上 `LIST` 校验的是"设备自己登记了哪些用例"；
   实时观察的目录来自**项目包声明**（L1b 已经按会话算过一遍），再问一遍设备就会
   出现"两个目录"。
2. **命令里的身份是 `execution_id`**（需求 §4：每次启动生成新的 id，绑定当前
   `session_id` 与用例定义快照）。设备事件的归属按回包里的身份字段判定；
   **回包没带身份就不猜**（如实记进 `foreign`，不是"大概是这条吧"）。

阶段怎么读也是**项目包的事**：协议可以提供 `stage_event(event) -> (stage_id, status, detail)`；
没提供就不上阶段卡（与 `step_lifecycle.steps_from_exchanges` 同一口径：没有阶段声明的照旧执行）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

try:
    from .core_primitives import utc_now
    from .live_test_batch import LiveTestBatch
    from .live_test_host import HOST_CASE_KIND, HostCase  # noqa: F401  （形状与常量）
except ImportError:  # 直跑脚本时的退化路径（与同层其它模块一致）
    from core_primitives import utc_now  # type: ignore[no-redef]
    from live_test_batch import LiveTestBatch  # type: ignore[no-redef]
    from live_test_host import HOST_CASE_KIND, HostCase  # type: ignore[no-redef]  # noqa: F401


#: 设备能报的终局（**与 `embedded_test_runner.TERMINAL_EVENTS` 同一套词**）。
#: `SKIPPED` 不在里面：那是"这一步没轮到"，由主机侧决定，设备不会报它。
DEVICE_TERMINAL_EVENTS = frozenset(("PASS", "FAIL", "ERROR", "CANCELLED"))


class LiveTestLinkError(ValueError):
    """接线写错了（没给协议、命令没写出去、重复开始）—— 当场报出来。"""


@dataclass(frozen=True)
class CommandRecord:
    """写出去的一条命令（证据与判据都按它看：谁的命令、哪条执行、写给谁）。

    `kind`：`start` / `cancel`（设备用例的规矩）与 `host`（宿主用例自己发的，
    比如那条又一次身份查询 —— 它没有对应的设备侧命令字）。
    """

    execution_id: str
    kind: str           # "start" / "cancel"
    line: str
    utc: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, "kind": self.kind, "line": self.line,
                "utc": self.utc or None}


@dataclass(frozen=True)
class IgnoredLine:
    """没归到本批次身上的那一行（如实记下来，不静默丢）。"""

    reason: str
    detail: str
    line: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail, "line": self.line}


class LiveTestLink:
    """批次 ↔ 协议 ↔ 写口 之间的那一层（纯状态推进，无 I/O）。"""

    def __init__(
        self,
        batch: LiveTestBatch,
        protocol: Any,
        *,
        write: Callable[[str], None],
        emit: Callable[[dict[str, Any]], None] | None = None,
        line_anchors: Callable[[], Mapping[str, int]] | None = None,
        identity_probe: Callable[[], Mapping[str, Any]] | None = None,
        expected_facts: Callable[[], Mapping[str, Any]] | None = None,
        probe_identity: Callable[[], Mapping[str, Any]] | None = None,
        build_known: Callable[[Any], Any] | None = None,
        now: Callable[[], float] = time.monotonic,
        utc: Callable[[], str] = utc_now,
        warmup_s: float = 0.0,
    ) -> None:
        if protocol is None:
            raise LiveTestLinkError("实时观察这条路上必须给一个测试协议（项目包提供）")
        for name in ("run_command", "cancel_command", "parse_line"):
            if not callable(getattr(protocol, name, None)):
                raise LiveTestLinkError(f"测试协议缺少 {name}（形状见 TestProtocolContract）")
        self.batch = batch
        self.protocol = protocol
        self.commands: list[CommandRecord] = []
        self.foreign: list[IgnoredLine] = []
        self.notes: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        #: 宿主用例（L8）正在跑的运行器与它的执行计划（没有就是 `None`）。
        self._host: Any = None
        self._host_plan: Any = None
        self._identity_probe = identity_probe
        self._expected_facts = expected_facts
        self._probe_identity = probe_identity
        self._build_known = build_known
        #: 不是本协议的行（普通日志）的条数。**只计数不存文本**：
        #: 这一行在 `raw.bin` / `io_events.jsonl` 里本来就有完整一份，再抄一遍只会让
        #: 冻结块被噪音撞大（真实设备每秒都在打无关的一行）。
        self.skipped = 0
        self._write = write
        self._emit = emit
        self._line_anchors = line_anchors
        self._now = now
        self._utc = utc
        self.warmup_s = float(warmup_s)

    # ------------------------------------------------------------------ 出口
    def _publish(self, *snapshots: dict[str, Any]) -> list[dict[str, Any]]:
        """把快照交给调用方（本模块不碰证据文件：事件只从一处发布）。"""
        for snapshot in snapshots:
            self.published.append(snapshot)
            if self._emit is not None:
                self._emit(snapshot)
        return list(snapshots)

    def _record_command(self, execution_id: str, kind: str, line: str) -> CommandRecord:
        """记一条**已经**写出去的命令（写不是这里做的：命令一律由唯一的写口发）。

        宿主用例那一条就是这个情形：命令是会话的 `probe_identity()` 写出去的
        （同一个写口、`source=live_test`），链路这边只补上“这一批发过它”这笔账。
        """
        record = CommandRecord(execution_id=execution_id, kind=kind, line=str(line),
                               utc=self._utc())
        self.commands.append(record)
        return record

    def _send(self, execution_id: str, kind: str, line: str) -> CommandRecord:
        self._write(line)
        return self._record_command(execution_id, kind, line)

    # ------------------------------------------------------------- 推进
    def start(self) -> list[dict[str, Any]]:
        """开始整批：推进第一条，并把它的启动命令写出去（宿主用例则让项目包去跑）。"""
        row = self.batch.start()
        execution_id = str(row["execution_id"])
        plan = self.batch.get(execution_id).plan
        published = self._publish(row)
        if self._is_host(plan):
            return published + self._begin_host(plan)
        self._send(
            execution_id, "start",
            self.protocol.run_command(execution_id, plan.case_id, plan.version,
                                      dict(plan.parameters)),
        )
        return published

    # ------------------------------------------------------------ 宿主用例（L8）
    def _is_host(self, plan: Any) -> bool:
        """这一条执行是**宿主用例**吗（`TestCase.kind`）—— 判据在项目包，这里只认种类。"""
        return str(getattr(plan.case, "kind", "device") or "device") == HOST_CASE_KIND

    def _host_case_for(self, case_id: str) -> Any:
        """项目包对这条宿主用例的执行声明（没声明就当场拒绝，不假装开跑）。"""
        reader = getattr(self.protocol, "host_case", None)
        case = reader(str(case_id)) if callable(reader) else None
        if not isinstance(case, HostCase):
            raise LiveTestLinkError(
                f"用例 {case_id} 是宿主用例，而这一路的项目包没有给它的执行器（`host_case`）"
            )
        return case

    def _begin_host(self, plan: Any) -> list[dict[str, Any]]:
        """让宿主用例自己开跑：它只拿得到 `HostCaseContext`，串口与状态机都在后面。"""
        case = self._host_case_for(plan.case_id)
        runner = case.runner()
        self._host, self._host_plan = runner, plan
        ctx = self._context_for(plan)
        try:
            runner.begin(ctx)
        except Exception as error:  # 开跑就失败也要收口：不许留一条永久 RUNNING
            self._fail_host(ctx, error)
        return ctx.emitted

    def feed(self, line: str) -> list[dict[str, Any]]:
        """设备回来的**一行**：算清楚它属于谁、是什么，再推进批次。"""
        if self._host is not None:
            return self._feed_host(str(line))
        event = self.protocol.parse_line(line)
        if not isinstance(event, Mapping):
            if self._owns(line):
                # 协议说"这是我的行"，却又没解出事件 —— 那是协议实现与声明不一致，
                # 值得记下来（与"这一行根本不是我的"是两件事）。
                self.foreign.append(IgnoredLine(
                    "unparsed", "协议认领了这一行，却没解出事件", str(line)))
            else:
                self.skipped += 1
            return []
        owner = str(event.get("run_id") or "")
        if not owner:
            # ⚠️ 不猜：回包里没有执行身份就不往任何一条上记（猜错会把 A 的结果写成 B 的）。
            self.foreign.append(IgnoredLine(
                "no_identity", "回包里没有执行身份（命令里带了，回包却没带回来）", str(line)))
            return []
        if not self._belongs(owner):
            self.foreign.append(IgnoredLine(
                "other_execution", f"回包属于 {owner}，不是这一批里的执行", str(line)))
            return []
        kind = str(event.get("event") or "")
        if kind in DEVICE_TERMINAL_EVENTS:
            snapshot = self.batch.note_result(
                owner, kind,
                reason=str(event.get("reason") or event.get("detail")
                           or event.get("message") or ""),
                # 设备这条结论行就在眼前：它是"这一步直到此刻"的最后一个证据行。
                anchors=self._anchors())
            published = self._publish(snapshot)
            return published + self._drain_stage_events() + self._hand_off()
        stage = self._stage_event(event)
        if stage is not None:
            stage_id, status, detail = stage
            return self._publish(self.batch.note_step(
                owner, stage_id, status, detail=detail, anchors=self._anchors(),
            ))
        # 认得出归属、但既不是终局也不是阶段：如实留给调用方（页面可能想显示它）。
        self.notes.append(dict(event))
        return self._publish({"kind": "live_test.device", "execution_id": owner, **dict(event)})

    def _belongs(self, owner: str) -> bool:
        return any(row.execution_id == owner for row in self.batch.executions)

    def _anchors(self) -> dict[str, int]:
        """**这一行**在各来源里各是第几行（阶段坐标的中心锚点就靠它）。

        ⚠️ 由调用方（观察会话）回答：行数属于会话的四个写口，链路这里只负责
        "在收到这一步的那一行上问一次"。问不到（没注入 / 报错）就给空表 ——
        于是 `anchor_line` 退回到区间末行，而不是编一个行号。
        """
        if self._line_anchors is None:
            return {}
        try:
            return {str(key): int(value) for key, value in dict(self._line_anchors()).items()}
        except Exception:
            return {}

    def _owns(self, line: str) -> bool:
        """这一行到底算不算本协议的东西（**协议回答**；没声明就按"算"处理）。

        缺省值是"算"（宁可多记，不静默丢）—— 但那在有噪音的目标上会让证据里全是
        普通日志；需要干净证据的协议自己声明 `owns_line(line)`。
        """
        reader = getattr(self.protocol, "owns_line", None)
        return True if not callable(reader) else bool(reader(line))

    def _stage_event(self, event: Mapping[str, Any]) -> tuple[str, str, str] | None:
        """阶段怎么读由**协议**回答；协议没提供这一项就不上阶段卡（不替它编）。"""
        reader = getattr(self.protocol, "stage_event", None)
        if not callable(reader):
            return None
        result = reader(dict(event))
        if isinstance(result, tuple) and len(result) == 3:
            return str(result[0]), str(result[1]), str(result[2])
        return None

    def _drain_stage_events(self) -> list[dict[str, Any]]:
        """把"被收口带着结束"的阶段快照当 `test_step` 发出去（）。

        设备只报了**执行**终局（或超时/取消/断线）时，那几个阶段自己是没有设备行触发的
        终态快照的 —— 不补这一发，服务端已经收口而屏幕上那张卡还在"进行中"，
        它在证据里的坐标也就没人看得到（需求 §6.6 / §8.4）。
        """
        rows = self.batch.take_stage_events()
        return self._publish(*rows) if rows else []

    def _hand_off(self) -> list[dict[str, Any]]:
        """一条执行收口后，如果批次推进到了下一条 ⇒ 立刻把它的命令写出去。

        ⚠️ 下一条是**宿主用例**时同样要立刻开跑（否则它永远停在 SELECTED）。
        """
        following = self.batch.running
        if following is None:
            return []
        plan = following.plan
        if self._is_host(plan):
            return self._begin_host(plan)
        self._send(
            following.execution_id, "start",
            self.protocol.run_command(following.execution_id, plan.case_id, plan.version,
                                      dict(plan.parameters)),
        )
        return []

    # ---------------------------------------------------------- 宿主用例的接面
    def _context_for(self, plan: Any) -> "HostCaseContext":
        """给宿主用例造一个只看得见这几个接口的现场（它拿不到串口，也拿不到批次）。"""
        return HostCaseContext(self, plan)

    def _feed_host(self, line: str) -> list[dict[str, Any]]:
        """宿主用例在跑时，这一行**归它看**：设备协议那条路不参与（它没有设备侧命令）。"""
        ctx = self._context_for(self._host_plan)
        try:
            self._host.line(line, ctx)
        except Exception as error:
            self._fail_host(ctx, error)
        return ctx.emitted

    def _fail_host(self, ctx: "HostCaseContext", error: Exception) -> None:
        """宿主用例自己抛了（写口坏了 / 判据坏了）：收口成 ERROR，不留永久 `RUNNING`。"""
        detail = f"宿主用例执行失败：{type(error).__name__}: {error}"
        self.notes.append({"kind": "host.error", "detail": detail})
        try:
            ctx.done("ERROR", detail)
        except Exception:  # 收口也失败：只能放弃（批次那条超时仍是兜底）
            self._host, self._host_plan = None, None

    # --------------------------------------------------------------- 超时
    def tick(self) -> list[dict[str, Any]]:
        """按声明的超时判一次：到点就先告诉设备停，再收口（顺序不能反）。

        ⚠️ 宿主用例（L8）自己也有一个"等自报"的期限，先让它看一眼；
        批次这一层的那条超时仍是**兜底**（用例声明的那条更大）。
        """
        published: list[dict[str, Any]] = []
        if self._host is not None:
            ctx = self._context_for(self._host_plan)
            try:
                self._host.tick(ctx)
            except Exception as error:
                self._fail_host(ctx, error)
            published = ctx.emitted
        row = self.batch.overdue(self.warmup_s)
        if row is None:
            return published
        # 宿主用例没有设备侧命令可取消（它的"命令"就是那一次身份查询）⇒ 不写它看不懂的命令。
        if not self._is_host(row.plan):
            self._send(row.execution_id, "cancel", self.protocol.cancel_command(row.execution_id))
        self._host, self._host_plan = None, None
        return published + self._publish(self.batch.timed_out()) + self._drain_stage_events()

    # ------------------------------------------------------- 取消与收口
    def cancel(self, reason: str = "使用者取消了这一批") -> list[dict[str, Any]]:
        running = self.batch.running
        if running is not None and not self._is_host(running.plan):
            self._send(running.execution_id, "cancel",
                       self.protocol.cancel_command(running.execution_id))
        self._host, self._host_plan = None, None
        snapshots = [self.batch.cancel(reason)]
        return self._publish(*snapshots) + self._drain_stage_events()

    def disconnected(self, reason: str = "连接断开了：这一步没有结论") -> list[dict[str, Any]]:
        """断线**不写命令**（口已经没了）；只把批次收口。"""
        self._host, self._host_plan = None, None
        return self._publish(self.batch.disconnected(reason)) + self._drain_stage_events()

    def session_ended(self, reason: str = "观察会话结束了：这一步没有结论") -> list[dict[str, Any]]:
        self._host, self._host_plan = None, None
        return self._publish(self.batch.session_ended(reason)) + self._drain_stage_events()

    # ------------------------------------------------------------------ 读
    def to_json(self) -> dict[str, Any]:
        return {
            "batch": self.batch.to_json(),
            "commands": [row.to_json() for row in self.commands],
            "foreign": [row.to_json() for row in self.foreign],
            "notes": [dict(row) for row in self.notes],
            "skipped": int(self.skipped),
            "published": len(self.published),
        }


class HostCaseContext:
    """宿主用例运行时看得见的**全部**东西（项目包的执行器只用这几个方法）。

    ⚠️ 它一条判断都不做，只转发四件事：发命令（链路的**唯一**写口）、推进阶段与收口
    （`step_lifecycle` + `live_test_batch`，与设备用例同一套状态机）、拿事实
    （会话的身份状态视图 + 装配处声明的"当前组合"）。于是项目包那边写不出
    "第二套状态机"或"第二条写路径" —— 它连那些东西都拿不到。
    """

    def __init__(self, link: "LiveTestLink", plan: Any) -> None:
        self._link = link
        self._plan = plan
        #: 这一次调用里被发布出去的快照（调用方按它决定要不要再广播一次）。
        self.emitted: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 身份
    @property
    def execution_id(self) -> str:
        return str(self._plan.execution_id)

    @property
    def case_id(self) -> str:
        return str(self._plan.case_id)

    @property
    def version(self) -> int:
        return int(self._plan.version)

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(self._plan.parameters)

    def now(self) -> float:
        return float(self._link._now())

    def utc(self) -> str:
        return str(self._link._utc())

    # ------------------------------------------------------------ 事实
    def identity(self) -> Mapping[str, Any]:
        """这条会话的**身份状态视图**（`state` / `ok` / `fields` / `detail` / `prefix`）。

        它就是会话那台既有状态机此刻的样子 —— 宿主用例不自己解析设备行。
        """
        return self._call(self._link._identity_probe, {})

    def expected(self) -> Mapping[str, Any]:
        """当前运行组合的**声明**（设备可能自称的项目名、domain、有无本机构建产物…）。"""
        return self._call(self._link._expected_facts, {})

    def probe(self) -> Mapping[str, Any]:
        """**重新问一次身份**（复用既有查询协议，走唯一写口；会话会把上一次的结论作废）。

        ⚠️ 这条命令是**被调的**那次读出自己写出去的（会话的唯一写口），所以这里只
        **记账**（`kind="host"`）、**不再写第二遍** —— 真机验收撞到过：早先这里记完账
        又 `send()` 了一遍，设备收到了两条一模一样的查询。
        """
        answer = dict(self._call(self._link._probe_identity, {}) or {})
        line = str(answer.get("query_text") or "")
        if line:
            self._link._record_command(self.execution_id, "host", line)
        return answer

    def build_known(self, value: Any) -> bool | None:
        """本机这一份固件的构建产物里有没有这个构建号（`None` = 本机没有构建产物可比）。

        ⚠️ 三种回答要分得开：`True/False` 是核对结论，`None` 是**没得核对** ——
        后者在判据里是 ERROR，不是 FAIL（"我不知道"不能算"它错了"）。
        """
        reader = getattr(self._link, "_build_known", None)
        if not callable(reader):
            return None
        try:
            answer = reader(value)
        except Exception:
            return None
        return None if answer is None else bool(answer)

    # ------------------------------------------------------------ 推进
    def send(self, line: str) -> None:
        """写一条命令（**唯一**写口；证据里记成这条用例自己发的，`kind="host"`）。"""
        self._link._send(self.execution_id, "host", str(line))

    def stage(self, stage_id: str, status: str, detail: str = "", *,
              first_line: bool = False, event_seq: int | None = None) -> dict[str, Any]:
        """推进一个阶段（`running` 或五个终态；与设备用例同一套阶段机与证据坐标）。

        `first_line=True` = **“我刚刚读到的那一行就是这一阶段的第一行证据”**
        （设备在某一行上报了这一步 / 自报刚刚到达）—— 只有**确实如此**时才传：它把区间
        起点挪到那一行上；传错会把一条无关的行圈进这一阶段的证据里（真机上那一行是心跳）。

        `event_seq`（可选）= **我自己刚发出去的那条命令**在 `io_events.jsonl` 里的事件号：
        工具窗口是派生视图（行号不与文件对齐）⇒ 它那一行的身份就是这个事件号，
        阶段卡上落成 `{"source": "tool", "event_seq": N}`（）。只有确实知道
        （写口把这个号返回给了我）时才传 —— 不知道就不传，宁缺不编。

        进 `running` 且 `first_line=False` ⇒ 基线就是“现在这一刻”（区间从下一行开始）；
        落终态那次一律带上当行的锚点（`anchor_line` = “足以判定这一步的关键行”），
        落在区间外时由坐标那一侧自己退回区间末行，不会编一个行号。
        """
        current = self._link._anchors()
        anchors = current if (first_line or str(status) != "running") else {}
        declared = []
        if event_seq is not None:
            try:
                seq = int(event_seq)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                declared.append({"source": "tool", "event_seq": seq})
        snapshot = self._link.batch.note_step(
            self.execution_id, str(stage_id), str(status), detail=str(detail or ""),
            anchors=anchors, extra_refs=declared,
        )
        self.emitted.extend(self._link._publish(snapshot))
        return snapshot

    def done(self, result: str, reason: str = "") -> None:
        """收口这一条执行（结论只走批次），随后照既有规矩交接下一条。"""
        snapshot = self._link.batch.note_result(
            self.execution_id, str(result), reason=str(reason or ""),
            anchors=self._link._anchors(),
        )
        self._link._host, self._link._host_plan = None, None
        self.emitted.extend(self._link._publish(snapshot))
        self.emitted.extend(self._link._drain_stage_events())
        self.emitted.extend(self._link._hand_off())

    # ------------------------------------------------------------ 内部
    def _call(self, reader: Any, fallback: Any) -> Any:
        """调一个注入的读出；没注入 / 抛错都给回退值（**不猜**一个事实出来）。"""
        if not callable(reader):
            return fallback
        try:
            answer = reader()
        except Exception:
            return fallback
        return answer if answer is not None else fallback


def evidence_block(link: "LiveTestLink") -> dict[str, Any]:
    """这一批的**冻结快照**（会写进会话 manifest）：定义 + 结果 + 命令。

    ⚠️ 为什么连**定义**一起冻结：用例目录会随项目包改版，而回放旧会话时必须
    按**当时那一版**解释（需求 §7：catalog 改版不影响旧会话）。阶段声明也一并进块
     —— 于是"当时阶段卡上写了什么"以后也改不了。
    """
    snapshot = link.batch.to_json()
    return {
        "schema": 1,
        "batch_id": link.batch.batch_id,
        "session_id": link.batch.session_id,
        "stop_on_failure": bool(link.batch.stop_on_failure),
        "status": snapshot["status"],
        "result": snapshot["result"],
        "reason_code": snapshot["reason_code"],
        "reason": snapshot["reason"],
        # 冻结的定义（回放只认这一份，不去查当前目录）。
        "definitions": [
            {
                **row.plan.to_json(),
                "label": str(getattr(row.plan.case, "label", "") or ""),
                "steps": [
                    {"stage_id": step.stage_id, "label": step.label,
                     "expected": step.expected, "version": step.version,
                     "timeout_s": step.timeout_s}
                    for step in row.plan.steps
                ],
            }
            for row in link.batch.executions
        ],
        "executions": snapshot["executions"],
        "summary": snapshot["summary"],
        "commands": [row.to_json() for row in link.commands],
        "late_events": snapshot["late_events"],
        "foreign": [row.to_json() for row in link.foreign],
        # 不是本协议的行（普通日志）的条数：只记数、不抄文本（raw.bin 里有一份）。
        "skipped_lines": int(link.skipped),
    }


__all__ = [
    "DEVICE_TERMINAL_EVENTS", "CommandRecord", "HostCaseContext", "IgnoredLine",
    "LiveTestLink", "LiveTestLinkError", "evidence_block",
]

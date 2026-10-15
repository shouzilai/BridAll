""" / L2a：实时观察里**一次受控测试批次**的生命周期（纯模型）。

需求 §4 把这件事写得很具体，这个模块只做那几件事，一行 I/O 都没有
（不发命令、不读行、不开串口、不认识任何目标或项目包）：

```text
SELECTED -> STARTING -> RUNNING -> PASS / FAIL / ERROR / SKIPPED / CANCELLED
```

* 终态不可逆，**迟到响应不得改写终态**（`step_lifecycle.advance` 立的那条规矩）；
* 第一版按选择顺序**串行**执行（同一时刻只有一条在执行）；
* 前一条失败后**默认停掉剩余**并写明 `SKIPPED` 原因；
* 取消、断线、结束观察、超时都必须收敛到明确终态（不留永久 `RUNNING`）；
* 每条用例的阶段卡片仍归 `step_lifecycle.TestStepRecorder` —— 这里不另建一套阶段状态机。

⚠️ **发命令与读行不在这里**（那是 L2b：接进 `ObserveSession._write_tx` 那条**唯一**写口）。
于是"不创建第二个 owner"在这层是结构性的：本模块连 transport 都拿不到，它只会
"把状态推进到该有的样子"并把快照交给调用方去发事件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

try:
    from .core_primitives import utc_now
    from .embedded_test_model import RESULT_STATUSES
    from .step_lifecycle import (
        TestStep,
        TestStepError,
        TestStepRecorder,
        declared_refs,
        is_terminal,
    )
except ImportError:  # 直跑脚本时的退化路径（与同层其它模块一致）
    from core_primitives import utc_now  # type: ignore[no-redef]
    from embedded_test_model import RESULT_STATUSES  # type: ignore[no-redef]
    from step_lifecycle import (  # type: ignore[no-redef]
        TestStep,
        TestStepError,
        TestStepRecorder,
        declared_refs,
        is_terminal,
    )


#: 终态就是**既有**结果枚举（`PASS/FAIL/ERROR/SKIPPED/CANCELLED`），一个都不新增。
TERMINAL = tuple(RESULT_STATUSES)

#: 一条执行的生命周期（需求 §4 那张图的顺序）。
EXECUTION_STATUSES: tuple[str, ...] = ("SELECTED", "STARTING", "RUNNING") + TERMINAL

#: 批次自己只有三态：还没开始 / 正在跑 / 已收口（已收口的那一态就是结果）。
BATCH_IDLE = "IDLE"
BATCH_RUNNING = "RUNNING"

#: 收敛原因码（判据按它对拍；新增只增不改）。
CONVERGE_CODES = (
    "previous_failed",   # 前一条没通过，按需求 §4 停掉剩余
    "cancelled",         # 使用者取消
    "disconnected",      # 连接断了
    "session_ended",     # 观察会话结束
    "timed_out",         # 超过这条用例声明的超时
    "error",             # 其它执行错误
)


class LiveTestBatchError(ValueError):
    """批次状态写错了（重复开始、给错用例、越权推进）—— 当场报出来，不静默改正。"""


@dataclass(frozen=True)
class BatchPlan:
    """一条**已经过启动校验**的用例：定义快照 + 参数 + 项目包声明的阶段。

    `execution_id` 由服务端在启动那一刻生成（需求 §4：每次启动生成新的 id，
    绑定当前 `session_id` 与用例定义快照）—— 这里只负责拿着它，不自己造。
    """

    execution_id: str
    case: Any
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: 项目包声明的阶段（可为空：没声明阶段的用例照旧执行，只是不上阶段卡）。
    steps: tuple[TestStep, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.execution_id or ""):
            raise LiveTestBatchError("执行必须有 execution_id")
        if getattr(self.case, "case_id", None) is None:
            raise LiveTestBatchError("执行必须带上用例定义快照")

    @property
    def case_id(self) -> str:
        return str(self.case.case_id)

    @property
    def version(self) -> int:
        return int(self.case.version)

    @property
    def timeout_s(self) -> float:
        """这条用例声明的超时（秒）；0 = 不限（由执行处按声明判定）。"""
        return float(getattr(self.case, "timeout_s", 0.0) or 0.0)

    def to_json(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "case_id": self.case_id,
            "version": self.version,
            "timeout_s": self.timeout_s,
            "parameters": dict(self.parameters),
        }


@dataclass
class ExecutionState:
    """一条执行的运行期状态（含它自己的阶段表）。"""

    plan: BatchPlan
    status: str = "SELECTED"
    reason: str = ""
    started_mono: float | None = None
    ended_mono: float | None = None
    started_utc: str = ""
    ended_utc: str = ""
    steps: TestStepRecorder = field(init=False)

    def __post_init__(self) -> None:
        self.steps = TestStepRecorder(tuple(self.plan.steps))

    @property
    def execution_id(self) -> str:
        return self.plan.execution_id

    @property
    def finished(self) -> bool:
        return is_terminal(self.status)

    def duration_ms(self, now_mono: float) -> int:
        if self.started_mono is None:
            return 0
        end = self.ended_mono if self.ended_mono is not None else now_mono
        return max(0, int(round((end - self.started_mono) * 1000)))

    def to_json(self, now_mono: float) -> dict[str, Any]:
        return {
            **self.plan.to_json(),
            "status": self.status,
            "reason": self.reason,
            "started_utc": self.started_utc or None,
            "ended_utc": self.ended_utc or None,
            "duration_ms": self.duration_ms(now_mono),
            "steps": self.steps.snapshots(now_mono),
        }


class LiveTestBatch:
    """一次受控批次：串行推进、一个失败就停、任何异常都收敛。

    调用方（L2b）的顺序是：`start()` → 按设备回包 `note_step()`/`note_result()`，
    或者在任何时刻 `cancel()`/`disconnected()`/`session_ended()`/`timed_out()`。
    每个方法都返回**该发的快照**（本模块不碰事件总线：事件只从一处发布）。
    """

    def __init__(
        self,
        batch_id: str,
        session_id: str,
        plans: Iterable[BatchPlan],
        *,
        stop_on_failure: bool = True,
        evidence: Any | None = None,
        now: Callable[[], float] = time.monotonic,
        utc: Callable[[], str] = utc_now,
    ) -> None:
        """`evidence` = 阶段证据坐标的记账号（可注入；没有就是**不给坐标**）。

        它只要两个方法（鸭子类型，与 `step_lifecycle.StageEvidence` 同形）：
        `open(key)`（阶段进 `running`：记下开始前的基线）与 `refs(key, anchors=…)`
        （阶段落终态：算出区间与中心锚点）。**本模块仍然一行 I/O 都没有** ——
        行数是那个对象自己从写口问来的。
        """
        if not str(batch_id or ""):
            raise LiveTestBatchError("批次必须有 batch_id")
        if not str(session_id or ""):
            raise LiveTestBatchError("批次必须绑定一个观察会话")
        rows = tuple(plans)
        if not rows:
            raise LiveTestBatchError("空批次：至少要选一条用例（没有要执行的东西就别开批次）")
        seen: set[str] = set()
        for plan in rows:
            if plan.execution_id in seen:
                raise LiveTestBatchError(f"execution_id 重复：{plan.execution_id!r}")
            seen.add(plan.execution_id)
        self.batch_id = str(batch_id)
        self.session_id = str(session_id)
        self.stop_on_failure = bool(stop_on_failure)
        self.executions: list[ExecutionState] = [ExecutionState(plan=plan) for plan in rows]
        self.status = BATCH_IDLE
        self.result = ""
        self.reason = ""
        self.reason_code = ""
        self.started_mono: float | None = None
        self.ended_mono: float | None = None
        self._started_utc = ""
        self._ended_utc = ""
        #: 终态之后又来的事件（迟到的设备回包）。**如实记下来**，但不改写任何状态。
        self.late_events: list[dict[str, Any]] = []
        #: 被"执行/批次收口"带着结束的阶段快照（`take_stage_events()` 取走）。
        #: ⚠️ 这些阶段**没有自己的设备行**（设备只报了执行终局，或者超时/取消/断线），
        #: 所以发布方要把它们当 `test_step` 补发出去 —— 不发的话服务端已经收口，
        #: 页面上那张卡却一直"进行中"（需求 §6.6：不留永久 running）。
        self._stage_events: list[dict[str, Any]] = []
        self._evidence = evidence
        self._now = now
        self._utc = utc

    def take_stage_events(self) -> list[dict[str, Any]]:
        """把"被收口带着结束"的阶段快照交出去（取走即清空）。"""
        rows, self._stage_events = self._stage_events, []
        return rows

    # ------------------------------------------------------------------ 读
    @property
    def running(self) -> ExecutionState | None:
        """正在跑的那一条（串行 ⇒ 最多一条）。"""
        return next((row for row in self.executions if row.status == "RUNNING"), None)

    @property
    def selected(self) -> ExecutionState | None:
        """下一条要跑的（`SELECTED` 或已经 `STARTING`）。"""
        return next(
            (row for row in self.executions if row.status in ("SELECTED", "STARTING")), None
        )

    def get(self, execution_id: str) -> ExecutionState:
        for row in self.executions:
            if row.execution_id == execution_id:
                return row
        raise LiveTestBatchError(f"这个批次里没有执行 {execution_id!r}")

    def overdue(self, warmup_s: float = 0.0) -> ExecutionState | None:
        """有没有哪一条**超过了它自己声明的超时**（0 = 不限，永远不超）。

        ⚠️ 判定用"这条用例声明的 `timeout_s`"（需求 §4/§6.6），不是平台统一给的数。
        `warmup_s` 是给调用方留的一点余量（例如等设备接受命令的那一小段）。
        """
        row = self.running
        if row is None or row.started_mono is None:
            return None
        budget = row.plan.timeout_s
        if budget <= 0:
            return None
        return row if (self._now() - row.started_mono) > budget + max(0.0, warmup_s) else None

    def to_json(self) -> dict[str, Any]:
        now_mono = self._now()
        return {
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "status": self.status,
            "result": self.result,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "stop_on_failure": self.stop_on_failure,
            "started_utc": None if self.started_mono is None else self._started_utc,
            "ended_utc": None if self.ended_mono is None else self._ended_utc,
            "executions": [row.to_json(now_mono) for row in self.executions],
            "late_events": [dict(row) for row in self.late_events],
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        """按终态口径汇总（与既有 `test_summary` 同一套词，页面可复用）。"""
        counts: dict[str, int] = {}
        for row in self.executions:
            counts[row.status] = counts.get(row.status, 0) + 1
        finished = [row for row in self.executions if row.finished]
        return {
            "total": len(self.executions),
            "counts": counts,
            "finished": len(finished),
            "passed": sum(row.status == "PASS" for row in self.executions),
        }

    # --------------------------------------------------------------- 推进
    def _set(self, row: ExecutionState, status: str, *, reason: str = "") -> None:
        """一次状态转换（终态不可回退由这里兜住）。"""
        if row.finished:
            raise LiveTestBatchError(
                f"执行 {row.execution_id} 已经是终态 {row.status}：迟到的更新不能改写它"
            )
        row.status = status
        if reason:
            row.reason = reason
        if status in ("STARTING", "RUNNING") and row.started_mono is None:
            row.started_mono = self._now()
            row.started_utc = self._utc()
        if is_terminal(status):
            row.ended_mono = self._now()
            row.ended_utc = self._utc()

    def start(self) -> dict[str, Any]:
        """整批开始：第一条进入 `STARTING`（服务端校验已经过了，见 `verify_start`）。"""
        if self.status != BATCH_IDLE:
            raise LiveTestBatchError(
                f"这个批次已经在 {self.status}（一次只允许一个受控批次）"
            )
        self.status = BATCH_RUNNING
        self.started_mono = self._now()
        self._started_utc = self._utc()
        row = self.selected
        if row is None:  # 不可能：构造时保证非空且都是 SELECTED
            raise LiveTestBatchError("批次里没有可开始的执行")
        self._set(row, "STARTING")
        self._set(row, "RUNNING")
        return row.to_json(self._now())

    def note_step(
        self,
        execution_id: str,
        stage_id: str,
        status: str,
        *,
        detail: str = "",
        anchors: Mapping[str, int] | None = None,
        extra_refs: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """推进一个阶段（`status` 只接受 `running` 或五个终态之一）。

        证据坐标（）：进 `running` 时记下基线，落终态时把这个阶段在
        raw / decoded / app_events 里占的行号区间写进阶段状态（`evidence_refs`），
        并把 `anchors` 里那一行当**中心锚点** —— 调用方量出来的"设备报了这一步的那一行"
        （行号在各来源里各算各的；不在区间里的锚点由 `refs()` 自己退回区间末行）。

        `extra_refs`（可选）= 调用方**自己知道的那一条坐标**，与上面自动算出来的合并。
        目前唯一的用法是工具窗口：「发起身份查询」那一步的 TX 行不在任何行号空间里，
        它的身份是那条 io 事件的**事件号**（`{"source": "tool", "event_seq": N}`，
        ）；没给就什么也不加（不编一个）。
        """
        row = self.get(execution_id)
        if row.finished:
            return self._late("step", row, {"stage_id": stage_id, "status": status,
                                            "detail": detail})
        key = self._stage_key(execution_id, stage_id)
        declared = declared_refs(extra_refs)
        if status == "running":
            self._open_stage(key, anchors)
            return row.steps.start(stage_id, detail=detail, mono=self._now(), utc=self._utc(),
                                   refs=declared)
        refs = self._stage_refs(key, anchors)
        if declared:
            # 同一个来源只留一条（声明的那一条更具体）—— 与 `merge_refs` 同一条不变量：
            # 一个来源一行坐标，免得同一类在卡上出现两个互相矛盾的区间。
            taken = {row.get("source") for row in declared}
            refs = [row for row in refs if row.get("source") not in taken] + declared
        return row.steps.finish(stage_id, status, detail=detail, mono=self._now(),
                                utc=self._utc(), refs=refs)

    @staticmethod
    def _stage_key(execution_id: str, stage_id: str) -> str:
        """阶段证据基线的键：**执行 + 阶段**（同一个 stage_id 在同一次会话里会重复出现）。"""
        return f"{execution_id}:{stage_id}"

    def _open_stage(self, key: str, anchors: Mapping[str, int] | None = None) -> None:
        """记下这一阶段开始前的证据基线（记账号缺失 / 报错都不影响阶段本身）。

        `anchors` = 设备报"这一步开始"的那一行在各来源里各是第几行 —— 那一行本身就是
        这一步的第一行证据（区间不能从它的**下一行**开始，否则定位整体差一行）。
        """
        if self._evidence is None:
            return
        try:
            self._evidence.open(key, first=dict(anchors or {}))
        except Exception:  # 记不下来就不给坐标（绝不能把阶段推进拉坏）
            pass

    def _stage_refs(
        self, key: str, anchors: Mapping[str, int] | None
    ) -> list[dict[str, Any]]:
        """算出这一阶段的证据坐标；记账号缺失 / 报错 ⇒ 空表（退回到“没有定位信息”）。"""
        if self._evidence is None:
            return []
        try:
            return list(self._evidence.refs(key, anchors=dict(anchors or {})))
        except Exception:
            return []

    def _late(self, kind: str, row: ExecutionState, payload: dict[str, Any]) -> dict[str, Any]:
        """终态之后到的东西：记下来、不改状态（需求 §4：迟到响应不得改写终态）。"""
        record = {"kind": kind, "execution_id": row.execution_id, "kept_status": row.status,
                  **payload}
        self.late_events.append(record)
        return {**record, "late": True}

    def _late_batch(self, code: str, reason: str) -> dict[str, Any]:
        """整批已经收口之后又来的收口请求（重复取消、收口后断线）。

        ⚠️ 这种记录**不借用**某条执行的状态：批次自己已经定了结论，记的就是它。
        """
        record = {"kind": "converge", "scope": "batch", "batch_id": self.batch_id,
                  "kept_status": self.status, "code": code, "reason": reason}
        self.late_events.append(record)
        return {**record, "late": True}

    def note_result(
        self,
        execution_id: str,
        status: str,
        *,
        reason: str = "",
        anchors: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """收到一条执行的终局结论；必要时推进到下一条（或收口整批）。

        `anchors` = 设备那条结论行在各来源里各是第几行：正在跑的那个阶段被这一步拖停，
        它直到这一刻写下的证据就是它的区间（中心锚点就是这一行）。
        """
        if status not in TERMINAL:
            raise LiveTestBatchError(
                f"执行结论只能是 {list(TERMINAL)}，收到 {status!r}（运行态走 start/note_step）"
            )
        row = self.get(execution_id)
        if row.finished:
            return self._late("result", row, {"status": status, "reason": reason})
        if self.running is not None and self.running.execution_id != execution_id:
            raise LiveTestBatchError(
                f"执行 {execution_id} 不在跑（正在跑的是 {self.running.execution_id}）："
                "串行执行时一次只有一条在执行"
            )
        self._set(row, status, reason=reason)
        # 阶段跟着这条执行的结论收口：跑着的那一个拿它的终态（连带这一刻的证据坐标），
        # 还没轮到的记 SKIPPED（一行证据都没写过 ⇒ 不给坐标）。
        self._stage_events.extend(row.steps.converge(
            detail=reason or f"执行结束（{status}）：这一阶段没有结论",
            mono=self._now(), utc=self._utc(), terminal=status,
            refs_for=lambda stage_id: self._stage_refs(
                self._stage_key(execution_id, stage_id), anchors
            ),
        ))
        self._after(row)
        return row.to_json(self._now())

    def _after(self, row: ExecutionState) -> None:
        """一条执行收口之后：要么推进下一条，要么把剩余停掉，要么收口整批。"""
        if row.status != "PASS" and self.stop_on_failure:
            self._skip_rest(
                "previous_failed",
                f"前一条用例 {row.plan.case_id} 是 {row.status}"
                f"（{row.reason or '没有给出原因'}）：按需求 §4 停掉剩余用例",
            )
        elif self.selected is not None:
            following = self.selected
            self._set(following, "STARTING")
            self._set(following, "RUNNING")
        self._settle()

    def _skip_rest(self, code: str, reason: str) -> None:
        for row in self.executions:
            if row.finished or row.status == "RUNNING":
                continue
            row.status = "SKIPPED"
            row.reason = reason
            row.ended_mono = self._now()
            row.ended_utc = self._utc()
            self._stage_events.extend(row.steps.converge(
                detail=reason, mono=self._now(), utc=self._utc(), terminal="SKIPPED"
            ))
        self.reason = reason
        self.reason_code = code

    def _settle(self) -> None:
        if any(not row.finished for row in self.executions):
            return
        self.ended_mono = self._now()
        self._ended_utc = self._utc()
        self.status = "PASS" if all(row.status == "PASS" for row in self.executions) else next(
            (row.status for row in self.executions if row.status != "PASS"), "ERROR"
        )
        self.result = self.status

    # ------------------------------------------------------- 四种收口入口
    def _converge_all(self, code: str, reason: str, *, terminal: str) -> dict[str, Any]:
        """把还在跑的那条收成 `terminal`、剩下的收成 `SKIPPED`，并收口整批。"""
        if is_terminal(self.status):
            return self._late_batch(code, reason)
        row = self.running
        if row is None:
            row = self.selected
        if row is not None:
            execution_id = row.execution_id
            self._set(row, terminal, reason=reason)
            # 这几种收口（超时/取消/断线/结束观察）**没有设备行** ⇒ 锚点为空，
            # 区间仍然是"跑到哪算到哪"，中心行由 `refs()` 退回区间末行。
            self._stage_events.extend(row.steps.converge(
                detail=reason, mono=self._now(), utc=self._utc(), terminal=terminal,
                refs_for=lambda stage_id: self._stage_refs(
                    self._stage_key(execution_id, stage_id), None
                ),
            ))
        self._skip_rest(code, reason)
        self._settle()
        return self.to_json()

    def cancel(self, reason: str = "使用者取消了这一批") -> dict[str, Any]:
        """取消：正在跑的那条是 `CANCELLED`（**不是** `ERROR` —— 用户取消与执行出错是两件事）。"""
        return self._converge_all("cancelled", reason, terminal="CANCELLED")

    def disconnected(self, reason: str = "连接断开了：这一步没有结论") -> dict[str, Any]:
        """断线：正在跑的那条 `ERROR`（跑到一半没有下文，那是执行错误，不是"没跑"）。"""
        return self._converge_all("disconnected", reason, terminal="ERROR")

    def session_ended(self, reason: str = "观察会话结束了：这一步没有结论") -> dict[str, Any]:
        """结束观察：与断线同一种收口，**理由分开报**（回看时才分得清发生了什么）。"""
        return self._converge_all("session_ended", reason, terminal="ERROR")

    def timed_out(self, reason: str = "") -> dict[str, Any]:
        """超时：理由里带上这条用例声明的上限（谁的超时、超了多久）。"""
        row = self.running
        text = reason or (
            f"用例 {row.plan.case_id} 超过它声明的超时 {row.plan.timeout_s:g}s"
            if row is not None else "这一批超过声明的超时"
        )
        return self._converge_all("timed_out", text, terminal="ERROR")


__all__ = [
    "BATCH_IDLE", "BATCH_RUNNING", "CONVERGE_CODES", "EXECUTION_STATUSES", "TERMINAL",
    "BatchPlan", "ExecutionState", "LiveTestBatch", "LiveTestBatchError", "TestStepError",
]

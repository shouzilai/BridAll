"""统一操作入口（**core**）：一份操作服务，多处调用。

```text
PREFLIGHT -> BUILD -> FLASH -> VERIFY_IDENTITY -> DEBUG_CHECK -> PASS
                             -> KNOWN_FAILURE -> RETRY_ONCE / NEEDS_HUMAN_ACTION
```

需求 §3 要求 CLI、Pipeline 与（未来的）Web/MCP 调**同一份**操作服务。这个模块就是那
一份：它只做三件事 —— **什么时候做**、**把结果记成证据**、**失败时说什么**。

它拿到的每一样东西都是**交进来**的：

| 角色 | 谁提供 | core 知道什么 |
| --- | --- | --- |
| 契约（步骤、判据、故障、能力） | `hwops_contract.HardwareDeclaration` | 只有形状 |
| 路径与工具 | 注入的 `Resolver`（`RecipeResolver` 是参考实现） | 只按占位符展开 |
| 跑命令 | 注入的 `executor` | 只看退出码、输出、耗时 |
| 串口 / 身份 | 注入的 `debug_probe` / `identity_probe` | 只按声明的字段核对 |
| 证据落点 | 注入的 `evidence` 回调 | 只把一份 JSON 交出去 |

两条硬规矩：

1. **判据只有一个来源**：每一步算不算成功由 `hwops_contract.judge()` 按**该步骤声明
   的判据**判。这一层里**不许**出现"按步骤名挑判据"的写法（那条规矩的由来）。
2. **熔断在调用厂商工具之前**：同一目标/固件/故障码**连续两次**之后，第三次在
   执行器被调用**之前**就拒掉（`fuse_open`），并且说清怎么恢复（人工处置 + 显式清除）。
   失败后无判断地循环烧录正是这条规矩要防的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping

try:  # imported as part of the package (``webconsole.hwops_runner``)
    from . import hwops_contract as contract_mod
    from . import target_adapters as adapter_mod
except ImportError:  # imported as a top-level module (CLI entry points)
    import hwops_contract as contract_mod  # type: ignore[no-redef]
    import target_adapters as adapter_mod  # type: ignore[no-redef]

#: 熔断阈值：同一目标 + 同一故障码**连续**出现这么多次之后，下次在动手之前拒掉。
FUSE_CAPACITY = 2

PLACEHOLDER_RE = re.compile(r"\{(project|config|port|tools)\}")


# ------------------------------------------------------------------ 解析（路径）
class Resolver:
    """把声明里的占位符展开成本机值（**值全部由装配处交进来**）。

    ⚠️ **两件事分开**（真机踩到过）：
    - `expand()` 只替换占位符，**不碰别的字** —— 参数数组里的开关（`-build`、`--config`）
      原样保留；
    - `path()` 才把一条**路径**解析成绝对路径（相对项按工程根算）。
    把两者混成一个函数，`--project` 就会被拼成 `<工程根>/--project`（那条命令必死）。
    """

    def expand(self, text: str) -> str:  # pragma: no cover - 接口
        raise NotImplementedError

    def path(self, text: str) -> str:
        """把声明里的**路径**解析成绝对路径（默认实现：只展开占位符）。"""
        return str(self.expand(text))

    def tool(self, key: str) -> str:  # pragma: no cover - 接口
        raise NotImplementedError

    def exists(self, path: str) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def bench(self, key: str) -> tuple[bool, str]:  # pragma: no cover - 接口
        raise NotImplementedError

    @property
    def port(self) -> str:  # pragma: no cover - 接口
        return ""


@dataclass
class RecipeResolver(Resolver):
    """参考解析器：一个工程根 + 一张工具表 + 台架值。

    它**不认识任何一块板**：`{project}` 是这次干活的工程根，`{config}` 是构建配置名，
    `{port}` 是这一趟要用的通信口，`{tools}` 是平台工具目录。四个占位符之外的东西
    一律原样保留（`-build` 这种开关就是原样保留的）。
    """

    project_dir: Path
    tools: Mapping[str, str] = field(default_factory=dict)
    config: str = ""
    port_name: str = ""
    tools_dir: str = ""
    bench_values: Mapping[str, tuple[bool, str]] = field(default_factory=dict)

    @property
    def port(self) -> str:
        return self.port_name

    def expand(self, text: str) -> str:
        """只替换四个占位符（**不拼路径**：开关与字面参数必须原样过去）。"""
        replacements = {
            "{project}": str(self.project_dir),
            "{config}": str(self.config),
            "{port}": str(self.port_name),
            "{tools}": str(self.tools_dir or Path(__file__).resolve().parent.parent),
        }
        return PLACEHOLDER_RE.sub(
            lambda match: replacements.get(match.group(0), match.group(0)), str(text)
        )

    def path(self, text: str) -> str:
        """一条**路径** → 绝对路径（相对项按工程根算）。"""
        value = self.expand(text)
        path = Path(value)
        return str(path if path.is_absolute() else Path(self.project_dir) / path)

    def tool(self, key: str) -> str:
        value = self.tools.get(str(key))
        if not value:
            raise adapter_mod.AdapterUnavailable(
                f"工具键 {key!r} 没有交进来（可用的是 {sorted(self.tools)}）"
            )
        return str(value)

    def exists(self, path: str) -> bool:
        return Path(str(path)).is_file()

    def bench(self, key: str) -> tuple[bool, str]:
        row = self.bench_values.get(str(key))
        return (bool(row[0]), str(row[1])) if row else (False, "没有这一格")


# ------------------------------------------------------------------ 跑命令
@dataclass(frozen=True)
class ToolRun:
    """一条命令跑完之后**能观察到的那点事实**（executor 如实交回来）。"""

    returncode: int | None = None
    output: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    error: str = ""

    @property
    def no_output(self) -> bool:
        """超时且**一个字节都没输出**（探针卡住时的典型症状）。"""
        return bool(self.timed_out) and not str(self.output or "").strip()


@dataclass(frozen=True)
class ProbeOutcome:
    """调试/通信连通性的一次探测结果。`unavailable` = 这条链做不到（不是失败）。"""

    ok: bool
    detail: str = ""
    lines: tuple[str, ...] = ()
    unavailable: bool = False
    fault: str = ""


@dataclass(frozen=True)
class IdentityProbe:
    """一次身份探测的结果。`observed` 为空时，`detail` **必须说清为什么**。

    ⚠️ 三种「没拿到」的下一步完全不同（2026-10-13 真机踩到）：
    ① 打不开通信口（多半是另一个 owner / 另一个进程正占着）⇒ 让占用者放手再重跑；
    ② 口打得开、但一行都没收到 ⇒ 设备没在跑；
    ③ 收到了行、但没有一句能被身份解析器认出来 ⇒ 设备在跑但不报身份（查固件/协议）。
    以前这三种都被压成同一句「没拿到设备自报」，要靠人手逐层排除才分得清。
    """

    observed: Mapping[str, Any] | None = None
    detail: str = ""
    fault: str = "identity_unavailable"
    lines: tuple[str, ...] = ()


# ------------------------------------------------------------------ 熔断状态
@dataclass
class FuseState:
    """同一目标 + 同一故障码的**连续**计数（跨次调用可持久化）。

    ⚠️ 键是"适配器 + 故障码"：同一块探针卡住时措辞每次都不一样，但**故障码是同一个**
    —— 挂在措辞上就等于没有熔断（实测过）。
    """

    strikes: dict[str, int] = field(default_factory=dict)
    last_fault: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def key(adapter_id: str, fault: str) -> str:
        return f"{adapter_id}|{fault}"

    def count(self, adapter_id: str, fault: str) -> int:
        return int(self.strikes.get(self.key(adapter_id, fault), 0))

    def blocked(self, adapter_id: str) -> str:
        """这个适配器现在能不能动手（空串 = 可以；否则给出被拦下的故障码）。

        ⚠️ 熔断对**所有**故障码生效（需求 §3 的口径就是“同一故障码连续两次”）：
        构建错了一行也是“先处置（改代码），再显式恢复”，而不是让第四、五次跑继续烧时间。
        """
        fault = self.last_fault.get(str(adapter_id), "")
        if not fault:
            return ""
        return fault if self.count(adapter_id, fault) >= FUSE_CAPACITY else ""

    def record(self, adapter_id: str, fault: str) -> int:
        fault = str(fault or "")
        if not fault or fault == contract_mod.FAULT_NONE:
            return 0
        previous = self.last_fault.get(str(adapter_id), "")
        if previous and previous != fault:
            # ⚠️ "连续"是按故障码算的：中间出现了**别的**故障码 ⇒ 上一种的连续被打断，
            #    它的账**清零**（而不是只把新码的账清掉 —— 那样上一种会永远挂着）。
            self.strikes.pop(self.key(adapter_id, previous), None)
        self.last_fault[str(adapter_id)] = fault
        key = self.key(adapter_id, fault)
        self.strikes[key] = int(self.strikes.get(key, 0)) + 1
        return self.strikes[key]

    def succeed(self, adapter_id: str) -> None:
        """成功一次 ⇒ 这个适配器的账**清零**（连续中断了）。"""
        self.last_fault.pop(str(adapter_id), None)
        for key in [item for item in self.strikes if item.startswith(f"{adapter_id}|")]:
            self.strikes.pop(key, None)

    def clear(self, adapter_id: str = "") -> None:
        """人工处置之后的**显式恢复**（命令行只在这里开口子）。"""
        if not adapter_id:
            self.strikes.clear()
            self.last_fault.clear()
            return
        self.last_fault.pop(str(adapter_id), None)
        for key in [item for item in self.strikes if item.startswith(f"{adapter_id}|")]:
            self.strikes.pop(key, None)

    def to_json(self) -> dict[str, Any]:
        return {
            "strikes": dict(sorted(self.strikes.items())),
            "last_fault": dict(sorted(self.last_fault.items())),
        }

    @classmethod
    def from_json(cls, document: Mapping[str, Any] | None) -> "FuseState":
        document = dict(document or {})
        return cls(
            strikes={str(k): int(v) for k, v in dict(document.get("strikes") or {}).items()},
            last_fault={
                str(k): str(v) for k, v in dict(document.get("last_fault") or {}).items()
            },
        )


# ------------------------------------------------------------------ 结果形状
@dataclass(frozen=True)
class StepOutcome:
    """一个步骤的结论（人读摘要、证据与页面共用同一份）。"""

    step: str
    adapter_id: str
    label: str
    status: str
    fault: str
    detail: str
    seconds: float = 0.0
    argv: tuple[str, ...] = ()
    artifacts: tuple[Mapping[str, Any], ...] = ()
    log_bytes: int = 0
    #: 工具输出的**尾部**（有界的）：失败时，光有"多少字节"看不出原因 ——
    #: 证据里必须留下那几行（失败要自己带着下一步）。
    output_tail: str = ""
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "adapter_id": self.adapter_id,
            "label": self.label,
            "status": self.status,
            "fault": self.fault,
            "fault_label": contract_mod.FAULT_LABELS.get(self.fault, self.fault),
            "detail": self.detail,
            "seconds": round(float(self.seconds), 3),
            "argv": list(self.argv),
            "artifacts": [dict(row) for row in self.artifacts],
            "log_bytes": self.log_bytes,
            "output_tail": self.output_tail,
            "notes": list(self.notes),
        }


@dataclass
class OperationReport:
    """一次统一入口操作的全部结论。"""

    adapter_id: str
    target_id: str
    contract_version: int
    result: str
    steps: list[StepOutcome] = field(default_factory=list)
    fuse: FuseState = field(default_factory=FuseState)
    confirm: dict[str, Any] = field(default_factory=dict)
    evidence: Any = None
    notes: tuple[str, ...] = ()

    def step(self, name: str) -> StepOutcome | None:
        for row in self.steps:
            if row.step == name:
                return row
        return None

    def failures(self) -> list[StepOutcome]:
        return [row for row in self.steps if row.status in ("fail", "refused")]

    def first_fault(self) -> str:
        for row in self.steps:
            if row.status in ("fail", "refused") and row.fault:
                return row.fault
        return contract_mod.FAULT_NONE

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "target_id": self.target_id,
            "contract_version": self.contract_version,
            "result": self.result,
            "fault": self.first_fault(),
            "steps": [row.to_json() for row in self.steps],
            "fuse": self.fuse.to_json(),
            "confirm": dict(self.confirm),
            "notes": list(self.notes),
        }


#: 证据里保留多少工具输出（尾部）。
OUTPUT_TAIL_CHARS = 1500


def output_tail(text: str, *, limit: int = OUTPUT_TAIL_CHARS) -> str:
    """工具输出的尾部（**有界**：证据不会因为一条输出而涨到看不清）。"""
    value = str(text or "")
    return value if len(value) <= limit else "…" + value[-limit:]


STATUS_MARKS: dict[str, str] = {
    "pass": "OK",
    "fail": "FAIL",
    "skipped": "SKIP",
    "refused": "REFUSED",
    "unsupported": "N/A",
}


def summarize(report: OperationReport) -> list[str]:
    """人读摘要（命令行与人读证据共用；**一个事实都不加**）。"""
    head = f"{report.adapter_id}：{report.result}"
    fault = report.first_fault()
    if fault != contract_mod.FAULT_NONE:
        head += f" · {contract_mod.FAULT_LABELS.get(fault, fault)}"
    lines = [head]
    for row in report.steps:
        mark = STATUS_MARKS.get(row.status, row.status)
        suffix = ""
        if row.fault not in ("", contract_mod.FAULT_NONE):
            suffix = f" · {contract_mod.FAULT_LABELS.get(row.fault, row.fault)}"
        lines.append(f"  [{mark:>7}] {row.step:<11} {row.detail}{suffix}")
    return lines


# ================================================================== 统一入口
def resolve_identity_source(
    expectation: contract_mod.IdentityExpectation | None,
    resolver: Resolver,
) -> contract_mod.IdentityExpectation | None:
    """把“期望值来自**本次构建的产物**”这件事做掉（读不到就如实记成**未比**）。

    ⚠️ 只在**本次构建真的过了之后**才调用。写死的期望值只能证明“设备报得出某个
    值”；“这次烧的就是这次编的”要拿本次产物的身份去比。读不到就记进 `unresolved`，
    核验那一步会明说这几个字段没比 —— **不是默默放过**。
    """
    if expectation is None:
        return None
    source = expectation.source
    if source is None:
        return expectation
    values = dict(expectation.values)
    unresolved: tuple[str, ...] = ()
    try:
        path = Path(resolver.path(source.artifact))
        payload = json.loads(path.read_text(encoding="utf-8"))
        values[source.field] = source.render(payload[source.key])
    except Exception:  # noqa: BLE001 - 读不到就是读不到（不在这一层猜）
        unresolved = (source.field,)
    return contract_mod.with_resolved(expectation, values, unresolved)


def run_operation(
    declaration: contract_mod.HardwareDeclaration,
    *,
    executor: Callable[[adapter_mod.CommandSpec], ToolRun],
    resolver: Resolver,
    steps: Iterable[str] | None = None,
    fuse: FuseState | None = None,
    evidence: Callable[[dict[str, Any]], Any] | None = None,
    confirm_flash: bool = False,
    allow_overwrite: bool = False,
    debug_probe: Callable[[], ProbeOutcome] | None = None,
    identity_probe: Callable[[], Mapping[str, Any] | IdentityProbe | None] | None = None,
    identity_expectation: contract_mod.IdentityExpectation | None = None,
    commands: Mapping[str, contract_mod.PreparedCommand] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> OperationReport:
    """按声明的契约跑一遍操作（**判据、故障码、熔断都由声明与契约决定**）。"""
    wanted = {str(name) for name in (steps if steps is not None else contract_mod.STEP_ORDER)}
    state = fuse if fuse is not None else FuseState()
    #: 这一趟要比对的身份期望值（**构建之后**可能被产物里的值补上）。
    expectation = (
        identity_expectation if identity_expectation is not None
        else declaration.identity_expectation
    )
    report = OperationReport(
        adapter_id=declaration.adapter_id,
        target_id=declaration.target_id,
        contract_version=int(declaration.contract_version),
        result="incomplete",
        fuse=state,
        confirm={"flash": bool(confirm_flash), "allow_overwrite": bool(allow_overwrite)},
        notes=tuple(declaration.notes),
    )

    # ---------------------------------------------------------------- preflight
    preflight = _preflight(declaration, resolver)
    report.steps.append(preflight)
    if preflight.status != "pass":
        for name in contract_mod.DECLARED_STEPS:
            if name in wanted and declaration.step(name) is not None:
                report.steps.append(_skip(name, declaration, "前置检查没过 ⇒ 不往下走"))
        report.result = "fail" if preflight.status == "fail" else preflight.status
        return _finish(report, declaration, evidence)

    blocked_by = ""

    # -------------------------------------------------------------------- build
    build = declaration.step("build")
    if build is not None:
        if "build" not in wanted:
            report.steps.append(_skip("build", declaration, "没有请求这一步"))
        else:
            outcome = _run_tool_step(
                build, declaration, resolver, executor, state, clock, commands
            )
            report.steps.append(outcome)
            if outcome.status != "pass":
                blocked_by = f"build 没有通过（{outcome.detail}）"
            else:
                # 身份期望值来自**本次构建的产物**：只有构建真过了才去读。
                expectation = resolve_identity_source(expectation, resolver)

    # -------------------------------------------------------------------- flash
    flash = declaration.step("flash")
    if flash is None or "flash" not in wanted:
        report.steps.append(
            _skip(
                "flash", declaration,
                "这份契约没有声明烧录步骤" if flash is None else "没有请求这一步",
            )
        )
    elif blocked_by:
        report.steps.append(_skip("flash", declaration, blocked_by))
    else:
        missing = []
        if not confirm_flash:
            missing.append("--confirm-flash")
        if not allow_overwrite:
            missing.append("--allow-overwrite")
        if missing:
            # 需求 §3：覆盖板上现有应用的**安全确认**在 core 这一层挡住，
            # 而不是把"有没有同意"留给适配器自己去猜。
            report.steps.append(
                StepOutcome(
                    step="flash", adapter_id=flash.adapter_id, label=flash.label,
                    status="refused", fault="flash_refused",
                    detail="没有拿到显式同意（缺少 " + " ".join(missing) + "）：不写任何字节",
                )
            )
        else:
            outcome = _run_tool_step(
                flash, declaration, resolver, executor, state, clock, commands
            )
            report.steps.append(outcome)
            if outcome.status != "pass":
                blocked_by = f"flash 没有通过（{outcome.detail}）"

    # ------------------------------------------------------------------- verify
    if "verify" not in wanted:
        report.steps.append(_skip("verify", declaration, "没有请求这一步"))
    elif blocked_by:
        report.steps.append(_skip("verify", declaration, blocked_by))
    else:
        report.steps.append(
            _verify_step(declaration, resolver, executor, state, clock,
                         identity_probe, expectation, commands)
        )

    # -------------------------------------------------------------- debug-check
    if "debug-check" in wanted:
        _append_debug(report, declaration, debug_probe)
    else:
        report.steps.append(_skip("debug-check", declaration, "没有请求这一步"))

    report.result = _overall(report)
    return _finish(report, declaration, evidence)


# ------------------------------------------------------------------ 各步骤实现
def _preflight(
    declaration: contract_mod.HardwareDeclaration, resolver: Resolver
) -> StepOutcome:
    """合成的前置检查：契约自洽 + 输入在 + 工具解析得出来 + 台架配了。

    它**不碰硬件、不跑命令**：只把"这一趟能不能开始"说清楚（配错就拒绝，不部分采用）。
    产物**不在这里判**：它们是构建的产出，构建之前本来就该不在。
    """
    problems: list[str] = []
    for item in declaration.problems():
        problems.append(f"契约：{item}")
    missing_steps = contract_mod.missing_steps(declaration)
    if missing_steps:
        problems.append(
            "契约没有覆盖这些步骤：" + "、".join(missing_steps)
            + "（要么声明它，要么在 excluded 里写清为什么不覆盖）"
        )
    for step in declaration.steps:
        for item in step.inputs:
            path = resolver.path(item)
            if not resolver.exists(path):
                problems.append(f"{step.step} 的输入不在：{path}")
    for key in declaration.bench_keys:
        configured, detail = resolver.bench(key)
        if not configured:
            problems.append(f"台架键 {key} 没配置（{detail}）")
    for key in declaration.tools:
        try:
            path = resolver.tool(key)
        except adapter_mod.AdapterUnavailable as error:
            problems.append(f"工具 {key}：{error}")
            continue
        if not resolver.exists(path):
            problems.append(f"工具 {key} 不在：{path}")
    fault = contract_mod.FAULT_NONE
    if problems:
        if any("台架键" in item for item in problems):
            fault = "bench_unconfigured"
        elif any("工具" in item for item in problems):
            fault = "tool_missing"
        else:
            fault = "input_missing"
    return StepOutcome(
        step="preflight",
        adapter_id=f"{declaration.adapter_id}.preflight",
        label="前置检查（契约 / 输入 / 工具 / 台架）",
        status="fail" if problems else "pass",
        fault=fault,
        detail="；".join(problems) if problems else "契约、输入、工具与台架都就绪",
        notes=tuple(declaration.transport.notes),
    )


def _skip(name: str, declaration: contract_mod.HardwareDeclaration, why: str) -> StepOutcome:
    step = declaration.step(name)
    return StepOutcome(
        step=name,
        adapter_id=step.adapter_id if step else "",
        label=step.label if step else name,
        status="skipped",
        fault=contract_mod.FAULT_NONE,
        detail=why,
    )


def _command_for(
    step: contract_mod.StepSpec,
    resolver: Resolver,
    commands: Mapping[str, contract_mod.PreparedCommand] | None = None,
) -> adapter_mod.CommandSpec:
    """这一趟要跑的命令：目标层准备好了就用它，否则按声明的工具键与参数展开。"""
    prepared = (commands or {}).get(step.adapter_id)
    if prepared is not None:
        return adapter_mod.CommandSpec(
            adapter_id=step.adapter_id,
            argv=tuple(prepared.argv),
            cwd=Path(prepared.cwd) if prepared.cwd else Path(resolver.path("{project}")),
            purpose=step.label,
            timeout_s=float(prepared.timeout_s or step.timeout_s),
            attach=bool(prepared.attach or step.attach),
        )
    if step.prepared:
        raise adapter_mod.AdapterUnavailable(
            f"{step.adapter_id} 声明了「argv 由目标层给」，但装配处没有交出命令："
            "这一步没法跑（不是失败，是装配缺了一半）"
        )
    argv: list[str] = []
    if step.tool:
        argv.append(resolver.tool(step.tool))
    # ⚠️ 参数用 `expand`（只展开占位符）：开关与字面参数必须原样过去。
    argv.extend(resolver.expand(item) for item in step.args)
    cwd = resolver.path(step.cwd) if step.cwd else resolver.path("{project}")
    return adapter_mod.CommandSpec(
        adapter_id=step.adapter_id,
        argv=tuple(argv),
        cwd=Path(cwd),
        purpose=step.label,
        timeout_s=float(step.timeout_s),
        attach=bool(step.attach),
    )


def _artifact_facts(
    step: contract_mod.StepSpec, resolver: Resolver
) -> tuple[tuple[Mapping[str, Any], ...], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in step.artifacts:
        path = Path(resolver.path(item))
        if not path.is_file():
            rows.append({"path": str(path), "present": False})
            missing.append(str(path))
            continue
        rows.append({
            "path": str(path),
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return tuple(rows), missing


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_tool_step(
    step: contract_mod.StepSpec,
    declaration: contract_mod.HardwareDeclaration,
    resolver: Resolver,
    executor: Callable[[adapter_mod.CommandSpec], ToolRun],
    fuse: FuseState,
    clock: Callable[[], float],
    commands: Mapping[str, contract_mod.PreparedCommand] | None = None,
) -> StepOutcome:
    """跑一步受控命令：熔断 → 执行 → 按**声明的判据**判 → 记账。"""
    blocked = fuse.blocked(step.adapter_id)
    if blocked:
        advice = (
            "；".join(declaration.faults.recovery)
            if blocked in contract_mod.HARDWARE_FAULTS else ""
        )
        return StepOutcome(
            step=step.step, adapter_id=step.adapter_id, label=step.label,
            status="refused", fault="fuse_open",
            detail=(
                f"同一故障（{contract_mod.FAULT_LABELS.get(blocked, blocked)}）已经连续"
                f"出现 {FUSE_CAPACITY} 次 ⇒ 这一次在调用工具之前拒掉。"
                "先人工处置，再显式清除熔断（命令行：`fuse --clear`）"
                + (f"；{advice}" if advice else "")
            ),
            notes=("熔断只对需要人工处置的故障开口子：重试解决不了的问题不该继续重试。",),
        )

    try:
        spec = _command_for(step, resolver, commands)
    except adapter_mod.AdapterUnavailable as error:
        fuse.record(step.adapter_id, "tool_missing")
        return StepOutcome(
            step=step.step, adapter_id=step.adapter_id, label=step.label,
            status="fail", fault="tool_missing", detail=str(error),
        )

    started = clock()
    run = executor(spec)
    seconds = run.seconds or (clock() - started)

    facts, missing = _artifact_facts(step, resolver)
    if run.timed_out and run.no_output:
        verdict = contract_mod.Verdict(
            "fail", "no_output_timeout", f"超时（>{spec.timeout_s:g}s）且一个字节都没有输出"
        )
    elif run.timed_out:
        verdict = contract_mod.Verdict("fail", "timeout", f"超时（>{spec.timeout_s:g}s）")
    elif run.error:
        # ⚠️ “命令起不来”是**工具不可用**，不是这一步本身失败：把它归到 build_failed
        #    / unknown_failure 会把下一步指向“改代码/重试”，而它该指向“把工具找回来”
        #    （2026-10-13 验收：脚手架把工具键指向一个 `.py`，Windows 报 WinError 193，
        #    屏幕写的是“构建失败”）。
        verdict = contract_mod.Verdict(
            "fail", "tool_missing",
            f"命令起不来（不是它跑失败，是它根本没起来）：{run.error}",
        )
    else:
        verdict = contract_mod.judge(
            step.success,
            returncode=run.returncode,
            output=run.output,
            missing_artifacts=missing,
            artifacts_total=len(facts),
        )

    detail = verdict.detail
    fault = verdict.fault
    if verdict.status == "fail" and fault in ("", contract_mod.FAULT_NONE):
        fault = declaration.faults.default_for(step.step)
    if verdict.status == "fail":
        hint = declaration.faults.explain(run.output)
        if hint:
            detail = f"{detail}；{hint}"

    outcome = StepOutcome(
        step=step.step, adapter_id=step.adapter_id, label=step.label,
        status=verdict.status, fault=fault, detail=detail, seconds=seconds,
        argv=spec.argv, artifacts=facts,
        log_bytes=len(str(run.output or "").encode("utf-8")),
        output_tail=output_tail(run.output),
        notes=tuple(step.notes),
    )
    if outcome.status == "pass":
        fuse.succeed(step.adapter_id)
    else:
        fuse.record(step.adapter_id, fault)
    return outcome


def _verify_step(
    declaration: contract_mod.HardwareDeclaration,
    resolver: Resolver,
    executor: Callable[[adapter_mod.CommandSpec], ToolRun],
    fuse: FuseState,
    clock: Callable[[], float],
    identity_probe: Callable[[], Mapping[str, Any] | IdentityProbe | None] | None,
    identity_expectation: contract_mod.IdentityExpectation | None,
    commands: Mapping[str, contract_mod.PreparedCommand] | None = None,
) -> StepOutcome:
    """烧录后核验身份：先按声明的身份步骤问一次，再拿注入的只读探针取回答。

    ⚠️ 核心**不**自己向端口写命令（DEVICE-001 / D4 立下的规矩）：问身份是**项目包**的
    契约，编排只按声明执行步骤。核验的四种结论由 `judge_identity()` 分开报
    （「问不了」与「说得不一样」不是一回事）。
    """
    step = declaration.step("verify")
    expectation = identity_expectation or declaration.identity_expectation

    if step is not None:
        outcome = _run_tool_step(step, declaration, resolver, executor, fuse, clock, commands)
        if outcome.status != "pass":
            return outcome

    if identity_probe is None:
        return StepOutcome(
            step="verify", adapter_id=step.adapter_id if step else "",
            label="烧录后身份核验", status="unsupported", fault="identity_unavailable",
            detail="这一条链没有可用的身份核验入口 ⇒ 平台核验不了（不是核验通过）",
        )
    probed = identity_probe()
    # 兼容两种返回：旧的 `Mapping | None`，与新的 `IdentityProbe`（带理由）。
    # ⚠️ 「做不到」与「做了但没认出来」要分开：打不开口是**环境**问题（下一步是让占用者
    #    放手），不是设备 / 固件的问题 —— 两者报成同一句就会把人往错方向支（真机踩到）。
    fields: Mapping[str, Any] | None = probed
    detail = ""
    fault = "identity_unavailable"
    if isinstance(probed, IdentityProbe):
        fields, detail, fault = probed.observed, probed.detail, probed.fault
    if fields is None:
        return StepOutcome(
            step="verify", adapter_id=step.adapter_id if step else "",
            label="烧录后身份核验",
            status="unsupported" if fault == "transport_unavailable" else "skipped",
            fault=fault,
            detail=detail or "没拿到设备自报 ⇒ 未核验（不判失败，也不冒充通过）",
        )
    verdict = contract_mod.judge_identity(expectation, fields)
    return StepOutcome(
        step="verify", adapter_id=step.adapter_id if step else "",
        label="烧录后身份核验", status=verdict.status, fault=verdict.fault,
        detail=verdict.detail,
    )


def _append_debug(
    report: OperationReport,
    declaration: contract_mod.HardwareDeclaration,
    debug_probe: Callable[[], ProbeOutcome] | None,
) -> None:
    """调试/通信连通性：这一条链**做不到**时如实 `unsupported`，不是失败。"""
    if debug_probe is None:
        report.steps.append(
            StepOutcome(
                step="debug-check", adapter_id=declaration.transport.adapter_id,
                label="调试 / 通信连通性", status="unsupported",
                fault="transport_unavailable",
                detail="没有交进任何串口实现 ⇒ 平台判不了这一项（不是通过）",
            )
        )
        return
    outcome = debug_probe()
    if outcome.unavailable:
        status, fault = "unsupported", outcome.fault or "transport_unavailable"
    elif outcome.ok:
        status, fault = "pass", contract_mod.FAULT_NONE
    else:
        status, fault = "fail", outcome.fault or "transport_unavailable"
    report.steps.append(
        StepOutcome(
            step="debug-check", adapter_id=declaration.transport.adapter_id,
            label="调试 / 通信连通性", status=status, fault=fault,
            detail=outcome.detail,
            log_bytes=sum(len(line) for line in outcome.lines),
        )
    )


def _overall(report: OperationReport) -> str:
    """整份操作的结论（失败优先于阻挡，阻挡优先于未核验）。"""
    statuses = [row.status for row in report.steps]
    if "fail" in statuses:
        return "fail"
    if "refused" in statuses:
        return "refused"
    if any(
        row.status == "unsupported" for row in report.steps if row.step in ("build", "flash")
    ):
        return "skipped"
    # ⚠️ **请求了却判不出来**（未核验 / 串口不可用 / 明确做不到）⇒ 整份操作算“未完成”。
    #    只靠步骤那一行说“未核验”不够：顶层写 pass + 退出码 0 会被脚本读成“这趟通过了”，
    #    而“不判失败，也不冒充通过”这句话对**整份操作**同样成立（2026-10-13 验收拓到）。
    #    区别看故障码：“没有请求这一步”的跳过不带故障码。
    unjudged = [
        row for row in report.steps
        if row.status in ("skipped", "unsupported")
        and str(row.fault or contract_mod.FAULT_NONE) != contract_mod.FAULT_NONE
    ]
    if unjudged:
        return "incomplete"
    if all(row.status == "skipped" for row in report.steps):
        return "skipped"
    return "pass"


def _finish(
    report: OperationReport,
    declaration: contract_mod.HardwareDeclaration,
    evidence: Callable[[dict[str, Any]], Any] | None,
) -> OperationReport:
    if evidence is not None:
        document = report.to_json()
        document["declaration"] = declaration.to_json()
        report.evidence = evidence(document)
    return report

"""假工具 / 假串口 + 一份**中性**参考契约（**core**）。

两件事共用同一个模块：

1. **契约测试**：不接硬件就能把成功、已知失败、未知失败、零输出超时、身份不符与
   两次熔断各跑一遍（需求 §7.4）。这是"纸面契约"与"真行为"之间的那道桥。
2. **脚手架的第一个可跑产物**：`create-adapter` 生成的骨架拿它当冒烟对象 ——
   新 Agent 先让假工具跑通，再去接真工具。

⚠️ 这里的一切都是**中性**的：没有芯片族、没有工具厂商、没有串口名。假工具按
`adapter_id` 领一个脚本化的反应；假串口按脚本回行、回身份或不回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

try:  # imported as part of the package
    from . import hwops_contract as contract_mod
    from . import hwops_runner as runner_mod
    from . import target_adapters as adapter_mod
except ImportError:  # imported as a top-level module
    import hwops_contract as contract_mod  # type: ignore[no-redef]
    import hwops_runner as runner_mod  # type: ignore[no-redef]
    import target_adapters as adapter_mod  # type: ignore[no-redef]

#: 假工具的目录（`RecipeResolver.tools` 里指到这里；它只是一个**存在的文件**）。
FAKE_TOOL_KEY = "fake-tool"
FAKE_TOOL_FILE = __file__


@dataclass
class FakeExecutor:
    """按 `adapter_id` 领反应的执行器（数调用次数、留调用现场）。

    `script` 的值可以是 `ToolRun`（每次都同一个反应）或 `Callable[[CommandSpec], ToolRun]`
    （按命令现场决定，用来造"第一次失败、第二次成功"这种序列）。
    """

    script: Mapping[str, Any] = field(default_factory=dict)
    default: runner_mod.ToolRun = field(
        default_factory=lambda: runner_mod.ToolRun(returncode=0, output="ok")
    )
    calls: list[adapter_mod.CommandSpec] = field(default_factory=list)

    def __call__(self, spec: adapter_mod.CommandSpec) -> runner_mod.ToolRun:
        self.calls.append(spec)
        reaction = self.script.get(str(spec.adapter_id), self.default)
        return reaction(spec) if callable(reaction) else reaction

    def called(self, adapter_id: str) -> int:
        return sum(1 for spec in self.calls if spec.adapter_id == adapter_id)


@dataclass
class FakeDevice:
    """假串口 / 假身份：按脚本回行，或如实说"这条链做不到"。"""

    identity: Mapping[str, Any] | None = None
    lines: tuple[str, ...] = ()
    unavailable: bool = False
    detail: str = ""
    opened: int = 0

    def probe(self) -> runner_mod.ProbeOutcome:
        """调试/通信连通性那一步的注入物。"""
        self.opened += 1
        if self.unavailable:
            return runner_mod.ProbeOutcome(
                ok=False, unavailable=True, detail=self.detail or "这条链没有串口实现"
            )
        if not self.lines and self.identity is None:
            return runner_mod.ProbeOutcome(
                ok=False, fault="transport_unavailable",
                detail=self.detail or "打开了口，但一个字节都没读到（不冒充连通）",
            )
        return runner_mod.ProbeOutcome(
            ok=True, lines=self.lines, detail=self.detail or "打开成功并读到数据"
        )

    def identity_probe(self) -> Mapping[str, Any] | None:
        return dict(self.identity) if self.identity else None


#: 假契约里那两行"工具自己打出来的统计"（把"退出码 0 ≠ 没事"这件事做成可测的形状）。
FAKE_ERROR_LINE = "Total number of errors: {count}"
FAKE_LINKED_LINE = "Linking"
FAKE_FLASH_DONE = "flash done"

#: 假契约声明的产物（相对工程根）。
FAKE_BUILD_ARTIFACT = "out/demo-app.elf"
FAKE_BUILD_REPORT = "out/demo-app.map"


def demo_declaration() -> contract_mod.HardwareDeclaration:
    """一份**中性**的参考契约：它是"接入一条新硬件要声明什么"的最小完整形状。

    它同时是脚手架生成物的对照物：生成出来的骨架应当与它同形（只是名字不同）。
    """
    return contract_mod.HardwareDeclaration(
        adapter_id="demo-board-bm",
        target_id="demo-target",
        label="演示板（假工具 · 只用于契约测试与脚手架冒烟）",
        layer="target-demo",
        module="demo_target",
        project_entry="project/entry.ini",
        build_config="release",
        bench_keys=("demo_path",),
        tools=(FAKE_TOOL_KEY,),
        steps=(
            contract_mod.StepSpec(
                step="build",
                adapter_id="demo.build",
                label="构建演示固件（判据要在日志里：退出码 0 不等于编成了）",
                tool=FAKE_TOOL_KEY,
                args=("build", "{project}", "--config", "{config}"),
                inputs=("project/entry.ini",),
                artifacts=(FAKE_BUILD_ARTIFACT, FAKE_BUILD_REPORT),
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=(FAKE_LINKED_LINE,),
                    error_count_pattern=r"Total number of errors:\s*(\d+)",
                    require_count=True,
                    min_artifacts=2,
                    note="工具即便没链接成功也可能返回 0 ⇒ 数错误行",
                ),
                notes=("这一步不碰硬件。",),
            ),
            contract_mod.StepSpec(
                step="flash",
                adapter_id="demo.flash",
                label="写入演示板（覆盖板上现有应用要显式同意）",
                tool=FAKE_TOOL_KEY,
                args=("flash", "--port", "{port}", "{project}"),
                inputs=("project/entry.ini",),
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=(FAKE_FLASH_DONE,),
                    failure_markers=("Fatal error", "Connection lost"),
                    note="失败时工具可能只打 Warning",
                ),
                notes=("写完之后由 verify 那一步核身份。",),
            ),
            contract_mod.StepSpec(
                step="verify",
                adapter_id="demo.identity",
                label="烧录后核身份（拿设备自报对表，不猜）",
                tool=FAKE_TOOL_KEY,
                args=("identity", "--port", "{port}"),
                timeout_s=30.0,
                success=contract_mod.SuccessCriterion(kind="exit"),
            ),
            contract_mod.StepSpec(
                step="debug-check",
                adapter_id="demo.debug",
                label="打开通信口读一小段（最小连通性检查）",
                tool=FAKE_TOOL_KEY,
                args=("probe", "--port", "{port}"),
                timeout_s=20.0,
            ),
        ),
        faults=contract_mod.FaultKnowledge(
            markers=(
                contract_mod.FaultMarker(
                    "Fatal error", "flash_failed", "工具在写之前就放弃了（这次是零写入）"
                ),
                contract_mod.FaultMarker(
                    "Connection lost", "transport_unavailable",
                    "链路在写的中途断了（不算写成功，即使退出码是 0）",
                ),
            ),
            recovery=("拔插一次再试，不要连着重试",),
            unknown_advice="这一步出现了没见过的失败，先保留原文再判断",
            step_defaults=(("build", "build_failed"), ("flash", "flash_failed")),
        ),
        capabilities=(
            contract_mod.CapabilityResult("build", "supported"),
            contract_mod.CapabilityResult("flash", "supported"),
            contract_mod.CapabilityResult("transport", "supported"),
            contract_mod.CapabilityResult("identity", "supported"),
            contract_mod.CapabilityResult(
                "symbolize", "unsupported", "这条链还没有地址还原入口"
            ),
        ),
        transport=contract_mod.TransportHint(
            kind="serial", baud=115200, line_ending="lf", read_only=True,
            adapter_id="demo.serial.read-only",
            minimal_check="打开口、读一行就关（不写复位线）",
            notes=("打开通信口不写调制线，所以不会复位板子。",),
        ),
        identity=contract_mod.IdentityHint(
            query="identity", fields=("project", "build_id"),
            banner_markers=("boot build=",),
        ),
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"), values={"project": "demo"}
        ),
        excluded=(),
        notes=("这份契约是**中性参考**：假工具、假串口，不接任何真实硬件。",),
    )


def demo_resolver(
    project_dir: Any, *, port: str = "PORT-A", bench_ok: bool = True,
    bench_detail: str = "",
) -> "runner_mod.RecipeResolver":
    """假契约配套的解析器（工具指向本文件，所以"工具存在"这条一定成立）。"""
    from pathlib import Path

    return runner_mod.RecipeResolver(
        project_dir=Path(project_dir),
        tools={FAKE_TOOL_KEY: FAKE_TOOL_FILE},
        config="release",
        port_name=port,
        bench_values={
            "demo_path": (bench_ok, bench_detail or ("已配置" if bench_ok else "没有这一格"))
        },
    )


def tool_run(
    *, code: int = 0, output: str = "", timed_out: bool = False, error: str = "",
) -> runner_mod.ToolRun:
    """造一条命令的反应（成功 / 非零 / 超时 / 起不来）。"""
    return runner_mod.ToolRun(
        returncode=code, output=output, seconds=0.01, timed_out=timed_out, error=error
    )


def build_output(*, errors: int = 0, linked: bool = True) -> str:
    """工具那条"统计行 + 链接标志"的输出（判据要看的正是这两样）。"""
    lines = [FAKE_ERROR_LINE.format(count=errors)]
    if linked:
        lines.append(FAKE_LINKED_LINE)
    return "\n".join(lines) + "\n"


def flash_output(*, done: bool = True, fatal: str = "") -> str:
    lines = []
    if fatal:
        lines.append(f"Fatal error: {fatal}")
    if done:
        lines.append(FAKE_FLASH_DONE)
    return "\n".join(lines) + "\n"


def default_script(**overrides: Any) -> dict[str, Any]:
    """一套"一切正常"的假反应（按需覆盖某一步来造失败）。"""
    script: dict[str, Any] = {
        "demo.build": tool_run(output=build_output()),
        "demo.flash": tool_run(output=flash_output()),
        "demo.identity": tool_run(output="identity ok"),
        "demo.debug": tool_run(output="probe ok"),
    }
    script.update(overrides)
    return script


def materialize(
    declaration: contract_mod.HardwareDeclaration | None = None,
    root: Any = None,
    *,
    artifact: bool = True,
) -> list[str]:
    """在临时工程里铺出「输入在、产物在」的样子（测试与脚手架冒烟共用）。

    ⚠️ 按**声明**铺，不按写死的文件名铺：生成出来的骨架与仓库里那份假契约声明的
    产物名不一样，若夹具另写一份文件名，测出来的就是「夹具与声明对不上」，
    而不是「这一条能不能跑」。
    """
    from pathlib import Path

    if declaration is None:
        declaration = demo_declaration()
    base = Path(root)
    replacements = {
        "{project}": str(base),
        "{config}": str(declaration.build_config),
        "{port}": "PORT-A",
        "{tools}": "",
    }

    def expand(text: str) -> str:
        out = str(text)
        for key, value in replacements.items():
            out = out.replace(key, value)
        return out

    written: list[str] = []

    def write(relative: str, text: str = "") -> None:
        if not relative:
            return
        path = Path(expand(relative))
        path = path if path.is_absolute() else base / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path))

    write(declaration.project_entry, "[demo]\n")
    for step in declaration.steps:
        for item in step.inputs:
            write(item, "input\n")
        if artifact:
            for item in step.artifacts:
                write(item, "artifact-bytes\n")
    return written

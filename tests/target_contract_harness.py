#!/usr/bin/env python3
"""参考适配器契约测试的**共用夹具**（不接任何硬件）。

这一份是"每接一条硬件都要跑的那几件事"的一次性实现：成功、已知失败、零输出超时、
烧录安全闸门、身份四态、熔断、机器检查、开源边界。参考适配器的测试文件各自调用它，
所以判据只有一份，不会随适配器数量漂。

Run:  由 `tests/test_*_target.py` 调用（也可以直接跑它自己，跑的是内置的自检）。
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
TARGETS_DIR = Path(__file__).resolve().parent.parent / "targets"
for _directory in (str(TOOLS_DIR / "webconsole"), str(TOOLS_DIR), str(TARGETS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import unicode_console  # noqa: E402

unicode_console.install()  # 管道/GBK 控制台下打印中文不再中途抛错

import hwops_check as check_mod  # noqa: E402
import hwops_contract as contract_mod  # noqa: E402
import hwops_fake as fake_mod  # noqa: E402
import hwops_runner as runner_mod  # noqa: E402


@dataclass
class Reporter:
    """把 PASS/FAIL 收成一份可判的结论（退出码 = 有没有 FAIL）。"""

    title: str = ""
    failures: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        if condition:
            print(f"PASS {name}{suffix}")
        else:
            self.failures.append(name)
            print(f"FAIL {name}{suffix}")

    def finish(self) -> int:
        if self.failures:
            print("\n失败：" + "；".join(self.failures))
            return 1
        print(f"\n{self.title or '这一套'}：全部通过")
        return 0


def rig(
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    script: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    lines: tuple[str, ...] = ("boot @PWINFO project=demo build_id=0",),
    port: str = "PORT-A",
):
    """一次操作的现场：假执行器 + 假串口 + 解析器（工具指向平台自带的假工具）。

    ⚠️ 对 `prepared=True` 的步骤也交一条**装配处准备好的命令**（指向假工具）：
    没有它，那一步会如实报"装配缺了一半" —— 那是另一条判据，不在这里测。
    """
    tools = {key: fake_mod.FAKE_TOOL_FILE for key in declaration.tools}
    resolve = runner_mod.RecipeResolver(
        project_dir=Path(root), tools=tools, config=declaration.build_config,
        port_name=port,
        # ⚠️ 台架键在这里**当成已配置**：这一套测的是"声明与判据对不对"，
        #    不是"这台机器配没配"（后者由 doctor 与入口的装配检查说）。
        bench_values={key: (True, "测试夹具：当成已配置")
                      for key in declaration.bench_keys},
    )
    executor = fake_mod.FakeExecutor(script=dict(script or fake_mod.default_script()))
    device = fake_mod.FakeDevice(identity=identity, lines=lines)
    commands = {
        step.adapter_id: contract_mod.PreparedCommand(
            argv=(fake_mod.FAKE_TOOL_FILE, step.step), cwd=str(root),
        )
        for step in declaration.steps if step.prepared
    }
    return resolve, executor, device, commands


def scenario(
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    script: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    steps: Iterable[str] | None = None,
    confirm_flash: bool = True,
    allow_overwrite: bool = True,
    fuse: runner_mod.FuseState | None = None,
) -> tuple[runner_mod.OperationReport, fake_mod.FakeExecutor]:
    resolve, executor, device, commands = rig(
        declaration, root, script=script, identity=identity
    )
    report = runner_mod.run_operation(
        declaration,
        executor=executor,
        resolver=resolve,
        steps=steps,
        fuse=fuse,
        confirm_flash=confirm_flash,
        allow_overwrite=allow_overwrite,
        identity_probe=device.identity_probe,
        debug_probe=device.probe,
        commands=commands,
    )
    return report, executor


def passing_script(
    declaration: contract_mod.HardwareDeclaration,
) -> dict[str, Any]:
    """按**声明自己**合成一套"会通过"的假工具反应。

    ⚠️ 不另写一份文件名/标记：假工具要满足的正是**那条步骤声明的判据**，
    否则测出来的是"夹具与声明对不上"，而不是"这一步能不能跑"。
    """
    script: dict[str, Any] = {}
    for step in declaration.steps:
        criterion = step.success
        lines: list[str] = []
        if criterion is not None:
            if criterion.require_count and criterion.error_count_pattern:
                lines.append(_count_line(criterion.error_count_pattern))
            lines.extend(criterion.success_markers)
        script[step.adapter_id] = fake_mod.tool_run(output="\n".join(lines) + "\n")
    return script


def _count_line(pattern: str) -> str:
    """从统计行的正则造一条"零错误"的统计行（捕获组换成 0）。"""
    head = pattern.split("(", 1)[0].replace(r"\s*", " ").rstrip()
    return f"{head} 0"


# ============================================================ 共用判据
def check_declaration_shape(
    reporter: Reporter, declarations: tuple[contract_mod.HardwareDeclaration, ...]
) -> None:
    for declaration in declarations:
        reporter.check(
            f"{declaration.adapter_id}：契约本身站得住",
            not declaration.problems(), "；".join(declaration.problems()),
        )
        reporter.check(
            f"{declaration.adapter_id}：覆盖了全部步骤",
            not contract_mod.missing_steps(declaration),
            "、".join(contract_mod.missing_steps(declaration)),
        )
        reporter.check(
            f"{declaration.adapter_id}：能力四态都带理由",
            all(
                row.result == "supported" or bool(row.reason)
                for row in declaration.capabilities
            ),
        )


def check_machine_checks(
    reporter: Reporter,
    declarations: tuple[contract_mod.HardwareDeclaration, ...],
    *,
    registered: Mapping[str, Iterable[str]],
    sources: Mapping[str, str],
    bench: Mapping[str, Mapping[str, tuple[bool, str]]] | None = None,
) -> None:
    report = check_mod.check_declarations(
        declarations, registered=registered, sources=sources, bench=bench,
    )
    reporter.check("机器检查：没有判不过的项", not report.problems, "；".join(report.problems))
    reporter.check("机器检查：没有判不了的项", not report.unjudged, "；".join(report.unjudged))


def check_bypass_has_teeth(
    reporter: Reporter, declaration: contract_mod.HardwareDeclaration
) -> None:
    """把一条步骤挂到未登记的适配器上 ⇒ 旁路检查必须红。"""
    broken = contract_mod.capsule(
        declaration,
        steps=(
            *declaration.steps,
        ),
    )
    first = declaration.steps[0]
    sneaky = contract_mod.capsule(
        first, adapter_id="vendor.raw-command",
    )
    doctored = contract_mod.capsule(
        broken, steps=(sneaky, *declaration.steps[1:]),
    )
    problems = check_mod.bypass_problems(doctored, ("only.this.one",))
    reporter.check("旁路检查有牙（直接挂一条未登记的厂商命令 ⇒ 红）", bool(problems),
                   "；".join(problems))


def check_isolation(
    reporter: Reporter, declarations: tuple[contract_mod.HardwareDeclaration, ...]
) -> None:
    problems = check_mod.isolation_problems(declarations)
    reporter.check("同一目标的多份 profile 不串用", not problems, "；".join(problems))


def check_opensource(reporter: Reporter, module: Any) -> None:
    path = Path(module.__file__)
    hits = check_mod.opensource_problems([(path.name, path.read_text(encoding="utf-8"))])
    reporter.check("适配器源码里没有本机事实（绝对路径 / 串口名）", not hits, "；".join(hits))


def check_normal_path(
    reporter: Reporter,
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    device_identity: Mapping[str, Any] | None = None,
) -> None:
    report, _executor = scenario(
        declaration, root, script=passing_script(declaration), identity=device_identity
    )
    if report.result != "pass":
        for row in report.steps:
            print(f"    · {row.step:<11} {row.status:<11} {row.detail}")
    reporter.check(f"{declaration.adapter_id}：正常路径 PASS",
                   report.result == "pass", report.result)
    reporter.check("正常路径：每一步都过了",
                   all(row.status in ("pass", "skipped") for row in report.steps),
                   "；".join(f"{row.step}={row.status}" for row in report.steps))


def check_safety_gate(
    reporter: Reporter, declaration: contract_mod.HardwareDeclaration, root: Path
) -> None:
    report, executor = scenario(
        declaration, root, script=passing_script(declaration),
        confirm_flash=False, allow_overwrite=False,
    )
    flash = report.step("flash")
    reporter.check("没有显式同意 ⇒ 拒烧",
                   flash is not None and flash.status == "refused",
                   flash.detail if flash else "")
    reporter.check("拒烧的故障码是稳定的",
                   flash is not None and flash.fault == "flash_refused")
    reporter.check("拒烧之后没有调用工具（一个字节都没写）",
                   executor.called(declaration.step("flash").adapter_id) == 0)
    reporter.check("整份操作的结论是 refused", report.result == "refused", report.result)


def check_known_failure(
    reporter: Reporter,
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    adapter_id: str,
    marker: str,
) -> None:
    script = dict(passing_script(declaration))
    script[adapter_id] = fake_mod.tool_run(code=1, output=f"{marker}\n")
    report, _executor = scenario(declaration, root, script=script, steps=("preflight", "build", "flash", "verify"))
    step = next(row for row in report.steps if row.adapter_id == adapter_id)
    reporter.check(f"{adapter_id}：已知失败 ⇒ 稳定故障码",
                   step.status == "fail" and step.fault in contract_mod.FAULT_CODES
                   and step.fault != contract_mod.FAULT_NONE, step.fault)
    reporter.check(f"{adapter_id}：失败详情自带处置",
                   bool(declaration.faults.explain(marker)), step.detail)


def check_zero_output_timeout(
    reporter: Reporter,
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    adapter_id: str,
) -> None:
    script = dict(passing_script(declaration))
    script[adapter_id] = fake_mod.tool_run(timed_out=True, output="")
    report, _executor = scenario(declaration, root, script=script)
    step = next(row for row in report.steps if row.adapter_id == adapter_id)
    reporter.check(f"{adapter_id}：零输出超时被认出来",
                   step.fault == "no_output_timeout", step.fault)


def check_identity_states(
    reporter: Reporter,
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    wrong_identity: Mapping[str, Any],
) -> None:
    report, _executor = scenario(declaration, root, identity=wrong_identity,
                                script=passing_script(declaration))
    verify = report.step("verify")
    reporter.check("身份不符 ⇒ 判失败", verify is not None and verify.status == "fail",
                   verify.detail if verify else "")
    reporter.check("身份不符的故障码是 identity_mismatch",
                   verify is not None and verify.fault == "identity_mismatch")

    report, _executor = scenario(declaration, root, identity=None,
                                script=passing_script(declaration))
    verify = report.step("verify")
    reporter.check("拿不到身份 ⇒ 未核验（不冒充通过）",
                   verify is not None and verify.status in ("skipped", "unsupported"),
                   verify.status if verify else "")
    reporter.check("未核验 ⇒ 整份操作不算完成",
                   report.result in ("incomplete", "skipped"), report.result)


def check_fuse(
    reporter: Reporter,
    declaration: contract_mod.HardwareDeclaration,
    root: Path,
    *,
    adapter_id: str,
) -> None:
    builder = next(row for row in declaration.steps if row.adapter_id == adapter_id)
    marker = declaration.faults.markers_for(
        declaration.faults.default_for(builder.step)
    )[0]
    script = dict(passing_script(declaration))
    script[adapter_id] = fake_mod.tool_run(code=1, output=f"{marker}\n")
    fuse = runner_mod.FuseState()
    calls = 0
    for _ in range(runner_mod.FUSE_CAPACITY):
        _report, executor = scenario(
            declaration, root, script=script, fuse=fuse,
            steps=("preflight", builder.step),
        )
        calls += executor.called(adapter_id)
    _report, executor = scenario(
        declaration, root, script=script, fuse=fuse, steps=("preflight", builder.step)
    )
    outcome = next(row for row in _report.steps if row.adapter_id == adapter_id)
    reporter.check("连续两次之后，第三次被熔断拦住",
                   outcome.status == "refused" and outcome.fault == "fuse_open",
                   outcome.fault)
    reporter.check("熔断发生在调工具之前",
                   calls == runner_mod.FUSE_CAPACITY and executor.called(adapter_id) == 0,
                   f"前几轮调了 {calls} 次")
    reporter.check("拒绝的话里带着怎么恢复", "fuse --clear" in outcome.detail)


def check_identity_from_build(
    reporter: Reporter, declaration: contract_mod.HardwareDeclaration, root: Path
) -> None:
    """“这次烧的就是这次编的”：期望值来自**本次构建的产物**。

    声明了 `identity_expectation.source` 的适配器才跑这一条（没有来源 = 只证明设备报得出来，
    那是另一回事，不能当成"核验过了"）。
    """
    expectation = declaration.identity_expectation
    source = expectation.source if expectation is not None else None
    if source is None:
        return
    import json

    path = Path(root) / source.artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    project = expectation.values.get("project", "")
    script = passing_script(declaration)

    path.write_text(json.dumps({source.key: 4242}), encoding="utf-8")
    expected = source.render(4242)
    report, _executor = scenario(
        declaration, root, script=script,
        identity={"project": project, "build_id": expected},
    )
    verify = report.step("verify")
    reporter.check("期望值来自本次构建的产物 ⇒ 核验通过",
                   verify is not None and verify.status == "pass",
                   verify.detail if verify else "")

    path.write_text(json.dumps({source.key: 1}), encoding="utf-8")
    report, _executor = scenario(
        declaration, root, script=script,
        identity={"project": project, "build_id": expected},
    )
    verify = report.step("verify")
    reporter.check("设备报的与本次构建不是一份 ⇒ 红",
                   verify is not None and verify.status == "fail"
                   and verify.fault == "identity_mismatch",
                   verify.detail if verify else "")

    path.unlink()
    report, _executor = scenario(
        declaration, root, script=script,
        identity={"project": project, "build_id": expected},
    )
    verify = report.step("verify")
    reporter.check("产物读不到 ⇒ 记成未比（不当成失败，也不冒充分过）",
                   verify is not None and verify.status == "pass"
                   and "没读到" in (verify.detail or ""),
                   verify.detail if verify else "")


def standard_suite(
    reporter: Reporter,
    declarations: tuple[contract_mod.HardwareDeclaration, ...],
    module: Any,
    *,
    registered: Mapping[str, Iterable[str]],
    coverage: Iterable[tuple[str, str]],
    wrong_identity: Mapping[str, Any],
) -> None:
    """一条硬件要跑的那几件事（参考适配器共用这一份）。"""
    check_declaration_shape(reporter, declarations)
    check_machine_checks(
        reporter, declarations, registered=registered,
        sources={Path(module.__file__).stem: Path(module.__file__).read_text(encoding="utf-8")},
    )
    check_isolation(reporter, declarations)
    check_opensource(reporter, module)
    check_bypass_has_teeth(reporter, declarations[0])

    primary = declarations[0]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(primary, root)
        check_normal_path(reporter, primary, root, device_identity={"project": f"{primary.target_id}-demo", "build_id": "0"})
        check_identity_from_build(reporter, primary, root)
        check_safety_gate(reporter, primary, root)
        check_identity_states(reporter, primary, root, wrong_identity=wrong_identity)
        for adapter_id, marker in coverage:
            check_known_failure(reporter, primary, root, adapter_id=adapter_id, marker=marker)
        check_zero_output_timeout(
            reporter, primary, root, adapter_id=primary.step("build").adapter_id
        )
        check_fuse(reporter, primary, root, adapter_id=primary.step("build").adapter_id)


def check_materialize_defaults() -> int:
    """夹具自检：假工具 + 中性契约能跑完五步（不依赖任何参考适配器）。"""
    reporter = Reporter(title="共用夹具")
    declaration = fake_mod.demo_declaration()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(declaration, root)
        check_normal_path(reporter, declaration, root,
                          device_identity={"project": "demo", "build_id": "0"})
        check_safety_gate(reporter, declaration, root)
    return reporter.finish()


if __name__ == "__main__":
    raise SystemExit(check_materialize_defaults())

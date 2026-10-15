#!/usr/bin/env python3
"""STM32 参考适配的契约测试（**不接硬件**）。

跑的是"每接一条硬件都要跑的那几件事"（见 `target_contract_harness.py`），
加上这一条适配**特有**的两条判据：

- 两条 profile（裸机 / RT-Thread）**不串用**工程入口与产物；
- 装配处的登记表盖住声明里每一个步骤（旁路检查才判得了）。

Run:  python tests\\test_stm32_target.py
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from target_contract_harness import (  # noqa: E402  (它会把 tools/ 与 targets/ 放进 sys.path)
    Reporter,
    check_fuse,
    check_known_failure,
    check_normal_path,
    check_safety_gate,
    check_zero_output_timeout,
    standard_suite,
)

import stm32_target as target  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "webconsole"))
import bench_config  # noqa: E402
import hwops_check as check_mod  # noqa: E402
import hwops_contract as contract_mod  # noqa: E402
import hwops_fake as fake_mod  # noqa: E402


def test_two_profiles_do_not_share_a_project() -> None:
    """裸机与 RT-Thread 是同一块板上的两份固件：**不许**共用工程入口 / 产物 / 判据。"""
    reporter = Reporter(title="STM32 两条 profile 的隔离")
    declarations = target.declarations()
    reporter.check("两条 profile 都声明了", len(declarations) == 2, str(len(declarations)))
    entries = {row.project_entry for row in declarations}
    reporter.check("两条 profile 的工程入口不同", len(entries) == 2, "、".join(sorted(entries)))
    artifacts: set[str] = set()
    shared = []
    for row in declarations:
        for step in row.steps:
            for item in step.artifacts:
                if item in artifacts:
                    shared.append(item)
                artifacts.add(item)
    reporter.check("两条 profile 不共用产物", not shared, "、".join(shared))
    reporter.check("隔离检查整体通过",
                   not check_mod.isolation_problems(declarations),
                   "；".join(check_mod.isolation_problems(declarations)))
    reporter.check("两条 profile 是同一个目标（共用适配器是对的）",
                   len({row.target_id for row in declarations}) == 1)
    return reporter.finish()


def test_declared_tools_and_registration() -> None:
    """登记表盖住每一个步骤；工具键都在声明里列过。"""
    reporter = Reporter(title="STM32 装配形状")
    for declaration in target.declarations():
        used = {step.adapter_id for step in declaration.steps}
        missing = sorted(used - set(target.ADAPTERS))
        reporter.check(f"{declaration.adapter_id}：步骤里的适配器都在登记表里",
                       not missing, "、".join(missing))
        reporter.check("工具键都列在 tools 里",
                       all(step.tool in declaration.tools or not step.tool
                           for step in declaration.steps))
        reporter.check("transport 的适配器也登记了",
                       declaration.transport.adapter_id in target.ADAPTERS)
    reporter.check("登记表里没有多余的适配器",
                   not (set(target.ADAPTERS)
                        - {step.adapter_id for row in target.declarations()
                           for step in row.steps}
                        - {row.transport.adapter_id for row in target.declarations()}),
                   str(target.ADAPTERS))
    return reporter.finish()


def test_wirings_and_bench_keys() -> None:
    """装配：交得出两条契约；台架键"配上/没配"分别有不同结论。"""
    reporter = Reporter(title="STM32 装配与台架键")
    bench_config.reset()
    wirings = target.hwops_wirings()
    reporter.check("交出两条装配", len(wirings) == 2, str(len(wirings)))
    reporter.check("每条装配都带受控适配器登记表",
                   all(row.registered for row in wirings))
    reporter.check("没有台架值时，装配如实说「站不住」",
                   all(row.problems() for row in wirings))
    reporter.check("没有台架值时，工程根留空（**不猜路径**）",
                   all(row.project_dir == "" for row in wirings))

    declarations = tuple(row.declaration for row in wirings)
    registered = {}
    for row in wirings:
        registered.setdefault(row.declaration.target_id, []).extend(row.registered)
    sources = {Path(target.__file__).stem: Path(target.__file__).read_text(encoding="utf-8")}

    configured = {
        row.adapter_id: {key: (True, "测试里当成已配置")
                         for key in row.declaration.bench_keys}
        for row in wirings
    }
    ok = check_mod.check_declarations(
        declarations, registered=registered, sources=sources, bench=configured
    )
    reporter.check("台架键都配上 ⇒ 没有判不过的项", not ok.problems, "；".join(ok.problems))

    empty = {row.adapter_id: {} for row in wirings}
    worse = check_mod.check_declarations(
        declarations, registered=registered, sources=sources, bench=empty
    )
    reporter.check("台架键没配上 ⇒ 如实报出来（不是静默通过）",
                   bool(worse.problems), str(len(worse.problems)))
    return reporter.finish()


def test_fake_end_to_end() -> None:
    """假工具走完五步 + 四条关键判据（每条硬件都要过的那一套）。"""
    reporter = Reporter(title="STM32 假工具端到端")
    declarations = target.declarations()
    primary = declarations[0]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(primary, root)
        check_normal_path(
            reporter, primary, root,
            device_identity={"project": f"{primary.target_id}-demo", "build_id": "0"},
        )
        check_safety_gate(reporter, primary, root)
        check_known_failure(
            reporter, primary, root,
            adapter_id="stm32.iar-build", marker="Total number of errors: 1",
        )
        check_known_failure(
            reporter, primary, root,
            adapter_id="stm32.cspy-download", marker="No MCU device found",
        )
        check_zero_output_timeout(
            reporter, primary, root, adapter_id="stm32.iar-build"
        )
        check_fuse(reporter, primary, root, adapter_id="stm32.iar-build")
    return reporter.finish()


def test_identity_protocol() -> None:
    """身份协议是**这块板的事实**：解析器只认那一行，认不出就返回 None。"""
    reporter = Reporter(title="STM32 身份协议")
    fields = target.parse_identity("boot @PWINFO project=mcu-a build_id=0x0000abcd")
    reporter.check("认得出自报的身份",
                   fields == {"project": "mcu-a", "build_id": "0x0000abcd"}, str(fields))
    reporter.check("认不出的行返回 None（不猜）", target.parse_identity("hello") is None)
    return reporter.finish()


def test_the_standard_suite() -> None:
    reporter = Reporter(title="STM32（共用判据）")
    standard_suite(
        reporter,
        target.declarations(),
        target,
        registered={target.TARGET_ID: target.ADAPTERS},
        coverage=(
            ("stm32.iar-build", "Total number of errors: 1"),
            ("stm32.cspy-download", "No MCU device found"),
        ),
        wrong_identity={"project": "some-other-board", "build_id": "0"},
    )
    return reporter.finish()


TESTS = (
    test_two_profiles_do_not_share_a_project,
    test_declared_tools_and_registration,
    test_wirings_and_bench_keys,
    test_fake_end_to_end,
    test_identity_protocol,
    test_the_standard_suite,
)


def main() -> int:
    failures = 0
    for test in TESTS:
        print(f"\n=== {test.__name__} ===")
        failures += test()
    print(f"\n{len(TESTS)} 组测试，{failures} 组有失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

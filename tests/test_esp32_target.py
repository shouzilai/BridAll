#!/usr/bin/env python3
"""ESP32 参考适配的契约测试（**不接硬件**）。

跑"每接一条硬件都要跑的那几件事"（见 `target_contract_harness.py`），加上这一条适配
**特有**的一条判据：**烧录后核验的期望值来自本次构建的产物**
（"这次烧的就是这次编的"，写死的常量证不了这件事）。

Run:  python tests\\test_esp32_target.py
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from target_contract_harness import (  # noqa: E402  (它会把 tools/ 与 targets/ 放进 sys.path)
    Reporter,
    check_fuse,
    check_identity_from_build,
    check_known_failure,
    check_normal_path,
    check_safety_gate,
    check_zero_output_timeout,
    standard_suite,
)

import esp32_target as target  # noqa: E402
import bench_config  # noqa: E402
import hwops_check as check_mod  # noqa: E402
import hwops_fake as fake_mod  # noqa: E402


def test_declared_shape() -> None:
    """登记表盖住每一个步骤；身份期望值来自构建产物（不是写死的常量）。"""
    reporter = Reporter(title="ESP32 装配形状")
    declaration = target.declaration()
    used = {step.adapter_id for step in declaration.steps}
    missing = sorted(used - set(target.ADAPTERS))
    reporter.check("步骤里的适配器都在登记表里", not missing, "、".join(missing))
    reporter.check("transport 的适配器也登记了",
                   declaration.transport.adapter_id in target.ADAPTERS)
    expectation = declaration.identity_expectation
    reporter.check("身份期望值声明了来源（来自本次构建的产物）",
                   expectation is not None and expectation.source is not None)
    reporter.check("期望值模板把整数写成设备会报的形态",
                   expectation is not None and expectation.source is not None
                   and expectation.source.render(4242) == "0x00001092",
                   expectation.source.render(4242) if expectation and expectation.source else "")
    return reporter.finish()


def test_wirings_and_bench_keys() -> None:
    """装配：没有台架值时如实说"站不住"；台架键配上/没配分别有不同结论。"""
    reporter = Reporter(title="ESP32 装配与台架键")
    bench_config.reset()
    wirings = target.hwops_wirings()
    reporter.check("交出一条装配", len(wirings) == 1, str(len(wirings)))
    reporter.check("没有台架值时，装配如实说「站不住」",
                   all(row.problems() for row in wirings))
    reporter.check("没有台架值时，工程根留空（**不猜路径**）",
                   all(row.project_dir == "" for row in wirings))

    declarations = tuple(row.declaration for row in wirings)
    registered = {row.declaration.target_id: list(row.registered) for row in wirings}
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
    """假工具走完五步 + 四条关键判据。"""
    reporter = Reporter(title="ESP32 假工具端到端")
    declaration = target.declaration()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(declaration, root)
        check_normal_path(
            reporter, declaration, root,
            device_identity={"project": "esp32-generic-demo", "build_id": "0x00000000"},
        )
        check_identity_from_build(reporter, declaration, root)
        check_safety_gate(reporter, declaration, root)
        check_known_failure(
            reporter, declaration, root,
            adapter_id="esp32.pio-upload", marker="Timed out waiting for packet header",
        )
        check_known_failure(
            reporter, declaration, root,
            adapter_id="esp32.esptool-verify", marker="A fatal error occurred",
        )
        check_zero_output_timeout(
            reporter, declaration, root, adapter_id="esp32.pio-build"
        )
        check_fuse(reporter, declaration, root, adapter_id="esp32.pio-build")
    return reporter.finish()


def test_the_standard_suite() -> None:
    reporter = Reporter(title="ESP32（共用判据）")
    standard_suite(
        reporter,
        target.declarations(),
        target,
        registered={target.TARGET_ID: target.ADAPTERS},
        coverage=(
            ("esp32.pio-upload", "Timed out waiting for packet header"),
            ("esp32.esptool-verify", "A fatal error occurred"),
        ),
        wrong_identity={"project": "some-other-board", "build_id": "0x00000000"},
    )
    return reporter.finish()


TESTS = (
    test_declared_shape,
    test_wirings_and_bench_keys,
    test_fake_end_to_end,
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

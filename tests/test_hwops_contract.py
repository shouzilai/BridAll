#!/usr/bin/env python3
"""硬件接入契约与统一入口（**core** 套件）。

这一套拿**假工具**与**假串口**把接入框架的每一条承诺各跑一遍（不接任何硬件）：

1. **契约自述站得住**：缺项、版本、步骤覆盖、能力四态的理由一次列全（配错就拒绝）。
2. **判据是数据**：退出码、日志里的完成标志、必须为零的统计行、必须出现的产物 ——
   由声明决定；同一条日志换个声明就换个结论。
3. **失败带下一步**：已知标记 → 稳定故障码 + 处置；不认得就如实 `unknown_failure`。
4. **零输出超时 ≠ 有事**：它和"超时但有输出"分开报，且都不冒充通过。
5. **身份四态**：`pass` / `mismatch` / 未核验 / 核验不了，一个都不许写成通过。
6. **熔断在动手之前**：同一故障码连续两次之后，第三次**不调用工具**就被拒。
7. **机器检查有牙**：旁路命令、按步骤名挑判据、跨契约串用、开源边界 —— 各配一份
   **做坏的输入**，证明它们不是摆设。
8. **生成即可跑**：脚手架产出的声明与契约测试当场可执行（本套验的是形状与自检）。

⚠️ 本套件是 **core 套件**：源码里不出现任何平台或产品字眼（连注释都不行）。
夹具一律中性：目录 `demo-*`、端口 `PORT-A`、工具 `fake-tool`。

Run:  python tests\\test_hwops_contract.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

CONSOLE_DIR = Path(__file__).resolve().parent.parent / "tools" / "webconsole"
for path in (str(CONSOLE_DIR), str(CONSOLE_DIR.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

import unicode_console  # noqa: E402

unicode_console.install()  # 管道/GBK 控制台下打印中文不再中途抛错

from webconsole import hwops_check as check_mod  # noqa: E402
from webconsole import hwops_contract as contract_mod  # noqa: E402
from webconsole import hwops_fake as fake_mod  # noqa: E402
from webconsole import hwops_runner as runner_mod  # noqa: E402
from webconsole import hwops_scaffold as scaffold_mod  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}{': ' + detail if detail else ''}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name}{': ' + detail if detail else ''}")


def _raises(call) -> bool:
    """这一句**当场报错**吗（声明配错就该在构造时炸，而不是留到跑的时候）。"""
    try:
        call()
    except Exception:  # noqa: BLE001 - 只要是报错就算，具体类型由那句自己保证
        return True
    return False


# ==================================================================== 夹具
def demo() -> contract_mod.HardwareDeclaration:
    return fake_mod.demo_declaration()


def rig(root: Path, script=None, *, identity=None, port: str = "PORT-A", lines=("demo line",)):
    """一次操作的现场：假执行器 + 假串口 + 解析器（工具指向平台自带的假工具）。"""
    declaration = demo()
    resolve = fake_mod.demo_resolver(root, port=port)
    executor = fake_mod.FakeExecutor(script=script or fake_mod.default_script())
    device = fake_mod.FakeDevice(identity=identity, lines=tuple(lines))
    for key, value in list(resolve.tools.items()):
        assert key == fake_mod.FAKE_TOOL_KEY, key
        assert Path(value).is_file(), value
    return declaration, resolve, executor, device


def run(declaration, resolve, executor, device, **kwargs):
    options = {
        "executor": executor,
        "resolver": resolve,
        "confirm_flash": True,
        "allow_overwrite": True,
        "identity_probe": getattr(device, "identity_probe", None),
        "debug_probe": getattr(device, "probe", None),
    }
    options.update(kwargs)
    return runner_mod.run_operation(declaration, **options)


# ============================================================ 1. 契约自述
def test_declaration_selfcheck() -> None:
    declaration = demo()
    check("参考契约本身没有自述问题", not declaration.problems(),
          "；".join(declaration.problems()))
    check("契约覆盖了全部步骤", not contract_mod.missing_steps(declaration),
          str(contract_mod.missing_steps(declaration)))
    check("契约能说清自己是什么", "demo-board-bm" in contract_mod.summary_of(declaration))

    # 缺项要一次列全（配错就拒绝，不部分采用）
    # ⚠️ 用 `strict_registration=False`：这里判的是**契约自己**说不说得清，
    #    而不是它有没有登记到平台的分层表里（那是接入做完之后的动作）。
    broken = [
        replace(declaration, adapter_id=""),
        replace(declaration, project_entry=""),
        replace(declaration, contract_version=contract_mod.CONTRACT_VERSION + 1),
        replace(declaration, steps=()),
        replace(declaration, tools=()),
        replace(declaration, capabilities=()),
    ]
    for index, row in enumerate(broken):
        found = check_mod.declaration_problems(row, strict_registration=False)
        check(f"坏契约 #{index + 1} 被认出来", bool(found), "；".join(found))

    # 一个会抛错的声明：步骤重复 / 未知占位符 / 缺产物
    for label, kwargs in (
        ("步骤重复", dict(step="build")),
        ("未知占位符", dict(step="flash", args=("{unknown}",))),
    ):
        ok = False
        try:
            contract_mod.StepSpec(adapter_id="demo.x", label="x", **kwargs)
        except ValueError:
            ok = True
        check(f"非法步骤（{label}）当场报错", ok)

    try:
        contract_mod.StepSpec(step="build", adapter_id="demo.x", label="x", args=("a",))
        check("build 缺产物当场报错", False)
    except ValueError:
        check("build 缺产物当场报错", True)


# ============================================================ 2. 判据是数据
def test_criteria_are_data() -> None:
    criterion = demo().step("build").success
    passing = contract_mod.judge(
        criterion, returncode=0, output=fake_mod.build_output(), artifacts_total=2
    )
    check("退 0 + 日志齐 + 产物在 ⇒ 通过", passing.ok, passing.detail)

    nonzero = contract_mod.judge(
        criterion, returncode=0, output=fake_mod.build_output(errors=3), artifacts_total=2
    )
    check("退 0 但统计行非零 ⇒ 失败", not nonzero.ok, nonzero.detail)

    silent = contract_mod.judge(criterion, returncode=0, output="", artifacts_total=2)
    check("数不到统计行 ⇒ 无法判定（不是没事）", not silent.ok and silent.fault == "unknown_failure",
          silent.detail)

    unlinked = contract_mod.judge(
        criterion, returncode=0, output=fake_mod.build_output(linked=False), artifacts_total=2
    )
    check("缺完成标志 ⇒ 失败", not unlinked.ok, unlinked.detail)

    missing = contract_mod.judge(
        criterion, returncode=0, output=fake_mod.build_output(),
        missing_artifacts=["out/x.bin"], artifacts_total=1,
    )
    check("产物没出现 ⇒ 失败", not missing.ok, missing.detail)

    # 同一条输出、换一个声明 ⇒ 换个结论（证明判据真的来自声明）
    lax = contract_mod.SuccessCriterion(kind="exit")
    check("只看退出码的声明下同一条输出算过",
          contract_mod.judge(lax, returncode=0, output="").ok)

    # 非法判据形状：exit 不许带日志标记
    try:
        contract_mod.SuccessCriterion(kind="exit", success_markers=("x",))
        check("exit 带日志标记被拒", False)
    except ValueError:
        check("exit 带日志标记被拒", True)


# ============================================================ 3. 失败带下一步
def test_failures_carry_next_step() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(demo(), root)

        # 已知标记
        declaration, resolve, executor, device = rig(
            root,
            fake_mod.default_script(**{
                "demo.flash": fake_mod.tool_run(
                    code=1, output=fake_mod.flash_output(done=False, fatal="probe stuck")
                )
            }),
        )
        report = run(declaration, resolve, executor, device)
        flash = report.step("flash")
        check("已知标记 ⇒ 稳定故障码",
              flash is not None and flash.fault == "flash_failed", flash.fault if flash else "")
        check("已知标记 ⇒ 处置出现在详情里",
              flash is not None and "拔插" in flash.detail, flash.detail if flash else "")
        check("已知标记 ⇒ 认出是哪一类", declaration.faults.classify("Fatal error: x") is not None)

        # 不认得的标记（统计行还在 ⇒ 类别由**声明**说，不自己编一类）
        declaration, resolve, executor, device = rig(
            root,
            fake_mod.default_script(**{
                "demo.build": fake_mod.tool_run(code=1, output=fake_mod.build_output(errors=2))
            }),
        )
        report = run(declaration, resolve, executor, device)
        build = report.step("build")
        check("失败类别来自声明（不自己编一类）",
              build is not None and build.fault == "build_failed", build.fault if build else "")
        check("不认得标记时只给声明里的通用建议（不编原因）",
              declaration.faults.explain("something brand new") == declaration.faults.unknown_advice)
        check("认不认得说得出（返回 None 而不是随便指一条）",
              declaration.faults.classify("something brand new") is None)

        # 完全无法判定（连统计行都没有）⇒ unknown_failure，而不是“构建失败”
        declaration, resolve, executor, device = rig(
            root,
            fake_mod.default_script(**{
                "demo.build": fake_mod.tool_run(code=1, output="something brand new\n")
            }),
        )
        report = run(declaration, resolve, executor, device)
        build = report.step("build")
        check("数不到统计行 ⇒ unknown_failure（不冒充知道原因）",
              build is not None and build.fault == "unknown_failure", build.fault if build else "")

        # 超时的两种
        declaration, resolve, executor, device = rig(
            root, fake_mod.default_script(**{"demo.build": fake_mod.tool_run(timed_out=True)})
        )
        report = run(declaration, resolve, executor, device)
        check("零输出超时被认出来",
              (report.step("build") or None) is not None
              and report.step("build").fault == "no_output_timeout")

        declaration, resolve, executor, device = rig(
            root,
            fake_mod.default_script(**{
                "demo.build": fake_mod.tool_run(timed_out=True, output="half a line")
            }),
        )
        report = run(declaration, resolve, executor, device)
        check("超时但有输出 ⇒ 与零输出分开",
              report.step("build").fault == "timeout", report.step("build").fault)

        # 命令起不来
        declaration, resolve, executor, device = rig(
            root, fake_mod.default_script(**{"demo.build": fake_mod.tool_run(error="no such file")})
        )
        report = run(declaration, resolve, executor, device)
        check("起不来的命令 ⇒ 失败并说清",
              report.step("build").status == "fail"
              and "no such file" in report.step("build").detail)


# ============================================================ 4. 安全闸门
def test_flash_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(demo(), root)
        declaration, resolve, executor, device = rig(root)
        report = run(declaration, resolve, executor, device,
                     confirm_flash=False, allow_overwrite=False)
        flash = report.step("flash")
        check("没有显式同意 ⇒ 拒烧", flash is not None and flash.status == "refused",
              flash.status if flash else "")
        check("拒烧的故障码是稳定的", flash is not None and flash.fault == "flash_refused")
        check("拒烧之后一个字节都没写（没调用工具）", executor.called("demo.flash") == 0)
        check("整份操作的结论是 refused", report.result == "refused", report.result)


# ============================================================ 5. 身份四态
def test_identity_four_states() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(demo(), root)

        declaration, resolve, executor, device = rig(
            root, None, identity={"project": "demo", "build_id": "0"}
        )
        check("身份相符 ⇒ pass", run(declaration, resolve, executor, device).step("verify").status == "pass")

        declaration, resolve, executor, device = rig(
            root, None, identity={"project": "other", "build_id": "0"}
        )
        report = run(declaration, resolve, executor, device)
        verify = report.step("verify")
        check("身份不符 ⇒ fail", verify.status == "fail" and verify.fault == "identity_mismatch",
              verify.detail)

        declaration, resolve, executor, device = rig(root, None, identity=None)
        verify = run(declaration, resolve, executor, device).step("verify")
        check("拿不到 ⇒ 未核验（不冒充通过）",
              verify.status in ("skipped", "unsupported"), verify.status)

        declaration, resolve, executor, device = rig(
            root, None, identity={"software_version": "1"}
        )
        verify = run(declaration, resolve, executor, device).step("verify")
        check("回答里没有可比字段 ⇒ 未核验",
              verify.status == "skipped" and verify.fault == "identity_unavailable",
              verify.detail)

        no_expectation = replace(demo(), identity_expectation=None)
        del no_expectation
        verdict = contract_mod.judge_identity(None, {"project": "demo"})
        check("没声明要比什么 ⇒ 核验不了（不是通过）", verdict.status == "unsupported")
        expect_but_empty = contract_mod.IdentityExpectation(fields=())

        check("声明了但一条字段都没有 ⇒ 同样核验不了",
              contract_mod.judge_identity(expect_but_empty, {"project": "demo"}).status
              == "unsupported")


def test_the_unverified_says_which_kind_of_unavailable() -> None:
    """「没拿到」要分三档报：打不开口 / 一行没收到 / 有行但没有身份句。

    ⚠️ 三档的下一步完全不同（打不开口是**环境**问题，多半是别的进程占着它）——
       2026-10-13 真机踩到：实因是另一个工作台占着那个通信口，屏幕却只说「没拿到设备自报」，
       要靠人手逐层排除（读串口 / 搜固件 / 查进程）才分清，等于把 10 分钟诊断成本推给每个人。
    """
    # ⚠️ 每个用例都换一个**新现场**：同一条链上连续 `run` 两次会被 preflight 挡住
    #    （第一次 run 已经把产物 / 输入消费掉）—— 与本次改动无关，是既有的夹具约束。
    cases = (
            (
                runner_mod.IdentityProbe(
                    fault="transport_unavailable",
                    detail="打不开通信口（COMx）⇒ 未核验：PermissionError",
                ),
                "unsupported", "transport_unavailable", "打不开通信口",
            ),
            (
                runner_mod.IdentityProbe(
                    detail="口打得开（COMx），但 3 秒里**一行都没收到** ⇒ 未核验",
                ),
                "skipped", "identity_unavailable", "一行都没收到",
            ),
            (
                runner_mod.IdentityProbe(
                    detail="口打得开、也收到了 2 行，但**没有一句**能被身份解析器认出来",
                ),
                "skipped", "identity_unavailable", "没有一句",
            ),
            (None, "skipped", "identity_unavailable", "没拿到设备自报"),
        )
    for probed, status, fault, needle in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_mod.materialize(demo(), root)
            declaration, resolve, executor, device = rig(root, None)
            verify = run(
                declaration, resolve, executor, device,
                identity_probe=lambda probe=probed: probe,
            ).step("verify")
            check(
                f"未核验分档「{needle}」⇒ {status} / {fault}",
                verify.status == status and verify.fault == fault
                and needle in str(verify.detail),
                f"{verify.status} / {verify.fault} / {verify.detail}",
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(demo(), root)
        declaration, resolve, executor, device = rig(root, None)
        check("旧的「返回 Mapping 或 None」照样能跑（向后兼容）",
              run(declaration, resolve, executor, device,
                  identity_probe=lambda: {"project": "demo"}).step("verify").status == "pass")


# ============================================================ 6. 熔断
def test_fuse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_mod.materialize(demo(), root)
        script = fake_mod.default_script(**{
            "demo.build": fake_mod.tool_run(code=1, output=fake_mod.build_output(errors=1))
        })
        fuse = runner_mod.FuseState()
        calls = 0
        for _ in range(2):
            declaration, resolve, executor, device = rig(root, script)
            run(declaration, resolve, executor, device, fuse=fuse,
                steps=("preflight", "build"))
            calls += executor.called("demo.build")
        check("前两次真的调了工具（判据红了两次）", calls == 2, f"调了 {calls} 次")
        check("账本记着两次", fuse.count("demo.build", "build_failed") == runner_mod.FUSE_CAPACITY)

        declaration, resolve, executor, device = rig(root, script)
        report = run(declaration, resolve, executor, device, fuse=fuse,
                     steps=("preflight", "build"))
        build = report.step("build")
        check("第三次在动手之前被拒",
              build.status == "refused" and build.fault == "fuse_open", build.detail)
        check("被拒时没有调用工具", executor.called("demo.build") == 0)
        check("拒绝的话里带着怎么恢复", "fuse --clear" in build.detail)

        # 连续的含义：中间成功一次就把账清零
        fuse.succeed("demo.build")
        check("成功一次 ⇒ 账清零", fuse.blocked("demo.build") == "")
        fuse.record("demo.build", "build_failed")
        check("清零之后重新计数", fuse.count("demo.build", "build_failed") == 1)
        # 换一种故障码 ⇒ 上一种的账不算连续
        fuse.record("demo.build", "unknown_failure")
        check("换了故障码 ⇒ 上一种的连续被打断",
              fuse.count("demo.build", "build_failed") == 0)
        fuse.clear("demo.build")
        check("显式清除之后不再拦", fuse.blocked("demo.build") == "")


# ============================================================ 7. 机器检查
def test_machine_checks() -> None:
    declaration = demo()
    registered = [step.adapter_id for step in declaration.steps]
    check("登记齐 ⇒ 没有旁路问题",
          not check_mod.bypass_problems(declaration, registered))
    broken = check_mod.bypass_problems(declaration, ("demo.build",))
    check("未登记的适配器 ⇒ 报旁路", len(broken) == 3, "；".join(broken))
    check("登记表空 ⇒ 如实说判不了",
          bool(check_mod.bypass_problems(declaration, ())))

    # 判据的两个来源：做坏一份源码，必须红
    bad_source = {
        "demo_target": (
            "def verdict(step, code, log):\n"
            "    ok, note = False, ''\n"
            "    if step == 'build':\n"
            "        ok, note = succeeded(code, log)\n"
            "    return ok\n"
        )
    }
    found = check_mod.criterion_source_problems(bad_source)
    check("按步骤名挑判据 ⇒ 红", len(found) == 1, "；".join(found))
    good_source = {
        "demo_target": (
            "def verdict(step, criterion, code, log):\n"
            "    return judge(criterion, returncode=code, output=log)\n"
            "    # 判据由声明给；这里没有任何按步骤名的分支\n"
        )
    }
    check("判据由声明给 ⇒ 不红", not check_mod.criterion_source_problems(good_source))
    commented = {
        "demo_target": (
            "# 旧写法（说明用）：\n"
            "#   if step == 'build':\n"
            "#       ok = succeeded(code, log)\n"
            "def verdict(criterion, code, log):\n"
            "    return judge(criterion, returncode=code, output=log)\n"
        )
    }
    check("注释里引到旧写法不算（判据别盯字面量）",
          not check_mod.criterion_source_problems(commented))

    # 两步共用一套判据
    shared = contract_mod.SuccessCriterion(kind="log", success_markers=("done",))
    twinned = replace(
        declaration,
        steps=(
            replace(declaration.step("build"), success=shared),
            replace(declaration.step("flash"), success=shared),
        ),
    )
    check("两步共用同一套判据 ⇒ 红",
          bool(check_mod.duplicate_criteria_problems(twinned)))
    check("通用形状（只看退出码）不算抄",
          not check_mod.duplicate_criteria_problems(declaration))

    # 跨契约不串用
    left = replace(declaration, adapter_id="demo-a", target_id="t1")
    # 同一块板的**第二份固件**：换工程入口、换产物，但**共用同一条目标适配器**。
    right = replace(
        declaration, adapter_id="demo-b", target_id="t1",
        project_entry="project/second.ini",
        steps=tuple(
            replace(
                step,
                inputs=tuple("project/second.ini" if item == "project/entry.ini" else item
                             for item in step.inputs),
                artifacts=tuple(item.replace("out/", "out-b/") for item in step.artifacts),
            )
            for step in declaration.steps
        ),
    )
    check("同一目标、共用同一条适配器 ⇒ 允许（那是**同一块板**的两份固件）",
          not check_mod.isolation_problems([left, right]),
          "；".join(check_mod.isolation_problems([left, right])))
    other_target = replace(declaration, adapter_id="demo-c", target_id="t2",
                           project_entry="project/other.ini")
    crossed = check_mod.isolation_problems([left, other_target])
    check("跨目标共用产物/适配器 ⇒ 红", bool(crossed), "；".join(crossed))
    same_entry = check_mod.isolation_problems(
        [left, replace(declaration, adapter_id="demo-d", project_entry=left.project_entry)]
    )
    check("两份契约用同一个工程入口 ⇒ 红", any("工程入口" in item for item in same_entry),
          "；".join(same_entry))

    # 完整性：缺步骤、能力无理由
    thin = replace(declaration, capabilities=())
    check("没声明能力 ⇒ 红", bool(check_mod.declaration_problems(thin)))
    try:
        contract_mod.CapabilityResult("decode", "unsupported", "")
        check("做不到却不给理由 ⇒ 当场报错", False)
    except ValueError:
        check("做不到却不给理由 ⇒ 当场报错", True)

    no_expect = replace(declaration, identity_expectation=None)
    check("声明了 verify 却没有可比字段 ⇒ 红",
          any("identity_expectation" in item
              for item in check_mod.declaration_problems(no_expect)))

    report = check_mod.check_declarations(
        [declaration], registered={"demo-target": registered},
        sources=good_source, strict_registration=False,
    )
    check("整份检查：干净的契约通过", report.ok, "；".join(report.problems))
    strict = check_mod.declaration_problems(declaration)
    check("严格登记下：没登记进平台就红（这是接入做完之后的动作）",
          any("分层表" in item for item in strict), "；".join(strict))


def test_opensource_scan() -> None:
    clean = [("demo/file.py", "path = 'tools/thing.py'\n")]
    check("没有本机事实 ⇒ 干净", not check_mod.opensource_problems(clean))
    # ⚠️ 串口名的**字面量**不能出现在本文件里（core 套件里一处平台标识都不许有），
    #    所以在这里拼出来再用 —— 扫描器看的是文本，拼出来的照样能命中。
    port_like = "CO" + "M7"
    separator = chr(92)
    absolute = "D:" + separator + "some" + separator + "place"
    dirty = [
        ("demo/file.py", f"home = '{absolute}'\n"),
        ("demo/file.py", f"port = '{port_like}'\n"),
    ]
    hits = check_mod.opensource_problems(dirty)
    check("绝对路径 ⇒ 报出来", any("absolute_path" in item for item in hits))
    check("串口名 ⇒ 报出来", any("serial_port" in item for item in hits), "；".join(hits))
    allowed = check_mod.opensource_problems(
        dirty,
        allow=[("demo/file.py", "absolute_path"), ("demo/file.py", "serial_port"),
               ("demo/file.py", "port_default")],
    )
    check("显式豁免（点名到形状）之后不报", not allowed)
    partial = check_mod.opensource_problems(
        dirty, allow=[("demo/file.py", "absolute_path")]
    )
    check("只豁免一种形状，另两种照旧报",
          len(partial) == 2 and all(
              ("serial_port" in item or "port_default" in item) for item in partial),
          "；".join(partial))
    # 「真的写死了端口」与「文档在讲不要写死端口」必须分得开：前者是**赋值形状**。
    hard = [("demo/config.py", "DEFAULTS = {'port': '" + port_like + "', 'baud': 115200}\n")]
    prose = [("demo/guide.md", "别写成 " + port_like + " —— 那是本机事实。\n")]
    hard_hits = check_mod.opensource_problems(hard)
    check("赋值形状（键值对里写死端口）⇒ 报 port_default",
          any("port_default" in item for item in hard_hits), "；".join(hard_hits))
    prose_hits = check_mod.opensource_problems(prose)
    check("句子里的举例 ⇒ 不报 port_default（只报泛指的那条）",
          not any("port_default" in item for item in prose_hits), "；".join(prose_hits))
    # ⚠️ 判据自己的源码不许命中自己的判据：判据文本写在源码里，
    #    一旦整段照抄，框架面自扫就会红（2026-10-13 真发生过一次）。
    with open(check_mod.__file__, encoding="utf-8") as handle:
        own_source = handle.read()
    self_hits = [
        item for item in check_mod.opensource_problems([("hwops_check.py", own_source)])
        if "port_default" in item
    ]
    check("判据自己的源码不自匹配（port_default）", not self_hits, "；".join(self_hits))


def test_prepared_command_handoff() -> None:
    """argv 由**目标层装配**的那类步骤：命令必须真的被交下去、真的被用上。

    ⚠️ 这条判据是补的（2026-10-13 真机踩到）：某个目标的烧录就是这种步骤，而契约套件
    此前**一条都没覆盖它** —— 于是"装配处拿到了命令、入口却没往下传"这个缺口
    一路跑到真机才发现（`list` 里明明有命令，`run` 说"装配缺了一半"）。
    """
    import ast

    prepared_argv = ("demo-flash", "--port", "PORT-A", "--verify")
    declaration = demo()
    steps = tuple(
        replace(step, adapter_id="demo.download", tool="", args=(), prepared=True)
        if step.step == "flash" else step
        for step in declaration.steps
    )
    declaration = replace(declaration, steps=steps)

    # ① 交下去了 ⇒ 用的是**装配处给的那条 argv**（不是声明里那条）。
    with tempfile.TemporaryDirectory() as tmp:
        base, resolve, executor, device = rig(Path(tmp))
        declaration = replace(declaration, bench_keys=base.bench_keys,
                              project_entry=base.project_entry)
        fake_mod.materialize(declaration, Path(tmp))
        command = contract_mod.PreparedCommand(
            argv=prepared_argv, cwd="", timeout_s=7.0
        )
        report = run(declaration, resolve, executor, device,
                     steps=("preflight", "flash"),
                     commands={"demo.download": command})
        used = [spec for spec in executor.calls if spec.adapter_id == "demo.download"]
        check("装配处给的命令真的被用了",
              bool(used) and tuple(used[-1].argv) == prepared_argv,
              f"调用现场={[tuple(spec.argv) for spec in used]}")
        check("装配处给的超时也带过去了",
              bool(used) and float(used[-1].timeout_s) == 7.0,
              f"timeout={None if not used else used[-1].timeout_s}")

        # ② 没交 ⇒ 如实报"装配缺了一半"，不假装跑过。
        empty = rig(Path(tmp))
        fake_mod.materialize(declaration, Path(tmp))
        quiet = run(declaration, empty[1], empty[2], empty[3],
                    steps=("preflight", "flash"))
        outcome = [row for row in quiet.steps if row.step == "flash"][0]
        check("没交命令 ⇒ 那一步不算通过", outcome.status != "pass", outcome.status)
        check("没交命令 ⇒ 说清楚是装配缺了一半（不是工具坏了）",
              "装配" in (outcome.detail or ""), outcome.detail)

    # ③ 入口那条接缝：`run_operation(...)` 必须带着 `commands`（漏了就等于①从未生效）。
    source = (CONSOLE_DIR.parent / "hwops.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_operation"
    ]
    handed = [
        node for node in calls
        if any(kw.arg == "commands" for kw in node.keywords)
    ]
    check("统一入口把装配命令交下去了", bool(calls) and len(handed) == len(calls),
          f"{len(handed)}/{len(calls)} 处带 commands")


def test_identity_expectation_comes_from_the_build() -> None:
    """“这次烧的就是这次编的”：期望值必须来自**本次构建的产物**（不是写死的常量）。

    ⚠️ 写死的期望值只能证明“设备报得出某个值”。这里的判据三件事都要钉住：
    ① 读到了 ⇒ 真拿它去比（不符就红）；② 读不到 ⇒ 明说那几个字段**没比**；
    ③ 没构建（或构建没过）⇒ 不偷偷拿上一次的产物顶上。
    """
    source = contract_mod.IdentitySource(
        artifact="out/identity.json", key="build_id32", field="build_id",
        template="0x{value:08x}",
    )
    check("来源缺项 ⇒ 当场报错", _raises(lambda: contract_mod.IdentitySource(
        artifact="", key="k", field="f")))
    check("template 里没有 {value} ⇒ 当场报错", _raises(
        lambda: contract_mod.IdentitySource(
            artifact="a.json", key="k", field="f", template="0x08x")))
    check("格式化照声明做", source.render(4242) == "0x00001092", source.render(4242))

    expectation = contract_mod.IdentityExpectation(
        fields=("project", "build_id"), values={"project": "demo"}, source=source,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "out").mkdir(parents=True, exist_ok=True)
        (root / "out" / "identity.json").write_text(
            json.dumps({"build_id32": 4242}), encoding="utf-8"
        )
        resolve = fake_mod.demo_resolver(root, port="PORT-A")

        # ① 产物在 ⇒ 期望值被补上，且**真的拿去比**。
        filled = runner_mod.resolve_identity_source(expectation, resolve)
        check("期望值从产物里读出来了",
              filled is not None and filled.values.get("build_id") == "0x00001092",
              str(None if filled is None else filled.values))
        same = contract_mod.judge_identity(filled, {"project": "demo", "build_id": "0x00001092"})
        check("设备报的与本次构建一致 ⇒ 通过", same.status == "pass", same.detail)
        other = contract_mod.judge_identity(filled, {"project": "demo", "build_id": "0xdeadbeef"})
        check("设备报的和本次构建不是一份 ⇒ 红",
              other.status == "fail" and other.fault == "identity_mismatch", other.detail)

        # ② 产物不在 ⇒ 那几个字段**没比**，而且要说得出来为什么。
        (root / "out" / "identity.json").unlink()
        blind = runner_mod.resolve_identity_source(expectation, resolve)
        check("产物读不到 ⇒ 记成未比", blind is not None and blind.unresolved == ("build_id",),
              str(None if blind is None else blind.unresolved))
        quiet = contract_mod.judge_identity(blind, {"project": "demo", "build_id": "0xdeadbeef"})
        check("未比的字段不会把结论变成失败", quiet.status == "pass", quiet.detail)
        check("未比的那一个字段在结论里点名了",
              "build_id" in quiet.detail and "没读到" in quiet.detail, quiet.detail)

    # ③ 没请求构建 ⇒ 不去读产物（不会拿上一次的清单冒充"本次"）。
    declaration = demo()
    steps = tuple(
        replace(step, adapter_id="demo.download", tool="", args=(), prepared=True)
        if step.step == "flash" else step
        for step in declaration.steps
    )
    declaration = replace(
        declaration, steps=steps,
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"), values={"project": "demo"}, source=source,
        ),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "out").mkdir(parents=True, exist_ok=True)
        (root / "out" / "identity.json").write_text(
            json.dumps({"build_id32": 4242}), encoding="utf-8"
        )
        base, resolve, executor, device = rig(
            root,
            script={**fake_mod.default_script(),
                    "demo.download": fake_mod.tool_run(output="flash done\n")},
            identity={"project": "demo", "build_id": "0xdeadbeef"},
        )
        fake_mod.materialize(declaration, root)
        report = run(declaration, resolve, executor, device,
                     steps=("preflight", "flash", "verify"),
                     commands={"demo.download": contract_mod.PreparedCommand(
                         argv=("demo-flash",), cwd="", timeout_s=5.0)})
        outcome = [row for row in report.steps if row.step == "verify"][0]
        check("没有构建这一步 ⇒ 不拿旧产物当“本次”的期望值",
              outcome.status == "pass"
              and "build_id" in (outcome.detail or "")
              and "没有期望值" in (outcome.detail or ""),
              f"{outcome.status} / {outcome.detail}")


# ============================================================ 8. 脚手架
def test_scaffold_roundtrip() -> None:
    import importlib.util
    import os

    with tempfile.TemporaryDirectory() as tmp:
        result = scaffold_mod.scaffold_adapter(
            Path(tmp), adapter_id="demo-board-x", label="演示板 X", layer="target-demo",
        )
        check("生成声明文件", result.declaration.is_file())
        check("生成契约测试", result.test.is_file())
        check("生成接入清单", result.readme.is_file())
        text = result.declaration.read_text(encoding="utf-8")
        check("生成物不写死本机路径", "D:" + chr(92) not in text)
        readme = result.readme.read_text(encoding="utf-8")
        for item in ("build 判据", "烧录判据", "故障知识", "能力四态", "台架键"):
            check(f"清单里有「{item}」", item.split()[0] in readme)

        # 生成物**现在就能加载**（靠 PW_PLATFORM_TOOLS 找平台），而且形状合法。
        os.environ["PW_PLATFORM_TOOLS"] = str(CONSOLE_DIR)
        spec = importlib.util.spec_from_file_location("_hwops_generated", result.declaration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        generated = module.declaration()
        problems = check_mod.declaration_problems(generated, strict_registration=False)
        check("生成物的声明形状合法", not problems, "；".join(problems))
        check("生成物解析器指向平台自带的假工具",
              Path(module.resolver(Path(tmp), tools={"demo-tool": fake_mod.FAKE_TOOL_FILE})
                   .tools["demo-tool"]).is_file())

        # ⚠️ 生成物还得**能被统一入口驱动**：光有一份声明不够 —— 入口要的是
        #    `hwops_wirings()`（否则只能跑它自带的那份测试，`run --adapter` 找不到它）。
        check("生成物交出了统一入口要的装配",
              callable(getattr(module, "hwops_wirings", None)))
        wirings = module.hwops_wirings() if callable(
            getattr(module, "hwops_wirings", None)) else []
        check("装配就一条，且指出了它自己是谁",
              len(wirings) == 1
              and wirings[0].declaration.adapter_id == generated.adapter_id,
              str([row.declaration.adapter_id for row in wirings]))
        check("装配自带受控适配器登记表（旁路检查才判得了）",
              bool(wirings) and bool(wirings[0].registered),
              str(None if not wirings else wirings[0].registered))
        check("登记表盖住了声明里每一个步骤",
              bool(wirings) and all(
                  step.adapter_id in wirings[0].registered for step in generated.steps
              ),
              str(None if not wirings else sorted(
                  step.adapter_id for step in generated.steps)))
        wired = [] if not wirings else wirings[0].problems()
        check("这条装配站得住（没有旁路 / 没有缺件）", not wired, "；".join(wired))

        # 骨架的内存版：放进 core 必须被挡住
        for layer, expect in (("target-demo", False), ("core", True)):
            found = check_mod.declaration_problems(
                scaffold_mod.skeleton_declaration(layer=layer), strict_registration=False
            )
            check(f"骨架放进 {layer} ⇒ {'红' if expect else '不因层名而红'}",
                  (any("core" in item for item in found) if expect
                   else not any("core" in item for item in found)),
                  "；".join(found))


def test_the_check_says_what_it_could_not_judge() -> None:
    """判不了就说判不了（**不许静默跳过**）。

    ⚠️ 这条是验收时被一个新 Agent 抓到的：从前缺输入（没有登记表 / 没有源码）时那一项
    **静默不跑**，屏幕上却写着"通过" ⇒ `check --module` 与 `check --targets` 各覆盖
    一半，而人以为两边都盖住了（2026-10-13，实测）。
    """
    declaration = demo()
    silent = check_mod.check_declarations([declaration], strict_registration=False)
    check("没有登记表 ⇒ 明说旁路判不了",
          any("旁路" in item for item in silent.unjudged), "；".join(silent.unjudged))
    check("没有源码 ⇒ 明说“按步骤名挑判据”判不了",
          any("按步骤名" in item for item in silent.unjudged), "；".join(silent.unjudged))
    check("判不了是提示、不是失败", not silent.problems, "；".join(silent.problems))

    known = {declaration.target_id: [step.adapter_id for step in declaration.steps]}
    complete = check_mod.check_declarations(
        [declaration], registered=known, sources={declaration.module: "x = 1\n"},
        strict_registration=False,
    )
    check("输入齐了 ⇒ 没有“判不了”",
          not complete.unjudged, "；".join(complete.unjudged))

    # ⚠️ 串用检查不许因为"只拿到一份契约"而跳过：**同一份**契约里两处共用同一个产物
    #    也是串用（真机上就是这么撞到自己人：同一个产物被声明了两遍）。
    doubled = replace(
        declaration,
        steps=tuple(
            replace(step, artifacts=("out/one.elf",))
            if step.step in ("build", "flash") else step
            for step in declaration.steps
        ),
    )
    shared = check_mod.check_declarations([doubled])
    check("同一份契约里两处共用产物 ⇒ 红",
          any("共用产物" in item for item in shared.problems), "；".join(shared.problems))

    # ⚠️ 台架键：声明了、而装配处（`bench_values`）接不上 ⇒ `preflight` 会拒，
    #    检查必须看得见（2026-10-13 验收拓到：以前机器检查全绿而 preflight 直接拒）。
    declared = replace(declaration, bench_keys=("device:demo.port",))
    empty = check_mod.check_declarations(
        [declared], bench={declaration.adapter_id: {}}, strict_registration=False
    )
    check("声明了台架键而装配处读不到 ⇒ 红",
          any("台架键" in item for item in empty.problems), "；".join(empty.problems))
    wired = check_mod.check_declarations(
        [declared],
        bench={declaration.adapter_id: {"device:demo.port": (True, "来自台架文件")}},
        strict_registration=False,
    )
    check("台架读到了 ⇒ 不因它红",
          not any("台架键" in item for item in wired.problems), "；".join(wired.problems))
    unread = check_mod.check_declarations(
        [declared],
        bench={declaration.adapter_id: {"device:demo.port": (False, "没有这一格")}},
        strict_registration=False,
    )
    check("台架里有那一格但没配上 ⇒ 红",
          any("台架键" in item for item in unread.problems), "；".join(unread.problems))
    absent = check_mod.check_declarations([declared], strict_registration=False)
    check("这一趟没交台架值（例：--module）⇒ 不当成问题",
          not any("台架键" in item for item in absent.problems), "；".join(absent.problems))

    # ⚠️ “有没有源码”要**逐份**判：兄弟模块有源码，不能把这一份没源码掩盖成静默漏判
    #    （2026-10-13 第三轮验收拓到：平台自带模块永远提供源码 ⇒ 外部模块缺源码时
    #    既不判、也不报“判不了”，屏幕写“通过”）。
    sibling = replace(
        declaration, adapter_id="demo-two", module="other_module",
        project_entry="project/two.ini", label="另一份（它的模块没给源码）",
    )
    mixed = check_mod.check_declarations(
        [declaration, sibling], sources={declaration.module: "x = 1\n"},
        strict_registration=False,
    )
    check("一份有源码、另一份没有 ⇒ 点名那一份没判",
          any("other_module" in item for item in mixed.unjudged),
          "；".join(mixed.unjudged))
    check("有源码的那一份不因它被判不了",
          not any(str(declaration.module) in item for item in mixed.unjudged),
          "；".join(mixed.unjudged))


# ================================================================== 主流程
def main() -> int:
    # ⚠️ 这份清单是**手工**列的：新写的测试忘了接进来就会**静默不跑** —— 那正是本专项最
    #    忌讳的「没跑当过了」（2026-10-13 本文件刚踩到一次：新用例写完没接进清单，
    #    PASS 数一个没涨）。所以先自查清单完不完整，它自己就是一条判据。
    source = Path(__file__).read_text(encoding="utf-8")
    routed = {
        line.strip().split("(")[0]
        for line in source.splitlines()
        if line.startswith("    test_") and "()" in line
    }
    written = {
        line.split("def ")[1].split("(")[0].strip()
        for line in source.splitlines()
        if line.startswith("def test_")
    }
    unwired = sorted(written - routed)
    check("每个 test_ 函数都被 main() 调到（漏接 = 静默不跑）", not unwired, str(unwired))

    test_declaration_selfcheck()
    test_criteria_are_data()
    test_failures_carry_next_step()
    test_flash_gate()
    test_identity_four_states()
    test_the_unverified_says_which_kind_of_unavailable()
    test_fuse()
    test_machine_checks()
    test_opensource_scan()
    test_prepared_command_handoff()
    test_identity_expectation_comes_from_the_build()
    test_the_check_says_what_it_could_not_judge()
    test_scaffold_roundtrip()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：" + "；".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

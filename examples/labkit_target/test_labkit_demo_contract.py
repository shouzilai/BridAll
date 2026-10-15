#!/usr/bin/env python3
"""LabKit demo board 的契约测试（脚手架生成物）。

它用**假工具**把六件事各跑一遍：成功、没有显式同意（拒烧）、已知失败、
**未知失败**、零输出超时、身份不符 / 未核验，以及"同一故障连续两次之后第三次被拒"。

Run:  python test_labkit_demo_contract.py
"""

from __future__ import annotations


def _find_platform() -> "Path":
    """找到平台的 `tools`（环境变量优先，其次从当前目录向上找）。

    ⚠️ 生成物**不写死任何绝对路径**：它要能放进任何工程、也能跟着仓库一起换机器。
    ⚠️ `from pathlib import Path` 写在函数里：这段代码被插在生成文件的**最上面**，
    那时 `import` 语句还没轮到（而且本函数的返回标注是字符串，不依赖它）。
    """
    import os
    from pathlib import Path

    given = os.environ.get("PW_PLATFORM_TOOLS", "")
    if given:
        return Path(given)
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "tools"
        if (candidate / "webconsole" / "hwops_contract.py").is_file():
            return candidate
        if (parent / "webconsole" / "hwops_contract.py").is_file():
            return parent
    raise SystemExit(
        "找不到平台：把 PW_PLATFORM_TOOLS 指到仓库的 tools（它下面有 webconsole/）"
    )


import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(_find_platform() / "webconsole"))
sys.path.insert(0, str(_find_platform()))

import unicode_console  # noqa: E402

unicode_console.install()  # 管道/GBK 控制台下打印中文不再中途抛错

import hwops_check as check_mod  # noqa: E402
import hwops_contract as contract_mod  # noqa: E402
import hwops_fake as fake_mod  # noqa: E402
import hwops_runner as runner_mod  # noqa: E402

from labkit_demo import declaration, resolver  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}{': ' + detail if detail else ''}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name}{': ' + detail if detail else ''}")


def _env(project_dir: Path, script, *, identity=None):
    """一次操作的现场：假执行器 + 假串口 + 解析器（工具指向平台自己的假工具）。"""
    tools = dict((key, fake_mod.FAKE_TOOL_FILE) for key in declaration().tools)
    resolve = resolver(project_dir, port="PORT-A", tools=tools)
    executor = fake_mod.FakeExecutor(script=script)
    device = fake_mod.FakeDevice(identity=identity)
    return resolve, executor, device


def main() -> int:
    decl = declaration()
    # ⚠️ 生成物还**没有登记**到平台的分层表里（那是接入做完之后的动作），所以这里
    # 用 `strict_registration=False`：层名要合法，但"模块已登记"这一条此时不成立。
    problems = check_mod.declaration_problems(decl, strict_registration=False)
    check("契约本身站得住", not problems, "；".join(problems))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 按**这份声明**铺出「输入在、产物在」的样子（不另写一份文件名）。
        fake_mod.materialize(decl, root)
        build_ok = fake_mod.tool_run(output=fake_mod.build_output())

        # 1) 一切正常：三步都过
        resolve, executor, device = _env(
            root, fake_mod.default_script(), identity={"project": "demo", "build_id": "0"}
        )
        report = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, confirm_flash=True,
            allow_overwrite=True, identity_probe=device.identity_probe,
            debug_probe=device.probe,
        )
        check("正常路径 PASS", report.result == "pass", report.result)

        # 2) 没有显式同意 ⇒ 一个字节都不写
        resolve, executor, device = _env(root, fake_mod.default_script())
        report = runner_mod.run_operation(decl, executor=executor, resolver=resolve)
        flash = report.step("flash")
        check("没有同意就拒烧", flash is not None and flash.status == "refused",
              flash.detail if flash else "")
        check("拒烧之后没有调用工具", executor.called("demo.flash") == 0)

        # 3) 已知失败标记 ⇒ 稳定故障码 + 处置
        resolve, executor, device = _env(
            root, fake_mod.default_script(**{
                "demo.flash": fake_mod.tool_run(
                    code=1, output=fake_mod.flash_output(done=False, fatal="probe stuck")
                )
            })
        )
        report = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, confirm_flash=True, allow_overwrite=True
        )
        flash = report.step("flash")
        check("已知失败有稳定故障码",
              flash is not None and flash.status == "fail"
              and flash.fault in contract_mod.FAULT_CODES, flash.fault if flash else "")
        check("失败详情自带下一步",
              flash is not None and bool(decl.faults.explain("Fatal error: probe stuck")),
              flash.detail if flash else "")

        # 3b) 不认得的失败标记 ⇒ unknown_failure（**不编原因**，保留原文）
        resolve, executor, device = _env(
            root, fake_mod.default_script(**{
                "demo.flash": fake_mod.tool_run(
                    code=1, output="something brand new went wrong"
                )
            })
        )
        report = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, confirm_flash=True, allow_overwrite=True
        )
        flash = report.step("flash")
        check("不认得的失败 ⇒ unknown_failure（不编原因）",
              flash is not None and flash.status == "fail"
              and flash.fault == "unknown_failure", flash.fault if flash else "")

        # 4) 零输出超时 ⇒ 与"超时但有输出"分开
        resolve, executor, device = _env(
            root, fake_mod.default_script(**{
                "demo.build": fake_mod.tool_run(timed_out=True, output="")
            })
        )
        report = runner_mod.run_operation(decl, executor=executor, resolver=resolve)
        build = report.step("build")
        check("零输出超时被认出来",
              build is not None and build.fault == "no_output_timeout",
              build.fault if build else "")

        # 5) 身份不符 ⇒ mismatch；没拿到 ⇒ 未核验（都不是通过）
        resolve, executor, device = _env(
            root, fake_mod.default_script(), identity={"project": "other", "build_id": "0"}
        )
        report = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, confirm_flash=True,
            allow_overwrite=True, identity_probe=device.identity_probe,
        )
        verify = report.step("verify")
        check("身份不符判失败",
              verify is not None and verify.status == "fail"
              and verify.fault == "identity_mismatch", verify.fault if verify else "")

        resolve, executor, device = _env(root, fake_mod.default_script(), identity=None)
        report = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, confirm_flash=True,
            allow_overwrite=True, identity_probe=device.identity_probe,
        )
        verify = report.step("verify")
        check("拿不到身份 ⇒ 未核验（不冒充通过）",
              verify is not None and verify.status in ("skipped", "unsupported"),
              verify.status if verify else "")

        # 6) 同一故障连续两次之后，第三次在调工具**之前**被拒
        fuse = runner_mod.FuseState()
        script = fake_mod.default_script(**{
            "demo.build": fake_mod.tool_run(
                code=1, output=fake_mod.build_output(errors=1)
            )
        })
        attempts = 0
        for _ in range(3):
            resolve, executor, device = _env(root, script)
            runner_mod.run_operation(
                decl, executor=executor, resolver=resolve, fuse=fuse, steps=("preflight", "build")
            )
            attempts += executor.called("demo.build")
        build = runner_mod.run_operation(
            decl, executor=executor, resolver=resolve, fuse=fuse, steps=("preflight", "build")
        ).step("build")
        check("两次之后第三次被熔断拦住",
              build is not None and build.status == "refused" and build.fault == "fuse_open",
              build.fault if build else "")
        check("熔断发生在调工具之前", attempts == 2, f"调了 {attempts} 次")

    if FAILURES:
        print("\n失败：" + "；".join(FAILURES))
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

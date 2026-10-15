"""Adapter 脚手架（ / F2，**core**）。

需求 §4 的第三条：`create-adapter` 生成骨架、声明文件、fixture 与验收清单，**并且
生成物立刻能跑**（先让假工具跑通，再去接真工具）。

生成三样东西（都落在 `--out` 指定的目录里）：

```text
<out>/<module>.py                  声明文件：一条 HardwareDeclaration + 组装用的解析器
<out>/test_<module>_contract.py    契约测试：拿假工具跑成功/失败/超时/身份/熔断
<out>/README.md                    接入清单：还差哪几件事才算接完
```

⚠️ 生成物**不带任何本机事实**：没有绝对路径、没有串口名、没有厂商私有工程。它只
要求调用方给一个"平台在哪"的线索（环境变量 `PW_PLATFORM_TOOLS`，或从当前目录向上找
`tools/webconsole`），所以它可以被放到任何工程的任何位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # imported as part of the package
    from . import hwops_contract as contract_mod
except ImportError:  # imported as a top-level module
    import hwops_contract as contract_mod  # type: ignore[no-redef]

#: 生成物里那段"平台在哪"的解析（写进每个生成文件；只有一个含义）。
PLATFORM_LOOKUP = '''

def _find_platform() -> "Path":
    """找到平台的 `tools/`（环境变量优先，其次从当前目录向上找）。

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
        candidate = parent / "示例项目" / "tools"
        if (candidate / "webconsole" / "hwops_contract.py").is_file():
            return candidate
        if (parent / "webconsole" / "hwops_contract.py").is_file():
            return parent
    raise SystemExit(
        "找不到平台：把 PW_PLATFORM_TOOLS 指到仓库的 tools/（它下面有 webconsole/）"
    )
'''

#: 声明文件的模板（`string.Template` 的 `$name` 占位符）。
DECLARATION_TEMPLATE = '''"""$label 的接入契约（ 脚手架生成物）。

这是个**骨架**：`TODO` 的地方要按你自己的工具链填。填完先跑 `test_${module}_contract.py`
（拿假工具跑），再去跑 `hwops.py check --module <这份文件>` 与真机。

```powershell
$$env:PW_PLATFORM_TOOLS = "<仓库>/tools/"
python -m pytest test_${module}_contract.py       # 或者直接 python test_${module}_contract.py
```
"""

from __future__ import annotations
$lookup

import sys
from pathlib import Path

sys.path.insert(0, str(_find_platform() / "webconsole"))
sys.path.insert(0, str(_find_platform()))

import hwops_contract as contract_mod  # noqa: E402
import hwops_runner as runner_mod  # noqa: E402

#: 这条接入用到的**受控适配器登记表**：步骤里写的 `adapter_id` 必须在这份表里。
#: ⚠️ 它是一条真的守卫（不是形式）：有人把手写的厂商命令接在某一步上（而不是
#: 走登记过的适配器），机器检查的旁路那一条就会红 —— 见 `hwops check`。
ADAPTERS = (
    "demo.build",
    "demo.flash",
    "demo.identity",
    "demo.debug",
)


def declaration() -> contract_mod.HardwareDeclaration:
    """这条硬件的接入契约（**声明是数据**：工具、步骤、判据、故障、能力）。"""
    return contract_mod.HardwareDeclaration(
        adapter_id="$adapter_id",
        target_id="$target_id",
        label="$label",
        layer="$layer",
        module="$module",
        # TODO：你的工程入口（相对工程根）。构建/烧录都要它对得上。
        project_entry="$project_entry",
        build_config="$build_config",
        # TODO：这一趟真要用到的台架键（住在本机台架文件里，不进仓库）。
        bench_keys=(),
        # TODO：工具键 —— 参数里用 `{tool}` 之外，还要在这里列出它。
        tools=("demo-tool",),
        steps=(
            contract_mod.StepSpec(
                step="build",
                adapter_id="demo.build",
                label="构建（TODO：写清用哪条命令）",
                tool="demo-tool",
                args=("build", "{project}", "--config", "{config}"),
                # TODO：跑之前必须在的输入。
                inputs=("$project_entry",),
                # TODO：构建后必须出现的产物（构建成不成，一半靠它）。
                artifacts=("out/app.bin",),
                success=contract_mod.SuccessCriterion(
                    # TODO：你的工具"算成功"的判据。退出码不够就写 kind="log"，
                    #       并给出必须出现/必须不出现/必须为零的标记。
                    kind="exit",
                ),
            ),
            contract_mod.StepSpec(
                step="flash",
                adapter_id="demo.flash",
                label="写入（TODO：写清用哪条命令；覆盖现有应用要显式同意）",
                tool="demo-tool",
                args=("flash", "--port", "{port}", "{project}"),
                inputs=("$project_entry",),
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=("flash done",),
                    failure_markers=("Fatal error",),
                ),
            ),
            contract_mod.StepSpec(
                step="verify",
                adapter_id="demo.identity",
                label="烧录后核身份（拿设备自报对表，不猜）",
                tool="demo-tool",
                args=("identity", "--port", "{port}"),
                success=contract_mod.SuccessCriterion(kind="exit"),
            ),
            contract_mod.StepSpec(
                step="debug-check",
                adapter_id="demo.debug",
                label="打开通信口读一小段（最小连通性检查）",
                tool="demo-tool",
                args=("probe", "--port", "{port}"),
            ),
        ),
        faults=contract_mod.FaultKnowledge(
            # TODO：把"看到这个标记 → 下一步做什么"写成数据（别只写在文档里）。
            markers=(
                contract_mod.FaultMarker("Fatal error", "flash_failed", "写之前就放弃了"),
            ),
            recovery=("TODO：这一条对本机硬件有效的处置（例：拔插一次，不要连着试）",),
            unknown_advice="没见过的失败：先保留原文，再判断，不要直接重试",
            step_defaults=(("build", "build_failed"), ("flash", "flash_failed")),
        ),
        capabilities=(
            contract_mod.CapabilityResult("build", "supported"),
            contract_mod.CapabilityResult("flash", "supported"),
            contract_mod.CapabilityResult("transport", "supported"),
            contract_mod.CapabilityResult("identity", "supported"),
            # TODO：做不到的**明确**写 unsupported（不要留空：留空就没人知道是什么状态）。
            contract_mod.CapabilityResult(
                "symbolize", "unsupported", "TODO：为什么做不到，或改成 supported"
            ),
        ),
        transport=contract_mod.TransportHint(
            kind="serial",
            baud=$baud,
            line_ending="$line_ending",
            read_only=True,
            adapter_id="demo.serial.read-only",
            minimal_check="打开口、读一行就关（不写复位线）",
        ),
        identity=contract_mod.IdentityHint(
            # TODO：问身份的那条命令，以及回答里哪几个字段算身份。
            query="$identity_query",
            fields=("project", "build_id"),
        ),
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"),
            values={"project": "$project"},
        ),
        excluded=(),
        notes=("脚手架生成：先填 TODO，再拿假工具跑通，最后接真机。",),
    )


def resolver(project_dir, *, port: str = "", tools=None) -> "runner_mod.RecipeResolver":
    """装配处交进来的解析器（工程根 / 工具表 / 台架值都在这里）。"""
    return runner_mod.RecipeResolver(
        project_dir=project_dir,
        tools=dict(tools or {}),
        config="$build_config",
        port_name=port,
    )


def hwops_wirings():
    """统一入口（`hwops.py list/run`）要用的装配 —— 有了它你的模块就能被驱动。

    ⚠️ 这是“接一条新硬件**不必改 core**”的那一半：入口按名字找到你的模块，
    叫这个函数拿装配（平台侧清单在 `bench_setup.hwops_target_modules()`；
    不想动它就用本机配置 `--targets <你的模块名或 .py>`）。

    ⚠️ 默认用**平台自带的假工具**，所以不接硬件也能把通路跑一遍；接真机时改两处：
    `tools` 换成你的工具表（真实可执行文件），`project_dir` 换成你的工程根。
    台架值（装在哪、插在哪个口）走 `bench_values` / 台架文件，**不要**写进代码。
    """
    from hwops_fake import FAKE_TOOL_FILE

    decl = declaration()
    return [
        contract_mod.AdapterWiring(
            declaration=decl,
            project_dir=str(Path(__file__).resolve().parent),
            tools={key: str(FAKE_TOOL_FILE) for key in decl.tools},
            bench_values={},
            registered=ADAPTERS,
            port_factory=None,
            identity_parser=None,
            notes=("脚手架生成：默认拿平台的假工具；串口实现与身份解析还没接（TODO）。",),
        )
    ]
'''

#: 契约测试的模板：**生成即可跑**（假工具跑成功/失败/超时/身份/熔断五件事）。
TEST_TEMPLATE = '''#!/usr/bin/env python3
"""$label 的契约测试（ 脚手架生成物）。

它用**假工具**把五件事各跑一遍（需求 §7.4）：成功、已知失败、未知失败、
零输出超时、身份不符 / 未核验，以及"同一故障连续两次之后第三次被拒"。

Run:  python test_${module}_contract.py
"""

from __future__ import annotations
$lookup

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(_find_platform() / "webconsole"))
sys.path.insert(0, str(_find_platform()))

import hwops_check as check_mod  # noqa: E402
import hwops_contract as contract_mod  # noqa: E402
import hwops_fake as fake_mod  # noqa: E402
import hwops_runner as runner_mod  # noqa: E402

from $module import declaration, resolver  # noqa: E402

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
            root, fake_mod.default_script(), identity={"project": "$project", "build_id": "0"}
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
        print("\\n失败：" + "；".join(FAILURES))
        return 1
    print("\\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 接入清单（README）。
README_TEMPLATE = """# $label —— 接入清单

这份目录是 `hwops create-adapter` 生成的**骨架**。它现在就能跑（拿假工具），
你的任务是把 `TODO` 换成自己工具链的真实声明。

## 一秒跑通

```powershell
$$env:PW_PLATFORM_TOOLS = "<仓库路径>/tools/"
python test_${module}_contract.py
```

生成物里的假工具来自平台（`webconsole/hwops_fake.py`）：它按适配器 ID 给反应，
所以**不需要任何硬件**就能把下面五件事各跑一遍：

| 场景 | 它证明什么 |
| --- | --- |
| 正常路径 | 三步按声明的判据各自通过 |
| 没有显式同意 | 安全闸门在 Core 就挡住，**一个字节都不写** |
| 已知失败标记 | 稳定故障码 + 处置出现在失败详情里 |
| 零输出超时 | 与"超时但有输出"分开报 |
| 身份不符 / 拿不到 | `mismatch` 与"未核验"分开，都不冒充通过 |
| 连续两次熔断 | 第三次在调工具之前被拒 |

## 接入清单（做完一条勾一条）

1. [ ] **工程入口**：`project_entry` / `build_config` 指向你的工程（不是别人机器上的路径）。
2. [ ] **工具键**：`tools` 里列出要用的工具；参数里只写占位符（`{project}` / `{config}` /
       `{port}` / `{tools}`），不写绝对路径。
3. [ ] **构建判据**：退出码 0 不等于成功就写 `kind="log"`，并给出必须出现的标记、
       必须为零的统计行、以及**必须出现**的产物。
4. [ ] **烧录判据**：写清"这次算不算真写进去了"。失败时工具可能只打 Warning 而返回 0。
5. [ ] **故障知识**：把"看到这个标记 ⇒ 下一步做什么"写成 `FaultMarker` + `recovery`。
       别只写在文档里 —— 用到它的那一刻没人会去翻文档。
6. [ ] **能力四态**：做不到的写 `unsupported` 并给理由；留空等于"没人知道"。
7. [ ] **身份核验**：`identity.query` 与要比对的字段；拿不到就如实"未核验"。
8. [ ] **台架键**：本机事实（装在哪、插在哪个口）写进台架文件，`bench_keys` 只列键名。
9. [ ] **机器检查**：`python tools/hwops.py check --module <你的模块>`
       —— 契约完整性、旁路命令、判据重复、跨 profile 串用、Core 泄漏一次跑完。
10. [ ] **让统一入口看见它**：`hwops.py list --targets <到 .py 的路径>`（也可以设
        `PW_HWOPS_TARGETS`，或把模块加进平台装配处 `bench_setup._target_modules()` ——
        那份 import 与返回元组各加一处）—— 两种做法都**不必改 core**。
        能看到之后：`hwops.py run --adapter $adapter_id --targets <到 .py 的路径> --steps preflight,build --fake`。
        ⚠️ **`--fake` 是必需的**：这份装配把工具键指向平台的假工具（一个 `.py`），
        不加 `--fake` 会得到“命令起不来（WinError 193）· 工具不可用”。
        ⚠️ 给 `--targets` 用**路径**而不是模块名：裸名字在子目录布局下会命中那个目录。
        ⚠️ 生成物默认拿**平台的假工具**，所以 `preflight` 会通过、而 `build` 会红在“声明的产物
        没出现”—— 那是 TODO 还没填（产物要你的工具**真**产出），不是通路不通。
11. [ ] **真机证据**：`hwops.py run --adapter <id> --steps preflight,build` →
        接上工具 → `--steps flash --confirm-flash --allow-overwrite` → `verify` → `debug-check`。
        每一步的结论都带稳定故障码与耗时，落在 `artifacts//hwops/` 里。

## 边界（别踩）

- ⚠️ 改了两样东西要**同步改生成物自带的契约测试**：① 步骤的 `adapter_id`（测试里绑着
  `demo.*`，包括 `executor.called("demo.build")` 这种**不在断言句子**里的地方；改了之后
  那几条断言会静默退化成“恒真”）；② 要核的身份字段名（测试里绑着 `project` / `build_id`；
  改了之后 `身份不符判失败` 那条会红）。改它们是为了让测试跟着**你的声明**走，
  不是放松断言。
- **不许**把本机路径、串口名、序列号、厂商私有工程或受限二进制放进仓库：
  `hwops.py opensource-audit` 会逐行报出来。
- **不许**绕开登记表直接调厂商命令：`check` 的旁路检查会红。
- **不许**按步骤名挑判据：判据只能来自声明（那正是  的形状）。
- Core 里**不许**出现你的平台名：声明放目标/项目层，Core 只按形状调。
"""


@dataclass(frozen=True)
class ScaffoldResult:
    """生成结果（路径 + 怎么跑）。"""

    directory: Path
    declaration: Path
    test: Path
    readme: Path

    def to_json(self) -> dict[str, str]:
        return {
            "directory": str(self.directory),
            "declaration": str(self.declaration),
            "test": str(self.test),
            "readme": str(self.readme),
        }


def _identifier(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_-" else "-" for char in value)
    return cleaned.strip("-") or "adapter"


def scaffold_adapter(
    out_dir: Path | str,
    *,
    adapter_id: str,
    target_id: str = "",
    label: str = "",
    layer: str = "target-demo",
    module: str = "",
    project_entry: str = "project/entry.ini",
    build_config: str = "Debug",
    baud: int = 115200,
    line_ending: str = "lf",
    identity_query: str = "identity",
    project: str = "demo",
) -> ScaffoldResult:
    """生成 Adapter 骨架（声明 + 契约测试 + 接入清单）。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    module = _identifier(module or adapter_id.replace("-", "_"))
    label = label or f"{adapter_id}（脚手架生成）"
    values = {
        "lookup": PLATFORM_LOOKUP,
        "adapter_id": adapter_id,
        "target_id": target_id or adapter_id,
        "label": label,
        "layer": layer,
        "module": module,
        "project_entry": project_entry,
        "build_config": build_config,
        "baud": str(int(baud)),
        "line_ending": line_ending,
        "identity_query": identity_query,
        "project": project,
    }
    from string import Template

    declaration_path = directory / f"{module}.py"
    test_path = directory / f"test_{module}_contract.py"
    readme_path = directory / "README.md"
    declaration_path.write_text(
        Template(DECLARATION_TEMPLATE).substitute(values), encoding="utf-8", newline="\n"
    )
    test_path.write_text(
        Template(TEST_TEMPLATE).substitute(values), encoding="utf-8", newline="\n"
    )
    readme_path.write_text(
        Template(README_TEMPLATE).substitute(values), encoding="utf-8", newline="\n"
    )
    # ⚠️ 把**声明里的输入**铺出来（占位，带一行说清它是占位）：
    #    这样 `hwops.py run --targets <这份模块> --steps preflight` 能当场告诉你
    #    “通路是通的、还差什么”；而构建那一步仍会红——因为**产物得你的工具真产出**，
    #    判据不是摆设（这正是它该有的样子）。
    entry = Path(str(project_entry).replace("\\", "/"))
    placeholder = directory / entry
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    if not placeholder.exists():
        placeholder.write_text(
            "# 脚手架铺出来的**占位输入**：接真机时把声明的 project_entry /\n"
            "# project_dir 换成你自己的工程（这一份只是为了 preflight 能告诉你还差什么）。\n",
            encoding="utf-8",
        )
    return ScaffoldResult(
        directory=directory, declaration=declaration_path, test=test_path, readme=readme_path
    )


def skeleton_declaration(**overrides: object) -> contract_mod.HardwareDeclaration:
    """骨架里那条契约的**内存版**（`check --module` 可以拿它先做一遍形状检查）。"""
    declaration = {
        "adapter_id": "demo-board",
        "target_id": "demo-target",
        "label": "脚手架骨架",
        "layer": "target-demo",
        "module": "demo_target",
        "project_entry": "project/entry.ini",
    }
    declaration.update({key: value for key, value in overrides.items()})
    return contract_mod.HardwareDeclaration(**declaration)  # type: ignore[arg-type]

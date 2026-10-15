"""LabKit demo board 的接入契约（脚手架生成物）。

这是个**骨架**：`TODO` 的地方要按你自己的工具链填。填完先跑 `test_labkit_demo_contract.py`
（拿假工具跑），再去跑 `hwops.py check --module <这份文件>` 与真机。

```powershell
$env:PW_PLATFORM_TOOLS = "<仓库>/tools"
python -m pytest test_labkit_demo_contract.py       # 或者直接 python test_labkit_demo_contract.py
```
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
        adapter_id="labkit-demo",
        target_id="labkit-demo",
        label="LabKit demo board",
        layer="target-demo",
        module="labkit_demo",
        # TODO：你的工程入口（相对工程根）。构建/烧录都要它对得上。
        project_entry="project/entry.ini",
        build_config="Debug",
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
                inputs=("project/entry.ini",),
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
                inputs=("project/entry.ini",),
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
            # ⚠️ 骨架**还没有**交出串口实现与身份解析器（见 `hwops_wirings()` 的
            #    `port_factory=None` / `identity_parser=None`）⇒ 那两顶就**不能**写
            #    supported。接上之后再把理由删掉。
            contract_mod.CapabilityResult(
                "transport", "unsupported",
                "TODO：还没交出串口实现（AdapterWiring.port_factory）"
            ),
            contract_mod.CapabilityResult(
                "identity", "unsupported",
                "TODO：还没交出身份解析器（AdapterWiring.identity_parser）"
            ),
            # TODO：做不到的**明确**写 unsupported（不要留空：留空就没人知道是什么状态）。
            contract_mod.CapabilityResult(
                "symbolize", "unsupported", "TODO：为什么做不到，或改成 supported"
            ),
        ),
        transport=contract_mod.TransportHint(
            kind="serial",
            baud=115200,
            line_ending="lf",
            read_only=True,
            adapter_id="demo.serial.read-only",
            minimal_check="打开口、读一行就关（不写复位线）",
        ),
        identity=contract_mod.IdentityHint(
            # TODO：问身份的那条命令，以及回答里哪几个字段算身份。
            query="identity",
            fields=("project", "build_id"),
        ),
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"),
            values={"project": "demo"},
        ),
        excluded=(),
        notes=("脚手架生成：先填 TODO，再拿假工具跑通，最后接真机。",),
    )


def resolver(project_dir, *, port: str = "", tools=None) -> "runner_mod.RecipeResolver":
    """装配处交进来的解析器（工程根 / 工具表 / 台架值都在这里）。"""
    return runner_mod.RecipeResolver(
        project_dir=project_dir,
        tools=dict(tools or {}),
        config="Debug",
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

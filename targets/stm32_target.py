#!/usr/bin/env python3
"""STM32 参考适配（**target-stm32**）：裸机 IAR 与 RT-Thread 两条 profile。

这份模块是"平台差异住哪儿"的示范：它声明**这块板与这套工具链**的知识 —— 用哪个工具、
命令怎么写、成功怎么看、失败了怎么办、身份怎么问 —— 而 core 只按形状调它，一个字都不认识
STM32。

两条 profile 共用**同一个目标**（`stm32f103`），因为它们本来就是同一块板上的两份固件；
它们**不共用**工程入口与产物（换固件时拿错 ELF/字典正是要挡住的事）。

| profile | 构建 | 烧录 | 适用场景 |
| --- | --- | --- | --- |
| `stm32f103-baremetal` | IAR `IarBuild` | C-SPY 下载（argv 由本层给） | 裸机工程 |
| `stm32f103-rtthread` | GNU `make` | C-SPY 下载（同上） | RT-Thread 工程 |

## 本机事实从哪儿来

工具装在哪、工程根在哪、板子插在哪个口，**都不写在这里**。它们按同一个梯子取值：
`--bench` 指定的台架文件 / `bench.local.json` → 环境变量 → （没有兜底值）。

| 槽 | 环境变量 | 说明 |
| --- | --- | --- |
| `tools.iar_build` | `PW_STM32_IAR_BUILD` | `IarBuild.exe` 的完整路径 |
| `tools.cspybat` | `PW_STM32_CSPYBAT` | `CSpyBat.exe` 的完整路径 |
| `tools.make` | `PW_STM32_MAKE` | `make` 的完整路径（默认按 PATH 找） |
| `projects.stm32f103-baremetal` | `PW_STM32_BARE_PROJECT` | 裸机工程根 |
| `projects.stm32f103-rtthread` | `PW_STM32_RT_PROJECT` | RT-Thread 工程根 |
| `devices.stm32a.port` | `PW_STM32_PORT` | 通信口（设备自报身份要读它） |

⚠️ 声明里的目标/固件 ID、工程入口与配置名都是**形状示例**：接你自己的工程时必须替换。
"""

from __future__ import annotations

import re
from pathlib import Path
import sys
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
CONSOLE_DIR = TOOLS_DIR / "webconsole"
for _directory in (str(CONSOLE_DIR), str(TOOLS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import bench_config  # noqa: E402  (core：台架配置层)
import bench_target  # noqa: E402  (core：公开运行装配的公共部分)
import doctor_target  # noqa: E402  (core：体检目标扩展接口)
import hwops_contract as contract_mod  # noqa: E402  (core：契约形状)

# ============================================================ 它是谁
LAYER = "target-stm32"
TARGET_ID = "stm32f103"
TARGET_LABEL = "STM32F103（IAR 裸机 / RT-Thread）"

#: 受控适配器登记表：步骤里写的 `adapter_id` 必须在这份表里（旁路检查对着它比）。
ADAPTERS: tuple[str, ...] = (
    "stm32.iar-build",
    "stm32.make-build",
    "stm32.cspy-download",
    "stm32.identity",
    "stm32.serial.read-only",
)

#: 工具键（`{tool}` 之外，声明里还要列出它们；可执行文件从台架/环境变量来）。
TOOL_KEYS: tuple[str, ...] = ("iar_build", "cspybat", "make")
#: 台架键 → 环境变量（取值梯子里的第二级）。
ENV_VARS: dict[str, str] = {
    "iar_build": "PW_STM32_IAR_BUILD",
    "cspybat": "PW_STM32_CSPYBAT",
    "make": "PW_STM32_MAKE",
    "stm32f103-baremetal": "PW_STM32_BARE_PROJECT",
    "stm32f103-rtthread": "PW_STM32_RT_PROJECT",
    "stm32a.port": "PW_STM32_PORT",
}
#: 设备键。
DEVICE_KEY = "stm32a"

FIRMWARE_BARE = "stm32f103-baremetal"
FIRMWARE_RT = "stm32f103-rtthread"

#: 设备自报身份的那一行（**这块板的协议**：`@PWINFO project=… build_id=…`）。
IDENTITY_RE = re.compile(
    r"@PWINFO\s+project=(?P<project>\S+)\s+build_id=(?P<build_id>\S+)"
)


def parse_identity(line: str) -> dict[str, str] | None:
    """把设备自报的那一行解释成身份字段（认不出就 `None`，不猜）。"""
    match = IDENTITY_RE.search(str(line or ""))
    if not match:
        return None
    return {"project": match.group("project"), "build_id": match.group("build_id")}


# ============================================================ 声明（数据）
def _steps(for_baremetal: bool) -> tuple[contract_mod.StepSpec, ...]:
    if for_baremetal:
        build = contract_mod.StepSpec(
            step="build",
            adapter_id="stm32.iar-build",
            label="构建裸机固件（IAR：退出码 0 不等于编成了，要数错误行）",
            tool="iar_build",
            args=("{project}/project/entry.ewp", "-build", "{config}", "-log", "warnings"),
            inputs=("project/entry.ewp",),
            artifacts=("{config}/Exe/app.out", "{config}/List/app.map"),
            success=contract_mod.SuccessCriterion(
                kind="log",
                success_markers=("Linking",),
                error_count_pattern=r"Total number of errors:\s*(\d+)",
                require_count=True,
                min_artifacts=2,
                note="工具即便没链接成功也可能返回 0 ⇒ 数错误行 + 核产物",
            ),
        )
    else:
        build = contract_mod.StepSpec(
            step="build",
            adapter_id="stm32.make-build",
            label="构建 RT-Thread 固件（GNU make：判据 = 退出码 + 产物）",
            tool="make",
            args=("-C", "{project}", "-j"),
            inputs=("Makefile",),
            artifacts=("build/app.elf",),
            success=contract_mod.SuccessCriterion(
                kind="log",
                success_markers=("app.elf",),
                failure_markers=("Error ", "*** "),
            ),
        )
    return (
        build,
        contract_mod.StepSpec(
            step="flash",
            adapter_id="stm32.cspy-download",
            label="用 C-SPY 下载（argv 由本层给：要按本次产物算参数文件路径）",
            tool="cspybat",
            prepared=True,
            inputs=("project/entry.ewp",),
            success=contract_mod.SuccessCriterion(
                kind="log",
                success_markers=("Download complete",),
                failure_markers=("Flash loading failed", "No MCU device found", "Session aborted"),
                note="写完之后由 verify 那一步核身份",
            ),
            notes=("argv 由 `hwops_wirings()` 交出来的 PreparedCommand 决定（见本文件）。",),
        ),
        contract_mod.StepSpec(
            step="verify",
            adapter_id="stm32.identity",
            label="烧录后读回校验",
            tool="cspybat",
            args=("--verify", "{project}/project/entry.ewp"),
            timeout_s=120.0,
            success=contract_mod.SuccessCriterion(
                kind="log", success_markers=("Verify complete",)
            ),
        ),
        contract_mod.StepSpec(
            step="debug-check",
            adapter_id="stm32.serial.read-only",
            label="打开通信口读一小段（最小连通性检查）",
            args=("probe", "--port", "{port}"),
            timeout_s=20.0,
            notes=(
                "这一步**不跑工具**：由入口注入的串口实现只读打开（打开前就把 DTR/RTS "
                "按下去，所以不复位设备）。args 只是形状，用来固定「读哪个口」。",
            ),
        ),
    )


def _declaration(firmware: str) -> contract_mod.HardwareDeclaration:
    baremetal = firmware == FIRMWARE_BARE
    return contract_mod.HardwareDeclaration(
        adapter_id=firmware,
        target_id=TARGET_ID,
        label=("STM32F103 裸机固件（IAR + C-SPY + ST-LINK）" if baremetal
               else "STM32F103 RT-Thread 固件（GNU make + C-SPY + ST-LINK）"),
        layer=LAYER,
        module=Path(__file__).stem,
        project_entry=("project/entry.ewp" if baremetal else "Makefile"),
        build_config=("Debug" if baremetal else "rtthread"),
        bench_keys=(*(TOOL_KEYS if baremetal else ("make",)), firmware, f"{DEVICE_KEY}.port"),
        tools=TOOL_KEYS,
        steps=_steps(baremetal),
        faults=contract_mod.FaultKnowledge(
            markers=(
                contract_mod.FaultMarker(
                    "Flash loading failed", "flash_failed",
                    "探针和目标之间没连上：这次是**零写入**",
                ),
                contract_mod.FaultMarker(
                    "No MCU device found", "flash_failed",
                    "探针找不到目标芯片（这是链路问题，不是代码问题）",
                ),
                contract_mod.FaultMarker(
                    "Session aborted", "flash_failed", "调试会话被中断（多半是探针状态脏了）",
                ),
                contract_mod.FaultMarker(
                    "Total number of errors: 1", "build_failed", "编译报错：先改代码",
                ),
            ),
            recovery=("把探针（ST-LINK）**拔插一次**再试，不要连着重试",),
            unknown_advice="没见过的失败：先保留原文再判断，不要直接重试",
            step_defaults=(("build", "build_failed"), ("flash", "flash_failed")),
        ),
        capabilities=(
            contract_mod.CapabilityResult("build", "supported"),
            contract_mod.CapabilityResult("flash", "supported"),
            contract_mod.CapabilityResult("transport", "supported"),
            contract_mod.CapabilityResult("identity", "supported"),
            contract_mod.CapabilityResult(
                "symbolize", "unsupported",
                "这条参考链没有交进地址还原入口（接你自己的符号工具时可以补）",
            ),
        ),
        transport=contract_mod.TransportHint(
            kind="serial", baud=115200, line_ending="crlf", read_only=True,
            adapter_id="stm32.serial.read-only",
            minimal_check="打开口、读一行就关（不写调制线 ⇒ 不复位设备）",
            notes=("打开通信口不写 DTR/RTS，所以不会复位板子。",),
        ),
        identity=contract_mod.IdentityHint(
            query="@PWINFO", fields=("project", "build_id"), banner_markers=("@PWINFO",),
        ),
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"),
            values={"project": f"{TARGET_ID}-demo"},
            note=("`build_id` 只证明**设备报得出来**：这份参考构建没有交出一份可读的身份产物，"
                  "所以它的期望值不来自本次构建（要接上就先让构建落一份身份清单）。"),
        ),
        excluded=(),
        notes=("参考适配：声明的形状是框架的一部分；工具与工程位置由本机台架给。",),
    )


def declarations() -> tuple[contract_mod.HardwareDeclaration, ...]:
    return (_declaration(FIRMWARE_BARE), _declaration(FIRMWARE_RT))


def declaration(firmware: str = FIRMWARE_BARE) -> contract_mod.HardwareDeclaration:
    return _declaration(firmware)


# ============================================================ 台架 / 体检 / 装配
def bench_declaration() -> bench_config.BenchDeclaration:
    return bench_config.BenchDeclaration(
        owner="stm32",
        tool_keys=TOOL_KEYS,
        firmware_ids=(FIRMWARE_BARE, FIRMWARE_RT),
        device_keys=(DEVICE_KEY,),
    )


def doctor_declaration() -> doctor_target.TargetDeclaration:
    """体检声明：这台机器上还缺什么才能真跑这两条 profile。"""
    machine = bench_config.active()
    iar = machine.tool("iar_build", env_var=ENV_VARS["iar_build"])
    cspy = machine.tool("cspybat", env_var=ENV_VARS["cspybat"])
    bare = machine.project(FIRMWARE_BARE, env_var=ENV_VARS[FIRMWARE_BARE])
    rt = machine.project(FIRMWARE_RT, env_var=ENV_VARS[FIRMWARE_RT])

    def _rule(key: str, title: str, kind: str, value: str, remediation: str,
              *, expect: str = doctor_target.Expectation.PRESENT,
              source: str = "") -> doctor_target.Requirement:
        # ⚠️ 声明必须带 value（工具/路径类不接受空串）。没配值时给一个**一眼看出是占位**的
        #    值：体检会如实报「这个相对路径解析不了（不猜路径）」+ 上面那句 remediation。
        return doctor_target.Requirement(
            key=key, title=title, kind=kind,
            value=value.strip() or f"<未配置：{key}>",
            expected=("可用" if kind != doctor_target.RequirementKind.PATH else "存在"),
            remediation=remediation, expect=expect, source=source,
        )

    requirements = (
        _rule(
            "tool.iar_build", "IAR 构建工具（IarBuild.exe）",
            doctor_target.RequirementKind.PATH, iar.value,
            "把 `iar_build` 写进本机台架文件（或设 PW_STM32_IAR_BUILD）指到 IarBuild.exe",
            source=bench_config.source_note(iar),
        ),
        _rule(
            "tool.cspybat", "C-SPY 命令行下载器（CSpyBat.exe）",
            doctor_target.RequirementKind.PATH, cspy.value,
            "把 `cspybat` 写进本机台架文件（或设 PW_STM32_CSPYBAT）指到 CSpyBat.exe",
            source=bench_config.source_note(cspy),
        ),
        _rule(
            "tool.make", "GNU make（RT-Thread 那条 profile 用）",
            doctor_target.RequirementKind.TOOL, "make",
            "把 make 放进 PATH；或用台架键 `make` 指到它的完整路径",
        ),
        _rule(
            "device.serial", "串口承载（设备自报身份要读它）",
            doctor_target.RequirementKind.MODULE, "serial",
            "python -m pip install pyserial",
        ),
    )
    firmwares = (
        doctor_target.FirmwareDeclaration(
            firmware_id=FIRMWARE_BARE, label="STM32F103 裸机固件",
            project_root=Path(bare.value) if bare.value else None,
            source=bench_config.source_note(bare),
            requirements=(
                _rule(
                    "project.entry", "裸机工程入口（entry.ewp）",
                    doctor_target.RequirementKind.PATH, "project/entry.ewp",
                    "把 `stm32f103-baremetal` 写成本机裸机工程根（台架键 = 固件 id）",
                ),
            ),
        ),
        doctor_target.FirmwareDeclaration(
            firmware_id=FIRMWARE_RT, label="STM32F103 RT-Thread 固件",
            project_root=Path(rt.value) if rt.value else None,
            source=bench_config.source_note(rt),
            requirements=(
                _rule(
                    "project.entry", "RT-Thread 工程入口（Makefile）",
                    doctor_target.RequirementKind.PATH, "Makefile",
                    "把 `stm32f103-rtthread` 写成本机 RT-Thread 工程根（台架键 = 固件 id）",
                ),
            ),
        ),
    )
    return doctor_target.TargetDeclaration(
        target_id=TARGET_ID,
        label=TARGET_LABEL,
        requirements=requirements,
        firmwares=firmwares,
        roles=("profile", "build", "flash", "transport", "identity", "project", "host_suite"),
        expected_roles=("profile", "build", "flash", "transport", "identity", "project", "host_suite"),
        project_root=None,
    )


def read_only_port(port: str, baud: int):
    """只读打开一个通信口：**打开之前**就把调制线按下去，所以不会复位设备。

    ⚠️ 这个实现故意不写任何 DTR/RTS 脉冲：复位是**显式动作**，不能由"打开一个口"顺带完成。
    """
    import serial  # 只有真的要用串口时才 import

    handle = serial.Serial()
    handle.port = str(port)
    handle.baudrate = int(baud)
    handle.timeout = 0.05
    handle.dtr = False
    handle.rts = False
    return handle


def _bench_row(key: str, value: bench_config.BenchValue) -> tuple[bool, str]:
    return (bool(value.configured), bench_config.source_note(value) or "没有这一格")


def hwops_wirings() -> tuple[contract_mod.AdapterWiring, ...]:
    """统一入口要的装配：契约（随代码）+ 这一趟的本机值（随机器）。

    ⚠️ 本机值一个都没有时，这里**不猜**：工程根留空、工具留空 —— 于是
    `preflight` 或入口的装配检查会如实说"未配置"，而不是拿一条看着像真的错路径去跑。
    """
    machine = bench_config.active()
    port_value = machine.device_port(DEVICE_KEY, env_var=ENV_VARS[f"{DEVICE_KEY}.port"])
    tools = {
        key: machine.tool(key, env_var=ENV_VARS[key]).value for key in TOOL_KEYS
    }
    port_factory = read_only_port if _has_pyserial() else None
    rows: list[contract_mod.AdapterWiring] = []
    for firmware in (FIRMWARE_BARE, FIRMWARE_RT):
        decl = _declaration(firmware)
        project = machine.project(firmware, env_var=ENV_VARS[firmware])
        bench_values: dict[str, tuple[bool, str]] = {
            key: _bench_row(key, machine.tool(key, env_var=ENV_VARS[key]))
            for key in decl.bench_keys if key in TOOL_KEYS
        }
        bench_values[firmware] = _bench_row(
            firmware, machine.project(firmware, env_var=ENV_VARS[firmware])
        )
        bench_values[f"{DEVICE_KEY}.port"] = _bench_row(f"{DEVICE_KEY}.port", port_value)
        rows.append(
            contract_mod.AdapterWiring(
                declaration=decl,
                project_dir=project.value,
                tools=tools,
                port=port_value.value,
                bench_values=bench_values,
                registered=ADAPTERS,
                commands=_commands(project.value, machine),
                port_factory=port_factory,
                identity_parser=parse_identity,
                notes=(
                    "C-SPY 下载的 argv 由本层算（见 `_commands`）：它要按本次产物拼参数文件。",
                ),
            )
        )
    return tuple(rows)


def _has_pyserial() -> bool:
    """有没有串口库（没有就如实交 `port_factory=None`，那一项会报 unsupported）。"""
    try:
        import serial  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _commands(project_dir: str, machine: bench_config.Bench) -> dict[str, Any]:
    """目标层**已经准备好**的命令（`adapter_id -> PreparedCommand`）。

    ⚠️ 为什么不让 core 拼这条命令：C-SPY 的 argv 要按**本次产物**算参数文件路径 ——
    那是这套工具链的知识。core 只拿结果，于是同一条适配器的 argv 只有一个来源。
    """
    cspy = machine.tool("cspybat", env_var=ENV_VARS["cspybat"]).value
    if not cspy:
        # 没配工具 ⇒ 交不出来（**不是**编一条假命令）：那一步会如实报"装配缺了一半"。
        return {}
    return {
        "stm32.cspy-download": contract_mod.PreparedCommand(
            argv=(
                cspy,
                "--download",
                f"{project_dir}/project/entry.ewp",
            ),
            cwd=project_dir or ".",
            timeout_s=300.0,
            attach=True,
        )
    }


# ============================================================ Web 公开运行装配（只读观察）
#: Web 上这一路的项目包键与名字（**只读观察**：不构建、不烧录、不解码）。
WEB_PROJECT_KEY = "stm32-observe"
WEB_PROJECT_LABEL = "STM32 只读观察（明文行）"


def web_facts() -> bench_target.Facts:
    """这块板在 Web 里怎么观察（**事实**；通信口从本机台架取，台架没点名时由系统枚举补上）。

    ⚠️ 本仓库这一路**只观察**：能力表里 build/flash/reset/symbolize/decode/metrics 逐项写着
    `unsupported` 并给了理由 —— 页面上看得到“做不到什么”，执行时也会当场拒，
    而不是拿一个“看着合理”的默认值顶上。
    """
    return bench_target.Facts(
        target_id=TARGET_ID,
        label=f"{TARGET_LABEL} · 只读观察",
        platform="stm32",
        device_key=DEVICE_KEY,
        device_label="STM32F103 参考板（IAR 裸机 / RT-Thread）",
        boundary="只读观察：不构建、不烧录、不动调制线（换固件请用你自己的工程）",
        project_key=WEB_PROJECT_KEY,
        project_label=WEB_PROJECT_LABEL,
        port_env=ENV_VARS[f"{DEVICE_KEY}.port"],
        baud=115200,
        # 这块板的 shell 把 CR / LF **各自**当成一次行结束（真机实测：发 `\r\n` 等于回车两次，
        # 一条命令跑两遍、屏上连出两行提示符）⇒ 协议与终端都只发 `\r`。
        line_ending="cr",
        identity_prefix="@PWINFO",
        identity_query="@PWINFO",
        identity_parser=parse_identity,
        # ⚠️ **设备侧自称的项目名**（`@PWINFO project=…`）：身份匹配就查它。
        # 直接从本模块的声明里读（一处事实、一处来源），不另写一份字符串。
        device_projects=tuple(
            name
            for name in (
                str(declaration().identity_expectation.values.get("project") or ""),
            )
            if name
        ),
        firmware_label="STM32F103 参考固件（明文身份行）",
        notes=(
            "身份按设备自报的 `@PWINFO project=… build_id=…` 明文行读；这一路不解码。",
            "通信口来自本机台架 `devices.%s.port`：没配就是空的候选项，不会去猜系统串口。"
            % DEVICE_KEY,
        ),
    )


def web_chain(*, runs_dir, repository, bus=None, transport=None) -> bench_target.Chain:
    """把这块板装成 Web 的一条只读观察链（传输可注入 ⇒ 没有硬件也能测同一份装配）。"""
    return bench_target.build(
        web_facts(),
        runs_dir=runs_dir,
        repository=repository,
        bus=bus,
        transport=transport,
    )


def vendor_tools() -> dict[str, tuple[str, ...]]:
    """这块板要用的**厂商可执行文件**（名字住在目标层：core 里不许出现它们）。

    ⚠️ 只报**名字**，不报路径：路径是本机事实（台架/环境变量/不猜），而“这套工具链
    叫什么”是这块板的知识。Doctor 只把它们逐个 `which` 一遍，缺了也只说影响这一路 Adapter。
    """
    return {
        "IAR 构建工具（IarBuild.exe）": ("IarBuild", "IarBuild.exe"),
        "C-SPY 命令行下载器（CSpyBat.exe）": ("CSpyBat", "CSpyBat.exe"),
        "GNU make（RT-Thread 那条 profile 用）": ("make", "make.exe"),
    }


__all__ = [
    "ADAPTERS",
    "DEVICE_KEY",
    "ENV_VARS",
    "FIRMWARE_BARE",
    "FIRMWARE_RT",
    "LAYER",
    "TARGET_ID",
    "TOOL_KEYS",
    "WEB_PROJECT_KEY",
    "WEB_PROJECT_LABEL",
    "bench_declaration",
    "declaration",
    "declarations",
    "doctor_declaration",
    "hwops_wirings",
    "parse_identity",
    "read_only_port",
    "web_chain",
    "web_facts",
    "vendor_tools",
]

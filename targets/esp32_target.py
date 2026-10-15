#!/usr/bin/env python3
"""ESP32 参考适配（**target-esp32**）：PlatformIO 构建 + esptool 校验 + 串口自报身份。

这份模块声明**这套工具链**的知识：命令怎么写、判据是什么、失败了怎么办、身份怎么问。
core 不认识 ESP32，也不认识 PlatformIO 或 esptool —— 它只按 `HardwareDeclaration` 的形状调。

## 本机事实从哪儿来

工具装在哪、工程根在哪、板子插在哪个口都不写在这里，按同一个梯子取值
（`--bench` 指定的台架文件 / `bench.local.json` → 环境变量 → 没有兜底值）：

| 槽 | 环境变量 | 说明 |
| --- | --- | --- |
| `tools.pio` | `PW_ESP32_PIO` | PlatformIO Core（`pio`）可执行文件 |
| `tools.esptool` | `PW_ESP32_ESPTOOL` | esptool 可执行文件（也可用 `python -m esptool`） |
| `projects.esp32-generic-fw` | `PW_ESP32_PROJECT` | PlatformIO 工程根 |
| `devices.esp32a.port` | `PW_ESP32_PORT` | 通信/烧录共用的串口 |

⚠️ 目标/固件 ID、工程入口与配置名都是**形状示例**：接你自己的工程时替换成你自己的值。
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
LAYER = "target-esp32"
TARGET_ID = "esp32-generic"
TARGET_LABEL = "ESP32 通用固件（PlatformIO + esptool）"
FIRMWARE_ID = "esp32-generic-fw"

ADAPTERS: tuple[str, ...] = (
    "esp32.pio-build",
    "esp32.pio-upload",
    "esp32.esptool-verify",
    "esp32.serial.read-only",
)
TOOL_KEYS: tuple[str, ...] = ("pio", "esptool")
ENV_VARS: dict[str, str] = {
    "pio": "PW_ESP32_PIO",
    "esptool": "PW_ESP32_ESPTOOL",
    FIRMWARE_ID: "PW_ESP32_PROJECT",
    "esp32a.port": "PW_ESP32_PORT",
}
DEVICE_KEY = "esp32a"
#: 烧录偏移（这块板的事实：应用分区从这里开始）。
FLASH_OFFSET = "0x10000"
#: 设备自报身份的那一行。
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
def declaration() -> contract_mod.HardwareDeclaration:
    return contract_mod.HardwareDeclaration(
        adapter_id=FIRMWARE_ID,
        target_id=TARGET_ID,
        label=TARGET_LABEL,
        layer=LAYER,
        module=Path(__file__).stem,
        project_entry="platformio.ini",
        build_config="esp32dev",
        bench_keys=("pio", FIRMWARE_ID, f"{DEVICE_KEY}.port"),
        tools=TOOL_KEYS,
        steps=(
            contract_mod.StepSpec(
                step="build",
                adapter_id="esp32.pio-build",
                label="构建（PlatformIO：看它的成功标记 + 核产物）",
                tool="pio",
                args=("run", "-d", "{project}", "-e", "{config}"),
                inputs=("platformio.ini",),
                artifacts=(".pio/build/{config}/firmware.bin", ".pio/build/{config}/firmware.elf"),
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=("[SUCCESS]",),
                    failure_markers=("*** [.pio",),
                    min_artifacts=2,
                    note="PlatformIO 失败时也可能退出码 0（例如依赖下载失败后回退）",
                ),
            ),
            contract_mod.StepSpec(
                step="flash",
                adapter_id="esp32.pio-upload",
                label="烧录（覆盖板上现有应用要显式同意）",
                tool="pio",
                args=("run", "-d", "{project}", "-e", "{config}", "-t", "upload",
                      "--upload-port", "{port}"),
                inputs=("platformio.ini",),
                timeout_s=600.0,
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=("[SUCCESS]",),
                    failure_markers=(
                        "Failed to connect", "A fatal error occurred",
                        "Timed out waiting for packet header", "*** [upload]",
                    ),
                    note="写完之后由 verify 那一步读回校验",
                ),
            ),
            contract_mod.StepSpec(
                step="verify",
                adapter_id="esp32.esptool-verify",
                label="烧录后读回校验（esptool：与本次构建的镜像逐字节比）",
                tool="esptool",
                args=("--port", "{port}", "verify_flash", FLASH_OFFSET,
                      "{project}/.pio/build/{config}/firmware.bin"),
                timeout_s=180.0,
                success=contract_mod.SuccessCriterion(
                    kind="log",
                    success_markers=("Hash of data verified.",),
                    failure_markers=("A fatal error occurred",),
                    note="判据是「读回来的和我这份一样」，不是「退 0」",
                ),
            ),
            contract_mod.StepSpec(
                step="debug-check",
                adapter_id="esp32.serial.read-only",
                label="打开串口读一小段（最小连通性检查）",
                args=("probe", "--port", "{port}"),
                timeout_s=20.0,
                notes=(
                    "这一步**不跑工具**：由入口注入的串口实现只读打开（打开前就把 DTR/RTS "
                    "按下去，所以不复位设备）。",
                ),
            ),
        ),
        faults=contract_mod.FaultKnowledge(
            markers=(
                contract_mod.FaultMarker(
                    "Failed to connect", "flash_failed",
                    "串口打不开或板子不在烧录模式：这次是**零写入**",
                ),
                contract_mod.FaultMarker(
                    "Timed out waiting for packet header", "flash_failed",
                    "板子没进下载模式（多半是有别的进程占着串口）",
                ),
                contract_mod.FaultMarker(
                    "A fatal error occurred", "flash_failed", "esptool 自己放弃了",
                ),
                contract_mod.FaultMarker(
                    "*** [.pio", "build_failed", "PlatformIO 报错：先看它上面那几行",
                ),
            ),
            recovery=(
                "先确认**没有别的程序占着这个串口**（串口监视器、另一个工作台）；"
                "再按一下板子的复位键重跑",
            ),
            unknown_advice="没见过的失败：先保留原文再判断，不要直接重试",
            step_defaults=(("build", "build_failed"), ("flash", "flash_failed")),
        ),
        capabilities=(
            contract_mod.CapabilityResult("build", "supported"),
            contract_mod.CapabilityResult("flash", "supported"),
            contract_mod.CapabilityResult("transport", "supported"),
            contract_mod.CapabilityResult("identity", "supported"),
            contract_mod.CapabilityResult(
                "symbolize", "supported",
                "构建产出 ELF；用工具链自带的 addr2line 还原地址",
            ),
        ),
        transport=contract_mod.TransportHint(
            kind="serial", baud=115200, line_ending="lf", read_only=True,
            adapter_id="esp32.serial.read-only",
            minimal_check="打开口、读一行就关（不写调制线 ⇒ 不复位设备）",
            notes=("打开串口不写 DTR/RTS：这正是「不会复位板子」的那条判据。",),
        ),
        identity=contract_mod.IdentityHint(
            query="@PWINFO", fields=("project", "build_id"), banner_markers=("@PWINFO",),
        ),
        identity_expectation=contract_mod.IdentityExpectation(
            fields=("project", "build_id"),
            values={"project": "esp32-generic-demo"},
            source=contract_mod.IdentitySource(
                artifact="build/identity.json", key="build_id", field="build_id",
                template="0x{value:08x}",
            ),
            note="期望值来自**本次构建的产物**（写死的常量证不了「这次烧的是这次编的」）",
        ),
        excluded=(),
        notes=("参考适配：声明的形状是框架的一部分；工具与工程位置由本机台架给。",),
    )


def declarations() -> tuple[contract_mod.HardwareDeclaration, ...]:
    return (declaration(),)


# ============================================================ 台架 / 体检 / 装配
def bench_declaration() -> bench_config.BenchDeclaration:
    return bench_config.BenchDeclaration(
        owner="esp32",
        tool_keys=TOOL_KEYS,
        firmware_ids=(FIRMWARE_ID,),
        device_keys=(DEVICE_KEY,),
    )


def _configured(value: str, slot: str) -> str:
    """声明必须带值（工具/路径类不接受空串）：没配就给一个一眼看出是占位的值。"""
    return value.strip() or f"<未配置：{slot}>"


def doctor_declaration() -> doctor_target.TargetDeclaration:
    machine = bench_config.active()
    pio = machine.tool("pio", env_var=ENV_VARS["pio"])
    esptool = machine.tool("esptool", env_var=ENV_VARS["esptool"])
    project = machine.project(FIRMWARE_ID, env_var=ENV_VARS[FIRMWARE_ID])
    requirements = (
        doctor_target.Requirement(
            key="tool.pio", title="PlatformIO Core（pio）",
            kind=doctor_target.RequirementKind.PATH, value=_configured(pio.value, "pio"),
            expected="可用",
            remediation="把 `pio` 写进本机台架文件（或设 PW_ESP32_PIO）指到 pio 可执行文件",
            source=bench_config.source_note(pio),
        ),
        doctor_target.Requirement(
            key="tool.esptool", title="esptool",
            kind=doctor_target.RequirementKind.PATH, value=_configured(esptool.value, "esptool"),
            expected="可用",
            remediation="把 `esptool` 写进本机台架文件（或设 PW_ESP32_ESPTOOL）",
            source=bench_config.source_note(esptool),
        ),
        doctor_target.Requirement(
            key="device.serial", title="串口承载（身份核验要读它）",
            kind=doctor_target.RequirementKind.MODULE, value="serial",
            expected="可导入", remediation="python -m pip install pyserial",
        ),
    )
    firmware = doctor_target.FirmwareDeclaration(
        firmware_id=FIRMWARE_ID, label="ESP32 通用固件",
        project_root=Path(project.value) if project.value else None,
        source=bench_config.source_note(project),
        requirements=(
            doctor_target.Requirement(
                key="project.entry", title="PlatformIO 工程入口（platformio.ini）",
                kind=doctor_target.RequirementKind.PATH, value="platformio.ini",
                expected="存在",
                remediation="把 `esp32-generic-fw` 写成本机 PlatformIO 工程根（台架键 = 固件 id）",
            ),
        ),
    )
    return doctor_target.TargetDeclaration(
        target_id=TARGET_ID,
        label=TARGET_LABEL,
        requirements=requirements,
        firmwares=(firmware,),
        roles=("profile", "build", "flash", "transport", "identity", "symbolize",
               "project", "host_suite"),
        expected_roles=("profile", "build", "flash", "transport", "identity", "symbolize",
                        "project", "host_suite"),
        project_root=None,
    )


def read_only_port(port: str, baud: int):
    """只读打开一个串口：**打开之前**就把调制线按下去，所以不会复位设备。"""
    import serial  # 只有真的要用串口时才 import

    handle = serial.Serial()
    handle.port = str(port)
    handle.baudrate = int(baud)
    handle.timeout = 0.05
    handle.dtr = False
    handle.rts = False
    return handle


def _has_pyserial() -> bool:
    try:
        import serial  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _bench_row(value: bench_config.BenchValue) -> tuple[bool, str]:
    return (bool(value.configured), bench_config.source_note(value) or "没有这一格")


def hwops_wirings() -> tuple[contract_mod.AdapterWiring, ...]:
    """统一入口要的装配：契约（随代码）+ 这一趟的本机值（随机器）。"""
    machine = bench_config.active()
    decl = declaration()
    project = machine.project(FIRMWARE_ID, env_var=ENV_VARS[FIRMWARE_ID])
    port_value = machine.device_port(DEVICE_KEY, env_var=ENV_VARS[f"{DEVICE_KEY}.port"])
    return (
        contract_mod.AdapterWiring(
            declaration=decl,
            project_dir=project.value,
            tools={key: machine.tool(key, env_var=ENV_VARS[key]).value for key in TOOL_KEYS},
            port=port_value.value,
            bench_values={
                **{key: _bench_row(machine.tool(key, env_var=ENV_VARS[key]))
                   for key in decl.bench_keys if key in TOOL_KEYS},
                FIRMWARE_ID: _bench_row(project),
                f"{DEVICE_KEY}.port": _bench_row(port_value),
            },
            registered=ADAPTERS,
            port_factory=read_only_port if _has_pyserial() else None,
            identity_parser=parse_identity,
            notes=("串口与烧录共用同一个口（这块板的真实情况就是这样）。",),
        ),
    )


# ============================================================ Web 公开运行装配（只读观察）
#: Web 上这一路的项目包键与名字（**只读观察**：不构建、不烧录、不解码）。
WEB_PROJECT_KEY = "esp32-observe"
WEB_PROJECT_LABEL = "ESP32 只读观察（明文行）"


def web_facts() -> bench_target.Facts:
    """这块板在 Web 里怎么观察（**事实**；通信口从本机台架取，台架没点名时由系统枚举补上）。

    ⚠️ 本仓库这一路**只观察**：能力表里 build/flash/reset/symbolize/decode/metrics 逐项写着
    `unsupported` 并给了理由 —— 页面上看得到“做不到什么”，执行时也会当场拒。
    """
    return bench_target.Facts(
        target_id=TARGET_ID,
        label=f"{TARGET_LABEL} · 只读观察",
        platform="esp32",
        device_key=DEVICE_KEY,
        device_label="ESP32 参考板（PlatformIO + esptool）",
        boundary="只读观察：不构建、不烧录、不动调制线（换固件请用你自己的工程）",
        project_key=WEB_PROJECT_KEY,
        project_label=WEB_PROJECT_LABEL,
        port_env=ENV_VARS[f"{DEVICE_KEY}.port"],
        baud=115200,
        line_ending="lf",
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
        firmware_label="ESP32 参考固件（明文身份行）",
        notes=(
            "身份按设备自报的 `@PWINFO project=… build_id=…` 明文行读；这一路不解码。",
            "行约定是 LF（这一路自己的事实，不是平台偏好）。",
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
    """这块板要用的**厂商可执行文件**（名字住在目标层：core 里不许出现它们）。"""
    return {
        "PlatformIO Core（pio）": ("pio", "pio.exe"),
        "esptool": ("esptool", "esptool.exe", "esptool.py"),
    }


__all__ = [
    "ADAPTERS",
    "DEVICE_KEY",
    "ENV_VARS",
    "FIRMWARE_ID",
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

#!/usr/bin/env python3
"""**公开运行装配**的公共部分（core）：把台架里登记的一台设备装成一条只读观察链。

目标层只交**事实**（这块板叫什么、身份怎么问、回答长什么样、波特率与行约定、台架槽位），
这里把它们与本机台架里的候选通信口拼成 core 认识的那几个形状：

```text
Facts（事实，目标层给）
  ├─ TargetProfile   ← 自述：连哪儿、能力表、复位策略（固定“打开不动调制线”）
  ├─ DeviceSpec      ← 设备登记：口来自台架 devices.<键>.port，**不写死**
  ├─ ProjectPackage  ← 项目包：套件与身份查询文本
  ├─ PlatformPlugins ← 插件束：串口实现、占用探测、受控一问一答、身份解析器
  └─ Pipeline        ← 采集与证据（**只读**：不发运构建/烧录链）
```

⚠️ 这个模块**不认识任何芯片**：串口实现从 `devices` 取（或由调用方注入假传输，
于是同一份装配可以在没有硬件、没有 pyserial 的机器上被测）。core 里不许出现平台名
（`platform_boundary.token_hit_problems`），所以芯片名只住在 `targets/` 的那些模块里。

⚠️ 三条硬规矩（都是"宁可少做一件事"）：
1. **只读**：`ResetPolicy.on_open` 恒为 `never`，`pipeline_allowed` 恒为 `False`
   —— 本仓库不发运第三方工程的构建/烧录链，"这台设备能不能被写"不由平台猜；
2. **不猜**：口没配就是空串（`sniff` 的空候选项、`连接` 时的 `NotConfigured`），
   身份前缀没声明就不发一个字节；
3. **能力如实**：做不到的逐项写 `unsupported` 并给理由，缺 `pyserial` 时
   `transport` 也如实降级（不冒充"能连"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable

CONSOLE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = CONSOLE_DIR.parent
for _directory in (str(CONSOLE_DIR), str(TOOLS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import bench_config  # noqa: E402  (core：台架配置层)
import device_registry  # noqa: E402  (core：设备登记)
import devices as devices_mod  # noqa: E402  (core：串口实现)
import platform_plugins  # noqa: E402  (core：插件束)
import project_package  # noqa: E402  (core：项目包)
import target_adapters  # noqa: E402  (core：适配器形状)
import target_profile  # noqa: E402  (core：目标自述)

# ⚠️ 这里**不** import `pipeline`：它是目标层模块，而本模块登记在 core
# （`platform_boundary.scope_problems`：core 不许 import 目标层）。所以这里只交出
# 造 `Pipeline` 需要的**那几个对象**（`pipeline_kwargs()`），真正的构造由组合入口做。

#: 能力表的说法就这几句，逐条写死理由 —— 别让页面替平台猜。
REASON_NO_BUILD = "本仓库不发运第三方工程的构建链：这一路只做只读观察"
REASON_NO_FLASH = "本仓库不发运第三方工程的烧录链：换固件请用你自己的工程与工具"
REASON_NO_SYMBOL = "没有交进符号还原入口（你自己的工程可以补这条链）"
REASON_NO_METRICS = "明文行输出没有结构化指标；接上你自己的项目包才有"
REASON_NO_DECODE = "这一路按明文行落盘（不解码）：设备输出什么，页面就显示什么"
REASON_NO_STORAGE = "这一路不碰设备存储"
REASON_NO_SERIAL = "这台机器上没有 pyserial：先 `pip install pyserial` 再启动服务"
#: 只读观察链的"构建环境名"：它不会被拿去构建任何东西，只是这条链的自述。
OBSERVE_ONLY_ENV = "observe-only"


@dataclass(frozen=True)
class Transport:
    """一条观察链要用的四件传输实现（默认取 core 的串口实现；测试可注入假的）。"""

    port_factory: Callable[[str, int], Any]
    port_probe: Callable[[str], str]
    exchange: Callable[[str, int, bytes, int, float], bytes]
    #: **系统串口枚举**（`() -> ((口名, 描述), …)`）。它是“零配置也能用”的基础候选源
    #: （需求 §6.11），所以跟其它三件一样属于**传输实现** —— 测试可以注入一份，
    #: 于是“枚举到哪些口”不再取决于跑测试的那台机器。
    list_ports: Callable[[], tuple[tuple[str, str], ...]] | None = None
    #: 这台机器上传输实现能不能用（空串 = 不可用 ⇒ 能力表如实降级）。
    note: str = ""
    #: 自述里那一行（"传输来自哪儿"）。
    label: str = "core 串口实现"

    @property
    def ready(self) -> bool:
        return bool(str(self.note).strip())

    def ports(self) -> tuple[tuple[str, str], ...]:
        """本机有哪些串口（没交枚举实现就是空 —— 如实说“枚举不了”）。"""
        if self.list_ports is None:
            return ()
        try:
            return tuple(self.list_ports())
        except Exception:  # noqa: BLE001 - 枚举失败不能弄断启动：如实当“没枚举到”
            return ()


#: 通用设备（系统枚举到、台架没点名的口）的键前缀。
GENERIC_KEY_PREFIX = "generic-"


def generic_key(port: str) -> str:
    """通用设备的键：**只由端口名决定**（同一个口永远同一个键，不随轮次变）。"""
    safe = re.sub(r"[^0-9a-zA-Z]+", "-", str(port or "")).strip("-").lower()
    return f"{GENERIC_KEY_PREFIX}{safe or 'port'}"


def generic_capabilities() -> tuple[Any, tuple[str, ...]]:
    """一条**普通串口终端**能做到什么（需求 §6.11/§6.12）。

    - `transport` / `identity` / `evidence`：supported（能连、能问、能落证据）；
    - `decode` / `metrics` / `build` / `flash` / `reset` / `symbolize`：unsupported + 理由
      —— “这不是一块适配过的板子”要**写着**，而不是拿默认值冒充。
    """
    declared = {
        "transport": "supported",
        "identity": "supported",
        "evidence": "supported",
        "decode": "unsupported",
        "metrics": "unsupported",
        "build": "unsupported",
        "flash": "unsupported",
        "reset": "unsupported",
        "symbolize": "unsupported",
        "storage_encryption": "unsupported",
    }
    reasons = {
        "decode": "通用串口：没有项目包字节字典 ⇒ 只显示原始行",
        "metrics": "通用串口：没有结构化指标来源",
        "build": "通用串口：没有工程与工具链（接自己的工程后就有了）",
        "flash": "通用串口：没有烧录通路（接自己的工程后就有了）",
        "reset": "通用串口：没有复位声明",
        "symbolize": "通用串口：没有 ELF 与符号工具",
        "storage_encryption": "通用串口：不碰设备存储",
    }
    notes = tuple(
        f"{name}：{reasons[name]}"
        for name in declared
        if declared[name] != "supported"
    )
    return target_profile.Capabilities(declared=dict(declared)), notes


def system_ports(transport: Transport | None = None) -> tuple[tuple[str, str], ...]:
    """**系统串口枚举**（`(口名, 描述)`）—— 零配置路径的基础候选源（需求 §2/§6.11）。

    ⚠️ 没有 pyserial（或调用方没交枚举实现）就是空元组：调用方要把这件事说给用户听，
    而不是假装“本机一台设备都没有”。
    """
    return (transport or serial_transport()).ports()


def merge_candidates(
    declared: Iterable[str], discovered: Iterable[str]
) -> tuple[tuple[str, ...], str]:
    """候选口 = **台架点名的** + **系统枚举到的**（去空、去重、保序）+ 一句说明。

    需求 §6.11：系统枚举是**基础候选源**，台架只增强；两者在**同一份列表**里合并去重，
    不得隐藏未配置、未适配或身份未知的系统串口。台架点名的排前面（它们带着别名、
    默认参数与已适配目标绑定），系统枚举到的排在后面。
    """
    rows: list[str] = []
    seen: set[str] = set()
    for raw in (*tuple(declared), *tuple(discovered)):
        name = str(raw or "").strip()
        if not name or name.upper() in seen:
            continue
        seen.add(name.upper())
        rows.append(name)
    note = "候选通信口 = 系统串口枚举 + 本机台架点名的那些口（合并去重）"
    return tuple(rows), note


def generic_spec_for_port(port: str) -> Any:
    """给“系统枚举到、台架没点名”的口造一条**通用设备**（需求 §6.11/§6.12）。

    它不是适配目标（没有别名、没有能力声明），但**必须能连**：登记之后，连接状态机、
    串口 owner、收发与证据路径全部复用同一套，不另写一份。
    """
    name = str(port or "").strip()
    description = devices_mod.port_description(name)
    caps, _notes = generic_capabilities()
    return device_registry.DeviceSpec(
        key=generic_key(name),
        label=(description or name or "通用串口"),
        port=name,
        role="generic",
        # ⚠️ 通用串口**不能**跑流水线：没有工程、没有工具链（想跑就先适配）。
        pipeline_allowed=False,
        boundary="通用串口（未适配）：能连、能收发、能断开重连；没有身份与能力声明",
        line_ending="crlf",
        # 终端里的回车发 `\r`：任何 shell 都认它，而把 `\r`/`\n` 各自当成一次回车的 shell
        # （RT-Thread msh 实测）收到 `\r\n` 会**一条命令跑两次**。设备协议的行结束保持 `crlf`
        # —— 有的固件只按 `\n` 断行，两者不能共用一个值。
        terminal_enter_ending="cr",
        capabilities=caps,
    )


def serial_transport() -> Transport:
    """core 的串口实现（真机路径）：打开不动 DTR/RTS、只读探测、受控一问一答。"""
    return Transport(
        port_factory=devices_mod.open_read_only,
        port_probe=devices_mod.port_access,
        exchange=devices_mod.sniff_exchange,
        list_ports=devices_mod.system_ports,
        note=devices_mod.pyserial_note(),
        label="core 串口实现（devices）",
    )


@dataclass(frozen=True)
class Facts:
    """一块板在本机要怎么观察（**事实**，由目标层交进来；这里不写任何芯片名）。"""

    target_id: str
    label: str
    platform: str
    device_key: str
    device_label: str
    #: 这块板在这份装配里的角色（页面上那句"它是干什么的"，也是边界声明）。
    boundary: str
    project_key: str
    project_label: str
    #: 通信口的台架环境变量名（取值梯子里的第二级；键本身是 `devices.<键>.port`）。
    port_env: str = ""
    baud: int = 115200
    line_ending: str = "crlf"
    terminal_backspace: str = "del"
    #: 终端直输的回车用哪个行结束符（空 = 沿用 `line_ending`）。
    #: 有的 shell 把 `\r` 与 `\n` **各自**当成一次回车（RT-Thread msh 实测）⇒ 给它发 `\r\n`
    #: 就是一条命令跑两次、屏上连出两行提示符。所以终端回车默认 `\r`，
    #: 而 `line_ending`（设备协议的断行）不受影响。
    terminal_enter_ending: str = "cr"
    #: 身份：前缀（回答长什么样）与查询文本（空 = 只等设备自己说，不发字节）。
    identity_prefix: str = ""
    identity_query: str = ""
    identity_parser: Callable[[str], dict[str, Any] | None] | None = None
    identity_unsupported_markers: tuple[str, ...] = ()
    #: 设备的自称名（与项目包键不同时登记在这里，兼容性判定要用）。
    device_projects: tuple[str, ...] = ()
    #: 固件条目在页面上叫什么（只描述固件特性，不许写板卡名义与固定端口）。
    firmware_label: str = ""
    notes: tuple[str, ...] = ()
    observe_seconds: float = 0.0
    #: 台架里这台设备的口取哪个字段（默认 `port`）。
    port_field: str = "port"


@dataclass(frozen=True)
class Chain:
    """一条装好的只读观察链（装配处拿它去造 `Pipeline`、`Assembly` 与观察会话）。"""

    facts: Facts
    profile: target_profile.TargetProfile
    project: project_package.ProjectPackage
    registry: device_registry.DeviceRegistry
    plugins: platform_plugins.PlatformPlugins
    #: 这条链用的传输实现（枚举与打开都从它来；装配处还要拿它做候选口合并）。
    transport: Transport
    #: 本机台架里这台设备的口（空串 = 没配；`sniff` 会把它算作"没有候选项"）。
    port: str
    port_source: str

    @property
    def firmware_id(self) -> str:
        return f"{self.facts.target_id}/default"

    @property
    def device_key(self) -> str:
        return self.facts.device_key


def capabilities(*, transport: Transport, identity: bool) -> tuple[Any, tuple[str, ...]]:
    """这一路**真能做到**什么（逐项表态）+ 做不到那几项的理由（给人看）。

    ⚠️ `Capabilities` 只装“能做到/做不到”，理由得另外说 —— 所以这里成对返回，
    理由进 `TargetProfile.notes`（页面的能力行与诊断包都带得走）。
    """
    declared = {
        "build": "unsupported",
        "flash": "unsupported",
        "reset": "unsupported",
        "symbolize": "unsupported",
        "metrics": "unsupported",
        "storage_encryption": "unsupported",
        "decode": "unsupported",
        "evidence": "supported",
        "transport": "supported" if transport.ready else "unsupported",
        "identity": (
            "supported" if (identity and transport.ready) else "unsupported"
        ),
    }
    reasons = {
        "build": REASON_NO_BUILD,
        "flash": REASON_NO_FLASH,
        "reset": REASON_NO_FLASH,
        "symbolize": REASON_NO_SYMBOL,
        "metrics": REASON_NO_METRICS,
        "storage_encryption": REASON_NO_STORAGE,
        "decode": REASON_NO_DECODE,
        "transport": REASON_NO_SERIAL,
        "identity": (
            REASON_NO_SERIAL if not transport.ready else "这一路没有声明身份协议"
        ),
    }
    notes = tuple(
        f"{name}：{reasons[name]}"
        for name in declared
        if declared[name] != "supported"
    )
    return target_profile.Capabilities(declared=dict(declared)), notes


def device_specs(facts: Facts, *, port: str, transport: Transport, caps: Any) -> tuple:
    """设备登记：口来自**本机台架**（空串就是空串 —— 不猜、不枚举系统串口冒充）。"""
    return (
        device_registry.DeviceSpec(
            key=facts.device_key,
            label=facts.device_label,
            port=port,
            role="observe",
            # ⚠️ 本仓库**不**允许跑流水线：这台设备能不能被写由使用者的工程决定。
            pipeline_allowed=False,
            boundary=facts.boundary,
            line_ending=facts.line_ending,
            terminal_backspace=facts.terminal_backspace,
            terminal_enter_ending=facts.terminal_enter_ending,
            capabilities=caps,
        ),
    )


def project_package_for(facts: Facts) -> project_package.ProjectPackage:
    """项目包：这一路的套件（本仓库为空）与身份查询文本。"""
    return project_package.ProjectPackage(
        key=facts.project_key,
        label=facts.project_label,
        token_domain="",
        suites=(),
        identity_query=str(facts.identity_query or ""),
        identity_query_delay_s=0.0,
        default_observe_seconds=float(facts.observe_seconds or 0.0),
    )


def profile_for(
    facts: Facts, *, port: str, caps: Any, reset_note: str, notes: tuple[str, ...] = ()
) -> target_profile.TargetProfile:
    """目标自述：只读、不动调制线、套件为空。"""
    return target_profile.TargetProfile(
        target_id=facts.target_id,
        label=facts.label,
        platform=facts.platform,
        project=facts.project_key,
        transport=target_profile.TransportSpec(
            kind="serial",
            port=None,
            baud=int(facts.baud),
            adapter=f"{facts.target_id}.serial.read-only",
            read_only=True,
            line_ending=facts.line_ending,
            terminal_backspace=facts.terminal_backspace,
            terminal_enter_ending=facts.terminal_enter_ending,
        ),
        reset=target_profile.ResetPolicy(
            on_open="never",
            pulse="explicit-only",
            adapter=None,
            default_request=False,
            note=reset_note,
        ),
        capabilities=caps,
        build_adapter=None,
        flash_adapter=None,
        symbol_adapter=None,
        suites=(),
        token_domain="",
        # ⚠️ 固定 False：本仓库不发运构建/烧录链（需求 §2/§6.10）。
        pipeline_allowed=False,
        device_key=facts.device_key,
        boundary=facts.boundary,
        notes=tuple(facts.notes) + tuple(notes),
        firmware_label=str(facts.firmware_label or ""),
        first_flash_allowed=False,
        requires_flasher_binding=False,
    )


def plugins_for(
    facts: Facts, *, registry: Any, transport: Transport, caps: Any
) -> platform_plugins.PlatformPlugins:
    """插件束：core 需要而只有目标/项目知道的那几件。"""
    return platform_plugins.PlatformPlugins(
        label=f"{facts.platform} · {facts.project_label}",
        platform=facts.platform,
        project=facts.project_key,
        device_registry=registry,
        port_factory=transport.port_factory,
        port_probe=transport.port_probe,
        library_factory=None,
        build_ids=None,
        decoder_factory=None,
        decoder_probe=None,
        decoder_backend=None,
        panic_parser=None,
        event_parser=None,
        metrics_series=None,
        token_domain="",
        identity_parser=facts.identity_parser,
        identity_reply_prefix=str(facts.identity_prefix or ""),
        device_projects=tuple(facts.device_projects),
        identity_unsupported_markers=tuple(facts.identity_unsupported_markers),
        sniff_exchange=transport.exchange,
        adapter_ids=(
            f"{facts.target_id}.serial.read-only",
            f"{facts.target_id}.discovery",
        ),
    )


def build(
    facts: Facts,
    *,
    runs_dir: Any,
    repository: Any,
    bus: Any = None,
    transport: Transport | None = None,
) -> Chain:
    """把一块板装成一条只读观察链（**只读**：不构建、不烧录、不打开发送、不复位）。"""
    wire = transport or serial_transport()
    bench = bench_config.active()
    value = bench.device_field(facts.device_key, facts.port_field, env_var=facts.port_env)
    port = str(value.value or "")
    caps, cap_notes = capabilities(
        transport=wire, identity=bool(facts.identity_prefix)
    )
    registry = device_registry.DeviceRegistry(
        device_specs(facts, port=port, transport=wire, caps=caps),
        probe=wire.port_probe,
        describe=devices_mod.port_description,
    )
    plugins = plugins_for(facts, registry=registry, transport=wire, caps=caps)
    project = project_package_for(facts)
    profile = profile_for(
        facts,
        port=port,
        caps=caps,
        reset_note="打开通信口**不写** DTR/RTS（不动调制线 ⇒ 不复位设备）",
        notes=cap_notes,
    )
    return Chain(
        facts=facts,
        profile=profile,
        project=project,
        registry=registry,
        plugins=plugins,
        transport=wire,
        port=port,
        port_source=str(value.detail or value.source or ""),
    )


class NoBuildDiscovery:
    """这一路**没有构建链**的发现适配器：如实交一份“没有产物”的 `Discovery`。

    ⚠️ `Pipeline.snapshot()` 会问发现适配器要工具与产物（页面顶栏与 run manifest 都要它）。
    只读观察链没有工程、没有工具链，所以这里**不**编路径、**不**猜产物 ——
    交一份各个字段为 `None` 的 `Discovery`，于是页面说“不知道”，而不是拿一个
    “看着合理”的默认值顶上。
    """

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id

    def discovery(self, project_dir: Any = None, pio_env: Any = None) -> Any:
        del project_dir, pio_env
        return target_adapters.Discovery(adapter_id=self.adapter_id)


def discovery_adapter(facts: Facts) -> NoBuildDiscovery:
    return NoBuildDiscovery(f"{facts.target_id}.discovery")


def pipeline_kwargs(chain: Chain, *, runs_dir: Any, repository: Any, bus: Any = None) -> dict:
    """造 `Pipeline` 需要的参数（**构造由组合入口做**：`Pipeline` 是目标层模块）。

    ⚠️ 除了发现适配器之外**不交**构建/烧录/符号适配器：这一路没有那三条链，
    用到时会如实报“没装配”，而不是拿一个“看着合理”的默认值顶上。
    ⚠️ `pio_env` 必须给一个**非空**值：空串会让 `Pipeline.pio_env` 去问构建适配器要
    "默认构建环境"，于是一个只读观察链会因为一件它根本没用到的事而报错（实测踩到）。
    这个名字不会拿去构建任何东西：它就写在这条链自己的自述里。
    """
    return {
        "project_dir": repository,
        "runs_dir": runs_dir,
        "pio_env": OBSERVE_ONLY_ENV,
        "adapters": {"discovery": discovery_adapter(chain.facts)},
        "plugins": chain.plugins,
        "project": chain.project,
        "devices": chain.registry,
        "capabilities": chain.profile.capabilities,
        "event_bus": bus,
    }


__all__ = [
    "Chain",
    "Facts",
    "GENERIC_KEY_PREFIX",
    "NoBuildDiscovery",
    "OBSERVE_ONLY_ENV",
    "Transport",
    "build",
    "capabilities",
    "device_specs",
    "discovery_adapter",
    "generic_capabilities",
    "generic_key",
    "generic_spec_for_port",
    "merge_candidates",
    "pipeline_kwargs",
    "plugins_for",
    "profile_for",
    "project_package_for",
    "serial_transport",
    "system_ports",
]

"""平台核心的**目标描述模型**（ / R1）：目标是谁、做得到什么、允不允许做。

需求见 （见公开文档）
（DEC-002 目标描述数据化、DEC-009 结果语义明确、DEC-010 AI 可诊断）。

这个模块是**纯数据 + 纯判断**，因此它可以被 core 层放心引用：

- 不 import 任何目标模块（`devices` / `pipeline` / `panic`）或项目模块（`assertions` /
  `metrics` / `app_events` / `record_file`）；
- 不 import 任何第三方包；不碰串口、文件、子进程；
- 没有"默认目标"这种东西：查不到就是查不到，由调用方决定怎么报（`UnknownTarget`）。

三条规矩（每一条都有 `test_core_layer.py` 的契约测试）：

1. **没声明 ≠ 支持。** 能力值只有三种：`supported` / `unsupported` / `unknown`；
   没写进 profile 的能力一律是 `unknown`。`unsupported`（做不到/不适用）与
   `unknown`（不知道）必须分开 —— 一个"看起来支持"的默认值比没有字段更糟。
2. **能力 ≠ 策略。** "做不做得到"（capability）与"允不允许做"（`pipeline_allowed`）
   是两件事：板 B 的串口完全打得开，但第一版**不许**构建/烧录它。
3. **只认受信适配器 ID。** profile 里存的是适配器**标识**（例如 `platformio.build`），
   不是可执行命令、也不是任意回调 —— 参数数组由代码生成，配置进不来（变更 §7 风险控制）。

`参考目标.py` 把现有 ESP32 示例项目 配置装进这个模型（R1 里仍**转调旧实现**：
字段取自 `devices.DEVICES` 与 `pipeline` 的常量，逐字段被契约测试钉住）；
`fake_target.py` 提供一个不含 ESP32/DHT/LittleFS/PlatformIO/COM 口的假目标，
证明通用层可以独立运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


class Capability:
    """能力值的三个取值（没有第四种，也没有默认值）。"""

    SUPPORTED = "supported"      # 目标做得到，平台也实现了
    UNSUPPORTED = "unsupported"  # 做不到 / 本版不适用（**不是失败**，是"不适用"）
    UNKNOWN = "unknown"          # 没声明：不知道，不许当成 supported

    ALL = (SUPPORTED, UNSUPPORTED, UNKNOWN)


#: 一个目标**可能**具备的能力名。没在 `Capabilities.declared` 里出现的一律 `unknown`。
#: 名字表是封闭的：写错名字当场报错，而不是悄悄变成 "unknown"。
CAPABILITY_NAMES = (
    "build",      # 交叉编译出产物（含构建身份）
    "flash",      # 把产物写进设备
    "reset",      # 显式复位脉冲（只有显式请求才允许做）
    "transport",  # 打开/关闭/读写一条 transport（今天只有串口一种实现）
    "symbolize",  # 用平台工具链把崩溃地址还原成源码位置
    "decode",     # 把原始字节解成可读行（平台协议解码器 + 精确选库）
    "identity",   # 设备自报身份（`@PWINFO` 或明文横幅回退）
    "evidence",   # 只追加证据（raw 字节 + 事件时间线 + 会话清单）
    "metrics",    # 指标 schema（由项目包定义，平台只搬运）
    #  / L5：加密存储。需求 §10.1 要求有一个**真实的能力不足反例**，
    # 而"加密存储验证"就是它 —— 名字进名单（拼错会当场报错）不代表目标有它：
    # STM32/RT-Thread 两路都**明确声明** `unsupported`（这一版确实没有加密存储）。
    "storage_encryption",
)


def known_capabilities() -> tuple[str, ...]:
    return CAPABILITY_NAMES


#: **会话面向的四个能力**，以及它们分别由目标能力表里的哪一项决定（ / R2a）。
#:
#: 这张表回答一个很具体的问题：实时观察快照里的 `capabilities`（页面据它决定
#: "这一版到底有没有这个能力"、会话 manifest 也记它）该从哪里来。
#: 答案是**目标的唯一能力声明**，而不是在会话里再写一遍四个布尔值 ——
#: 过去两边各写一份，改了一处忘另一处，页面与诊断包就会互相矛盾（）。
SESSION_CAPABILITY_MAP: tuple[tuple[str, str], ...] = (
    ("send", "transport"),     # 能不能往这条 transport 写字节（命令框 / 终端直输）
    ("identity", "identity"),  # 能不能查身份（@PWINFO / 明文横幅回退）
    ("decode", "decode"),      # 能不能解码（平台解码器 + 按构建号精确选库）
    ("evidence", "evidence"),  # 能不能留下只追加证据（raw + 事件 + 会话清单）
)


def session_capability_names() -> tuple[str, ...]:
    """会话快照里那四个键的名字（顺序固定，跟 `SESSION_CAPABILITY_MAP` 一致）。"""
    return tuple(name for name, _source in SESSION_CAPABILITY_MAP)


def session_capabilities(capabilities: Capabilities | None) -> dict[str, bool]:
    """把目标的能力表投影成会话的四项能力。

    ⚠️ `unknown`（没声明）一律按**不支持**算 —— "没提" 不等于 "支持"；
    这是 R1 立下的规矩，页面文案与 manifest 都靠它保持诚实。
    """
    table = capabilities if capabilities is not None else Capabilities()
    return {
        name: table.is_supported(source)
        for name, source in SESSION_CAPABILITY_MAP
    }


def _validate_capability(name: str, value: Any) -> None:
    if name not in CAPABILITY_NAMES:
        raise ValueError(
            f"不认识的能力名 {name!r}：可用的是 {list(CAPABILITY_NAMES)}"
            "（拼错的名字不该悄悄变成 unknown）"
        )
    if value not in Capability.ALL:
        raise ValueError(
            f"能力 {name!r} 的值 {value!r} 不合法：只能是 {list(Capability.ALL)}"
        )


@dataclass(frozen=True)
class Capabilities:
    """目标声明自己有哪些能力；**只声明它知道的那些**。"""

    declared: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in self.declared.items():
            _validate_capability(name, value)

    # ------------------------------------------------------------------ 查询
    def get(self, name: str) -> str:
        """取一个能力的值；没声明就是 `unknown`（绝不会是 `supported`）。"""
        if name not in CAPABILITY_NAMES:
            raise KeyError(f"不认识的能力名 {name!r}（可用：{list(CAPABILITY_NAMES)}）")
        return self.declared.get(name, Capability.UNKNOWN)

    def is_supported(self, name: str) -> bool:
        return self.get(name) == Capability.SUPPORTED

    def declared_only(self) -> tuple[str, ...]:
        """目标**明确表过态**的能力（`supported` 或 `unsupported`）。"""
        return tuple(name for name in CAPABILITY_NAMES if name in self.declared)

    def undeclared(self) -> tuple[str, ...]:
        """目标**没提**的能力 —— 与"声明为不支持"是两件不同的事。"""
        return tuple(name for name in CAPABILITY_NAMES if name not in self.declared)

    def unsupported(self) -> tuple[str, ...]:
        return tuple(
            name for name in CAPABILITY_NAMES
            if self.declared.get(name) == Capability.UNSUPPORTED
        )

    def to_json(self) -> dict[str, str]:
        """**全量**导出（含 `unknown`）：诊断包要能一眼看出缺了什么（DEC-010）。"""
        return {name: self.get(name) for name in CAPABILITY_NAMES}

    @classmethod
    def of(cls, **values: str) -> "Capabilities":
        """`Capabilities.of(build=Capability.SUPPORTED, …)`，图个可读。"""
        return cls(declared=dict(values))


@dataclass(frozen=True)
class TransportSpec:
    """会话核心要连的东西：怎么连、连哪儿、走哪个适配器。"""

    kind: str                       # "serial" / "fake"（R1 真实现只有 serial）
    port: str | None = None         # 端口名（串口世界是 COMx，但不是**写死**的 COM<n>）
    baud: int | None = None
    adapter: str | None = None      # 受信适配器 ID，见 `参考目标.ADAPTERS`
    read_only: bool = True          # 打开时不许动 dtr/rts（I-022 / ）
    # 项目/设备配置（不是平台策略）：命令框与终端直输的行约定与退格字节。
    line_ending: str = "crlf"
    terminal_backspace: str = "del"
    #: 终端直输的**回车**用哪个结束符（空 = 沿用 `line_ending`）：
    #: `line_ending` 是设备协议的断行，它是“人按的那个键”—— 混用会让把
    #: `\r`/`\n` 各自当成一次回车的 shell 一条命令跑两次。
    terminal_enter_ending: str = ""
    terminal_local_echo: bool = False
    #: 这条串口是一个控制台终端（由目标/项目声明）。只影响**页面默认显示哪种视图**，
    #: 不改字节流，也不改证据；用户仍可以手动切。
    terminal_console: bool = False


@dataclass(frozen=True)
class ResetPolicy:
    """复位是**被测设备的副作用**，所以它必须在 profile 里说清楚。"""

    on_open: str = "never"          # 打开 transport 时动不动复位线（必须 never）
    pulse: str = "explicit-only"    # 只有显式请求才脉冲
    settle_s: float | None = None   # 两次复位之间要等的静默时间（横幅会被截断）
    adapter: str | None = None
    note: str = ""
    #: **自动验证默认**要不要复位（页面与 API 都没有明说时用哪一个）。
    #: ⚠️ 这是"这台设备的复位动的是什么"的结论，不是界面偏好：
    #:
    #: - 串口调制线复位（DTR/RTS）不会占住任何独占资源 ⇒ 参考组合默认**开**
    #:   （A1 要求捕获从完整启动横幅开始，不复位就拿不到横幅）；
    #: - 走**调试探针**的复位（SWD 附加）会占住探针，实测会让 ST-LINK 需要拔插
    #:   （）⇒ 默认**关**，只有人明确要求才做。
    #:
    #: 没声明一律按 `False`（"不说就不做"）—— 一个会动硬件的默认值不该是平台猜的。
    default_request: bool = False


@dataclass(frozen=True)
class TargetProfile:
    """一个目标的完整描述：它是谁、连哪儿、能不能构建/烧录/符号化、有哪些套件。"""

    target_id: str
    label: str
    platform: str                  # "esp32" / "fake" …（自由字符串，仅用于展示与分组）
    project: str | None            # 提供业务语义的项目包（"示例项目" / None）
    transport: TransportSpec
    reset: ResetPolicy
    capabilities: Capabilities
    build_adapter: str | None = None
    flash_adapter: str | None = None
    symbol_adapter: str | None = None
    suites: tuple[str, ...] = ()            # 这个目标上可跑的项目套件
    token_domain: str | None = None         # 平台协议解码用的 domain（项目给的）
    #: 平台**策略**：这个目标允不允许被构建/烧录（与"做不做得到"分开）。
    #: 沿用旧词汇 `DeviceSpec.pipeline_allowed`，页面对面因此不用改（DEC-008）。
    pipeline_allowed: bool = False
    #: R1 里 profile 是 `devices.DEVICES` 的**投影**，用 key 记住它对应哪一条登记。
    device_key: str | None = None
    boundary: str = ""
    notes: tuple[str, ...] = ()
    #: ：**固件条目**在页面上叫什么。
    #: ⚠️ 这一条只许描述**固件的特性**（项目 / 日志能力 / 运行栈），不得出现板卡名称
    #: （“板 A / 板 B”）与固定通信口 —— 那是本机台架的事实，不是固件的属性。
    #: 空串 = 没声明 ⇒ 调用方退到“平台 · 项目包”，绝不拿 `label`（它可能是台架名义）去顶。
    firmware_label: str = ""
    #: ：这份固件要求设备自报的 `protocols` 里有哪几项（都是**正数**才算）。
    #: 用它回答“这份固件能不能用在这个设备上”（`device_sniff.compatibility`）。
    required_protocols: tuple[str, ...] = ()
    #: ：这几项设备**明确**自报支持就不匹配（= 这份固件没有它）。
    #: 同一块板上的两份构建（带 / 不带 Token Log）靠它区分；没报 / 说 0 都不拒。
    absent_protocols: tuple[str, ...] = ()
    #: ：身份**未知**时准不准走首次烧录。
    #: ⚠️ 缺省是 **False**：会动硬件的宽容值不该由平台猜（与 `ResetPolicy.default_request` 同规矩）。
    first_flash_allowed: bool = False
    #:  / D3：这个目标要不要一个**显式**的烧录器绑定（台架 `devices.<键>.flasher`）。
    #: 通信口与烧录通路是**两个资源**的目标（串口 + SWD 探针）必须写 True ——
    #: 缺了这条绑定，平台就不知道该烧哪一个，只能拒（不按枚举顺序猜）。
    #: ⚠️ 缺省 False：通信与烧录共用一个口的目标（参考目标那一类）本来就不需要它。
    requires_flasher_binding: bool = False

    # ------------------------------------------------------------------ 查询
    def capability(self, name: str) -> str:
        return self.capabilities.get(name)

    def supports(self, name: str) -> bool:
        return self.capabilities.is_supported(name)

    def to_json(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "platform": self.platform,
            "project": self.project,
            "device_key": self.device_key,
            "transport": {
                "kind": self.transport.kind,
                "port": self.transport.port,
                "baud": self.transport.baud,
                "adapter": self.transport.adapter,
                "read_only": self.transport.read_only,
                "line_ending": self.transport.line_ending,
                "terminal_backspace": self.transport.terminal_backspace,
                "terminal_enter_ending": self.transport.terminal_enter_ending,
                "terminal_local_echo": self.transport.terminal_local_echo,
            },
            "reset": {
                "on_open": self.reset.on_open,
                "pulse": self.reset.pulse,
                "settle_s": self.reset.settle_s,
                "adapter": self.reset.adapter,
                "note": self.reset.note,
                # 自动验证的**默认**（页面与 API 都不明说时用哪个），见 `ResetPolicy`。
                "default_request": bool(self.reset.default_request),
            },
            "capabilities": self.capabilities.to_json(),
            "adapters": {
                "build": self.build_adapter,
                "flash": self.flash_adapter,
                "symbol": self.symbol_adapter,
            },
            "suites": list(self.suites),
            "token_domain": self.token_domain,
            "pipeline_allowed": self.pipeline_allowed,
            "boundary": self.boundary,
            "notes": list(self.notes),
            # ：固件条目的特性名与兼容性声明（**只增字段**，旧读者不认识就不读）。
            "firmware_label": self.firmware_label,
            "required_protocols": list(self.required_protocols),
            "absent_protocols": list(self.absent_protocols),
            "first_flash_allowed": bool(self.first_flash_allowed),
            "requires_flasher_binding": bool(self.requires_flasher_binding),
        }


class UnknownTarget(LookupError):
    """查不到这个 target_id：**没有默认目标**，调用方必须把这件事说出来。"""


class TargetRegistry:
    """手上认识哪些目标。查不到就是查不到 —— 不编一个默认的出来。"""

    def __init__(self, profiles: Iterable[TargetProfile] = ()) -> None:
        self._profiles: dict[str, TargetProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: TargetProfile) -> TargetProfile:
        if not profile.target_id:
            raise ValueError("target_id 不能为空")
        if profile.target_id in self._profiles:
            raise ValueError(f"target_id 重复：{profile.target_id!r}")
        self._profiles[profile.target_id] = profile
        return profile

    def get(self, target_id: str) -> TargetProfile | None:
        return self._profiles.get(str(target_id or ""))

    def require(self, target_id: str) -> TargetProfile:
        profile = self.get(target_id)
        if profile is None:
            raise UnknownTarget(
                f"没有登记这个目标 {target_id!r}：已知的是 {sorted(self._profiles)}"
                "（平台不会替它编一份默认配置）"
            )
        return profile

    def by_device_key(self, device_key: str) -> TargetProfile | None:
        for profile in self._profiles.values():
            if profile.device_key == device_key:
                return profile
        return None

    def profiles(self) -> list[TargetProfile]:
        return list(self._profiles.values())

    def to_json(self) -> list[dict[str, Any]]:
        return [profile.to_json() for profile in self._profiles.values()]

    def __len__(self) -> int:
        return len(self._profiles)

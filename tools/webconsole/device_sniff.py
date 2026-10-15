"""端口优先的设备嗅探：**形状与判断**（ / D0，core）。

这一层只回答四个问题，而且四个答案都由**注入的数据**决定：

1. 一次受控查询留下了什么（`PortObservation`：TX/RX 字节、结论、理由、时间）；
2. 设备卡片要给页面看什么（`DeviceCard`：显示序号、通信口、可用性、版本、最近活动）；
3. 身份状态是哪一种（`IDENTITY_*`）—— **只有四种**，且必须把"没法判定"与"没事"分开；
4. 这份固件能不能用在这次嗅探选中的设备上（`compatibility()`）。

⚠️ **这个模块不许认识任何具体设备。** 探针命令、行结束符、等待时长、回答前缀、项目名、
目标适配器标识全部是**声明**，由 Target Adapter / Project Package 交进来；core 只按声明办事，
不写"如果是 ESP32 就……"。判据在 `test_core_layer.py`（core 模块里出现平台标识就红）。

⚠️⚠️ **Device Num 只是本次嗅探的显示序号。** 它随重新嗅探变化，**不是**持久设备身份：
`DeviceCard.num` 与 `SCAN_SEQ_PERSISTENT = False` 一起写进证据，让读报告的人一眼看出
"这个 3 是这一趟里的第 3 个，不是设备编号"。历史关联、烧录路由、权限判断、证据匹配
都不许用它 —— 需求 §3 与工程理念 §12 都点名了这条。

⚠️ 这个模块**不打开串口、不读文件、不碰时钟**：时间由调用方盖（`started_utc` / `ended_utc`
是入参不是现算值），于是同一份输入永远得到同一份结论，夹具可以直接对拍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

try:  # 包内 import
    from . import identity as identity_mod
    from .device_registry import (
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_FREE,
        ACCESS_UNKNOWN,
    )
except ImportError:  # 直接跑脚本/测试时的回退
    import identity as identity_mod  # type: ignore[no-redef]
    from device_registry import (  # type: ignore[no-redef]
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_FREE,
        ACCESS_UNKNOWN,
    )

#: 嗅探结果、卡片与 run manifest 里的 `device` 块共用的形状版本。
SCHEMA = 1

#: Device Num 的语义声明（写进证据；`False` = **不是**持久设备身份）。
SCAN_SEQ_PERSISTENT = False

# ============================================================ 身份状态（需求 §4）
#: 已确认：响应符合声明且字段通过校验。
IDENTITY_CONFIRMED = "confirmed"
#: 未知：无响应、空板、旧固件或不支持身份协议。
IDENTITY_UNKNOWN = "unknown"
#: 冲突：返回了身份，但与当前选择或预期不兼容（**默认禁止烧录**）。
IDENTITY_CONFLICT = "conflict"
#: 不可查询：端口被占用、消失或打开失败。
IDENTITY_UNQUERYABLE = "unqueryable"

IDENTITY_STATES: tuple[str, ...] = (
    IDENTITY_CONFIRMED,
    IDENTITY_UNKNOWN,
    IDENTITY_CONFLICT,
    IDENTITY_UNQUERYABLE,
)

# ===================================================== 单口探针结论（机器可读）
#: 收到可解析、且通过声明校验的回答。
PROBE_OK = "ok"
#: 写出去之后在等待窗口内没有拿到符合声明的回答。
PROBE_NO_RESPONSE = "no_response"
#: 有回答，但不是声明要求的那条协议（前缀/JSON 不符合）—— 与"没回答"分开。
PROBE_MALFORMED = "malformed"
#: 设备明确回"不认识这条命令"（那条回答的文本由设备/项目声明，core 不认字）。
PROBE_UNSUPPORTED = "unsupported"
#: 端口被其他 owner 占着（**不抢占、不关闭别人的 owner**）。
PROBE_PORT_BUSY = "port_busy"
#: 端口不存在（拔了 / 驱动没绑上）。
PROBE_PORT_ABSENT = "port_absent"
#: 端口在，但打不开（权限、驱动、半死状态）。
PROBE_OPEN_FAILED = "open_failed"
#: 本轮**刻意没有发字节**（例如端口被别人占着、或候选口超过这一轮的上限）。
PROBE_SKIPPED = "skipped"

PROBE_OUTCOMES: tuple[str, ...] = (
    PROBE_OK,
    PROBE_NO_RESPONSE,
    PROBE_MALFORMED,
    PROBE_UNSUPPORTED,
    PROBE_PORT_BUSY,
    PROBE_PORT_ABSENT,
    PROBE_OPEN_FAILED,
    PROBE_SKIPPED,
)

#: 探针结论 → 身份状态的**唯一**映射。
#: ⚠️ `conflict` 不在这张表里：冲突是"和设备对过一次之后"才知道的事
#: （见 `identity_state()`），不是单次查询能得出的 — 把两种来源混进一张表，
#: 下一个人就会以为探针能直接报冲突。
_PROBE_STATE: Mapping[str, str] = {
    PROBE_OK: IDENTITY_CONFIRMED,
    PROBE_NO_RESPONSE: IDENTITY_UNKNOWN,
    PROBE_MALFORMED: IDENTITY_UNKNOWN,
    PROBE_UNSUPPORTED: IDENTITY_UNKNOWN,
    PROBE_PORT_BUSY: IDENTITY_UNQUERYABLE,
    PROBE_PORT_ABSENT: IDENTITY_UNQUERYABLE,
    PROBE_OPEN_FAILED: IDENTITY_UNQUERYABLE,
    PROBE_SKIPPED: IDENTITY_UNQUERYABLE,
}


def identity_state(outcome: str, *, matches_expectation: bool | None = None) -> str:
    """单次查询的结论 + 可选的对表结果 ⇒ 身份状态。

    `matches_expectation` 只对 `PROBE_OK` 有意义：
    `False` ⇒ `conflict`（"设备回答了，但回答的不是我要找的那一块"）；
    `None` ⇒ 还没对（本轮只看这一口，不做兼容性判断）⇒ `confirmed`；
    `True` ⇒ `confirmed`。

    ⚠️ 不认识的结论**不当成成功**：返回 `unknown`（"没法判定"与"没事"必须分开，
    工程理念 §3）。
    """
    if outcome not in PROBE_OUTCOMES:
        return IDENTITY_UNKNOWN
    state = _PROBE_STATE[outcome]
    if state == IDENTITY_CONFIRMED and matches_expectation is False:
        return IDENTITY_CONFLICT
    return state


def state_rank(state: str) -> int:
    """给页面排序/筛选用的稳定次序（越靠前越"能用"）。

    ⚠️ 这里是**显示顺序**，不是优先级判断：能不能跑由 `compatibility()` 说。
    """
    order = (
        IDENTITY_CONFIRMED,
        IDENTITY_UNKNOWN,
        IDENTITY_CONFLICT,
        IDENTITY_UNQUERYABLE,
    )
    try:
        return order.index(state)
    except ValueError:
        return len(order)


# ==================================================== 声明式身份探针（数据，非分支）
#: 一条探针最多问几遍。**这是"不许重试风暴"的具体形式**：对未知设备反复发命令，
#: 既是打扰也可能是把设备日志冲垮的原因（本项目实测过：连发把命令挤成
#: `s…: command not found.`）。
MAX_ATTEMPTS_PER_PROBE = 3


@dataclass(frozen=True)
class IdentityProbe:
    """一条**只读**身份查询的完整声明。

    全部字段来自 Target Adapter / Project Package —— core 不写死 `INFO JSON`、
    不写死 LF、不写死 1 秒，也不写死"回答要以什么开头"（那是这条协议的文本，
    不是平台知识）。

    `prefix` 是**这条协议**的标志（例如某份固件用 `@NAME `）；空串表示"不要求前缀，
    只要非空且 JSON 能解析"。`unsupported_markers` 是设备用**自然语言**回"不认识这条命令"
    时的若干特征串，由设备/项目声明。
    """

    probe_id: str
    #: 只读查询文本（不含行结束符）。
    command: str
    line_ending: str = "lf"
    baud: int = 115200
    #: 等多久算"没有回答"。
    wait_s: float = 1.0
    #: 这一次问**没拿到回答**时，重发之前等多久（秒）。
    #: ⚠️ 平台默认 **4 s**（落在使用者要求的 3～5 s 里）；某台设备需要更久或更短时，
    #: 改的是**它的声明**，不是 core —— 间隔长短是那一台设备控制台就绪快不快的事实。
    #: 连发是明确错的做法：本平台实测过，紧接着上一次提问发命令会被设备日志洪流挤坏
    #: （`s…: command not found.`）。
    retry_gap_seconds: float = 4.0
    #: 这一条探针**最多问几次**（含第一次）：1 = 不问第二遍。
    attempts: int = MAX_ATTEMPTS_PER_PROBE
    #: 回答行的前缀标志（空串 = 不要求）。
    prefix: str = ""
    #: 单次查询最多读多少字节（防住对着聊天设备读出无穷流）。
    max_bytes: int = 4096
    #: 设备回"不认识这条命令"的特征串（由设备/项目声明）。
    unsupported_markers: tuple[str, ...] = ()    #: 用哪一个**解析器**把回答变成字段：
    #: ⚠️ 空串 = 平台自带的通用格式（前缀 + JSON，见 `sniff_service.parse_identity`）；
    #: 非空 = 由装配处按这个名字注进来的**项目自带解析器**（例如 RT-Thread 那行是
    #: `@PWINFO build=0x… fw=… protocol=… domain=…`，不是 JSON）。
    #: 名字是声明，实现在项目层 —— core 不认识任何一家的语法。
    parser_id: str = ""
    def __post_init__(self) -> None:
        if not str(self.probe_id or "").strip():
            raise ValueError("探针必须有 probe_id（证据里要说清这一趟用的是哪一条）")
        if not str(self.command or "").strip():
            raise ValueError("探针必须有命令文本（空命令＝无声地什么都不做）")
        if int(self.baud) <= 0:
            raise ValueError("baud 必须是正数")
        if float(self.wait_s) < 0:
            raise ValueError("wait_s 不能是负数")
        if int(self.attempts) < 1:
            raise ValueError("attempts 至少是 1（0 次＝这条探针什么都不做却又占着名额）")
        if int(self.attempts) > MAX_ATTEMPTS_PER_PROBE:
            raise ValueError(
                f"attempts 最多 {MAX_ATTEMPTS_PER_PROBE} 次（对未知设备反复发命令就是重试风暴）"
            )
        if int(self.attempts) > 1 and float(self.retry_gap_seconds) <= 0:
            raise ValueError(
                "声明重试就必须给 retry_gap_seconds（连发会把设备的回答挤坏，不是重试）"
            )
        if int(self.max_bytes) <= 0:
            raise ValueError("max_bytes 必须是正数")

    def to_json(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "command": self.command,
            "line_ending": self.line_ending,
            "baud": int(self.baud),
            "wait_s": float(self.wait_s),
            "retry_gap_seconds": float(self.retry_gap_seconds),
            "attempts": int(self.attempts),
            "prefix": self.prefix,
            "max_bytes": int(self.max_bytes),
            "unsupported_markers": list(self.unsupported_markers),
            "parser_id": self.parser_id,
        }


def outcome_for_reply(
    reply: str,
    *,
    prefix: str = "",
    unsupported_markers: tuple[str, ...] = (),
) -> str:
    """一行回答 ⇒ 探针结论（**纯文本判断**，不看串口）。

    规矩（顺序不能换）：先认"设备说不认识这条命令"，再认"这是我等的那条协议"，
    最后才把"有字但不是它"归到 `malformed` —— 一个不认识的回答**不是**身份，
    但它确实是一次回答，所以不能和"没回答"混起来（需求 §2.2）。
    """
    text = str(reply or "").strip()
    if not text:
        return PROBE_NO_RESPONSE
    for marker in unsupported_markers:
        if marker and marker in text:
            return PROBE_UNSUPPORTED
    if prefix:
        for line in text.splitlines():
            if line.strip().startswith(prefix):
                return PROBE_OK
        return PROBE_MALFORMED
    return PROBE_OK


# ======================================================= 单口观测（一次查询的证据）
@dataclass(frozen=True)
class PortObservation:
    """一次受控查询留下的**全部**事实（TX/RX 是字节，不是转述）。

    `rx_text` 是给人读的写法，`rx_hex` / `tx_hex` 才是证据；`raw` 那一行原文留在
    `identity_fields` 的 `raw` 里（`identity.normalize()` 的产出）。
    """

    port: str
    outcome: str
    #: 探测得到的端口占用状态（core 的四态）。
    access: str = ACCESS_UNKNOWN
    #: 设备自报字段（`identity.normalize()` 的产出；没有就是全 `None`）。
    identity_fields: Mapping[str, Any] = field(default_factory=dict)
    #: 这次查询用的探针（人可读的标识 + 完整声明）。
    probe_id: str = ""
    #: 这是这一条探针的**第几次**问（1 = 第一次）。重试与否要能从证据里看出来。
    attempt: int = 1
    #: 这一条探针**允许**问几次（声明值）—— 于是"没再试"与"不允许再试"分得开。
    attempts: int = 1
    tx_hex: str = ""
    rx_hex: str = ""
    rx_text: str = ""
    baud: int | None = None
    #: 人话理由（"无响应 1.0 s" / "端口被其他 owner 占用" …）；不许是空串却声称有结论。
    reason: str = ""
    started_utc: str = ""
    ended_utc: str = ""
    duration_ms: int | None = None
    #: 这个口上**上一次** run / 观察会话的最近活动（来自证据，不是猜的设备历史）。
    last_activity_utc: str | None = None
    last_activity_note: str = ""

    @property
    def identity_state(self) -> str:
        return identity_state(self.outcome)

    def to_json(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "outcome": self.outcome,
            "access": self.access,
            "identity_state": self.identity_state,
            "identity_fields": dict(self.identity_fields or {}),
            "probe_id": self.probe_id,
            "attempt": int(self.attempt),
            "attempts": int(self.attempts),
            "tx_hex": self.tx_hex,
            "rx_hex": self.rx_hex,
            "rx_text": self.rx_text,
            "baud": self.baud,
            "reason": self.reason,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "duration_ms": self.duration_ms,
            "last_activity_utc": self.last_activity_utc,
            "last_activity_note": self.last_activity_note,
        }


def availability(observation: PortObservation) -> tuple[bool, str]:
    """这个口现在能不能用（给 run / 实时观察用）+ 不可用时的人话原因。

    ⚠️ **"设备没回答"不等于"这个口不能用"。** 空板/旧固件是**正常**的一类设备，
    它的首次烧录路径是需求 §4 明确要支持的；把"无响应"判成"不可用"会直接把那条路径堵死。
    真正"不可用"的是**资源层面**的三件事：被别人占着、口不在、打不开。
    """
    access = str(observation.access or ACCESS_UNKNOWN)
    if observation.outcome == PROBE_PORT_BUSY or access == ACCESS_BUSY:
        return False, "端口被其他 owner 占用（不抢占、不关闭别人的 owner）"
    if observation.outcome == PROBE_PORT_ABSENT or access == ACCESS_ABSENT:
        return False, "这个通信口当前不存在"
    if observation.outcome == PROBE_OPEN_FAILED:
        return False, f"这个通信口打不开{('：' + observation.reason) if observation.reason else ''}"
    if access == ACCESS_UNKNOWN:
        # 占用状态都没拿到 ⇒ 如实说"状态未知"，但仍允许往下走：
        # 真正的独占由打开端口那一刻的出错来判（宁可让服务端拒一次）。
        return True, "占用状态未知（打开时才知道）"
    return True, ""


# ================================================================ 设备卡片
@dataclass(frozen=True)
class DeviceCard:
    """页面上的一张设备卡片（需求 §3 的字段表）。

    ⚠️ `num` 是**本次嗅探的显示序号**，随重新嗅探变化：
    不要把它写进历史关联、烧录路由、权限判断或证据匹配（`SCAN_SEQ_PERSISTENT=False`）。
    `software_version` / `hardware_version` 缺就是 `None`，页面上显示"未报告"，
    **不编占位版本**。
    """

    num: int
    port: str
    available: bool
    available_reason: str
    identity_state: str
    software_version: str | None
    hardware_version: str | None
    last_activity_utc: str | None
    last_activity_note: str
    observation: PortObservation
    #: 页面上的选中状态（选择是**视图状态**，不是设备身份）。
    selected: bool = False

    @property
    def scan_seq(self) -> int:
        return int(self.num)

    def to_json(self) -> dict[str, Any]:
        return {
            "num": int(self.num),
            "scan_seq": int(self.num),
            "scan_seq_persistent": SCAN_SEQ_PERSISTENT,
            "port": self.port,
            "available": bool(self.available),
            "available_reason": self.available_reason,
            "identity_state": self.identity_state,
            "software_version": self.software_version,
            "hardware_version": self.hardware_version,
            "last_activity_utc": self.last_activity_utc,
            "last_activity_note": self.last_activity_note,
            "selected": bool(self.selected),
            "observation": self.observation.to_json(),
        }


def recent_activity(
    records: Iterable[Mapping[str, Any]], port: str
) -> tuple[str | None, str]:
    """从**已记录**的端口活动里挑出这个口最近的一次（没有就如实空）。

    需求 §3 要求卡片显示"端口最近活动" —— 它只能来自**记录**：run 的 manifest 与会话的
    manifest 里写下的那个口、那个时刻。本函数只管"选哪一条"（纯函数，可单独验），
    读文件是调用方的事（服务端 `_port_activity_source`）。

    ⚠️ 只认记录里写着的字段：口要对得上（不按目录名、不按"看起来最近"猜）；
    没有时间戳的那一条**跳过** —— 宁可显示"没有记录"，也不要编一个时刻。
    ⚠️ 2026-10-09 实测发现这个字段**恒为空**：一个永远为 null 的字段比没有字段更糟
    （它看起来像证据）。
    """
    wanted = str(port or "").strip().upper()
    if not wanted:
        return (None, "")
    best_utc = ""
    best_note = ""
    for row in records:
        if str(row.get("port") or "").strip().upper() != wanted:
            continue
        utc = str(row.get("utc") or "").strip()
        if not utc or utc <= best_utc:
            continue
        best_utc, best_note = utc, str(row.get("note") or "").strip()
    return (best_utc or None, best_note)


def card_for(observation: PortObservation, *, num: int, selected: bool = False) -> DeviceCard:
    """观测 ⇒ 卡片（**唯一**的一处换算，页面与服务端读同一份）。"""
    usable, why = availability(observation)
    fields = dict(observation.identity_fields or {})
    return DeviceCard(
        num=int(num),
        port=observation.port,
        available=usable,
        available_reason=why,
        identity_state=observation.identity_state,
        software_version=_text_or_none(fields.get("software_version")),
        hardware_version=_text_or_none(fields.get("hardware_version")),
        last_activity_utc=observation.last_activity_utc,
        last_activity_note=observation.last_activity_note,
        observation=observation,
        selected=bool(selected),
    )


def _text_or_none(value: Any) -> str | None:
    """空串 / None ⇒ `None`（"未报告"），**绝不**编一个占位版本。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# =============================================================== 一次嗅探的结果
@dataclass(frozen=True)
class SniffResult:
    """一轮嗅探的全部产出（页面读它，run 也读它）。"""

    candidates: tuple[str, ...]
    cards: tuple[DeviceCard, ...]
    started_utc: str
    ended_utc: str = ""
    notes: tuple[str, ...] = ()
    #: 声明了但**当前不存在**的口（用户口径 2026-10-10：不展示成卡片）。
    #: 它们既不是设备、也不可能是选中的对象，但“少掉了哪几个”是个事实，不能沉默丢掉：
    #: 结构化的放在这里（页面据此说一句短话），一句完整的话在 `notes` 里。
    skipped: tuple[str, ...] = ()

    @property
    def ports(self) -> tuple[str, ...]:
        return tuple(card.port for card in self.cards)

    def card(self, num: int) -> DeviceCard | None:
        """按**本次显示序号**取卡片（不是设备身份，见 `DeviceCard.num`）。"""
        for card in self.cards:
            if int(card.num) == int(num):
                return card
        return None

    def by_port(self, port: str) -> DeviceCard | None:
        wanted = str(port or "")
        for card in self.cards:
            if card.port == wanted:
                return card
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "scan_seq_persistent": SCAN_SEQ_PERSISTENT,
            "candidates": list(self.candidates),
            "cards": [card.to_json() for card in self.cards],
            "notes": list(self.notes),
            "skipped": list(self.skipped),
        }


# ==================================================== 固件需求与兼容性（需求 §6）
@dataclass(frozen=True)
class FirmwareRequirement:
    """一份固件"要什么样的设备"的**声明**（来自 profile / 目标适配器）。

    这里的每一项都是数据：core 不认"这块板该配哪份固件"，只按声明对表。
    """

    firmware_id: str
    label: str
    #: 这份固件属于哪个项目（与设备自报的 `project` 对表；空串 = 不要求）。
    project: str = ""
    #: **设备侧可能自称的项目名**（来自 profile 的 `identity_hint`）。
    #: ⚠️ 2026-10-09 实测踩到的真问题：裸机固件回的是 `"project":"stm32bm"`
    #: （它自己的线上名字），而本机项目包键是 `baremetal` ⇒ 只比 `project` 会把
    #: 一张**能用的**卡片判成不兼容（Web 因此起不了 run）。设备自称什么已经声明过
    #: （`identity_hint`），判定要查它 —— 不是改固件、也不是改平台键。
    device_projects: tuple[str, ...] = ()
    #: 必须支持的协议（设备自报 `protocols` 里的键；空 = 不要求）。
    required_protocols: tuple[str, ...] = ()
    #: 这几项设备**明确**自报支持就不匹配（"这份固件没有它"）。空 = 不要求。
    #: 用途（2026-10-10，用户口径“所有硬件只需要选串口”）：同一块板上的两份构建
    #: （带 / 不带 Token Log）项目名一模一样，只能靠设备自报的 `protocols.token_log` 分 ——
    #: 带 token 那份 `required_protocols` 里要求它，不带那份把它写在这里。
    #: ⚠️ 只在设备**明确说支持**时才拒：没报 / 说 0 都不拒（说不清的旧固件不该被拦），
    #: 否则“设备自报里根本没这一项”会被读成“它不支持”而把能用的卡片判死。
    absent_protocols: tuple[str, ...] = ()
    #: 身份**未知**时是否允许走首次烧录（由目标适配器声明，不是平台的宽容）。
    first_flash_allowed: bool = False
    #: 首次烧录还要求台架给出**显式烧录器绑定**（需求 §4.3 / §5）。
    requires_flasher_binding: bool = False
    #: 需要烧录（覆盖 Flash）时才要逐次确认；这里只声明"要不要"。
    requires_flash: bool = False
    #: **同一个物理 target 上另一份固件**的自称名（）。
    #: 设备自报的名字落在这一组里 ⇒ 不是"这块板不对"，而是"板上跑着同板的另一份
    #: 固件，这一烧要把它换掉" ⇒ **逐次确认后允许**（每次启动 run 都要显式确认）。
    #: 为什么必须有这一条： 拿"设备自报的项目名"当闸门，而一块板上两份固件
    #: 分属两个项目（`baremetal` / `rtthread`）⇒ 只要板上跑着其中一份，另一份就永远被
    #: 判不兼容，平台再也切不回去（2026-10-11 真机撞到，且已确认是**双向**的）。
    #: ⚠️ 这里的协议检查必须一并跳过：板上跑的是**另一份**固件，它的自报协议当然与
    #: 这一份不同，拿它去比只会把一次合法的切换判死。
    sibling_projects: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "firmware_id": self.firmware_id,
            "label": self.label,
            "project": self.project,
            "device_projects": list(self.device_projects),
            "sibling_projects": list(self.sibling_projects),
            "required_protocols": list(self.required_protocols),
            "absent_protocols": list(self.absent_protocols),
            "first_flash_allowed": bool(self.first_flash_allowed),
            "requires_flasher_binding": bool(self.requires_flasher_binding),
            "requires_flash": bool(self.requires_flash),
        }


@dataclass(frozen=True)
class Compatibility:
    """"这份固件能不能用在这个口上的这个设备"的结论 + 理由。"""

    firmware_id: str
    allowed: bool
    reason: str
    #: 允许，但走的是"身份未知 + 首次烧录"那条路（页面必须显示这一点）。
    first_flash: bool = False
    #: 允许，但板上跑的是**同一个 target 上另一份固件**，这一烧会把它覆盖掉
    #: （；页面与证据都要把这个事实显示出来，不能当成"一切都对得上"）。
    switches_sibling: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "firmware_id": self.firmware_id,
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "first_flash": bool(self.first_flash),
            "switches_sibling": bool(self.switches_sibling),
        }


def compatibility(
    card: DeviceCard,
    requirement: FirmwareRequirement,
    *,
    flasher_bound: bool = True,
) -> Compatibility:
    """设备卡片 × 固件需求 ⇒ 允许/拒绝（**服务端**说了算，页面只展示结论）。

    规则（需求 §4 / §6，全部有正反夹具）：

    - 端口不可用 ⇒ 拒（`不可查询` 那一类：占用 / 不存在 / 打不开）；
    - 这份目标**要求显式烧录器绑定**而本机台架没给 ⇒ 拒（与身份无关：
      “通信口”与“烧录器”是两件事的目标（例如 SWD 探针 + 另一个串口）缺一条就**不确定**，
      而不确定的事不允许用“按枚举顺序猜”去补；需求 §5）；
    - 身份 `conflict` ⇒ 拒（**默认禁止烧录**；不静默降级成 `unknown`）；
    - 身份 `confirmed` ⇒ 项目名 / 协议对不上就拒，全对上就允许；
    - 身份 `unknown` ⇒ 只有声明了“允许首次烧录”才允许，并标 `first_flash=True`。
    """
    firmware_id = requirement.firmware_id
    if not card.available:
        return Compatibility(firmware_id, False, f"这个通信口现在不可用：{card.available_reason}")
    if requirement.requires_flasher_binding and not flasher_bound:
        return Compatibility(
            firmware_id, False,
            "这个目标要求一个**显式**的烧录器绑定，而本机台架没给出这条绑定"
            "（平台不按枚举顺序或“唯一在线的探针”去猜）",
        )
    state = card.identity_state
    if state == IDENTITY_CONFLICT:
        return Compatibility(
            firmware_id, False,
            "设备自报的身份与当前选择不兼容；冲突默认禁止烧录（要恢复设备需另行走显式恢复流程）",
        )
    if state == IDENTITY_UNQUERYABLE:
        return Compatibility(firmware_id, False, "身份不可查询：端口被占用、消失或打开失败")
    if state == IDENTITY_CONFIRMED:
        fields = dict(card.observation.identity_fields or {})
        reported = str(fields.get("project") or "").strip()
        # ⚠️ 设备自称的名字可能是它自己的线上名（`stm32bm`），而本机项目包键是另一个
        #    （`baremetal`）。这两者是否指同一份固件**由声明回答**（profile 的
        #    `identity_hint`）——2026-10-09 实测：不查这条会把一张能用的卡片判成不兼容。
        aliases = tuple(str(name) for name in requirement.device_projects if str(name))
        matched_as_alias = bool(reported) and reported != requirement.project and reported in aliases
        # ：**同一个物理 target 上另一份固件**在板上 ⇒ 允许（覆盖它），
        # 而且**跳过下面的协议检查** —— 板上跑的是另一份固件，它自报的协议当然不同。
        # 逐次确认由调用方保证：每次启动 run 都要显式 `confirm_flash`，且烧完会重新核验身份。
        siblings = tuple(str(name) for name in requirement.sibling_projects if str(name))
        if (requirement.project and reported and reported != requirement.project
                and not matched_as_alias and reported in siblings):
            return Compatibility(
                firmware_id, True,
                f"设备自报的项目是 {reported!r} —— 它是**同一个 target 上的另一份固件**："
                "逐次确认后允许烧录，这一次会把它覆盖掉（写完会重新核验身份）",
                switches_sibling=True,
            )
        if requirement.project and reported and reported != requirement.project and not matched_as_alias:
            return Compatibility(
                firmware_id, False,
                f"设备自报的项目是 {reported!r}，这份固件属于 {requirement.project!r}"
                + (f"（设备侧的名字只有 {list(aliases)}）" if aliases else ""),
            )
        if requirement.project and not reported:
            return Compatibility(
                firmware_id, False,
                f"设备没有自报项目，无法证明它就是 {requirement.project!r} 这一份固件",
            )
        protocols = fields.get("protocols")
        table = protocols if isinstance(protocols, Mapping) else {}
        missing = [name for name in requirement.required_protocols
                   if not _protocol_supported(table.get(name))]
        if missing:
            return Compatibility(
                firmware_id, False,
                f"设备自报的协议里没有这份固件需要的：{missing}",
            )
        # “这份固件**没有**的那几项”：只有设备**明确说支持**时才不匹配（见字段注释）。
        unexpected = [name for name in requirement.absent_protocols
                      if _protocol_supported(table.get(name))]
        if unexpected:
            return Compatibility(
                firmware_id, False,
                f"设备自报支持 {unexpected}，而这份固件不含它（是同一块板上的另一份构建）",
            )
        return Compatibility(
            firmware_id, True,
            (
                f"设备已确认，且项目与协议都对得上（设备自称 {reported!r}"
                f" ＝ 本机项目包 {requirement.project!r}）"
                if matched_as_alias
                else "设备已确认，且项目与协议都对得上"
            ),
        )
    # 到这里只剩 `unknown`（无响应 / 空板 / 旧固件 / 不支持身份协议）。
    if not requirement.first_flash_allowed:
        return Compatibility(
            firmware_id, False,
            "身份未知，而这份固件没有声明允许首次烧录（要在目标声明里明确写出来）",
        )
    return Compatibility(
        firmware_id, True,
        "身份未知：按该目标声明的首次烧录策略允许，烧录后必须重新查询并核验身份",
        first_flash=True,
    )


def _protocol_supported(value: Any) -> bool:
    """`protocols` 里的一项算不算"支持"。

    ⚠️ 只有**明确的正数**才算：`0`（这份构建关掉了它）、`null`（设备没说）、
    缺席都**不算**。把"没说"当"支持"会让 notoken 那份固件被当成能解码（工程理念 §3）。
    """
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value > 0
    try:
        return int(str(value).strip(), 0) > 0
    except ValueError:
        return False


# ============================================ 身份 ⇒ 运行组合（ / D9，§9.1）
#: 唯一匹配：这次实测身份**恰好**对应一条固件 profile ⇒ 自动采用。
MATCH_MATCHED = "matched"
#: 身份未知（空板 / 旧固件 / 无响应），但**恰好一条**声明了"允许首次烧录"：
#: 那不是"设备当前跑的那一份"，而是**待烧录方案**（需求 §9.1 第三条）。
MATCH_FIRST_FLASH = "first_flash"
#: 零匹配：没有任何一条固件对应这次实测身份（要说清缺哪条声明）。
MATCH_NONE = "none"
#: 多匹配：两条以上都命中 ⇒ **有歧义，不许挑一个**。
MATCH_AMBIGUOUS = "ambiguous"

MATCH_STATES: tuple[str, ...] = (
    MATCH_MATCHED,
    MATCH_FIRST_FLASH,
    MATCH_NONE,
    MATCH_AMBIGUOUS,
)

#: 可以直接拿来装配运行组合的那两个状态（其余两个必须**阻塞并说明原因**）。
MATCH_USABLE_STATES: tuple[str, ...] = (MATCH_MATCHED, MATCH_FIRST_FLASH)


@dataclass(frozen=True)
class FirmwareMatch:
    """这次实测身份 ⇒ 哪一份固件（**服务端唯一判定**，需求 §9.1）。

    为什么要单独一个结论、而不是页面拿 `compatible_firmwares()` 自己挑：`compatible_firmwares`
    回答的是"这一份**能不能用**"（烧录门那一族，允许"覆盖同板另一份固件"这种**开发**路径），
    而"设备**此刻跑的是**哪一份"是另一个问题 —— 同一块板上跑着 RT-Thread 时，
    裸机那份也 `allowed=True`（`switches_sibling`，一烧就把它换掉）。观察/装配运行组合
    必须用**真命中**的那一份，否则会拿错的字典去解这一路（）。

    规则（每一条都有正反夹具）：

    - `matched`：身份已确认，且**恰好一条**真命中（不是首次烧录、也不是覆盖兄弟固件）；
    - `first_flash`：身份未知，**恰好一条**声明了首次烧录 ⇒ 待烧录方案；
    - `ambiguous`：真命中的有两条以上（或身份未知而多条声明首次烧录）⇒ 有歧义；
    - `none`：一条都没有（含身份冲突 / 口不可查 / 没有任何声明）⇒ 阻塞。

    ⚠️ 它**不认识任何具体设备**：判据全在 `compatibility()` 的声明里；这里只做"数一数"。
    """

    state: str
    #: `matched` / `first_flash` 时是那一份；其余状态是空串（不许挑一个）。
    firmware_id: str = ""
    #: 有歧义时的候选（按传入顺序；其余状态为空）。
    candidates: tuple[str, ...] = ()
    #: 给人看的一句结论（含来源理由）。
    reason: str = ""
    #: 逐条不成立的理由（`firmware_id` → 为什么），供详情/排查用。
    rejections: tuple[tuple[str, str], ...] = ()

    @property
    def usable(self) -> bool:
        """这份结论能不能直接装配运行组合（`matched` / `first_flash`）。"""
        return self.state in MATCH_USABLE_STATES

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "firmware_id": self.firmware_id,
            "candidates": list(self.candidates),
            "reason": self.reason,
            "usable": self.usable,
            "rejections": [{"firmware_id": key, "reason": why} for key, why in self.rejections],
        }


def _identity_note(card: DeviceCard) -> str:
    """"这次实测到的身份是什么"的一句话摘要（只写实测字段，不认识具体设备）。"""
    fields = dict((card.observation.identity_fields or {}) if card.observation else {})
    if not fields:
        return "设备没有自报任何身份字段"
    keys = ("project", "software_version", "build_id")
    shown = [f"{name}={fields[name]!r}" for name in keys if name in fields]
    rest = len(fields) - len(shown)
    text = "、".join(shown) if shown else "（只有其余自报字段）"
    return f"设备自称 {text}" + (f"，另有 {rest} 个字段" if rest > 0 else "")


def match_firmware(
    card: DeviceCard,
    requirements: Iterable[FirmwareRequirement],
    *,
    flasher_bound: bool = True,
) -> FirmwareMatch:
    """这次实测身份 × 目录里的固件声明 ⇒ **唯一的运行组合**（ / D9）。

    ⚠️ 顺序固定、结论唯一：**不做**"按注册顺序挑第一个"、"沿用上次选择"或
    "用 COM 号猜"。数不出唯一的那一份就如实说 `none` / `ambiguous`。
    """
    rows = [
        (req, compatibility(card, req, flasher_bound=flasher_bound))
        for req in requirements
    ]
    named = [str(getattr(req, "firmware_id", "") or "") for req, _ in rows]
    # 真命中：允许，且**不是**"首次烧录方案"、也**不是**"覆盖同板另一份固件"。
    # 后两种都属于"设备此刻跑的不是它"，只能是**烧录**路径的候选，不能拿来装配观察组合。
    exact = [
        key
        for key, (_, verdict) in zip(named, rows)
        if verdict.allowed and not verdict.first_flash and not verdict.switches_sibling
    ]
    if card.identity_state == IDENTITY_CONFIRMED:
        if len(exact) == 1:
            return FirmwareMatch(
                MATCH_MATCHED,
                firmware_id=exact[0],
                reason=f"身份已确认，唯一匹配 {exact[0]}（{_identity_note(card)}）",
            )
        if len(exact) > 1:
            return FirmwareMatch(
                MATCH_AMBIGUOUS,
                candidates=tuple(exact),
                reason=(
                    "多匹配：这次实测身份同时命中 "
                    f"{'、'.join(exact)}（{_identity_note(card)}）—— 平台不替使用者在它们之间挑一个"
                ),
            )
        return FirmwareMatch(
            MATCH_NONE,
            reason=(
                f"零匹配：没有任何固件对应这次实测身份（{_identity_note(card)}）"
            ),
            rejections=tuple(
                (key, verdict.reason) for key, (_, verdict) in zip(named, rows)
            ),
        )
    if card.identity_state == IDENTITY_UNQUERYABLE:
        return FirmwareMatch(
            MATCH_NONE,
            reason=f"这个通信口当前无法查询身份：{card.available_reason}",
            rejections=tuple(
                (key, verdict.reason) for key, (_, verdict) in zip(named, rows)
            ),
        )
    if card.identity_state == IDENTITY_CONFLICT:
        return FirmwareMatch(
            MATCH_NONE,
            reason="设备自报的身份与目录里的声明冲突：冲突状态默认不做任何装配",
            rejections=tuple(
                (key, verdict.reason) for key, (_, verdict) in zip(named, rows)
            ),
        )
    # 到这里只剩 `unknown`（无响应 / 空板 / 旧固件）⇒ 首次烧录那一族。
    first = [
        key
        for key, (_, verdict) in zip(named, rows)
        if verdict.allowed and verdict.first_flash
    ]
    if len(first) == 1:
        return FirmwareMatch(
            MATCH_FIRST_FLASH,
            firmware_id=first[0],
            reason=(
                f"身份未知（{card.available_reason or '设备没有回应身份查询'}）："
                f"按声明只有 {first[0]} 允许首次烧录 —— 它是**待烧录方案**，"
                "不是“设备当前跑的那一份”"
            ),
        )
    if len(first) > 1:
        return FirmwareMatch(
            MATCH_AMBIGUOUS,
            candidates=tuple(first),
            reason=(
                "多匹配：身份未知，而有多份固件声明允许首次烧录（"
                f"{'、'.join(first)}）—— 要烧哪一份得由使用者明确选，平台不替他在这些方案里挑"
            ),
        )
    return FirmwareMatch(
        MATCH_NONE,
        reason=(
            "零匹配：身份未知，而目录里没有任何固件声明允许首次烧录"
            "（要在目标声明里明确写出来）"
        ),
        rejections=tuple((key, verdict.reason) for key, (_, verdict) in zip(named, rows)),
    )


# ============================================== 运行目标：冻结形状 + 旧 run 降级
#: run manifest 里 `device` 块的形状版本。
RUN_TARGET_SCHEMA = 1


@dataclass(frozen=True)
class RunTarget:
    """这一趟 run 的设备目标 = 本次通信口 + 实测身份 + 已确认的目标适配器。

    ⚠️ 顺序很重要：**通信口**是运行时选出来的，不是从静态 "板 A/板 B" 推出来的。
    `device_key` 只剩"本机台架里的绑定键"这一个含义（用来找工程根与烧录器），
    它**不再决定**通信口 —— 通信口由本次嗅探/复核给出。
    """

    port: str
    target_id: str
    firmware_id: str
    device_key: str
    #: 本次嗅探的显示序号（**非持久**，见 `SCAN_SEQ_PERSISTENT`）。
    scan_seq: int | None = None
    #: 烧录**前**的身份状态与原始自报字段。
    identity_state: str = IDENTITY_UNKNOWN
    identity_before: Mapping[str, Any] = field(default_factory=dict)
    #: 本机台架绑定的来源与版本（"这一趟用的是哪份台架事实"）。
    bench_binding: Mapping[str, Any] = field(default_factory=dict)
    probe: Mapping[str, Any] = field(default_factory=dict)
    #: 目标适配器 / 烧录器标识（缺了就是没绑定 —— 如实留空，不编）。
    adapter_id: str = ""
    flasher_id: str = ""
    requires_flash: bool = False
    first_flash: bool = False
    #: 这一趟要**覆盖同一个 target 上的另一份固件**（）：
    #: 记进证据，事后能看出“烧之前板上是另一份固件”。
    switches_sibling: bool = False
    #: 这一趟**必须有**的协议能力（来自固件清单的声明）。烧录后复核拿它对表。
    required_protocols: tuple[str, ...] = ()

    def to_evidence(self) -> dict[str, Any]:
        """写进 run manifest 的 `device` 块（**只增**：老工具读不到它不会坏）。"""
        return {
            "schema": RUN_TARGET_SCHEMA,
            "port": self.port,
            "target_id": self.target_id,
            "firmware_id": self.firmware_id,
            "device_key": self.device_key,
            "scan_seq": None if self.scan_seq is None else int(self.scan_seq),
            "scan_seq_persistent": SCAN_SEQ_PERSISTENT,
            "identity_before": {
                "state": self.identity_state,
                "fields": dict(self.identity_before or {}),
            },
            "identity_after": None,
            "bench_binding": dict(self.bench_binding or {}),
            "probe": dict(self.probe or {}),
            "adapter_id": self.adapter_id,
            "flasher_id": self.flasher_id,
            "requires_flash": bool(self.requires_flash),
            "first_flash": bool(self.first_flash),
            "switches_sibling": bool(self.switches_sibling),
            "required_protocols": list(self.required_protocols),
        }


@dataclass(frozen=True)
class RecordedTarget:
    """从一个**已经落盘**的 run manifest 读出来的目标（回放用）。

    ⚠️ 旧 run（ 之前）里没有 `device` 块，只有 `run.target`（设备键）
    与 `run.port`。回放**按它自己的 manifest**，不拿当前嗅探结果或当前台架配置改写 ——
    所以这一层只做"读"，而且把每个字段**来自哪个键**记下来（`source_keys`），
    这样"这条结论是从哪儿读到的"永远可审计（工程理念 §1）。
    """

    port: str
    target_id: str
    firmware_id: str
    device_key: str
    scan_seq: int | None
    identity_before: Mapping[str, Any] | None
    identity_after: Mapping[str, Any] | None
    #: True = 这份 manifest 里没有 `device` 块，是按旧形状（`run.target` / `run.port`）回放的。
    legacy: bool
    source_keys: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "target_id": self.target_id,
            "firmware_id": self.firmware_id,
            "device_key": self.device_key,
            "scan_seq": self.scan_seq,
            "scan_seq_persistent": SCAN_SEQ_PERSISTENT,
            "identity_before": dict(self.identity_before) if self.identity_before else None,
            "identity_after": dict(self.identity_after) if self.identity_after else None,
            "legacy": bool(self.legacy),
            "source_keys": list(self.source_keys),
        }


def recorded_target(manifest: Mapping[str, Any] | None) -> RecordedTarget:
    """读一份 run manifest 里的设备目标（**新键优先，旧键兼容**）。

    ⚠️ "读不出来"也要**如实**：一份既没有 `device` 块、也没有 `run.port` 的 manifest
    得到空串而不是猜一个口。历史证据不许被当前事实污染（需求 §7）。
    """
    document = dict(manifest or {})
    block = document.get("device")
    run = document.get("run") if isinstance(document.get("run"), Mapping) else {}
    if isinstance(block, Mapping):
        before = block.get("identity_before")
        after = block.get("identity_after")
        seq = block.get("scan_seq")
        return RecordedTarget(
            port=str(block.get("port") or ""),
            target_id=str(block.get("target_id") or ""),
            firmware_id=str(block.get("firmware_id") or ""),
            device_key=str(block.get("device_key") or ""),
            scan_seq=None if seq is None else int(seq),
            identity_before=dict(before) if isinstance(before, Mapping) else None,
            identity_after=dict(after) if isinstance(after, Mapping) else None,
            legacy=False,
            source_keys=("device.port", "device.target_id", "device.firmware_id", "device.device_key"),
        )
    return RecordedTarget(
        port=str(run.get("port") or ""),
        target_id="",
        firmware_id="",
        device_key=str(run.get("target") or ""),
        scan_seq=None,
        identity_before=None,
        identity_after=None,
        legacy=True,
        source_keys=("run.port", "run.target"),
    )


# ================================================ 烧录后的身份核验（D4 的纯判断）
@dataclass(frozen=True)
class IdentityCheck:
    """烧录后复核的结论（`matched=False` 时 `mismatches` 必须逐条说清哪里不符）。"""

    matched: bool
    reason: str
    mismatches: tuple[str, ...] = ()
    #: 这一趟**真的比过**的字段名（"没得核对"不等于"核对通过"）。
    compared: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "matched": bool(self.matched),
            "reason": self.reason,
            "mismatches": list(self.mismatches),
            "compared": list(self.compared),
        }


def verify_identity(
    after: Mapping[str, Any] | None,
    *,
    expect_project: str = "",
    expect_build_id: int | None = None,
    required_protocols: tuple[str, ...] = (),
    project_aliases: tuple[str, ...] = (),
) -> IdentityCheck:
    """烧录**之后**重新查询到的身份 ⇒ 与预期对表（需求 §4.5 / §7）。

    ⚠️ `expect_build_id is None`（这一趟没有构建号可以做预期）时，构建号**不参与**
    比较，也**不**出现在 `compared` 里 —— 一个"永远为真"的字段比没有字段更糟
    （它看起来像证据，见仓库里那条老教训）。
    ⚠️ `project_aliases` 是**设备可能自称的名字**（声明在 `identity_hint`）：裸机固件回的是
    `"project":"stm32bm"`，而本机项目包键是 `baremetal` —— 两者指同一份固件由声明回答。
    """
    fields = after if isinstance(after, Mapping) else None
    if fields is None:
        return IdentityCheck(False, "烧录后没有取到身份回答（无法确认板上跑的是什么）")
    normalized = identity_mod.normalize(dict(fields))
    aliases = tuple(str(name) for name in project_aliases if str(name))
    compared: list[str] = []
    mismatches: list[str] = []
    if expect_project:
        compared.append("project")
        reported = str(normalized.get("project") or "").strip()
        if reported != expect_project and reported not in aliases:
            shown = repr(reported) if reported else "（空）"
            mismatches.append(
                f"project：预期 {expect_project!r}"
                + (f"或设备侧名字 {list(aliases)}" if aliases else "")
                + f"，设备自报 {shown}"
            )
    if expect_build_id is not None:
        compared.append("build_id")
        reported_id = identity_mod.build_id32(normalized)
        if reported_id != int(expect_build_id):
            want = f"0x{int(expect_build_id) & 0xFFFFFFFF:08x}"
            got = "（没有）" if reported_id is None else f"0x{reported_id:08x}"
            mismatches.append(f"build_id：预期 {want}，设备自报 {got}")
    protocols = normalized.get("protocols")
    table = protocols if isinstance(protocols, Mapping) else {}
    for name in required_protocols:
        compared.append(f"protocols.{name}")
        if not _protocol_supported(table.get(name)):
            mismatches.append(f"protocols.{name}：设备没有自报支持")
    if not compared:
        return IdentityCheck(
            False,
            "没有可以做预期的字段（项目 / 构建号 / 协议都没给）⇒ 身份核验不成立，不当作通过",
        )
    if mismatches:
        return IdentityCheck(False, "烧录后的身份与预期不符", tuple(mismatches), tuple(compared))
    return IdentityCheck(True, "烧录后的身份与预期一致", (), tuple(compared))


__all__ = [
    "SCHEMA",
    "RUN_TARGET_SCHEMA",
    "SCAN_SEQ_PERSISTENT",
    "IDENTITY_CONFIRMED",
    "IDENTITY_UNKNOWN",
    "IDENTITY_CONFLICT",
    "IDENTITY_UNQUERYABLE",
    "IDENTITY_STATES",
    "MAX_ATTEMPTS_PER_PROBE",
    "PROBE_OK",
    "PROBE_NO_RESPONSE",
    "PROBE_MALFORMED",
    "PROBE_UNSUPPORTED",
    "PROBE_PORT_BUSY",
    "PROBE_PORT_ABSENT",
    "PROBE_OPEN_FAILED",
    "PROBE_SKIPPED",
    "PROBE_OUTCOMES",
    "IdentityProbe",
    "PortObservation",
    "DeviceCard",
    "SniffResult",
    "FirmwareRequirement",
    "Compatibility",
    "MATCH_MATCHED",
    "MATCH_FIRST_FLASH",
    "MATCH_NONE",
    "MATCH_AMBIGUOUS",
    "MATCH_STATES",
    "MATCH_USABLE_STATES",
    "FirmwareMatch",
    "match_firmware",
    "RunTarget",
    "RecordedTarget",
    "IdentityCheck",
    "identity_state",
    "state_rank",
    "outcome_for_reply",
    "availability",
    "card_for",
    "compatibility",
    "recorded_target",
    "verify_identity",
]

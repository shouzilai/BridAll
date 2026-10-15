"""实时观察会话：串口所有权状态机 + 只读会话证据（Phase 7.1 切片 1/2）。

- **切片 1（已完成）**：谁在什么时候持有串口。打开 / 断开 / 手工重连，以及
  「被自动验证或别的进程占用」的拒绝。
- **切片 2（本文件）**：把连接期间读到的 RX 变成**可核对的证据** ——
  `observe-sessions/<session-id>/{manifest.json,raw.bin,io_events.jsonl}`（见 `observe_store.py`）
  并把每一行同步推到页面上的「原始串口」窗口。

仍然不做的事（每一步都有能力声明跟着走）：

- 打开端口**不写** `dtr` / `rts`：赋值会经过板上的自动复位电路把板子复位（I-022 /
  ），而"连接"不允许是一个会改变被测设备的动作（需求 §3.1）；
- **不发任何字节**（命令框与终端直输是切片 3/4）⇒ `capabilities.send = False`；
- **不解码**（身份握手与字典匹配是切片 5）⇒ `capabilities.decode = False`，
  而且那之前**不生成** `decoded.log` / `app_events.jsonl`：空的解码日志会被读成"设备什么都没说"。

⚠️ 一个"永远为 0 / 永远为 False"的字段比没有字段更糟 —— 它看起来像证据。所以这份快照里
带 `capabilities`：页面按它决定"这一版到底有没有这个能力"，宿主测试也据它断言
"能力声明与已实现的切片一致"。

这个模块不认识任何业务命令（不解析 `STATUS` / `DUMP` / `CLEAR YES`），也不做命令白名单：
平台只认识串口、字节、连接状态与（后续切片的）身份握手。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping

try:  # imported as a package (``webconsole.observe``)
    from . import identity, io_timeline
    from .archive import ArchiveIndex
    from .console_input import (
        BACKSPACE_CHOICES,
        LINE_ENDING_CHOICES,
        MODIFIER_KEYS,
        TERMINAL_SOURCE,
        build_keys_payload,
        build_payload,
        bytes_hex,
        normalize_backspace,
        normalize_line_ending,
    )
    from .core_primitives import (
        idle_partial, partial_idle_s, split_complete_lines, utc_now, visible_line_remainder,
    )
    from .core_primitives import READ_TIMEOUT_S
    from . import bench_config
    from .device_registry import (
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_UNKNOWN,
        DeviceRegistry,
        unknown_access,
    )
    from .live_decode import LiveDecoder
    from .observe_store import (
        IMPORT_DIR_NAME,
        SESSION_DIR_NAME,
        ObserveSessionStore,
        new_session_id,
    )
    from .platform_plugins import NO_PLUGINS, PlatformPlugins
    from . import platform_info
    from .platform_info import describe as describe_platform
    from .target_profile import session_capabilities
except ImportError:  # executed as a plain script
    import identity  # type: ignore[no-redef]
    import io_timeline  # type: ignore[no-redef]
    from archive import ArchiveIndex  # type: ignore[no-redef]
    from console_input import (  # type: ignore[no-redef]
        BACKSPACE_CHOICES,
        LINE_ENDING_CHOICES,
        MODIFIER_KEYS,
        TERMINAL_SOURCE,
        build_keys_payload,
        build_payload,
        bytes_hex,
        normalize_backspace,
        normalize_line_ending,
    )
    from core_primitives import (  # type: ignore[no-redef]
        idle_partial, partial_idle_s, split_complete_lines, utc_now, visible_line_remainder,
    )
    from core_primitives import READ_TIMEOUT_S  # type: ignore[no-redef]
    import bench_config  # type: ignore[no-redef]
    from device_registry import (  # type: ignore[no-redef]
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_UNKNOWN,
        DeviceRegistry,
        unknown_access,
    )
    from live_decode import LiveDecoder  # type: ignore[no-redef]
    from observe_store import (  # type: ignore[no-redef]
        IMPORT_DIR_NAME,
        SESSION_DIR_NAME,
        ObserveSessionStore,
        new_session_id,
    )
    from platform_plugins import NO_PLUGINS, PlatformPlugins  # type: ignore[no-redef]
    import platform_info  # type: ignore[no-redef]
    from platform_info import describe as describe_platform  # type: ignore[no-redef]
    from target_profile import session_capabilities  # type: ignore[no-redef]

#  / L2b：实时观察里的**受控测试**（批次 + 与写口之间的那一层）。
# 两者都是纯模型（不碰串口），这里只把"会话那条唯一写口"与"行流"接给它们。
try:
    from .console_input import LIVE_TEST_SOURCE, build_text_payload, render_text
    from .live_test_batch import TERMINAL, LiveTestBatch, LiveTestBatchError
    from .live_test_link import LiveTestLink, LiveTestLinkError, evidence_block
    from .step_lifecycle import StageEvidence
except ImportError:  # executed as a plain script
    from console_input import (  # type: ignore[no-redef]
        LIVE_TEST_SOURCE, build_text_payload, render_text,
    )
    from live_test_batch import TERMINAL, LiveTestBatch, LiveTestBatchError  # type: ignore[no-redef]
    from live_test_link import (  # type: ignore[no-redef]
        LiveTestLink, LiveTestLinkError, evidence_block,
    )
    from live_test_link import LiveTestLink, LiveTestLinkError  # type: ignore[no-redef]
    from step_lifecycle import StageEvidence  # type: ignore[no-redef]

# 会话目录的默认位置：`observe-sessions//`（与 `runs//` 并列，同样 gitignore）；
# 导入的诊断包在 `observe-imports//`（同样 gitignore，但**不**与本机采集混在一起）。
LOGGER_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SESSIONS_DIR = LOGGER_DIR / SESSION_DIR_NAME

OBSERVE_BAUD = 115200

#: 实时观察里「多久重算一次指标序列」（秒）。设备打一条 `[I][metric]` 的间隔是 2～30 s，
#: 所以这个节流实际上等于"每条新度量行算一次"；它挡的是"一批行里算很多次"。
LIVE_METRICS_INTERVAL_S = 1.0
#: 累积到这么多条新事件时，就算没到上面的时间也重算一次（密集事件时不让图落后太多）。
LIVE_METRICS_EVENT_STEP = 25
#: 一帧增量最多带多少个点（每键，）。正常情况下一次重算只多出“一条度量行的几个
#: 字段”（十几个点），到不了这里；这条挡的是“节流期间攒了很久”或“事件突然涌入”之后一帧
#: 变胖 —— 超出的部分**丢掉并报数**（`dropped_points`），不假装没发生。
LIVE_METRICS_DELTA_MAX_POINTS = 400


def metrics_delta(
    payload: dict[str, Any], sent: dict[str, int]
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """把“到此刻为止”的序列切成“上一次之后新增的那些点”（）。

    为什么必须切：`app_events` 是**每条度量行一帧**，而每帧带整份序列 ⇒ 一帧的字节数随
    会话线性增长 ⇒ 整场会话是 O(n²) 字节（实测：重放时单帧 6.8 MB、一次刷新几百 MB，
    页面被拖成逐行慢放，看起来像“没连接却一直在输出”）。切完之后每帧只带新点。

    只切 `by_key[*].points`：其余字段（`schema` / `missing` / `restarts` / `per_kind_events` /
    `sample_count` / `span_s`）本来就是**到此刻为止**的小字段，留着让页面能继续核对
    “为什么少了一个点”。页面把这些小字段直接采用（比上一份新），只把点并进自己那份。

    返回 `(增量或 None, 新的游标)`：一个新增点都没有 ⇒ `None`（宁可这一帧不带指标，也不带
    一份空壳假装有更新）。游标**对所有键**都给，包括这一次没带点的：否则某个键的点数一旦
    回退（重算的输入被截过），它的游标就永远卡在旧值上，后面的新点再也发不出去。
    """
    cursors: dict[str, int] = {}
    by_key: dict[str, dict[str, Any]] = {}
    added = 0
    dropped = 0
    for key, row in (payload.get("by_key") or {}).items():
        points = list(row.get("points") or ())
        already = min(int(sent.get(key, 0)), len(points))
        fresh = points[already:]
        if len(fresh) > LIVE_METRICS_DELTA_MAX_POINTS:
            dropped += len(fresh) - LIVE_METRICS_DELTA_MAX_POINTS
            fresh = fresh[-LIVE_METRICS_DELTA_MAX_POINTS:]
        cursors[key] = already + len(fresh)
        if not fresh:
            continue
        values = [
            point["value"] for point in fresh
            if isinstance(point.get("value"), (int, float))
        ]
        by_key[key] = {
            **row,
            "points": fresh,
            "count": len(fresh),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
        added += len(fresh)
    if not added:
        return None, cursors
    return (
        {
            **payload,
            "by_key": by_key,
            # `point_count` 说的是**这一帧带了几个点**（增量）；总数另外给一个字段，
            # 页面把增量并进自己那份之后自己算总数。
            "point_count": added,
            "point_count_total": payload.get("point_count"),
            "incremental": True,
            "dropped_points": dropped,
        },
        cursors,
    )
# 读超时：与 run 的读循环**共用一份**（`core_primitives.READ_TIMEOUT_S`，在下面的 import 里转口）——
# 它决定"安静的尾巴"晚多久才出现，两条路不能各写一个值。
# 断开时等读取线程退出的上限：读的 timeout 是 0.1 s，所以正常远小于它。
READER_JOIN_S = 0.6

OBSERVE_IDLE = "idle"
OBSERVE_CONNECTING = "connecting"
OBSERVE_CONNECTED = "connected"
OBSERVE_DISCONNECTING = "disconnecting"
OBSERVE_RECONNECTING = "reconnecting"
OBSERVE_PORT_MISSING = "port_missing"
OBSERVE_OWNED_ELSEWHERE = "owned_elsewhere"
OBSERVE_OPEN_FAILED = "open_failed"

# 「设备无身份响应」是**连接之后**的一个身份状态（切片 5），
# 不是连接状态机里的一格：连接成功但设备不回 `@PWINFO`，串口本身仍然是好的。
IDENTITY_UNKNOWN = "unknown"
IDENTITY_QUERYING = "querying"
IDENTITY_OK = "ok"
IDENTITY_TIMEOUT = "timeout"
IDENTITY_UNSUPPORTED = "unsupported"

# 身份来源（需求 §6.2：设备自报与主机横幅回退必须**分开标注**）。
IDENTITY_SOURCE_NONE = "none"
IDENTITY_SOURCE_DEVICE = "device"       # `@PWINFO {...}`（设备自报）
IDENTITY_SOURCE_BANNER = "banner"       # `BOOT build=0x…`（旧设备的回退通道）

# 写给人看的身份状态（用于拼错误原因，不给界面用 —— 界面有它自己的文案表）。
IDENTITY_TEXT_HINT = {
    IDENTITY_UNKNOWN: "还没查",
    IDENTITY_QUERYING: "查询中",
    IDENTITY_OK: "已回应",
    IDENTITY_TIMEOUT: "没有回应",
    IDENTITY_UNSUPPORTED: "设备不支持",
}

# 自动身份查询：连接/重连后立即发一次，未响应每 3 秒重试，最多三次。
IDENTITY_WAIT_S = 3.0
IDENTITY_MAX_ATTEMPTS = 3

STATE_LABELS = {
    OBSERVE_IDLE: "未连接",
    OBSERVE_CONNECTING: "连接中",
    OBSERVE_CONNECTED: "已连接",
    OBSERVE_DISCONNECTING: "断开中",
    OBSERVE_RECONNECTING: "重连中",
    OBSERVE_PORT_MISSING: "串口消失",
    OBSERVE_OWNED_ELSEWHERE: "被其他模式占用",
    OBSERVE_OPEN_FAILED: "打开失败",
}

# 这些状态表示"我方（正在）持有串口"，此时自动验证必须被拒绝。
HOLDING_STATES = frozenset(
    {
        OBSERVE_CONNECTING,
        OBSERVE_CONNECTED,
        OBSERVE_DISCONNECTING,
        OBSERVE_RECONNECTING,
    }
)


class ObserveError(Exception):
    """A refused control request (HTTP ``status`` is used by the service)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def unwired_port_factory(port_name: str, baud: int) -> Any:
    """没装配串口实现时的默认值：**直接报没装配**，不默默降级。

     / R2b：打开串口的实现（pyserial，只设端口/波特率/超时，不写 dtr/rts）
    搬到了目标层（`devices.open_read_only`，适配器 `serial.read-only-open`），
    由装配处经 `PlatformPlugins.port_factory` 交进来。“连不上”必须是**一个状态**
    （`open_failed` + 原因），而不是一个看似能跑但什么都不做的假串口。
    """
    raise RuntimeError(
        "没有装配串口实现（组合入口没提供 port_factory）：不能打开 "
        f"{port_name}，也不知道该用什么方式打开"
    )


#: 基础串口参数（需求 §6.10）：不设就是这一份。
#: ⚠️ `line_ending` **不在这里** —— 它是“行约定”（跟着设备/项目/请求走），
#: 与打开口的电气参数不是一件事。
SERIAL_DEFAULTS: dict[str, Any] = {"baud": OBSERVE_BAUD, "bytesize": 8, "parity": "N", "stopbits": 1}
#: 允许的设置项与合法值（白名单：不认识的名字**当场拒**，不静默忽略）。
SERIAL_CHOICES: dict[str, tuple[Any, ...]] = {
    "baud": (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600),
    "bytesize": (5, 6, 7, 8),
    "parity": ("N", "E", "O", "M", "S"),
    "stopbits": (1, 1.5, 2),
}


def sanitize_serial(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """把请求里的串口参数洗成**只认识那几个**的一份（不认识 / 越界当场拒）。

    需求 §6.10 说的是“设置…后可进行基础收发”，所以这里只收**基础**那四项：
    越界或拼错的参数要报出来，而不是默默用默认值（那会让使用者以为设上了）。
    """
    rows: dict[str, Any] = {}
    for raw_name, raw in dict(values or {}).items():
        name = str(raw_name)
        if name not in SERIAL_CHOICES:
            raise ObserveError(
                f"不认识的串口参数 {name!r}：可用的是 {list(SERIAL_CHOICES)}"
            )
        if raw in (None, ""):
            continue
        if name == "parity":
            value: Any = str(raw).strip().upper()
        elif name == "stopbits":
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = str(raw).strip()
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = str(raw).strip()
        if value not in SERIAL_CHOICES[name]:
            raise ObserveError(
                f"串口参数 {name}={raw!r} 不在可用值里：{list(SERIAL_CHOICES[name])}"
            )
        rows[name] = value
    return rows


def apply_serial_settings(port: Any, settings: Mapping[str, Any]) -> tuple[dict, dict]:
    """把参数设到**已经造好、还没打开**的口上。返回（应用上的, 拒绍的）。

    ⚠️ 不 import pyserial：`baudrate` / `bytesize` / `parity` / `stopbits` 是通用属性名，
    值也用通用形状（比特数 / `N E O` / 1、1.5、2）。替身端口没有某个属性 ⇒ 记进“拒绍”
    并如实说出来，**不报错**（那是替身的事实，不是平台的失败）。
    """
    attribute = {
        "baud": "baudrate",
        "bytesize": "bytesize",
        "parity": "parity",
        "stopbits": "stopbits",
    }
    applied: dict[str, Any] = {}
    refused: dict[str, str] = {}
    for name in ("baud", "bytesize", "parity", "stopbits"):
        if name not in settings:
            continue
        target = attribute[name]
        try:
            if not hasattr(port, target):
                refused[name] = f"这个串口实现没有 {target}（用默认值）"
                continue
            setattr(port, target, settings[name])
            applied[name] = settings[name]
        except Exception as error:  # noqa: BLE001 - 设不上就是设不上，不把整次连接弄挂
            refused[name] = f"{target}: {type(error).__name__}: {error}"
    return applied, refused


def default_store_factory(
    sessions_dir: Path,
    session_id: str,
    *,
    device: dict[str, Any],
    port: str,
    baud: int,
    capabilities: dict[str, bool],
    platform: dict[str, Any] | None = None,
) -> ObserveSessionStore:
    return ObserveSessionStore(
        sessions_dir,
        session_id,
        device=device,
        port=port,
        baud=baud,
        capabilities=capabilities,
        platform=platform,
    )


def _parse_utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _elapsed_ms(stamp: str | None) -> int | None:
    started = _parse_utc(stamp)
    if started is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


def _duration_text(stamp: str | None) -> str:
    ms = _elapsed_ms(stamp)
    if ms is None:
        return ""
    seconds = ms // 1000
    if seconds < 60:
        return f"（已连接 {seconds} s）"
    return f"（已连接 {seconds // 60} 分 {seconds % 60} 秒）"


class ObserveSession:
    """一次只连接一个设备；状态机是这一层的全部内容。

    两把锁，各有明确职责（不要合并）：

    - ``_lock``：只保护字段。``snapshot()`` 只拿它，所以页面轮询不会被 ``open()`` 挡住
      —— 否则"连接中"这个状态永远不会被谁看见。
    - ``_op_lock``：串行化 connect / disconnect / reconnect 这三个动作（含真正的开与关）。
    """

    def capabilities(self) -> dict[str, bool]:
        """本会话对外声明的四项能力：`send` / `identity` / `decode` / `evidence`。

        ⚠️ 它不是写在这里的常量，而是从**目标的能力声明**投影出来的
        （`target_profile.session_capabilities`，映射表 `SESSION_CAPABILITY_MAP`）：
        平台里只有一处能力声明，改那一处，页面文案、会话快照与会话 manifest 一起变
        （ / R2a；过去这里与 profile 各写一份 ⇒ ）。

        还没连过任何设备时，用**第一条设备登记**的能力（参考登记里两块板一致，
        替身登记只有一条）；登记表为空、或目标没声明能力 ⇒ 四项都是 False
        —— "不知道" 不冒充 "支持"。
        """
        return session_capabilities(self._capability_table())

    def _capability_table(self) -> Any:
        """会话当前参照的那份能力声明（目标登记的 `capabilities` 字段）。"""
        spec = self._device or self._target
        if spec is None:
            specs = self.registry.specs()
            spec = specs[0] if specs else None
        return getattr(spec, "capabilities", None)

    def _platform_facts(self) -> dict[str, Any]:
        """平台自述（R2d）：进会话 manifest，诊断包因此自带"这是哪套组合"。

        ⚠️ 只说**装配处真的交进来**的东西：没装插件时它是一份诚实的"什么都没有"
        （label 为空、adapters 为空、capabilities 为 null），
        而不是抄一份看起来很像的默认值。
        """
        spec = self._device or self._target
        table = getattr(spec, "capabilities", None)
        return describe_platform(
            plugins=self.plugins,
            targets=(
                platform_info.TargetFacts(
                    target_id=str(getattr(spec, "key", "") or ""),
                    device_key=str(getattr(spec, "key", "") or ""),
                    platform=str(getattr(self.plugins, "platform", "") or ""),
                    project=str(getattr(self.plugins, "project", "") or ""),
                    pipeline_allowed=bool(getattr(spec, "pipeline_allowed", False)),
                    capability_table=table,
                ),
            )
            if spec is not None
            else (),
            session_capabilities=session_capabilities(table),
            bench=bench_config.block(),
        )

    def __init__(
        self,
        *,
        registry: DeviceRegistry | None = None,
        port_factory: Callable[[str, int], Any] | None = None,
        probe: Callable[[str], str] | None = None,
        active_run: Callable[[], dict[str, Any] | None] | None = None,
        emit: Callable[..., Any] | None = None,
        rx_sink: Callable[[bytes], None] | None = None,
        store_factory: Callable[..., ObserveSessionStore] | None = None,
        sessions_dir: Path | None = None,
        imports_dir: Path | None = None,
        baud: int = OBSERVE_BAUD,
        archive_index: ArchiveIndex | None = None,
        decoder_factory: Callable[[Path, str], Callable[[bytes], bytes]] | None = None,
        plugins: PlatformPlugins | None = None,
        identity_wait_s: float = IDENTITY_WAIT_S,
        identity_query_text: str = "",
        identity_query_delay_s: float = 0.0,
        auto_identity: bool = True,
        expected_identity: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        # 组合入口交进来的插件束（ / R2b）：串口实现、零共享探测、字典库、
        # 解码器、项目事件解析器、token domain、设备特定回答规则都在里面。
        # 没装插件不是错误状态：core 会**如实降级**（不知道就不假装知道）。
        self.plugins = plugins or NO_PLUGINS
        self.registry = registry or self.plugins.device_registry or DeviceRegistry(())
        self._default_baud = int(baud)
        self.baud = self._default_baud
        #: 需求 §6.10：使用者可以在连接时设基础串口参数（白名单见 `SERIAL_CHOICES`）。
        self._serial_overrides: dict[str, Any] = {}
        self._serial_applied: dict[str, Any] = {}
        self._serial_refused: dict[str, str] = {}
        self.sessions_dir = Path(sessions_dir or DEFAULT_SESSIONS_DIR)
        # 导入的诊断包住在**另一个**根（切片 8）。默认与 sessions_dir 并排：
        # 宿主测试注入临时 sessions_dir 时，导入目录也跟着进临时目录，不会污染真证据。
        self.imports_dir = Path(imports_dir or (self.sessions_dir.parent / IMPORT_DIR_NAME))
        self._port_factory = port_factory or self.plugins.port_factory or unwired_port_factory
        self._probe = probe or self.plugins.port_probe or unknown_access
        self._active_run = active_run or (lambda: None)
        # ⚠️ ：实时观察这条路的每一帧都带**会话身份**。
        # 为什么必须有：这条路的事件会进**进程级**环形缓冲（`EventBus.backlog`），
        # 新页面连上来时会重放一段 —— 没有会话号，页面就分不出“这是正在跑的那次会话的行”
        # 还是“上一次会话的录像”，只能按路标（`origin="live"`）投递，于是未连接的实时屏
        # 被旧录像填满（用户报的“未连接却在一直输出”）。
        # 会话号要到 `_connect_locked` 建会话目录时才定，所以这里**调用时**才读；
        # 还没有会话（例如启动阶段的自述）就不带这个键，而不是带一个 null 充数。
        upstream = emit or (lambda *args, **kwargs: None)

        def emit_with_session(kind: str, **fields: Any) -> Any:
            session_id = getattr(self, "_session_id", None)
            if session_id:
                fields.setdefault("session_id", session_id)
            return upstream(kind, **fields)

        self._emit = emit_with_session
        self._rx_sink = rx_sink
        self._store_factory = store_factory or default_store_factory
        # 构建归档索引与解码器都可以注入：宿主测试因此不需要真的归档或 Workbench 子模块。
        # 没注入时用插件束里的库工厂；插件也没给 ⇒ 这个索引会如实说"没有字典库"。
        self._archive = archive_index or ArchiveIndex(
            library_factory=self.plugins.library_factory
        )
        self._decoder_factory = decoder_factory
        # 实时指标（2026-10-07）：项目包把结构化事件折成指标序列（`metrics.jsonl` 那一份）。
        # 没装配 ⇒ 指标图在实时观察里不动（事件照常发，**不编一条假序列**）。
        # 累积的事件与 run 路径的内存量级相同（都留着一整份事件列表）。
        self._metrics_series = self.plugins.metrics_series
        self._live_events: list[Any] = []
        #: **当前这一行**产生的证据在各来源里各是第几行（ 的中心锚点）。
        #: 每处理一行重填一次：`raw` 恒有，`decoded` / `app_events` 只有这一行真的
        #: 解出了/产生了一条才有 —— 否则会把"上一行留下的行号"当成关键行。
        self._line_anchors: dict[str, int] = {}
        self._metrics_at = 0.0
        self._metrics_pending = 0
        #: 每个指标键**已经推给页面**多少个点（）：增量推送的游标。
        self._metrics_sent: dict[str, int] = {}
        # token domain：注入 > 插件束（它由目标 profile 提供）> 没声明。
        self.token_domain = str(self.plugins.token_domain or "")
        # 等 @PWINFO 的时长：测试里会调得很短。
        self.identity_wait_s = float(identity_wait_s)
        self.identity_query_text = str(identity_query_text or identity.IDENTITY_QUERY_TEXT)
        self.identity_query_delay_s = max(0.0, float(identity_query_delay_s))
        self.auto_identity = bool(auto_identity)
        #:  / L8：当前运行组合的**声明**（设备可能自称的项目名 / domain / 固件 id）。
        #: 装配处给；没给就是空表（宿主用例的判据如实跳过那几条核对）。
        self._expected_identity = expected_identity

        self._lock = threading.RLock()
        self._op_lock = threading.RLock()

        self._state = OBSERVE_IDLE
        self._detail = "尚未连接任何设备；打开页面不会占用串口"
        self._target = None  # 最近一次尝试连接的设备（失败后仍记得，重连要用）
        self._device = None  # 只在真正持有串口时才有值
        self._port = None
        self._reader: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._identity_worker: threading.Thread | None = None
        self._identity_stop: threading.Event | None = None

        self._opens = 0
        self._connected_utc: str | None = None
        self._closed_utc: str | None = None
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._rx_chunks = 0
        self._tx_messages = 0
        self._last_rx_utc: str | None = None
        self._last_tx_utc: str | None = None
        self._rx_error: str | None = None
        self._identity = IDENTITY_UNKNOWN
        self._identity_fields: dict[str, Any] = {}
        self._last: dict[str, Any] | None = None
        # 身份（需求 §6）：来源要分开记（device = 设备自报；banner = 主机看到的启动横幅）
        self._identity_source: str = IDENTITY_SOURCE_NONE
        self._identity_detail: str = ""
        self._identity_build_id32: int | None = None
        self._identity_query_at: float | None = None
        self._identity_answered_at: str | None = None
        self._identity_archive: dict[str, Any] | None = None
        self._identity_archive_tried: bool = False
        # 实时解码（切片 5）：字典精确命中才有 decoder；没命中时 decode.reason 说明原因。
        self._decoder: LiveDecoder | None = None
        self._decoder_reason: str = "还没查身份：不知道设备是哪个构建"
        # 当前会话的落盘与行视图（只在持有串口时存在）
        self._session_id: str | None = None
        self._session_started_utc: str | None = None
        self._store: ObserveSessionStore | None = None
        # 受控测试（ / L2b）：这条会话上**正在跑**的那一批。
        # 没跑过就是 None（快照里如实给 null，而不是给一个空壳）。
        self._live_tests: LiveTestLink | None = None
        self._live_batch_seq = 0
        #: L3：已经写进证据的"开始/每条执行的结论/整批收口"（每种只写一次）。
        self._live_recorded: set[str] = set()
        self._line_buffer = bytearray()
        # 这一行的前多少字节已经推给窗口了（安静时先把"半行"显示出来，见 `_flush_idle_tail`）。
        self._line_shown = 0
        self._last_rx_mono: float | None = None
        # "安静多久算一行"：阈值随波特率放宽（慢波特率下 30 ms 会盖不住一行，见 core_primitives）。
        self._partial_idle_s = partial_idle_s(OBSERVE_BAUD)

    # ============================================================= public API
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._tick_identity_locked()
            return self._snapshot_locked()

    def active_session_id(self) -> str | None:
        """当前（或刚刚结束的）会话号 —— 只读，不碰状态机，也不改快照。

        会话清单用它把"正在写的那一个"标出来（切片 6）：页面不需要为了问这一件事
        而把整个 `snapshot()` 走一遍（那会顺带推进身份超时的判定）。
        """
        with self._lock:
            return self._session_id

    def port_conflict(self, port: str) -> str | None:
        """自动验证的闸门：实时观察正拿着这个口时给出拒绝理由（否则 None）。"""
        with self._lock:
            if self._device is None or self._state not in HOLDING_STATES:
                return None
            if (self._device.port or "").upper() != (port or "").upper():
                return None
            device = self._device
            held = _duration_text(self._connected_utc)
        return (
            f"{device.label}（{device.port}）正被实时观察会话占用{held}："
            "请先在实时观察里断开，再启动自动验证（同一个串口不能有两个 owner）"
        )

    def serial_settings(self) -> dict[str, Any]:
        """当前生效的串口参数（页面、快照与证据都只读这一处）。

        `effective` = 默认值 + 本次连接的覆盖；`applied` / `refused` = **上一次打开**
        实际设上了什么、哪几项没设上（替身端口会拒绝，如实说出来）。
        """
        return {
            "defaults": dict(SERIAL_DEFAULTS),
            "effective": {**SERIAL_DEFAULTS, **self._serial_overrides},
            "applied": dict(self._serial_applied),
            "refused": dict(self._serial_refused),
            "choices": {name: list(values) for name, values in SERIAL_CHOICES.items()},
        }

    def connect(
        self, device_key: str, *, serial: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """打开所选串口。失败会作为一种**状态**返回，而不是抛异常。

        被拒绝的是"控制意图"（未知设备 / 已经连着别的设备），这是调用方用错了 API；
        而"串口不存在 / 被别的进程占着 / 自动验证正在用"是**环境事实**，
        页面要把它们显示成状态（需求 §3.2），所以走状态而不是错误码。

        `serial`（需求 §6.10）：本次连接的串口参数（波特率 / 数据位 / 校验位 / 停止位）。
        不传就沿用上一次；始终不传就是默认值。
        """
        if serial is not None:
            self._serial_overrides = sanitize_serial(serial)
        spec = self.registry.get(str(device_key or "").strip().lower())
        if spec is None:
            raise ObserveError(f"未知设备 {device_key!r}：可用的是 {[s.key for s in self.registry.specs()]}")
        with self._op_lock:
            with self._lock:
                if self._device is not None or self._state in HOLDING_STATES:
                    current = self._device or self._target
                    where = f"{current.label}（{current.port}）" if current else self._state
                    raise ObserveError(
                        f"已经连接 {where}：一次只连接一个设备，请先断开再切换",
                        status=409,
                    )
                self._target = spec
                self._state = OBSERVE_CONNECTING
                self._detail = f"正在打开 {spec.port}（不写 dtr/rts，不复位设备）"
            self._publish()
            return self._open(spec, how="connect")

    def disconnect(self, *, reason: str = "用户断开") -> dict[str, Any]:
        """关闭串口并释放所有权。没有连接时是幂等的（不是错误）。"""
        self._stop_identity_queries()
        with self._op_lock:
            with self._lock:
                if self._port is None and self._state == OBSERVE_IDLE:
                    return self._snapshot_locked()
            self._close_current(reason)
            return self.snapshot()

    def reconnect(self, *, serial: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """只做"关闭 + 重新打开"：不复位、不清设备数据；成功后自动查询身份。

        `serial`：重连时可以换成另一套串口参数（不传 = 沿用上一次）。

        ⚠️ **同一次观察会话**：重连不会新开一个会话目录 —— 它在 `io_events.jsonl` 里
        留一条 `reconnect` 事件，把"什么时候断过、什么时候又接上"记在同一条时间线上
        （真机验收时发现第一版每次重连都另开一个目录，一次观察被切成好几段）。
        会话只在**显式断开**或**串口消失**时结束。
        """
        if serial is not None:
            self._serial_overrides = sanitize_serial(serial)
        self._stop_identity_queries()
        with self._op_lock:
            with self._lock:
                spec = self._device or self._target
            if spec is None:
                raise ObserveError("还没有连接过任何设备：请先选择设备并连接", status=409)
            self._detach_port("重连：只关闭并重新打开串口", keep_session=True)
            # 重连也会把串口关一下：那一批已经"没下文"了（如实收 ERROR，不装作还在跑）。
            self._converge_live_tests("disconnected", "重连：串口被关了一下，这一步没有结论")
            with self._lock:
                self._state = OBSERVE_RECONNECTING
                self._detail = f"重连中：关闭并重新打开 {spec.port}（不复位、不清设备数据）"
            self._publish()
            return self._open(spec, how="reconnect")

    def note_identity(self, state: str, fields: dict[str, Any] | None = None) -> None:
        """身份握手的结果由切片 5 写入；现在只有一个诚实的"未查询"。"""
        with self._lock:
            self._identity = state
            if fields is not None:
                self._identity_fields = dict(fields)
        self._publish()

    # ============================================ 受控测试（ / L2b）
    def start_live_tests(
        self,
        *,
        plans: Iterable[Any],
        protocol: Any,
        emit: Callable[[dict[str, Any]], None] | None = None,
        batch_id: str = "",
        warmup_s: float = 0.0,
        stop_on_failure: bool = True,
    ) -> dict[str, Any]:
        """在这条**已经连上**的观察会话里开一批受控测试（需求 §4）。

        三件事都在这里定下来，而且都只有一个来源：

        * 会话身份：批次的 `session_id` 取当前会话（没连接就没有会话 ⇒ 409）；
        * 写口：命令走 `_write_tx` 那条**唯一**写路径（来源标 `live_test`）；
        * 阶段与终局：设备回来的每一行由 `_handle_line` 喂给链路，本方法不管读。

        ⚠️ 同时只允许一个受控批次（需求 §4）：已经在跑就 409，而不是排队或者"再来一批"。
        """
        with self._op_lock:
            with self._lock:
                if self._state != OBSERVE_CONNECTED or self._store is None:
                    raise ObserveError(
                        "只有已连接的观察会话才能跑受控测试：请先连接设备",
                        status=409,
                    )
                existing = self._live_tests
                session_id = self._session_id or ""
            if existing is not None and existing.batch.running is not None:
                raise ObserveError(
                    "这条观察会话已经有一个受控测试批次在跑（一次只允许一个）",
                    status=409,
                )
            subject = batch_id or f"{session_id}-b{self._batch_seq()}"
            try:
                batch = LiveTestBatch(
                    subject, session_id, plans, stop_on_failure=stop_on_failure,
                    # 阶段证据坐标（）：行数从会话的写口问（与磁盘上的行对齐），
                    # 采集期只记基线，阶段落终态时算区间与中心锚点。
                    evidence=StageEvidence(totals=self._evidence_totals),
                )
                link = LiveTestLink(
                    batch, protocol, write=self._write_test_command,
                    # 这一行在各类证据里各是第几行 —— 由会话回答（它才知道写到哪了）。
                    line_anchors=self._line_anchor_values,
                    # 宿主用例（L8）：身份状态视图 + 当前组合的声明 + 重新问一次身份的口子
                    # + "这个构建号本机构建过吗"。四个都是读出，不是事实本身。
                    identity_probe=self._identity_view,
                    expected_facts=self._expectations,
                    probe_identity=self.probe_identity,
                    build_known=self._known_build,
                    emit=emit or self._emit_live_tests, warmup_s=warmup_s,
                )
            except (LiveTestBatchError, LiveTestLinkError) as error:
                raise ObserveError(str(error), status=400) from None
            with self._lock:
                self._live_tests = link
                # ⚠️ 「每种只写一次」的账**属于这一批**，不属于这个服务进程。
                #    写成进程级（`__init__` 里建、从不重置）的后果：**同一条服务上的第二个
                #    观察会话一条受控测试证据都写不出来** —— `started` / `finished` 与每条的
                #    `execution_id` 都会被上一次的账挡掉，而 `_record_live_tests` 是**静默**
                #    return 的（不报错、不写事件、`manifest.live_tests` 也没有），于是出现
                #    “服务端快照说这一批 PASS、会话证据里一个字都没有”（）。
                #    通则：每加一个“只做一次”的环节，都要问清楚**是谁**只做一次 ——
                #    去重键的有效期写错，不会报错，只会静默少证据。
                self._live_recorded = set()
            try:
                snapshots = link.start()
            except ObserveError:
                with self._lock:
                    self._live_tests = None
                raise
            self._publish()
            return snapshots[-1]

    def cancel_live_tests(self, *, reason: str = "使用者取消了这一批") -> dict[str, Any]:
        """取消正在跑的那一批（没跑过就 409，不是静默成功）。"""
        with self._lock:
            link = self._live_tests
        if link is None:
            raise ObserveError("这条会话还没有跑过受控测试", status=409)
        try:
            link.cancel(reason)
        except Exception as error:  # 取消失败要说出来，不能假装取消了
            raise ObserveError(f"取消受控测试失败：{type(error).__name__}: {error}",
                              status=400) from error
        self._publish()
        return link.to_json()

    def live_tests_snapshot(self) -> dict[str, Any] | None:
        """只读：这一批的现状（不碰状态机，也不推进任何判定）。"""
        with self._lock:
            link = self._live_tests
        return None if link is None else link.to_json()

    def _batch_seq(self) -> int:
        with self._lock:
            self._live_batch_seq += 1
            return self._live_batch_seq

    def _live_tests_json_locked(self) -> dict[str, Any] | None:
        link = self._live_tests
        return None if link is None else link.to_json()

    def _emit_live_tests(self, snapshot: dict[str, Any]) -> None:
        """把链路的快照发出去，并写进会话证据（L3）。

        事件只从一处发布：证据与广播用的是**同一份**快照，不另拼一份。
        """
        self._record_live_tests(snapshot)
        # ⚠️ 阶段快照（`kind == "test_step"`）要按 **`test_step`** 发出去：页面的阶段窗
        #    （）认的就是那个 kind，再按 `origin=live` 落进**实时观察**那一屏
        #    （需求 §8.4：复用现有机制、首条有效 test_step 到达后自动展开）。
        #    其它快照（执行/收口）一律走 `live_test`。写成显式分支而不是靠参数覆盖 ——
        #    "复用阶段窗"要是结构性的，不能是巧合。
        kind = str(snapshot.get("kind") or "live_test")
        fields = {key: value for key, value in snapshot.items() if key != "kind"}
        try:
            self._emit("test_step" if kind == "test_step" else "live_test", **fields)
        except Exception:  # 广播失败不能影响采集
            pass

    def _record_live_tests(self, snapshot: dict[str, Any]) -> None:
        """受控测试会话证据（L3）：**粗粒度**事件 + 收口时一份冻结块进 manifest。

        * `live_test event=started`：这一批要跑哪几条（只写一次）；
        * `live_test event=execution`：每条执行的结论（每条只写一次）；
        * `live_test event=finished`：整批收口 + `manifest.live_tests`（定义 + 结果 + 命令）。

        ⚠️ 阶段变化**不**逐步落盘：它们已在冻结块里，逐步写会让工具窗口全是同义词；
        迟到的事件本身也不落盘（它们不改写任何结论，记在快照的 `late_events` 里）。
        """
        if snapshot.get("late"):
            return
        kind = str(snapshot.get("kind") or "")
        if kind in ("test_step", "live_test.device"):
            return
        with self._lock:
            link, store = self._live_tests, self._store
        if link is None or store is None:
            return
        try:
            # ① 开始：只写一次。不依赖"这一条快照长什么样"—— 开始那一刻发出去的是
            #    第一条执行的快照（不是批次级的），按形状判会漏。
            if "started" not in self._live_recorded:
                self._live_recorded.add("started")
                store.note(
                    "live_test", event="started", batch_id=link.batch.batch_id,
                    stop_on_failure=bool(link.batch.stop_on_failure),
                    executions=[
                        {"execution_id": row.execution_id,
                         "case_id": row.plan.case_id, "version": row.plan.version}
                        for row in link.batch.executions
                    ],
                )
            # ② 每条执行的结论：每条只写一次（终局快照带 execution_id 与终态）。
            execution_id = str(snapshot.get("execution_id") or "")
            status = str(snapshot.get("status") or "")
            if (execution_id and status in TERMINAL
                    and execution_id not in self._live_recorded):
                self._live_recorded.add(execution_id)
                store.note(
                    "live_test", event="execution", execution_id=execution_id,
                    case_id=snapshot.get("case_id"), version=snapshot.get("version"),
                    status=status, reason=snapshot.get("reason") or "",
                    duration_ms=snapshot.get("duration_ms"),
                )
            # ③ 整批收口：写一次事件 + 一份**冻结块**进 manifest。
            #     判定看**批次自己的状态**（四个收口入口都会把它置成终态）。
            if "finished" not in self._live_recorded and str(link.batch.status) in TERMINAL:
                self._live_recorded.add("finished")
                store.note(
                    "live_test", event="finished", batch_id=link.batch.batch_id,
                    status=link.batch.status, result=link.batch.result,
                    reason_code=link.batch.reason_code or "",
                    reason=link.batch.reason or "",
                    summary=link.batch.summary(),
                )
                store.note_live_tests(evidence_block(link))
        except Exception as error:   # 写证据失败不能把测试或断开流程搞崩
            self._note_rx_error(error)

    def _evidence_totals(self) -> dict[str, int]:
        """四类证据**各自**的行数（阶段坐标的区间基准，）。

        阶段**开始**时快照一次 ⇒ 这一阶段的区间起点。行数由写口报
        （`ObserveStore.evidence_totals()`），**不在页面或链路里重数** ——
        行号必须与证据文件逐行对齐，重数一遍就会漂。
        中心锚点（"设备报那一步的那一行"）走 `_line_anchor_values()`：
        它比这里窄 —— 只有那一行**真的产生了某一类证据**时才算那一类的锚点。
        """
        with self._lock:
            store = self._store
        if store is None:
            return {}
        try:
            return dict(store.evidence_totals())
        except Exception as error:   # 读不到行数不给坐标，但绝不影响采集与批次
            self._note_rx_error(error)
            return {}

    def _line_anchor_values(self) -> dict[str, int]:
        """**当前这一行**在各来源里各是第几行 —— 只算它真的产生了证据的那几类。

        `raw`：每一行设备输出都是一行原始串口（恒有）；
        `decoded` / `app_events`：只有这一行解出了/产生了一条才算 —— 否则会给一个
        "上一行留下的那个行号"，而它恰好落在区间里时就会被当成锚点
        （实测：一条不产生事件的心跳行会被当成"足以判定这一步的关键行"）。
        由 `_handle_line` 逐行填（读一行、清一行）。
        """
        with self._lock:
            return dict(self._line_anchors)

    def _write_test_command(self, line: str) -> None:
        """受控测试的命令走**同一条**写口（`_write_tx`）—— 不另开一条写路径。"""
        with self._lock:
            target = self._device or self._target
        ending = getattr(target, "line_ending", None)
        try:
            payload = build_text_payload(line, ending)
        except ValueError as error:
            raise ObserveError(f"受控测试的命令非法：{error}", status=400) from None
        self._write_tx(
            payload, source=LIVE_TEST_SOURCE, display=render_text(line, ending)
        )

    def _feed_live_tests(self, text: str) -> None:
        """把一行设备输出交给链路（它自己判定这行算不算本批次的）。"""
        with self._lock:
            link = self._live_tests
        if link is None:
            return
        try:
            changed = link.feed(text)
        except Exception as error:  # 一行回包不能把采集线程弄死
            self._note_rx_error(error)
            return
        if changed:
            self._publish()

    def _tick_live_tests(self) -> None:
        """超时判定（读取循环每跑一圈问一次：安静时也得判，否则永远不超时）。"""
        with self._lock:
            link = self._live_tests
        if link is None or link.batch.running is None:
            return
        try:
            changed = link.tick()
        except Exception as error:  # 写不出取消命令（口没了）也是一种结果，收口仍要走
            self._note_rx_error(error)
            self._converge_live_tests("disconnected", f"收口时写不出命令：{error}")
            return
        if changed:
            self._publish()

    def _converge_live_tests(self, code: str, reason: str) -> None:
        """串口走了 / 会话结束了：把还在跑的那批收口（不留永久 `RUNNING`）。"""
        with self._lock:
            link = self._live_tests
        if link is None:
            return
        try:
            if code == "session_ended":
                changed = link.session_ended(reason)
            else:
                changed = link.disconnected(reason)
        except Exception as error:  # 收口失败也不能把断开流程搞崩
            self._note_rx_error(error)
            return
        if changed:
            self._publish()

    def send(self, *, mode: str, text: str, line_ending: object = None) -> dict[str, Any]:
        """命令框：把一次输入变成字节写进串口，并记进会话时间线（需求 §4.1 / §5.2）。

        职责边界很清楚：`console_input` 负责"人输入 → 字节"（严格、非法就报错），
        这里只负责"写下去 + 记下来"。写下去的是什么就记什么：
        `bytes_hex` 是证据，`display` 只是视图。
        """
        try:
            payload, display, source = build_payload(
                mode=mode, text=text, line_ending=line_ending
            )
        except ValueError as error:
            # 非法输入一个字节也不发（需求 §4.1）。
            raise ObserveError(str(error), status=400) from None
        return self._write_tx(payload, source=source, display=display)

    def send_keys(self, keys: object) -> dict[str, Any]:
        """终端直输：一批按键 → 一段字节 → **一次** write（需求 §4.2 / §5.2）。

        - 按键 → 字节的映射只有 `console_input.build_keys_payload()` 一份实现：
          浏览器只说"按了什么"，由服务端换算字节（两份映射一定会漂，）。
        - 整批都按不出字节（只按修饰键 / 键名不认识）⇒ 400，**一个字节也不写**；
        - `keys` 字段把"这段字节是哪几个按键按出来的"一起记进 `io_events.jsonl` ——
          字节是证据，按键名是它的说明书。
        """
        with self._lock:
            target = self._device or self._target
        try:
            payload, display, labels = build_keys_payload(
                keys,
                line_ending=getattr(target, "line_ending", None),
                backspace=getattr(target, "terminal_backspace", None),
            )
        except ValueError as error:
            raise ObserveError(str(error), status=400) from None
        return self._write_tx(payload, source=TERMINAL_SOURCE, display=display, keys=labels)

    def _write_tx(
        self,
        payload: bytes,
        *,
        source: str,
        display: str,
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """**整个模块唯一写串口的地方**（命令框与终端直输都走这里）。

        ⚠️ 只有一处 `port.write()` 是故意的：宿主回归直接数源码里的 `.write(`
        （`the_only_write_path_is_the_explicit_send`）。多一条写路径，
        "发出去的字节都记了证据"这个承诺就要重新证。

        没连接时 409、写失败时 502 —— **两种都不会记一条"发过了"的事件**。
        写入成功之后才出现 `tx` 事件：先有字节，再有指向它的记录。
        """
        with self._op_lock:
            with self._lock:
                port = self._port
                store = self._store
                if port is None or self._state != OBSERVE_CONNECTED:
                    raise ObserveError(
                        f"当前状态是「{STATE_LABELS.get(self._state, self._state)}」："
                        "只有已连接时才能发送字节，请先连接",
                        status=409,
                    )
            try:
                port.write(payload)
                port.flush()
            except Exception as error:
                # 写失败就说失败：**不**记一条“发过了”的事件。
                self._note_rx_error(error)
                raise ObserveError(
                    f"写入串口失败：{type(error).__name__}: {error}", status=502
                ) from error
            with self._lock:
                self._tx_bytes += len(payload)
                self._tx_messages += 1
                self._last_tx_utc = utc_now()
            record: dict[str, Any] | None = None
            if store is not None:
                try:
                    record = store.write_tx(
                        payload, source=source, display=display, keys=keys
                    )
                except Exception as error:
                    self._note_rx_error(error)

        self._emit_tx_line(source, display, event_seq=(record or {}).get("seq"))
        self._publish()  # TX 计数与 io 事件数要立刻反映到页面上
        return {
            "seq": (record or {}).get("seq"),
            "t_mono_us": (record or {}).get("t_mono_us"),
            "kind": "tx",
            "source": source,
            "display": display,
            "keys": keys,
            "bytes_hex": bytes_hex(payload),
            "length": len(payload),
        }

    # ========================================================== 身份握手（切片 5）
    def query_identity(self) -> dict[str, Any]:
        """发送项目声明的只读身份查询并等 `@PWINFO` 回应。"""
        with self._lock:
            target = self._device or self._target
            state = self._state
        if state != OBSERVE_CONNECTED:
            raise ObserveError(
                f"当前状态是「{STATE_LABELS.get(state, state)}」："
                "只有已连接时才能查身份，请先连接",
                status=409,
            )
        payload, display, _source = build_payload(
            mode="text",
            text=self.identity_query_text,
            line_ending=getattr(target, "line_ending", None),
        )
        sent = self._write_tx(payload, source="identity_query", display=display)
        now = time.perf_counter()
        with self._lock:
            self._identity = IDENTITY_QUERYING
            self._identity_source = IDENTITY_SOURCE_DEVICE
            self._identity_query_at = now
            self._identity_detail = (
                f"已发出 {display}，等 @PWINFO 回应（{self.identity_wait_s:g} s 超时）"
            )
        self._publish()
        return sent

    def probe_identity(self) -> dict[str, Any]:
        """受控用例要求**重新**问一次身份（ / L8）：既有查询、同一个写口。

        与 `query_identity()` 的三点区别，每一点都是需求决定的：

        1. 来源标 `live_test` —— 这条命令属于那一次执行（它自己会在证据里）；
        2. **把上一次的结论作废**（清字段）—— 不然"重新核对"会拿连接时那一份旧自报
           当成本次的结果，PASS 就成了空话；
        3. 不启动自动重试那条链路：受控用例自己声明等待时间与超时（需求 §6.1）。
        """
        with self._lock:
            target = self._device or self._target
            state = self._state
        if state != OBSERVE_CONNECTED:
            raise ObserveError("只有已连接的观察会话才能查身份：请先连接设备", status=409)
        payload, display, _source = build_payload(
            mode="text",
            text=self.identity_query_text,
            line_ending=getattr(target, "line_ending", None),
        )
        with self._lock:
            self._identity = IDENTITY_QUERYING
            self._identity_source = IDENTITY_SOURCE_DEVICE
            self._identity_fields = {}
            self._identity_detail = f"受控用例正在问身份：已发出 {display}"
            self._identity_query_at = time.perf_counter()
        sent = self._write_tx(payload, source=LIVE_TEST_SOURCE, display=display)
        self._publish()
        return {**dict(sent), "display": display, "query_text": self.identity_query_text}

    def _identity_view(self) -> dict[str, Any]:
        """给宿主用例看的**身份状态视图**（就是会话那台既有状态机此刻的样子）。

        宿主用例（L8）不自己解析设备行：它只问"现在认出来了吗、认出的是什么"。
        `ok` 只在**身份确定且字段非空**时为真 —— 与页面上那句身份结论同源。
        `prefix` 是这一路声明的自报行前缀（比如 `@PWINFO ` / `BOOT build=`）：
        宿主用例拿它认"这一行看着像自报却没解出来"（协议错误的现场）。
        """
        with self._lock:
            state = self._identity
            fields = dict(self._identity_fields)
            detail = self._identity_detail
            source = self._identity_source
        return {
            "state": state,
            "ok": state == IDENTITY_OK and bool(fields),
            "fields": fields,
            "source": source,
            "detail": detail,
            "prefix": str(getattr(self.plugins, "identity_reply_prefix", "") or ""),
            "query_text": self.identity_query_text,
        }

    def _expectations(self) -> dict[str, Any]:
        """当前运行组合的**声明**（受控用例的判据拿它对表）。

        ⚠️ 只有"声明过的事实"才在表里：设备可能自称的项目名（profile 的 `identity_hint`）、
        这一路的 token domain。装配处没注入（宿主夹具 / 精简装配）就是空表 ——
        判据那边会如实跳过那几条（"没得核对"不等于"对上了"）。
        """
        reader = self._expected_identity
        if not callable(reader):
            return {}
        try:
            answer = reader()
        except Exception:
            return {}
        return dict(answer) if isinstance(answer, Mapping) else {}

    def _known_build(self, value: Any) -> bool | None:
        """"这个构建号在**本机这一份固件**的构建产物里吗"；`None` = 本机没有构建产物可比。

        问的是插件束交进来的 `known_build_ids()`（目标适配器读自己那份构建产物清单/生成头）——
        与**字典库分开**：没有 Token Log 的构建没有字典，它的构建号却照样在产物里；
        拿字典库当"本机构建"会把那份误判成"不是这里构建的"（真机验收当场撞到）。
        `None`（没装配 / 问不出来 / 一个都没有）与 `False`（确实不在）必须分得开 ——
        前者是"没得核对"，后者才是"板上跑的不是这里构建的那一份"。
        """
        reader = getattr(self.plugins, "known_build_ids", None)
        if not callable(reader):
            return None
        try:
            ids = reader()
        except Exception:
            return None
        if ids is None:
            return None
        number = identity.build_id32({"build_id": value})
        if number is None:
            return False   # 解析不出来的构建号，肯定不是本机那些产物里的一个
        return int(number) in tuple(int(item) for item in ids)

    def _start_identity_queries(self) -> None:
        """连接成功后自动查询；会话结束、收到身份或三次用尽时停止。"""
        self._stop_identity_queries()
        if not self.auto_identity or not self.capabilities().get("identity", False):
            return
        stop = threading.Event()
        worker = threading.Thread(
            target=self._identity_query_loop,
            args=(stop,),
            name="observe-identity",
            daemon=True,
        )
        with self._lock:
            self._identity_stop = stop
            self._identity_worker = worker
        worker.start()

    def _stop_identity_queries(self) -> None:
        with self._lock:
            stop, worker = self._identity_stop, self._identity_worker
            self._identity_stop = None
            self._identity_worker = None
        if stop is not None:
            stop.set()
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=READER_JOIN_S)

    def _identity_query_loop(self, stop: threading.Event) -> None:
        if self.identity_query_delay_s and stop.wait(self.identity_query_delay_s):
            return
        for attempt in range(1, IDENTITY_MAX_ATTEMPTS + 1):
            if stop.is_set():
                return
            with self._lock:
                if self._state != OBSERVE_CONNECTED or self._identity in {
                    IDENTITY_OK, IDENTITY_UNSUPPORTED
                }:
                    return
            with self._op_lock:
                if stop.is_set():
                    return
                try:
                    self.query_identity()
                except ObserveError:
                    return
            with self._lock:
                self._identity_detail = (
                    f"自动身份查询第 {attempt}/{IDENTITY_MAX_ATTEMPTS} 次已发送；"
                    f"{self.identity_wait_s:g} s 内无响应将重试"
                )
            self._publish()
            if stop.wait(self.identity_wait_s):
                return
        with self._lock:
            if self._identity not in {IDENTITY_OK, IDENTITY_UNSUPPORTED}:
                self._identity = IDENTITY_TIMEOUT
                self._identity_query_at = None
                self._identity_detail = (
                    f"自动身份查询已发送 {IDENTITY_MAX_ATTEMPTS} 次，设备均未在 "
                    f"{self.identity_wait_s:g} s 内回应：身份未知，继续采集 raw.bin"
                )
                self._decoder_reason = self._no_dictionary_reason()
        self._record_identity()
        self._publish()

    def _tick_identity_locked(self) -> None:
        """看身份查询是不是超时了（在锁里调用）。"""
        if self._identity != IDENTITY_QUERYING or self._identity_query_at is None:
            return
        if time.perf_counter() - self._identity_query_at < self.identity_wait_s:
            return
        self._identity = IDENTITY_TIMEOUT
        self._identity_detail = (
            f"发过 {self.identity_query_text} 但 {self.identity_wait_s:g} s 内没有 @PWINFO 回应："
            "身份未知，继续采集 raw.bin（不猜字典、不复位设备）"
        )
        self._identity_query_at = None
        self._decoder_reason = self._no_dictionary_reason()

    def _accept_identity(self, fields: dict[str, Any]) -> None:
        """收到 `@PWINFO`：记下**设备自报**的字段，并试着按 build id 选库。"""
        normalized = identity.normalize(fields)
        with self._lock:
            self._identity = IDENTITY_OK
            self._identity_source = IDENTITY_SOURCE_DEVICE
            self._identity_fields = normalized
            self._identity_query_at = None
            self._identity_answered_at = utc_now()
            build = identity.build_id32(normalized)
            self._identity_build_id32 = build
            self._identity_detail = "设备自报身份（@PWINFO）"
        self._resolve_archive(build, source=IDENTITY_SOURCE_DEVICE)
        self._record_identity()
        self._say(
            f"设备自报身份：build={normalized.get('build_id')} "
            f"project={normalized.get('project')} mcu={normalized.get('mcu')}",
            kind="identity",
        )
        self._publish()

    def _accept_banner(self, build_id32: int) -> None:
        """旧设备回退：从明文启动横幅里拿到 build id（需求 §6.2 允许的降级）。"""
        with self._lock:
            if self._identity == IDENTITY_OK:
                return  # 设备自报的更具体，别用横幅盖掉
            self._identity = IDENTITY_OK
            self._identity_source = IDENTITY_SOURCE_BANNER
            self._identity_build_id32 = build_id32
            self._identity_query_at = None
            self._identity_detail = (
                f"设备没有自报身份，用启动横幅里的 build=0x{build_id32:08x}（旧设备回退）"
            )
        self._resolve_archive(build_id32, source=IDENTITY_SOURCE_BANNER)
        self._record_identity()
        self._say(f"身份来自启动横幅：build=0x{build_id32:08x}", kind="identity")
        self._publish()

    def _identity_unsupported(self) -> None:
        """设备明说不认识查询命令：比超时更准确的一个状态。"""
        with self._lock:
            if self._identity == IDENTITY_OK:
                return
            self._identity = IDENTITY_UNSUPPORTED
            self._identity_query_at = None
            self._identity_detail = (
                f"设备回答不认识 {self.identity_query_text}：本机不做身份握手，继续采集 raw.bin"
            )
            self._decoder_reason = self._no_dictionary_reason()
        self._record_identity()
        self._publish()

    def _no_dictionary_reason(self) -> str:
        """为什么现在没有字典 —— 分清"没有可查的目标"与"查了没命中"（两者不能混为一谈）。"""
        with self._lock:
            build = self._identity_build_id32
            state = self._identity
        if build is None:
            return (
                f"设备没有报告 build id（身份状态：{IDENTITY_TEXT_HINT.get(state, state)}）："
                "没有可查的目标，字典未选定 —— 只采 raw.bin，不猜"
            )
        return f"主机没有 build 0x{build:08x} 对应的字典，拒绝解码（不猜字典）"

    def _record_identity(self) -> None:
        """身份一确定就写进 manifest（不等会话结束 —— 进程被杀也不能丢掉"是谁"）。"""
        with self._lock:
            store = self._store
            payload = {
                "state": self._identity,
                "source": self._identity_source,
                "fields": dict(self._identity_fields),
                "archive": self._identity_archive,
                "detail": self._identity_detail,
            }
        if store is None:
            return
        try:
            store.note_identity(**payload)
        except Exception as error:
            self._note_rx_error(error)

    def _resolve_archive(self, build_id32: int | None, *, source: str) -> None:
        """把 build id 换成"主机这边的字典"，并把结果写进会话 manifest（§6.2）。"""
        selection = self._archive.match(build_id32)
        with self._lock:
            self._identity_archive = selection
            self._identity_archive_tried = True
            store = self._store
        if selection is None:
            available = "；".join(self._archive.available()) or "（没有任何字典）"
            detail = (
                f"{self._no_dictionary_reason()}。已知字典：{available}"
            )
            with self._lock:
                self._decoder_reason = detail
            self._say(detail, kind="identity")
        else:
            with self._lock:
                self._decoder_reason = ""
            self._say(
                f"按 build 0x{build_id32:08x} 选了字典：{selection['origin']}"
                f"（{selection['entries']} 条）"
                + ("" if source == IDENTITY_SOURCE_DEVICE else " —— 身份来自启动横幅"),
                kind="identity",
            )
            self._start_decoder(selection)

    def _start_decoder(self, selection: dict[str, Any]) -> None:
        """字典精确命中之后才开始解码（没能耐就不要写半份 decoded.log）。"""
        with self._lock:
            if self._decoder is not None or self._store is None:
                return
        factory = self._decoder_factory or self.plugins.decoder_factory
        if factory is None:
            with self._lock:
                self._decoder_reason = (
                    "没有装配解码器（组合入口没提供 decoder_factory）："
                    "采集与 TX/RX 证据不受影响"
                )
            self._say(self._decoder_reason)
            return
        if self._decoder_factory is None and not self.plugins.decoder_is_available():
            with self._lock:
                self._decoder_reason = (
                    "Workbench 子模块缺失：本机没有解码器（先 git submodule update --init），"
                    "采集与 TX/RX 证据不受影响"
                )
            self._say(self._decoder_reason)
            return
        try:
            decoder = LiveDecoder(
                Path(selection["path"]),
                domain=self.token_domain,
                decoder_factory=factory,
                event_parser=self.plugins.event_parser,
            )
            decoder.decoder()  # 先把解码器建出来：建不起来就现在说，别等第一行
        except Exception as error:
            with self._lock:
                self._decoder_reason = f"实时解码不可用：{type(error).__name__}: {error}"
            self._say(self._decoder_reason)
            return
        with self._lock:
            self._decoder = decoder
            self._decoder_reason = ""
        self._say(
            f"实时解码已就绪：字典 {Path(selection['path']).name}（{selection['origin']}）"
        )
        self._publish()

    def _emit_tx_line(self, source: str, display: str, *, event_seq: Any = None) -> None:
        """工具窗口里的 TX 标记（需求 §5.2 的 `TX · command_text · STATUS<CR><LF>`）。

        ⚠️ 格式只有一份实现（`io_timeline.tx_line`）：会话结束回看时，
        `session_review` 会把同一份 `io_events.jsonl` 重新画一遍 —— 两份格式一定不能漂，
        否则"现场看到的"和"事后回看的"会成为两个故事。

        ⚠️ 它以 `TX` 开头 —— 不冒充设备输出，也不进 `raw.bin`。

        `event_seq`（这条 TX 事件在 `io_events.jsonl` 里的序号）随行发给页面：工具窗是派生
        视图，行号与文件行号不同源，阶段卡要指到这一行只能按事件号（）。写事件失败时
        它就是 `None` —— 那就**不编**一个。

        ⚠️ 键名必须是 `event_seq` 而**不能**是 `seq`：`seq` 是**平台事件流自己的序号**
        （`EventBus.publish` 先放进事件再 `update(fields)`，SSE 断线重连靠它接着发）——
        拿它装 io 时间线的号会当场把总线序号覆盖掉（实测：`events_since` 直接 TypeError）。
        """
        try:
            self._emit("tool", line=io_timeline.tx_line(source, display), event_seq=event_seq)
        except Exception:
            pass

    # =============================================================== internals
    def _open(self, spec: Any, *, how: str) -> dict[str, Any]:
        busy = self._active_run()
        if busy:
            return self._fail(
                spec,
                OBSERVE_OWNED_ELSEWHERE,
                f"自动验证 run {busy.get('run_id')} 正在使用 "
                f"{busy.get('port') or spec.port}；等它结束或取消后再连接",
            )

        access = self._probe(spec.port)
        if access == ACCESS_BUSY:
            return self._fail(
                spec,
                OBSERVE_OWNED_ELSEWHERE,
                f"{spec.port} 已经被别的程序打开（例如串口助手）；关掉它再连接",
            )
        if access == ACCESS_ABSENT:
            return self._fail(
                spec,
                OBSERVE_PORT_MISSING,
                f"本机没有枚举到 {spec.port}（换板子/换线之后点重连）",
            )

        try:
            settings = {**SERIAL_DEFAULTS, **self._serial_overrides}
            self.baud = int(settings["baud"])
            port = self._port_factory(spec.port, self.baud)
            # 需求 §6.10：基础收发要能设串口参数 —— 必须**在打开之前**设好，
            # 打开之后再改是白改（驱动不会重新配置那一帧）。
            applied, refused = apply_serial_settings(port, settings)
            self._serial_applied = {"baud": self.baud, **applied}
            self._serial_refused = refused
            # 读超时由**这一层**定（`core_primitives.READ_TIMEOUT_S`）：它决定
            # "安静的尾巴"晚多久才出现（0.1 s ⇒ 最多迟 100 ms，真机实测过）。
            try:
                port.timeout = READ_TIMEOUT_S
            except Exception:  # 替身端口不一定有 timeout，不影响采集
                pass
            port.open()
        except Exception as error:  # 打开失败要说清原因，不能只说"失败"
            return self._fail(
                spec,
                OBSERVE_OPEN_FAILED,
                f"{type(error).__name__}: {error}",
            )

        # 会话证据目录：**连不上就不建** —— 一个空目录会被读成"设备什么都没说"。
        device = {
            "key": spec.key,
            "label": spec.label,
            "port": spec.port,
            "role": spec.role,
        }
        # 重连复用同一个会话：会话只在显式断开/串口消失时结束（见 `reconnect()`）。
        reuse_session = self._store is not None
        if reuse_session:
            with self._lock:
                session_id, store = self._session_id, self._store
        else:
            try:
                session_id = new_session_id(self.sessions_dir, spec.key)
                store = self._store_factory(
                    self.sessions_dir,
                    session_id,
                    device=device,
                    port=spec.port,
                    baud=self.baud,
                    # 会话证据里记的能力声明 = **这个目标**的能力声明（同一处来源）。
                    capabilities=session_capabilities(getattr(spec, "capabilities", None)),
                    # 平台自述（R2d）：装配束里的 label/platform/project/adapters/token_domain
                    # + 版本表。写进 manifest ⇒ 诊断包自带“这是哪套组合、按哪个 schema 写的”。
                    platform=self._platform_facts(),
                )
            except Exception as error:
                # 证据落不下去就不要继续装成"在观察"：先把口关掉，再报状态。
                self._close_port(port)
                return self._fail(
                    spec,
                    OBSERVE_OPEN_FAILED,
                    f"无法创建会话目录：{type(error).__name__}: {error}",
                )

        with self._lock:
            self._port = port
            self._device = spec
            self._target = spec
            self._store = store
            self._session_id = session_id
            self._line_buffer = bytearray()
            self._opens += 1
            if not reuse_session:
                # ⚠️ 计数器属于**会话**，不是属于本次打开：重连不清零。
                # 否则页面上的 RX 与 raw.bin 会对不上（真机验收时看到 162 B vs 324 B），
                # 而"三个数永远对得上"正是这份证据能被核对的前提。
                self._rx_bytes = 0
                self._rx_chunks = 0
                self._tx_bytes = 0
                self._tx_messages = 0
                self._session_started_utc = utc_now()
                # 新会话 = 指标图从头开始（重连留在同一会话里，所以累积也继续）。
                self._live_events = []
                self._metrics_at = 0.0
                self._metrics_pending = 0
                self._metrics_sent = {}
            self._last_rx_utc = None
            self._last_tx_utc = None
            self._rx_error = None
            self._identity = IDENTITY_UNKNOWN
            self._identity_fields = {}
            if not reuse_session:
                # 新会话 = 重新认识这台设备：身份、字典匹配、解码器全部重来。
                # （重连留在同一会话里，所以设备侧那份身份也继续有效。）
                self._identity_source = IDENTITY_SOURCE_NONE
                self._identity_detail = ""
                self._identity_build_id32 = None
                self._identity_query_at = None
                self._identity_answered_at = None
                self._identity_archive = None
                self._identity_archive_tried = False
                self._decoder = None
                self._decoder_reason = "还没查身份：不知道设备是哪个构建"
            self._connected_utc = utc_now()
            self._closed_utc = None
            self._state = OBSERVE_CONNECTED
            self._detail = (
                f"已打开 {spec.port} @ {self.baud}；打开时未写 dtr/rts，设备没有被复位"
            )
        self.registry.set_owner(spec.key, "observe")
        action = "重连" if how == "reconnect" else "连接"
        self.registry.touch(spec.key, f"实时观察{action} {spec.port}")
        self._start_reader(port)
        self._start_identity_queries()
        self._publish()
        if how == "reconnect":
            # 重连不新开会话，只在同一条时间线上留一个断点（`reconnect` 事件已在
            # `_detach_port` 里写过；这里只补一句人能读的说明，不再写第二个事件）。
            self._say(
                f"会话 {session_id} 已重连：{spec.port} @ {self.baud}"
                "（只关重开、不复位；随后自动查询身份）"
            )
        else:
            self._say(
                f"会话 {session_id} 开始：{spec.port} @ {self.baud}，打开时不写 dtr/rts、不复位；"
                f"连接后自动查询身份，TX 留证；证据目录 {SESSION_DIR_NAME}/{session_id}/"
            )
        return self.snapshot()

    def _fail(self, spec: Any, state: str, detail: str) -> dict[str, Any]:
        """把一次失败落成状态：页面显示它，而不是一个不解释的错误码。

        ⚠️ 失败也要**收尾会话**：一个会话目录只在它真的被观察期间存在。
        否则“已经断开但目录里没有 closed_utc / totals”看起来像还在跑。
        """
        summary = self._close_store(f"连接失败：{detail}")
        with self._lock:
            self._target = spec
            self._device = None
            self._port = None
            self._state = state
            self._detail = detail
            self._closed_utc = utc_now()
            self._remember_last_locked(state, detail, summary)
        self._publish()
        return self.snapshot()

    def _detach_port(self, reason: str, *, keep_session: bool) -> dict[str, Any] | None:
        """关掉串口并停掉读取线程；`keep_session=True` 时保留会话目录与计数（重连）。"""
        with self._lock:
            if self._port is None and self._device is None:
                return None
            device = self._device
            self._state = OBSERVE_DISCONNECTING
            self._detail = f"正在关闭 {device.port if device else '串口'}（{reason}）"
        self._publish()
        self._stop_identity_queries()
        with self._lock:
            port = self._port
        port_closed = self._stop_reader(port)
        with self._lock:
            self._port = None
            if not keep_session:
                self._device = None
        if port is not None and not port_closed:
            self._close_port(port)
        self._flush_lines()  # 没以换行结尾的尾巴也要显示出来（与 run 的 collector 一致）
        if keep_session:
            with self._lock:
                store = self._store
            if store is not None:
                try:
                    store.note("reconnect", reason=reason, rx_bytes=store.rx_bytes)
                except Exception as error:
                    self._note_rx_error(error)
            summary = None
        else:
            summary = self._close_store(reason)
        if device is not None:
            self.registry.set_owner(device.key, None)
        return summary

    def _close_current(self, reason: str) -> None:
        """显式断开：关串口、结束会话、释放设备归属。"""
        with self._lock:
            if self._port is None and self._device is None:
                return
            device = self._device
        # 使用者显式结束观察：正在跑的那一批也要收口（"结束观察"与"断线"分开报）。
        # ⚠️ 必须在 `_detach_port` **之前**：那一步会把会话存储关掉，
        #    收口要写的那几条证据（事件 + manifest 冻结块）就没地方写了。
        self._converge_live_tests("session_ended", f"观察会话结束（{reason}）")
        summary = self._detach_port(reason, keep_session=False)
        with self._lock:
            self._remember_last_locked(OBSERVE_IDLE, f"已断开（{reason}）", summary)
            self._device = None
            self._closed_utc = utc_now()
            self._state = OBSERVE_IDLE
            self._detail = self._leave_text(reason, summary)
        if device is not None:
            self.registry.touch(device.key, "实时观察已断开")
        self._publish()

    # ------------------------------------------------------------------ reader
    def _start_reader(self, port: Any) -> None:
        stop = threading.Event()
        reader = threading.Thread(
            target=self._pump, args=(port, stop), name="observe-rx", daemon=True
        )
        with self._lock:
            self._stop = stop
            self._reader = reader
        reader.start()

    def _stop_reader(self, port: Any | None = None) -> bool:
        """Stop the RX worker and return whether this method had to close the port.

        A Windows serial read can occasionally remain blocked past the configured timeout.  Merely
        timing out ``join()`` and forgetting the thread leaves the process holding the COM handle
        while the public state has already moved to ``idle``.  Ask pyserial to cancel the pending
        read first; if that is unavailable or ineffective, closing the handle is the final wake-up.
        """
        with self._lock:
            stop, reader = self._stop, self._reader
        if stop is not None:
            stop.set()
        cancel_read = getattr(port, "cancel_read", None)
        if callable(cancel_read):
            try:
                cancel_read()
            except Exception:
                # Some drivers reject cancellation after an unplug.  Closing below is still valid.
                pass
        closed = False
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=READER_JOIN_S)
            if reader.is_alive() and port is not None:
                self._close_port(port)
                closed = True
                reader.join(timeout=READER_JOIN_S)
            if reader.is_alive():
                with self._lock:
                    self._detail = (
                        "串口句柄已请求关闭，但接收线程仍未退出；端口尚未确认释放，"
                        "服务不会把状态伪装成未连接"
                    )
                self._publish()
                raise ObserveError(self._detail, status=503)
        with self._lock:
            if self._stop is stop:
                self._stop = None
            if self._reader is reader:
                self._reader = None
        return closed

    def _pump(self, port: Any, stop: threading.Event) -> None:
        """把 RX 读走、落盘、撕成行推给页面。

        必须读：没人读的时候驱动缓冲会满、设备侧的写会阻塞 —— 那就不是"观察"了，
        是我们把被测设备拖住了。读到的字节不丢：先写进会话目录的 `raw.bin`
        （逐块 flush），再切成行推给「原始串口」窗口。
        """
        while not stop.is_set():
            try:
                waiting = int(getattr(port, "in_waiting", 0) or 0)
                chunk = port.read(waiting or 1)
            except Exception as error:  # 拔线 / 驱动报错
                if not stop.is_set():
                    self._port_gone(port, error)
                return
            if not chunk:
                # 没新字节：缓冲区里那截"还没等到换行"的尾巴，安静够久就照实显示出来。
                # ⚠️ 这一步只影响**视图**：敲一个字符的回显不带换行，不这样做就要等设备
                # 下一次输出换行（实测那次心跳 1 s 一次，滞后 45〜1940 ms）。
                self._flush_idle_tail(port)
                self._tick_live_tests()   # 安静时也要判超时，否则那一批永远不超时
                continue
            self._on_rx(port, chunk)

    def _flush_idle_tail(self, port: Any) -> None:
        """把"半行"照实交给窗口（终端语义：字节到了就显示，不必等换行）。

        ⚠️ 不消费缓冲区：半行仍然是半行，身份识别 / 实时解码只认完整行（`_handle_line`）。
        """
        with self._lock:
            if self._port is not port:   # 已经断开或换了端口：这段字节不属于任何会话
                return
            buffer = self._line_buffer
            shown = self._line_shown
            last = self._last_rx_mono
        if last is None:
            return
        idle_s = time.monotonic() - last
        tail = idle_partial(
            buffer, shown=shown, idle_s=idle_s, threshold_s=self._partial_idle_s
        )
        if not tail:
            return
        with self._lock:
            store = self._store
            self._line_shown = shown + len(tail)
        # 这半行**将来**会是 raw.bin 的第几行：缓冲区里始终只有正在拼的这一行，所以
        # 「已经完整处理过的行数 + 1」就是它 —— 不是猜的（对照 `_handle_line` 里数出来的
        # 那个数，两者相等；有判据盯着）。半行也照实带上行号：页面靠它给这一窗落**真**基准。
        pending = 0
        if store is not None:
            try:
                pending = int(store.raw_lines) + 1
            except Exception:
                pending = 0
        try:
            self._emit_raw_line(tail, continuation=shown > 0, line_no=pending)
        except Exception:  # 广播失败不能影响采集
            pass

    def _on_rx(self, port: Any, chunk: bytes) -> None:
        with self._lock:
            if self._port is not port:  # 已经断开或换了端口：这段字节不属于任何会话
                return
            self._rx_bytes += len(chunk)
            self._rx_chunks += 1
            self._last_rx_utc = utc_now()
            sink = self._rx_sink
            store = self._store
            buffer = self._line_buffer
        # 顺序要紧：**先落盘**（证据），再把同一份字节切成行推给页面（视图）。
        # 反过来的话，页面上看得到的那一行可能永远不在磁盘上。
        if store is not None:
            try:
                store.write_rx(chunk)
            except Exception as error:  # 落盘失败不能让读取线程死掉
                self._note_rx_error(error)
        if sink is not None:
            try:
                sink(chunk)
            except Exception as error:
                self._note_rx_error(error)
        buffer.extend(chunk)
        with self._lock:
            self._last_rx_mono = time.monotonic()
        for line in split_complete_lines(buffer):
            with self._lock:
                shown = self._line_shown
                self._line_shown = 0     # 新的一行："已经显示过多少"从零重算
            self._handle_line(line, shown=shown)

    def _handle_line(self, raw_line: bytes, *, shown: int = 0) -> None:
        """一整行 RX 的**第二道**处理：身份识别 + 实时解码（切片 5）。

        顺序有讲究：先把这行推给「原始串口」窗口（视图与 raw.bin 同源），
        再识别身份 —— 因为身份决定用哪份字典，而解码要靠它。

        交接受控测试放在**解码之后**（）：阶段坐标要写清"设备报这一步的那一行
        在各证据里各是第几行"，而 decoded / app_events 那两份证据正是这一步产生的 ——
        先喂测试的话，锚点会指到**上一行**去（差一行就是定位差一行）。

        `shown` = 这行的前多少字节已经当"半行"显示过了（安静时就先显示出去了）：
        窗口只补上剩下那段，**不要重复画**；身份识别与解码始终用**完整行**。

        ⚠️ **先数这一行、再把它推给窗口**（）：窗口那一行因此带着**真行号**出发
        （`line_no`），页面才敢拿它当这一窗的行号基准 —— 不然刷新之后只能停在
        "窗口第一行大概就是文件第一行"这个假前提上。阶段坐标的 `raw` 锚点用的也是这个数。
        """
        remainder = visible_line_remainder(raw_line, shown)
        # raw.bin 的行数（与按换行切出来的行逐行对齐）：受控测试阶段的 `raw` 坐标用它。
        # ⚠️ 在这里数、不在 `write_rx` 里数：一个分块可能装好几行，按字节数出来的是
        #    "这一块里有多少行"，而阶段坐标与实时行号要的是"这一行是第几行"。
        anchors: dict[str, int] = {}
        with self._lock:
            store = self._store
        if store is not None:
            try:
                anchors["raw"] = store.count_raw_line()
            except Exception as error:
                self._note_rx_error(error)
        if remainder:
            self._emit_raw_line(
                remainder, continuation=shown > 0, line_no=anchors.get("raw", 0)
            )
        text = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        # ① 身份：设备自报 > 它的"不认识"回答 > 启动横幅（旧设备回退）
        #    "不认识"的**判定文本属于设备/项目**（注入），core 只把结果收敛成状态。
        pwinfo = identity.parse_pwinfo(text)
        if pwinfo is not None:
            self._accept_identity(pwinfo)
        elif (project_identity := self.plugins.parse_project_identity(text)) is not None:
            self._accept_identity(project_identity)
        elif self.plugins.device_is_unsupported_reply(text):
            self._identity_unsupported()
        else:
            banner = identity.banner_build_id(text)
            if banner is not None:
                self._accept_banner(banner)
        with self._lock:
            decoder = self._decoder
        if decoder is not None:
            try:
                decoded, event = decoder.feed_line(raw_line)
            except Exception as error:
                # 解码出问题不能让采集停：记下原因、关掉解码，raw.bin 继续写。
                self._note_rx_error(error)
                with self._lock:
                    self._decoder = None
                    self._decoder_reason = (
                        f"实时解码已停止：{type(error).__name__}: {error}（raw.bin 采集不受影响）"
                    )
                self._say(self._decoder_reason)
            else:
                decoded_text = decoded.decode("utf-8", "replace").rstrip("\r\n")
                if store is not None:
                    try:
                        store.write_decoded_line(decoded.decode("utf-8", "replace"))
                        anchors["decoded"] = store.decoded_lines
                    except Exception as error:
                        self._note_rx_error(error)
                try:
                    self._emit("decoded", **self._line_fields(decoded_text, anchors, "decoded"))
                except Exception:
                    pass
                if event is not None:
                    if store is not None:
                        try:
                            store.write_app_event(event)
                            anchors["app_events"] = store.app_events
                        except Exception as error:
                            self._note_rx_error(error)
                    with self._lock:
                        self._live_events.append(event)
                        self._metrics_pending += 1
                    # 指标**随事件一起推**（节流）：实时观察那一屏的「指标趋势」因此会随着数据长，
                    # 而不是只在 run 的 decode 步出现一次。算不出来就不带这个字段。
                    try:
                        fields = self._line_fields(None, anchors, "app_events")
                        self._emit("app_events", events=[event.to_json()],
                                   metrics=self._live_metrics(), **fields)
                    except Exception:
                        pass
        # ② 受控测试：这一行是不是某条执行的回包、是不是某一步的推进（L2b）。
        #    ⚠️ 先把"这一行产生的证据"挂上去再喂：阶段坐标的中心锚点就是它。
        with self._lock:
            self._line_anchors = anchors
        self._feed_live_tests(text)

    def _live_metrics(self) -> dict[str, Any] | None:
        """把这次会话**到目前为止**的结构化事件折成一条指标序列（节流）。

        三条自我约束（错了比不做更糟）：
         1. 没装配项目包的 `metrics_series`（或本会话还没有事件）⇒ 返回 None：事件照常发，
            指标图不动 —— 如实降级，**不编一条假序列**；
         2. 重算有节流（`LIVE_METRICS_INTERVAL_S` / `LIVE_METRICS_EVENT_STEP`）：设备打一条
            `[I][metric]` 的间隔是 2～30 s，所以实际是“每条新度量行算一次”，但一批行涌入
            时不会把读串口的线程按在 O(n²) 上；
         3. 重算抛错 ⇒ **停掉实时指标并说出来**（不是每行报一次错），采集与证据不受影响。

        ⚠️ （第四条约束）：**只推新增的点**（`metrics_delta`），不再每次带“到此刻
        为止”的整份序列 —— 那样一帧的字节数随会话线性增长，整场会话 O(n²)（单帧曾经
        6.8 MB）。页面把增量**并进**自己那份，所以图上看到的仍然是完整序列：
        变的是“线上传什么”，不是“图上有什么”。
        """
        series = self._metrics_series
        if series is None or self._metrics_pending <= 0:
            return None
        now = time.perf_counter()
        if ((now - self._metrics_at) < LIVE_METRICS_INTERVAL_S
                and self._metrics_pending < LIVE_METRICS_EVENT_STEP):
            return None
        with self._lock:
            events = list(self._live_events)
            self._metrics_pending = 0
            sent = dict(self._metrics_sent)
        try:
            payload = series(events)
        except Exception as error:
            self._metrics_series = None
            self._say(f"实时指标已停止：{type(error).__name__}: {error}（采集与证据不受影响）")
            return None
        delta, cursors = metrics_delta(payload, sent)
        with self._lock:
            self._metrics_sent = cursors
            # 普通日志也会触发这段重算。只有真的产出新指标点才占用节流时间片；
            # 否则“普通日志在前、指标行紧随其后”的设备会永久饿死指标图。
            if delta is not None:
                self._metrics_at = now
        return delta

    def _note_rx_error(self, error: Exception) -> None:
        with self._lock:
            self._rx_error = f"{type(error).__name__}: {error}"

    @staticmethod
    def _line_fields(
        text: str | None, anchors: Mapping[str, int], source: str
    ) -> dict[str, Any]:
        """一条实时证据行事件的字段：文本（可选）+ **这一行是那一份证据的第几行**。

        `line_no`（1 基）**只在数得出来的时候**带 —— 数不出来就不带这个字段，页面据此把
        那一窗降级成"行号是假定值"（而不是拿一个猜的数当真行号）。它与受控测试阶段坐标
        （`manifest.live_tests` 的 `start_line` / `anchor_line`）用的是**同一份账**。
        """
        fields: dict[str, Any] = {}
        if text is not None:
            fields["line"] = text
        value = int(anchors.get(source, 0) or 0)
        if value > 0:
            fields["line_no"] = value
        return fields

    def _emit_raw_line(
        self, line: bytes, *, continuation: bool = False, line_no: int = 0
    ) -> None:
        """「原始串口」窗口的一行 —— 与 raw.bin 里那一行是同一份字节。

        `line_no` = 它是 raw.bin 的第几行（1 基）；0 = 数不出来（不带这个字段）。
        """
        try:
            fields = self._line_fields(
                line.decode("utf-8", "replace").rstrip("\r\n"),
                {"raw": line_no},
                "raw",
            )
            self._emit("raw", continuation=continuation, **fields)
        except Exception:  # 广播失败不能影响采集
            pass

    def _flush_lines(self) -> None:
        """把没以换行结尾的尾巴也显示出来（拔线/断开时最后那段字节）。"""
        with self._lock:
            buffer = self._line_buffer
            shown = self._line_shown
            self._line_buffer = bytearray()
            self._line_shown = 0
            self._last_rx_mono = None
        if buffer:
            # 已经当"半行"显示过的那一段不重画（`shown`），但身份/解码仍然用完整的那截。
            self._handle_line(bytes(buffer), shown=shown)

    def _say(self, text: str, *, kind: str | None = None) -> None:
        """一条主机侧说明：进「工具输出」窗口，可选地也进 io_events.jsonl 的时序。

        ⚠️ 它**不会**进 raw.bin：那里只有设备说的话（需求 §5.1）。

        ⚠️ 两种行的身份不一样，页面必须分得清：**带了 `kind`** 的那几句在
        `io_events.jsonl` 里也有一条事件 ⇒ 随行带上它的事件号 `event_seq`（阶段卡能按事件号
        指到这一行）；**不带 `kind`** 的那几句只活在现场（回看不会重画它们）⇒ 行上没有事件号，
        不编一个。

        ⚠️ 键名是 `event_seq` 不是 `seq`：`seq` 属于**平台事件流自己**（SSE 的续传游标），
        见 `_emit_tx_line` 里的同一条提醒。
        """
        with self._lock:
            store = self._store
        event_seq: Any = None
        if store is not None and kind:
            try:
                event_seq = (store.note(kind, message=text) or {}).get("seq")
            except Exception:
                event_seq = None
        try:
            self._emit("tool", line=f"[observe] {text}", event_seq=event_seq)
        except Exception:
            pass

    def _close_store(self, reason: str) -> dict[str, Any] | None:
        with self._lock:
            store, self._store = self._store, None
            session_id, self._session_id = self._session_id, None
            self._decoder = None
            # 会话结束：实时指标累积归零（下一次连接从头开始）——
            # 页面上已经画出来的图**不清空**（那是刚看过的东西）。
            self._live_events = []
            self._metrics_pending = 0
            self._metrics_sent = {}
            identity_state = self._identity
            identity_source = self._identity_source
            identity_fields = dict(self._identity_fields)
            identity_archive = self._identity_archive
            identity_detail = self._identity_detail
        if store is None:
            return None
        # 身份与字典匹配写进 manifest（需求 §6.2）：会话结束时它得能自解释。
        try:
            store.note_identity(
                state=identity_state,
                source=identity_source,
                fields=identity_fields,
                archive=identity_archive,
                detail=identity_detail,
                announce=False,   # 时间线上那条已经在身份确定时记过了
            )
        except Exception as error:
            self._note_rx_error(error)
        try:
            store.close(reason)
        except Exception as error:
            self._note_rx_error(error)
        summary = {
            "session_id": session_id,
            "rx_bytes": store.rx_bytes,
            "rx_chunks": store.rx_chunks,
            "tx_bytes": store.tx_bytes,
            "tx_messages": store.tx_messages,
            "raw_bytes": store.raw_offset,
            "io_events": store.events,
            "decoded_bytes": store.decoded_bytes,
            "decoded_lines": store.decoded_lines,
            "app_events": store.app_events,
            "identity": identity_state,
            "identity_source": identity_source,
        }
        decoded_note = (
            f"；decoded.log {store.decoded_bytes} B / {store.decoded_lines} 行"
            f"、app_events.jsonl {store.app_events} 条"
            if store.decoded_lines or store.app_events
            else ""
        )
        self._say(
            f"会话 {session_id} 结束：RX {store.rx_bytes} B / {store.rx_chunks} 次{decoded_note}；"
            f"证据 {SESSION_DIR_NAME}/{session_id}/"
        )
        return summary

    def _leave_text(self, reason: str, summary: dict[str, Any] | None) -> str:
        if summary is None:
            return f"已断开（{reason}）；本次会话一共打开过 {self._opens} 次串口（没有会话目录）"
        return (
            f"已断开（{reason}）：本次会话 RX {summary['rx_bytes']} B / {summary['rx_chunks']} 次，"
            f"证据 {SESSION_DIR_NAME}/{summary['session_id']}/ 已落盘"
        )

    def _port_gone(self, port: Any, error: Exception) -> None:
        """设备被拔掉：如实说"串口消失"，并且**不**自动重连（需求 §3.2）。"""
        with self._op_lock:
            with self._lock:
                stop = self._stop
                if self._port is not port or (stop is not None and stop.is_set()):
                    return
                detail = (
                    f"读取时出错（{type(error).__name__}: {error}）；设备可能被拔掉了 "
                    "—— 不会自动重连，插好后请手工点重连"
                )
                device = self._device
                self._state = OBSERVE_PORT_MISSING
                self._detail = detail
                self._port = None
                self._device = None
                self._closed_utc = utc_now()
                self._stop = None
                self._reader = None
            self._close_port(port)
            self._flush_lines()
            self._converge_live_tests("disconnected", "串口消失：这一步没有结论")
            summary = self._close_store(f"串口消失（{type(error).__name__}）")
            with self._lock:
                self._remember_last_locked(OBSERVE_PORT_MISSING, detail, summary)
            if device is not None:
                self.registry.set_owner(device.key, None)
                self.registry.touch(device.key, "实时观察断开（串口消失）")
            self._publish()

    def _close_port(self, port: Any) -> None:
        try:
            port.close()
        except Exception:  # 已经关掉 / 句柄失效：不是错误
            pass

    # ------------------------------------------------------------- publishing
    def _publish(self) -> None:
        snapshot = self.snapshot()
        try:
            self._emit(
                "observe_state",
                observe=snapshot,
                message=f"实时观察：{snapshot['state_label']} —— {snapshot['detail']}",
            )
        except Exception:  # 广播失败不能影响状态机本身
            pass

    def _remember_last_locked(
        self, state: str, detail: str, summary: dict[str, Any] | None = None
    ) -> None:
        device = self._device or self._target
        session_id = (summary or {}).get("session_id") or self._session_id
        self._last = {
            "device": device.label if device else None,
            "port": device.port if device else None,
            "state": state,
            "detail": detail,
            "rx_bytes": self._rx_bytes,
            "tx_bytes": self._tx_bytes,
            "tx_messages": self._tx_messages,
            "opens": self._opens,
            "started_utc": self._session_started_utc or self._connected_utc,
            "ended_utc": utc_now(),
            "session_id": session_id,
            "evidence_dir": f"{SESSION_DIR_NAME}/{session_id}" if session_id else None,
            "raw_bytes": (summary or {}).get("raw_bytes"),
            "io_events": (summary or {}).get("io_events"),
            # 派生产物与身份也跟着"上一次会话"一起留着：断开之后页面还能说清
            # 这次观察到底解了多少行、认的是哪台设备。
            "decoded_bytes": (summary or {}).get("decoded_bytes", 0),
            "decoded_lines": (summary or {}).get("decoded_lines", 0),
            "app_events": (summary or {}).get("app_events", 0),
            "identity": (summary or {}).get("identity"),
            "identity_source": (summary or {}).get("identity_source"),
        }

    def _snapshot_locked(self) -> dict[str, Any]:
        device = self._device
        store = self._store
        session = None
        if device is not None:
            session = {
                "device": device.key,
                "port": device.port,
                "baud": self.baud,
                "session_id": self._session_id,
                "evidence_dir": (
                    f"{SESSION_DIR_NAME}/{self._session_id}" if self._session_id else None
                ),
                "io_events": store.events if store is not None else 0,
                "started_utc": self._session_started_utc,
                # 两个时长分开报：`connected_ms` 是**本次打开**多久（需求 §3.2），
                # `uptime_ms` 是**这次观察会话**多久（跨重连）。混成一个数必错一边。
                "connected_ms": _elapsed_ms(self._connected_utc),
                "uptime_ms": _elapsed_ms(self._session_started_utc),
                "rx_bytes": self._rx_bytes,
                "tx_bytes": self._tx_bytes,
                "tx_messages": self._tx_messages,
                "rx_chunks": self._rx_chunks,
                "last_rx_utc": self._last_rx_utc,
                "last_tx_utc": self._last_tx_utc,
                "opens": self._opens,
                # 派生产物的会话计数（切片 5）：与磁盘上的 decoded.log / app_events.jsonl 对应。
                "decoded_bytes": store.decoded_bytes if store is not None else 0,
                "decoded_lines": store.decoded_lines if store is not None else 0,
                "app_events": store.app_events if store is not None else 0,
            }
        target = self._target or device
        return {
            "state": self._state,
            "state_label": STATE_LABELS.get(self._state, self._state),
            "detail": self._detail,
            "holding": self._state in HOLDING_STATES,            "device": (
                {
                    "key": device.key,
                    "label": device.label,
                    "port": device.port,
                    "role": device.role,
                    "boundary": device.boundary,
                }
                if device
                else None
            ),
            "target": (
                {
                    "key": target.key,
                    "label": target.label,
                    "port": target.port,
                    "role": target.role,
                    "boundary": target.boundary,
                }
                if target
                else None
            ),
            "port": device.port if device else (target.port if target else None),
            "baud": self.baud,
            "session": session,
            # 身份（需求 §6.2）：**设备自报**的字段与**主机**按 build id 匹配出的字典
            # 分成两个键，谁也不冒充谁。
            "identity": {
                "state": self._identity,
                "detail": self._identity_detail,
                "source": self._identity_source,
                "fields": dict(self._identity_fields),
                "build_id32": self._identity_build_id32,
                "responded_utc": self._identity_answered_at,
                "archive": self._identity_archive,
                "matched": bool((self._identity_archive or {}).get("path")),
            },
            # 实时解码（切片 5）：`ready` 才是"这一刻真的在解"，`reason` 说明为什么没有。
            "decode": {
                "ready": self._decoder is not None,
                "reason": self._decoder_reason,
                "dictionary": (self._identity_archive or {}).get("path"),
                "dictionary_origin": (self._identity_archive or {}).get("origin"),
                "lines": self._decoder.lines if self._decoder else 0,
                "token_lines": self._decoder.decoded_lines if self._decoder else 0,
                "app_events": self._decoder.events if self._decoder else 0,
                # 解码器在**本机**能不能用（组合入口提供的探测；没装配就是 false）。
                "decoder_package": self.plugins.decoder_is_available(),
            },
            "last": self._last,
            "rx_error": self._rx_error,
            # 受控测试（ / L2b）：这条会话上那一批的现状与阶段。
            # 没跑过就是 null —— 页面不显示这一块，而不是显示一个空壳。
            "live_tests": self._live_tests_json_locked(),
            "opens": self._opens,
            # 命令框要用到的“设备/项目配置”（需求 §8）：默认行结束符来自设备登记，
            # 不是平台写死的。平台也不提供任何命令建议或白名单。
            "commands": {
                "line_endings": list(LINE_ENDING_CHOICES),
                "line_ending_default": normalize_line_ending(
                    getattr(target, "line_ending", None)
                ),
            },
            # 终端直输的配置（需求 §4.2 / §8）：回车发什么、退格发什么、要不要本地回显，
            # 都属于设备/项目配置。**回车默认与命令框同一个行结束符** —— 一个设置一个真相，
            # 否则“在终端里按回车没反应”这类问题会很难归因（板 A 的固件按 `\n` 收行）。
            "terminal": {
                "enter_ending": normalize_line_ending(getattr(target, "line_ending", None)),
                "backspace": normalize_backspace(getattr(target, "terminal_backspace", None)),
                "backspace_choices": list(BACKSPACE_CHOICES),
                "local_echo": bool(getattr(target, "terminal_local_echo", False)),
                # 哪些按键**本身不产生字节**（只按下修饰键）也由服务端告知：
                # 否则页面会把这些按键当一批发上来，服务端只能 400，
                # 而用户看到的就是一条“发送失败”的提示 —— 其实什么都没错。
                "modifier_keys": sorted(MODIFIER_KEYS),
            },
            # 能力声明（需求 §3.3 的诚实声明）：**从目标的能力表派生**，
            # 不在会话里再写一份；`unknown` 按不支持算。
            "capabilities": self.capabilities(),
            "options": [
                {
                    "key": spec.key,
                    "label": spec.label,
                    "port": spec.port,
                    "role": spec.role,
                    "boundary": spec.boundary,
                    "pipeline_allowed": spec.pipeline_allowed,
                    "line_ending": spec.line_ending,
                }
                for spec in self.registry.specs()
            ],
        }

"""Read-only COM port registry and non-intrusive presence/busy probes.

Nothing in this module opens a port through pyserial. Writing ``dtr``/``rts``
(or letting a driver toggle them) resets an ESP32 dev board, and a reset is an
observation-changing side effect -- see repository issue I-022.

Busy detection therefore uses a raw ``CreateFileW`` probe with zero sharing,
which is the same exclusive-access check pyserial performs on ``open()`` but
without ever touching the modem control lines. ``CreateFileW`` on ``\\.\COMx``
does not assert DTR/RTS; those change only through ``SetCommState`` or
``EscapeCommFunction``, neither of which is called here.

目标层（）：这里的端口名、板子角色与能力声明都属于**参考目标**，不属于平台核心。
`utc_now()` 过去住在这里，现在是 core（`core_primitives`）的**转口**。
"""

from __future__ import annotations

from dataclasses import replace
import socket
import threading
import time
from typing import Any, Callable

try:  # imported as a package (``webconsole.devices``)
    from . import bench_config
    from .core_primitives import utc_now
    from .device_registry import (
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_FREE,
        ACCESS_UNKNOWN,
        DeviceRegistry as _DeviceRegistry,
        DeviceSpec,
    )
    from .target_profile import Capability, Capabilities
    from .target_profile import Capabilities as _Capabilities  # noqa: F401  (兼容旧写法)
    #  / D1：受控嗅探的异常类型住在 core；目标层只负责"怎么把字发出去"。
    from .sniff_service import ExchangeError
except ImportError:  # executed as a plain script
    import bench_config  # type: ignore[no-redef]
    from core_primitives import utc_now  # type: ignore[no-redef]
    from device_registry import (  # type: ignore[no-redef]
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_FREE,
        ACCESS_UNKNOWN,
        DeviceRegistry as _DeviceRegistry,
        DeviceSpec,
    )
    from target_profile import Capability, Capabilities  # type: ignore[no-redef]
    from target_profile import Capabilities as _Capabilities  # type: ignore[no-redef]  # noqa: F401
    from sniff_service import ExchangeError  # type: ignore[no-redef]

try:  # pragma: no cover - exercised implicitly on the Windows host
    import serial.tools.list_ports as _list_ports
except ImportError:  # pragma: no cover
    _list_ports = None

OPEN_EXISTING = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
# ⚠️ 探测用的**共享模式**：不是 0（详见 `port_access()` 里那段注释）。
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002

#: 进程内把探测**串行化**。
#: COM 口的驱动按独占语义服务 `CreateFileW`，**共享模式参数拦不住它**（实测：改成
#: FILE_SHARE_READ|FILE_SHARE_WRITE 之后，3 线程各 300 次探一个**没人用**的口仍然
#: 报 busy 600/900）⇒ 唯一可靠的做法是“同一瞬间只让一个探测进去”。
#: 这个锁盖住**所有**直接调用者（登记表的 `access()` 与 `第二目标` 自己的探针）。
_PROBE_LOCK = threading.Lock()

_win32: Any = None
if _list_ports is not None:  # only meaningful where pyserial is importable
    try:
        import ctypes
        from ctypes import wintypes

        _win32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _win32.CreateFileW.restype = wintypes.HANDLE
        _win32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        _win32.CloseHandle.argtypes = [wintypes.HANDLE]
        _INVALID_HANDLE = ctypes.c_void_p(-1).value
    except (AttributeError, OSError):  # pragma: no cover - non-Windows host
        _win32 = None


# `utc_now()` 如今住在 core（`core_primitives`），这里只**转口**：
# 老调用方（`from .devices import utc_now`）一个都不用改，而 core 模块不再为了
# 一个时间戳反向依赖目标层（ / R2a）。契约测试用 `is` 钉住两者是同一个函数，
# 防止又冒出一份副本（`console_lock.py` 里那份就是从它抄过去的）。


def access_from_open_error(error: int) -> str:
    """把 `CreateFileW` 的失败码翻译成占用结论（纯函数，便于无硬件单测）。

    - `ERROR_ACCESS_DENIED`(5) / `ERROR_SHARING_VIOLATION`(32) ⇒ `busy`。
      真正**独占**持有这个口的进程（pyserial 就是零共享打开的）会让我们撞上这两个之一；
      而 32 以前被归到"其它错误"里 → 最后报 `absent`，等于把"有人在用"说成"口不存在"
      （ 里第二种错报）。
    - 其它错误码 ⇒ `""`（空串 = 不是占用）：交给调用者按"瞬态/真的没有"处理。
    """
    if error in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION):
        return ACCESS_BUSY
    return ""


def port_access(port: str) -> str:
    """Return ``free`` / ``busy`` / ``absent`` / ``unknown`` for one COM port.

    ⚠️ B6：端口可能是空串（这台机器还没写台架文件）⇒ 如实给 `unknown`，
    不拿 `\\\\.\\` 去探一个空名字（那会得到一个看着像真的 `absent`）。
    ⚠️ 端口名也可以写成 **pyserial 的 URL**（`loop://` / `socket://主机:端口`）：
    那一类走 `probe_url()`，不拿 `CreateFileW` 去问（它们根本不是 COM 口）。
    ⚠️⚠️ **不再用零共享打开**（ / ）：零共享意味着"**我自己的两次探测**
    也会互相判成被别人占用"——实测 3 线程各 300 次探同一个**没人用**的口 ⇒ `busy` 600/900；
    而页面每 2 秒就把所有设备探一遍（`/api/state`），precheck 在另一个线程也探同一个口
    ⇒ 撞上就报"COM<n> 已被其他进程占用"，**而那个口是空的**（用户被绕了一圈）。

    改成 `FILE_SHARE_READ|FILE_SHARE_WRITE` 之后：真正**独占**持有它的进程（pyserial 就是
    零共享打开的）依旧会把我们拒掉 —— 拒绝可能是 `ERROR_ACCESS_DENIED`（5）或
    `ERROR_SHARING_VIOLATION`（32），**两个都算 busy**（以前 32 会被当成 `absent`，
    那是另一种错报：把"有人在用"说成"这个口不存在"）。

    还剩一种情形会漏判：对方也**带着共享**打开了这个口（那种情况下我们真能打开它）。
    宁可漏判成 `free` —— 因为**假阳性**的代价是"一次本来能跑的 run 被拒"，
    而**假阴性**的代价是一个真正的冲突在那一次被设备/驱动自己拦下。

    ⚠️ 真正把假阳性去掉的是 `_PROBE_LOCK`（见上面的说明）：共享模式参数对 COM 口无效，
    “同一瞬间只让一个探测进去”才行。
    """
    if not str(port or "").strip():
        return ACCESS_UNKNOWN
    name = str(port)
    if "://" in name:
        return probe_url(name)
    if _win32 is None:
        return ACCESS_UNKNOWN
    with _PROBE_LOCK:
        return _probe_once(name)


def probe_url(name: str) -> str:
    """**URL 形式的端口**（`loop://` / `socket://主机:端口` / `spy://`）怎么判占用。

    ⚠️ 它们不是 COM 口，`CreateFileW` 那一套对它们没有意义 —— 拿它去探一个 URL，
    只会把**正在跑的虚拟板**判成“不存在”，于是卡片根本不出来（实测踩到）。所以：

    - 能按这个 URL 打开 ⇒ `free`；
    - `socket://` 连不上 ⇒ `absent`（对端没在跑）；连得上却打不开 ⇒ `busy`；
    - 其它 scheme（`spy://` / `rfc2217://` …）打不开 ⇒ `unknown`（**不猜**）。
    """
    try:
        import serial  # noqa: PLC0415 - 只有真的要开端口时才需要它
    except ImportError:
        return ACCESS_UNKNOWN
    try:
        handle = serial.serial_for_url(name, do_not_open=False, timeout=0.2)
    except Exception:  # noqa: BLE001 - 打不开：再分一次"是对端没在，还是别的"
        return _url_unreachable(name)
    try:
        handle.close()
    except Exception:  # noqa: BLE001 - 关不掉不影响结论（会随进程释放）
        pass
    return ACCESS_FREE


def _url_unreachable(name: str) -> str:
    if not name.lower().startswith("socket://"):
        return ACCESS_UNKNOWN
    host, _, port_text = name[len("socket://"):].rpartition(":")
    try:
        port_number = int(port_text)
    except ValueError:
        return ACCESS_UNKNOWN
    try:
        with socket.create_connection((host or "127.0.0.1", port_number), timeout=0.5):
            pass
    except OSError:
        return ACCESS_ABSENT
    return ACCESS_BUSY


def _probe_once(port: str) -> str:
    """真正的那一下（调用者必须已经拿着 `_PROBE_LOCK`）。"""
    for attempt in range(3):
        handle = _win32.CreateFileW(
            f"\\\\.\\{port}",
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle != _INVALID_HANDLE:
            _win32.CloseHandle(handle)
            return ACCESS_FREE
        verdict = access_from_open_error(_win32.GetLastError())
        if verdict:
            return verdict
        # 其它错误（口不存在 / 驱动在重绑）等一下子再试：那一下往往是瞬态的
        if attempt < 2:
            time.sleep(0.02)
    return ACCESS_ABSENT


def port_description(port: str) -> str:
    if _list_ports is None:
        return ""
    for info in _list_ports.comports():
        if info.device and info.device.upper() == port.upper():
            return info.description or ""
    return ""


def system_ports() -> tuple[tuple[str, str], ...]:
    """**系统串口枚举**（`(口名, 描述)`，保序）。

    这是“零配置也能用”的**基础候选源**（需求 §2/§6.11）：没有 Adapter、没有
    `bench.local.json` 时，使用者仍然看得到本机有哪些口、能选一个连上去当串口终端用。
    `bench.local.json` 只**增强**（别名、默认参数、已适配目标绑定），**不是前置条件**。

    ⚠️ 没有 pyserial ⇒ 空元组（**如实说“枚举不了”**，不编一份出来）。
    """
    if _list_ports is None:
        return ()
    rows: list[tuple[str, str]] = []
    for info in _list_ports.comports():
        name = str(getattr(info, "device", "") or "").strip()
        if name:
            rows.append((name, str(getattr(info, "description", "") or "")))
    return tuple(rows)


def serial_settings_note() -> str:
    """基础串口依赖能不能用（空串 = 用不了）—— Doctor 与页面都读这一句。"""
    return pyserial_note()


def open_read_only(port_name: str, baud: int) -> Any:
    """打开一个串口并**只**设置端口/波特率/超时（适配器 `serial.read-only-open`）。

    ⚠️ 刻意不赋 `dtr` / `rts`：这两个属性一赋值就会经过板上的自动复位电路
    （I-022 的教训， 是它的第二次现身）。"打开端口"必须是零副作用的。

     / R2b：这段实现过去住在 core 的 `observe.default_port_factory`
    —— core 里唯一一处平台实现（边界清单 §6）。现在它在目标层，core 通过
    `platform_plugins.PlatformPlugins.port_factory` 拿到它。
    """
    if not str(port_name or "").strip():
        # ⚠️ B6：直接开 `""` 只会得到一句 pyserial 的怪话（“could not open port”），
        # 人看不出是“没配置”。所以在这里就说清是什么、去哪补。
        raise bench_config.NotConfigured("串口（台架键 devices.<设备>.port）")
    import serial  # noqa: PLC0415 - 只有真的要开串口时才需要它

    # ⚠️ 端口名允许写成 **pyserial 的 URL**（`loop://` / `socket://主机:端口` / `spy://`）：
    #    写 URL 就走 `serial_for_url`，普通端口名（本机那些串口）一个字都不变。
    #    这条口子让"没有这块板也把真实串口栈跑一遍"成为可能 —— `loop://` 把写出去的字节
    #    原样回显，`socket://` 可以连一台**虚拟板**（自己写的小服务）。它不是替身：
    #    打开/写/读/关、超时与失败回执全部走 pyserial 的真路径。
    name = str(port_name)
    if "://" in name:
        port = serial.serial_for_url(name, do_not_open=True)
    else:
        port = serial.Serial()
        port.port = name
    port.baudrate = baud
    # ⚠️ 读超时**不在这里定**：它属于读取策略（`core_primitives.READ_TIMEOUT_S`），
    #    由要读的那一层（实时观察 / run 的读循环）打开端口后设。这里写一个值就会
    #    变成第二份真相 —— 实测过的后果：“安静的尾巴”晚 100 ms 才显示（W6）。
    return port


def open_for_exchange(port: Any) -> None:
    """Open a pyserial handle without racing this process's presence probe.

    Windows COM drivers commonly ignore the requested sharing flags.  A concurrent
    ``CreateFileW`` presence check can therefore make our own real ``open()`` fail with
    ``ERROR_ACCESS_DENIED`` even though no external program owns the port.
    """
    with _PROBE_LOCK:
        port.open()


def pyserial_note() -> str:
    """本机串口实现能不能用：`pyserial x.y`，或者**空串 = 不可用**。

    ⚠️  / R4：这段体检以前写在编排的 precheck 里（`import serial`）——
    "这台机器的串口实现装没装"是 **transport 实现**的知识，所以它跟着
    `port_factory` 一起经插件束交进去（编排只把这句话转给用户）。
    """
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        return ""
    return f"pyserial {serial.__version__}"


def sniff_exchange(
    port_name: str, baud: int, payload: bytes, max_bytes: int, wait_s: float
) -> bytes:
    """受控的一问一答（ / D1 的传输实现）：打开 → 写 → 读到期限 → 关。

    它是 `sniff_service.SniffExecutor` 的第四个注入点（"怎么把字发出去"）。

    - ⚠️ **打开时不赋 `dtr` / `rts`**：赋它们会经过板上的自动复位电路
      （I-022 /  两次实测的教训）。
    - ⚠️ 只保留"我们问了之后"的字节（先清空输入缓冲）：不清的话，打开之前的残留
      会被当成这次询问的回答 —— 那是"看着合理"的错误。
    - ⚠️ 失败一律抛 `ExchangeError`：core 只把它记成 `open_failed`，不猜原因。
    - ⚠️ **只问一条**：不自动重试、不试第二个波特率、不复位、不清设备数据。
    """
    name = str(port_name or "").strip()
    if not name:
        raise ExchangeError("没有配置通信口（台架键 devices.<设备>.port）")
    try:
        port = open_read_only(name, baud)
    except bench_config.NotConfigured as error:
        raise ExchangeError(str(error)) from None
    except Exception as error:  # pyserial 的打开失败
        raise ExchangeError(f"打不开 {name}：{error}") from None
    try:
        port.timeout = 0.05
        open_for_exchange(port)
        try:
            port.reset_input_buffer()
        except Exception:  # noqa: BLE001 - 清不掉不是致命：下面照常读
            pass
        port.write(bytes(payload))
        port.flush()
        limit = max(1, int(max_bytes))
        deadline = time.monotonic() + max(0.0, float(wait_s))
        chunks: list[bytes] = []
        got = 0
        while got < limit and time.monotonic() < deadline:
            data = port.read(min(256, limit - got))
            if data:
                chunks.append(bytes(data))
                got += len(data)
        return b"".join(chunks)
    except Exception as error:  # noqa: BLE001 - 读写失败也要变成一句可读的理由
        raise ExchangeError(f"{name} 读写失败：{error}") from None
    finally:
        try:
            port.close()
        except Exception:  # noqa: BLE001 - 关不掉不影响结论（端口会随进程释放）
            pass


# `DeviceSpec` / `DeviceRegistry` / `ACCESS_*` 现在住在 core（`device_registry`），
# 本模块只**转口**它们（老调用方 `from .devices import DeviceSpec` 一个都不用改），
# 并把参考目标的默认值接上去：`DEVICES` 是登记内容，`port_access` / `port_description`
# 是这台机器上的探测实现。于是 core 不再 import `devices`（ / R2b）。

#: 参考目标（平台的第一块 DUT）的能力声明 —— **能力的唯一声明处**。
#: 两块板声明的是同一张表：差别在 `pipeline_allowed`（平台**策略**：第一版不允许
#: 构建/烧录板 B），不是"板 B 做不到"。把"不允许"写成"不支持"，
#: 下一个人就会去找一个不存在的技术障碍。
REFERENCE_TARGET_CAPABILITIES = Capabilities.of(
    build=Capability.SUPPORTED,
    flash=Capability.SUPPORTED,
    reset=Capability.SUPPORTED,       # 只有显式请求才脉冲（见 TargetProfile.reset）
    transport=Capability.SUPPORTED,
    symbolize=Capability.SUPPORTED,
    decode=Capability.SUPPORTED,      # 平台解码器 + 按构建号精确选库
    identity=Capability.SUPPORTED,    # @PWINFO，旧设备回退明文横幅
    evidence=Capability.SUPPORTED,    # raw.bin + io_events.jsonl + 会话清单
    metrics=Capability.SUPPORTED,     # schema 由项目包提供
    # ：加密存储这一项**要明确表态**（参考目标这一版没有它）。
    # 判据盯着"参考目标不许留给未来"（`the_reference_target_does_not_leave_anything_unstated`）：
    # 没提的能力是 `unknown`，而"没提"会被下一个人读成"也许有"。
    storage_encryption=Capability.UNSUPPORTED,
)


#: **同一份源码的第二种日志能力**（）：`-DPW_TOKEN_LOG=0` 那份构建。
#: 与上面那张表只差一项 —— `decode` 从 supported 变 unsupported。
#:
#: ⚠️ 这不是"解码失败"，是"这份固件里根本没有可解码的帧"：它不编译 Workbench
#:    Token Log，也不生成字典。其余能力一项不少（构建/烧录/复位/传输/身份/证据/指标），
#:    所以"关闭 Token Log 不得关闭身份、原始采集与自动测试协议"不是靠自觉 ——
#:    它由这张表说了算，Core 只按能力办事。
#: ⚠️ 从 `REFERENCE_TARGET_CAPABILITIES` 派生而不是重写一遍：两张表只差一行，
#:    以后加一项能力不会只在其中一张表里生效。
NO_TOKEN_LOG_CAPABILITIES = Capabilities.of(
    **{
        **REFERENCE_TARGET_CAPABILITIES.declared,
        "decode": Capability.UNSUPPORTED,
    }
)


# Board B is intentionally registered but read-only for the MVP: it must not be
# flashed or captured until an independent-observer test case exists.
#: ⚠️ B6：两块的端口**不在代码里**（过去是 `COM<n>` / `COM<n>`）—— 它们住在台架文件的
#: `devices.a.port` / `devices.b.port`；没配置就是空串（台面上显示“未配置”，开的时候会说清楚）。
DEVICES: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        key="a",
        label="板 A",
        port="",
        role="demo DUT",
        pipeline_allowed=True,
        boundary="可构建 / 烧录 / 采集",
        line_ending="lf",
        capabilities=REFERENCE_TARGET_CAPABILITIES,
    ),
    DeviceSpec(
        key="b",
        label="板 B",
        port="",
        role="已登记 Tester",
        pipeline_allowed=False,
        boundary="第一版只显示状态，不烧录、不采集",
        line_ending="crlf",
        capabilities=REFERENCE_TARGET_CAPABILITIES,
    ),
)


class DeviceRegistry(_DeviceRegistry):
    """参考目标的登记表：默认接上零共享探测与端口描述（core 只认接口）。

     / R2b：容器与四个状态常量现在住在 core（`device_registry`），
    这里只把默认值接到**目标层**的实现上 —— 于是 `observe`（core）不再 import
    `devices`，而老调用方（`pipeline` / `server` / 测试）写 ``DeviceRegistry()``
    拿到的仍然是带真实探测的那一份。
    """

    def __init__(
        self,
        specs: tuple[DeviceSpec, ...] | None = None,
        *,
        probe: Callable[[str], str] | None = None,
        describe: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(
            bench_specs() if specs is None else specs,
            probe=probe or port_access,
            describe=describe or port_description,
        )


def registry(specs: tuple[DeviceSpec, ...] | None = None) -> DeviceRegistry:
    """参考目标的登记表（装配处用这一个，别各自 new 一份）。"""
    return DeviceRegistry(specs)


def bench_specs() -> tuple[DeviceSpec, ...]:
    """登记表 + **台架层**的端口（ / B2、B6）。

    `DEVICES` 里**已经没有端口默认值**（B6 删了「原开发机的 COM<n>/COM<n>」）；每一项的
    `port` 过一遍台架取值梯子（命令行 > 环境变量 > 台架文件）。**没配置就是空串**
    —— 调用方会把它报成“未配置”，而不是拿一个别的机器上的口去开。

    ⚠️ 端口不能在 import 时就算好：台架文件是入口 `main()` 装好之后才读得到的。
    """
    rows: list[DeviceSpec] = []
    for spec in DEVICES:
        value = bench_config.active().device_port(spec.key, default=spec.port)
        rows.append(spec if value.value == spec.port else replace(spec, port=value.value))
    return tuple(rows)

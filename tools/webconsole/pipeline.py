"""The run state machine: the only owner of PlatformIO and the COM port.

The browser never touches the serial port. It POSTs an intent, and this module
executes ``precheck -> build -> upload -> observe -> decode -> assert`` in a
worker thread while publishing every event to the UI.

Safety boundaries implemented here:

- only one run at a time, so two writers can never race for one COM port;
- only a target the registry marks ``pipeline_allowed`` may start a pipeline;
- the serial port is opened without ever assigning ``dtr``/``rts`` unless the
  run explicitly requested a reset pulse (repository issue I-022);
- fixed argv lists only -- no shell string is ever built from user input;
- cancelled or failed runs still leave a complete evidence directory.

⚠️ ** / R4：这一层是 core 的编排，不是参考目标的代码。** 它只做三件事：
“什么时候做、怎么记账、失败时说什么”。具体怎么构建/烧录/复位/符号化来自**目标适配器**，
项目的业务（事件/指标/判据/套件/采集窗口/与设备的固定对话）来自**项目包**，
“这台机器上怎么探串口”来自装配处交进来的插件束 —— 本模块不 import 任何一个。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import queue
from typing import Any, Callable, Iterator, Mapping

try:  # imported as a package (``webconsole.pipeline``)
    from . import assertion_groups
    from . import device_dialogue
    from . import device_registry
    #  / D4：烧录后的身份闭环要用的几样（都是 core）。
    from . import console_input
    from . import device_sniff as sniff_mod
    from . import identity
    from . import sniff_service
    from . import platform_plugins
    from . import project_package
    from . import target_adapters as adapters_mod
    from .core_primitives import (
        idle_partial, partial_idle_s, split_complete_lines, utc_now, visible_line_remainder,
    )
    from .core_primitives import READ_TIMEOUT_S
    from .host_scenario_runner import execute_image_roundtrip
    from .embedded_test_model import TestSelection
    from .embedded_test_runner import TestRunError, execute_device_tests
    from . import step_lifecycle
    from . import large_evidence
    from . import runstore
    from .runstore import RunStore
except ImportError:  # executed as a plain script
    import assertion_groups  # type: ignore[no-redef]
    import device_dialogue  # type: ignore[no-redef]
    import device_registry  # type: ignore[no-redef]
    import console_input  # type: ignore[no-redef]
    import device_sniff as sniff_mod  # type: ignore[no-redef]
    import identity  # type: ignore[no-redef]
    import sniff_service  # type: ignore[no-redef]
    import platform_plugins  # type: ignore[no-redef]
    import project_package  # type: ignore[no-redef]
    import target_adapters as adapters_mod  # type: ignore[no-redef]
    from core_primitives import (  # type: ignore[no-redef]
        idle_partial, partial_idle_s, split_complete_lines, utc_now, visible_line_remainder,
    )
    from core_primitives import READ_TIMEOUT_S  # type: ignore[no-redef]
    from host_scenario_runner import execute_image_roundtrip  # type: ignore[no-redef]
    from embedded_test_model import TestSelection  # type: ignore[no-redef]
    from embedded_test_runner import TestRunError, execute_device_tests  # type: ignore[no-redef]
    import step_lifecycle  # type: ignore[no-redef]
    import large_evidence  # type: ignore[no-redef]
    import runstore  # type: ignore[no-redef]
    from runstore import RunStore  # type: ignore[no-redef]

def _recorded_hashes(report: dict[str, Any]) -> dict[str, str]:
    """`report.json` 里**已经记下来**的产物哈希（`evidence` 索引，run 收尾时写入）。

    ⚠️ 详情首屏**不现算**哈希（PERF-HIST-07）：这份索引是 run 当时留下来的记录，
    它可以是"未记录"（旧 run），但绡不能是"刚刚为了显示摘要算出来的"。
    两种形状都认：`{name: {sha256: …}}`（现在的）与 `[{name:…, sha256:…}]`（防御）。
    """
    index = report.get("evidence")
    out: dict[str, str] = {}
    if isinstance(index, dict):
        for name, row in index.items():
            value = row.get("sha256") if isinstance(row, dict) else row
            if value:
                out[str(name)] = str(value)
    elif isinstance(index, list):
        for row in index:
            if isinstance(row, dict) and row.get("name") and row.get("sha256"):
                out[str(row["name"])] = str(row["sha256"])
    return out


# ⚠️  / R3e：编排**不再 import 任何项目模块**。
# 项目的记录解析 / 指标 / 断言 / 记录格式 / 基线 / 套件都从装配处交进来的
# `ProjectPackage` 拿（`self.project`）；没装项目包时如实降级（见 `_project`）。

CONSOLE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = CONSOLE_DIR.parent
LOGGER_DIR = TOOLS_DIR.parent
REPO_DIR = LOGGER_DIR.parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
# ⚠️  / R4：这里以前还 import 了 `console_lock`（工作区锁）与 `dictionary`
# （选库/读横幅）—— 两者都是目标层的实现。现在工作区锁由装配处交进来
# （`Pipeline(workspace_lock=…)`），字典库走 `Discovery.library` / 插件束，
# 于是这个模块可以登记回 **core**（编排不再认识任何一个平台实现）。
# ⚠️  / R3b：构建布局（`.pio/build/<env>/logging/…`、分区表、工程文件、平台工具）
# **不在**编排里 —— 那是参考工程的结构知识，现在由目标适配器给（`Discovery`）。
# 编排只问 `self.discovery`（见 `_adapter("discovery")`），不再自己拼路径。

SERIAL_BAUD = 115200

# ⚠️ token domain / 套件名 / 采集窗口 / 与设备的固定对话**都不在这里**：
# 它们是**项目包的声明**（ / R4，）—— 见 `Pipeline.token_domain`、
# `Pipeline.suites`、`Pipeline._observe_seconds_for()` 与 `Pipeline._dialogue()`。
# 把参考项目的那些常量留在编排里，等于“换一个项目就要改编排”。

STEP_ORDER = ("precheck", "build", "upload", "observe", "decode", "assert")

MIN_OBSERVE_SECONDS = 5.0
#: 采集窗口的上限。2026-10-09（用户口径）：**不缩短有效测试窗口** ——
#: 受控序列跑完（含两次重启）本来就要几分钟，60 s 会把它压在边界上
#: （ 的"边界截断"正是这么来的）。上限改为 240 s；
#: ⚠️ 同时页面必须显示**实际生效值**与进度，否则长窗口会被误判成卡死（）。
MAX_OBSERVE_SECONDS = 240.0

#: 项目包**没有**声明采集窗口时用的保守值。它**不代表任何项目的取样节奏**：
#: 谁想让窗口有意义（例如“必须覆盖一个记录周期”），就由那个项目自己声明
#: （`ProjectPackage.default_observe_seconds` / `observe_seconds_by_suite`）。
NEUTRAL_OBSERVE_SECONDS = 20.0

# 赋 DTR 与赋 RTS 都会复位一次（）。复位时序本身现在是**目标适配器给的数据**
# （`ResetPlan`， / R3c）：先只动 DTR → 等 → 丢掉 → 再单独脉冲 RTS。
# 那两次之间的静默时间（`RESET_SETTLE_SECONDS`）也搬去了目标层。

# ⚠️ “等启动横幅 / 清记录 / 对时 / 取记录文件”这几段固定对话**不在编排里**：
# 它们是 `device_dialogue.DeviceDialogue`（项目包声明），编排只负责按开关执行它们
# （ / R4，）。

# ⚠️  / R4：尺寸行的正则（`SIZE_LINE`）与 `firmware.bin` / `firmware.elf` 的相对路径
# 已搬到**目标层**（`参考目标.BUILD_SIZE_PATTERN` + `Discovery.{elf,binary,size_pattern}`）——
# 编排不再知道参考工具的输出格式与产物名（）。


class RequestError(Exception):
    """The request itself is invalid (HTTP 400/403/409)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class StepFailure(Exception):
    """A pipeline step ran but did not meet its acceptance criteria."""


class RunCancelled(Exception):
    """The user cancelled the run."""


# ⚠️  / R3b：`_platformio_scripts_dir()` / `PLATFORMIO_EXE` / `PIO_PYTHON_EXE`
# 已搬到 `参考目标`（工具在哪、工程怎么摆，都是参考目标的知识）。
# 编排要工具路径时问 `self.discovery.tool`。


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", *args],
            cwd=str(REPO_DIR),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    # ⚠️ ：**不要 `text=True`** —— 本平台用 `python -X utf8` 跑，严格 UTF-8 解码一旦
    #    碰上工具按 OEM 代码页（中文 Windows 是 GBK）打的字，会**直接打死读取线程**，
    #    于是这里静默变成一个空字符串。拿字节 + 宽松解码（与读取证据那条链同一个惯例）。
    return (result.stdout or b"").decode("utf-8", "replace").strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client_label(client: Any) -> str:
    """Human-readable name of whoever issued a control request."""
    if isinstance(client, dict):
        label = str(client.get("label") or "").strip()
        identifier = str(client.get("id") or "").strip()
        if label:
            return f"{label}（{identifier}）" if identifier else label
        if identifier:
            return f"客户端 {identifier}"
    return "未知客户端"


def _normalize_client(client: Any) -> dict[str, str]:
    if not isinstance(client, dict):
        return {"id": "", "label": ""}
    return {
        "id": str(client.get("id") or "")[:32],
        "label": str(client.get("label") or "")[:64],
    }


IDENTITY_RETRY_GAP_SECONDS = 4.0


def _build_id_of(block: Any) -> int | None:
    """ / D4：从构建自述里取 32 位构建号（`build_id32` / `build_id`）。

    `None` = 这一趟的构建证据里**没有**可比的构建号 ⇒ 让它退出比较，
    而不是拿一个假值去比（那样“核验通过”就永远为真了）。
    """
    if not isinstance(block, dict):
        return None
    for key in ("build_id32", "build_id"):
        raw = block.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw & 0xFFFFFFFF
        text = str(raw).strip()
        try:
            base = 16 if text.lower().startswith("0x") else 10
            return int(text, base) & 0xFFFFFFFF
        except ValueError:
            continue
    return None


def _elapsed_ms(started_utc: str | None, finished_utc: str | None) -> int | None:
    """Milliseconds between two UTC ISO-8601 stamps.

    Both stamps carry their own offset, so this is independent of the machine's
    timezone and DST state (the previous ``time.mktime`` version was not).
    """
    if not started_utc or not finished_utc:
        return None
    try:
        started = datetime.fromisoformat(started_utc.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _iter_lines(fileno: int, chunk_size: int = 4096) -> Iterator[str]:
    """Yield lines from a pipe fd, splitting on both CR and LF.

    ``os.read`` is used instead of ``BufferedReader.read`` so that a partially
    filled buffer is delivered immediately and the UI stays live.
    """
    buffer = b""
    while True:
        chunk = os.read(fileno, chunk_size)
        if not chunk:
            break
        buffer += chunk
        while True:
            index = min(
                (idx for idx in (buffer.find(b"\n"), buffer.find(b"\r")) if idx >= 0),
                default=-1,
            )
            if index < 0:
                break
            yield buffer[:index].decode("utf-8", "replace")
            buffer = buffer[index + 1:]
        if len(buffer) > 1 << 20:  # a runaway "line" must not eat memory
            yield buffer.decode("utf-8", "replace")
            buffer = b""
    if buffer:
        yield buffer.decode("utf-8", "replace")


# `split_complete_lines()` 已搬进 core（`core_primitives`）并在这里转口：
# run 的 `_LineCollector` 与实时观察必须用**同一份**行切分实现，
# 但那一份实现不该住在 PlatformIO 编排模块里（ / R2a）。


class _LineCollector:
    """Split serial bytes into lines and fan them out to the UI panes."""

    def __init__(
        self,
        emit_raw: Callable[..., None],
        emit_decoded: Callable[[str], None],
        decode_line: Callable[[bytes], bytes],
        limit: int = 8192,
        sink: Any = None,
    ) -> None:
        self._buffer = bytearray()
        self._emit_raw = emit_raw
        self._emit_decoded = emit_decoded
        self._decode_line = decode_line
        self._limit = limit
        # Optional durability sink: gets the raw line before it is interpreted.
        self._sink = sink
        # 这一行的前多少字节已经当"半行"推给窗口了（见 `idle_flush`）。
        self._shown = 0
        self._last_rx_mono: float | None = None
        # 阈值随波特率放宽（慢波特率下 30 ms 盖不住一行，会把行切开）。
        self._partial_idle_s = partial_idle_s(SERIAL_BAUD)

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        self._last_rx_mono = time.monotonic()
        for line in split_complete_lines(self._buffer):
            shown, self._shown = self._shown, 0
            self._emit(line, shown=shown)
        if len(self._buffer) > self._limit:
            self.flush()

    def idle_flush(self) -> bool:
        """这一秒没有新字节 ⇒ 把"半行"（还没等到换行的尾巴）照实显示出来。

        与实时观察的 `_flush_idle_tail` 是**同一条规则**（`core_primitives.idle_partial`）：
        同一份字节流不能在两条路上被切成两种样子。返回是否真的交出去了一段。
        """
        if self._last_rx_mono is None:
            return False
        tail = idle_partial(
            self._buffer, shown=self._shown, idle_s=time.monotonic() - self._last_rx_mono,
            threshold_s=self._partial_idle_s,
        )
        if not tail:
            return False
        self._shown += len(tail)
        self._emit_raw(
            tail.decode("utf-8", "replace").rstrip("\r\n"),
            continuation=self._shown > len(tail),
        )
        return True

    def flush(self) -> None:
        if not self._buffer:
            return
        line = bytes(self._buffer)
        shown, self._shown = self._shown, 0
        self._buffer.clear()
        self._last_rx_mono = None
        self._emit(line, shown=shown)

    def _emit(self, line: bytes, *, shown: int = 0) -> None:
        if self._sink is not None:
            self._sink.raw_line(line)
        remainder = visible_line_remainder(line, shown)
        if remainder:             # 纯 CR/LF 只闭合已显示的半行，不另画空行
            self._emit_raw(
                remainder.decode("utf-8", "replace").rstrip("\r\n"),
                continuation=shown > 0,
            )
        try:
            decoded = self._decode_line(line)
        except Exception:  # decoding must never break the capture
            self._emit_decoded(line.decode("utf-8", "replace").rstrip("\r\n"))
            return
        self._emit_decoded(decoded.decode("utf-8", "replace").rstrip("\r\n"))


class CaptureSink:
    """Stream a capture into the run directory as lines arrive.

    The observation window used to buffer everything in memory and write ``raw.bin``
    in a ``finally`` block. That is fine until the process dies -- ``taskkill``, a
    crash, a power cut -- and then the run has no bytes at all, which is exactly the
    run whose evidence is worth most. Now every complete line is appended and
    flushed as it is seen.

    ``decoded.log`` written here is a *best-effort live view*: the decode step
    re-derives it from ``raw.bin``, so this file is only the fallback when the run
    never reaches that step. Both facts are encoded in the artifact descriptions
    (``runstore.EVIDENCE_PURPOSE``).

    Kept separate from the port loop so it can be tested without hardware.
    """

    def __init__(self, store: Any, run_id: str, *, decoded: bool = True, cursor: Any = None) -> None:
        self._store = store
        self._raw = store.open_raw_sink(run_id)
        self._decoded = store.open_decoded_sink(run_id) if decoded else None
        #: 四类证据的行号游标（T5）。`None` = 没有游标（纯测试夹具），照旧写文件只是不记引用。
        self._cursor = cursor
        self.bytes = 0
        self.lines = 0
        self.closed = False

    def raw_line(self, line: bytes) -> None:
        self.bytes += len(line)
        self.lines += 1
        self._raw.write(line)
        self._raw.flush()
        if self._cursor is not None:
            self._cursor.advance("raw")

    def decoded_line(self, text: str) -> None:
        if self._decoded is None:
            return
        self._decoded.write(text if text.endswith("\n") else text + "\n")
        self._decoded.flush()
        if self._cursor is not None:
            self._cursor.advance("decoded")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for handle in (self._raw, self._decoded):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass


class _CapturedLineTransport:
    """Line protocol over the Web-owned port while preserving run evidence."""

    def __init__(self, port: Any, collector: _LineCollector, pipeline: Any, run_id: str) -> None:
        self._port = port
        self._collector = collector
        self._pipeline = pipeline
        self._run_id = run_id
        self._buffer = bytearray()
        self._pending: deque[str] = deque()

    def write_line(self, line: str) -> None:
        time.sleep(0.2)
        self._port.write((line + "\r\n").encode("ascii"))
        self._port.flush()
        shown = f"> {line}"
        self._pipeline.store.append_tool(self._run_id, shown)
        # tool.log 也是一类证据（T5）：写成功之后才移游标。
        cursor = getattr(self._pipeline, "_evidence_cursor", None)
        if cursor is not None:
            cursor.advance("tool")
        self._pipeline._emit("tool", step="observe", line=shown)

    def read_line(self, timeout_s: float) -> str | None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if self._pending:
                return self._pending.popleft()
            chunk = self._port.read(self._port.in_waiting or 1)
            if not chunk:
                self._collector.idle_flush()
                continue
            self._collector.feed(chunk)
            self._buffer.extend(chunk)
            for raw in split_complete_lines(self._buffer):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._pending.append(text)
        return self._pending.popleft() if self._pending else None


#: 重放（新页面/新订阅者第一次连上 SSE）的**三条上限**（）。
#:
#: 为什么要有上限：积压是**进程级**的（下面那个 `deque(maxlen=5000)`），它不认会话、
#: 不认时间 —— 上一次会话断开之后，它的尾巴还躺在里面。于是"新页面连上来"=整包推下去：
#: ① 那些行带着 `origin="live"` 的路标 ⇒ 落进**实时观察**那一屏（未连接却在逐行出数据）；
#: ② 一帧 `app_events` 曾经带整份指标序列（实测单帧 6.8 MB）⇒ 整包重放几百 MB，
#: 页面被拖成逐行慢放，看起来更像"实时输出"。
REPLAY_EVENT_LIMIT = 5000            # 条数：不超过环形缓冲本身的大小
REPLAY_BYTE_BUDGET = 2 * 1024 * 1024  # 字节：一次重放最多给这么多
REPLAY_EVENT_MAX_BYTES = 256 * 1024  # 单帧：超过它就不进重放（派生物去证据里取）


class EventBus:
    """A process-local event stream shared by independently assembled pipelines."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.listeners: set[queue.Queue] = set()
        self.backlog: deque[dict[str, Any]] = deque(maxlen=5000)
        self.seq = 0
        #: 订阅者跟不上时被挤掉的条数（**按订阅者记**）。
        #: ⚠️ 以前这里是 `except queue.Full: pass` —— 一个慢客户端（页面在批处理、
        #: 或者干脆开着标签页去泡咖啡）会**静默地**丢掉事件，而页面看起来一切正常。
        #: "丢了一点"与"本来就没有"长得一模一样，所以这条必须记账并说得出口
        #: （ / L5：UI 降载不许冒充证据丢失，反过来也不许把丢失咽下去）。
        self.dropped: dict[queue.Queue, int] = {}

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.listeners.add(channel)
            self.dropped[channel] = 0
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self.lock:
            self.listeners.discard(channel)
            self.dropped.pop(channel, None)

    def stats(self) -> dict[str, Any]:
        """广播的自述：**序号水位 / 积压 / 订阅者进度 / 因为跟不上而丢掉的条数**。

        页面拿 `seq` 与自己的游标比，就知道自己落后多少条；拿 `dropped` 就知道
        有没有东西是自己**根本没见过**的（这时候页面必须说出来，不许装成没发生）。
        """
        with self.lock:
            pending = [channel.qsize() for channel in self.listeners]
            return {
                "seq": int(self.seq),
                "backlog": len(self.backlog),
                "listeners": len(self.listeners),
                "pending": max(pending) if pending else 0,
                "dropped": sum(self.dropped.values()),
            }

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.backlog if event["seq"] > seq]

    def replay_window(
        self,
        since: int,
        *,
        byte_budget: int = REPLAY_BYTE_BUDGET,
        event_max_bytes: int = REPLAY_EVENT_MAX_BYTES,
        limit: int = REPLAY_EVENT_LIMIT,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """新订阅者该收到的**那一段**（有界），外加"少给了多少"的自述（）。

        三条规则都能单独说清：
          1. **从最新往回取**：新页面要的是"现在"，不是会话的开头（开头在证据文件里），
             所以取最近的这一段，丢掉的永远是**更早**的那些。
          2. **条数 / 字节上限**：攒够就停 —— 至少留最新的一条（空手而归比多给几条更没道理）。
          3. **过大的帧不进重放**：单帧超过 `event_max_bytes` 的（整份指标序列、整批结构化
             事件这种派生物）跳过并记账；它们本来就能从证据文件里取。

        返回的每一条都带 `replay=True`：页面据此把"历史"与"这次新到的"分开 ——
        实时那一屏只认**正在跑的那次会话**的行，旧会话的积压不再冒充实时。
        自述里的 `available/kept/skipped/oversize` 让页面说得出口"我漏了多少"，
        而不是假装拿全了。
        """
        with self.lock:
            candidates = [event for event in self.backlog if event["seq"] > since]
        kept: list[dict[str, Any]] = []
        kept_bytes = 0
        oversize = 0
        for event in reversed(candidates):
            size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
            if size > event_max_bytes:
                oversize += 1
                continue
            if kept and (len(kept) >= limit or kept_bytes + size > byte_budget):
                break
            kept.append(event)
            kept_bytes += size
        kept.reverse()
        report = {
            "available": len(candidates),
            "kept": len(kept),
            "skipped": len(candidates) - len(kept),
            "oversize": oversize,
            "bytes": kept_bytes,
            "byte_budget": int(byte_budget),
            "event_max_bytes": int(event_max_bytes),
            "since": int(since),
            "from_seq": kept[0]["seq"] if kept else None,
            "to_seq": kept[-1]["seq"] if kept else None,
        }
        return [dict(event, replay=True) for event in kept], report

    def publish(self, run_id: str | None, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": utc_now(), "run_id": run_id, "kind": kind}
            event.update(fields)
            self.backlog.append(event)
            listeners = list(self.listeners)
        for channel in listeners:
            try:
                channel.put_nowait(event)
            except queue.Full:
                # 挤掉了就记一笔：这条**不在**这个订阅者手里了。
                with self.lock:
                    self.dropped[channel] = self.dropped.get(channel, 0) + 1
        return event


def resolve_run_parameters(
    project: Any,
    *,
    suite: str = "",
    set_clock: bool | None = None,
    dump_records: bool | None = None,
    clear_records: bool | None = None,
    probe_command: str | None = None,
    project_options: Mapping[str, Any] | None = None,
    declared_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """这一轮真正要用的运行参数（**纯函数**，可单独验）。

    优先级：**显式参数 > 套件默认 > 项目默认 > 中立默认（什么都不做）**。

    ⚠️ 为什么要有这条规则（）：这几件曾是 `Pipeline.start()` 的默认值
    （`set_clock=True` / `dump_records=True` / `probe_command=""`）——
    那是**参考项目（示例项目）固件的事实**，不是平台的。第二个目标走 Web 时没人给它覆盖，
    于是"身份探测命令"是空 ⇒ 明文横幅抓不到 ⇒ 真实烧录+采集都成功的那一轮被 decode
    如实判成 `UNKNOWN_BUILD`（拒绝猜字典是对的，"平台自己决定不问"是错的）。
    """
    if declared_options is None:
        declared_options = project.validate_run_options(project_options)
    declared = dict(declared_options)
    resolver = getattr(project, "run_defaults_for_suite", None)
    if callable(resolver):
        defaults = dict(resolver(suite))
    else:
        defaults = dict(getattr(project, "run_defaults", None) or {})
        suite_defaults = (getattr(project, "suite_run_defaults", None) or {}).get(suite, {})
        defaults.update(dict(suite_defaults or {}))

    def _flag(name: str, explicit: bool | None) -> bool:
        if explicit is not None:
            return bool(explicit)
        if name in defaults:
            return bool(defaults[name])
        return False

    values: dict[str, Any] = {
        "set_clock": _flag("set_clock", set_clock),
        "dump_records": _flag("dump_records", dump_records),
        "clear_records": _flag("clear_records", clear_records),
        "probe_command": (
            str(probe_command)
            if probe_command is not None
            else str(defaults.get("probe_command", "") or "")
        ),
    }
    # 老 CLI 仍走显式参数；Web 走 project_options。统一摊平进 run/manifest，保持历史证据键兼容。
    if project_options is None:
        declared.setdefault("clear_records", values["clear_records"])
    values["clear_records"] = bool(declared.get("clear_records", values["clear_records"]))
    values["project_options"] = declared
    return values


class Pipeline:
    def __init__(
        self,
        project_dir: Path = LOGGER_DIR,
        runs_dir: Path | None = None,
        pio_env: str | None = None,
        adapters: dict[str, Any] | None = None,
        plugins: platform_plugins.PlatformPlugins | None = None,
        platform_facts: Callable[[], dict[str, Any]] | None = None,
        project: project_package.ProjectPackage | None = None,
        firmware_regions: tuple[Any, ...] = (),
        devices: Any = None,
        capabilities: Any = None,
        workspace_lock: Callable[[], dict[str, Any] | None] | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        # Which build environment this pipeline builds and uploads. Empty means
        # "the shipping one" -- and which one that is, the *build adapter* answers
        # (R3b: the orchestrator does not keep a second copy of that name).
        self._pio_env = (pio_env or "").strip()
        # 受控适配器（ / R3a）：**怎么构建/烧录/复位/符号化**由目标层提供，
        # 编排只负责“什么时候做、怎么记账、失败时说什么”。
        # ⚠️  / R4：**没有默认装配** —— 以前这里会延迟 import 参考目标
        # （`参考目标.reference_adapters()`），于是编排里留着一条“认识参考目标”的
        # 后门，core 就登记不了。现在由装配处交进来；没交的适配器在用到时报一件说得清的事。
        self._adapters: dict[str, Any] = dict(adapters) if adapters else {}
        self._discovery: adapters_mod.Discovery | None = None
        # 插件束（ / R4）：core 需要而只有目标/项目才有的那几件
        # （字典库、解码器、串口探测、设备登记）。没装就如实降级。
        self.plugins = plugins or platform_plugins.NO_PLUGINS
        # 平台自述的出处（装配处给的回调， / R3d）：run manifest 也要带上它。
        # 是**公开属性**而不是构造参数：装配处通常先把 pipeline 建出来、再拿它的设备登记
        # 去装插件束，那之后才知道自述是什么（`server.py` / CLI 入口都是这么接的）。
        self.platform_facts = platform_facts
        # 项目包（ / R3e）：装配处把项目那一半交进来；没交就是一份诚实的空
        # （采集与证据照旧，但没有结构化事件/指标/判据）。
        self.project = project if project is not None else project_package.no_project()
        # Firmware Profile owns writable test regions. Core only carries the injected
        # declarations into the generic test runner; it never invents an address.
        self.firmware_regions = tuple(firmware_regions)
        self.store = RunStore(runs_dir or (LOGGER_DIR / "runs"))
        # 设备登记表：优先显式传入（测试/CLI 会换掉它），再问插件束，最后是一份
        # **空的 core 登记表**（没有可接的目标 —— 这比猜一份参考登记的副本诚实）。
        self.devices = devices or self.plugins.device_registry or device_registry.DeviceRegistry()
        #: 这一路装配**自己的**能力表（）。设备登记是按**板**的，而同一块板
        #: 可以有不止一份装配（同一份源码的两种日志能力）—— 装配处不交就退回设备登记
        #: （CLI 与老测试就是这一路），于是"谁支持什么"仍然只有一个来源。
        self.capabilities = capabilities
        # 工作区锁的**读**口子（只用于展示“现在谁持有工作区”）：装配处交进来的回调，
        # 没交就如实报“不知道”（不再 import 目标层的 `console_lock`）。
        self.workspace_lock_provider = workspace_lock
        # 实时观察会话（由服务注入）。它持有串口时自动验证必须被**明确拒绝**，
        # 而不是两个 owner 各抢一个 COM 口（需求 §3.1 / LIVE-12）。
        # CLI 入口（hil.py / crash_drill.py）不注入它：那里没有实时观察会话。
        self.observe: Any = None

        self._lock = threading.RLock()
        self.event_bus = event_bus or EventBus()

        self._run: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._step_output: dict[str, list[str]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        #: 当前这条命令是不是"附加到目标"（决定取消/超时时杀不杀它）；
        #: 以及它跑的是哪条适配器命令（写证据时说明"还活着的那一个是什么"）。
        self._process_attach = False
        self._process_purpose = ""
        #: 被留下继续跑（因为不能杀）的进程：按步骤记，进证据。Popen 引用也必须留着，
        #: 否则父进程过早关闭 stdout 管道，仍在运行的附加工具可能收到 broken pipe。
        self._left_running: dict[str, dict[str, Any]] = {}
        self._left_running_processes: dict[str, subprocess.Popen[bytes]] = {}
        self.server_started_utc = utc_now()
        #: 字典库只问一次（`_library_or_none()` 缓存它；`_library_asked` 与
        #: `_library is None` 是两件事：问过了但确实没有，不该每次再扫一遍目录）。
        self._library: Any = None
        self._library_asked = False
        self._seed_device_state()

    @property
    def adapters(self) -> dict[str, Any]:
        """受控适配器表（按角色取）。

        ⚠️  / R4：这里**不再延迟 import 参考目标**。适配器由装配处交进来
        （`hil.py` / `crash_drill.py` / 测试都这么接）；没交就是空表，
        `_adapter()` 会报一件说得清的事（“这个组合没装配 build 适配器”）。
        """
        return self._adapters

    def _adapter(self, role: str) -> Any:
        """按**角色**取一件适配器（没装配就报一件说得清的事，不是 AttributeError）。"""
        adapter = self.adapters.get(role)
        if adapter is None:
            raise adapters_mod.AdapterUnavailable(
                f"这个组合没装配 {role} 适配器（当前角色：{sorted(self.adapters)}）"
            )
        return adapter

    @property
    def suites(self) -> tuple[str, ...]:
        """这个项目能跑的套件（项目包说了算；没装项目包时一个都没有）。"""
        return tuple(self.project.suites)

    def _dialogue(self) -> device_dialogue.DeviceDialogue:
        """这个项目与设备的固定对话（**没声明就是一句话都不说**）。

        ⚠️  / R4：编排不替任何项目准备一套默认对话 —— 那正是"平台偷偷
        替某台设备说话"的样子（）。没有对话的设备只做只读采集。
        """
        declared = getattr(self.project, "dialogue", None)
        if declared is None:
            return device_dialogue.silent_dialogue()
        return declared

    def _observe_seconds_for(self, suite: str) -> float:
        """这个项目的这个套件该开多长的窗口（项目包声明；没声明就用中立保守值）。

        以前这是编排里的 `DEFAULT_OBSERVE_SECONDS_BY_SUITE` / `DEFAULT_OBSERVE_SECONDS`
        （"一个 30 s 记录周期 + 余量"是**那个固件**的契约，不是平台的）。
        """
        by_suite = dict(getattr(self.project, "observe_seconds_by_suite", None) or {})
        if suite in by_suite:
            return float(by_suite[suite])
        declared = float(getattr(self.project, "default_observe_seconds", 0.0) or 0.0)
        return declared if declared > 0 else NEUTRAL_OBSERVE_SECONDS

    def _capability_table(self) -> Any:
        """这一路装配的**能力表**（与"有没有在跑 run"无关）。

        ⚠️ 与 `_target_supports()` 是两件事：那个问的是"**这次 run 的目标**支持吗"，它按
        `run["target"]` 去设备登记里查 —— 没在跑 run 时那个键是空的，它会答 "不支持"。
        那是"**问不出**"，不是"做不到"（记忆里的老教训：两者必须分得开）。
        只读的问答（装配契约、页面探针、断言是否适用）必须走这个函数。
        """
        if self.capabilities is not None:
            return self.capabilities          # 装配处交进来的那一份（同一块板的两条链靠它分）
        run = self._run or {}
        spec = self.devices.get(str(run.get("target") or ""))
        if spec is not None:
            return getattr(spec, "capabilities", None)
        # 没有 run 上下文：设备登记里只要**所有**登记声明的是同一张表，就用它；
        # 说不一致（或压根没登记）就如实返回 None —— 不猜。
        rows = self.devices.specs() if hasattr(self.devices, "specs") else []
        tables = {id(getattr(row, "capabilities", None)): getattr(row, "capabilities", None)
                  for row in rows}
        return next(iter(tables.values())) if len(tables) == 1 else None

    def _declared_supports(self, capability: str) -> bool:
        """这一路**声明**支持某项能力吗（与 run 上下文无关；不知道按不支持算）。"""
        table = self._capability_table()
        return bool(table is not None and table.is_supported(capability))

    def _capabilities_json(self) -> dict[str, Any] | None:
        """这一路装配的能力声明（九项），写进 run manifest。

        ⚠️ ：需求 §2 要“历史 run 按其 manifest 中的能力回放”—— 没有这个字段时，
        看一份旧 run 只能从 `firmware.pio_env` 反推“这次到底做没做解码”，而那是猜。
        新 run 直接回答；旧 run 没这个键 ⇒ 读的人按“未声明”处理（不猜）。
        说不清（登记里能力不一致）就是 `None` —— 与 `unsupported` 是两件事。
        """
        table = self._capability_table()
        return dict(table.to_json()) if table is not None else None

    def _target_supports(self, capability: str) -> bool:
        """当前 run 的目标是否明确声明支持某项能力；未知也按不支持处理。

        ⚠️ ：能力表**优先取这一路装配的**（`self.capabilities`）。设备登记
        是按**板**的，而同一块板可以有不止一份装配（同一份源码的两种日志能力）——
        拿设备登记当唯一答案，第二个装配会静默继承第一个的能力（"以为关了
        其实按开着算"）。装配处没交时才退回设备登记（CLI / 老测试）。
        """
        table = self.capabilities
        if table is None:
            run = self._run or {}
            spec = self.devices.get(str(run.get("target") or ""))
            table = getattr(spec, "capabilities", None)
        return bool(table is not None and table.is_supported(capability))

    def _skip_reason(self, step: str) -> str:
        """能力不足是可审计的 skipped，不是失败，也不能伪装成执行成功。

        ⚠️  / R4：**没有的东西不猜**。解码要三样东西同时在位：目标声明支持、
        项目声明 token domain、装配处给了字典库与解码器。缺任何一样都写成
        `skipped` + 理由（"拿一个看起来像字典的东西去解码"会产出**读起来像日志的虚构**）。
        """
        if step == "decode":
            if not self._target_supports("decode"):
                return "目标声明 decode=unsupported；raw.bin 保留，decoded.log 保持空"
            if not self.token_domain:
                return "没有声明 token domain（不知道按哪个域过滤 token，不猜）；raw.bin 保留"
            if self._library_or_none() is None:
                return "没有装配字典库（拒绝猜字典）；raw.bin 保留，decoded.log 保持空"
            if self._decoder_or_none() is None:
                return "没有装配解码器；raw.bin 保留，decoded.log 保持空"
        if step == "upload" and bool(
            self.project.run_default(
                "skip_flash", False, suite=str((self._run or {}).get("suite") or "")
            )
        ):
            return (
                "这一套件声明跑在**现有镜像**上（skip_flash）⇒ 不烧录："
                "upload 记为可审计的 skipped，理由留在证据里"
            )
        if step == "assert" and self.project.assertions is None:
            return "项目包未提供 assertions；不对设备业务下判决"
        if (
            step == "assert"
            and bool(getattr(self.project, "assertions_require_decode", False))
            and not self._declared_supports("decode")
        ):
            # ⚠️ ：项目的断言读的是**解码出来的行**。这一路没有解码能力
            #    （例如关闭了 Token Log 的那份构建）时它们根本**验不了** —— 记成可审计的
            #    skipped，而不是让它们集体报 FAIL（那种报告是把"不适用"说成"坏了"）。
            #    是否依赖解码行由**项目包**声明，编排不替它决定。
            return (
                "这一路没有解码能力（目标声明 decode=unsupported），"
                "而项目的断言读的是解码行 ⇒ 本轮不对设备业务下判决；"
                "raw.bin 与设备身份仍然完整"
            )
        return ""

    @property
    def pio_env(self) -> str:
        """本次构建的 PlatformIO 环境（没指定时问构建适配器要默认那个）。"""
        if not self._pio_env:
            self._pio_env = str(self._adapter("build").default_env()).strip()
        return self._pio_env

    @property
    def discovery(self) -> adapters_mod.Discovery:
        """构建产物 / 字典 / 布局 / 工程文件在哪（问一次，整个 run 用同一份）。

        ⚠️ 它只**描述位置**，不保证文件存在：找不到文件的判断（与“人话”理由）在
        `_step_precheck` 里 —— “工具没装”是一个要报给用户的状态，不是崩溃。
        """
        if self._discovery is None:
            self._discovery = self._adapter("discovery").discovery(
                self.project_dir, self.pio_env
            )
        return self._discovery

    def _seed_device_state(self) -> None:
        """Restore the last known build identity of each board from disk.

        The device registry is in memory, but run directories are durable. After
        a restart the page must still be able to say which firmware was last
        seen on a board instead of claiming "未登记".
        """
        for spec in self.devices.specs():
            for summary in self.store.summaries(limit=200):
                if summary.get("target") != spec.key:
                    continue
                manifest = self.store.read_json(summary["run_id"], "manifest.json") or {}
                firmware = manifest.get("firmware") or {}
                if not firmware:
                    continue
                pio = firmware.get("pio_manifest") or {}
                self.devices.set_firmware(
                    spec.key,
                    {
                        "build_id": pio.get("build_id"),
                        "git_commit": pio.get("git_commit"),
                        "elf_sha256": firmware.get("elf_sha256"),
                        "bin_bytes": firmware.get("bin_bytes"),
                        "source": f"run {summary['run_id']}",
                    },
                )
                self.devices.set_activity(
                    spec.key,
                    manifest.get("run", {}).get("created_utc"),
                    f"最后一次 run 结果 {summary.get('result')}",
                )
                break

    # ============================================================= public API
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            run = dict(self._run) if self._run else None
            steps = [dict(step) for step in self._steps]
            active = bool(run and run.get("state") == "running")
            last_run_id = run.get("run_id") if run else None
        tool = self.discovery.tool
        return {
            "server": {
                "started_utc": self.server_started_utc,
                "project_dir": str(self.project_dir),
                "runs_dir": str(self.store.runs_dir),
                "platformio": str(tool or ""),
                "pid": os.getpid(),
                "workspace_lock": self.workspace_lock(),
            },
            "devices": self.devices.snapshot(),
            "steps": steps,
            "step_order": list(STEP_ORDER),
            "active_run": run if active else None,
            "last_run": run,
            "busy": active,
            # 广播自述（L5）：序号水位 + 积压 + 因为订阅者跟不上而丢掉的条数。
            # 页面用它算出"我落后多少条 / 有没有我根本没见过的东西"。
            "broadcast": self.event_bus.stats(),
            # 采集窗口的**生效区间**（）：这是 core 的通用策略，不是项目包声明，
            # 所以也不该由页面写死 —— 页面写死一个比 core 小的上限，用户就永远要不到
            # 他输入的值（输入框自己会把它挡回去），而对话框还会把他的原话抄一遍。
            "observe_limits": {
                "min_seconds": MIN_OBSERVE_SECONDS,
                "max_seconds": MAX_OBSERVE_SECONDS,
            },
        }

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Public entry to the event bus (used by the live-observe session).

        ⚠️ ：这里是**实时观察**那条通道，所以给事件打上 `origin="live"`。
        以前不同：总线上唯一能区分“谁发的”的东西是 `run_id`，而它取的是“发的时候
        有没有 run 在跑”——实时观察在收的行，只要有人跑着 run，就会被标成那个 run 的
        `run_id`；页面于是把 COM<n> 的行写进了自动验证（另一个口）的窗口。
        `origin` 说的是**这条路**，`run_id` 仍然按老样子留着（不动它，免得越到既有消费者）。
        """
        fields.setdefault("origin", "live")
        return self._emit(kind, **fields)

    def active_run_port(self) -> dict[str, Any] | None:
        """实时观察的闸门：自动验证此刻正在用哪个串口（None = 没有 run 在跑）。

        只要 run 还在跑就报出来 —— 包括 precheck 阶段（那时它还没开串口，
        但马上就要开；让实时观察这时候挤进去，只会两边都变得不可解释）。
        """
        with self._lock:
            run = self._run
            if not run or run.get("state") != "running":
                return None
            return {
                "run_id": run.get("run_id"),
                "port": run.get("port"),
                "target": run.get("target"),
            }

    def workspace_lock(self) -> dict[str, Any] | None:
        """现在谁持有工作区锁（None = 空着，或者没装配这个读口子）。

        ⚠️  / R4：锁的实现（`console_lock`，它靠 `platformio.ini` 认目录）
        住在目标层 —— 编排只把装配处交进来的那份读数转给页面，自己不去读锁文件。
        没装配时返回 None（**不知道**），不冒充“空着”。
        """
        if self.workspace_lock_provider is None:
            return None
        try:
            return self.workspace_lock_provider()
        except Exception:  # noqa: BLE001 - 页面上的一个展示字段不该弄崩一次请求
            return None

    def runs(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.store.summaries(limit=limit)
        with self._lock:
            run = dict(self._run) if self._run else None
        if run and run.get("state") == "running":
            for row in rows:
                if row["run_id"] == run["run_id"]:
                    row["result"] = "running"
            if not any(row["run_id"] == run["run_id"] for row in rows):
                rows.insert(
                    0,
                    {
                        "run_id": run["run_id"],
                        "suite": run["suite"],
                        "target": run["target"],
                        "port": run["port"],
                        # 正在跑的那一趟也要带设备（页面顶部那行与历史表同一条口径）。
                        "device": sniff_mod.recorded_target(
                            {"device": run.get("run_device") or None,
                             "run": {"port": run.get("port"), "target": run.get("target")}}
                        ).to_json(),
                        "result": "running",
                        "started_utc": run["started_utc"],
                        "duration_ms": None,
                        "has_report": False,
                        "failure_reason": None,
                    },
                )
        return rows

    def recorded_digests(self, run_id: str) -> dict[str, str]:
        """这个 run 的报告里**已经记下来**的产物 SHA-256（没有就是空表）。

        ⚠️ 它只读记录、**不现算**（PERF-HIST-07）。L4 的稀疏索引拿它当指纹用：
        有就比大小 + 比哈希（更严），没有就只比大小并在页面上如实写"指纹 = 大小"。
        "没得核对"永远不等于"核对通过"。
        """
        return _recorded_hashes(self.store.read_json(run_id, "report.json") or {})

    def artifact_metadata(self, run_id: str, *, live: bool = False) -> list[dict[str, Any]]:
        """产物元数据（**只 stat**：不读内容、不现算哈希）—— 详情首屏用它。

        `sha256` 只取 `report.json` 里已经记下的那一份；没有就是 `None`（页面写"未记录"）。
        """
        recorded = _recorded_hashes(self.store.read_json(run_id, "report.json") or {})
        rows: list[dict[str, Any]] = []
        for name in runstore.KNOWN_ARTIFACTS:
            path = self.store.artifact(run_id, name)
            if not path.is_file():
                continue
            row = large_evidence.artifact_metadata(
                path,
                name,
                role=(
                    large_evidence.ROLE_ORIGINAL
                    if name in runstore.ORIGINAL_ARTIFACTS
                    else large_evidence.ROLE_DERIVED
                ),
                state=large_evidence.STATE_OPEN if live else large_evidence.STATE_CLOSED,
                sha256=recorded.get(name),
            ).to_json()
            row["purpose"] = runstore.EVIDENCE_PURPOSE.get(name, "")
            row["url"] = f"/api/runs/{run_id}/evidence/{name}"
            row["download_url"] = f"{row['url']}?download=1"
            rows.append(row)
        return rows

    def _inline_texts(
        self, run_id: str, artifacts_meta: list[dict[str, Any]], content: str
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """按阈值决定哪几份文本证据可以**内联**进详情，其余只给元数据与 Range 地址。"""
        sizes = {str(row["name"]): int(row["bytes"]) for row in artifacts_meta}
        texts: dict[str, str] = {}
        omitted: list[dict[str, Any]] = []
        for name in ("decoded.log", "tool.log", "app_events.jsonl", "events.jsonl"):
            size = sizes.get(name, 0)
            if content == "inline" and not large_evidence.should_inline(size):
                # 旧的内嵌正文形式**只在阈值内可用**：不然它就还是一条“默认全量读取”
                # 的公开路径（需求 §5.1）。拒绝时直接把 Range 指路说清楚。
                error = large_evidence.inline_too_large(name, size)
                raise RequestError(error.message, status=error.status)
            if content == "none" or not large_evidence.should_inline(size):
                texts[name] = ""
                if name in sizes:
                    omitted.append({
                        "name": name,
                        "bytes": size,
                        "reason": "超过内联阈值" if size else "本次没有这份证据",
                        "url": f"/api/runs/{run_id}/evidence/{name}",
                        "download_url": f"/api/runs/{run_id}/evidence/{name}?download=1",
                    })
                continue
            texts[name] = self.store.read_text(run_id, name)
        # ⚠️ 二进制原件（`raw.bin` 等）**本来就不内联**，但它们必须出现在 `content_omitted` 里：
        #    页面靠这一条才知道"这一路要走区间窗口、总共多少字节、Range 地址在哪"。
        #    少了它，前端就只能像以前那样整份 GET 回来 —— 而那一份就是本需求要消灭的。
        for name in runstore.ORIGINAL_ARTIFACTS:
            size = sizes.get(name)
            if size is None or large_evidence.should_inline(size):
                continue
            omitted.append({
                "name": name,
                "bytes": size,
                "reason": "二进制证据不内联（按区间取）",
                "url": f"/api/runs/{run_id}/evidence/{name}",
                "download_url": f"/api/runs/{run_id}/evidence/{name}?download=1",
            })
        return texts, omitted

    def run_detail(self, run_id: str, *, content: str = "auto") -> dict[str, Any]:
        """一个 run 的详情（首屏）。

        `content` 三态（需求 §5.1）：
        - `"auto"`（默认）：只内联**小于阈值**的文本证据；超出的不读内容，
          改在 `artifacts_meta` / `content_omitted` 里给名字、字节数与 Range 地址；
        - `"none"`：完全不内联（只看摘要的客户端用）；
        - `"inline"`：旧的内嵌正文形式，**超阈值给结构化 413**。
        """
        if content not in ("auto", "none", "inline"):
            raise RequestError(f"认不出的 content={content!r}（可用：auto / none / inline）")
        directory = self.store.path(run_id)
        if not directory.is_dir():
            raise RequestError(f"run {run_id} 不存在", status=404)
        report = self.store.review_report(run_id)
        manifest = self.store.read_json(run_id, "manifest.json")
        with self._lock:
            live = bool(
                self._run
                and self._run.get("run_id") == run_id
                and self._run.get("state") == "running"
            )
            steps = [dict(step) for step in self._steps] if live else None
        artifacts_meta = self.artifact_metadata(run_id, live=live)
        text_files, omitted = self._inline_texts(run_id, artifacts_meta, content)
        # 指标序列不读目录里的 metrics.jsonl，而是从 app_events.jsonl 重算 —— 这样
        # 即使是 Phase 6 之前跑的旧 run（没有那份导出），页面也能画出趋势；而“导出物与
        # 序列是否一致”这件事由 A31 在 run 当时就核对过了。
        #
        # ⚠️  / R4：这只对**当前项目**的 run 成立。同一份 store 里可能出现
        # 另一个项目（例如第二目标）的 run，它的事件不是本项目的形状 —— 那时候要
        # **如实说算不了**，而不是让详情页 500（阶段 1 的教训：详情页报错会让人以为
        # 整份证据都坏了）。
        metrics: dict[str, Any] | None = None
        metrics_note: str | None = None
        # 先看这份 run 是不是**当前项目**跑的（manifest 的平台自述里有项目名）。
        # 不是就明确说"算不了"：拿 示例项目 的指标表去算裸机的事件，画出来的图是假的。
        ran_as = str(((manifest.get("platform") or {}).get("project")) or "")
        if self.project.metrics is not None and self.project.events is not None:
            if ran_as and self.project.key and ran_as != self.project.key:
                metrics_note = (
                    f"这份 run 是「{ran_as}」项目跑的，当前页面装的是「{self.project.key}」，"
                    "指标无法重算"
                )
            elif not text_files["app_events.jsonl"]:
                # 事件文件被阈值拦下（没内联）⇒ 首屏**不去扫整份事件文件**算指标
                # （PERF-HIST-06：指标摘要不在首屏请求里扫描整个事件文件）。
                metrics_note = (
                    "app_events.jsonl 超过内联阈值，首屏不重算指标"
                    "（按需用 Range 装载后再算）"
                )
            else:
                try:
                    metrics = self.project.metrics.series(
                        self.project.events.from_jsonl(
                            text_files["app_events.jsonl"].encode("utf-8")
                        )
                    )
                except Exception as error:  # noqa: BLE001 - 详情页绝不允许因此 500
                    metrics = None
                    metrics_note = (
                        f"这份 run 不是当前项目跑的，指标无法重算（{type(error).__name__}: {error}）"
                    )
        with self._lock:
            live = bool(
                self._run
                and self._run.get("run_id") == run_id
                and self._run.get("state") == "running"
            )
            steps = [dict(step) for step in self._steps] if live else None
        return {
            "run_id": run_id,
            "run_dir": str(directory),
            "report": report,
            "manifest": manifest,
            #  / D6：**这一趟自己记下的设备**（`device` 块；旧 run 退到
            # `run.port` / `run.target`，并如实标 `legacy=true`）。
            # ⚠️ 它只读这一份 manifest —— 不拿当前嗅探结果、也不拿当前台架配置去改写历史：
            #    回放看到的口必须是**当时**那个口，否则"上次跑的是哪块板"就永远说不清了。
            #    旧 run 里没记过的字段（`scan_seq` / 身份）如实留空，**不补**。
            "device": sniff_mod.recorded_target(manifest).to_json(),
            "artifact_names": list(self.store.artifacts(run_id)),
            # ⚠️ `artifacts` 现在与 `artifacts_meta` 同一份数据（**只 stat**）：
            #    它以前会为每一份产物现算 SHA-256，400 MB 的 run 一打开就白读一遍
            #    （PERF-HIST-07）。要继续算哈希请用 CLI / 独立流式工具。
            "artifacts": {str(row["name"]): row for row in artifacts_meta},
            "artifacts_meta": artifacts_meta,
            "content_mode": content,
            "content_omitted": omitted,
            "evidence_contract": large_evidence.describe(),
            "decoded_log": text_files["decoded.log"],
            "tool_log": text_files["tool.log"],
            "app_events": text_files["app_events.jsonl"],
            "event_stream": text_files["events.jsonl"],
            "metrics": metrics,
            "metrics_note": metrics_note,
            "live": live,
            "steps": steps,
        }

    def start(
        self,
        suite: str = "smoke",
        target: str = "a",
        observe_seconds: float | None = None,
        reset: bool = True,
        client: dict[str, Any] | None = None,
        set_clock: bool | None = None,
        dump_records: bool | None = None,
        clear_records: bool | None = None,
        probe_command: str | None = None,
        project_options: dict[str, Any] | None = None,
        test_selections: list[dict[str, Any]] | None = None,
        test_inputs: dict[str, bytes] | None = None,
        run_target: Any | None = None,
    ) -> dict[str, Any]:
        suite = (suite or "smoke").strip()
        target = (target or "a").strip().lower()
        if suite not in self.suites:
            raise RequestError(f"未知测试套件 {suite!r}，可用的是 {self.suites}")
        # "这个套件必须用哪个构建环境"由**项目包**声明（ / R4，）：
        # 它以前是编排里的 `SUITE_ENVS`。没声明就没有这条约束。
        allowed_envs = dict(getattr(self.project, "suite_environments", None) or {}).get(suite)
        if allowed_envs is not None and self.pio_env not in allowed_envs:
            raise RequestError(
                f"套件 {suite!r} 必须用构建环境 {tuple(allowed_envs)} 跑，"
                f"当前是 {self.pio_env!r}；否则报告会拿一个构建的数字说另一个构建的事"
            )
        spec = self.devices.get(target)
        if spec is None:
            raise RequestError(f"未知目标板 {target!r}")
        if not spec.pipeline_allowed:
            raise RequestError(
                f"{spec.label}（{spec.port}）在第一版只显示状态，不接受流水线操作",
                status=403,
            )
        #  / D2：通信口可以由**本次设备嗅探 / 复核**给出（`RunTarget`）。
        # ⚠️ 没给时保持老行为：从静态登记里取那块板配的口（CLI 与老调用方一点不受影响）。
        # 顺序很重要：**先定口，再问闸门** —— 闸门比的是端口，不是“哪块板”。
        port = str(getattr(run_target, "port", "") or "").strip() or spec.port
        # 模式互斥：实时观察连着同一个设备时，自动验证要么被明确拒绝，要么让用户先断开
        # —— 绝不允许两个 owner 同时读/写同一个串口（需求 §3.1 / LIVE-12）。
        if self.observe is not None:
            conflict = self.observe.port_conflict(port)
            if conflict:
                raise RequestError(conflict, status=409)

        #  / §4.6（2026-10-09 实测）：**这一趟要烧录、而目标没声明任何身份查询**
        # ⇒ 在**烧录之前**确定性拒绝。
        # ⚠️ 为什么不是"先烧完再看能不能核验"：核验不了就必须停在核验阶段（§4.6），
        #    那时板子已经换了固件、而且已经爆过一回电 —— 与其把代价推给设备，
        #    不如按"配错就拒绝"在动手前拒掉（与"缺烧录器绑定一律拒烧"同一条口径）。
        refusal = self._flash_identity_refusal(suite)
        if refusal:
            raise RequestError(refusal, status=409)

        # 采集窗口没指定时问**项目包**（"必须覆盖一个记录周期"是那个固件的契约，
        # 不是平台的； / R4，）。项目也没声明就用中立的保守值。
        if observe_seconds is None:
            observe_seconds = self._observe_seconds_for(suite)
        try:
            observe_seconds = float(observe_seconds)
        except (TypeError, ValueError):
            raise RequestError("observe_seconds 必须是数字") from None
        observe_seconds = max(MIN_OBSERVE_SECONDS, min(MAX_OBSERVE_SECONDS, observe_seconds))
        try:
            declared_options = self.project.validate_run_options(project_options)
        except ValueError as error:
            raise RequestError(str(error)) from None
        # ⚠️ 这一轮**运行参数**的优先级（W5 真机验收时定的，）：
        #   显式参数 > 套件默认 > 项目包 `run_defaults` > 中立默认（什么都不做）。
        # 以前它们是本函数的默认值（`set_clock=True` / `dump_records=True` /
        # `probe_command=""`）—— 那是**参考项目的固件事实**，第二个目标走 Web 时就会拿到
        # 别人的默认值：烧录与采集都成功，却因为没人问一句身份横幅而 `UNKNOWN_BUILD`。
        # `None` = "调用方没说"（不是 False），所以老调用方显式传的布尔值一点不变。
        resolved = resolve_run_parameters(
            self.project,
            suite=suite,
            set_clock=set_clock,
            dump_records=dump_records,
            clear_records=clear_records,
            probe_command=probe_command,
            project_options=project_options,
            declared_options=declared_options,
        )
        set_clock = resolved["set_clock"]
        dump_records = resolved["dump_records"]
        clear_records = resolved["clear_records"]
        probe_command = resolved["probe_command"]
        declared_options = resolved["project_options"]

        with self._lock:
            self._reap_stale_run()
            if self._run and self._run.get("state") == "running":
                raise RequestError(
                    f"已有 run {self._run['run_id']} 正在执行，请先等待或取消", status=409
                )

            manifest = self._build_run_manifest(suite, target, port, observe_seconds, reset)
            manifest["client"] = _normalize_client(client)
            # 本次 run 是否与设备聊过存储。写进 manifest 而不是留给断言去猜：
            # 崩溃演练的固件在 setup 里就崩了，它不可能有记录文件，也不应该被拿这个
            # 判失败（crash_drill.py 显式传 False）。
            manifest["run"]["set_clock"] = bool(set_clock)
            manifest["run"]["dump_records"] = bool(dump_records)
            # 本轮的记录文件是**新创建**的（设备先清空日志目录）。没有这一条，按天累积的
            # 文件就总是上一次构建（甚至上一次相位）留下的，头里的 timebase / 构建号永远
            # 核不了 —— “新建文件”这条路径也就从来没在真机上跑过。
            manifest["run"]["clear_records"] = bool(clear_records)
            manifest["run"]["project_options"] = dict(declared_options)
            # A3 freezes the server-validated selection in evidence. Device execution is
            # deliberately left to A4; downstream code must consume this snapshot.
            manifest["tests"] = {
                "schema": 1,
                "selection": list(test_selections or []),
            }
            # ⚠️  / R4：有的目标没有"对时/清空/转储"这套对话，它的验证方式
            # 是**发一条命令看有没有回应**（裸机固件是 `PING`）。命令由**目标/套件**声明，
            # 编排只管在窗口里发一次、把"发了什么"记进 manifest —— 断言才有东西可判。
            manifest["run"]["probe_command"] = str(probe_command or "")
            manifest["run"]["commands_sent"] = []
            if run_target is not None:
                #  / D2：**这一趟跑的是哪个设备**由本次嗅探/复核决定 ——
                # 通信口、实测身份、目标适配器与台架绑定一起冻结进 manifest。
                # ⚠️ `identity_after` 现在是空的：烧录后复核（D4）才会填，
                #    烧录前就把“核过了”写进去就是伪造证据。
                manifest["device"] = run_target.to_evidence()
            run_id = self.store.create(suite, target, manifest)
            self._library = None
            self._library_asked = False
            self._step_output = {}
            self._left_running = {}
            self._left_running_processes = {}
            # T5：四类证据流的行号游标（需求 §8.2）。**与 run 同生命周期** ——
            # 谁往证据里写一行（tool/raw/decoded），谁就在写成功后 `advance`。
            self._evidence_cursor = step_lifecycle.EvidenceCursor()
            self._steps = [
                {
                    "name": name,
                    "status": "pending",
                    "detail": "",
                    "started_utc": None,
                    "ended_utc": None,
                    "duration_ms": None,
                }
                for name in STEP_ORDER
            ]
            self._run = {
                "run_id": run_id,
                "suite": suite,
                "target": target,
                "port": port,
                "state": "running",
                "result": None,
                "failure_reason": None,
                "started_utc": utc_now(),
                "finished_utc": None,
                "duration_ms": None,
                "observe_seconds": observe_seconds,
                "reset_pulse": bool(reset),
                "set_clock": bool(set_clock),
                "dump_records": bool(dump_records),
                "clear_records": bool(clear_records),
                # ⚠️ 运行时那一份也必须带上：`_step_observe` 读的是 `self._run`，
                # 只写 manifest 的话它永远看不到这条命令（实测就是这么被漏掉的）。
                "probe_command": str(probe_command or ""),
                "commands_sent": [],
                "assertions": [],
                "assertion_summary": None,
                "firmware": {},
                "client": _normalize_client(client),
                "project_options": dict(declared_options),
                "test_selections": list(test_selections or []),
                "test_inputs": dict(test_inputs or {}),
                "events_summary": {},
                "artifacts_dir": str(self.store.path(run_id)),
                #  / D4：这一趟**冻结下来的设备声明**（新路径才有）。
                # 核验按它比，不按"当前台架"——否则事后改配置就能追认一份旧 run。
                "run_device": dict(manifest.get("device") or {}),
            }
            #: 套件声明的**本轮策略**（项目包按套件给）：`skip_flash`（跑在现有镜像上）、
            #: `run_sequence`（要发一串固定命令）这类是**声明**；manifest 与运行时那份都要带上
            #: —— 证据读 manifest、对话的 gate 与 `_skip_reason()` 读运行时那份（同一处声明）。
            for name, value in (self.project.suite_run_defaults.get(suite) or {}).items():
                self._run.setdefault(str(name), value)
                manifest["run"].setdefault(str(name), value)
            self.store.write_manifest(run_id, manifest)
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._execute, args=(run_id,), name=f"run-{run_id}", daemon=True
            )

        self._emit(
            "run_started",
            client=_client_label(client),
            message=(
                f"开始 smoke run：目标 {spec.label} {port}，"
                f"采集 {observe_seconds:.0f} s，复位脉冲={'是' if reset else '否'}，"
                f"发起者 {_client_label(client)}"
            ),
        )
        self._thread.start()
        return {"run_id": run_id, "target": target, "port": port}

    def cancel(self, run_id: str) -> bool:
        """取消当前 run（**附加类命令不杀进程**：只停止等待、把 PID 记进证据）。

        ⚠️ 以前这里无条件 `process.kill()`：对构建/生成那类命令没问题，
        但对**附加到目标**的命令（握着 ST-LINK 的 C-SPY）就是  那个坑 ——
        强杀下去探针要拔插才能恢复。现在按 `CommandSpec.attach` 分开做事。
        """
        with self._lock:
            if not self._run or self._run.get("run_id") != run_id:
                raise RequestError(f"没有正在执行的 run {run_id}", status=404)
            if self._run.get("state") != "running":
                return False
            self._cancel.set()
            process = self._process
            attach = self._process_attach
        self._emit("info", message="收到取消请求，正在停止当前步骤")
        if process is not None and process.poll() is None:
            if attach:
                self._emit(
                    "info",
                    message=(
                        f"当前步骤是附加到目标的命令：**不杀**进程 {process.pid}"
                        "（强杀会让探针要拔插）；本次 run 按已取消收场"
                    ),
                )
            else:
                try:
                    process.kill()
                except OSError:
                    pass
        return True

    def note_rejected_request(
        self, message: str, *, status: int, client: Any = None
    ) -> None:
        """Broadcast a refused control request to every open page.

        Two drivers can talk to this service at the same time (a human in one
        tab, an agent over HTTP). Without this, a click rejected in one tab is
        invisible in the other and simply looks like "the button did nothing".
        """
        label = _client_label(client)
        self._emit(
            "request_rejected",
            status=status,
            client=label,
            message=f"{label} 的请求被拒绝（HTTP {status}）：{message}",
        )

    # ------------------------------------------------------------- event bus
    def subscribe(self) -> queue.Queue:
        return self.event_bus.subscribe()

    def unsubscribe(self, channel: queue.Queue) -> None:
        self.event_bus.unsubscribe(channel)

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        return self.event_bus.since(seq)

    def replay_window(self, since: int, **limits: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """新订阅者的**有界**那一段（）：见 `EventBus.replay_window`。"""
        return self.event_bus.replay_window(since, **limits)

    def _emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        # ⚠️ ：编排这一路的事件默认 `origin="run"`（实时观察那一路走
        # `Pipeline.emit`，它自己传 `"live"`）。`setdefault` 是为了让那一路的标签
        # 穿过这里不被覆盖。
        fields.setdefault("origin", "run")
        with self._lock:
            run_id = self._run["run_id"] if self._run else None
        event = self.event_bus.publish(run_id, kind, fields)
        if run_id:
            try:
                self.store.append_event(run_id, event)
            except (OSError, ValueError):
                pass
        return event

    def _evidence_refs_since(
        self, before: dict[str, int] | None, anchors: dict[str, int] | None = None
    ) -> list[dict]:
        """把“阶段开始前的游标快照”变成这一段的证据行号引用（T5 / 需求 §8.2）。

        没写过一行的来源**不进表**（不伪造引用）；没有游标（纯测试夹具）就交空表。
        `anchors` = 每一类来源的**实际命中行**（可选；见 `EvidenceCursor.refs`）。
        """
        cursor = getattr(self, "_evidence_cursor", None)
        if cursor is None:
            return []
        return cursor.refs(before, anchors=anchors) if anchors else cursor.refs(before)

    def _record_stage_refs(
        self,
        run: dict[str, Any],
        stage_id: str,
        before: dict[str, int] | None,
        *,
        anchors: dict[str, int] | None = None,
    ) -> list[dict]:
        """阶段结束：四类区间记进 run（供 decode 后补写），返回**当时可得**的那部分。

        ⚠️ 用户 2026-10-09 口径：observe 期的终态事件只负责状态、耗时与**当时可得**的
        `tool/raw`；`decoded` / `app_events` 由 decode 之后的补写事件提供。

        `anchors`（用户口径 2026-10-13）= 这一阶段等的那一行回话**实际**出现在哪一行
        （按来源分开记）—— 它进 `anchor_line`，定位时把那一行滚到窗口中间。
        """
        all_refs = self._evidence_refs_since(before, anchors)
        run.setdefault("stage_refs", {})[stage_id] = all_refs
        return [row for row in all_refs if row["source"] in step_lifecycle.LIVE_REF_SOURCES]

    def _emit_stage_refs_supplement(
        self, run: dict[str, Any], *, decoded: bytes, events: list[Any] | None
    ) -> list[str]:
        """decode 之后补写阶段证据坐标（`test_step_refs`，**只补坐标**）。

        为什么要校验而不是直接重用：`decoded.log` 是 decode 阶段**整块重写**的
        （`decode_bytes(raw)` → `write_decoded`），observe 期间逐行累积的行号只有在
        “重写之后的行数 == 当时累积的行数”时才仍然指向同一行。一致才补，不一致就不补 ——
        宁缺不猜（与需求 §8.2 “不伪造引用”同一条规矩）。

        `app_events.jsonl` 的位置**靠 `offset` 换算**，不是靠“行数恰好相等”：
        每条结构化事件带 `offset` = 它在 `decoded.log` 里的**字节**偏移（`app_events.parse`
        逐行累加得出），而 decoded 每行的字节偏移可以由文本**确定**地算出来。
        于是“阶段占据的 decoded 行区间”能换算成字节区间，落进去的事件就是这一阶段的。
        这是计算，不是猜 —— 与“不按文本搜位置”不冲突。

        返回补写出去的阶段 id（给日志/报告用）；没补就返回空表。
        """
        recorded = dict(run.get("stage_refs") or {})
        seen_decoded = int((run.get("evidence_totals") or {}).get("decoded", 0))
        if not recorded or not seen_decoded:
            return []
        text = decoded.decode("utf-8", "replace")
        decoded_lines_after = len(text.splitlines())
        if decoded_lines_after != seen_decoded:
            self._emit(
                "info",
                message=(
                    f"解码后的行数（{decoded_lines_after}）与采集时逐行累积的行数"
                    f"（{seen_decoded}）不一致 ⇒ 不补写 decoded/app_events 坐标"
                    "（宁缺不猜：行号已经指不准了）"
                ),
            )
            return []
        # decoded 每一行的**起始字节偏移**（与 `app_events.parse` 同一套口径：keepends 的行、UTF-8 字节）。
        line_offsets: list[int] = [0]
        cursor = 0
        for line in text.splitlines(keepends=True):
            cursor += len(line.encode("utf-8"))
            line_offsets.append(cursor)
        rows = list(events or [])
        versions = dict(run.get("stage_versions") or {})
        written: list[str] = []
        for stage_id, refs in recorded.items():
            decoded_ref = next((row for row in refs if row.get("source") == "decoded"), None)
            if decoded_ref is None:
                continue
            patch: list[dict] = [dict(decoded_ref)]
            last = len(line_offsets) - 1
            start = line_offsets[min(max(int(decoded_ref["start_line"]) - 1, 0), last)]
            end = line_offsets[min(max(int(decoded_ref["end_line"]), 0), last)]
            inside = [row for row in rows if start <= int(getattr(row, "offset", -1)) < end]
            if inside:
                seqs = [int(getattr(row, "seq", 0)) for row in inside]
                # 中心锚点（用户口径 2026-10-13）：app_events 的"行号"是**事件序号**，
                # 与 decoded 的行号不是一回事（不得互相套用）⇒ 用**字节偏移换算**：
                # 取锚点那一行起点之后的第一条事件（那就是这一行产生的事件）；一条都没有
                # 就退回区间末行 —— 这是计算，不是猜（每条事件自带 offset）。
                anchor_seq = max(seqs)
                anchor_line = decoded_ref.get("anchor_line")
                if anchor_line is not None:
                    anchor_start = line_offsets[
                        min(max(int(anchor_line) - 1, 0), last)
                    ]
                    after_anchor = [
                        row for row in inside
                        if int(getattr(row, "offset", -1)) >= anchor_start
                    ]
                    if after_anchor:
                        anchor_seq = int(getattr(after_anchor[0], "seq", anchor_seq))
                patch.append(
                    {
                        "source": "app_events",
                        "start_line": min(seqs),
                        "end_line": max(seqs),
                        "anchor_line": max(min(seqs), min(anchor_seq, max(seqs))),
                    }
                )
            payload = step_lifecycle.refs_event_json(
                stage_id, int(versions.get(stage_id, 1)), patch, utc=utc_now()
            )
            payload.pop("kind", None)  # kind 由 _emit 的入参决定，不在负载里重复
            self._emit(step_lifecycle.TEST_STEP_REFS_KIND, **payload)
            written.append(stage_id)
        if written and not rows:
            self._emit("info", message="这一轮没有结构化事件层 ⇒ 只补 decoded 坐标")
        return written

    def _platform_description(self, target_key: str) -> dict[str, Any]:
        """平台自述（R2d 那一份）——run manifest 里也带上一份（ / R3d）。

        ⚠️ 装配处把它算一次、以**回调**交进来（`platform_facts=`），于是 `/api/state`、
        会话 manifest 与 run manifest 说的是同一份事实；没人交时如实报空
        （不是抄一份看着很像的默认值）。
        """
        if self.platform_facts is None:
            return {}
        try:
            described = dict(self.platform_facts())
        except Exception as error:  # pragma: no cover - 装配处的问题不该干掉一次 run
            self._emit("error", message=f"平台自述取不到：{error!r}")
            return {"error": repr(error), "run_target": target_key}
        return described

    # ================================================================ helpers
    def _tools_block(self) -> dict[str, Any]:
        """run manifest 里的“这台机器用什么构的”：全部来自发现适配器（R3b）。"""
        discovery = self.discovery
        tool = discovery.tool
        return {
            "platformio": str(tool or ""),
            "platformio_present": bool(tool) and Path(tool).is_file(),
            "platformio_python": str(discovery.tool_python or ""),
            "project_dir": str(self.project_dir),
            "pio_env": self.pio_env,
            "partitions": str(discovery.layout or ""),
        }

    def _build_run_manifest(
        self, suite: str, target: str, port: str, observe_seconds: float, reset: bool
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "run": {
                "suite": suite,
                "target": target,
                "port": port,
                "observe_seconds": observe_seconds,
                "reset_pulse": bool(reset),
                "created_utc": utc_now(),
            },
            "host": {
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "python_executable": sys.executable,
            },
            "tools": self._tools_block(),
            "repo": {
                "path": str(REPO_DIR),
                "git_commit": _git("rev-parse", "HEAD"),
                "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "git_dirty": bool(_git("status", "--porcelain")),
                "upstream_commit": "",
            },
            # 平台自述（DEC-010 / R2d）随会话 manifest 一起走，自动验证的 run 也不该少
            # （R3d）。只增字段：老工具读不到它不会坏。
            "platform": self._platform_description(target),
            "firmware": {},
            # ：这一路装配的能力声明（九项）。**开始时就有**，不依赖构建产物；
            # 于是“这份 run 到底该不该有解码结果”在 run 目录里可查，不必去猜 pio_env。
            "capabilities": self._capabilities_json(),
        }

    def _fault_evidence(self, run: dict[str, Any]) -> dict[str, Any]:
        """Everything needed to re-check a crash by hand.

        The verification plan (``公开文档`` §6) requires a
        failing run to keep the exact command, its exit code and the last stage
        that succeeded, so a human does not have to reconstruct how a verdict was
        reached -- or which binary it was reached against.
        """
        finding = run.get("panic") or {}
        backtrace = finding.get("backtrace") or {}
        if not isinstance(backtrace, dict):
            backtrace = {}
        last_ok = next(
            # Careful with the key names here: the timeline rows use "name" while
            # the SSE event for the same thing uses "step". An always-null field
            # is worse than no field, because it looks like evidence -- this was
            # wrong twice (first "ok" instead of "pass", then "step" instead of
            # "name"), and only the host test caught it.
            (step.get("name") for step in reversed(self._steps) if step.get("status") == "pass"),
            None,
        )
        return {
            "last_successful_step": last_ok,
            "verdict": finding.get("verdict"),
            "kinds": list(finding.get("kinds") or []),
            "addr2line_command": backtrace.get("command"),
            "addr2line_exit_code": backtrace.get("exit_code"),
            "addr2line": backtrace.get("addr2line"),
            "elf": backtrace.get("elf"),
            "elf_sha256": backtrace.get("elf_sha256"),
            "elf_matches_firmware": backtrace.get("elf_matches_firmware"),
            "frames": backtrace.get("frames") or [],
            "unresolved": backtrace.get("unresolved") or [],
            "error": backtrace.get("error"),
        }

    # --------------------------------------------------------- build location
    def _required_path(self, value: Path | None, what: str) -> Path:
        """适配器必须告诉我们"东西在哪"；说不出来是一件要说清楚的事，不是猜。"""
        if value is None:
            raise adapters_mod.AdapterUnavailable(
                f"这个组合没告诉我们{what}在哪（发现适配器没给这条路径）"
            )
        return Path(value)

    @property
    def build_dir(self) -> Path:
        """Output directory of *this* run's PlatformIO environment."""
        return self._required_path(self.discovery.build_dir, "构建目录")

    @property
    def token_domain(self) -> str:
        """解码用哪个 domain：**项目包声明的那个**；没声明就是空串（⇒ 不解码）。

        ⚠️  / R4：以前这里有一句 `or TOKEN_DOMAIN`，而那个常量是 `"示例项目"` ——
        一个平台模块"顺口说出"某个项目的 domain，正是  说的那类泄漏
        （第二目标的 domain 是 `stm32bm`，参考项目是 示例项目）。
        现在域名只有项目包一个来源；空 ⇒ `_skip_reason()` 把 decode 写成 `skipped`。
        """
        return str(getattr(self.project, "token_domain", "") or "")

    def _library_or_none(self) -> Any:
        """这次用哪一份**字典库**；拿不到就是 None（"没有"要说出来，不是编一份）。

        两个来源，顺序固定（ / R4）：

        1. `Discovery.library`（**目标**声明"这次构建的字典在哪"）；
        2. 插件束的 `library_factory`（装配处交进来的"库从哪来"）。

        ⚠️ 以前这里还有第三条退路：`dictionary.discover()`（参考项目的当前构建 + 归档目录）。
        那是一条**藏在 core 里的平台假设**（）—— 现在没有就是没有，
        解码步骤会写成 `skipped` 并说明原因。
        """
        if self._library_asked:
            return self._library
        self._library_asked = True
        provided = None
        try:
            provided = getattr(self.discovery, "library", None)
        except adapters_mod.AdapterUnavailable:
            # 连发现适配器都没装配：那就是"没有库"（而不是把一次调用炸在这里）。
            provided = None
        if callable(provided):
            self._library = provided()
        elif self.plugins.library_factory is not None:
            self._library = self.plugins.library_factory()
        return self._library

    @property
    def library(self) -> Any:
        """字典库；拿不到时**抛一件说得清的事**（调用方要么先 `_skip_reason`，要么接受失败）。"""
        found = self._library_or_none()
        if found is None:
            raise adapters_mod.AdapterUnavailable(
                "这个组合没有字典库（发现适配器没给 `library`，装配处也没交 `library_factory`）"
            )
        return found

    @property
    def dictionary_path(self) -> Path:
        return self._required_path(self.discovery.dictionary, "字典文件")

    @property
    def pio_manifest_path(self) -> Path:
        return self._required_path(self.discovery.artifact_manifest, "构建清单")

    def _firmware_elf(self) -> Path:
        """The ELF of this project's build -- the only binary a backtrace may be
        decoded against.

         / R4：哪个文件是这次构建的 ELF 由**目标**声明（`Discovery.elf`）。
        它没声明时如实报“这个组合没告诉我们 ELF 在哪”，不再照 `.pio` 的相对路径猜。
        """
        return self._required_path(self.discovery.elf, "本次构建的 ELF")

    def _open_transport(self, port_name: str) -> Any:
        """打开采集用的 transport（装配处经插件束交进来的串口实现）。

        ⚠️  / R4 / DEC-004：core 不 import pyserial，也不自己 new 一个
        `Serial`。没装配就**如实说不能采集**（不是崩在 `import serial` 上）。
        """
        factory = getattr(self.plugins, "port_factory", None)
        if factory is None:
            raise StepFailure(
                "没有装配串口实现（装配处没交 `port_factory`）：这一步不能采集"
            )
        port = factory(port_name, SERIAL_BAUD)
        # 读超时由**读循环这一层**定（`core_primitives.READ_TIMEOUT_S`）：
        # 它决定 pump 多久发现"这段字节安静下来了"（安静的尾巴也要显示）。
        try:
            port.timeout = READ_TIMEOUT_S
        except Exception:  # 替身端口不一定有 timeout
            pass
        port.open()
        return port

    def _panic_parser(self) -> Any:
        """崩溃解析器（`inspect` / `summarize`）；没装配就是 None。

        ⚠️  / R4：以前编排直接 `import panic`（它的复位码表与符号化工具
        都是某个平台的实现）。没装配时**不做崩溃判定**，也不写空的 panic.json。
        """
        return getattr(self.plugins, "panic_parser", None)

    def _decoder_or_none(self) -> Any:
        """解码器（`make_line_decoder` / `decode_bytes`）；没装配就是 None。

        ⚠️  / R4：以前这里是 `import token 解码模块` —— Workbench 的
        detokenizer 是**参考目标的实现**，藏在编排里就又是一条平台假设（）。
        现在它由装配处经插件束交进来（`PlatformPlugins.decoder_backend`）。
        """
        return getattr(self.plugins, "decoder_backend", None)

    def _decoder(self) -> Any:
        decoder = self._decoder_or_none()
        if decoder is None:
            raise adapters_mod.AdapterUnavailable(
                "没有装配解码器（装配处没交 `decoder_backend`）"
            )
        return decoder

    def _update_step(
        self, name: str, status: str, detail: str = "", duration_ms: int | None = None
    ) -> dict[str, Any]:
        with self._lock:
            for step in self._steps:
                if step["name"] != name:
                    continue
                step["status"] = status
                if detail:
                    step["detail"] = detail
                if status == "running":
                    step["started_utc"] = utc_now()
                elif status in {"pass", "fail", "skipped"}:
                    step["ended_utc"] = utc_now()
                    if duration_ms is not None:
                        step["duration_ms"] = duration_ms
                row = dict(step)
                run_id = self._run["run_id"] if self._run else None
                break
            else:
                raise KeyError(name)
        if run_id:
            try:
                self.store.append_step(run_id, row)
            except (OSError, ValueError):
                pass
        return row

    def _symbol_tool(self) -> Path | None:
        """符号化工具在哪（问 `symbol` 适配器；它说没有就如实报“没能定位”）。

        ⚠️ 不在这里自己做工具链发现（ / R3d）：本机装没装 xtensa、装在哪，
        都是目标的知识；编排只把答案转给 `panic.inspect`。
        """
        try:
            found = self._adapter("symbol").locate()
        except adapters_mod.AdapterUnavailable:
            return None
        return Path(found) if found is not None else None

    def _symbol_argv(self, elf: Path, addresses: list[str]) -> list[str]:
        """符号化命令行由 `symbol` 适配器给（受控参数，DEC-003）。"""
        return list(self._adapter("symbol").symbolize(Path(elf), list(addresses)).argv)

    def _apply_reset_plan(self, port: Any) -> Any:
        """照目标适配器给的**复位时序**执行（ / R3c）。

        ⚠️ 这里就是  /  的落地处：先只动 DTR（启动模式那根线）→ 等它把
        启动横幅打完 → 丢掉那一段字节 → 再单独脉冲 RTS(EN)。两次复位**必须分开**，
        否则第一次的横幅会被第二次从中间截断（raw.bin 第一行是碎片）。

        时序本身是**数据**（`ResetPlan`），编排只认这几种动作（`assign` / `settle` /
        `flush_input` / `tool`）—— 也不给"跑一段代码"这种口子：计划是不受信的输入，
        动作与线名都要在这里校验过后才作用到串口上；`tool` 那一步带的是一条
        **受控命令**（R4：SWD 探针侧的复位不是串口能做的事，由目标层给命令）。
        """
        plan = self._adapter("reset").plan()
        self._emit("info", message=f"复位时序来自 {plan.adapter_id}：{plan.note}")
        for index, step in enumerate(plan.steps, 1):
            if step.action == "assign":
                if step.line not in ("dtr", "rts"):
                    raise StepFailure(
                        f"复位计划第 {index} 步要给不认识的线赋值（{step.line!r}）："
                        "只允许 dtr / rts"
                    )
                setattr(port, step.line, bool(step.value))
            elif step.action == "settle":
                time.sleep(max(0.0, float(step.seconds)))
            elif step.action == "flush_input":
                port.reset_input_buffer()
            elif step.action == "tool":
                spec = getattr(step, "spec", None)
                if spec is None:
                    raise StepFailure(
                        f"复位计划第 {index} 步是 tool，但没带命令：这样的计划不成立"
                    )
                code = self._run_spec(spec, "reset")
                if code != 0:
                    raise StepFailure(
                        f"复位计划第 {index} 步的命令退出码 {code}（{spec.purpose}）："
                        "复位没做成，不往后走"
                    )
            else:
                raise StepFailure(
                    f"复位计划第 {index} 步的动作不认识（{step.action!r}）："
                    "只允许 assign / settle / flush_input / tool"
                )
        return plan

    def _run_spec(self, spec: Any, step: str) -> int:
        """跑一条**受控命令**：命令、工作目录与超时都由适配器给（ / R3a）。

        编排在这里只做三件事：把"这是哪个适配器要干什么"记进工具窗口（读证据的人
        要能复核）、把退出码还给调用方（"成没成"是每个步骤自己的判据）、
        以及按 `spec.attach` 决定**取消/超时时能不能杀它**。
        """
        adapter_id = str(getattr(spec, "adapter_id", "") or "")
        if adapter_id:
            self._emit("tool", step=step, line=f"# 适配器 {adapter_id} · {spec.purpose}")
        return self._run_tool(
            list(spec.argv), step,
            cwd=Path(spec.cwd),
            timeout_s=float(getattr(spec, "timeout_s", 0) or 0),
            attach=bool(getattr(spec, "attach", False)),
            purpose=str(getattr(spec, "purpose", "") or ""),
        )

    def _run_tool(
        self, argv: list[str], step: str, cwd: Path | None = None,
        timeout_s: float = 0.0, attach: bool = False, purpose: str = "",
    ) -> int:
        """Run one fixed-argv tool, streaming its output to the UI and disk.

        ⚠️ **取消与超时对"附加到目标"的命令不杀进程**（`attach=True`）：
        实测把握着 ST-LINK 的 C-SPY 强杀下去，探针要**拔插**才能恢复。
        这类命令到点就**停止等待**、如实记下"进程 N 还活着"、把人留在现场，
        而不是把一个握着硬件的进程弄没（ / ）。
        """
        run_id = self._run["run_id"]  # type: ignore[index]
        self._emit("tool", step=step, line=f"$ {' '.join(argv)}")
        lines: list[str] = []
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        process = subprocess.Popen(
            argv,
            cwd=str(cwd or self.project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self._process = process
        self._process_attach = attach
        self._process_purpose = purpose
        deadline = (time.monotonic() + timeout_s) if timeout_s > 0 else None
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for output_line in _iter_lines(process.stdout.fileno()):
                    messages.put(("line", output_line))
            except OSError as error:
                messages.put(("error", error))
            finally:
                messages.put(("done", None))

        # Windows 管道不能可靠地交给 selectors；单独的读取线程让编排线程即使在工具
        # **完全静默**时也能每 50 ms 检查取消和 deadline。线程是 daemon：附加工具被
        # 刻意留下时，它继续替这个进程守着 stdout，不会反过来卡住服务退出。
        reader = threading.Thread(
            target=read_output, name=f"tool-output-{step}-{process.pid}", daemon=True
        )
        reader.start()
        stop_reason: str | None = None
        reader_done = False
        try:
            while not reader_done:
                cancelled = self._cancel.is_set()
                timed_out = deadline is not None and time.monotonic() >= deadline
                if (cancelled or timed_out) and process.poll() is None:
                    stop_reason = "cancelled" if cancelled else "timeout"
                    if attach:
                        fact = {
                            "pid": process.pid,
                            "adapter_purpose": purpose,
                            "reason": stop_reason,
                            "timeout_s": timeout_s,
                        }
                        self._left_running[step] = fact
                        self._left_running_processes[step] = process
                        self._emit(
                            "tool", step=step,
                            line=(
                                f"# {'已取消' if cancelled else '超时'}：附加类命令**不杀**，"
                                f"进程 {process.pid} 仍在（它握着探针；要结束请人工处理）"
                            ),
                        )
                        break
                    try:
                        process.kill()
                    except OSError:
                        pass
                    # kill 后继续排空 reader，保留终止前已经产生的输出。
                    deadline = None
                try:
                    kind, payload = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind == "done":
                    reader_done = True
                    continue
                if kind == "error":
                    # 普通终止/kill 可能同时关闭管道；退出码仍是主判据。
                    continue
                line = str(payload)
                if line:
                    lines.append(line)
                    self._emit("tool", step=step, line=line)
                    try:
                        self.store.append_tool(run_id, line)
                    except (OSError, ValueError):
                        pass
                    else:
                        cursor = getattr(self, "_evidence_cursor", None)
                        if cursor is not None:
                            cursor.advance("tool")
            if attach and stop_reason is not None:
                self._step_output.setdefault(step, []).extend(lines)
                if stop_reason == "cancelled":
                    raise RunCancelled()
                raise StepFailure(
                    f"附加类命令超时（>{timeout_s:g}s），进程 {process.pid} 未被强杀；"
                    "PID 已写入 report.json"
                )
            code = process.wait()
        finally:
            self._process = None
            self._process_attach = False
            self._process_purpose = ""
        # 一个步骤可以跑**多条**命令（构建 = prepare → build → finalize）：输出要**累加**，
        # 不能让最后一条覆盖前面的。覆盖时 RT-Thread 的尺寸行（在 build 那条里）被
        # finalize 的输出冲掉 ⇒ 构建说明一直是 "RAM ? B，Flash ? B"（）。
        self._step_output.setdefault(step, []).extend(lines)
        if self._cancel.is_set():
            raise RunCancelled()
        return code

    def _step_text(self, step: str) -> str:
        return "\n".join(self._step_output.get(step, ()))

    # ============================================================== execution
    def _execute(self, run_id: str) -> None:
        run = self._run
        assert run is not None
        try:
            for step in STEP_ORDER:
                skipped = self._skip_reason(step)
                if skipped:
                    self._update_step(step, "skipped", skipped, 0)
                    self._emit(
                        "step", step=step, status="skipped", detail=skipped, duration_ms=0,
                    )
                    continue
                self._update_step(step, "running")
                self._emit("step", step=step, status="running")
                started = time.monotonic()
                try:
                    detail = getattr(self, f"_step_{step}")()
                except RunCancelled:
                    elapsed = int((time.monotonic() - started) * 1000)
                    self._update_step(step, "fail", "已取消", elapsed)
                    self._emit("step", step=step, status="fail", detail="已取消")
                    raise
                except StepFailure as failure:
                    elapsed = int((time.monotonic() - started) * 1000)
                    message = str(failure)
                    run["failure_reason"] = f"{step}: {message}"
                    self._update_step(step, "fail", message, elapsed)
                    self._emit("step", step=step, status="fail", detail=message)
                    raise
                elapsed = int((time.monotonic() - started) * 1000)
                self._update_step(step, "pass", detail or "", elapsed)
                self._emit("step", step=step, status="pass", detail=detail or "", duration_ms=elapsed)
            run["result"] = "PASS"
        except RunCancelled:
            run["result"] = "CANCELLED"
            run["failure_reason"] = run.get("failure_reason") or "用户取消"
        except StepFailure:
            run["result"] = "FAIL"
        except Exception as error:  # pragma: no cover - defensive
            run["result"] = "ERROR"
            run["failure_reason"] = f"未预期异常: {error!r}"
            self._emit("error", message=f"未预期异常: {error!r}")
        finally:
            self._finalize(run_id)

    def _finalize(self, run_id: str) -> None:
        """Close a run and persist its report.

        Everything in here is best-effort **except** the state transition. A run
        that stays "running" wedges the whole console: the start button never
        re-enables and every new request gets 409. That happened for real when
        `summarize()` raised inside this method (an exception from a `finally`
        block is not caught by the surrounding handler) -- see .
        """
        run = self._run
        assert run is not None

        run["state"] = "finished"
        run["finished_utc"] = utc_now()
        run["duration_ms"] = _elapsed_ms(run.get("started_utc"), run["finished_utc"])

        if self.project.assertions is None:
            summary = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "failed_ids": [],
                "note": "项目包未提供 assertions；本轮只判平台步骤是否完成",
            }
        else:
            try:
                summary = self.project.assertions.summarize(run.get("assertions") or [])
            except Exception as error:  # noqa: BLE001 - must not wedge the console
                summary = {}
                run["failure_reason"] = run.get("failure_reason") or f"断言汇总失败: {error!r}"
                self._emit("error", message=f"断言汇总失败: {error!r}")
        run["assertion_summary"] = summary

        report = {
            "schema": 1,
            "run_id": run_id,
            "suite": run["suite"],
            "target": run["target"],
            "port": run["port"],
            "started_utc": run.get("started_utc"),
            "finished_utc": run["finished_utc"],
            "duration_ms": run["duration_ms"],
            "result": run["result"],
            "failure_reason": run.get("failure_reason"),
            "observe_seconds": run["observe_seconds"],
            "reset_pulse": run["reset_pulse"],
            "project_options": dict(run.get("project_options") or {}),
            "steps": self._steps,
            "assertions": run.get("assertions") or [],
            "assertion_summary": summary,
            "assertion_domains": run.get("assertion_domains") or [],
            "firmware": run.get("firmware") or {},
            "client": run.get("client") or {},
            "events_summary": run.get("events_summary") or {},
            "panic": run.get("panic_summary") or {},
            "dictionary_used": run.get("dictionary_used") or {},
            "fault_evidence": self._fault_evidence(run),
            "left_running_processes": dict(self._left_running),
        }
        interim = self.store.read_json(run_id, "report.json") or {}
        tests = run.get("tests") or interim.get("tests")
        test_summary = run.get("test_summary") or interim.get("test_summary")
        if tests is not None:
            report["tests"] = tests
        if test_summary is not None:
            report["test_summary"] = test_summary
        # `run_finished` is part of events.jsonl. Emit it before taking the final
        # artifact snapshot, otherwise the report immediately contains a stale
        # size/hash for that file.
        self.devices.set_owner(run["target"], None)
        self._emit(
            "run_finished",
            result=run["result"],
            duration_ms=run["duration_ms"],
            failure_reason=run.get("failure_reason"),
            assertion_summary=summary,
            assertion_domains=run.get("assertion_domains") or [],
            message=(
                f"run {run_id} 结束：{run['result']}"
                + (f"（{run['failure_reason']}）" if run.get("failure_reason") else "")
            ),
        )
        try:
            report["artifacts"] = self.store.artifacts(run_id)
            # A JSON document cannot contain its own final hash. Keep the file
            # downloadable, but do not publish the placeholder report's hash as
            # if it described the final report.
            report["artifacts"].pop("report.json", None)
        except (OSError, ValueError):
            report["artifacts"] = {}
        # The index a reader actually needs: which file is authoritative, what each
        # one is for, and its hash. `artifacts` alone is a directory listing.
        try:
            report["evidence"] = self.store.evidence_index(run_id)
            report["evidence"].pop("report.json", None)
        except (OSError, ValueError):
            report["evidence"] = {}
        try:
            self.store.write_report(run_id, report)
        except (OSError, ValueError) as error:
            self._emit("error", message=f"写 report.json 失败: {error}")


    def _reap_stale_run(self) -> None:
        """Un-wedge a run whose worker thread died without finalizing.

        Without this, one bug in the finalizer makes the console refuse every
        later request until the service is restarted. The evidence directory is
        left untouched; only the in-memory state is corrected.
        """
        run = self._run
        if not run or run.get("state") != "running":
            return
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        run["state"] = "finished"
        run["result"] = run.get("result") or "ERROR"
        run["failure_reason"] = run.get("failure_reason") or (
            "编排线程在写入报告前结束（内部错误）；证据目录保留，但本次运行没有判决"
        )
        run["finished_utc"] = run["finished_utc"] or utc_now()
        run["duration_ms"] = _elapsed_ms(run.get("started_utc"), run["finished_utc"])
        self.devices.set_owner(run.get("target", "a"), None)
        try:
            self.store.write_report(
                run["run_id"],
                {
                    "schema": 1,
                    "run_id": run["run_id"],
                    "suite": run.get("suite"),
                    "target": run.get("target"),
                    "port": run.get("port"),
                    "started_utc": run.get("started_utc"),
                    "finished_utc": run["finished_utc"],
                    "duration_ms": run["duration_ms"],
                    "result": run["result"],
                    "failure_reason": run["failure_reason"],
                    "steps": self._steps,
                    "assertions": run.get("assertions") or [],
                    "assertion_summary": run.get("assertion_summary") or {},
                    "assertion_domains": run.get("assertion_domains") or [],
                    "firmware": run.get("firmware") or {},
                    "artifacts": self.store.artifacts(run["run_id"]),
                },
            )
        except (OSError, ValueError):
            pass
        self._emit(
            "error",
            message=(
                f"上一个 run {run['run_id']} 的编排线程已结束但未写报告，"
                "已标记为 ERROR；可以重新发起运行"
            ),
        )

    # ------------------------------------------------------------------ steps
    def _step_precheck(self) -> str:
        run = self._run  # type: ignore[assignment]
        problems: list[str] = []
        notes: list[str] = []

        discovery = self.discovery
        if discovery.project_ini is None or not Path(discovery.project_ini).is_file():
            problems.append(f"缺少 {discovery.project_ini or '工程文件'}")
        tool = discovery.tool
        if tool is None or not Path(tool).is_file():
            problems.append(self._build_tool_problem(tool))
        if discovery.layout is not None and not Path(discovery.layout).is_file():
            problems.append(f"缺少存储布局文件 {discovery.layout}")

        # 串口实现（transport）与它的体检都由装配处经插件束交进来：
        # ⚠️  / R4：编排以前自己 `import serial` —— 那是把一种 transport 实现
        # 钉在 core 里（DEC-004 要的是可替换）。这里只转达那句话。
        if getattr(self.plugins, "port_factory", None) is None:
            problems.append("没有装配串口实现（装配处没交 `port_factory`）：不能采集")
        else:
            probe = getattr(self.plugins, "transport_probe", None)
            note_text = ""
            if probe is not None:
                try:
                    note_text = str(probe() or "")
                except Exception as error:  # noqa: BLE001 - 体检本身炸了也是一条要说的事
                    problems.append(f"串口实现体检失败：{error!r}")
            if note_text:
                notes.append(note_text)
            else:
                hint = str(getattr(discovery, "tool_python", "") or "").strip()
                problems.append(
                    "本机的串口实现不可用（装配处交进来的那份说自己装不起来）；"
                    "请用带 pyserial 的解释器启动本服务"
                    + (f"（本次组合给的是 {hint}）" if hint else "")
                )

        if self._target_supports("decode"):
            try:
                self._decoder()
                notes.append("Workbench 解码器可用")
            except SystemExit as error:
                problems.append(str(error))
            except Exception as error:  # pragma: no cover - defensive
                problems.append(f"Workbench 解码器不可用: {error!r}")
        else:
            notes.append("目标声明 decode=unsupported（保留 raw 证据）")

        # ⚠️  / R4：占用探测走**设备登记表**（它拿着装配处交进来的探测实现）。
        # 这里以前直接调目标层的 `devices.port_access` —— 编排不该知道"用 CreateFileW 探口"。
        # 登记表没装配探测实现时返回 `unknown`（**不冒充空闲**）。
        # ⚠️ ：那句 `busy` 以前会因为**探测自己撞自己**而误报（页面每 2 s 探一遍
        #    所有设备，precheck 在另一个线程又探同一个口）。现在探测已经串行化，
        #    再说 `busy` 就真的有别人独占着它。
        access = self.devices.access(run["port"])
        if access == "absent":
            problems.append(f"{run['port']} 不存在或未被枚举")
        elif access == "busy":
            problems.append(
                f"{run['port']} 已被其他进程占用（关掉 ATK-XCOM / 串口助手 / ChatGPT 桌面版"
                "这类会独家占着串口的程序再试；另见 I-004 / ）"
            )
        elif access == "free":
            notes.append(f"{run['port']} 空闲")
        else:
            notes.append(f"{run['port']} 占用状态未知")

        if problems:
            raise StepFailure("；".join(problems))

        pio_version = self._probe_build_tool_version()
        if pio_version:
            notes.append(pio_version)
        return "，".join(notes)

    def _build_tool_problem(self, tool: Path | None) -> str:
        """构建工具不可用时，把**适配器自己**的说法带出来。

        适配器比编排更知道"该装什么、用哪个环境变量覆盖"（例如参考目标会说
        `PW_CONSOLE_PLATFORMIO_PENV`）；编排只负责把这句话转给用户，
        不再自己写"找不到 PlatformIO"这种带参考工具名的文案（）。
        """
        try:
            self._adapter("build").probe(
                adapters_mod.ToolProbeRequest(project_dir=self.project_dir)
            )
        except adapters_mod.AdapterUnavailable as error:
            return str(error)
        except Exception:  # noqa: BLE001 - 适配器抛别的类型是它的问题，别盖住真正的原因
            pass
        return f"找不到构建工具 {tool or '（发现适配器没给这条路径）'}"

    def _probe_build_tool_version(self) -> str:
        """问构建适配器要版本号（名字里不带任何工具名：那是目标层的知识）。"""
        try:
            spec = self._adapter("build").probe(
                adapters_mod.ToolProbeRequest(project_dir=self.project_dir)
            )
            result = subprocess.run(
                list(spec.argv),
                cwd=str(spec.cwd),
                capture_output=True,
                timeout=spec.timeout_s,
            )
        except (OSError, subprocess.SubprocessError, adapters_mod.AdapterUnavailable):
            return ""
        # ⚠️ ：同样**不要 `text=True`** —— 这条问的是**构建工具**的版本号，
        #    而工具可能是本地化的（中文 Windows 的 IAR / 厂商 CLI 都是 GBK）⇒
        #    严格 UTF-8 解码会打死读取线程，清单里的版本号会静默变成空。
        stdout = (result.stdout or b"").decode("utf-8", "replace")
        stderr = (result.stderr or b"").decode("utf-8", "replace")
        text = (stdout or stderr).strip().splitlines()
        if not text:
            return ""
        # ``platformio --version`` prints ``PlatformIO Core, version 6.2.0``.
        version = re.sub(
            r",\s*version\s+", " ", text[0].strip(), flags=re.IGNORECASE
        )
        try:
            manifest_path = self.store.artifact(self._run["run_id"], "manifest.json")  # type: ignore[index]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["tools"]["platformio_version"] = version
            self.store.write_manifest(self._run["run_id"], manifest)  # type: ignore[index]
        except (OSError, ValueError):
            pass
        return version

    def _step_build(self) -> str:
        #  / R3a：命令行由**目标适配器**给（受控参数，DEC-003），
        # 编排只负责跑它、流式输出进证据、把结果说成人话。
        #
        # ⚠️  / R4：“构建后必须有什么”是**目标**的知识（`Discovery.required_artifacts`），
        # 尺寸行的格式也是（`Discovery.size_pattern`）；这里以前写死参考工程的三样产物、
        # 它的尺寸正则、以及产物文件名的相对路径 —— 第二目标一个都过不去（）。
        # ⚠️  / R4：有的目标的"构建"其实是**两步**（先生成固件身份头与字典，
        #     再编译）。这个钩子与 `archive` 同一个形状：适配器没这一步就不发生。
        #     没有它的话，第二目标会拿一份**过期的身份头**去编译（自己宣布一个主机没有字典的号），
        #     而现象看起来像"解码坏了"（）。
        builder = self._adapter("build")
        prepare = getattr(builder, "prepare", None)
        if prepare is not None:
            prep_spec = prepare(
                adapters_mod.BuildRequest(pio_env=self.pio_env, project_dir=self.project_dir)
            )
            # ⚠️  / R4-B（N1）：适配器可以**声明**"没有这一步"（返回 None）——
            # 例如 RT-Thread 工程还没有 Token 机制，那就没有"生成身份头与字典"这回事。
            # 把它与"钩子不存在"分开说，但两种都不跑任何东西（不跑一条什么都没干的命令）。
            if prep_spec is not None:
                prep_code = self._run_spec(prep_spec, "build")
                if prep_code != 0:
                    raise StepFailure(
                        f"构建前的准备步骤失败（退出码 {prep_code}；命令由 {prep_spec.adapter_id} 给）"
                    )
        spec = builder.build(
            adapters_mod.BuildRequest(pio_env=self.pio_env, project_dir=self.project_dir)
        )
        code = self._run_spec(spec, "build")
        if code != 0:
            raise StepFailure(f"构建失败（退出码 {code}；命令由 {spec.adapter_id} 给）")
        for required, what in self.discovery.required_artifacts:
            if not Path(required).is_file():
                raise StepFailure(f"构建后未生成{what}：{required}")

        # 构建前 prepare 与链接后 finalize 是两个阶段，不能靠旧 ELF 是否存在来猜。
        finalize = getattr(builder, "finalize", None)
        if finalize is not None:
            final_spec = finalize(
                adapters_mod.BuildRequest(pio_env=self.pio_env, project_dir=self.project_dir)
            )
            if final_spec is not None:
                final_code = self._run_spec(final_spec, "build")
                if final_code != 0:
                    raise StepFailure(
                        f"构建后的产物清单收口失败（退出码 {final_code}；"
                        f"命令由 {final_spec.adapter_id} 给）"
                    )

        # 字典是**参考工程**的产物（Workbench token 字典）：没有字典的组合（例如还没有
        # 字典机制的第二目标）就没有这条判据 —— 空就是没有，不是"跳过一下而已"。
        dictionary = self.discovery.dictionary
        dictionary_text = ""
        entry_count: int | None = None
        if dictionary is not None and Path(dictionary).is_file():
            dictionary_text = Path(dictionary).read_text(encoding="utf-8", errors="replace")
            entry_count = len([line for line in dictionary_text.splitlines() if line.strip()])
            absent = [
                format_string
                for format_string in self.project.expected_format_strings
                if format_string not in dictionary_text
            ]
            if absent:
                raise StepFailure(
                    "构建字典不含固件声明的格式串，固件与字典可能不同步：" + "；".join(absent)
                )

        elf = Path(self.discovery.elf) if self.discovery.elf is not None else None
        binary = Path(self.discovery.binary) if self.discovery.binary is not None else None
        firmware: dict[str, Any] = {
            "project": self.project_dir.name,
            "pio_env": self.pio_env,
            "partitions": str(self.discovery.layout or ""),
        }
        if dictionary is not None and entry_count is not None:
            firmware["dictionary"] = Path(dictionary).name
            firmware["dictionary_sha256"] = _sha256_bytes(Path(dictionary).read_bytes())
            # Counted here so the machine-readable baseline (仓库根/baseline.json)
            # can compare the dictionary size of a later run without rebuilding it.
            firmware["dictionary_entries"] = entry_count
        if elf is not None and elf.is_file():
            firmware["elf"] = elf.name
            firmware["elf_sha256"] = _sha256_bytes(elf.read_bytes())
        if binary is not None and binary.is_file():
            firmware["bin"] = binary.name
            firmware["bin_bytes"] = binary.stat().st_size
            firmware["bin_sha256"] = _sha256_bytes(binary.read_bytes())

        # 尺寸行：格式是**目标工具**的（`Discovery.size_pattern`）；没给模式就不记尺寸。
        sizes: dict[str, dict[str, Any]] = {}
        if self.discovery.size_pattern:
            sizes = {
                match.group(1): {
                    "percent": float(match.group(2)),
                    "used_bytes": int(match.group(3)),
                    "total_bytes": int(match.group(4)),
                }
                for match in re.finditer(self.discovery.size_pattern, self._step_text("build"), re.MULTILINE)
            }
        if sizes:
            firmware["memory"] = sizes

        # 产物清单（参考工程的概念：构建时写下的那份自述）——没有就不记。
        if self.discovery.artifact_manifest is not None:
            try:
                firmware["pio_manifest"] = json.loads(
                    Path(self.discovery.artifact_manifest).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass

        run = self._run  # type: ignore[assignment]
        run["firmware"] = firmware
        # Archive this build before anything else can overwrite it: a later crash is
        # decoded against the archived ELF, found by hash (Phase 0 item 4, and
        # 04-verification §6.3 "保存/引用匹配的 ELF").
        # ⚠️  / R4：归档是**构建适配器**的事（产物名、目录布局、归档命名都是那套工具链的）。
        # 适配器没有这个能力（或它的目标没有"按构建号存档"这套东西）⇒ 返回 None，
        # 证据里就没有 `archive` 这一项（）。
        archiver = getattr(self._adapter("build"), "archive", None)
        if archiver is not None:
            try:
                archived = archiver(self.discovery, firmware, self.pio_env)
                if archived:
                    firmware["archive"] = archived
            except OSError as error:
                firmware["archive"] = {"error": repr(error)}
                self._emit("error", message=f"归档构建产物失败: {error}")
        self.devices.set_firmware(run["target"], {
            "build_id": (firmware.get("pio_manifest") or {}).get("build_id"),
            "git_commit": (firmware.get("pio_manifest") or {}).get("git_commit"),
            "elf_sha256": firmware.get("elf_sha256"),
            "bin_bytes": firmware.get("bin_bytes"),
            "source": f"run {run['run_id']}",
        })
        self._store_firmware_in_manifest(firmware)

        # 区段名的大小写是**目标工具**的（PlatformIO 打 `Flash`，RT-Thread 的 build.py 打 `FLASH`）：
        # 证据里原样保留，说明文字按名字不分大小写取（ 的第二半）。
        by_name = {name.casefold(): row for name, row in sizes.items()}
        ram = by_name.get("ram", {}).get("used_bytes")
        flash = by_name.get("flash", {}).get("used_bytes")
        # 步骤说明也是证据的一部分：参考目标说的还是那三样（RAM/Flash/字典条数），
        # 换一个没有这些概念的目标时，就如实说它产出了什么。
        if entry_count is None:
            produced = f"{binary.name} {binary.stat().st_size} B" if binary is not None and binary.is_file() \
                else "（目标没有声明固件镜像）"
            return f"构建成功；产物 {produced}"
        return "构建成功；RAM {} B，Flash {} B，字典 {} 条".format(
            ram if ram is not None else "?",
            flash if flash is not None else "?",
            entry_count,
        )

    def _store_firmware_in_manifest(self, firmware: dict[str, Any]) -> None:
        run_id = self._run["run_id"]  # type: ignore[index]
        try:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            manifest["firmware"] = firmware
            self.store.write_manifest(run_id, manifest)
        except (OSError, ValueError):
            pass

    def _record_clock_sent(self, when: str) -> None:
        """Record the clock the host actually sent, for the storage assertions."""
        run = self._run  # type: ignore[assignment]
        run["clock_sent"] = when
        run_id = run["run_id"]
        try:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            manifest.setdefault("run", {})["clock_sent"] = when
            self.store.write_manifest(run_id, manifest)
        except (OSError, ValueError):
            pass

    def _note_evidence(self, run: dict[str, Any], size: int) -> None:
        """记下"最近一次真的有原始字节进来"（页面显示"最近证据：N s 前"，）。

        ⚠️ 只认**原始字节**：设备还在说话，这是唯一直接的证据。事件流里的 `info`
        是主机自己写的 —— 拿它当"设备还在"，就会把"设备已经不响了"报成一切正常。
        长窗口（上限 240 s）尤其需要这一行：否则"还在安静地等"看着就像"卡死"。
        """
        run["observed_bytes"] = int(run.get("observed_bytes") or 0) + max(0, int(size))
        run["last_evidence_utc"] = utc_now()

    def _record_command_sent(self, command: str) -> None:
        """记下"主机到底发了哪条命令"（断言拿它判"有没有被回应"，不靠猜）。"""
        run = self._run  # type: ignore[assignment]
        sent = list(run.get("commands_sent") or []) + [str(command)]
        run["commands_sent"] = sent
        run_id = run["run_id"]
        try:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            manifest.setdefault("run", {})["commands_sent"] = sent
            self.store.write_manifest(run_id, manifest)
        except (OSError, ValueError):
            pass

    def _store_dictionary_in_manifest(self, selection: dict[str, Any]) -> None:
        """Record which dictionary decoded this capture, and why that one.

        The manifest is the run's identity document, so "which dictionary" belongs
        there: a decoded log without its dictionary is not reproducible.
        """
        run_id = self._run["run_id"]  # type: ignore[index]
        try:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            manifest["dictionary_used"] = selection
            self.store.write_manifest(run_id, manifest)
        except (OSError, ValueError):
            pass

    def _upload_failure_reason(self, text: str) -> str:
        """烧录**失败**日志 ⇒ 目标自己给的说明（给不出来就空着，不编）。

        同一个接缝（`verify_upload`）在两条路上都用：成功路上回答“写进去了吗”，
        失败路上回答“这是哪一种失败、下一步做什么”。
        """
        verifier = getattr(self._adapter("flash"), "verify_upload", None)
        if verifier is None:
            return ""
        try:
            ok, note = verifier(text)
        except Exception:  # pragma: no cover - 目标侧实现坏了不该掩盖烧录失败
            return ""
        return "" if ok else str(note or "")

    def _step_upload(self) -> str:
        run = self._run  # type: ignore[assignment]
        # esptool holds COM<n> for the duration of the upload, so mark ownership
        # explicitly; otherwise the port probe reports someone *else* is using it.
        self.devices.set_owner(run["target"], "webapp")
        try:
            spec = self._adapter("flash").upload(
                adapters_mod.UploadRequest(
                    pio_env=self.pio_env, port=run["port"], project_dir=self.project_dir
                )
            )
            code = self._run_spec(spec, "upload")
        finally:
            self.devices.set_owner(run["target"], None)
        text = self._step_text("upload")
        if code != 0:
            # ⚠️ 这里**过去只报退出码**（`烧录失败（退出码 1）`）—— 真正的线索
            # （`Fatal error: ST-Link Connection error`）躺在 `tool.log` 里没人翻，
            # 于是每失败一次都要从零重新推理一遍“这块探针怎么了”。2026-10-11 为这件事
            # 连烧 6 次：报错每次都不一样（四种），而处置只有一条。⇒ 失败**也要问一次目标**，
            # 让失败详情自己带着已知处置。
            reason = self._upload_failure_reason(text)
            raise StepFailure(f"烧录失败（退出码 {code}）" + (f"；{reason}" if reason else ""))
        # 写入确认的判据是**目标的知识**：参考目标是 esptool 的那句 `Hash of data verified`，
        # 而 C-SPY/IAR 不打印它。适配器给得出来就听它的（ / R4）。
        verifier = getattr(self._adapter("flash"), "verify_upload", None)
        if verifier is not None:
            verified, note = verifier(text)
            if not verified:
                raise StepFailure(note or "烧录日志里没有出现目标声明的成功判据，写入未被确认")
        elif "Hash of data verified" not in text:
            raise StepFailure("烧录结束但未出现 'Hash of data verified'，写入未被确认")
        self.devices.touch(run["target"], "烧录成功")
        return f"{run['port']} 烧录成功，写入已校验"

    def _step_observe(self) -> str:
        run = self._run  # type: ignore[assignment]
        run_id = run["run_id"]
        seconds = float(run["observe_seconds"])
        # Incremental durability: raw.bin / decoded.log are appended and flushed per
        # line, so an interrupted run still has the bytes it already saw. There is no
        # in-memory buffer of the whole capture any more (it also used to hold every
        # decoded line in a list nobody read -- unbounded memory in a long soak).
        decode_supported = self._target_supports("decode")
        sink = CaptureSink(
            self.store, run_id, decoded=decode_supported, cursor=getattr(self, "_evidence_cursor", None)
        )

        if decode_supported:
            try:
                decode_line = self._decoder().make_line_decoder(self.dictionary_path, self.token_domain)
            except Exception as error:  # pragma: no cover - defensive
                sink.close()
                raise StepFailure(f"加载解码器失败: {error!r}") from error
        else:
            decode_line = lambda line: line

        # ---- 与设备的**固定对话** -------------------------------------------------
        #
        # ⚠️  / R4（）：这一整段（等启动横幅 / 清记录 / 对时 / 取记录文件）
        # 以前写在这里，动词、命令字与设备的回答都是**参考固件**的知识。现在它是一份
        # **数据**（`device_dialogue.DeviceDialogue`，由项目包声明）：编排只做四件事 ——
        # 按 profile 的复位计划复位、等声明的横幅标记、按 run 的开关执行声明的交互、
        # 把声明的 `notice` 写进事件流。所以这里再也看不到任何具体命令字。
        dialogue = self._dialogue()
        seen: dict[str, bool] = {marker: False for marker in device_dialogue.markers(dialogue)}

        # ---  / T1：把本轮要做的交互声明变成"阶段"，先铺一遍 pending ---
        # ⚠️ 只登记**这一轮真的会做**的：门控没开的不是"还没跑"，而是"没被选中"，
        # 不该出现在卡片里（需求 §3.1）。旧声明没有 stage_id ⇒ 阶段表为空 ⇒ 一行都不记，
        # 行为与从前完全一样（这是向后兼容的那条路）。
        def _step_payload(payload: dict) -> dict:
            """`_emit(kind, **fields)` 的 kind 由调用处给 —— 事件里那份 `kind` 不重复传。"""
            return {key: value for key, value in payload.items() if key != "kind"}

        steps_recorder = step_lifecycle.TestStepRecorder(
            step_lifecycle.steps_from_exchanges(
                (
                    *device_dialogue.open_exchanges(dialogue),
                    *device_dialogue.close_exchanges(dialogue),
                ),
                gate_is_open=lambda item: device_dialogue.gate_is_open(item, run),
            )
        )
        # 补写事件用 `run_id + stage_id + version` 关联（用户 2026-10-09 口径），
        # 所以声明版本要在这一刻记下来 —— 声明改语义时 +1，旧补写就不会被误合到新语义上。
        run["stage_versions"] = {
            state.stage_id: int(state.step.version) for state in steps_recorder.states
        }
        for _pending in steps_recorder.snapshots(time.monotonic()):
            self._emit(step_lifecycle.TEST_STEP_KIND, **_step_payload(_pending))

        #  / D4：这一趟采集到的**每一行**留一份。核验**先**从这一趟的证据里取回答；
        # 只有"烧了设备、而采集里没有答案"时才会自己补问一次（为什么：见
        # `_verify_identity_after_flash` 的说明）。
        identity_lines: list[str] = []

        # 中心锚点（用户口径 2026-10-13）：阶段声明要等的那一行回话（`wait_for`，比如 `@TASKS`）
        # **实际**出现在哪一行 —— 那才是"足以判定本阶段结果的关键行"，比区间首行有信息量。
        # 记法 `marker_lines[标记][证据来源] = 1 基行号`；行号取自**各来源自己的**游标，
        # 绝不把一个窗的行号套到另一个窗（四窗各自定位）。
        marker_lines: dict[str, dict[str, int]] = {}

        def cursor_line(source: str) -> int:
            """某一类证据的当前行号（1 基）；没有游标（纯夹具）就是 0。"""
            cursor = getattr(self, "_evidence_cursor", None)
            if cursor is None:
                return 0
            return int(cursor.snapshot().get(source, 0) or 0)

        def note_marker(text: str, source: str, line: int) -> None:
            """第一次看见某个标记时记下它在**这一类证据**里的行号（行号由调用处给）。"""
            if line <= 0:
                return
            for marker in seen:
                if marker in text and not seen[marker]:
                    seen[marker] = True
                    marker_lines.setdefault(marker, {})[source] = int(line)

        def emit_raw(text: str, *, continuation: bool = False) -> None:
            # `emit_raw` 只在**完整行写进 raw.bin 之后**被调（`_LineCollector._emit` 里
            # `sink.raw_line` 先走）⇒ 游标的当前行号就是这一行。半行（`idle_flush`）不算一行，
            # 那时记到的是最后一条完整行 —— 宁可回退，不编一个行号。
            note_marker(text, "raw", cursor_line("raw"))
            identity_lines.append(text)
            self._emit("raw", line=text, continuation=continuation)

        def emit_decoded(text: str) -> None:
            if not decode_supported:
                return
            sink.decoded_line(text)
            # decoded 自己一份行号（与 raw 的那一份可能不同 —— 解码会改写/合并行）。
            note_marker(text, "decoded", cursor_line("decoded"))
            self._emit("decoded", line=text)

        collector = _LineCollector(emit_raw, emit_decoded, decode_line, sink=sink)

        # Deliberately do NOT assign dtr/rts here: doing so resets the board
        # (I-022). A reset is only performed when the run explicitly asked.
        #
        # ⚠️  / R4：串口实现也由装配处交进来（`PlatformPlugins.port_factory`）——
        # 编排以前自己 `import serial` 并 `serial.Serial()`，那是把 transport 实现
        # 钉在 core 里（DEC-004 要的是"可替换的 transport"）。参考组合交进来的那份
        # 打开时**不写 modem 线**（`devices.open_read_only`），这一条由它保证。
        self.devices.set_owner(run["target"], "webapp")
        #  / D4：烧录后身份核验的结论（写进步骤说明；核验不通过会抛 StepFailure）。
        identity_note = ""
        try:
            port = self._open_transport(run["port"])
        except Exception:
            # 打不开就把所有权还回去：不然设备卡会一直说"被 webapp 拿着"（而它并没有）。
            self.devices.set_owner(run["target"], None)
            sink.close()
            raise
        sent_clock = ""
        try:
            port.reset_input_buffer()
            if run["reset_pulse"]:
                self._emit(
                    "info",
                    message=(
                        "显式复位脉冲 RTS→EN（本次 run 请求了复位，"
                        "目的是捕获早期启动日志）"
                    ),
                )
                # ⚠️ 这段时序**会**复位两次，而且是故意的：赋 DTR 与赋 RTS 各自都会经过
                # 板上的自动复位电路（）。两件事挨着做时，第一次复位刚打出来的
                # 启动横幅会被第二次复位**从中间截断**，于是 raw.bin 的第一行是几个字节的
                # 碎片加一个半字符（实测形态：`ets Jun  8 20` + 0xf9 + 完整横幅）。
                # 它不影响任何断言，但读 raw 的人会以为捕获坏了。
                #
                # 结论（先只动 DTR → 等它把横幅打完 → 丢掉 → 再单独脉冲 RTS）现在住在
                # 目标层，是一份**数据**（`ResetPlan`）；编排只负责把它跑完（R3c）。
                self._apply_reset_plan(port)

            selected_tests = list(run.get("test_selections") or [])
            if selected_tests:
                transport = _CapturedLineTransport(port, collector, self, run_id)
                kinds = {str(row["case"].get("kind")) for row in selected_tests}
                if len(kinds) != 1:
                    raise StepFailure("同一轮暂不混跑设备自主用例与主机场景")
                if kinds == {"device"}:
                    selections = tuple(
                        TestSelection(
                            str(row["case"]["id"]), int(row["case"]["version"]),
                            dict(row.get("parameters") or {}),
                            bool(row.get("confirmed_destructive", False)),
                        )
                        for row in selected_tests
                    )
                    try:
                        completed = execute_device_tests(
                            package=self.project,
                            firmware_regions=self.firmware_regions,
                            selections=selections,
                            transport=transport,
                            store=self.store,
                            run_id=run_id,
                            cancelled=self._cancel.is_set,
                        )
                    except TestRunError as error:
                        raise StepFailure(f"AUTOTEST 设备自主用例失败：{error}") from error
                    run["tests"] = [row.to_json() for row in completed.results]
                elif kinds == {"host_scenario"}:
                    if len(selected_tests) != 1:
                        raise StepFailure("首个 Web 主机场景验收每轮只允许一个 host_scenario")
                    prepared = selected_tests[0]
                    case = prepared["case"]
                    file_keys = [
                        key for key, spec in (case.get("parameters") or {}).items()
                        if spec.get("type") == "file"
                    ]
                    if len(file_keys) != 1:
                        raise StepFailure("主机场景必须声明且只声明一个文件参数")
                    input_key = f"{case['id']}@{case['version']}:{file_keys[0]}"
                    input_bytes = (run.get("test_inputs") or {}).get(input_key)
                    if not isinstance(input_bytes, bytes):
                        raise StepFailure("本轮没有通过 Web 上传主机场景输入字节")
                    try:
                        result = execute_image_roundtrip(
                            protocol=self.project.test_protocol,
                            transport=transport,
                            store=self.store,
                            run_id=run_id,
                            prepared=prepared,
                            input_bytes=input_bytes,
                            cancelled=self._cancel.is_set,
                        )
                    except TestRunError as error:
                        raise StepFailure(f"AUTOTEST 主机场景失败：{error}") from error
                    run["tests"] = [result.to_json()]
                else:
                    raise StepFailure(f"不支持的 AUTOTEST 用例类型：{sorted(kinds)}")
                run["test_summary"] = {
                    "total": len(run["tests"]),
                    "passed": sum(row["status"] == "PASS" for row in run["tests"]),
                    "result": (
                        "PASS" if run["tests"] and all(row["status"] == "PASS" for row in run["tests"])
                        else next((row["status"] for row in run["tests"] if row["status"] != "PASS"), "ERROR")
                    ),
                }
                if run["test_summary"]["result"] == "CANCELLED":
                    raise RunCancelled()
                if run["test_summary"]["result"] != "PASS":
                    raise StepFailure(f"AUTOTEST 测试结果 {run['test_summary']['result']}")

            def pump(until_mono: float, until_marker: str = "") -> bool:
                """读到"看见某个标记"或"到点"为止（同时喂给证据与窗口）。"""
                while time.monotonic() < until_mono:
                    if self._cancel.is_set():
                        raise RunCancelled()
                    chunk = port.read(port.in_waiting or 1)
                    if chunk:
                        collector.feed(chunk)
                    else:
                        # 安静的尾巴也要显示出来（与实时观察同一条规则）：
                        # 设备的回显不带换行，等下一次换行就要等很久。
                        collector.idle_flush()
                    if until_marker and seen.get(until_marker):
                        return True
                return bool(until_marker) and bool(seen.get(until_marker))

            # ⚠️ 等启动横幅必须**最先**做：命令字节进不了还没开始收的 UART 驱动，
            # 而设备看起来一切正常（实测代价：整轮 fs.write 变 fs.skip，或 CLEAR 被丢掉）。
            # 等不等、等什么、等多久，都由那份对话声明。
            if run["reset_pulse"] and dialogue.banner_marker:
                if not pump(
                    time.monotonic() + float(dialogue.banner_timeout_s),
                    dialogue.banner_marker,
                ):
                    self._emit(
                        "info",
                        message=(
                            f"复位后 {dialogue.banner_timeout_s:.0f} s 内没等到设备自报启动"
                            f"（等的是 {dialogue.banner_marker!r}）：后面的命令可能被丢弃"
                        ),
                    )

            def speak(exchange: Any) -> None:
                """执行一次声明的交互（渲染模板 → 记证据 → 发 → 记事件 → 可选等回话）。"""
                nonlocal sent_clock
                if exchange.delay_seconds:
                    time.sleep(float(exchange.delay_seconds))
                text = device_dialogue.render_text(exchange.send)
                if exchange.record_clock:
                    # 把“主机到底发了什么时刻”写进 manifest。断言拿它和设备回报的
                    # “时间已同步: …”对比：不一致就说明这一轮有别的东西在往同一条串口上写
                    # 命令（或者设备中途重启了），那时整轮存储证据都不是我们以为的那回事。
                    sent_clock = text
                    self._record_clock_sent(text)
                port.write(text.encode("ascii", "replace") + b"\n")
                port.flush()
                if exchange.notice:
                    self._emit(
                        "info",
                        message=device_dialogue.render_text(
                            exchange.notice, values={"sent": text}
                        ),
                    )
                if exchange.wait_for and not pump(
                    time.monotonic() + float(exchange.wait_seconds), exchange.wait_for
                ):
                    self._emit(
                        "info",
                        message=device_dialogue.render_text(
                            exchange.miss_notice,
                            values={"wait_seconds": float(exchange.wait_seconds)},
                        ),
                    )

            if dialogue.note:
                self._emit("info", message=f"采集期间的固定对话来自 {dialogue.source}：{dialogue.note}")

            def run_stage(exchange: Any) -> None:
                """执行一次交互，并按声明把它的阶段推到终态（ / T1）。

                ⚠️ 成功/失败的口径**不在这里发明**：等到声明的回话 = `PASS`；
                没等到（`speak` 内部已经如实报了 `miss_notice`）= `FAIL`；
                执行中抛错 = `ERROR`。判"等到没有"用的是同一个 `seen` 表 ——
                即 `speak` 里 `pump()` 的那把尺子，不另立一套。
                没有 `stage_id` 的声明照原文跑，只是不做阶段记录。

                **应答重试（ 的应对方案）**：只读、幂等的命令可以在声明里给
                `retry_max` / `retry_gap_seconds` —— 没等到回话就隔一小会儿重发，任一次
                拿到完整响应就继续。⚠️ 策略由项目包声明（语法层已经拦住"有副作用的命令
                声明重试"），这里只执行通用循环；每次重发与原因都进事件流。
                ⚠️ 重试成功只说明**这一次拿到了完整响应**："帧完不完整"仍由 `RT5` 独立判定，
                两者不互相抵消。
                """
                stage_id = str(getattr(exchange, "stage_id", "") or "")
                if not stage_id:
                    speak(exchange)
                    return
                wait_for = str(getattr(exchange, "wait_for", "") or "")
                retry_max = max(0, int(getattr(exchange, "retry_max", 0) or 0))
                gap = float(getattr(exchange, "retry_gap_seconds", 0.0) or 0.0)
                attempts = 1 + retry_max
                # 阶段边界：从这一刻起的四类写入都算这一阶段的证据（T5）。
                stage_before = (
                    self._evidence_cursor.snapshot()
                    if getattr(self, "_evidence_cursor", None) is not None
                    else None
                )
                self._emit(
                    step_lifecycle.TEST_STEP_KIND,
                    **_step_payload(
                        steps_recorder.start(
                            stage_id,
                            detail=(f"等待 {wait_for}" if wait_for else "发送中"),
                            mono=time.monotonic(),
                            utc=utc_now(),
                        )
                    ),
                )
                arrived = False
                used = 0
                # 这一阶段等的那一行在**四类证据各自**的行号（没等到就是空表 ⇒ 退回区间末行）。
                anchors = marker_lines.get(wait_for, {}) if wait_for else {}
                try:
                    for attempt in range(1, attempts + 1):
                        used = attempt
                        speak(exchange)
                        arrived = (not wait_for) or bool(seen.get(wait_for, False))
                        if arrived or attempt >= attempts:
                            break
                        # 每次重发都要有**理由**落进证据：第几次、等的是什么、隔多久重来。
                        self._emit(
                            "info",
                            message=(
                                f"{exchange.purpose}：第 {attempt} 次没等到 {wait_for!r}，"
                                f"{gap:g} s 后重发（只读且幂等，最多重发 {retry_max} 次）"
                            ),
                        )
                        if self._cancel.is_set():
                            raise RunCancelled()
                        time.sleep(gap)
                except BaseException as error:
                    # 取消/断开/超时都要把卡片收口 —— 页面不留永久转圈（需求 §6.6）。
                    self._emit(
                        step_lifecycle.TEST_STEP_KIND,
                        **_step_payload(
                            steps_recorder.finish(
                                stage_id,
                                "ERROR",
                                detail=f"执行中断：{error!r}",
                                mono=time.monotonic(),
                                utc=utc_now(),
                                refs=self._record_stage_refs(
                                    run, stage_id, stage_before, anchors=anchors
                                ),
                            )
                        ),
                    )
                    raise
                if wait_for and not arrived:
                    detail = device_dialogue.render_text(
                        exchange.miss_notice,
                        values={"wait_seconds": float(exchange.wait_seconds)},
                    )
                    if used > 1:
                        detail += f"（已重发 {used - 1} 次）"
                    status = "FAIL"
                elif wait_for:
                    detail = f"已收到 {wait_for}"
                    if used > 1:
                        detail += f"（第 {used} 次发送才收到）"
                    status = "PASS"
                else:
                    detail = "已发送（不等回话）"
                    status = "PASS"
                self._emit(
                    step_lifecycle.TEST_STEP_KIND,
                    **_step_payload(
                        steps_recorder.finish(
                            stage_id,
                            status,
                            detail=detail,
                            mono=time.monotonic(),
                            utc=utc_now(),
                            refs=self._record_stage_refs(
                                run, stage_id, stage_before, anchors=anchors
                            ),
                        )
                    ),
                )

            for exchange in device_dialogue.open_exchanges(dialogue):
                if not device_dialogue.gate_is_open(exchange, run):
                    continue
                run_stage(exchange)

            probe = str(run.get("probe_command") or "")
            if probe:
                # 目标/套件声明的验证命令：发一次、记下来。回应与否交给断言判 ——
                # 编排不替固件“相信”它答了。
                port.write(probe.encode("ascii", "replace") + b"\n")
                port.flush()
                self._record_command_sent(probe)
                self._emit("info", message=f"已向设备发送验证命令：{probe}")

            deadline = time.monotonic() + seconds
            closing = device_dialogue.close_exchanges(dialogue)
            closed: set[int] = set()
            # 采集从这一刻开始：页面用它算"进行中 · 83 / 240 s"（）。
            run["observe_started_utc"] = utc_now()
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    raise RunCancelled()
                for index, exchange in enumerate(closing):
                    if index in closed:
                        continue
                    if not device_dialogue.gate_is_open(exchange, run):
                        closed.add(index)  # 这一轮不做它（也不留到下一轮反复问）
                        continue
                    if time.monotonic() >= deadline - float(exchange.before_end_seconds):
                        closed.add(index)
                        run_stage(exchange)
                chunk = port.read(port.in_waiting or 1)
                if chunk:
                    collector.feed(chunk)
                    self._note_evidence(run, len(chunk))
                else:
                    collector.idle_flush()
            # ----  / D4：烧录后的身份闭环 -------------------------------
            # ⚠️ 时机在**采集窗口结束之后、关端口之前**：设备这时一定在应答，
            #    而且这一趟的采集证据已经落盘 —— 核验不过也是**可回放的 FAIL**。
            #    它**先读证据**；只有"这一趟烧了设备、而采集里没有答案"时才会自己补问
            #    （需求 §4.5：烧录后必须通过通信口重新查询身份）。
            identity_note = self._verify_identity_after_flash(port, collector, identity_lines, run)
        finally:
            collector.flush()
            # 收尾：还挂在 running/pending 的阶段当场收敛（取消与出错分别报 CANCELLED / ERROR）。
            try:
                for _left in steps_recorder.converge(
                    detail=(
                        "run 被取消，这一阶段没有走完"
                        if self._cancel.is_set()
                        else "采集窗口结束，这一阶段没有走完"
                    ),
                    mono=time.monotonic(),
                    utc=utc_now(),
                    terminal="CANCELLED" if self._cancel.is_set() else "ERROR",
                ):
                    self._emit(step_lifecycle.TEST_STEP_KIND, **_step_payload(_left))
            except Exception:  # 收口失败不能盖住原来的错（那才是要看的）
                pass
            try:
                port.close()
            finally:
                self.devices.set_owner(run["target"], None)
            sink.close()

        self.devices.touch(run["target"], f"采集 {sink.bytes} B")
        # 四类证据的**总数**：decode 之后靠它校验"整块重写有没有改变行号"（T5 补写）。
        cursor = getattr(self, "_evidence_cursor", None)
        if cursor is not None:
            run["evidence_totals"] = cursor.snapshot()
        timeout_note = "" if sink.bytes else "（窗口内没有任何字节）"
        # ⚠️ 这一句只说"这一轮有没有对时"，不替任何项目解释它的存储语义。
        banner_ok = (not dialogue.banner_marker) or seen.get(dialogue.banner_marker, False)
        if sent_clock:
            sync_note = f"已对时 {sent_clock}" + (
                "" if banner_ok or not run["reset_pulse"] else "（但未等到启动横幅）"
            )
        else:
            sync_note = "未对时"
        return (
            f"{run['port']} 采集 {sink.bytes} B / {seconds:.0f} s {timeout_note}；"
            f"{sync_note}{identity_note}"
        ).strip()

    # -------------------------------------------  / D4：烧录后的身份闭环
    def _identity_declaration(self):
        """烧录后要问的那条身份查询（**全部来自声明**）。

        返回 `(命令, 行结束符, 回答前缀, 等待秒, 项目解析器或 None, 重发上限, 重发间隔秒)`。
        项目/目标没声明查询文本或回答前缀 ⇒ `None`：**“问不了”要如实说**，
        不能假装问过了（工程理念 §3）。
        """
        run = self._run  # type: ignore[assignment]
        spec = self.devices.get(str(run.get("target") or ""))
        command = str(getattr(self.project, "identity_query", "") or "").strip()
        if not command:
            # 平台协议自己定死了查询文本（`identity.IDENTITY_QUERY_TEXT`），项目包可以覆盖。
            command = identity.IDENTITY_QUERY_TEXT
        prefix = str(getattr(self.plugins, "identity_reply_prefix", "") or "")
        if not prefix:
            return None
        wait_s = max(1.0, float(getattr(self.project, "identity_query_delay_s", 0.0) or 0.0))
        # 烧录后补问时的重发上限与间隔（使用者口径：隔 3～5 s 重发、最多三次）。
        # ⚠️ 用 `None` 判"没声明"，别用 `or`：声明成 `0` 是**有意的**（例如"不要间隔"），
        #    被 `or` 吃掉就变回默认 4 s 了（主机测试里实测需要这一条）。
        attempts_raw = self._declared_option("identity_attempts")
        attempts = (
            min(sniff_mod.MAX_ATTEMPTS_PER_PROBE, max(1, int(attempts_raw)))
            if attempts_raw is not None
            else sniff_mod.MAX_ATTEMPTS_PER_PROBE
        )
        gap_raw = self._declared_option("identity_retry_gap_seconds")
        gap = IDENTITY_RETRY_GAP_SECONDS if gap_raw is None else max(0.0, float(gap_raw))
        return (
            command,
            str(getattr(spec, "line_ending", "") or "lf"),
            prefix,
            wait_s,
            getattr(self.plugins, "identity_parser", None),
            attempts,
            gap,
        )

    def _declared_option(self, name: str):
        """身份这一环的可调项：项目包先说，插件束后补，都没有 ⇒ `None`（用默认）。"""
        for holder in (self.project, self.plugins):
            value = getattr(holder, name, None)
            if value is not None:
                return value
        return None

    def _flashed_in_this_run(self) -> bool:
        """这一趟**真的烧了**设备没有（`upload` 跑完且不是 `skipped`）。

        ⚠️ 这条只影响一件事：**烧过就必须在烧录后重新查询并核验身份**（需求 §4.5/§4.6）。
        没烧的那一趟"采集里没出现身份回答"只是"这一环没量过"，不该判 FAIL ——
        两者混成一条，要么把开发用的零烧录路径全卡死，要么把烧完不核验当成通过。
        """
        for step in self._steps or ():
            if str(step.get("name") or "") == "upload":
                return str(step.get("status") or "") == "pass"
        return False

    def identity_query_declared(self) -> bool:
        """这一路装配 + 项目包**有没有**声明一条可问、可解的身份查询（ / §4.5）。

        ⚠️ 只看**回答前缀**：没有前缀就无法把设备回的那一行认出来（查询文本可以从
        平台默认退，前缀退不了）。这与 `_identity_declaration()` 的返回 None 条件同一条。
        """
        return bool(str(getattr(self.plugins, "identity_reply_prefix", "") or ""))

    def _flash_identity_refusal(self, suite: str) -> str | None:
        """这一趟要烧录、而目标没声明任何身份查询 ⇒ 拒绝的理由（否则 `None`）。

        `skip_flash` 的那一趟（跑在现有镜像上）**不受这条约束** —— 它没换掉板上的东西，
        所以"核验不了"只是这一环没量过，不是把设备置于不可核验的状态。
        """
        skip_flash = bool(self.project.run_default("skip_flash", False, suite=suite))
        if skip_flash or self.identity_query_declared():
            return None
        return (
            f"套件 {suite!r} 会烧录，但这个目标/项目没有声明任何身份查询（缺回答前缀）⇒ "
            "烧完就核验不了板上跑的是不是刚写进去的那一份。按  §4.6"
            "（烧录后身份无法确认时不得继续）**拒绝在烧录前开始**："
            "请给这个目标补一条身份声明（回答前缀 + 查询文本），"
            "或改用不烧录的套件（套件声明 `skip_flash`）。"
        )

    def _parse_identity_fields(self, parser, prefix: str, text: str) -> dict[str, Any]:
        """一段文本 ⇒ 身份字段（项目自带的解析器优先；都没有就是空字典，不编）。"""
        fields = (
            sniff_service.identity_from_project(parser, text)
            if parser is not None
            else sniff_service.parse_identity(text, prefix=prefix)
        )
        return fields if isinstance(fields, dict) and identity.fields_with_values(fields) else {}

    def _ask_identity_after_flash(
        self, port, collector, *, command: str, line_ending: str, prefix: str,
        wait_s: float, parser, attempts: int, gap: float,
    ) -> tuple[bool, dict[str, Any], str]:
        """烧录后**真的去问一次**身份（只在采集里没答案、且这一趟确实烧了设备时）。

        返回 `(问过没有, 字段, 回答原文)`。每次提问的 TX/RX 都进采集证据（`collector.feed`），
        命令也记进 `commands_sent`；重发之间隔 `gap`（连发会把设备的回答挤坏，真机踩过）。
        """
        payload = console_input.build_text_payload(command, line_ending)
        transcript = ""
        for attempt in range(1, max(1, int(attempts)) + 1):
            if attempt > 1 and gap > 0:
                time.sleep(gap)
            try:
                port.write(payload)
                port.flush()
            except Exception as error:  # noqa: BLE001 - 写不出去也是一条要说清的结论
                return True, {}, f"身份查询写不出去：{error}"
            self._record_command_sent(command)
            chunks: list[bytes] = []
            seen_bytes = 0
            deadline = time.monotonic() + max(0.0, float(wait_s))
            while time.monotonic() < deadline and seen_bytes < 4096:
                chunk = port.read(port.in_waiting or 1)
                if chunk:
                    chunks.append(bytes(chunk))
                    seen_bytes += len(chunk)
                    # 同一份字节也要进采集证据（不是只给核验看）。
                    collector.feed(chunk)
                else:
                    time.sleep(0.02)
            # ⚠️ 累积着解，不逐次覆盖：设备偶尔会把上次的回答拖到下一个窗口才发完，
            #    那样"第二次没答"其实只是"第一次答晚了"。
            transcript += b"".join(chunks).decode("utf-8", "replace")
            fields = self._parse_identity_fields(parser, prefix, transcript)
            if fields:
                return True, fields, transcript
        return True, {}, transcript

    def _verify_identity_after_flash(self, port, collector, lines, run) -> str:
        """ / D4：烧录后的身份核验（需求 §4.5/§4.6）。

        两条规矩同时成立，缺一条都会错得很难看：

        1. **有答案就别再问。** 什么时候问身份是**项目包**的契约（RT-Thread 的受控对话
           第一条就是 `pwinfo`，且排在所有写动作之前）；core 在窗口末尾无脑补发会撞上设备
           正在跑的序列 —— 2026-10-09 真机实测的答案是 `spwinfo: command not found.`
           （多出来的 `s` 是被日志洪流挤掉的）。那样"身份不对"与"命令没送到"就分不清了。
        2. **烧了就必须问出来。** 这一趟真的烧了设备、而采集里没有答案时，**必须**通过通信口
           重新查询（需求 §4.5）；还是拿不到就**停在身份核验阶段做成可回放 FAIL**（§4.6），
           不进入业务断言。
           ⚠️ 这一条是 2026-10-09 的验收复核补上的：空板首烧那一趟烧完只写了"未核验"就
           继续跑完断言 —— 那是把"没量"当成了"没事"。

        预期全部来自**声明**：项目名来自插件束，构建号来自这一趟的构建证据，
        必需协议来自冻结进 `device` 块的声明。拿不到构建号时它**不参与**比较
        （一个永远为真的字段比没有字段更糟）。
        """
        declaration = self._identity_declaration()
        flashed = self._flashed_in_this_run()
        if declaration is None:
            if flashed:
                # ⚠️ 这一条理论上不该被走到：`Pipeline.start()` 已经按 `_flash_identity_refusal()`
                # 把"会烧录 + 没声明身份查询"**在烧录前**拒掉了。留着它是**兵兵一着**：
                # 万一有别的入口（CLI / 将来的调用方）绕过了那道闸门、真把设备烧了，
                # 也绝不允许"烧完不能核验却接着跑业务断言"（ §4.6）。
                self._record_identity_after({
                    "state": sniff_mod.IDENTITY_UNQUERYABLE,
                    "reason": "这一趟烧了设备，但这个目标/项目没有声明任何身份查询（缺回答前缀）",
                    "source": "declaration",
                    "flashed": True,
                })
                raise StepFailure(
                    "烧录后身份核验未通过：这一趟烧了设备，但目标/项目没有声明任何身份查询"
                    "（缺回答前缀）⇒ 无法确认板上跑的是什么，按 §4.6 停在核验阶段"
                )
            # **没烧录**的那一趟（开发用的零烧录路径）不算 FAIL：它没换掉板上的东西，
            # "核验不了"只是这一环没量过。如实报"未核验"，并且写清"这不是核验通过"。
            self._record_identity_after({
                "state": sniff_mod.IDENTITY_UNQUERYABLE,
                "reason": "这个目标/项目没有声明任何身份查询（缺回答前缀）⇒ core 这一环核验不了；"
                          "这**不是**“核验通过”（这一趟没烧录，板上的东西没被换掉）",
                "source": "declaration",
                "flashed": False,
            })
            return "；**烧录后身份未核验**（没有声明身份回答的样式；这一趟没烧录）"
        command, line_ending, prefix, wait_s, parser, attempts, gap = declaration
        text = "\n".join(str(line) for line in lines)
        # "问过没有"看**回显**：设备会把收到的命令抄回来。看不到回显就只说"没问过"，
        # 绝不把"我们没量"报成"设备不对"。
        asked = bool(command) and command in text
        fields = self._parse_identity_fields(parser, prefix, text)
        source = "capture"
        if not fields and flashed:
            # 需求 §4.5：烧录后**必须**通过通信口重新查询身份。
            asked, fields, transcript = self._ask_identity_after_flash(
                port, collector, command=command, line_ending=line_ending, prefix=prefix,
                wait_s=wait_s, parser=parser, attempts=attempts, gap=gap,
            )
            if transcript:
                text = f"{text}\n{transcript}" if text else transcript
            source = "query-after-flash"
        answer_lines = [line for line in text.splitlines() if prefix and prefix in str(line)][-4:]
        device_block = run.get("run_device") if isinstance(run.get("run_device"), dict) else {}
        required = tuple(str(name) for name in (device_block.get("required_protocols") or ()))
        build = ((run.get("firmware") or {}).get("pio_manifest")) or {}
        check = sniff_mod.verify_identity(
            fields or None,
            expect_project=str(getattr(self.plugins, "project", "") or ""),
            expect_build_id=_build_id_of(build),
            required_protocols=required,
            # 设备侧自称的名字（声明在 profile 的 `identity_hint`）——裸机固件回的是
            # `"project":"stm32bm"`，与本机项目包键不同，但指的是同一份固件。
            project_aliases=tuple(getattr(self.plugins, "device_projects", ()) or ()),
        )
        state = sniff_mod.identity_state(
            sniff_mod.PROBE_OK if fields else sniff_mod.PROBE_NO_RESPONSE,
            matches_expectation=check.matched if fields else None,
        )
        # ⚠️ 四件事分清楚，别混成一条结论：
        #  · **没烧、也没问过** ⇒ "无法核验"，**不判 FAIL**（是这一环没量过）；
        #  · **烧了、问过、但拿不到合格回答** ⇒ **FAIL**（需求 §4.6：不许静默放行）；
        #  · **回答里没有可比的字段** ⇒ "无法核验"，**不判 FAIL**（是没标尺，不是设备不对）；
        #  · 拿到回答且不符 ⇒ **FAIL**（conflict，逐条列）。
        if not fields:
            if not asked and not flashed:
                state = sniff_mod.IDENTITY_UNQUERYABLE
        unverifiable = bool(fields) and not check.compared
        if unverifiable:
            state = sniff_mod.IDENTITY_UNQUERYABLE
        if not fields:
            if flashed and asked:
                reason = (
                    f"烧录后通过通信口重新查询了身份（{command!r}，{attempts} 次），"
                    f"但没有拿到符合声明（{prefix.strip()}）的回答 ⇒ 无法确认板上跑的是什么"
                )
            elif flashed:
                reason = "烧了设备，但没有可用的身份查询声明 ⇒ 无法完成烧录后核验"
            elif not asked:
                reason = f"这一趟的回显里没有那条身份查询（{command!r}）⇒ 没问过，不当作核验"
            else:
                reason = f"身份查询发出去了（收到回显），但没有拿到符合声明（{prefix.strip()}）的回答"
        elif unverifiable:
            reason = f"回答拿到了，但没有可以做预期的字段（{check.reason}）"
        else:
            reason = check.reason
        self._record_identity_after({
            "state": state,
            "state_reason": reason,
            # 结论是**从哪儿来的**：这一趟的采集里就有，还是烧完之后补问出来的。
            "source": source,
            "flashed": bool(flashed),
            "probe": {"command": command, "prefix": prefix, "asked": bool(asked),
                      "attempts": int(attempts), "gap_seconds": float(gap)},
            "answer_lines": answer_lines,
            "fields": dict(fields),
            "check": check.to_json(),
        })
        if check.matched:
            return f"；烧录后身份已核验（{check.reason}）"
        if state == sniff_mod.IDENTITY_UNQUERYABLE:
            return f"；**烧录后身份未核验**（{reason}）"
        raise StepFailure(f"烧录后身份核验未通过：{reason}")

    def _record_identity_after(self, block: dict[str, Any]) -> None:
        """把 D4 的结论写进 run manifest 的 `device.identity_after`（**只增字段**）。

        ⚠️ 没有 `device` 块的 run（老的 CLI / 不点名设备的路径）**不新造一块**：
        `recorded_target()` 只看“有没有 `device` 块”来判新旧，凭空造一块会让历史回放
        把一份旧 run 当成走过设备路径的。那种情况下结论只写在步骤说明里。
        """
        run_id = self._run["run_id"]  # type: ignore[index]
        try:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            device = manifest.get("device")
            if not isinstance(device, dict):
                return
            device["identity_after"] = dict(block)
            self.store.write_manifest(run_id, manifest)
        except (OSError, ValueError):
            pass

    def _step_decode(self) -> str:
        run = self._run  # type: ignore[assignment]
        run_id = run["run_id"]
        raw = self.store.artifact(run_id, "raw.bin").read_bytes()
        if not raw:
            raise StepFailure("raw.bin 为空，没有可解码的数据")

        # Dictionary selection happens *before* decoding, from the plain-text banner.
        # It has to: the build identity could not be tokenized without already knowing
        # the dictionary. If the banner is missing or names an unknown build we stop
        # here -- decoding with a plausible-looking dictionary is how you get output
        # that reads like a log and is fiction.
        #
        # ⚠️  / R4：**怎么读横幅**是那份字典库自己的事（它属于哪个项目/固件）。
        # 这里以前调的是参考项目的 `dictionary.read_build_banner` —— 换一个固件就会
        # 拿别人的横幅格式去解自己的捕获（）。
        library = self.library
        read_banner = getattr(library, "read_banner", None)
        if read_banner is None:
            raise StepFailure(
                "这份字典库不提供 read_banner（读不出构建身份就不能解码："
                "拿一个看起来像字典的东西去解会产出读起来像日志的虚构）"
            )
        device_build, banner_fields = read_banner(raw.decode("utf-8", "replace"))
        source = library.exact(device_build)
        if device_build is None:
            # ⚠️ 这里**不点名**任何横幅格式：读横幅的规则属于那份字典库（目标/项目），
            # 编排只知道"读不出 build id 就不能解码"。
            raise StepFailure(
                "UNKNOWN_BUILD: 捕获里没有明文构建身份（读不出 build id），拒绝猜字典。"
                "已知字典：" + "；".join(library.describe_available())
            )
        if source is None:
            raise StepFailure(
                f"UNKNOWN_BUILD: 没有 build 0x{device_build:08x} 对应的字典，"
                "拒绝用别的字典解码。已知字典：" + "；".join(library.describe_available())
            )

        decode_bytes = self._decoder().decode_bytes
        try:
            decoded = decode_bytes(raw, source.path, self.token_domain)
        except Exception as error:
            raise StepFailure(f"解码失败: {error!r}") from error
        self.store.write_decoded(run_id, decoded)

        selection: dict[str, Any] = {
            "path": str(source.path),
            "origin": source.origin,
            "build_id32": source.build_id32,
            "device_build_id32": device_build,
            "banner": banner_fields,
            "elf_sha256": source.elf_sha256,
            "entries": len(library.entries_for(source)),
            "exact_match": source.build_id32 == device_build,
        }
        run["dictionary_used"] = selection
        self._store_dictionary_in_manifest(selection)

        # Structured layer: parse the records exactly once, here. Assertions, the
        # page and any agent then read the same machine-readable events instead
        # of each re-matching log text.
        #
        #  / R3e：这一层是**项目**的事（记录格式的语义）。装配处没交项目包时
        # 就没有事件层 —— 跳过它、在报告里如实说"没有项目包"，而不是编出一堆事件。
        text = decoded.decode("utf-8", "replace")
        project = self.project
        if project.events is None:
            note = "；未装配项目包，没有结构化事件与指标" if not project.problems() else (
                "；项目包缺少：" + "、".join(project.problems())
            )
            self._emit("info", message=f"跳过结构化事件层{note}")
            run["events_summary"] = {
                "total": 0,
                "counts": {},
                "unparsed": 0,
                "errors": [],
                "warnings": [],
                "note": f"未装配项目包（{note.lstrip('；')}）",
            }
            run["metrics_series"] = None
            self._emit_stage_refs_supplement(run, decoded=decoded, events=None)
        else:
            events = project.events.parse(decoded)
            self.store.write_artifact(run_id, "app_events.jsonl", project.events.to_jsonl(events))
            # T5 补写：decode 之后才知道 decoded / app_events 在哪一行（用户 2026-10-09 口径）。
            self._emit_stage_refs_supplement(run, decoded=decoded, events=events)
            summary = project.events.summarize(events)
            run["events_summary"] = summary
            # 指标导出（Phase 6 / 路线图第四项）：这一步与 app_events.jsonl 同源，所以
            # “图上的数”和“事件里的数”不可能来自两次不同的解析。页面画的就是这一份。
            metric_series = (
                project.metrics.series(events) if project.metrics is not None else None
            )
            if metric_series is not None:
                self.store.write_artifact(
                    run_id, "metrics.jsonl", project.metrics.to_jsonl(metric_series)
                )
            run["metrics_series"] = metric_series
            self._emit(
                "app_events",
                events=[event.to_json() for event in events],
                summary=summary,
                metrics=metric_series,
            )

        # Crash evidence. A panic is plain ASCII, so it survives in decoded.log;
        # resolving it needs the ELF of *this* build, which is why the manifest
        # hash is passed in -- frames decoded against another binary are fiction.
        # Detection itself never needs the toolchain, and a clean run never
        # spawns addr2line at all.
        #
        # ⚠️  / R4：**崩溃解析也是目标层的实现**（复位码表 + 那个工具链的符号化），
        # 以前编排直接 `import panic`（）。没装配就**不做判定**：
        # 写一份“看起来验过了、没事”的空证据比不做更糟。
        parser = self._panic_parser()
        if parser is None:
            panic_summary = {
                "verdict": "unavailable",
                "kinds": [],
                "addresses": 0,
                "resolved": 0,
                "note": "这个组合没有装配崩溃解析器；这一轮不做崩溃判定",
            }
            run["panic"] = None
            run["panic_summary"] = panic_summary
            self._emit(
                "info",
                message="没有装配崩溃解析器：这一轮不做崩溃判定（不写 panic.json）",
            )
        else:
            manifest = self.store.read_json(run_id, "manifest.json") or {}
            firmware = manifest.get("firmware") or {}
            finding = parser.inspect(
                text,
                elf=self._firmware_elf(),
                addr2line=self._symbol_tool(),
                argv_builder=self._symbol_argv,
                expected_elf_sha256=firmware.get("elf_sha256"),
            )
            self.store.write_artifact(
                run_id,
                "panic.json",
                (json.dumps(finding, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            run["panic"] = finding
            panic_summary = parser.summarize(finding)
            run["panic_summary"] = panic_summary
            self._emit("panic", finding=finding, summary=panic_summary)

        line_count = len(text.splitlines())
        crash_note = ""
        if panic_summary.get("verdict") == "fault":
            crash_note = (
                f"；!! 检测到 {'/'.join(panic_summary['kinds'])}，"
                f"解析 {panic_summary['resolved']}/{panic_summary['addresses']} 帧"
            )

        # 设备存储的证据（Phase 5）：DUMP BIN 把记录文件的原始字节取回主机，
        # 由一个没有写过这个格式的实现（webconsole/record_file.py）解析。
        # 设备自己那句“丢弃了 N B 尾巴”只是一个声明；下面这份解析才是证据。
        storage_note = self._collect_record_file(run_id, text)
        return f"{line_count} 行文本，{len(events)} 条结构化事件{crash_note}{storage_note}"

    def _collect_record_file(self, run_id: str, text: str) -> str:
        """Reassemble and parse the device's record file; record it as evidence.

        Always writes ``records.json``, even when there is no dump to parse: an
        assertion that has to say "the device never answered" needs a file saying
        exactly that, and a missing file would look the same as a bug in this step.
        """
        records = self.project.records
        if records is None:
            self.store.write_artifact(
                run_id,
                "records.json",
                (
                    json.dumps(
                        {
                            "dump": {},
                            "parsed": None,
                            "csv_view": None,
                            "project": self.project.to_json(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            return "；项目包未提供记录文件解析器，本轮不核对 records.bin"
        dump = records.parse_dump(text)
        payload: dict[str, Any] = {
            "dump": {key: value for key, value in dump.items() if key != "data"},
            "parsed": None,
            "csv_view": None,
        }
        if dump["data"]:
            self.store.write_artifact(run_id, "records.bin", dump["data"])
            parsed = records.parse(dump["data"])
            payload["parsed"] = parsed
            payload["csv_view"] = records.parse_csv_view(text)
        self.store.write_artifact(
            run_id,
            "records.json",
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

        if not dump["found"]:
            return "；未取到记录文件（捕获里没有 BINF 行）"
        if dump["problems"]:
            return "；记录文件取回失败：" + "；".join(dump["problems"])
        parsed = payload["parsed"] or {}
        return (
            f"；记录文件 {len(dump['data'])} B / {len(parsed.get('records', []))} 条记录"
            f"（问题 {parsed.get('problem')}）"
        )

    def _step_assert(self) -> str:
        run = self._run  # type: ignore[assignment]
        run_id = run["run_id"]
        decoded = self.store.artifact(run_id, "decoded.log").read_bytes()
        manifest = self.store.read_json(run_id, "manifest.json") or {}
        # Check the dictionary that actually did the decoding, which is not always
        # this environment's build directory: selection may have picked an archived
        # one (that is the whole point of a build id).
        selected = (run.get("dictionary_used") or {}).get("path") or (
            manifest.get("dictionary_used") or {}
        ).get("path")
        dictionary_path = Path(selected) if selected else self.dictionary_path
        dictionary_text = (
            dictionary_path.read_text(encoding="utf-8", errors="replace")
            if dictionary_path.is_file()
            else ""
        )
        # Re-parse from the evidence file rather than trusting in-memory state, so
        # a verdict can always be reproduced from the run directory alone.
        project = self.project
        if project.assertions is None:
            raise StepFailure(
                "未装配项目包：没有判据表，无法对这次采集下结论"
                + ("（缺：" + "、".join(project.problems()) + "）" if project.problems() else "")
            )
        events = project.events.parse(decoded) if project.events is not None else []
        run["events_summary"] = (
            project.events.summarize(events)
            if project.events is not None
            else {"total": 0, "counts": {}, "unparsed": 0, "errors": [], "warnings": []}
        )
        finding = None
        panic_path = self.store.artifact(run_id, "panic.json")
        if panic_path.is_file():
            try:
                finding = json.loads(panic_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                finding = None
        # The baseline lives in the repository, is updated on purpose, and is never
        # rewritten by a run (see 基线模块). Reading it here keeps evaluate()
        # free of file IO, like the dictionary and the decoded log before it.
        dictionary_entries = (manifest.get("firmware") or {}).get("dictionary_entries")
        if not isinstance(dictionary_entries, int):
            dictionary_entries = len(
                [line for line in dictionary_text.splitlines() if line.strip()]
            )
        # A31 核的是**落盘的导出物**，不是内存里的对象：一个 run 的结论必须能从 run 目录
        # 单独重算（与 panic.json 同样的理由）。
        metrics_path = self.store.artifact(run_id, "metrics.jsonl")
        metrics_artifact = (
            project.metrics.from_jsonl(metrics_path.read_bytes())
            if metrics_path.is_file() and project.metrics is not None
            else None
        )
        results = project.assertions.evaluate(
            decoded=decoded,
            events=events,
            manifest=manifest,
            dictionary_text=dictionary_text,
            observe_seconds=float(run["observe_seconds"]),
            panic=finding,
            baseline=(
                project.baseline.load(project.baseline.DEFAULT_PATH)
                if project.baseline is not None
                else None
            ),
            dictionary_entries=dictionary_entries,
            metrics_export=metrics_artifact,
        )
        run["assertions"] = results
        for row in results:
            self._emit("assert", assertion=row)
        summary = project.assertions.summarize(results)
        run["assertion_summary"] = summary
        # 按**结论域**分开念："证据与日志健康"与"业务结论"是两件事，混成一句会把
        # 日志被截断读成业务坏了。域由判据自己声明（`domain`），这里不替谁猜；
        # ⚠️ 分域**不放宽**任何判据：总分照旧是 `summary`，失败行照旧在 `results` 里。
        run["assertion_domains"] = assertion_groups.by_domain(results)
        if summary["failed"]:
            raise StepFailure(
                f"{summary['failed']}/{summary['total']} 项断言失败：" + "、".join(summary["failed_ids"])
            )
        return f"{summary['passed']}/{summary['total']} 项断言通过"

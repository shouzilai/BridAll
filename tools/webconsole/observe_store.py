"""实时观察会话的落盘：一个会话一个只追加目录（Phase 7.1 切片 2）。

```text
observe-sessions/<session-id>/
  manifest.json     会话、设备、端口/波特率、开始/结束时间、结束原因与计数、身份与字典匹配
  raw.bin           设备发来的**完整 RX 字节**（逐块追加 + flush）
  io_events.jsonl   连接状态、RX 索引（offset/length）、TX 字节与身份事件
  decoded.log       由 raw.bin + 字典推导（切片 5；字典不命中就不创建）
  app_events.jsonl  由 decoded.log 推导（切片 5）
```

三条不变量，每一条都有宿主断言守着：

1. **`raw.bin` 里只有设备说过的话。** TX 与人工标记一律进 `io_events.jsonl`
   （需求 §5.1）。一旦把 TX 混进 `raw.bin`，"这串字节是设备输出"这句话就不成立了 ——
   而它是整条证据链的根。
2. **先写字节，再写指向它的事件。** 任何时刻都满足 `len(raw.bin) >= offset + length`。
   反过来写的话，"事件说这里应该有 37 B"就只是一个承诺，而不是事实。
3. **逐块 flush。** 进程被杀也留下已经看到的字节（与 run 的 `CaptureSink` 同一个理由：
   最值得看的恰恰是没能正常结束的那一次）。

时间用**单调时钟**（`t_mono_us`）建立 TX/RX 的先后关系，同时写一份主机墙钟供人阅读：
墙钟可能被自动校时改动，单调时钟不会。

⚠️ 用 `time.perf_counter()` 而**不是** `time.monotonic()`：Windows 上后者的粒度约
15.6 ms，写成 µs 只是给了一个假的精度（真机验收时看到两个相邻的 rx 事件 `t_mono_us`
完全相同就是这个原因）。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
try:  # imported as a package (``webconsole.observe_store``)
    from .core_primitives import utc_now
except ImportError:  # executed as a plain script
    from core_primitives import utc_now  # type: ignore[no-redef]

SESSION_DIR_NAME = "observe-sessions"
# 导入的诊断包落在**另一个**目录（切片 8）：别人给的包不冒充本机采集的证据。
# 两个目录的读写都用同一套路径规则（`artifact_path`），区别只在根位置。
IMPORT_DIR_NAME = "observe-imports"
# 导入目录里那份“这个包是怎么来的”的记录。它**不是**证据（不进下面的白名单）：
# 证据是设备说的话，这一份是主机对“字节从哪来”的交代。
IMPORT_RECORD_NAME = "import.json"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
# 会话目录里对外暴露的文件。
# `decoded.log` / `app_events.jsonl` 是**可派生产物**（需求 §5.1），切片 5 起随会话生成：
# 实时解码与自动验证一样，字典**精确命中才写**；没命中就不创建这两个文件
# （一个空的 decoded.log 会被读成"设备什么都没说"）。
SESSION_ARTIFACTS = ("manifest.json", "raw.bin", "io_events.jsonl", "decoded.log", "app_events.jsonl")
# 会话开始时就应该存在的文件（派生产物按需创建，不在这个列表里）。
SESSION_MANDATORY_ARTIFACTS = ("manifest.json", "raw.bin", "io_events.jsonl")#: 哪些是**派生产物**（由 raw.bin + 当时的字典重算出来的，不是设备说的话）。
#: 放在这里是因为它描述的就是上面那份文件集合；页面里另有一份同样的名单（只用于标签），
#: 而**详情/搜索这条路只认这一份** —— 两处漂了的话，“哪些能搜/角色是什么”就会各说各话。
SESSION_DERIVED_ARTIFACTS = ("decoded.log", "app_events.jsonl")

def new_session_id(sessions_dir: Path, device_key: str) -> str:
    """`20260926-202531-live-a` 这样的会话号；同一秒里第二个会话加 `-2`。"""
    stamp = utc_now().replace("-", "").replace(":", "").replace("T", "-")[:15]
    base = f"{stamp}-live-{device_key or 'dev'}"
    session_id = base
    counter = 2
    while (sessions_dir / session_id).exists():
        session_id = f"{base}-{counter}"
        counter += 1
    return session_id


def artifact_path(sessions_dir: Path, session_id: str, name: str) -> Path:
    """会话证据的路径解析（与 `RunStore.artifact` 同样的越界校验）。"""
    if not SESSION_ID_PATTERN.match(session_id or ""):
        raise ValueError(f"非法会话号: {session_id!r}")
    if name not in SESSION_ARTIFACTS:
        raise ValueError(f"不暴露的会话文件: {name!r}")
    root = Path(sessions_dir).resolve()
    directory = (root / session_id).resolve()
    if directory.parent != root:
        raise ValueError(f"会话号越出目录: {session_id!r}")
    target = (directory / name).resolve()
    if target.parent != directory:
        raise ValueError(f"会话证据路径非法: {name!r}")
    return target


class ObserveSessionStore:
    """把一个实时观察会话写进它自己的目录。"""

    def __init__(
        self,
        sessions_dir: Path,
        session_id: str,
        *,
        device: dict[str, Any],
        port: str,
        baud: int,
        capabilities: dict[str, bool] | None = None,
        platform: dict[str, Any] | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.session_id = session_id
        self.directory = artifact_path(self.sessions_dir, session_id, "manifest.json").parent
        self.directory.mkdir(parents=True, exist_ok=False)
        self.raw_path = self.directory / "raw.bin"
        self.events_path = self.directory / "io_events.jsonl"
        self.manifest_path = self.directory / "manifest.json"
        self.decoded_path = self.directory / "decoded.log"
        self.app_events_path = self.directory / "app_events.jsonl"
        self._raw = self.raw_path.open("ab", buffering=0)
        self._events = self.events_path.open("a", encoding="utf-8", newline="\n")
        # 派生产物按需打开（切片 5）：字典没命中时它们**不存在**，宁可没有，
        # 也不要一个空的 decoded.log 被人读成"设备什么都没说"。
        self._decoded: Any = None
        self._app_events: Any = None
        # `perf_counter` 是这里唯一可用的高分辨率单调时钟（见模块文档）。
        self._t0 = time.perf_counter()
        # ⚠️ 事件是从**两个线程**写进来的（读取线程写 rx、命令写入线程写 tx）。
        # 没有这把锁，`seq` 可能重复或乱序 —— 而"seq 与单调时钟建立 TX/RX 先后关系"
        # 正是需求 §5.2 要的东西。
        self._lock = threading.RLock()
        self._seq = 0
        self.raw_offset = 0
        self.rx_bytes = 0
        self.rx_chunks = 0
        # TX 计数属于**会话**（重连不清零，见 ）。
        self.tx_bytes = 0
        self.tx_messages = 0
        self.events = 0
        self.decoded_bytes = 0
        self.decoded_lines = 0
        self.app_events = 0
        #  `raw.bin` 的**行数**（切片 5 没有这一项：字节数不能当行号用）。
        #  由读取线程每处理完**一整行**加一（`count_raw_line()`）—— 与 raw.bin 里换行切出来
        #  的行逐行对齐，而且**与页面上的行号是同一个口径**（半行不算一行）。
        #  受控测试的阶段坐标靠它（）：raw 那一类证据的行号就是它。
        self.raw_lines = 0
        self.closed = False
        self.manifest = {
            "schema": 1,
            "session_id": session_id,
            "mode": "live",
            "device": device,
            "port": port,
            "baud": baud,
            # 这两条是**声明**：它们由切片 1 的宿主测试（替身串口计数 dtr/rts 赋值）
            # 与静态契约共同保证，写在这里是为了让读证据的人不必去翻代码。
            "read_only": True,
            "modem_lines_touched": False,
            "opened_utc": utc_now(),
            "closed_utc": None,
            "end_reason": None,
            "host": {
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "python_executable": sys.executable,
            },
            "capabilities": dict(capabilities or {}),
            # 平台自述（R2d / DEC-010）：这是哪套组合、能力、适配器 ID、按哪个 schema 写的。
            # 写进 manifest 的好处是**诊断包自己带出去**，不必另外附一份说明。
            "platform": dict(platform or {}),
            "totals": {},
            "artifacts": list(SESSION_ARTIFACTS),
        }
        self._write_manifest()
        self.note("connect", port=port, baud=baud, device=device.get("key"))

    # ------------------------------------------------------- 派生产物（切片 5）
    def open_decoded(self) -> Any:
        """开始写 `decoded.log`（字典精确命中之后才调用）。"""
        with self._lock:
            if self._decoded is None:
                self._decoded = self.decoded_path.open("a", encoding="utf-8", newline="\n")
            return self._decoded

    def open_app_events(self) -> Any:
        """开始写 `app_events.jsonl`。"""
        with self._lock:
            if self._app_events is None:
                self._app_events = self.app_events_path.open("a", encoding="utf-8", newline="\n")
            return self._app_events

    def write_decoded_line(self, line: str) -> int:
        """追加一行解密后的文本（逐行 flush，与 raw.bin 同一个理由）。返回写入的字节数。

        `decoded_lines` 数的是**写进文件的换行**（）：它对外表示"这一份证据现在有
        几行"—— 受控测试阶段的 `decoded` 坐标、实时行事件的 `line_no` 都用它，而
        `decoded.log` 的权威行数就是按换行切出来的行数（`test_core_layer` 也是这么核的）。
        解码出的文本里若自带换行，文件里就是两行；按调用次数数会少算一行。
        """
        with self._lock:
            handle = self._decoded or self.open_decoded()
            text = line if line.endswith("\n") else line + "\n"
            handle.write(text)
            handle.flush()
            written = len(text.encode("utf-8"))
            self.decoded_bytes += written
            self.decoded_lines += text.count("\n")
            return written

    def write_app_event(self, event: Any) -> None:
        """追加一条结构化事件（与自动验证的 `app_events.jsonl` 同一种行）。"""
        with self._lock:
            handle = self._app_events or self.open_app_events()
            handle.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
            handle.flush()
            self.app_events += 1

    def note_identity(
        self, *, state: str, source: str, fields: dict[str, Any] | None,
        archive: dict[str, Any] | None, detail: str = "", announce: bool = True,
    ) -> dict[str, Any] | None:
        """把身份结论记进 manifest（与 TX/RX 分开：它不是字节，是一个**声明**）。

        ⚠️ 身份一确定就调它（而不是只等会话结束）：进程被杀在半路时，
        manifest 里仍然说得清"这是哪台设备、用的哪份字典"。
        `announce=False` 用于会话结束时只刷新 manifest、不再往时间线上多记一条。
        """
        with self._lock:
            self.manifest["identity"] = {
                "state": state,
                "source": source,
                "detail": detail,
                "fields": dict(fields or {}),
                "archive": dict(archive or {}) if archive else None,
            }
            self._write_manifest()
        if not announce:
            return None
        return self.note(
            "identity",
            state=state,
            source=source,
            build_id=(fields or {}).get("build_id"),
            dictionary=(archive or {}).get("origin") if archive else None,
        )

    # ------------------------------------------------------------------ writes
    def note(self, kind: str, **fields: Any) -> dict[str, Any]:
        """追加一条 io 事件（连接状态、RX 索引、TX 字节、人工说明）。"""
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "t_mono_us": self._mono_us(),
                "host_time": utc_now(),
                "kind": kind,
            }
            event.update(fields)
            self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events.flush()
            self.events = self._seq
            return event

    def note_live_tests(self, block: dict[str, Any]) -> None:
        """把受控测试的**冻结块**写进 manifest（收口时写一次）。

        ⚠️ 放 manifest 而不只放事件流：回放要按**当时那一版定义**解释
        （用例目录会随项目包改版），而事件流里只有过程。
        """
        with self._lock:
            self.manifest["live_tests"] = dict(block)
            self._write_manifest()

    def write_tx(
        self,
        payload: bytes,
        *,
        source: str,
        display: str,
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """记录**实际写进串口的字节**（需求 §5.2）。

        ⚠️ TX 不进 `raw.bin`：那里只有设备说的话。它的位置是 `io_events.jsonl`，
        与 RX 共用一条 seq 时间线。

        `keys` 只对终端直输有意义（哪些按键按出了这段字节）—— 它是**说明书**，
        `bytes_hex` 才是证据：两者不一致时，以字节为准。
        """
        with self._lock:
            self.tx_bytes += len(payload)
            self.tx_messages += 1
            fields: dict[str, Any] = {
                "source": source,
                "bytes_hex": " ".join(f"{byte:02X}" for byte in payload),
                "display": display,
                "length": len(payload),
            }
            if keys:
                fields["keys"] = list(keys)
            return self.note("tx", **fields)

    def write_rx(self, data: bytes) -> int:
        """先写字节、再写事件；返回这段字节在 `raw.bin` 里的起始偏移。"""
        if not data:
            return self.raw_offset
        with self._lock:
            offset = self.raw_offset
            self._raw.write(data)
            self.raw_offset = offset + len(data)
            self.rx_bytes += len(data)
            self.rx_chunks += 1
            self.note("rx", raw_offset=offset, length=len(data))
            return offset

    def count_raw_line(self) -> int:
        """读取线程每处理完 **一整行** 调一次 ⇒ `raw.bin` 的行数（返回新值）。

        口径：与"按换行切 raw.bin"得到的那一串逐行对齐 —— 安静时先显示出去的**半行**
        不算一行（它还会接上后面的字节），所以它与页面上那一列行号是同一套数。
        受控测试的阶段坐标（`raw` 那一类）就是问它要行数（）。
        """
        with self._lock:
            self.raw_lines += 1
            return self.raw_lines

    def evidence_totals(self) -> dict[str, int]:
        """四类证据**各自**的行数（阶段坐标的换算基准，key 与 `EVIDENCE_SOURCES` 同名）。

        ⚠️ `tool` 这一项是 `io_events.jsonl` 的**行数**（每条事件一行）—— 会话的「工具输出」
        窗口是那份文件的一个**派生视图**（跳过 RX 分块事件、离线回看还多一行标题），
        所以它**不作为会话阶段的 `tool` 坐标**使用（见 `step_lifecycle.SESSION_REF_SOURCES`）。
        留在这里是因为它确实是一个**行数事实**（核对证据时要用），不是为了凑第四个坐标。
        """
        with self._lock:
            return {
                "tool": self.events,
                "raw": self.raw_lines,
                "decoded": self.decoded_lines,
                "app_events": self.app_events,
            }

    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.note(
            "disconnect",
            reason=reason,
            rx_bytes=self.rx_bytes,
            rx_chunks=self.rx_chunks,
        )
        self.manifest["closed_utc"] = utc_now()
        self.manifest["end_reason"] = reason
        self.manifest["totals"] = {
            "rx_bytes": self.rx_bytes,
            "rx_chunks": self.rx_chunks,
            "tx_bytes": self.tx_bytes,
            "tx_messages": self.tx_messages,
            "raw_bytes": self.raw_offset,
            # 行数（）：与 `raw.bin` 按换行切出来的行数对齐 —— 受控测试的阶段坐标
            # 用的就是行号，只记字节数核不出来。
            "raw_lines": self.raw_lines,
            "io_events": self.events,
            "decoded_bytes": self.decoded_bytes,
            "decoded_lines": self.decoded_lines,
            "app_events": self.app_events,
        }
        self._write_manifest()
        for handle in (self._raw, self._events, self._decoded, self._app_events):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass

    # ----------------------------------------------------------------- helpers
    def _mono_us(self) -> int:
        return int((time.perf_counter() - self._t0) * 1_000_000)

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def read_events(session_dir: Path) -> list[dict[str, Any]]:
    """读回一个会话的 io 事件（供回看与宿主自测）。"""
    path = Path(session_dir) / "io_events.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows

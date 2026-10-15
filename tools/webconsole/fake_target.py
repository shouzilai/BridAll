"""core 层专用的**假目标**（ / R1）：一个不是 ESP32、也没有串口的设备。

这是 R1 验收要的那个东西：一个**不含** ESP32、DHT、LittleFS、PlatformIO、也没有
任何固定串口的 target，用注入的替身 transport、替身字典和替身解码器，走通
**最小会话 → 身份 → 原始证据 → 解码状态**这一整条 core 路径。

它的价值是"跑不通就说明核心偷偷绑定了参考目标"：

- 端口名是 `FAKE0`，不是任何 `COMx`；探测结果是注入的，**不碰 Windows 的零共享探测**；
- 字典是一份手写的 CSV，不经平台构建、没有 `.pio` 目录、没有归档；
- 解码器是注入的 `decoder_factory`，不需要 Workbench Python 包，也不查任何工具链；
- 没有构建/烧录/复位/符号化：profile 里这几项能力**明确声明为 `unsupported`**
  —— 它们不伪装成 `supported`，也不留成 `unknown` 让人猜；
- **它自己一行都不 import 目标层**：设备登记用本模块的最小替身
  （`FakeDeviceSpec` / `FakeRegistry`），因此 core 套件可以做到零目标依赖。

⚠️ 它不是产品功能，也不进任何证据链：只有宿主测试用它，且一律写在临时目录里
（真证据目录 `observe-sessions//` 一个字节都不该多出来）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping

try:  # imported as a package (``webconsole.fake_target``)
    from .archive import ArchiveIndex
    from .observe import ObserveSession
    from .platform_plugins import PlatformPlugins
    from .target_profile import (
        Capability,
        Capabilities,
        ResetPolicy,
        TargetProfile,
        TransportSpec,
    )
except ImportError:  # executed as a plain script
    from archive import ArchiveIndex  # type: ignore[no-redef]
    from observe import ObserveSession  # type: ignore[no-redef]
    from platform_plugins import PlatformPlugins  # type: ignore[no-redef]
    from target_profile import (  # type: ignore[no-redef]
        Capability,
        Capabilities,
        ResetPolicy,
        TargetProfile,
        TransportSpec,
    )

FAKE_TARGET_ID = "fake-loop"
FAKE_DEVICE_KEY = "fake"
#: 刻意**不是**串口名：核心要能连一个"想叫什么就叫什么"的 transport。
FAKE_PORT = "FAKE0"
FAKE_BAUD = 115200
#: 手写的构建号（与任何真实固件无关），用来验证"精确选库"那一步。
FAKE_BUILD_ID32 = 0x1BADB002
#: 手写字典里的 domain。⚠️ **这就是假目标自己的项目 domain**（R2b 之后它由假目标声明，
#: 经 `fake_plugins().token_domain` 交给 core；core 里再没有 "示例项目" 这个词）。
FAKE_DOMAIN = "fake"


#: 与目标层的 `devices.ACCESS_FREE` **同值**。替身刻意不 import 目标层，
#: 所以这两个常量相等这件事由 `test_target_profile.py` 钉住。
FAKE_ACCESS_FREE = "free"


# ======================================================= 假目标的设备登记与 profile
@dataclass(frozen=True)
class FakeDeviceSpec:
    """设备登记的最小替身：字段名与目标层的真登记表一致（由契约测试钉住）.。

    会话核心只从登记表读这几个字段（`key`/`label`/`port`/`role`/`boundary`/`line_ending`
    /`terminal_*`），并调用 registry 的 `get`/`specs`/`set_owner`/`touch`。
    """

    key: str
    label: str
    port: str
    role: str
    pipeline_allowed: bool
    boundary: str
    line_ending: str = "crlf"
    terminal_backspace: str = "del"
    terminal_local_echo: bool = False
    #: 能力声明（与真登记表同一种类型、同一个意思）：CONTRACT / R2a 之后它就是
    #: "能力"的**唯一**声明处，会话的四项能力与 profile 都从这里派生。
    capabilities: Capabilities = field(default_factory=Capabilities)


#: 假目标的能力声明：**四条 support，其余 unsupported**（不是 unknown）。
#: 它才真正"做不到"那几件事（没有构建链、没有复位线、没有工具链、也没有项目 schema）。
FAKE_CAPABILITIES = Capabilities.of(
    build=Capability.UNSUPPORTED,
    flash=Capability.UNSUPPORTED,
    reset=Capability.UNSUPPORTED,
    transport=Capability.SUPPORTED,
    symbolize=Capability.UNSUPPORTED,
    decode=Capability.SUPPORTED,
    identity=Capability.SUPPORTED,
    evidence=Capability.SUPPORTED,
    metrics=Capability.UNSUPPORTED,
)


class FakeRegistry:
    """设备登记表的最小替身（只实现会话核心真的会用到的那几个方法）。"""

    def __init__(self, specs: Iterable[FakeDeviceSpec]) -> None:
        self._specs = list(specs)
        self._owner: dict[str, str | None] = {spec.key: None for spec in self._specs}
        self.notes: list[tuple[str, str | None]] = []

    def get(self, key: str) -> FakeDeviceSpec | None:
        for spec in self._specs:
            if spec.key == key:
                return spec
        return None

    def specs(self) -> list[FakeDeviceSpec]:
        return list(self._specs)

    def set_owner(self, key: str, owner: str | None) -> None:
        self._owner[key] = owner

    def touch(self, key: str, note: str | None = None) -> None:
        self.notes.append((key, note))

    def owner(self, key: str) -> str | None:
        return self._owner.get(key)


def device_spec(
    key: str = FAKE_DEVICE_KEY,
    port: str = FAKE_PORT,
) -> FakeDeviceSpec:
    return FakeDeviceSpec(
        key=key,
        label="假目标",
        port=port,
        role="core 层替身（不是真实设备）",
        pipeline_allowed=False,
        boundary="没有构建/烧录/复位：只提供一条字节流",
        line_ending="lf",
        terminal_backspace="del",
        terminal_local_echo=False,
        capabilities=FAKE_CAPABILITIES,
    )


def profile(spec: FakeDeviceSpec | None = None) -> TargetProfile:
    """假目标的能力声明：**四条 support，其余 unsupported**（不是 unknown）。"""
    spec = spec or device_spec()
    return TargetProfile(
        target_id=FAKE_TARGET_ID,
        label=spec.label,
        platform="fake",
        project=None,
        transport=TransportSpec(
            kind="fake",
            port=spec.port,
            baud=FAKE_BAUD,
            adapter="fake.memory-loopback",
            read_only=True,
            line_ending=spec.line_ending,
            terminal_backspace=spec.terminal_backspace,
            terminal_local_echo=spec.terminal_local_echo,
        ),
        reset=ResetPolicy(
            on_open="never",
            pulse="unsupported",
            settle_s=None,
            adapter=None,
            note="假目标没有复位线：这里不是「暂时不做」，而是**做不到**。",
        ),
        capabilities=spec.capabilities,
        build_adapter=None,
        flash_adapter=None,
        symbol_adapter=None,
        suites=(),
        token_domain=FAKE_DOMAIN,
        pipeline_allowed=False,
        device_key=spec.key,
        boundary=spec.boundary,
        notes=(
            "存在的意义：证明 core 会话路径不依赖参考目标（参考芯片 / 串口 / 构建链 / 项目字典）。",
            "它不产生任何产品行为，也不进证据链 —— 只有 core 契约测试用它。",
            f"`token_domain={FAKE_DOMAIN}` 是假目标自己的 domain；但 core 今天还不会把它"
            "传给解码器（写死项目 domain，见边界清单的 `live_decode → token 解码模块`），"
            "所以替身解码器把传进来的 domain 记下来供契约测试断言。",
        ),
    )


# ================================================================ 替身 transport
class FakePort:
    """替身 transport：记下"有没有动过设备"（modem 线）和写出去的字节。"""

    def __init__(self, name: str = FAKE_PORT, baud: int = FAKE_BAUD) -> None:
        self.name = name
        self.baud = baud
        self.opened = 0
        self.closed = 0
        self.modem_writes = 0      # dtr/rts 被赋值几次（真机上每次都可能有副作用）
        self.written = b""
        self.chunks: list[bytes] = []
        self.flushes = 0
        self.reads = 0
        self._queue: list[bytes] = []
        self._dtr: Any = None
        self._rts: Any = None

    # --- modem 控制线：任何赋值都计数 --------------------------------------
    @property
    def dtr(self) -> Any:
        return self._dtr

    @dtr.setter
    def dtr(self, value: Any) -> None:
        self.modem_writes += 1
        self._dtr = value

    @property
    def rts(self) -> Any:
        return self._rts

    @rts.setter
    def rts(self, value: Any) -> None:
        self.modem_writes += 1
        self._rts = value

    # --- 生命周期 -----------------------------------------------------------
    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def write(self, data: bytes) -> int:
        self.written += bytes(data)
        self.chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    # --- 喂数据 -------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self._queue.append(bytes(data))

    @property
    def in_waiting(self) -> int:
        return sum(len(chunk) for chunk in self._queue)

    def read(self, size: int = 1) -> bytes:
        self.reads += 1
        if not self._queue:
            time.sleep(0.005)  # 真串口这里是 timeout，不是忙等
            return b""
        chunk = self._queue.pop(0)
        if size > 0 and len(chunk) > size:
            self._queue.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


# ============================================================ 替身字典与替身解码
@dataclass(frozen=True)
class FakeDictionarySource:
    path: Path
    build_id32: int
    origin: str
    elf_sha256: str


class FakeDictionaryLibrary:
    """只回答"这个 build id 有没有字典"；不认识的一律 None（和真归档一样拒绝猜）。"""

    def __init__(self, sources: Iterable[FakeDictionarySource] = ()) -> None:
        self.sources = list(sources)

    def exact(self, build_id32: int | None) -> FakeDictionarySource | None:
        for source in self.sources:
            if build_id32 is not None and source.build_id32 == build_id32:
                return source
        return None

    def entries_for(self, source: FakeDictionarySource) -> list[tuple[int, str]]:
        return list(read_dictionary(source.path).items())

    def describe_available(self) -> list[str]:
        return [source.origin for source in self.sources]

    def read_banner(self, text: str) -> tuple[int | None, dict[str, str]]:
        """读**明文**启动横幅（假目标的线格式，与真固件同一个形状）。

        ⚠️ 这是"读构建身份"那一半契约（ / R4）：编排只要求字典库能
        `read_banner()`，具体怎么读是**这份库所属的目标/项目**的事。
        """
        for line in str(text).splitlines():
            stripped = line.strip()
            if not stripped.startswith("BOOT "):
                continue
            fields: dict[str, str] = {}
            build: int | None = None
            for token in stripped[5:].split():
                if "=" not in token:
                    continue
                key, _, value = token.split("=", 1)
                fields[key] = value
                if key == "build" and value.startswith("0x"):
                    try:
                        build = int(value, 16)
                    except ValueError:
                        build = None
            return build, fields
        return None, {}


def write_dictionary(path: Path, entries: Mapping[int, str] | None = None) -> Path:
    """写一份**手写**的 tokens.csv（列顺序与 Workbench 提取器一致：token,_,domain,fmt）。"""
    rows = entries or {0x0000A001: "fake boot protocol=%u"}
    text = "".join(
        f"{token:08x},,{FAKE_DOMAIN},\"{fmt}\"\n" for token, fmt in rows.items()
    )
    Path(path).write_text(text, encoding="utf-8")
    return Path(path)


def read_dictionary(path: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split(",", 3)
        if len(parts) < 4:
            continue
        try:
            token = int(parts[0], 16)
        except ValueError:
            continue
        out[token] = parts[3].strip().strip('"')
    return out


def token_line(token: int, arguments: str = "") -> bytes:
    """一行"token 化"的日志（假目标的线格式，不是 Workbench 的 Base64）。"""
    return f"$TKN{token:08x}{arguments}\n".encode("utf-8")


def make_decoder(dictionary_path: Path, domain: str) -> Callable[[bytes], bytes]:
    """替身解码器：与真解码器**同一种契约**（一行进、一行出、行终止符保留）。

    ⚠️ 它刻意**不按 domain 过滤** —— 假目标的手写字典里只有一个 domain。
    但"core 传进来的 domain 是哪个"是可观察的：`FakeRig.domains_seen` 记下了它，
    契约测试要求它**等于假目标自己声明的 domain**（R2b 之前 core 把项目的 domain
    示例项目 硬写在 `live_decode.TOKEN_DOMAIN` 里，所以那时这个值是 "示例项目"）。
    """
    table = read_dictionary(dictionary_path)

    def decode_line(line: bytes) -> bytes:
        text = line.rstrip(b"\r\n")
        terminator = line[len(text):]
        if not text.startswith(b"$TKN"):
            return line
        try:
            token = int(text[4:12], 16)
        except ValueError:
            return line
        fmt = table.get(token)
        if fmt is None:
            return line
        rendered = fmt
        for value in text[12:].decode("utf-8", "replace").split():
            rendered = rendered.replace("%u", value, 1).replace("%s", value, 1)
        return rendered.encode("utf-8") + terminator

    return decode_line


# ================================================================== 身份与样例行
def identity_line(build_id32: int = FAKE_BUILD_ID32, **overrides: Any) -> bytes:
    """一行 `@PWINFO {...}`（平台协议，字段与真设备同构；缺的字段就是**不提供**）。"""
    fields: dict[str, Any] = {
        "schema": 1,
        "mcu": "fake-mcu",
        "project": FAKE_TARGET_ID,
        "software_version": "fake-0.1.0",
        "build_id": f"0x{build_id32:08x}",
        "protocols": {"token": 1},
    }
    fields.update(overrides)
    return ("@PWINFO " + json.dumps(fields, ensure_ascii=False)).encode("utf-8") + b"\n"


def banner_line(build_id32: int = FAKE_BUILD_ID32) -> bytes:
    """一行明文启动横幅（旧设备的身份回退通道）。"""
    return (
        f"BOOT build=0x{build_id32:08x} fw={FAKE_TARGET_ID}-0.1.0 protocol=1 reset_reason=1\n"
    ).encode("utf-8")


def plain_line(text: str = "plain text, no token") -> bytes:
    return (text + "\n").encode("utf-8")


# =========================================================== 假项目的"记录解析器"
FAKE_RECORD_PREFIX = "record"


@dataclass(frozen=True)
class FakeEvent:
    """最小事件对象：与项目包的 `AppEvent` 同一种对外形状（只要 `to_json()`）。"""

    kind: str
    fields: dict[str, Any]
    seq: int
    offset: int

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "seq": self.seq, "offset": self.offset, **self.fields}


def parse_record_line(text: str, *, seq: int = 0, offset: int = 0) -> FakeEvent | None:
    """假**项目**的记录解析器：只认 `record <name> key=value …` 这种行。

    ⚠️ 它不是平台能力，是**项目替身**：core 只能通过 `event_parser` 注入拿到它
    （R2b 之前 core 的 `live_decode` 直接 `import app_events`）。"""
    body = str(text or "").strip()
    if not body.startswith(FAKE_RECORD_PREFIX + " "):
        return None
    parts = body.split()
    kind = parts[1] if len(parts) > 1 else "record"
    fields: dict[str, Any] = {}
    for token in parts[2:]:
        # ⚠️ 刻意不用 `str.partition()`：那个标识符会被平台标识扫描当成
        # “分区表”（`partition`）命中 —— core 套件的扫描是给依赖看的，别给它添噪音。
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        fields[name] = value
    return FakeEvent(kind=kind, fields=fields, seq=seq, offset=offset)


def record_line(kind: str = "sample", **fields: Any) -> bytes:
    """一行假项目的记录（用来验证"事件确实由注入的项目解析器产生"）。"""
    tail = "".join(f" {name}={value}" for name, value in fields.items())
    return (f"{FAKE_RECORD_PREFIX} {kind}{tail}\n").encode("utf-8")


def fake_plugins(
    *,
    registry: FakeRegistry | None = None,
    on_domain: Callable[[str], None] | None = None,
    on_event: Callable[[FakeEvent], None] | None = None,
    unsupported_reply: Callable[[str], bool] | None = None,
) -> PlatformPlugins:
    """假目标的**装配**：core 需要的一切都由假目标自己提供（一行都不 import 目标层）。

    与参考装配的差别正好体现了 R2b 的价值：

    - `token_domain` 是 `fake`（不是 示例项目）：domain 由**目标/项目**声明；
    - `event_parser` 是假项目自己的 `parse_record_line`（不是 示例项目 的 `app_events`）；
    - `unsupported_reply` **故意不给**："设备说不认识某条命令"的文本属于设备，
      所以一个没有这种回答的假目标就不该有这个状态（core 里现在也没有那条规则）；
    - 没有构建链 / 工具链 / 项目 schema（能力表里都是 `unsupported`）。
    """

    def _decoder_factory(path: Path, domain: str) -> Callable[[bytes], bytes]:
        if on_domain is not None:
            on_domain(domain)
        return make_decoder(Path(path), domain)

    def _event_parser(text: str, *, seq: int = 0, offset: int = 0) -> FakeEvent | None:
        event = parse_record_line(text, seq=seq, offset=offset)
        if event is not None and on_event is not None:
            on_event(event)
        return event

    return PlatformPlugins(
        label="fake-loop",
        platform="fake",
        project="fake-project",
        device_registry=registry,
        port_probe=lambda _port: FAKE_ACCESS_FREE,
        decoder_factory=_decoder_factory,
        decoder_probe=lambda: True,   # 假解码器始终可用（不依赖 Workbench 子模块）
        event_parser=_event_parser,
        token_domain=FAKE_DOMAIN,
        unsupported_reply=unsupported_reply,
    )


# ===================================================================== 装配（rig）
class FakeRig:
    """一条假目标的装配：真实的 `ObserveSession` + 全部注入点都换成替身。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        identity_wait_s: float = 0.05,
        dictionary: Mapping[int, str] | None = None,
        build_id32: int = FAKE_BUILD_ID32,
        unsupported_reply: Callable[[str], bool] | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.ports: list[FakePort] = []
        self.probed: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.domains_seen: list[str] = []
        self.events_parsed: list[FakeEvent] = []
        self.dictionary_path = write_dictionary(
            self.sessions_dir.parent / "fake-tokens.csv", dictionary
        )
        self.archive = ArchiveIndex(
            library_factory=lambda: FakeDictionaryLibrary(
                [
                    FakeDictionarySource(
                        path=self.dictionary_path,
                        build_id32=build_id32,
                        origin=f"手写字典 {self.dictionary_path.name}",
                        elf_sha256="0" * 64,
                    )
                ]
            )
        )
        self.spec = device_spec()
        self.registry = FakeRegistry((self.spec,))
        # 假目标自己的装配（R2b）：domain、项目事件解析器、解码器全部由它提供，
        # core 只负责把它们接到自己的接口上 —— 没有它们就照实降级。
        self.plugins = fake_plugins(
            registry=self.registry,
            on_domain=self.domains_seen.append,
            on_event=self.events_parsed.append,
            unsupported_reply=unsupported_reply,
        )
        self.session = ObserveSession(
            registry=self.registry,
            port_factory=self._make_port,
            probe=self._probe,
            emit=self._emit,
            sessions_dir=self.sessions_dir,
            imports_dir=self.sessions_dir.parent / "observe-imports",
            archive_index=self.archive,
            plugins=self.plugins,
            identity_wait_s=identity_wait_s,
        )

    # ---------------------------------------------------------------- 装配点
    def __enter__(self) -> "FakeRig":
        return self

    def __exit__(self, *exception: Any) -> bool:
        """离开作用域时断开连接 —— 否则临时目录删不掉（Windows 不肯删还在打开的文件）。"""
        try:
            self.session.disconnect(reason="测试收尾")
        except Exception:  # 收尾失败不影响已经得到的结论
            pass
        return False

    def _make_port(self, name: str, baud: int) -> FakePort:
        port = FakePort(name, baud)
        self.ports.append(port)
        return port

    def _probe(self, port: str) -> str:
        self.probed.append(port)
        return FAKE_ACCESS_FREE

    def _emit(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})

    # ------------------------------------------------------------------ 便捷
    @property
    def port(self) -> FakePort:
        return self.ports[-1]

    def emitted(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("kind") == kind]

    def session_id(self) -> str:
        """当前（或刚结束的）会话号 —— 走公开接口，不翻私有字段。"""
        return str(self.session.active_session_id() or "")

    def session_dir(self) -> Path:
        return self.sessions_dir / self.session_id()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
        """等一个条件成立（RX 是另一个线程干的，别用 sleep 猜时间）。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

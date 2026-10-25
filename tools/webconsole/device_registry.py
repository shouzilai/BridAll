"""设备登记表与串口占用状态（**core**， / R2b）。

这一层只回答两个问题，而且两个答案都由**注入**决定：

1. **有哪些目标**（"板 A / 板 B"这种具体角色不是平台知识，是参考目标的配置）；
2. **某个串口现在什么状态**（`free` / `busy` / `absent` / `unknown`）—— 探测实现
   （Windows 零共享 `CreateFileW`）属于目标层，core 只认一个返回这四个字符串的函数。

⚠️ **core 不猜端口状态**：没装配探测函数时返回 `unknown`，而不是"大概是空闲吧"。
`unknown` 与 `free` 在界面上是两种状态（`idle` vs `unknown`），不会被混起来 ——
一个"永远说空闲"的探测比没有探测更危险（它会让我们在别人正拿着串口时照样去打开）。

R2b 之前这些类型住在 `devices.py`（目标层），core 的 `observe.py` 为了一个登记表容器
和一个字符串常量反向依赖目标层（`observe → devices`，见边界清单 §4）。现在实现反转：
core 提供**通用容器**，参考目标在 `devices.py` 里把它接到真实探测上（`DEVICES` + 探针）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable

try:  # imported as a package (``webconsole.device_registry``)
    from .core_primitives import utc_now
    from .target_profile import Capabilities
except ImportError:  # executed as a plain script
    from core_primitives import utc_now  # type: ignore[no-redef]
    from target_profile import Capabilities  # type: ignore[no-redef]

#: 串口占用状态的四个取值。**只有这四个**：调用方要么拿到其中之一，要么拿不到
#: （探测函数自己抛异常时上层按"探测失败"处理，不在这里编一个状态）。
ACCESS_FREE = "free"
ACCESS_BUSY = "busy"
ACCESS_ABSENT = "absent"
ACCESS_UNKNOWN = "unknown"


def unknown_access(port: str) -> str:
    """core 的默认探测：**不知道**。

    ⚠️ 刻意不返回 `ACCESS_FREE` —— "没法判定"和"空闲"是两件事（ 的同族教训：
    一个永远通过的判据比没有判据更糟）。
    """
    return ACCESS_UNKNOWN


def no_description(port: str) -> str:
    """core 的默认端口描述：空串（"不知道这是什么设备"，不编一个名字）。"""
    return ""


@dataclass(frozen=True)
class DeviceSpec:
    """一个可连接目标的静态描述（平台不认识的字段由目标/项目自己解释）。

    这里只有**平台通用**的字段；设备/项目自己的约定（行结束符、退格字节、本地回显）
    也是数据，不是代码分支 —— 所以它们以普通字段的形式带着，平台不解释它们
    （`console_input.normalize_*` 只做白名单校验）。
    """

    key: str
    label: str
    port: str
    role: str
    pipeline_allowed: bool
    boundary: str
    # 命令框默认的行结束符。**属于设备/项目配置**（需求 §8）：平台不该硬编码一个
    # "通用"约定 —— 不同固件/RTOS shell 的行约定不一样。板 A 的固件按 `\n` 收行。
    line_ending: str = "crlf"
    # 终端直输的退格键发哪个字节（需求 §4.2 要能发退格）：
    #   `del` = 0x7F（现代终端），`bs` = 0x08（嵌入式 console 常见）。
    terminal_backspace: str = "del"
    # 这条串口**就是一个控制台终端**（设备/项目自己声明）：会话快照里的
    # `terminal.projection` 由它派生。默认 false —— 平台不把“串口”猜成“终端”。
    terminal_console: bool = False
    # 本地回显是**显示层**（需求 §4.2）：设备已经在回显时默认关掉，免得字符重复。
    terminal_local_echo: bool = False
    #: 这个目标**具备哪些能力**（ / R2a）：全平台**唯一**一处能力声明。
    #: `TargetProfile.capabilities` 带出去的就是**这个对象本身**（不是副本）；
    #: 实时观察的四项会话能力（send/identity/decode/evidence）也由它派生
    #: —— 见 `target_profile.session_capabilities()`。
    #: 没声明的能力是 `unknown`，而 `unknown` 一律按**不支持**处理（绝不冒充支持）。
    capabilities: Capabilities = field(default_factory=Capabilities)


class DeviceRegistry:
    """设备身份 + 编排器自己那份"谁拿着这个口"的账。

    `probe` / `describe` 都是**注入**的（R2b）：core 不 import 任何探测实现。
    参考目标在 `devices.DeviceRegistry` 里把默认值接到零共享探测上。
    """

    def __init__(
        self,
        specs: tuple[DeviceSpec, ...] = (),
        *,
        probe: Callable[[str], str] | None = None,
        describe: Callable[[str], str] | None = None,
    ) -> None:
        self._specs = list(specs)
        self._probe = probe or unknown_access
        self._describe = describe or no_description
        # “同一瞬间同一个口只探一次”用（）：`_probes` 是正在探的（口 -> 完成信号），
        # `_results` 是那一下的结果（口名数量有限，不清理也无所谓）。
        self._lock = threading.Lock()
        self._probes: dict[str, threading.Event] = {}
        self._results: dict[str, str] = {}
        self._runtime: dict[str, dict[str, Any]] = {
            spec.key: {"owner": None, "last_activity_utc": None, "firmware": None}
            for spec in self._specs
        }

    def specs(self) -> list[DeviceSpec]:
        return list(self._specs)

    def register(self, spec: DeviceSpec) -> DeviceSpec:
        """登记一台**运行时发现的**设备（幂等：同键返回既有那一条）。

        ⚠️ 这是给“系统里枚举到、但台架没点名”的**通用串口**用的（需求 §6.11/§6.12）：
        它们不是适配目标（没有别名、没有能力声明），但**必须能连** —— 登记之后，
        连接状态机、串口 owner、收发与证据路径全部复用同一套，不另写一份。
        """
        with self._lock:
            for existing in self._specs:
                if existing.key == spec.key:
                    return existing
            self._specs.append(spec)
            self._runtime.setdefault(
                spec.key, {"owner": None, "last_activity_utc": None, "firmware": None}
            )
            return spec

    def by_port(self, port: str) -> DeviceSpec | None:
        """按通信口找登记（大小写不敏感）。

        ⚠️ **不猜**：同一个口挂着两条登记时返回 `None`，由调用方如实拒绝。
        """
        wanted = str(port or "").strip().upper()
        if not wanted:
            return None
        hits = [
            spec
            for spec in self._specs
            if str(spec.port or "").strip().upper() == wanted
        ]
        return hits[0] if len(hits) == 1 else None

    def get(self, key: str) -> DeviceSpec | None:
        for spec in self._specs:
            if spec.key == key:
                return spec
        return None

    def set_owner(self, key: str, owner: str | None) -> None:
        self._runtime[key]["owner"] = owner

    def set_firmware(self, key: str, firmware: dict[str, Any] | None) -> None:
        self._runtime[key]["firmware"] = firmware

    def touch(self, key: str, note: str | None = None) -> None:
        self._runtime[key]["last_activity_utc"] = utc_now()
        if note is not None:
            self._runtime[key]["last_note"] = note

    def set_activity(self, key: str, iso_utc: str | None, note: str | None = None) -> None:
        """Restore a historical activity stamp (used when seeding from runs)."""
        self._runtime[key]["last_activity_utc"] = iso_utc
        if note is not None:
            self._runtime[key]["last_note"] = note

    def access(self, port: str) -> str:
        """这个串口现在什么状态（探测失败就是"不知道"，不是"空闲"）。

        ⚠️ **同一瞬间，同一个口只探一次**（）。探测是"独占打开一瞬"，两次探测
        撞在一起时后来者会拿到"拒绝访问"，于是把**我们自己**判成"被其他进程占用"：
        实测 3 线程各 300 次探同一个没人用的口 ⇒ `busy` 600/900，
        并发 4×40 次 `GET /api/state` ⇒ 设备卡 `busy` **153/160**。
        而 `/api/state` 每 2 秒把所有设备都探一遍（每个标签页各自探），precheck 在另一个
        线程也探同一个口 —— 撞上就拒绝一个**本来能跑的 run**。

        并发调用共享同一份结果（最多差几毫秒），而假阳性的代价是一次真机验证被拒。
        """
        key = str(port or "")
        with self._lock:
            waiting = self._probes.get(key)
            mine = waiting is None
            if mine:
                waiting = threading.Event()
                self._probes[key] = waiting
        if not mine:
            waiting.wait(timeout=5.0)
            return self._results.get(key, ACCESS_UNKNOWN)
        try:
            raw = str(self._probe(port))
        except Exception:
            raw = ACCESS_UNKNOWN
        result = raw if raw in (ACCESS_FREE, ACCESS_BUSY, ACCESS_ABSENT) else ACCESS_UNKNOWN
        self._results[key] = result
        with self._lock:
            self._probes.pop(key, None)
        waiting.set()
        return result

    def snapshot(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in self._specs:
            runtime = self._runtime[spec.key]
            access = self.access(spec.port)
            owner = runtime["owner"]
            if owner == "webapp":
                state = "held"
            elif owner == "observe":
                # 实时观察**读**着这个口（打开时不写 dtr/rts，所以设备不会被复位）。
                # 与 "held"（自动验证正在 build/upload/采集）分开报：页面要能说清
                # “现在是谁在用它、会不会动设备”。
                state = "observing"
            elif access == ACCESS_BUSY:
                state = "busy"
            elif access == ACCESS_FREE:
                state = "idle"
            elif access == ACCESS_ABSENT:
                state = "offline"
            else:
                state = "unknown"
            rows.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "port": spec.port,
                    "role": spec.role,
                    "boundary": spec.boundary,
                    "pipeline_allowed": spec.pipeline_allowed,
                    "line_ending": spec.line_ending,
                    "description": self._describe(spec.port),
                    "state": state,
                    "access": access,
                    "owner": owner,
                    "last_activity_utc": runtime["last_activity_utc"],
                    "last_note": runtime.get("last_note"),
                    "firmware": runtime["firmware"],
                }
            )
        return rows

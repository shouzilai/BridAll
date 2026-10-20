"""多目标实时观察的**路由**（**core**，W5 / 2026-09-28）。

页面上的"实时观察"以前只有一条路：参考目标（ESP32）那一套。R4 之后服务同时登记三份
固件（ESP32 示例项目、STM32 裸机、STM32 RT-Thread），它们**各自的设备登记、字典、解码器、
身份规则、能力表都不一样**，所以"观察哪一个"必须是一个**显式选择**，而不是平台替用户猜。

这个模块做的事只有一件：**按 firmware 把 `ObserveSession` 分派出去**。

```text
ObserveCenter
  ├─ sessions[firmware_id]   ← 装配处给的 N 份会话（各带自己的 registry / plugins）
  ├─ select(firmware_id)     ← 页面切目标（**一次只能有一个在握串口**，否则 409）
  ├─ connect/…/identity      ← 全部转给"当前选中的那一份"
  ├─ port_conflict(port)     ← 自动验证的闸门：**逐份**问，不只看当前选中的
  └─ snapshot()              ← 选中那一份的快照 + firmware_id + targets[]
```

四条规矩（每一条都有宿主回归钉着）：

1. **一次只连一个设备。** 这是既有边界（需求 §3.2）：不做多设备并行。所以
   `select()` 在**另一份会话正握着串口**时拒绝，并在理由里点名是谁握着 ——
   隐式地把别人的串口断掉是"平台替用户做决定"，那比拒绝更糟。
2. **不许借用别的固件的字典/解析器。** 每条会话拿到的是**它自己**的插件束；
   center 只做分派，从不把 A 的 registry 交给 B，也从不把 A 的能力表当成 B 的。
   于是 RT-Thread 那一路的 `decode` 如实是 `unsupported`（项目包没给字典），
   页面上显示的是"没装配"，而不是"用裸机的字典解出看着合理的内容"。
3. **闸门必须盖住所有装配。** 每份 `Pipeline` 都拿这个 center 当自己的 `observe`，
   于是"串口被实时观察占着"这条 409 判据对三份固件**同时**成立（以前只有参考那一份有）。
4. **快照向后兼容。** 页面已经在读的键（`state` / `options` / `capabilities` /
   `identity` / `decode` / `commands` / `terminal` …）原样保留 —— 选中的那一份自己产生，
   center 只外加 `firmware_id` 与 `targets[]` 两个键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Iterable, Mapping

try:  # 包内 import（`python -m webconsole.server`）
    from .observe import ObserveError, ObserveSession
except ImportError:  # 直接跑脚本/测试时的回退
    from observe import ObserveError, ObserveSession  # type: ignore[no-redef]

#: 观察目标一行的 JSON 形状版本（页面与 E2E 都读它）。
SCHEMA = 1


@dataclass(frozen=True)
class ObserveTarget:
    """页面上"实时观察哪一个"的那一行（一个 firmware 一份会话）。

    `capabilities` 是这一路的**会话能力**（`send` / `identity` / `decode` / `evidence`），
    由那一份会话从**它自己的设备登记**投影出来 —— 页面据此逐路禁用按钮，
    而不是拿参考目标的能力去套所有板子。
    """

    firmware_id: str
    label: str
    target_id: str
    device_key: str
    platform: str
    project: str
    note: str = ""
    #: 这一路的能力表（装配处给的能力声明对象；`None` = 没说）。
    capability_table: Any = field(default=None, repr=False, compare=False)


class ObserveCenter:
    """N 份会话 + 一个"当前选中"（core；不 import 任何目标/项目模块）。"""

    def __init__(
        self,
        sessions: Mapping[str, ObserveSession],
        *,
        targets: Iterable[ObserveTarget] = (),
        primary: str = "",
    ) -> None:
        rows = {str(row.firmware_id): row for row in targets}
        table = {str(key): value for key, value in dict(sessions).items()}
        if not table:
            raise ValueError("实时观察至少要登记一份会话（否则页面没有可选项）")
        self._sessions = table
        self._order = tuple(key for key in table)
        self._primary = str(primary or self._order[0])
        if self._primary not in table:
            raise ValueError(
                f"主装配 {self._primary!r} 不在观察会话里；有 {sorted(table)}"
            )
        # 没给 target 行的会话补一行：**用会话自己的事实**填，不编内容
        # （label 退到 firmware_id，能力表退到那一份会话的登记）。
        self._targets: dict[str, ObserveTarget] = {}
        for key, session in table.items():
            row = rows.get(key)
            self._targets[key] = row or ObserveTarget(
                firmware_id=key,
                label=key,
                target_id="",
                device_key="",
                platform=str(getattr(session.plugins, "platform", "") or ""),
                project=str(getattr(session.plugins, "project", "") or ""),
            )
        self._selected = self._primary
        self._lock = threading.RLock()
        # 连接时身份**没有**唯一匹配 ⇒ 这一条会话按「原始串口模式」工作（用户口径 2026-10-13）。
        # 它只影响**页面怎么说**（“身份未识别，已进入原始串口模式” + 解码/受控用例说“不适用”）：
        # 串口本身照常收字节（raw.bin / io_events.jsonl 都落盘），会话连上之后照样去问身份。
        self._transparent = False

    # ------------------------------------------------------------- 只读属性
    @property
    def primary(self) -> str:
        return self._primary

    @property
    def firmware_ids(self) -> tuple[str, ...]:
        return self._order

    @property
    def selected(self) -> str:
        """当前选中的 firmware（页面切目标改的就是它）。"""
        with self._lock:
            return self._selected

    @property
    def transparent(self) -> bool:
        """这一条会话是不是「身份未识别、按原始串口工作」（连接时定的，用户口径 2026-10-13）。"""
        with self._lock:
            return bool(self._transparent)

    def session(self, firmware_id: str | None = None) -> ObserveSession:
        """取某一份会话（不给就是当前选中的那一份）。"""
        key = str(firmware_id or "") or self.selected
        try:
            return self._sessions[key]
        except KeyError:
            raise ObserveError(
                f"未登记的观察目标 {key!r}：可用的是 {sorted(self._sessions)}"
            ) from None

    @property
    def sessions_dir(self):
        return self.session().sessions_dir

    @property
    def imports_dir(self):
        return self.session().imports_dir

    @property
    def plugins(self):
        """当前选中那一份的插件束（页面/自述的兜底读法，与以前同名）。"""
        return self.session().plugins

    def capabilities(self) -> dict[str, bool]:
        """当前选中那一份的会话能力（**不**跨固件合并）。"""
        return self.session().capabilities()

    def active_session_id(self) -> str | None:
        """正在写的那一个观察会话号（会话清单用它标出"这一条还在跑"）。

        一次只能连一个设备 ⇒ 顶多一个非空；逐份问，取第一个非空的。
        """
        for key in self._order:
            found = self._sessions[key].active_session_id()
            if found:
                return found
        return None

    # ------------------------------------------------------------- 选择/分派
    def select(self, firmware_id: str) -> dict[str, Any]:
        """页面切目标：把"当前选中的那一路"换成 `firmware_id`。

        另一份会话正握着串口时**拒绝**（409）：先断开再切 —— 串口是独占资源，
        平台不许隐式地把别人正在看的那条链路关掉。返回切换后的快照。
        """
        key = str(firmware_id or "")
        if key not in self._sessions:
            raise ObserveError(
                f"未登记的观察目标 {key!r}：可用的是 {sorted(self._sessions)}"
            )
        with self._lock:
            if key == self._selected:
                return self.snapshot()
            held = self._held_by_other(key)
            if held:
                raise ObserveError(held, status=409)
            self._selected = key
        return self.snapshot()

    def connect(
        self,
        device_key: str,
        firmware_id: str | None = None,
        *,
        transparent: bool = False,
        serial: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """连接所选设备；给了 `firmware_id` 就先切到那一路（切换规则同上）。

        ⚠️ **用户口径（2026-10-13）：身份不是连接的前置条件**。
        实时观察只看“这个通信口存在、可用、没被占用”；身份未知 / 零匹配 / 多匹配一律
        照连（`transparent=True`）—— 连上之后会话自己会去问身份（`_start_identity_queries`），
        问不出来就按原始串口收字节（`raw.bin` / `io_events.jsonl` 照旧落盘）。
        要求“先匹配到固件才能连”会把一块**没跑已知固件**的板子变成连都连不上，
        而那时候人要看的就是它到底在吐什么。

        `transparent=True`：不从 `firmware_id` 选链，只按**设备键**挑一条承载这次连接的链
        （挑不出来就如实拒绝：一个设备键都没登记的口不该被连）。
        """
        if transparent:
            host = self._host_for_device(device_key)
            if not host:
                raise ObserveError(
                    f"设备 {device_key!r} 不在任何一条观察链的设备登记里："
                    f"可用的是 {sorted(self._sessions)}",
                    status=409,
                )
            self.select(host)
            with self._lock:
                self._transparent = True
            return self.session().connect(device_key, serial=serial)
        if firmware_id and str(firmware_id) != self.selected:
            self.select(str(firmware_id))
        with self._lock:
            self._transparent = False
        return self.session().connect(device_key, serial=serial)

    def _host_for_device(self, device_key: str) -> str:
        """这个设备键能挂在哪一条链上（身份未知时用它挑“承载这次连接”的那一条）。

        同一个物理设备在**不同链**里用的是同一个键（同一块板的两份构建都登记成 `a`），
        所以“同一个键出现在多条链上”是正常的（它本来就是同一台设备）；
        这里只回答“哪一条链带得动它”：优先当前选中的那一条，否则按登记顺序取第一条。
        一个都答不出来就返回空串（调用方如实拒绝 —— 不猜）。
        """
        key = str(device_key or "").strip().lower()
        if not key:
            return ""
        hosts = [
            chain for chain in self._order
            if any(
                str(spec.key).strip().lower() == key
                for spec in self._sessions[chain].registry.specs()
            )
        ]
        if not hosts:
            return ""
        selected = self.selected
        return selected if selected in hosts else hosts[0]

    def disconnect(self, *, reason: str = "用户断开") -> dict[str, Any]:
        return self.session().disconnect(reason=reason)

    def reconnect(self, *, serial: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.session().reconnect(serial=serial)

    def serial_settings(self) -> dict[str, Any]:
        """当前那条会话生效的串口参数（需求 §6.10；页面与快照都读它）。"""
        return self.session().serial_settings()

    def send(self, *, mode: str, text: str, line_ending: object = None) -> dict[str, Any]:
        return self.session().send(mode=mode, text=text, line_ending=line_ending)

    def send_keys(self, keys: object, backspace: object = None) -> dict[str, Any]:
        return self.session().send_keys(keys, backspace)

    def query_identity(self) -> dict[str, Any]:
        return self.session().query_identity()

    # ----------------------------------------------------------------- 闸门
    def port_conflict(self, port: str) -> str | None:
        """自动验证的闸门：**任何一份**实时观察会话握着这个口都给理由。

        ⚠️ 这里必须逐份问，而不是只看当前选中的那一份：页面把观察切到 STM32 之后，
        ESP32 那一路仍可能握着 COM<n>（状态机没断），此时启动 ESP32 的自动验证
        必须照样被拒绝 —— 否则两个 owner 会同时写同一个口。
        """
        for key in self._order:
            reason = self._sessions[key].port_conflict(port)
            if reason:
                return reason
        return None

    def held_ports(self) -> list[tuple[str, str]]:
        """谁握着哪个口（只读；页面与测试用它说清当前占用）。"""
        out: list[tuple[str, str]] = []
        for key in self._order:
            snapshot = self._sessions[key].snapshot()
            port = str(snapshot.get("port") or "")
            if snapshot.get("holding") and port:
                out.append((key, port))
        return out

    def held_snapshot(self, port: str) -> dict[str, Any] | None:
        """Return the live-session facts for a port already owned by this center."""
        wanted = str(port or "").strip().upper()
        for key in self._order:
            snapshot = self._sessions[key].snapshot()
            if not snapshot.get("holding"):
                continue
            if str(snapshot.get("port") or "").strip().upper() == wanted:
                return snapshot
        return None

    # ----------------------------------------------------------------- 快照
    def targets(self) -> list[dict[str, Any]]:
        """页面上可选的那几路（每条链一行，**逐路**带自己的能力与设备）。"""
        out: list[dict[str, Any]] = []
        for key in self._order:
            row = self._targets[key]
            session = self._sessions[key]
            snapshot = session.snapshot()
            out.append(
                {
                    "firmware_id": row.firmware_id,
                    "label": row.label,
                    "target_id": row.target_id,
                    "device_key": row.device_key,
                    "platform": row.platform,
                    "project": row.project,
                    "note": row.note,
                    "capabilities": session.capabilities(),
                    "state": snapshot.get("state"),
                    "holding": bool(snapshot.get("holding")),
                    "port": snapshot.get("port"),
                    "devices": list(snapshot.get("options") or []),
                }
            )
        return out

    def snapshot(self) -> dict[str, Any]:
        """选中那一份的快照 + 选择信息（**所有旧键原样保留**）。"""
        with self._lock:
            key = self._selected
        base = dict(self.session(key).snapshot())
        base["firmware_id"] = key
        base["schema"] = SCHEMA
        base["selected"] = key
        # 「身份未识别、按原始串口工作」只在**真握着串口**时才算（断开之后那条连接不存在了）。
        base["transparent"] = bool(self.transparent and base.get("holding"))
        base["targets"] = self.targets()
        return base

    # ----------------------------------------------------------- 装配期辅助
    def _held_by_other(self, firmware_id: str) -> str | None:
        """另一路正握着串口吗（是则给出一句**点名**的拒绝理由）。"""
        for key in self._order:
            if key == firmware_id:
                continue
            snapshot = self._sessions[key].snapshot()
            if not snapshot.get("holding"):
                continue
            port = str(snapshot.get("port") or "")
            label = self._targets[key].label or key
            return (
                f"{label} 那一组还在握着串口 {port or '(未知口)'}"
                "：先在页面上断开它，再切到其它目标（同一个串口不能有两个 owner）"
            )
        return None


__all__ = ["SCHEMA", "ObserveCenter", "ObserveTarget"]

"""Select one fully assembled Pipeline per firmware while enforcing one global run.

W5 起实时观察由 `observe_center.ObserveCenter` 逐路分派（一个 firmware 一份会话）；
这个模块只管**自动验证**的路由与那条“全局只有一个 run”的规矩。
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Iterable

try:
    from .pipeline import RequestError
except ImportError:  # executed beside the other webconsole modules
    from pipeline import RequestError  # type: ignore[no-redef]


class RunnerRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Runner:
    firmware_id: str
    pipeline: Any
    requires_flash_confirmation: bool = False


def _newer_device_row(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """两条链共用同一台物理设备时，哪一张卡片更接近事实（）。

    判据只有两条，按重要性排：
    1. **持有者优先**：谁握着串口不许被另一条链的旧卡片盖掉（那是"两个 owner"的前兆）；
    2. 否则取**最近活动**的那一张（ISO 时间戳按字符串比就够；`None` 算最旧）。
    """
    if bool(candidate.get("owner")) and not bool(current.get("owner")):
        return True
    if bool(current.get("owner")) and not bool(candidate.get("owner")):
        return False
    return str(candidate.get("last_activity_utc") or "") > str(
        current.get("last_activity_utc") or ""
    )


class RunnerRegistry:
    """A thin process-level router; it never mutates a Pipeline's target assembly."""
    def __init__(self, runners: Iterable[Runner], *, primary: str):
        rows = tuple(runners)
        self._runners = {row.firmware_id: row.pipeline for row in rows}
        self._flash_confirmation = {
            row.firmware_id: bool(row.requires_flash_confirmation) for row in rows
        }
        if not self._runners:
            raise RunnerRegistryError("runner registry 不能为空")
        if primary not in self._runners:
            raise RunnerRegistryError(f"primary runner {primary!r} 未登记")
        self.primary_id = primary
        self._lock = threading.RLock()
        stores = {str(row.store.runs_dir.resolve()) for row in self._runners.values()}
        buses = {id(row.event_bus) for row in self._runners.values()}
        if len(stores) != 1:
            raise RunnerRegistryError("所有 runner 必须共用同一个 run 证据目录")
        if len(buses) != 1:
            raise RunnerRegistryError("所有 runner 必须共用同一个 EventBus")

    @property
    def primary(self) -> Any:
        return self._runners[self.primary_id]

    @property
    def store(self) -> Any:
        return self.primary.store

    @property
    def devices(self) -> Any:
        # ⚠️ W5 起实时观察**不再**由这里提供设备登记：每份固件一条观察会话，由一个
        # `ObserveCenter` 按 firmware 分派（`observe_center.py`）。这个属性只为旧调用方
        # 保留“主装配的那份登记”这个读法。
        return self.primary.devices

    def require(self, firmware_id: str) -> Any:
        runner = self._runners.get(str(firmware_id or ""))
        if runner is None:
            raise RunnerRegistryError(f"固件 {firmware_id!r} 没有可执行 runner")
        return runner

    def _active(self) -> Any | None:
        for runner in self._runners.values():
            if runner.active_run_port() is not None:
                return runner
        return None

    def start(
        self, firmware_id: str, *, confirm_flash: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active()
            if active is not None:
                run = active.active_run_port() or {}
                raise RequestError(f"已有 run {run.get('run_id', '?')} 正在执行", status=409)
            runner = self.require(firmware_id)
            if self._flash_confirmation[firmware_id] and not confirm_flash:
                # 例外：**声明跑在现有镜像上**的套件（`skip_flash`）根本不会烧录：
                # 不要求“逐次确认覆盖 Flash”，否则存储验收永远拿不到一次零烧录的 run 记录。
                # ⚠️ 用 `getattr`：注册处是**薄路由**，不许假设每个 runner 都有 `.project`
                #（测试替身就没有 —— 实测就是在这里现形的 `AttributeError`）。
                project = getattr(runner, "project", None)
                skip_flash = bool(
                    project is not None
                    and project.run_default(
                        "skip_flash", False, suite=str(kwargs.get("suite") or "")
                    )
                )
                if not skip_flash:
                    raise RequestError(
                        "此固件会覆盖设备 Flash；必须逐次明确确认后才能启动", status=403
                    )
            return runner.start(**kwargs)

    def recorded_digests(self, run_id: str) -> dict[str, str]:
        """run 报告里**已经记下来**的产物哈希（L4 的稀疏索引拿它当指纹用）。

        ⚠️ 这一条不是可选的样板代码：路由层拿到的 `pipeline` 就是**这个登记处**，
        少转发一个方法，模块测试全绿而真 HTTP 一请求就 500/断连 ——
        L3 的 `run_detail(content=…)` 已经这么漏过一次，这次是 `recorded_digests`
        （真机验收当场抓到 `AttributeError: 'RunnerRegistry' object has no attribute
        'recorded_digests'`）。
        """
        return self.primary.recorded_digests(run_id)

    def snapshot(self) -> dict[str, Any]:
        snapshots = [runner.snapshot() for runner in self._runners.values()]
        active = next((row for row in snapshots if row.get("busy")), None)
        chosen = active or snapshots[0]
        result = dict(chosen)
        # ⚠️ 同一个物理设备可以出现在**不止一条链**上（STM32 那块板同时承载裸机与 RT-Thread
        # 两份固件 profile，设备键都是 `stm32a`）。以前这里是"后写覆盖" ⇒ 页面显示的是
        # **最后注册那条链**的卡片：裸机刚跑完 PASS，卡片却还写着 RT-Thread 那条链在服务启动时
        # 从 run 目录恢复出来的旧结果（"最后一次 run 结果 FAIL"）—— 说得跟事实不一样
        # （，与  同一类）。现在按**最近活动**取，而且**持有者优先**
        # （谁握着串口这件事不许被另一条链的旧卡片盖掉）。
        device_rows: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            for row in snapshot.get("devices") or []:
                key = str(row.get("key") or row.get("port") or len(device_rows))
                current = device_rows.get(key)
                if current is None or _newer_device_row(row, current):
                    device_rows[key] = row
        result["devices"] = list(device_rows.values())
        result["runner_ids"] = list(self._runners)
        return result

    def runs(self, limit: int = 40) -> list[dict[str, Any]]:
        return self.primary.runs(limit=limit)

    def run_detail(self, run_id: str, *, content: str = "auto") -> dict[str, Any]:
        """详情**必须把 `content` 透传下去**（ / L2）。

        ⚠️ 这条路由真正调的不是 `Pipeline` 而是这里 —— 路由里加了参数、这里漏了，
        页面就会拿到一个 500（我第一次就是这么错的：HTTP 上真跑才发现，
        函数级的判据看不到这一层）。
        """
        manifest = self.store.read_json(run_id, "manifest.json")
        project_key = str(((manifest.get("platform") or {}).get("project")) or "")
        matches = [
            runner for runner in self._runners.values()
            if str(getattr(getattr(runner, "project", None), "key", "")) == project_key
        ]
        runner = matches[0] if len(matches) == 1 else self.primary
        return runner.run_detail(run_id, content=content)

    def cancel(self, run_id: str) -> bool:
        active = self._active()
        if active is None:
            raise RequestError(f"没有正在执行的 run {run_id}", status=404)
        return bool(active.cancel(run_id))

    def active_run_port(self) -> dict[str, Any] | None:
        runner = self._active()
        return runner.active_run_port() if runner is not None else None

    def subscribe(self):
        return self.primary.subscribe()

    def unsubscribe(self, channel) -> None:
        self.primary.unsubscribe(channel)

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        return self.primary.events_since(seq)

    def replay_window(self, since: int, **limits: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """有界重放也走**租约持有者**那一个总线（）：SSE 是全局的，
        所以它跟 `events_since` 一样必须问同一份积压，不能按当前选中的固件分开。"""
        return self.primary.replay_window(since, **limits)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        return self.primary.emit(kind, **fields)

    def note_rejected_request(self, *args: Any, **kwargs: Any) -> None:
        self.primary.note_rejected_request(*args, **kwargs)


__all__ = ["Runner", "RunnerRegistry", "RunnerRegistryError"]

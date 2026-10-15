#!/usr/bin/env python3
"""Single-owner lock for the workspace.

``公开文档`` says the local orchestration service is the
only owner of PlatformIO and the COM ports. That rule only holds for work that
goes *through* the service: an agent (or a human) running ``platformio`` or a
serial reader directly in a terminal bypasses it completely, and two builds
writing the same ``.pio`` directory is undefined behaviour.

This module makes the rule checkable. The service takes the lock while it runs;
CLI tools that touch the build directory or a COM port refuse to start while a
live owner holds it.

The lock is **advisory** -- it prevents accidents, not adversaries. A stale lock
(owner pid gone) is ignored and cleaned up automatically, so a killed service
never blocks the CLI forever. PID reuse can in principle keep a stale lock
looking alive; that is accepted for an advisory lock and mentioned in the error
message so a human can delete the file.

Windows note: ``os.kill(pid, 0)`` is NOT usable -- on Windows any signal other
than CTRL_C/CTRL_BREAK *terminates* the target process. Liveness is therefore
checked with ``OpenProcess`` + ``GetExitCodeProcess``.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

# `utc_now()` 住在平台核心（`webconsole/core_primitives.py`）。这里以前抄了
# **第二份完全相同的实现** —— 两份时间戳一定会漂，所以现在只导入（ / R2a）。
#
# ⚠️ 两条导入路径都写出来是有意的：`core_primitives` 要用顶层名字（`webconsole` 目录
# 在 sys.path 上时，与其它 webconsole 模块在脚本模式下的取法一致），拿不到再走包名。
# 只有这样，"同一个文件"才不会在 `sys.modules` 里出现两次（ 的同族问题）。
try:
    from core_primitives import utc_now
except ImportError:  # pragma: no cover - 只有 sys.path 里没有 webconsole 时才会走到
    from webconsole.core_primitives import utc_now

LOCK_NAME = ".console.lock"
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class OwnerConflict(RuntimeError):
    """A live owner already holds the workspace."""


@dataclass(frozen=True)
class OwnerInfo:
    pid: int
    kind: str
    port: int | None
    url: str | None
    started_utc: str
    label: str = ""

    def describe(self) -> str:
        where = f" {self.url}" if self.url else ""
        return f"{self.kind}(pid {self.pid}){where}，自 {self.started_utc}"


def lock_path(project_dir: Path) -> Path:
    """``仓库根/.console.lock`` -- beside the project, not inside ``.pio``.

    ``.pio`` is wiped by ``fullclean``, and the lock must outlive that.
    """
    return Path(project_dir) / LOCK_NAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":  # pragma: no cover - the project is Windows-first
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_owner(project_dir: Path) -> OwnerInfo | None:
    """Return the live owner, or None if there is none.

    A lock whose process is gone is removed and treated as absent.
    """
    path = lock_path(project_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        owner = OwnerInfo(
            pid=int(payload["pid"]),
            kind=str(payload.get("kind", "unknown")),
            port=payload.get("port"),
            url=payload.get("url"),
            started_utc=str(payload.get("started_utc", "?")),
            label=str(payload.get("label", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        _remove(path)
        return None
    if not pid_alive(owner.pid):
        _remove(path)
        return None
    return owner


def acquire(
    project_dir: Path,
    *,
    kind: str = "webconsole",
    port: int | None = None,
    url: str | None = None,
    label: str = "",
    pid: int | None = None,
) -> OwnerInfo:
    """Take the lock, or raise OwnerConflict describing the current owner."""
    existing = read_owner(project_dir)
    if existing is not None:
        raise OwnerConflict(
            f"工作区已被 {existing.describe()} 占用。\n"
            "同一时刻只允许一个 owner 持有 PlatformIO 与串口。\n"
            "如果确定它已经不在运行，请删除 "
            f"{lock_path(project_dir)} 后重试。"
        )
    owner = OwnerInfo(
        pid=os.getpid() if pid is None else pid,
        kind=kind,
        port=port,
        url=url,
        started_utc=utc_now(),
        label=label,
    )
    path = lock_path(project_dir)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "pid": owner.pid,
                "kind": owner.kind,
                "port": owner.port,
                "url": owner.url,
                "started_utc": owner.started_utc,
                "label": owner.label,
                "note": "advisory lock; delete only when no owner is running",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return owner


def release(project_dir: Path, *, pid: int | None = None) -> None:
    """Release the lock. Only the holder's own pid may release it by default."""
    path = lock_path(project_dir)
    if not path.is_file():
        return
    if pid is not None:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if int(current.get("pid", -1)) != pid:
            return  # someone else's lock; never remove it
    _remove(path)


def require_free(project_dir: Path, *, what: str) -> None:
    """Raise OwnerConflict if a live owner would be disturbed by ``what``."""
    owner = read_owner(project_dir)
    if owner is None:
        return
    raise OwnerConflict(
        f"{what} 需要独占 PlatformIO/串口，但工作区正被 {owner.describe()} 占用。\n"
        "请先停止调试台（Ctrl+C），或改为通过它的接口发起（POST /api/run）。"
    )


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _main() -> int:
    """Tiny CLI so a human can ask who holds the lock without reading JSON."""
    project_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    # 参数是**路径**而不是项目名。如果解析出来的目录不是这个平台的工程（比如从另一个
    # 工作区里传了 示例项目），旧版本会对着一个根本不存在的锁回答 "free" ——
    # 一个让人安心的错误答案比报错更危险（）。所以这里宁可拒绝猜测。
    if not (project_dir / "platformio.ini").is_file():
        print(
            f"error: {project_dir} 不是这个平台的工程（没有 platformio.ini），拒绝猜测。\n"
            "提示：本命令的参数是**路径**；在别的工作区里传 示例项目 查的是另一份检出。",
            file=sys.stderr,
        )
        return 2
    owner = read_owner(project_dir)
    if owner is None:
        print(f"free: {project_dir} 无存活占用者（{lock_path(project_dir)} 不存在或已过期）")
        return 0
    print(f"held: {project_dir} → {owner.describe()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

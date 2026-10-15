#!/usr/bin/env python3
""" / D1：**平台基础体检**（**core**）。

这一层回答的是"这台电脑能不能跑平台" —— Python、必需模块、目录权限、监听端口、
装配可加载、串口 backend，以及开发/验收用的 Node / 浏览器 / Git 能力。它**不认识**
任何具体目标：串口枚举与 pyserial 体检是通用能力，目标专属的构建/烧录/解码工具
由目标适配器（D3）自带检查项，不在这里写。

默认安全边界（需求 §3，逐条对应；`test_doctor.py` 有源码级守卫盯着）：

- **不启动常驻服务**：端口检查只是 `bind` 后立刻释放；
- **不打开串口**：只 `import serial` 与（显式 `--hardware` 时）枚举端口名；
- **不构建、不下载、不联网、不改环境**；
- **不动工程与证据**：装配检查把证据目录指到系统临时目录，检查完就删。

所有"问本机"的动作都收在 `PlatformProbe` 里（一整套**可注入**的可调用对象）：
默认实现走真实世界，测试整份替换即可在无硬件、无网络、无工具链的机器上跑。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import datetime as _dt
import importlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

try:  # 包内 import
    from .doctor_model import Check, Scope, Status
except ImportError:  # 直接跑脚本/测试时的回退
    from doctor_model import Check, Scope, Status  # type: ignore[no-redef]

#: 装配探针在"台架未配置"时用的前缀（`doctor.py` 写、`_check_assembly` 读）。
#: 那类装不起来是**目标事实缺席**，不是平台坏了 —— 降成 WARN 并写清下一步。
UNCONFIGURED_PREFIX = "未配置："

#: 支持的最低 Python 版本（`tuple` 形式，便于比较）。
MIN_PYTHON: tuple[int, int] = (3, 9)

#: Web 平台自己直接 import 的模块（全是标准库）。缺一个就说明环境装坏了。
WEB_REQUIRED_MODULES: tuple[str, ...] = (
    "argparse",
    "dataclasses",
    "hashlib",
    "http.server",
    "importlib",
    "json",
    "pathlib",
    "re",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "threading",
    "urllib.request",
    "zlib",
)


#: 分类 id → **人话分类名**。页面二级分组只读报告里的 `group_label`，
#: 不自己抄一份映射表（改名只改这里一处）。
GROUP_LABELS: dict[str, str] = {
    "python": "Python 运行环境",
    "modules": "必需模块",
    "files": "目录与权限",
    "port": "监听端口",
    "assembly": "平台装配",
    "bench": "本机台架",
    "serial": "串口承载",
    "tooling": "开发与验收工具链",
    "hardware": "硬件在线探测",
}


def _labeled(rows: list[Check]) -> list[Check]:
    """给每条检查补上分类的人话名（`group` 是机器键，`group_label` 是给人看的）。"""
    return [replace(row, group_label=GROUP_LABELS.get(row.group, row.group)) for row in rows]


# ===================================================================== 路径
@dataclass(frozen=True)
class PlatformPaths:
    """平台自己的几个目录（从工作目录推出，**不创建**任何东西）。"""

    root: Path
    static_dir: Path
    runs_dir: Path
    sessions_dir: Path
    imports_dir: Path

    @classmethod
    def under(cls, root: Path | str) -> "PlatformPaths":
        base = Path(root)
        return cls(
            root=base,
            static_dir=base / "tools" / "webconsole" / "static",
            runs_dir=base / "runs",
            sessions_dir=base / "observe-sessions",
            imports_dir=base / "observe-imports",
        )


# ============================================================== 真实探针实现
def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def python_executable() -> str:
    return str(sys.executable)


def _stdout_encoding() -> str:
    return str(getattr(sys.stdout, "encoding", "") or "")


def _timezone_known() -> bool:
    try:
        return _dt.datetime.now().astimezone().tzinfo is not None
    except Exception:
        return False


def can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
    except Exception:
        return False
    return True


def path_state(path: Path | str, *, writable: bool = False) -> str:
    """`ok` / `missing` / `unreadable` / `read-only`（只读探测，不动文件）。"""
    target = Path(path)
    if not target.exists():
        return "missing"
    if not os.access(target, os.R_OK):
        return "unreadable"
    if writable and not os.access(target, os.W_OK):
        return "read-only"
    return "ok"


def temp_writable() -> bool:
    try:
        with tempfile.NamedTemporaryFile(prefix="pw-doctor-", suffix=".tmp", delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
    except OSError:
        return False
    return True


def port_state(host: str, port: int) -> str:
    """`free` / `in-use` / `denied` / `unknown` —— 绑定后**立即释放**（不起服务）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port)))
        except OSError as error:
            errno = getattr(error, "errno", None)
            winerror = getattr(error, "winerror", None)
            if errno in (48, 98) or winerror == 10048:
                return "in-use"
            if errno in (13, 1) or winerror == 10013:
                return "denied"
            return "unknown"
        return "free"


def _tool_text(raw: bytes | None) -> str:
    """把**系统工具**的输出变成文本：拿字节 + 宽松解码。

    ⚠️ （2026-10-11 实测）：这类工具按 **OEM 代码页**输出（中文 Windows 上
    `netstat` 是 GBK：`\\r\\n活动连接\\r\\n\\r\\n  协议  本地地址 …`），而本平台用
    `python -X utf8` 起服务 ⇒ 交给 `text=True` 的**严格 UTF-8 解码会打死读取线程**
    （实测 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb in position 2`，
    与 `netstat` 输出的第 3 个字节逐字节一致），于是这个检查静默退成“未知”，
    控制台还刷一屏线程栈。
    与 `pipeline` / `第二目标` 同一个惯例：**别让一条解码错误杀掉读取线程**。
    """
    return (raw or b"").decode("utf-8", "replace")


def listening_pid(port: int) -> int | None:
    """谁在 `LISTENING` 这个端口（只读的系统查询）；拿不到就 `None`。"""
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    needle = f":{int(port)}"
    for line in _tool_text(completed.stdout).splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING" or not parts[1].endswith(needle):
            continue
        try:
            return int(parts[4])
        except ValueError:
            return None
    return None


def transport_backend() -> str:
    """本机串口**实现**能不能用（枚举 + 打开的实现都在 pyserial 里）。空 = 不可用。"""
    try:
        import serial  # noqa: PLC0415 - 只有这里需要它
    except Exception:
        return ""
    try:
        from serial.tools import list_ports  # noqa: F401,PLC0415 - 枚举能力在它身上
    except Exception:
        return "pyserial（缺少 port 枚举子模块）"
    version = str(getattr(serial, "__version__", "") or "").strip()
    return f"pyserial {version}".strip()


def node_backend() -> str:
    exe = shutil.which("node")
    if not exe:
        return ""
    try:
        completed = subprocess.run([exe, "--version"], capture_output=True, timeout=8)
        version = _tool_text(completed.stdout).strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    return f"node {version}".strip() if version else exe


def _default_browsers_path() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return str(Path(base) / "ms-playwright") if base else ""
    return str(Path.home() / ".cache" / "ms-playwright")


def browser_backend() -> str:
    """开发/验收用的浏览器能力（Playwright 装下来的 Chromium）。空 = 不可用。

    ⚠️ 判据是**那台浏览器在不在**，不是 Python 的 `playwright` 包能不能 import：
    本仓库的浏览器 E2E 走 Node + `NODE_PATH` 指到这份 Chromium（见
    `test_webconsole_browser.py`），Python 包装没装与它无关。
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or _default_browsers_path()
    if root and Path(root).is_dir():
        for entry in sorted(Path(root).iterdir()):
            if entry.is_dir() and entry.name.lower().startswith("chromium"):
                return f"playwright chromium（{entry.name}）"
    return ""


def git_backend() -> str:
    exe = shutil.which("git")
    if not exe:
        return ""
    try:
        completed = subprocess.run([exe, "--version"], capture_output=True, timeout=8)
        version = _tool_text(completed.stdout).strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    return version or exe


def enumerate_ports() -> list[str]:
    """只读枚举串口名（**不打开**任何端口）。没有 pyserial 时抛 ImportError。"""
    from serial.tools import list_ports  # noqa: PLC0415

    return sorted(str(info.device) for info in list_ports.comports())


def bench_state() -> dict[str, Any]:
    """台架文件的当前情形（ / B3）—— 转发给 `bench_config.status()`。

    ⚠️ **读文件这件事只发生在 `bench_config` 里**（`test_bench_config` 的
    `only_entry_modules_touch_the_bench_file` 守着）：这里是核平台层，只问结论。
    没有那个模块（单文件拷出去跑）⇒ 如实说"没装过"。
    """
    try:
        # ⚠️ 包内优先：`bench_config` 的进程级状态挂在模块对象上，接错一份就成了"没装过"。
        from . import bench_config  # noqa: PLC0415
    except ImportError:
        try:
            import bench_config  # noqa: PLC0415
        except ImportError:
            return {"state": "uninstalled", "file": "", "from": "", "problems": []}
    try:
        try:
            from . import bench_setup  # noqa: PLC0415
        except ImportError:
            import bench_setup  # noqa: PLC0415

        declarations = bench_setup.declarations()
    except Exception:  # noqa: BLE001 - 列不出声明就只做形状校验，不猜
        declarations = ()
    try:
        return dict(bench_config.status(declarations=declarations))
    except Exception as error:  # noqa: BLE001 - 问不出来就如实说问不出来
        return {"state": "unknown", "file": "", "from": "",
                "problems": [f"{type(error).__name__}: {error}"]}


# ===================================================================== 探针束
@dataclass(frozen=True)
class PlatformProbe:
    """平台体检要问本机的全部动作。默认走真实世界，测试整份替换。"""

    python_version: Callable[[], tuple[int, ...]] = _python_version
    python_executable: Callable[[], str] = python_executable
    stdout_encoding: Callable[[], str] = _stdout_encoding
    timezone_known: Callable[[], bool] = _timezone_known
    can_import: Callable[[str], bool] = can_import
    path_state: Callable[..., str] = path_state
    temp_writable: Callable[[], bool] = temp_writable
    port_state: Callable[[str, int], str] = port_state
    port_owner: Callable[[int], str] | None = None
    assembly_problems: Callable[[], Sequence[str] | None] | None = None
    bench_state: Callable[[], dict[str, Any]] = bench_state
    transport_backend: Callable[[], str] = transport_backend
    node_backend: Callable[[], str] = node_backend
    browser_backend: Callable[[], str] = browser_backend
    git_backend: Callable[[], str] = git_backend
    enumerate_ports: Callable[[], Sequence[str]] = enumerate_ports
    #: 厂商工具在不在 PATH 上（缺失**只影响各自的 Adapter**，见 `_check_vendor_tools`）。
    #: ⚠️ 名字由**目标层**给（`Callable[[], Mapping[str, Sequence[str]]]`）：
    #: core 里出现厂商工具名会被边界检查拦下。
    vendor_tools: Callable[[], Mapping[str, Sequence[str]]] | None = None

    def with_overrides(self, **overrides: Any) -> "PlatformProbe":
        """派生一份改了若干探针的副本（测试与 CLI 都用它）。"""
        return replace(self, **overrides)


def _call(probe_call: Callable[[], Any] | None, default: Any) -> Any:
    """调一个可选探针；没装配或它自己炸了 ⇒ 如实退到 `default`（不猜）。"""
    if probe_call is None:
        return default
    try:
        return probe_call()
    except Exception:
        return default


# ===================================================================== 平台检查
def platform_checks(
    probe: PlatformProbe,
    paths: PlatformPaths,
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
) -> list[Check]:
    """平台基础能力检查（需求 §4.1）。返回顺序即报告顺序。"""
    checks: list[Check] = []
    checks.append(_check_python(probe))
    checks.append(_check_python_console(probe))
    checks.append(_check_modules(probe))
    checks.append(_check_assets(probe, paths))
    checks.append(_check_evidence(probe, paths))
    checks.append(_check_temp(probe))
    checks.append(_check_port(probe, host=host, port=port))
    checks.append(_check_assembly(probe))
    checks.append(_check_bench(probe))
    checks.append(_check_serial(probe))
    checks.append(_check_vendor_tools(probe))
    checks.append(_check_optional_tool(probe, "node"))
    checks.append(_check_optional_tool(probe, "browser"))
    checks.append(_check_optional_tool(probe, "git"))
    return _labeled(checks)


def _check_python(probe: PlatformProbe) -> Check:
    version = tuple(_call(probe.python_version, ()))
    executable = _call(probe.python_executable, "")
    text = ".".join(str(part) for part in version) or "未知"
    actual = f"{text}（{executable}）" if executable else text
    expected = ".".join(str(part) for part in MIN_PYTHON) + " 或更新"
    ok = bool(version) and version[:2] >= MIN_PYTHON
    if ok:
        return Check(
            id="platform.python.version",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="Python 版本与解释器",
            group="python",
            actual=actual,
            expected=expected,
        )
    return Check(
        id="platform.python.version",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="Python 版本与解释器",
        group="python",
        actual=actual,
        expected=expected,
        reason=f"当前解释器版本 {text}，低于平台支持的 {expected}",
        remediation=f"安装 Python {expected}，并用它重建运行环境（本平台用 pyserial 与标准库即可）",
    )


def _check_python_console(probe: PlatformProbe) -> Check:
    encoding = str(_call(probe.stdout_encoding, "") or "")
    normalized = encoding.lower().replace("-", "")
    utf8 = normalized in ("utf8", "utf8mb4", "cp65001")
    tz_known = bool(_call(probe.timezone_known, False))
    actual = f"stdout 编码 {encoding or '未知'}；时区 {'可用' if tz_known else '不可用'}"
    if utf8 and tz_known:
        return Check(
            id="platform.python.console",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            title="控制台编码与时区",
            group="python",
            actual=actual,
            expected="stdout 为 UTF-8；本机时区可解析",
        )
    reasons = []
    if not utf8:
        reasons.append(f"stdout 编码是 {encoding or '未知'}")
    if not tz_known:
        reasons.append("本机时区不可解析")
    return Check(
        id="platform.python.console",
        scope=Scope.PLATFORM,
        status=Status.WARN,
        title="控制台编码与时区",
        group="python",
        actual=actual,
        expected="stdout 为 UTF-8；本机时区可解析",
        reason="；".join(reasons) + "（不影响运行，但中文/符号输出可能被降级）",
        remediation="运行前设置 PYTHONUTF8=1（或 PYTHONIOENCODING=utf-8）；Doctor 输出已只用 ASCII 标记",
    )


def _check_modules(probe: PlatformProbe) -> Check:
    missing = [name for name in WEB_REQUIRED_MODULES if not _call_callable(probe.can_import, name)]
    total = len(WEB_REQUIRED_MODULES)
    actual = f"{total - len(missing)}/{total} 个模块可导入"
    if not missing:
        return Check(
            id="platform.modules.web",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="Web 平台必需的 Python 模块",
            group="modules",
            actual=actual,
            expected=f"{total} 个标准库模块全部可导入",
        )
    return Check(
        id="platform.modules.web",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="Web 平台必需的 Python 模块",
        group="modules",
        actual=actual + f"；缺 {', '.join(missing)}",
        expected=f"{total} 个标准库模块全部可导入",
        reason=f"这些模块导不进来：{', '.join(missing)}",
        remediation="当前解释器安装不完整：重装同版本 Python，或换一个完整解释器重建环境",
    )


def _call_callable(func: Callable[..., Any] | None, *args: Any) -> Any:
    if func is None:
        return False
    try:
        return func(*args)
    except Exception:
        return False


def _check_assets(probe: PlatformProbe, paths: PlatformPaths) -> Check:
    state = str(_call(lambda: probe.path_state(paths.static_dir), "missing"))
    if state == "ok":
        return Check(
            id="platform.files.assets",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="静态资源目录",
            group="files",
            actual=f"{paths.static_dir} 存在且可读",
            expected="页面静态资源目录存在且可读",
        )
    return Check(
        id="platform.files.assets",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="静态资源目录",
        group="files",
        actual=f"{paths.static_dir}（{state}）",
        expected="页面静态资源目录存在且可读",
        reason=f"找不到或读不了静态资源目录（{state}）",
        remediation="确认工作目录指向完整的平台工作树（页面资源随代码一起分发，不单独下载）",
    )


def _check_evidence(probe: PlatformProbe, paths: PlatformPaths) -> Check:
    rows = []
    blocked = []
    for label, path in (("运行目录", paths.runs_dir), ("会话目录", paths.sessions_dir)):
        verdict, note = _dir_or_parent(probe, path)
        rows.append(f"{label} {note}")
        if verdict != "ok":
            blocked.append(f"{label}（{note}）")
    actual = "；".join(rows)
    if not blocked:
        return Check(
            id="platform.files.evidence",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="运行目录与证据目录",
            group="files",
            actual=actual,
            expected="现有目录可读，缺失时父目录可写（首次运行会创建）",
        )
    return Check(
        id="platform.files.evidence",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="运行目录与证据目录",
        group="files",
        actual=actual,
        expected="现有目录可读，缺失时父目录可写（首次运行会创建）",
        reason=f"证据落不进去：{'；'.join(blocked)}",
        remediation="确认工作目录可写（不要放在只读介质或需要管理员权限的目录下）",
    )


def _dir_or_parent(probe: PlatformProbe, path: Path) -> tuple[str, str]:
    state = str(_call(lambda: probe.path_state(path), "missing"))
    if state == "ok":
        return "ok", "存在且可读"
    if state == "missing":
        parent = str(_call(lambda: probe.path_state(path.parent, writable=True), "missing"))
        if parent == "ok":
            return "ok", "尚不存在（父目录可写，首次运行会创建）"
        return "blocked", f"不存在，且父目录{parent}"
    return "blocked", state


def _check_temp(probe: PlatformProbe) -> Check:
    writable = bool(_call(probe.temp_writable, False))
    if writable:
        return Check(
            id="platform.files.temp",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="系统临时目录可写",
            group="files",
            actual=f"{tempfile.gettempdir()} 可写",
            expected="系统临时目录可写",
        )
    return Check(
        id="platform.files.temp",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="系统临时目录可写",
        group="files",
        actual=f"{tempfile.gettempdir()} 不可写",
        expected="系统临时目录可写",
        reason="临时目录不可写：装配检查、诊断包装配等都用它做暂存",
        remediation="检查 TEMP/TMP 环境变量指向的目录权限，或清理磁盘空间",
    )


def _check_port(probe: PlatformProbe, *, host: str, port: int) -> Check:
    state = str(_call(lambda: probe.port_state(host, int(port)), "unknown"))
    owner = ""
    if probe.port_owner is not None:
        owner = str(_call(lambda: probe.port_owner(int(port)), "") or "")
    where = f"{host}:{port}"
    if state == "free":
        return Check(
            id="platform.port.available",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="默认监听端口可用",
            group="port",
            actual=f"{where} 空闲（绑定后已立即释放）",
            expected="空闲，或已被本平台自己的服务占用",
        )
    if state == "in-use" and owner == "platform":
        return Check(
            id="platform.port.available",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="默认监听端口可用",
            group="port",
            actual=f"{where} 已被本平台自己的服务占用",
            expected="空闲，或已被本平台自己的服务占用",
        )
    if state == "denied":
        return Check(
            id="platform.port.available",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title="默认监听端口可用",
            group="port",
            actual=f"{where} 绑定被拒绝",
            expected="空闲，或已被本平台自己的服务占用",
            reason="绑定端口时被系统拒绝（权限不足）",
            remediation=f"换一个高位端口（--port）或调整权限/防火墙设置",
        )
    if state == "in-use":
        if owner == "other":
            reason = f"{where} 已被其他进程占用"
        else:
            reason = f"{where} 被占用，但只读探测无法确定占用者"
        return Check(
            id="platform.port.available",
            scope=Scope.PLATFORM,
            status=Status.WARN,
            required=True,
            title="默认监听端口可用",
            group="port",
            actual=f"{where} 被占用",
            expected="空闲，或已被本平台自己的服务占用",
            reason=reason,
            remediation="换一个端口（--port）或停掉占用进程；Doctor 不会强停任何进程",
        )
    return Check(
        id="platform.port.available",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title="默认监听端口可用",
        group="port",
        actual=f"{where} 可用性未知",
        expected="空闲，或已被本平台自己的服务占用",
        reason="端口绑定结果无法判定（既没报占用也没成功，属异常）",
        remediation="换一个端口（--port）后重跑；仍异常就报一个 issue 并附上本报告的 JSON",
    )


def _check_assembly(probe: PlatformProbe) -> Check:
    title = "Core / 装配可加载"
    if probe.assembly_problems is None:
        return Check(
            id="platform.assembly.loadable",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title=title,
            group="assembly",
            actual="没有装配探针",
            expected="装配可加载，且装配问题列表为空",
            reason="调用方没有提供装配探针 ⇒ 这一项无法判定（没判定不是没事）",
            remediation="用平台入口（doctor CLI 或 web 服务）运行体检；它们会带上装配探针",
        )
    try:
        problems = probe.assembly_problems()
    except Exception as error:  # noqa: BLE001 - 装配加载失败就是失败，说清原因
        return Check(
            id="platform.assembly.loadable",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title=title,
            group="assembly",
            actual="加载抛异常",
            expected="装配可加载，且装配问题列表为空",
            reason=f"装配加载失败：{type(error).__name__}: {error}",
            remediation="按上一条异常修好导入/登记问题后重跑；装配检查只在临时目录建对象，不动工程",
        )
    if problems is None:
        return Check(
            id="platform.assembly.loadable",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title=title,
            group="assembly",
            actual="探针没给出结论",
            expected="装配可加载，且装配问题列表为空",
            reason="装配探针返回了空值 ⇒ 无法判定",
            remediation="检查装配处（`build_web_assembly`）的返回值",
        )
    issues = [str(item) for item in problems]
    if not issues:
        return Check(
            id="platform.assembly.loadable",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title=title,
            group="assembly",
            actual="装配问题列表为空",
            expected="装配可加载，且装配问题列表为空",
        )
    if all(item.startswith(UNCONFIGURED_PREFIX) for item in issues):
        # 这台机器还没写台架文件（ / B6）：装配里少一条链是**未配置**，
        # 不是"平台装不起来" —— 平台本身是好的，参考目标那条链还在。
        return Check(
            id="platform.assembly.loadable",
            scope=Scope.PLATFORM,
            status=Status.WARN,
            required=True,
            title=title,
            group="assembly",
            actual=f"{len(issues)} 条链因为未配置而没有登记",
            expected="装配可加载，且装配问题列表为空",
            reason="；".join(issues[:5]) + ("…" if len(issues) > 5 else ""),
            remediation=(
                "把 bench.example.json 抄一份成 bench.local.json 并填上缺的那几格；"
                "填好后这一项会变成逐条核对装配（而不是少一条链）"
            ),
        )
    return Check(
        id="platform.assembly.loadable",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title=title,
        group="assembly",
        actual=f"{len(issues)} 条装配问题",
        expected="装配可加载，且装配问题列表为空",
        reason="；".join(issues[:5]) + ("…" if len(issues) > 5 else ""),
        remediation="按上面每条问题修目标/项目/套件的登记（它们说的必须是同一条链）",
    )


def _check_bench(probe: PlatformProbe) -> Check:
    """`platform.bench.file`（ §6.4）：有效 ⇒ PASS；没有 ⇒ WARN；无效 ⇒ FAIL。

    没有文件**不是错误**（需求 §6.1：零行为变化），所以是 WARN + 下一步：把示例抄成
    本机台架文件。坏了则是 FAIL —— 那种文件服务本来就会拒绝启动。
    """
    title = "本机台架文件"
    expected = "本机专属值集中在一份台架文件里（照 bench.example.json 抄）"
    example = "bench.example.json"
    try:
        raw = probe.bench_state()
    except Exception as error:  # noqa: BLE001 - 问不出来就如实说问不出来
        raw = {"state": "unknown", "file": "", "from": "", "problems": [f"{type(error).__name__}: {error}"]}
    state = str((raw or {}).get("state", "unknown") or "unknown")
    path = str((raw or {}).get("file", "") or "")
    where = str((raw or {}).get("from", "") or "")
    problems = [str(item) for item in ((raw or {}).get("problems") or ())]
    if state == "ok":
        count = (raw or {}).get("entries")
        shown = f"{path}（{where}）；自述 {count} 个值" if count is not None else f"{path}（{where}）"
        return Check(
            id="platform.bench.file",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title=title,
            group="bench",
            actual=shown,
            expected=expected,
        )
    if state in ("uninstalled", "absent"):
        # 两种情形都是"没有台架文件"：一个还没装、一个默认位置没文件。
        return Check(
            id="platform.bench.file",
            scope=Scope.PLATFORM,
            status=Status.WARN,
            required=True,
            title=title,
            group="bench",
            actual=f"没有台架文件（找的是 {path or '默认位置'}）" if path else "没有台架文件",
            expected=expected,
            reason=(
                "本机专属值（工具安装目录 / 工程根 / 串口）现在**没有代码兜底**了："
                "目标层那几处会报「未配置」，相关的链不会登记（ / B6）"
            ),
            remediation=f"把 {example} 抄一份成 bench.local.json，填上这台机器的路径与端口",
        )
    if state == "invalid":
        first = problems[0] if problems else "（没给出具体问题）"
        return Check(
            id="platform.bench.file",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title=title,
            group="bench",
            actual=f"{path} 无效（{len(problems) or 1} 条问题）",
            expected=expected,
            reason=first + ("…" if len(problems) > 1 else ""),
            remediation=f"按上面每条问题改这份文件；不知道怎么写就照 {example} 抄",
        )
    return Check(
        id="platform.bench.file",
        scope=Scope.PLATFORM,
        status=Status.FAIL,
        required=True,
        title=title,
        group="bench",
        actual=f"台架探针给出了看不懂的状态：{state}",
        expected=expected,
        reason="无法判定台架文件情形（没判定不是没事）",
        remediation="报一个 issue 并附上本报告的 JSON",
    )


def _resolve_vendor_tools(
    declared: Mapping[str, Sequence[str]] | None,
) -> dict[str, str]:
    """把**目标层声明的**可执行文件名解析成路径（core 只做 `which`，不认识任何厂商）。

    ⚠️ 名字**不能写在这里**：`core` 里出现厂商工具名会被
    `platform_boundary.token_hit_problems` 当场拦下（那是目标层的知识）。
    """
    rows: dict[str, str] = {}
    for label, candidates in dict(declared or {}).items():
        found = ""
        for candidate in candidates:
            located = shutil.which(str(candidate))
            if located:
                found = located
                break
        rows[str(label)] = found
    return rows


def _check_vendor_tools(probe: PlatformProbe) -> Check:
    """厂商工具：**缺失只影响各自的 Adapter**（需求 §6.13）。

    ⚠️ 这一项**永不 FAIL，也不 WARN**：把平台当通用串口工具用（枚举 / 嗅探 / 连接 /
    收发）与框架自检都不需要它们；它们只决定“能不能构建 / 烧录 / 符号化那几块板”。
    把它报成警告，会让第一次上手的人以为“我的环境有问题”，而其实什么都没错。

    工具名由**目标层**声明（`TargetDeclaration` 那一侧的 `vendor_tools()`），
    这里只把它们逐个 `which` 一遍。
    """
    declared = _call(probe.vendor_tools, None)
    rows = _resolve_vendor_tools(declared)
    missing = [name for name, path in rows.items() if not str(path or "").strip()]
    actual = "、".join(
        f"{name}{'（已找到）' if str(path or '').strip() else '（没找到）'}"
        for name, path in rows.items()
    ) or "（目标层没有声明厂商工具）"
    if missing:
        actual += "；缺的只影响对应 Adapter 的构建/烧录，不影响通用串口模式"
    return Check(
        id="platform.tooling.vendor",
        scope=Scope.PLATFORM,
        status=Status.PASS,
        required=False,
        title="厂商工具（只影响对应 Adapter）",
        group="tooling",
        actual=actual,
        expected=(
            "有它就能构建/烧录对应平台；没有只影响那一路 Adapter，"
            "枚举、嗅探、连接与收发不受影响"
        ),
    )


def _check_serial(probe: PlatformProbe) -> Check:
    """基础串口依赖：**影响面要说清**（需求 §6.13）。

    这一项管的是“能不能把平台当**通用串口工具**用”：系统端口枚举、嗅探、连接、收发
    全都靠它。它**不影响**框架自检、契约测试、Web 演示与厂商 Adapter —— 后者缺的是
    各自的工具链（见 `platform.tooling.vendor`）。
    """
    backend = str(_call(probe.transport_backend, "") or "")
    ports = list(_call(probe.enumerate_ports, ()) or ())
    enumeration = f"本机枚举到 {len(ports)} 个串口" if ports else "本机没有枚举到串口（或枚举不可用）"
    if backend and "缺少" not in backend:
        return Check(
            id="platform.serial.backend",
            scope=Scope.PLATFORM,
            status=Status.PASS,
            required=True,
            title="基础串口能力（backend）",
            group="serial",
            actual=f"{backend}；{enumeration}",
            expected="可导入的串口实现，且具备端口枚举能力（通用串口模式可用：嗅探 / 连接 / 收发）",
        )
    if backend:
        return Check(
            id="platform.serial.backend",
            scope=Scope.PLATFORM,
            status=Status.FAIL,
            required=True,
            title="基础串口能力（backend）",
            group="serial",
            actual=backend,
            expected="可导入的串口实现，且具备端口枚举能力（通用串口模式可用：嗅探 / 连接 / 收发）",
            reason="串口实现装得不完整（枚举端口的能力缺失）⇒ 系统串口枚举与通用串口模式不可用",
            remediation="重装串口依赖：`pip install --force-reinstall pyserial`",
        )
    return Check(
        id="platform.serial.backend",
        scope=Scope.PLATFORM,
        # 本仓库：pyserial 是**可选**依赖 —— 框架自检、契约测试与 Web 演示都不需要它；
        # 只有“当通用串口工具用”与接真机才必须。所以缺它是 WARN，不是 FAIL。
        status=Status.WARN,
        required=False,
        title="基础串口能力（backend）",
        group="serial",
        actual="没有可用的串口实现",
        expected="可导入的串口实现，且具备端口枚举能力（通用串口模式可用：嗅探 / 连接 / 收发）",
        reason=(
            "没有 pyserial ⇒ **基础串口能力不可用**：普通模式下的系统串口枚举、嗅探、连接与收发都要它；"
            "框架自检、契约测试、Web 演示与厂商 Adapter 不受影响"
        ),
        remediation="装上它就能当通用串口终端用：`pip install pyserial`（装完重启 Web 即可）",
    )


_OPTIONAL_TOOLS = {
    "node": {
        "id": "platform.tooling.node",
        "title": "Node.js（开发/验收用）",
        "reason": "本机没有 Node.js：浏览器端回归与 Playwright 都跑不了",
        "remediation": "安装 Node.js（仅开发/验收需要；不影响平台运行与硬件体检）",
    },
    "browser": {
        "id": "platform.tooling.browser",
        "title": "浏览器验收能力（Playwright）",
        "reason": "本机没有可用的 Playwright + Chromium：浏览器 E2E 会被跳过",
        "remediation": "安装 Node.js 后执行 `npx playwright install chromium`（仅开发/验收需要）",
    },
    "git": {
        "id": "platform.tooling.git",
        "title": "Git（版本证据用）",
        "reason": "本机没有 Git：构建身份里的 git 信息与版本证据会缺失",
        "remediation": "安装 Git（仅开发/版本证据需要；不影响平台运行）",
    },
}


def _check_optional_tool(probe: PlatformProbe, key: str) -> Check:
    meta = _OPTIONAL_TOOLS[key]
    backend = str(_call(getattr(probe, f"{key}_backend"), "") or "")
    if backend:
        return Check(
            id=str(meta["id"]),
            scope=Scope.PLATFORM,
            status=Status.PASS,
            title=str(meta["title"]),
            group="tooling",
            actual=backend,
            expected="可用（可选能力）",
        )
    return Check(
        id=str(meta["id"]),
        scope=Scope.PLATFORM,
        status=Status.WARN,
        title=str(meta["title"]),
        group="tooling",
        actual="未找到",
        expected="可用（可选能力）",
        reason=str(meta["reason"]),
        remediation=str(meta["remediation"]),
    )


# ===================================================================== 硬件检查
def hardware_checks(probe: PlatformProbe, *, enabled: bool) -> list[Check]:
    """硬件在线探测（需求 §2 第三层）：**默认不跑**，只在显式 `--hardware` 时枚举端口。

    枚举是只读的（只问系统有哪些端口名）；在线探针/握手还没实现（D6），
    所以它如实 `SKIP`，而不是假装"没连上就是没问题"。
    """
    if not enabled:
        return _labeled([
            Check(
                id="hardware.probe",
                scope=Scope.HARDWARE,
                status=Status.SKIP,
                title="硬件在线探测",
                group="hardware",
                actual="未请求",
                expected="显式请求（--hardware）时做只读枚举",
                reason="没有显式请求硬件探测；默认不枚举、不打开串口、不操作探针",
            )
        ])
    checks: list[Check] = []
    try:
        ports = sorted(str(name) for name in probe.enumerate_ports())
        detail = ", ".join(ports) if ports else "无"
        checks.append(
            Check(
                id="hardware.serial.enumerate",
                scope=Scope.HARDWARE,
                status=Status.PASS,
                title="串口枚举（只读）",
                group="hardware",
                actual=f"{len(ports)} 个串口：{detail}",
                expected="能列出串口；没有设备也算枚举成功",
            )
        )
    except Exception as error:  # noqa: BLE001 - 枚举失败要如实说，不编一个 0
        checks.append(
            Check(
                id="hardware.serial.enumerate",
                scope=Scope.HARDWARE,
                status=Status.SKIP,
                title="串口枚举（只读）",
                group="hardware",
                actual="枚举失败",
                expected="能列出串口；没有设备也算枚举成功",
                reason=f"串口枚举不可用：{type(error).__name__}: {error}"
                "（通常是没有串口实现；见平台层的串口 backend 检查）",
            )
        )
    checks.append(
        Check(
            id="hardware.serial.probe",
            scope=Scope.HARDWARE,
            status=Status.SKIP,
            title="在线探针 / 握手",
            group="hardware",
            actual="未实现",
            expected="受控的只读在线探测（D6 才接入）",
            reason="在线探针/握手尚未实现：本次只做只读枚举，没有打开、复位或写入任何串口",
        )
    )
    return _labeled(checks)

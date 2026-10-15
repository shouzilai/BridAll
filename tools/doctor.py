#!/usr/bin/env python3
""" / D2：平台体检的**命令行入口**。

在一台新电脑上、**不连接任何开发板**时执行一次只读体检，回答三件事：

1. 平台基本能力能不能跑；
2. 当前机器具备哪些目标适配前提、还缺什么；
3. 下一步应该安装、配置或验证什么。

```powershell
$py = "<你的工具目录>"
& $py tools\\doctor.py                 # 人读报告
& $py tools\\doctor.py --json          # 机器可读（同一份报告）
& $py tools\\doctor.py --target <id> --firmware <id>   # 只看某个目标（D3 接入后）
& $py tools\\doctor.py --hardware       # 额外做一次只读串口枚举
```

退出码（需求 §5）：`0` = 无平台 `FAIL`；`1` = 存在平台阻断；`2` = Doctor 自身无法完成报告。
**目标依赖缺失只给 `WARN`，不制造非零退出码**；`SKIP` 也从不被写成 `PASS`。

这个文件是**组合入口**（和 `server.py` 一样）：它把 core 的检查模型接上真实的探针 ——
装配探针来自 `server.build_web_assembly()`（只读，证据目录指到系统临时目录、检查完就删），
端口占用者用工作区锁与系统监听表来区分"本平台的服务"与"别的程序"。core 本身不认识这些。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Sequence

TOOLS_DIR = Path(__file__).resolve().parent
CONSOLE_DIR = TOOLS_DIR / "webconsole"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from core_primitives import utc_now  # noqa: E402  (core：时间戳的唯一出处)
import bench_config  # noqa: E402  (core： 台架配置层)
import bench_setup  # noqa: E402  (core：装配处的台架声明与入口安装)

#: 随发布的参考适配住在这儿（`<repo>/targets`）：目标层模块在下面按名字 import。
TARGETS_DIR = Path(__file__).resolve().parent.parent / "targets"
if str(TARGETS_DIR) not in sys.path:
    sys.path.insert(0, str(TARGETS_DIR))

# ⚠️ 目标层的 import 是**显式**的（不是动态 `__import__`）：边界清单能把这两条
# 装配处依赖逐条记下来。只有组合入口（本文件）许 import 它们。
import esp32_target  # noqa: E402  (目标层)
import stm32_target  # noqa: E402  (目标层)
from doctor_model import (  # noqa: E402  (core：D0 契约)
    EXIT_INCOMPLETE,
    Report,
    exit_code,
    render_lines,
)
import doctor_platform  # noqa: E402  (core：D1 平台检查)
import doctor_target  # noqa: E402  (core：D3 目标扩展接口)
from doctor_platform import PlatformPaths, PlatformProbe  # noqa: E402  (core)

#: 平台工作目录（`仓库根/`）——证据目录与工作区锁都相对它定位。
DEFAULT_ROOT = TOOLS_DIR.parent


# ================================================================ 组合探针（CLI 专属）
def _port_owner(port: int) -> str:
    """这个端口是不是**本平台自己**的服务占用（`platform` / `other` / `""` 未知）。

    读工作区锁拿持有者 pid，再和系统监听表里那个 pid 对一下 —— 两边都只读：
    不续占锁、不发请求、不强停任何进程。
    """
    pid = doctor_platform.listening_pid(port)
    if pid is None:
        return ""
    try:
        from console_lock import read_owner
    except ImportError:  # 直接把 CLI 当脚本跑时的回退
        read_owner = None  # type: ignore[assignment]
    owner = read_owner(DEFAULT_ROOT) if read_owner is not None else None
    if owner is not None and int(getattr(owner, "pid", -1)) == int(pid):
        return "platform"
    return "other"


def _assembly_problems() -> list[str] | None:
    """把装配建起来看有没有问题（**只读**：证据目录指到系统临时目录，检查完删掉）。

    直接问 `server.build_web_assembly()`（唯一的装配函数），所以 Doctor 与页面看到的
    是同一份装配；核心层拿不到它时（没有探针）那一项会如实 `FAIL`，不会假装通过。

    ⚠️ B6：这台机器**没写台架文件**时，一部分链根本装不起来（工程根没有值）。
    那不是"平台装不起来"，而是"这台机器还没告诉平台它在哪儿干活" ⇒ 返回带
    `UNCONFIGURED_PREFIX` 的理由，装配那一项会报 WARN + 下一步（而不是 FAIL）。
    """
    from server import build_web_assembly  # noqa: PLC0415 - 组合入口才需要

    staging = Path(tempfile.mkdtemp(prefix="pw-doctor-assembly-"))
    try:
        try:
            assembly = build_web_assembly(runs_dir=staging)
        except bench_config.NotConfigured as error:
            return [f"{doctor_platform.UNCONFIGURED_PREFIX}{error.slot} 没有值（{error}）"]
        # ⚠️ B6：装配处会把"没配置而没登记的链"如实带出来（它**不**拒绝启动）—— 那几条
        # 要算进这一项的结论里，否则两套 STM32 装配没登记、这一项却写着"问题列表为空"。
        skipped = [
            f"{doctor_platform.UNCONFIGURED_PREFIX}{line}"
            for line in getattr(assembly, "bench_skipped", ())
        ]
        return skipped + [str(item) for item in assembly.problems()]
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _esp32_declaration() -> doctor_target.TargetDeclaration | None:
    """ESP32 参考适配的体检声明（随仓库发布，所以这里真的有这条）。

    ⚠️ 目标层模块只在**组合入口**（本文件）里 import：core 只按
    `TargetDeclaration` 的形状消费，不认识任何一块板。
    """
    return esp32_target.doctor_declaration()


def _stm32_declaration() -> doctor_target.TargetDeclaration | None:
    """STM32 参考适配的体检声明（同上）。"""
    return stm32_target.doctor_declaration()


def target_declarations() -> tuple[doctor_target.TargetDeclaration, ...]:
    """本机登记的目标声明（ / D4：**组合处**的职责）。

    每个目标自己投影出声明（工具/工程入口/产物/烧录参数/字典），core 只按
    `TargetDeclaration` 的形状消费，不认识任何一块板。目标层的 import 是**显式**的
    （不是动态 `__import__`），所以边界清单能把这两条装配处依赖逐条记下来。
    """
    found = (_esp32_declaration(), _stm32_declaration())
    return tuple(row for row in found if row is not None)


def _vendor_tools() -> dict[str, Sequence[str]]:
    """各目标声明的厂商可执行文件（**名字由目标层给**，core 不认识任何一块板）。"""
    rows: dict[str, Sequence[str]] = {}
    for module in (stm32_target, esp32_target):
        reader = getattr(module, "vendor_tools", None)
        if callable(reader):
            rows.update(reader())
    return rows


def default_probe() -> PlatformProbe:
    return PlatformProbe(
        port_owner=_port_owner,
        assembly_problems=_assembly_problems,
        vendor_tools=_vendor_tools,
    )


def probe_for_assembly(assembly: object | None = None) -> PlatformProbe:
    """给**已经在跑的服务**用的探针：装配问题从**那一份现成的装配**读，不另建一份。

    CLI 那一份（`default_probe`）会在系统临时目录里临时建一份装配、问完就删；
    服务里那份是现成的（`server.build_web_assembly()` 建的），所以直接问它 ——
    两条路问的是**同一个函数**（`WebAssembly.problems()`），结论一致。
    """
    if assembly is None:
        return default_probe()
    return PlatformProbe(
        port_owner=_port_owner,
        assembly_problems=lambda: [str(item) for item in assembly.problems()],
    )


# ===================================================================== 报告组装
#: 报告标题（CLI 与 Web 用同一份 —— 它们展示的就是同一次体检的同一个模型）。
DEFAULT_LABEL = "平台基础体检（）"


def compose_report(
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    hardware: bool = False,
    root: Path | str | None = None,
    requested_target: str = "",
    requested_firmware: str = "",
    probe: PlatformProbe | None = None,
    declarations: Sequence[doctor_target.TargetDeclaration] | None = None,
    label: str = DEFAULT_LABEL,
) -> Report:
    """把一次体检的全部检查项装起来（**CLI 与 Web 共用这一个函数**）。

    两条入口只差三件事：探针（网页拿已经在跑的那份装配问问题）、
    `hardware`（两边默认都不做硬件探测）与端口（网页查的是**自己正在监听**的那个）。
    这保证页面与终端看到的是**同一个模型**算出来的同一份报告。
    """
    active = probe if probe is not None else default_probe()
    paths = PlatformPaths.under(Path(root) if root is not None else DEFAULT_ROOT)
    checks = list(
        doctor_platform.platform_checks(active, paths, host=host, port=int(port))
    )
    checks.extend(
        doctor_target.target_checks(
            target_declarations() if declarations is None else tuple(declarations),
            requested_target=requested_target,
            requested_firmware=requested_firmware,
        )
    )
    checks.extend(doctor_platform.hardware_checks(active, enabled=bool(hardware)))
    return Report(checks=tuple(checks), label=label, generated_utc=utc_now())


def build_report(args: argparse.Namespace, probe: PlatformProbe | None = None) -> Report:
    """CLI 入口：把命令行参数翻译成 `compose_report` 的参数。"""
    return compose_report(
        host=args.host,
        port=args.port,
        hardware=bool(args.hardware),
        root=args.root,
        requested_target=args.target or "",
        requested_firmware=args.firmware or "",
        probe=probe,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="把报告打成 JSON（机器可读）")
    parser.add_argument("--target", default="", help="只看某个目标（D3 接入后生效）")
    parser.add_argument("--firmware", default="", help="只看某个固件（D3 接入后生效）")
    parser.add_argument(
        "--hardware",
        action="store_true",
        help="额外做一次只读串口枚举（仍然不打开、不复位、不写入）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="端口检查的地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8770, help="端口检查的端口（默认 8770）")
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="平台工作目录（默认平台工作目录）",
    )
    parser.add_argument(
        "--bench",
        default="",
        help="台架文件（；默认按 --bench > PW_BENCH_FILE > bench.local.json 找）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        from unicode_console import install as _install
    except ImportError:
        _install = None
    if _install is not None:
        _install()

    args = parse_args(argv)
    try:
        #  / B2：体检要照着**这台电脑**的台架看（工程根/工具目录/串口）。
        # 入口装一次；库代码自己不会去读文件。
        bench_setup.install_from(cli_path=args.bench)
    except bench_config.BenchFileError as error:
        # 与其它入口**同一个码**（需求 §6.1）——`EXIT_INCOMPLETE` 留给"体检自己跑不完"。
        print(f"台架文件无效，拒绝开始体检：{error}", file=sys.stderr)
        return bench_config.EXIT_REFUSED
    try:
        report = build_report(args)
    except Exception as error:  # noqa: BLE001 - Doctor 自己跑不完就是退出码 2
        print(
            f"体检自身无法完成：{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE

    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        for line in render_lines(report):
            print(line)
    return exit_code(report) if report.checks else EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""统一硬件操作入口（**核心的上层外壳**）。

```text
python tools\\hwops.py list                     # 这台机器上装配了哪些接入契约
python tools\\hwops.py check                    # 机器检查（完整性 / 旁路 / 判据 / 串用 / Core 泄漏）
python tools\\hwops.py run --adapter <id> --steps preflight,build
python tools\\hwops.py run --adapter <id> --steps preflight,flash,verify \\
        --confirm-flash --allow-overwrite
python tools\\hwops.py run --adapter <id> --steps preflight,debug-check --port <PORT>
python tools\\hwops.py create-adapter --out <目录> --id <适配器 id>
python tools\\hwops.py fuse --clear
python tools\\hwops.py opensource-audit
```

三步都调**同一份**操作服务（`webconsole/hwops_runner.py`）：这里只做两件事 ——
把声明与本机值装配起来、把结论打印/落盘。判据、故障码、熔断全在下层，本文件里
**不许**出现"按步骤名挑判据"或任何厂商命令字。

⚠️ 烧录永远要两条显式确认（`--confirm-flash --allow-overwrite`）：没有它们，
`flash` 这一步会以 `refused` 收场，并且**一个字节都不写**。

退出码：`0` 通过；`1` 有失败；`2` 被拒（安全闸门 / 熔断，需求 §3 的"拒绝开始"）；
`3` 没失败但也没跑完（跳过 / 不支持 / 未核验）。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
CONSOLE_DIR = TOOLS_DIR / "webconsole"
for _directory in (str(CONSOLE_DIR), str(TOOLS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import bench_config  # noqa: E402  (core：台架配置层)
import bench_setup  # noqa: E402  (core：台架声明与入口安装)
import console_input  # noqa: E402  (core：文本 → 字节的唯一一处转换)
import core_primitives  # noqa: E402  (core：读超时的唯一出处)
import hwops_check as check_mod  # noqa: E402  (core：机器检查)
import hwops_contract as contract_mod  # noqa: E402  (core：契约形状)
import hwops_runner as runner_mod  # noqa: E402  (core：统一操作服务)
import hwops_scaffold as scaffold_mod  # noqa: E402  (core：脚手架)

#: 证据落点（与别的工具体一致：`artifacts/**` 不进仓库）。
EVIDENCE_RELATIVE = ("artifacts", "hwops")
#: 读串口的超时（**与实时观察同一个出处**：`core_primitives`）。
READ_TIMEOUT_S = core_primitives.READ_TIMEOUT_S
#: ⚠️ 问一句之前先静一下（开口的那一瞬间线路上可能有悬空字节）。
#: 真机现场（2026-10-13）：设备明明答得出来（手工问一句就有 `@PWINFO`），
#: 但入口**开口就问**时拿不到 —— 第一句被脏字节带跑是这类板子的老毛病。
QUERY_SETTLE_S = 0.3
#: 熔断账本（同一目标 + 同一故障码的连续次数，跨次调用有效）。
FUSE_FILE = "fuse.json"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_REFUSED = bench_config.EXIT_REFUSED   # 2：拒绝开始（与平台其它入口同一个码）
EXIT_INCOMPLETE = 3


# ============================================================ 装配处（叫出实现的名字）
#: 本机可以额外登记接入契约的地方（**本机配置，不是代码**）：
#: `PW_HWOPS_TARGETS` 里写逗号分隔的“模块名”或“到 .py 的路径”。
#: 有了它，接一条新硬件**不必改 core**：你的声明放哪都行，入口按这一行找到它。
TARGETS_ENV = "PW_HWOPS_TARGETS"


def _load_target(entry: str):
    """按一个条目把目标模块找出来（模块名 或 文件名）。

    ⚠️ 判“它是不是路径”看**后缀与分隔符**，不看它存不存在：写错了要给一句
    “找不到”，而不是惄惄地当成模块名再去 import 一遍。
    """
    import importlib
    import importlib.util

    entry = str(entry).strip()
    if not entry:
        raise ValueError("空的条目")
    looks_like_path = entry.endswith(".py") or "/" in entry or "\\" in entry
    if not looks_like_path:
        return importlib.import_module(entry)
    path = Path(entry).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到这份声明：{path}")
    name = f"hwops_target_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"加载不了这份声明：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collect(
    entries: Iterable[str],
) -> tuple[list[contract_mod.AdapterWiring], list[str], list[str], dict[str, str]]:
    """把每个条目交出来的接入契约收起来（交不出来就记一句，不假装有）。

    返回值：契约 / 提示 / **交不出来**的条目 / “模块名 → 它的源码”。
    ⚠️ 源码取**加载器拿到的那个文件**（而不是按名字再去目录里找）：外部模块常在
    `tools` 之外，按名字找不到时“按步骤名挑判据”那一条会**静默漏判**
    （2026-10-13 第三轮验收拓到）。
    ⚠️ “我让你加载这一个，你没交出来”是**错**，不是提示。
    """
    rows: list[contract_mod.AdapterWiring] = []
    notes: list[str] = []
    failed: list[str] = []
    sources: dict[str, str] = {}
    # ⚠️ 平台在哪儿，这个入口知道 ⇒ 告诉它：外部模块常在仓库之外（脚手架生成的
    #    `_find_platform()` 从自己往上找不到 `tools` 就 SystemExit）。已设的不覆盖。
    os.environ.setdefault("PW_PLATFORM_TOOLS", str(TOOLS_DIR))
    for entry in entries:
        try:
            module = _load_target(entry)
        except SystemExit as error:
            # ⚠️ 生成物找不到平台时抛的是 SystemExit（它不在 `Exception` 底下）：
            #    不收住就会把整个入口带崩（2026-10-13 第三轮验收拓到）。
            notes.append(f"这个条目自己退出了（{entry}）：{error}——它是不是放在仓库之外？")
            failed.append(entry)
            continue
        except Exception as error:  # noqa: BLE001 - 找不到就是找不到，如实说
            notes.append(f"这个条目没有交出接入契约：{entry}（{type(error).__name__}: {error}）")
            failed.append(entry)
            continue
        where = getattr(module, "__file__", "")
        if where and Path(where).is_file():
            try:
                text = Path(where).read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text:
                # ⚠️ 记**两个键**：加载器给模块起的内部名（`hwops_target_x`）与**文件名**。
                #    声明里的 `module` 字段写的是文件名那个（`labkit_fw`），只记前者会
                #    让“有没有拿到源码”这一判据永远说没有（2026-10-13 第三轮验收拓到）。
                sources[str(getattr(module, "__name__", entry))] = text
                sources[Path(where).stem] = text
        factory = getattr(module, "hwops_wirings", None)
        if not callable(factory):
            notes.append(
                f"{entry} 里没有 `hwops_wirings()` ⇒ 它没有接入契约"
                "（一个目标要能被入口用，得交出一个这个函数；见接入指令）"
            )
            failed.append(entry)
            continue
        try:
            rows.extend(factory())
        except Exception as error:  # noqa: BLE001
            notes.append(f"{entry} 交出接入契约时出错了：{type(error).__name__}: {error}")
            failed.append(entry)
    return rows, notes, failed, sources


def _wirings(
    spec: str = "",
) -> tuple[list[contract_mod.AdapterWiring], list[str], list[str], dict[str, str]]:
    """把各目标交进来的接入契约收集起来（**装配处唯一该做的事**）。

    返回值：契约 / 提示 / 本机配置里**交不出来**的那几条（后者决定退出码）/ “模块名 → 源码”。

    ⚠️ 这份**清单不住了 core 里**：不传 `spec` 时问平台的装配处（`bench_setup`）
    要一份默认清单；`spec`（`--targets` / `PW_HWOPS_TARGETS`）可以再添“模块名或
    .py 路径”—— 那就是接一条新硬件需要的全部动作，**不必改 core**（需求 §7.7）。

    ⚠️ 默认清单那条路用 `from <模块> import <函数>`（而不是 `import <模块>` 再取属性）：
    模块名一旦以属性访问的形式出现在 core 里，平台边界的那张表就会把它记成
    "core 认识平台"。导入是**装配处**的正当依赖（同名目标在 `DECLARED_EDGES` 里登记）。
    """
    extra = [item for item in str(spec or "").split(",") if item.strip()]
    if not extra:
        extra = [
            item for item in str(os.environ.get(TARGETS_ENV, "")).split(",") if item.strip()
        ]
    # ⚠️ `--targets` 是**额外**的（平台默认清单照旧在里面）：新接一条硬件时，
    #    你既不想让入口丢掉仓库里原有的那些，也不想为了让它看见你的模块去改 core。
    # 默认清单：平台装配处登记的那几份（新增**随仓库发布**的目标 = 在那边加一行）。
    from bench_setup import hwops_target_modules as _default_targets

    rows, notes, _, sources = _collect(list(_default_targets()))
    if extra:
        # ⚠️ 显式给我的那几条**得交出来**：交不出来就是错（不是提示）——
        #    裸模块名命中命名空间包时，以前屏幕上只剩一句“注意”而退出码是 0。
        extra_rows, extra_notes, failed, extra_sources = _collect(extra)
        rows.extend(extra_rows)
        notes.extend(extra_notes)
        sources.update(extra_sources)
        return rows, notes, failed, sources
    return rows, notes, [], sources


def _find(wirings: Sequence[contract_mod.AdapterWiring], adapter_id: str):
    for row in wirings:
        if row.adapter_id == str(adapter_id):
            return row
    return None


# ============================================================ 本机执行器（真跑命令）
def subprocess_executor(*, timeout_override: float = 0.0) -> Callable[..., runner_mod.ToolRun]:
    """本机执行器：跑一条受控命令，把退出码、输出与耗时如实交回来。

    ⚠️ **附加到目标的命令超时也不杀**（`CommandSpec.attach`）：把握着
    调试探针的进程强杀下去，探针要拔插才能恢复。所以那一路只报"进程还在"。
    """
    import subprocess

    def run(spec) -> runner_mod.ToolRun:
        timeout = timeout_override or float(spec.timeout_s)
        if spec.attach:
            try:
                completed = subprocess.run(
                    list(spec.argv), cwd=str(spec.cwd), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as expired:
                return runner_mod.ToolRun(
                    returncode=None,
                    output=_held(expired.output) + _held(expired.stderr),
                    seconds=timeout, timed_out=True,
                )
            except OSError as error:
                return runner_mod.ToolRun(returncode=None, error=str(error))
            return runner_mod.ToolRun(
                returncode=completed.returncode,
                output=(completed.stdout or "") + (completed.stderr or ""),
                seconds=0.0,
            )
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(spec.argv), cwd=str(spec.cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as expired:
            return runner_mod.ToolRun(
                returncode=None,
                output=_held(expired.output) + _held(expired.stderr),
                seconds=time.perf_counter() - started, timed_out=True,
            )
        except OSError as error:
            return runner_mod.ToolRun(returncode=None, error=str(error))
        return runner_mod.ToolRun(
            returncode=completed.returncode,
            output=(completed.stdout or "") + (completed.stderr or ""),
            seconds=time.perf_counter() - started,
        )

    return run


def _held(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# ============================================================ 串口探针（只读打开）
def serial_reader(
    wiring: contract_mod.AdapterWiring, *, baud: int, seconds: float,
    port: str = "", query: str = "", line_ending: str = "",
):
    """开一次只读通信口，收 `seconds` 秒的行（**不写调制线 ⇒ 不复位设备**）。

    `query` 非空时先发一条（**只发送声明里的那一句**，行结束符也由声明给）。
    这是"调试 / 通信验证"这一步的全部：它能证明"口打得开、问得出回答"，
    但它**不冒充**任何协议判定 —— 行怎么解释由目标的身份解析器决定。
    ⚠️ 串口实现**由装配处交进来**（`wiring.port_factory`）：这个外壳不认识任何串口库。
    ⚠️ **打开姿势照抄实时观察**（先设读超时、再 `open()`）：`port_factory` 造出来的
    只是一个**还没打开**的句柄，不 `open()` 就去读会得到一句 `ClearCommError failed`
    （真机踩到过：两个口都报"句柄无效"，看着像两条串口同时坏了）。
    """
    port = str(port or wiring.port or "")
    if not port:
        return None, "没有通信口可用（台架键没配置，也没有 --port）"
    if wiring.port_factory is None:
        return None, "这条链没有交进串口实现 ⇒ 平台判不了这一项"
    try:
        handle = wiring.port_factory(port, int(baud))
    except Exception as error:  # noqa: BLE001 - 打不开就是打不开，如实说
        return None, f"打不开 {port}：{type(error).__name__}: {error}"
    buffer = b""
    problem = ""
    try:
        try:
            handle.timeout = READ_TIMEOUT_S
        except Exception:  # noqa: BLE001 - 替身句柄不一定有 timeout
            pass
        handle.open()
    except Exception as error:  # noqa: BLE001
        return None, f"打不开 {port}：{type(error).__name__}: {error}"
    if query:
        try:
            # 先静一下、把开口时线路上的悬空字节丢掉，再问 —— 顺序与理由见 QUERY_SETTLE_S。
            time.sleep(QUERY_SETTLE_S)
            try:
                handle.reset_input_buffer()
            except Exception:  # noqa: BLE001 - 替身句柄不一定有这一项
                pass
            payload, display, _source = console_input.build_payload(
                mode="text", text=query, line_ending=line_ending or "lf"
            )
            handle.write(payload)
            handle.flush()
        except Exception as error:  # noqa: BLE001
            problem = f"发 {query!r} 失败：{type(error).__name__}: {error}"
    # ⚠️ 读的过程中**什么都可能发生**（真机踩到过：口被别的进程拿走 / 句柄失效）。
    #    探针必须自己收住并如实说明，**不许**把整份操作打断 ——
    #    一个只读的连通性检查不该让一次构建的证据全丢。
    try:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            try:
                waiting = getattr(handle, "in_waiting", 0) or 0
                chunk = handle.read(max(1, min(4096, waiting or 1)))
            except Exception as error:  # noqa: BLE001
                problem = f"读 {port} 时串口出错：{type(error).__name__}: {error}"
                break
            if chunk:
                buffer += bytes(chunk)
            time.sleep(0.05)
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - 关不掉也要往下走（结论已经拿到）
            pass
    text = buffer.decode("utf-8", "replace")
    lines = [
        line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    if problem and not buffer:
        return None, problem
    if problem:
        lines.append(f"（读取出错：{problem}）")
    return lines, problem


def _identity_probe(
    wiring: contract_mod.AdapterWiring, *, baud: int, seconds: float, send: Callable
):
    """读一次身份：按声明的 `identity.query` 问一句，再拿目标的解析器解释回答。

    ⚠️ **「没拿到」必须带回理由**（`IdentityProbe.detail` / `.fault`）：打不开串口、
    一行没收到、收到行但没身份句，这三种的下一步完全不同，而它们以前都被压成同一句
    「没拿到设备自报」（真机踩到：实因是通信口被另一个工作台的进程占着）。
    """

    def probe() -> runner_mod.IdentityProbe:
        query = str(wiring.declaration.identity.query or "")
        port = str(wiring.port or "")
        lines, problem = send(query)
        if lines is None:
            return runner_mod.IdentityProbe(
                detail=(
                    f"打不开通信口（{port}）⇒ 未核验：{problem}。"
                    "这多半是**另一个进程正占着它**（例如别的工作台的会话）——"
                    "先让占用者放手、再重跑，不要当设备或固件故障去查"
                ),
                fault="transport_unavailable",
            )
        real = [line for line in lines if line.strip()]
        if not real:
            return runner_mod.IdentityProbe(
                detail=(
                    f"口打得开（{port}），但 {seconds:g} 秒里**一行都没收到** ⇒ 未核验："
                    "设备没在跑、或者根本没在打（不是核验失败，也不是身份不符）"
                ),
            )
        parser = wiring.identity_parser
        for line in real:
            fields = parser(line) if parser is not None else None
            if fields:
                return runner_mod.IdentityProbe(
                    observed=dict(fields), lines=tuple(real)
                )
        return runner_mod.IdentityProbe(
            detail=(
                f"口打得开、也收到了 {len(real)} 行，但**没有一句**能被身份解析器认出来 ⇒ 未核验："
                "设备在跑，但不报身份（查固件有没有把身份编进去、协议对不对）"
            ),
            lines=tuple(real),
        )

    return probe


# ============================================================ 子命令
def cmd_list(args: argparse.Namespace) -> int:
    # ⚠️ 不给子命令时也会走到这里（那时 `--json` 不存在）⇒ 用 getattr 取。
    as_json = bool(getattr(args, "json", False))
    wirings, notes, failed, _sources = _wirings(getattr(args, "targets", ""))
    if as_json:
        print(json.dumps([row.to_json() for row in wirings], ensure_ascii=False, indent=2))
    else:
        print(f"这台机器上装配了 {len(wirings)} 条接入契约：")
        for row in wirings:
            print(f"  - {contract_mod.summary_of(row.declaration)}")
            if row.port:
                print(f"      通信口 {row.port}")
    for note in notes:
        print(f"  注意：{note}")
    if failed:
        print(
            "本机配置里这几条**没有交出接入契约**：" + "、".join(failed)
            + "（上面那几句「注意」写着为什么）",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return EXIT_PASS


def _bench_file_configured() -> bool:
    """这台机器上有没有一份**可读的**台架文件（没有 ⇒ 台架键那一项不判）。"""
    try:
        return bool(bench_config.active().file.configured)
    except Exception:  # noqa: BLE001 - 问不出来就按"没配"处理
        return False


def cmd_check(args: argparse.Namespace) -> int:
    explicit = bool(str(getattr(args, "targets", "") or "").strip())
    wirings, notes, failed, loaded_sources = _wirings(getattr(args, "targets", ""))
    declarations = [row.declaration for row in wirings]
    registered: dict[str, list[str]] = {}
    for row in wirings:
        registered.setdefault(row.declaration.target_id, []).extend(row.registered)
    # ⚠️ 源码**先用加载器拿到的那个文件**（外部模块常在 `tools` 之外，按名字
    #    找不到时“按步骤名挑判据”那一条会静默漏判；2026-10-13 第三轮验收拓到）；
    #    平台自带模块再退回按名字找。
    sources: dict[str, str] = dict(loaded_sources)
    for row in wirings:
        if str(row.declaration.module) in sources:
            continue
        text = _module_source(row.declaration.module)
        if text:
            sources[row.declaration.module] = text
    # ⚠️ 带了 `--targets` ⇒ 那些模块**就是还没登记**的（登记是接入做完之后的动作），
    #    所以层名那条按“还没登记”的口径检 —— 否则指南让你跑的第二条命令
    #    会在一句“层名不在分层表里”上红，而那正是所有新接的模块的常态。
    # ⚠️ 台架键只有在这条路上判得了：它要的是“装配处交的 `bench_values`”（单份声明没有）。
    # ⚠️ **这台机器还没有台架文件时，这一项不判**（`bench=None`）：那时"没配上"不是
    #    "接错了"，而是"还没告诉框架它在哪儿干活" —— 报 `unjudged` 会淹没真正的接入问题，
    #    报 `problem` 会让新克隆的第一次 `check` 看起来是坏的。有台架文件时才逐键核对。
    bench_values = None
    if _bench_file_configured():
        bench_values = {row.adapter_id: dict(row.bench_values) for row in wirings}
    report = check_mod.check_declarations(
        declarations, registered=registered, sources=sources,
        bench=bench_values, strict_registration=not explicit,
    )
    problems = list(report.problems)
    unjudged = list(report.unjudged)
    if failed:
        # 显式给我的那几条没交出来 ⇒ 这就是问题（不是提示）：屏幕上不能只写“通过”。
        problems.extend(
            f"本机配置里的这一条没有交出接入契约：{item}（上面「注意」写着为什么）"
            for item in failed
        )
    if explicit:
        notes = list(notes) + [
            "这次带了 `--targets` ⇒ 层名按“尚未登记”的口径检查（登记是接入做完之后的动作）；"
            "要核“平台里那几条还在不在分层表里”，跑一次不带参数的 `check`"
        ]
    if not args.skip_core:
        problems.extend(check_mod.core_leakage_problems())
    external_note: dict[str, Any] = {}
    if args.module:
        extra, extra_problems, extra_info = _external_declaration(args.module, args.factory)
        problems.extend(extra_problems)
        external_note = {
            "module": args.module,
            "adapter_id": getattr(extra, "adapter_id", "") if extra else "",
            "loaded": extra is not None,
            "problems": extra_problems,
        }
        if extra is not None:
            # 它自己的登记表（有就用、没有就交给 unjudged 说话）+ 与平台契约一起比串用。
            own = dict(registered)
            if extra_info["registered"]:
                own[extra_info["target_id"]] = list(extra_info["registered"])
            extra_report = check_mod.check_declarations(
                [extra], registered=own, sources=dict(extra_info["source"]),
                compare_isolation=True, strict_registration=False,
            )
            problems.extend(extra_report.problems)
            unjudged.extend(extra_report.unjudged)
    document = {
        "declarations": list(report.declarations),
        "notes": notes,
        "ok": not problems,
        "problems": problems,
        "unjudged": unjudged,
    }
    if external_note:
        document["external"] = external_note
    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(f"检查了 {len(report.declarations)} 条接入契约：{'通过' if not problems else '有问题'}")
        if external_note:
            print(
                f"  外部声明 {external_note['module']}："
                + ("加载不了" if not external_note["loaded"] else "已加载")
                + f"（{external_note['adapter_id'] or '—'}）"
                + "  ← ⚠️ 它**不在**上面那个计数里（那个数只数平台自带的契约）"
            )
        for item in problems:
            print(f"  - {item}")
        for item in unjudged:
            print(f"  判不了：{item}")
        for note in notes:
            print(f"  注意：{note}")
    return EXIT_PASS if not problems else EXIT_FAIL


def _module_source(module: str) -> str:
    """一个模块的源码（判“按步骤名挑判据”用；读不到就返回空串）。"""
    name = f"{module}.py"
    for directory in (CONSOLE_DIR, TOOLS_DIR):
        path = Path(directory) / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    # ⚠️ 第三方模块常在**子目录**里（`webconsole/<平台>/<模块>.py`）：只找两个平铺
    #    目录，会让“按步骤名挑判据”那一条在这类模块上**静默不跑**
    #    （2026-10-13 验收：新 Agent 的两条命令各覆盖一半，这就是其中一半）。
    for directory in (CONSOLE_DIR, TOOLS_DIR):
        for path in sorted(Path(directory).rglob(name)):
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return ""


def _external_declaration(module_path: str, factory: str):
    """把一个**外部模块**里的声明加载起来检查（脚手架生成物的第一次体检）。"""
    import importlib.util
    import os

    path = Path(module_path)
    if not path.is_file():
        return None, [f"找不到模块：{path}"]
    # ⚠️ 生成物靠 `PW_PLATFORM_TOOLS` 找平台（它自己不写死任何绝对路径）。
    #    这个命令**知道**平台在哪，所以给它一个线索；已设的就不覆盖。
    os.environ.setdefault("PW_PLATFORM_TOOLS", str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location(f"_hwops_external_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 极罕见
        return None, [f"加载不了模块：{path}"]
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit as error:
        return None, [f"{path.name} 自己退出了（{error}）：它找不到平台？"]
    except Exception as error:  # noqa: BLE001 - 生成物还没填完是常态
        return None, [f"{path.name} 执行失败：{type(error).__name__}: {error}"]
    make = getattr(module, str(factory), None)
    if make is None:
        return None, [f"{path.name} 里没有 {factory}()"]
    try:
        declaration = make()
    except Exception as error:  # noqa: BLE001
        return None, [f"{path.name} 的 {factory}() 失败：{type(error).__name__}: {error}"]
    sources = {path.stem: path.read_text(encoding="utf-8")}
    problems = check_mod.declaration_problems(declaration, strict_registration=False)
    problems.extend(check_mod.duplicate_criteria_problems(declaration))
    # ⚠️ “按步骤名挑判据”**不在这里**判：源码要交给 `check_declarations(sources=…)`
    #    一次判（从前两处都判 ⇒ 同一条问题被打印两遍，2026-10-13 验收拓到）。
    # ⚠️ 把这份模块**自己的**受控适配器登记表一并交出去：它自带的 `ADAPTERS` 就是
    #    “步骤里的适配器该在哪儿”那份声明。不交的话，旁路检查会静默跳过
    #    （2026-10-13 验收：`--module` 当时漏掉了旁路与串用两顶）。
    registered = tuple(getattr(module, "ADAPTERS", ()) or ())
    info = {
        "source": sources,
        "registered": registered,
        "target_id": str(getattr(declaration, "target_id", "")),
    }
    return declaration, [f"{path.name}：{item}" for item in problems], info


def _fuse_path() -> Path:
    root = TOOLS_DIR.parent
    return root.joinpath(*EVIDENCE_RELATIVE, FUSE_FILE)


def _load_fuse() -> runner_mod.FuseState:
    path = _fuse_path()
    if not path.is_file():
        return runner_mod.FuseState()
    try:
        return runner_mod.FuseState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return runner_mod.FuseState()


def _save_fuse(state: runner_mod.FuseState) -> Path:
    path = _fuse_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return path


def _evidence_writer(adapter_id: str, *, enabled: bool):
    if not enabled:
        return None

    def write(document: dict[str, Any]) -> str:
        root = TOOLS_DIR.parent.joinpath(*EVIDENCE_RELATIVE)
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = root / f"{adapter_id}-{stamp}.json"
        # ⚠️ 同一秒里的第二次 run 会**盖掉**第一次（文件名只精确到秒）—— 而证据的规矩是
        #    只追加、不覆盖。占用了就往后找一个名字（2026-10-13 验收拓到）。
        suffix = 1
        while path.exists():
            path = root / f"{adapter_id}-{stamp}-{suffix}.json"
            suffix += 1
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        return str(path)

    return write


def cmd_run(args: argparse.Namespace) -> int:
    wirings, notes, failed, _sources = _wirings(getattr(args, "targets", ""))
    if failed:
        print(
            "本机配置里这几条**没有交出接入契约**：" + "、".join(failed), file=sys.stderr,
        )
        for note in notes:
            print(f"  注意：{note}", file=sys.stderr)
        return EXIT_REFUSED
    if not wirings:
        print("这台机器上没有任何接入契约：先看 `hwops.py list` 与 `doctor.py`",
              file=sys.stderr)
        for note in notes:
            print(f"  注意：{note}", file=sys.stderr)
        return EXIT_REFUSED
    wiring = _find(wirings, args.adapter) if args.adapter else _pick(wirings, args)
    if wiring is None:
        print(
            f"没有这条接入契约：{args.adapter!r}"
            f"（有的是 {[row.adapter_id for row in wirings]}）",
            file=sys.stderr,
        )
        # ⚠️ 加载失败的那份要说话：否则屏幕上只有一句“没有这条契约”，
        #    而真因（模块没交 `hwops_wirings()` / 导不进来）藏在 notes 里没人看。
        for note in notes:
            print(f"  注意：{note}", file=sys.stderr)
        return EXIT_REFUSED

    problems = wiring.problems()
    if problems:
        print("这份装配站不住，拒绝开始：")
        for item in problems:
            print(f"  - {item}")
        return EXIT_REFUSED

    declaration = wiring.declaration
    port = args.port or wiring.port
    resolver = runner_mod.RecipeResolver(
        project_dir=Path(wiring.project_dir),
        tools=dict(wiring.tools),
        config=declaration.build_config,
        port_name=port,
        bench_values=dict(wiring.bench_values),
    )
    wanted = tuple(_steps(args.steps)) if args.steps else contract_mod.STEP_ORDER
    seconds = float(args.seconds)
    transport_baud = int(args.baud or declaration.transport.baud or 115200)
    active_port = port or wiring.port

    def reader(query: str):
        """一次只读打开的包装（问身份与看连通性共用同一条函数）。"""
        return serial_reader(
            wiring, baud=transport_baud, seconds=seconds, port=active_port,
            query=query, line_ending=declaration.transport.line_ending,
        )

    debug_probe = None
    identity_probe = None
    if "debug-check" in wanted:
        def _debug() -> runner_mod.ProbeOutcome:
            lines, problem = reader("")
            if lines is None:
                return runner_mod.ProbeOutcome(
                    ok=False, unavailable=True, detail=problem,
                    fault="transport_unavailable",
                )
            if not lines:
                return runner_mod.ProbeOutcome(
                    ok=False, fault="transport_unavailable",
                    detail="口打开了，但一个字节都没读到（不冒充连通）",
                )
            return runner_mod.ProbeOutcome(
                ok=True, lines=tuple(lines), detail=f"读到 {len(lines)} 行"
            )

        debug_probe = _debug
    if "verify" in wanted:
        identity_probe = _identity_probe(
            wiring,
            baud=transport_baud,
            seconds=seconds,
            send=reader,
        )

    fuse = _load_fuse()
    # ⚠️ 用 `is not None` 判有没有 `--fake`：`nargs="*"` 在"给了开关但没给值"时是 `[]`，
    #    而空列表是假值 —— 拿它当真值判就会**悄悄去跑真工具**（本轮踩过）。
    executor = (
        _scripted_executor(args) if args.fake is not None else subprocess_executor(
            timeout_override=float(args.timeout or 0.0)
        )
    )
    report = runner_mod.run_operation(
        declaration,
        executor=executor,
        resolver=resolver,
        steps=wanted,
        fuse=fuse,
        evidence=_evidence_writer(wiring.adapter_id, enabled=not args.no_evidence),
        confirm_flash=bool(args.confirm_flash),
        allow_overwrite=bool(args.allow_overwrite),
        debug_probe=debug_probe,
        identity_probe=identity_probe,
        # ⚠️ **必须把装配处拿到的命令交下去**：有些步骤的 argv 由目标层给（STM32 的
        #    `stm32.cspy-download` 就是），不交下去时那一步会如实报"装配缺了一半"——
        #    而装配其实是齐的（2026-10-13 真机踩到：`list` 里明明有命令，`run` 说没有）。
        commands=dict(wiring.commands),
    )
    _save_fuse(report.fuse).parent  # 落了账本；路径本身不进屏幕（它随机器变）
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        for line in runner_mod.summarize(report):
            print(line)
        if report.evidence:
            print(f"证据：{report.evidence}")
    if report.result == "fail":
        return EXIT_FAIL
    if report.result == "refused":
        return EXIT_REFUSED
    if report.result == "pass":
        return EXIT_PASS
    return EXIT_INCOMPLETE


def _scripted_executor(args: argparse.Namespace):
    """`--fake`：拿平台自带的假工具跑一遍（没有硬件也能验通路与判据）。"""
    import hwops_fake as fake_mod

    script: dict[str, Any] = {}
    for item in args.fake or ():
        parts = str(item).split("=", 1)
        adapter_id = parts[0].strip()
        reaction = parts[1] if len(parts) > 1 else ""
        if reaction == "fail":
            script[adapter_id] = fake_mod.tool_run(
                code=1, output="Fatal error: injected for the smoke run\n"
            )
        elif reaction == "timeout":
            script[adapter_id] = fake_mod.tool_run(timed_out=True)
        elif reaction == "silent":
            script[adapter_id] = fake_mod.tool_run(code=1, output="")
        else:
            script[adapter_id] = fake_mod.tool_run(output="ok\n")
    return fake_mod.FakeExecutor(script={**fake_mod.default_script(), **script})


def _pick(wirings: Sequence[contract_mod.AdapterWiring], args: argparse.Namespace):
    """没点名适配器时怎么挑：给了 `--firmware` 就按它挑，否则**不猜**。"""
    if args.firmware:
        for row in wirings:
            if args.firmware in (row.adapter_id, row.declaration.build_config):
                return row
        return None
    if len(wirings) == 1:
        return wirings[0]
    return None


def _steps(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def cmd_create(args: argparse.Namespace) -> int:
    result = scaffold_mod.scaffold_adapter(
        args.out,
        adapter_id=args.id,
        target_id=args.target or "",
        label=args.label or "",
        layer=args.layer,
        module=args.module or "",
        project_entry=args.project_entry,
        build_config=args.config,
        baud=int(args.baud),
        line_ending=args.line_ending,
        identity_query=args.identity_query,
        project=args.project,
    )
    print("生成好了（三步接入：填 TODO → 假工具跑通 → 接真机）：")
    for name, value in result.to_json().items():
        print(f"  {name:<12} {value}")
    if args.no_verify:
        print("\n下一步：")
        print(f"  PW_PLATFORM_TOOLS={TOOLS_DIR} python {result.test}")
        print(f"  python {Path(__file__).name} check --module {result.declaration}")
        return EXIT_PASS
    # ⚠️ 「生成即可跑」：这里**当场**把生成物自带的契约测试跑一遍（拿平台自带的假工具），
    #    所以"脚手架能用"不是一句承诺，而是一次可复核的运行结果。
    import os
    import subprocess

    print("\n跑一遍生成物自带的契约测试（假工具，不接硬件）：")
    environment = dict(os.environ)
    environment["PW_PLATFORM_TOOLS"] = str(TOOLS_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(result.test)],
        env=environment, text=True, encoding="utf-8", errors="replace",
    )
    print(f"  生成物自检退出码 {completed.returncode}")
    if completed.returncode == 0:
        print(f"\n接入的第一步已经成立。接着填 TODO：{result.declaration}")
        print(f"机器检查：python {Path(__file__).name} check --module {result.declaration}")
    return EXIT_PASS if completed.returncode == 0 else EXIT_FAIL


def cmd_fuse(args: argparse.Namespace) -> int:
    state = _load_fuse()
    if args.clear:
        state.clear(args.adapter or "")
        path = _save_fuse(state)
        print(f"熔断账本已清除{'（只清 ' + args.adapter + '）' if args.adapter else ''}：{path}")
        return EXIT_PASS
    print(json.dumps(state.to_json(), ensure_ascii=False, indent=2))
    if state.strikes:
        print("\n要恢复：先人工处置（例：拔插一次探针），再 `fuse --clear`")
    return EXIT_PASS


# ------------------------------------------------------------------ 开源边界
#: 三档范围（默认 = **框架面**：核心的接入框架 + 公开入口文档，必须干净）。
#: - `framework`：核心框架自己的代码与入口文档（**必须干净**）；
#: - `platform`：框架代码 + 公开文档（不含测试）；
#: - `all`：全仓（含测试与示例）。
SCOPE_FRAMEWORK: tuple[str, ...] = (
    "tools/hwops.py",
    "tools/doctor.py",
    "tools/webconsole/*.py",
    "README.md",
    "INSTALL.md",
    "HANDOFF.md",
    "AGENT_WORKFLOW.md",
    "docs/*.md",
)
SCOPE_PLATFORM: tuple[str, ...] = (
    "tools/**/*.py",
    "docs/**/*.md",
    "README.md",
    "INSTALL.md",
    "HANDOFF.md",
    "AGENT_WORKFLOW.md",
)
SCOPE_ALL: tuple[str, ...] = (
    "tools/**/*.py",
    "targets/**/*.py",
    "tests/**/*.py",
    "examples/**/*.py",
    "docs/**/*.md",
    "*.md",
    "*.json",
)
SCOPES: dict[str, tuple[str, ...]] = {
    "framework": SCOPE_FRAMEWORK,
    "platform": SCOPE_PLATFORM,
    "all": SCOPE_ALL,
}
#: 默认范围**不算测试**：测试夹具里出现端口名是正常的历史/举例。
SKIPPED_DIR_PARTS = (
    "artifacts", "__pycache__", ".pio", "node_modules", "third_party", ".git",
)


def _gather(patterns: Iterable[str], *, include_tests: bool = False) -> list[tuple[str, str]]:
    root = TOOLS_DIR.parent
    rows: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            if any(part in SKIPPED_DIR_PARTS for part in path.parts):
                continue
            if not include_tests and path.name.startswith("test_"):
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows.append((str(path.relative_to(root)).replace("\\", "/"), text))
    return rows


def cmd_opensource(args: argparse.Namespace) -> int:
    if args.path:
        patterns: tuple[str, ...] = tuple(args.path)
    else:
        patterns = SCOPES.get(args.scope, SCOPE_FRAMEWORK)
    entries = _gather(patterns, include_tests=bool(args.include_tests))
    allow = tuple(item.split(":", 1) for item in (args.allow or ()) if ":" in item)
    problems = check_mod.opensource_problems(entries, allow=allow)
    document = {
        "scope": args.scope if not args.path else "custom",
        "patterns": list(patterns),
        "files": len(entries),
        "ok": not problems,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(f"范围 {document['scope']}：扫了 {len(entries)} 份内容，"
              f"{'没有本机事实' if not problems else str(len(problems)) + ' 处命中'}")
        for item in problems:
            print(f"  - {item}")
    return EXIT_PASS if not problems else EXIT_FAIL


# ================================================================== 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="统一硬件操作入口：接入契约、统一步骤、机器检查",
    )
    parser.add_argument("--bench", default="", help="台架文件（默认按平台规则找）")
    parser.add_argument(
        "--targets", default="",
        help=("额外的接入契约在哪儿（逗号分隔的模块名或 .py 路径）；"
              f"也可以用环境变量 {TARGETS_ENV}。不传就用平台默认清单"),
    )
    #: ⚠️ `--targets` 在子命令那一层也认（`list --targets …` 比 `--targets … list` 自然）。
    #: 子命令上的默认写成 `SUPPRESS`：没给时**不要**把顶层那个值盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--targets", default=argparse.SUPPRESS,
        help=("额外的接入契约在哪儿（逗号分隔的模块名或 .py 路径）；"
              f"也可以用环境变量 {TARGETS_ENV}"),
    )
    # ⚠️ 子命令**不是必填**：不给子命令就列一遍契约。这条不是 UX 偏好 —— 
    #    "坏台架文件 ⇒ 拒绝开始（退出码 2）"这条守卫是本平台对**每个入口**的要求，
    #    而 argparse 的"必填子命令"会抢在它前面报自己的错（退出码也是 2，但理由不对）。
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", parents=[common], help="列出这台机器上装配的接入契约")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser(
        "check", parents=[common],
        help="机器检查（完整性 / 旁路 / 判据 / 串用 / Core 泄漏）",
    )
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--skip-core", action="store_true",
                         help="跳过 core 泄漏那一项（它扫的是全平台模块）")
    p_check.add_argument("--module", default="", help="额外检查一个外部声明模块（脚手架生成物）")
    p_check.add_argument("--factory", default="declaration", help="外部模块里的工厂函数名")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser(
        "run", parents=[common],
        help="按声明的契约跑 preflight/build/flash/verify/debug-check",
    )
    p_run.add_argument("--adapter", default="", help="哪一条接入契约（`list` 里有）")
    p_run.add_argument("--firmware", default="", help="按固件/配置挑（没点名 adapter 时）")
    p_run.add_argument("--steps", default="", help="逗号分隔的步骤（默认全部）")
    p_run.add_argument("--port", default="", help="通信口（默认取这台机器上配置的那一个）")
    p_run.add_argument("--baud", type=int, default=0)
    p_run.add_argument("--seconds", type=float, default=3.0, help="debug-check / 身份读多久")
    p_run.add_argument("--timeout", type=float, default=0.0, help="覆盖命令超时（秒）")
    p_run.add_argument("--confirm-flash", action="store_true", help="确认允许写 Flash")
    p_run.add_argument("--allow-overwrite", action="store_true", help="确认覆盖板上现有应用")
    p_run.add_argument("--no-evidence", action="store_true", help="不落证据（仅本地试验）")
    p_run.add_argument("--json", action="store_true")
    p_run.add_argument("--fake", nargs="*", default=None,
                       help="拿平台自带的假工具跑（`<adapter>=fail|timeout|silent`）")
    p_run.set_defaults(func=cmd_run)

    p_create = sub.add_parser("create-adapter", help="生成 Adapter 骨架（声明 + 契约测试 + 清单）")
    p_create.add_argument("--out", required=True, help="生成到哪个目录")
    p_create.add_argument("--id", required=True, help="适配器 ID（稳定、小写、点分）")
    p_create.add_argument("--target", default="", help="目标 ID（默认与适配器同）")
    p_create.add_argument("--label", default="", help="人话名字")
    p_create.add_argument("--layer", default="target-demo", help="它属于哪一层（分层表里的名字）")
    p_create.add_argument("--module", default="", help="模块名（默认由 ID 推导）")
    p_create.add_argument("--project-entry", default="project/entry.ini")
    p_create.add_argument("--config", default="Debug")
    p_create.add_argument("--baud", type=int, default=115200)
    p_create.add_argument("--line-ending", default="lf")
    p_create.add_argument("--identity-query", default="identity")
    p_create.add_argument("--project", default="demo")
    p_create.add_argument("--no-verify", action="store_true",
                          help="生成后不立刻跑一遍生成物的契约测试")
    p_create.set_defaults(func=cmd_create)

    p_fuse = sub.add_parser("fuse", help="看 / 清熔断账本（人工处置之后的显式恢复）")
    p_fuse.add_argument("--clear", action="store_true")
    p_fuse.add_argument("--adapter", default="", help="只清这一条（默认全清）")
    p_fuse.set_defaults(func=cmd_fuse)

    p_open = sub.add_parser("opensource-audit", help="扫本机事实（绝对路径 / 串口名）")
    p_open.add_argument("--path", action="append", default=None, help="自定义范围（可多次）")
    p_open.add_argument("--scope", default="framework", choices=sorted(SCOPES),
                        help="扫描范围（默认 framework：本轮交付的框架面）")
    p_open.add_argument("--include-tests", action="store_true",
                        help="把测试夹具也算进来（它们本来就写着真实串口名）")
    p_open.add_argument("--allow", action="append", default=None,
                        help="显式豁免 `<文件>:<形状>`（可多次；必须点名到形状）")
    p_open.add_argument("--json", action="store_true")
    p_open.set_defaults(func=cmd_opensource)

    parser.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import unicode_console

    unicode_console.install()
    args = build_parser().parse_args(argv)
    try:
        bench_setup.install_from(cli_path=args.bench)
    except bench_config.BenchFileError as error:
        print(f"台架文件无效，拒绝开始：{error}", file=sys.stderr)
        return bench_config.EXIT_REFUSED
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

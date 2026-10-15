#!/usr/bin/env python3
""" / B2：台架配置层的**装配处**（core，带已声明的跨层边）。

`bench_config` 只认**形状**：允许出现哪些台架键，得有人告诉它。那件事只有装配处知道 ——
"这台电脑上登记了哪些目标、它们各自消费哪些键"。所以这里有且只有两件事：

1. `declarations()`：把各目标自己的 `bench_declaration()` 叫出来（声明写在目标层，
   这里只负责**列出来**；新增目标时改这一处）；
2. `install_from(...)`：入口 `main()` 里的一行 —— 找文件 → 校验 → 装成进程级台架。
   没配文件 ⇒ 装空台架（一切照旧走环境变量与代码默认值，**零行为变化**）；
   配了但无效 ⇒ `BenchFileError` 抛给入口（拒绝启动，退出码 2）。

⚠️ **只有入口才读本机文件**：`ENTRY_MODULES` 是那句话的名单，`test_bench_config.py` 的
源码级守卫拿它当白名单（既查"库代码有没有偷偷读文件"，也查"名单里有没有人其实没读"）。
名单放在这里而不是测试里，是因为测试在 core 层、**不许点名平台**（`第二目标` 这种名字
写进 core 测试会被 `test_core_layer` 挡下）；这里本来就允许出现目标模块名。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Mapping

CONSOLE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = CONSOLE_DIR.parent
#: 随发布的参考适配住在这儿（`<repo>/targets`）—— 本仓库把它们**真的**认领回来。
TARGETS_DIR = TOOLS_DIR.parent / "targets"
for _directory in (str(CONSOLE_DIR), str(TOOLS_DIR), str(TARGETS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

try:  # imported as a package (``webconsole.bench_setup``)
    from . import bench_config
except ImportError:  # executed as a plain script
    import bench_config  # type: ignore[no-redef]

#: 允许"自己去读台架文件"的模块（都是入口/装配处）。守卫按这份名单查两个方向。
#: `bench_config` 自己算一个（它定义那几个函数）；加进来的人都要说得出为什么。
ENTRY_MODULES: tuple[str, ...] = (
    "bench_config",
    "bench_setup",
    "server",
    "doctor",
    #  / F1：统一硬件入口也是**组合入口** —— 它在真正干活之前装一次
    # 台架（与本名单里其它入口同一套做法）。
    "hwops",
    "fetch_logs",
    "sync_time",
    "autotest_scenario",
    # ：这两个是**流水线入口**（要串口），而 B6 把代码里的默认端口删了之后
    # 它们没跟着装台架 ⇒ 拿到空台架 ⇒ 一跑就 `NotConfigured`（自 2026-10-01 起）。
    # 登记进来之后，下面那两条双向守卫（“只有入口能读文件”/“名单里必须真的在读”）
    # 与“拒绝开始的退出码”都会盖到它们。
    "hil",
    "crash_drill",
    #  / S4+S5：产品存储的真机受控验收入口（要串口 + 固件工程根，
    # 两样都是"这台电脑的事实" ⇒ 走台架，不写死路径）。
    "stg_accept",
    #  / S6：真机加速磨损/延迟/回收入口（同样要串口与固件工程根）。
    "stg_stress",
)

#: 声明工厂住在哪：`(模块名, 函数名)`。**新增目标只改这一行**。
#: ⚠️ 名字以**真实 import 语句**出现（见 `_target_modules()`）—— 写成字符串的话，
#: 边界清单就量不到这条边（"声明 == 实测"会当场红）。
#: ⚠️ 本仓库**真的**认领两个参考目标（它们各自的 `bench_declaration()` 就是台架文件
#: 里那些键的允许来源）：少了这一步，用户按 `bench.example.json` 填的本机值会被
#: 当成"没有目标声明这个键"整份拒掉。
_DECLARATION_FACTORIES: tuple[tuple[str, str], ...] = (
    ("stm32_target", "bench_declaration"),
    ("esp32_target", "bench_declaration"),
)


def _target_modules() -> tuple[Any, ...]:
    """已登记的目标模块（本仓库 = 两个参考适配；自己的硬件走 `--targets`）。

    要接自己的硬件，请用本机登记（`--targets <模块名或 .py 路径>` / `PW_HWOPS_TARGETS`）
    或按 `docs/adapter-guide.md` 随仓库发布。
    """
    import esp32_target  # noqa: PLC0415  (真实 import 语句：边界清单量得到这条边)
    import stm32_target  # noqa: PLC0415

    return (stm32_target, esp32_target)


def declarations() -> tuple[bench_config.BenchDeclaration, ...]:
    """所有已登记目标的台架声明（校验台架文件时就靠它）。"""
    rows: list[bench_config.BenchDeclaration] = []
    for module in _target_modules():
        factory = getattr(module, "bench_declaration", None)
        if callable(factory):
            rows.append(factory())
    return tuple(rows)


def hwops_target_modules() -> tuple[str, ...]:
    """默认情况下，统一入口要问哪几份目标模块拿接入契约（**清单就这一处**）。

    ⚠️ 需求 §7.7 说“接一条新硬件**不必改 core**”。所以这份清单住在**装配处**
    （平台侧），不在 `hwops.py` 里：在这里加一行，或干脆用本机配置
    `--targets` / `PW_HWOPS_TARGETS` 把“模块名或 `.py` 路径”写进去。

    ⚠️ 两件事刻意分开：**清单只收交得出 `hwops_wirings()` 的模块**（没交的那些
    与“接入契约”无关，不必每次都念一遍）；而你**显式**给的那一条交不出来时，
    入口会如实告诉你是哪一点 —— 那时你确实在问它。

    ⚠️ 返回值取**已 import 对象的 `__name__`**，不是写死的字符串：字符串会被边界
    扫描记成“core 认识这个平台”，而这份清单不该长那样。
    """
    return tuple(
        module.__name__ for module in _target_modules()
        if callable(getattr(module, "hwops_wirings", None))
    )


def entries() -> tuple[bench_config.BenchEntry, ...]:
    """各目标自述的台架值（人话名 + 值 + 来源）。

    ⚠️ 只在 `install_from()` 里叫一次（那时 `active()` 已经是新的那一份），
    之后它就是 `Bench.entries` 里的**数据** —— core 任何地方都能读它，
    不用 import 目标模块、也不用新增跨层边。
    """
    rows: list[bench_config.BenchEntry] = []
    for module in _target_modules():
        factory = getattr(module, "bench_entries", None)
        if callable(factory):
            rows.extend(factory())
    return tuple(rows)


def install_from(
    *,
    cli_path: str = "",
    root: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bench_config.Bench:
    """入口一行：装一份进程级台架（`--bench` 那条路径由调用方传进来）。

    装好之后**立刻**把各目标自述的值收进 `Bench.entries`：自述要的是"这个值是什么、
    从哪来"，而那只在装好之后才答得对。
    """
    bench = bench_config.install_from(
        cli_path=cli_path,
        root=root,
        overrides=overrides,
        declarations=declarations(),
        environ=environ,
    )
    bench.entries = entries()
    return bench


def file_note() -> str:
    """给 Doctor / 自述用的一句话：现在有没有台架文件、找的是哪一个。"""
    location = bench_config.find()
    if location.exists:
        return f"台架文件 {location.path}（来自 {location.source}）"
    return (
        f"没有台架文件：会用代码里的默认值（找过 {location.path}，"
        f"可用 --bench 或 {bench_config.FILE_ENV} 指定）"
    )

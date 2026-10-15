"""平台边界的**自述**：哪些模块属于哪一层、核心有没有混进平台知识。

这份文件是「边界清单」的唯一来源：`hwops_check.core_leakage_problems()` 用它做机器检查，
`docs/ARCHITECTURE.md` 用它写文档。**改边界只改这里**。

三层：

```text
core            平台核心：契约、编排、检查、脚手架、假工具、台架、体检（不认识任何芯片）
target-stm32    STM32 参考适配（裸机 / RT-Thread：IAR + C-SPY + ST-LINK）
target-esp32    ESP32 参考适配（PlatformIO + esptool + 串口）
```

依赖方向只能是 core ← target。core 里的模块**不许** import 目标层模块；唯一的例外是
**组合入口**（`bench_setup` / `doctor` / `hwops` 把目标的声明接进框架），它们必须列在
`COMPOSITION_MODULES` 里，否则 `scope_problems()` 会红。

三条检查（都靠「声明 vs 实测」，不是裸实测）：

- `module_layer_problems()`：每个模块文件都必须在 `MODULE_LAYERS` 里登记（新模块不登记就红）；
- `scope_problems()`：core 模块不许 import 目标层模块（组合入口除外）；
- `token_hit_problems()`：core 里不许出现**目标层专有的字面量**（工具名、芯片族名）。

⚠️ 这些是**能红的判据**，不是文档承诺：往 core 里加一句 `import stm32_target` 或写下
`IarBuild`，`hwops.py check` 当场报出来。
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

LAYER_CORE = "core"
LAYER_TARGET_STM32 = "target-stm32"
LAYER_TARGET_ESP32 = "target-esp32"
LAYERS: tuple[str, ...] = (LAYER_CORE, LAYER_TARGET_STM32, LAYER_TARGET_ESP32)
#: 目标层（「这块板/这套工具链的知识住在这儿」）。
TARGET_LAYERS: tuple[str, ...] = (LAYER_TARGET_STM32, LAYER_TARGET_ESP32)
#: 非 core 的层 —— 「core 不许直接 import 目标模块」这条判据要盖住**全部**新增层。
NON_CORE_LAYERS: tuple[str, ...] = tuple(layer for layer in LAYERS if layer != LAYER_CORE)

CONSOLE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = CONSOLE_DIR.parent
#: 目标适配器住在这儿（`<repo>/targets`）。
TARGETS_DIR = TOOLS_DIR.parent / "targets"

# ===================================================================== 模块分层
#: 每个模块属于哪一层。**新模块必须在这里登记**（`module_layer_problems()` 会发现没登记的）。
MODULE_LAYERS: dict[str, str] = {
    # --- 公开首笔补登：Web 全套（与芯片无关的登记为 core；
    #     少数握有目标层知识的登记在目标层，见 assemble_web_release.py）----
    "archive": LAYER_CORE,
    "assertion_groups": LAYER_CORE,
    "bundle_export": LAYER_CORE,
    "bundle_import": LAYER_CORE,
    "bundle_stream_import": LAYER_CORE,
    "console_lock": LAYER_TARGET_ESP32,
    "device_dialogue": LAYER_CORE,
    "device_registry": LAYER_CORE,
    "device_sniff": LAYER_CORE,
    "devices": LAYER_CORE,
    "dictionary": LAYER_CORE,
    "embedded_test_model": LAYER_CORE,
    "embedded_test_runner": LAYER_CORE,
    "fake_target": LAYER_CORE,
    "firmware_profile": LAYER_CORE,
    "host_scenario_runner": LAYER_CORE,
    "http_range": LAYER_CORE,
    "identity": LAYER_CORE,
    "io_timeline": LAYER_CORE,
    "large_bundle": LAYER_CORE,
    "large_evidence": LAYER_CORE,
    "large_index": LAYER_CORE,
    "large_search": LAYER_CORE,
    "link_assertions": LAYER_CORE,
    "live_decode": LAYER_CORE,
    "live_test_batch": LAYER_CORE,
    "live_test_catalog": LAYER_CORE,
    "live_test_host": LAYER_CORE,
    "live_test_link": LAYER_CORE,
    "metric_schema": LAYER_CORE,
    "observe": LAYER_CORE,
    "observe_center": LAYER_CORE,
    "observe_store": LAYER_CORE,
    "panic": LAYER_TARGET_ESP32,
    "pipeline": LAYER_TARGET_ESP32,
    "platform_info": LAYER_CORE,
    "platform_plugins": LAYER_CORE,
    "project_package": LAYER_CORE,
    "runner_registry": LAYER_CORE,
    "runstore": LAYER_CORE,
    "search_center": LAYER_CORE,
    "server": LAYER_CORE,
    "session_review": LAYER_CORE,
    "sniff_service": LAYER_CORE,
    "step_lifecycle": LAYER_CORE,
    "target_profile": LAYER_CORE,
    "web_assembly": LAYER_CORE,
    "web_catalog": LAYER_CORE,
    # --- core：框架自己的模块 ------------------------------------------------
    "__init__": LAYER_CORE,
    "hwops_contract": LAYER_CORE,
    "hwops_runner": LAYER_CORE,
    "hwops_check": LAYER_CORE,
    "hwops_fake": LAYER_CORE,
    "hwops_scaffold": LAYER_CORE,
    "bench_config": LAYER_CORE,
    "bench_setup": LAYER_CORE,
    # 公开运行装配的公共部分（把台架里的设备装成只读观察链）：与芯片无关 ⇒ core。
    "bench_target": LAYER_CORE,
    "target_adapters": LAYER_CORE,
    "core_primitives": LAYER_CORE,
    "console_input": LAYER_CORE,
    "platform_boundary": LAYER_CORE,
    "doctor_model": LAYER_CORE,
    "doctor_platform": LAYER_CORE,
    "doctor_target": LAYER_CORE,
    "hwops": LAYER_CORE,
    "unicode_console": LAYER_CORE,
    "doctor": LAYER_CORE,
    # --- target：随发布的参考适配（住在 <repo>/targets） --------------------
    "stm32_target": LAYER_TARGET_STM32,
    "esp32_target": LAYER_TARGET_ESP32,
}

#: **组合入口**：唯一允许 import 目标层模块的 core 模块（它们把目标的声明接进框架）。
#: 加进来的人都要说得出为什么 —— 这条是「core 里不许出现平台知识」的窄口子。
COMPOSITION_MODULES: tuple[str, ...] = ("bench_setup", "doctor", "hwops", "server", "runner_registry")

#: core 里**不许出现**的目标层专有字面量（工具名 / 芯片族名）。
#: 形状是 `(正则, 为什么)`；只在**代码**里查（注释与文档字符串不查 —— 说明里引到工具名是正当的）。
TARGET_TOKENS: tuple[tuple[str, str], ...] = (
    (r"\bIarBuild\b|\bCSpyBat\b|\bSTM32\b|\bstm32\b", "STM32 工具链 / 芯片族是目标层的知识"),
    (r"\besptool\b|\bPlatformIO\b|\bplatformio\b|\bESP32\b|\besp32\b", "ESP32 工具链 / 芯片族是目标层的知识"),
)
_TARGET_TOKEN_RE = tuple((re.compile(pattern), why) for pattern, why in TARGET_TOKENS)

#: 这份自述自己就写着那些字面量（它是**定义**那几条形状的地方），不扫它。
#: 组合入口也在豁免里：接目标就是它们的职责，名字必然出现 —— 但**只此一处**。
TOKEN_SCAN_EXEMPT: frozenset[str] = frozenset({"platform_boundary", *COMPOSITION_MODULES})


# ===================================================================== 文件与层
def module_files() -> dict[str, Path]:
    """模块名 → 文件（`webconsole/*.py` + `tools/*.py`）。"""
    found: dict[str, Path] = {}
    for directory in (CONSOLE_DIR, TOOLS_DIR):
        for path in sorted(directory.glob("*.py")):
            found.setdefault(path.stem, path)
    return found


def target_module_names() -> tuple[str, ...]:
    """目标层的模块名（从分层表推导，不另写一份清单）。"""
    return tuple(
        name for name, layer in sorted(MODULE_LAYERS.items()) if layer in TARGET_LAYERS
    )


def module_layer(module: str) -> str | None:
    return MODULE_LAYERS.get(str(module))


# ===================================================================== 只读扫描
def _imported_stems(path: Path) -> set[str]:
    """这份源码 import 的**顶层模块名**（解析不了就返回空集：不猜）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    stems: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                stems.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：包内的兄弟模块
                if node.module:
                    stems.add(node.module.split(".", 1)[0])
                for alias in node.names:
                    stems.add(alias.name.split(".", 1)[0])
            elif node.module:
                stems.add(node.module.split(".", 1)[0])
    return stems


def _code_without_prose(path: Path) -> str:
    """去掉**注释**与**文档字符串**之后的源码（只查代码，不查说明）。

    ⚠️ 说明文字里引到工具名是正当的（"别把端口写死"这句话本身就要提到端口）；
    这条检查盯的是**代码**里出现平台知识。
    """
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return ""
    lines = text.splitlines()
    drop: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept: list[str] = []
    for index, line in enumerate(lines, start=1):
        if index in drop:
            continue
        kept.append(re.sub(r"#.*$", "", line))
    return "\n".join(kept)


# ===================================================================== 三条检查
def module_layer_problems(present: dict[str, Path] | None = None) -> list[str]:
    """每个模块文件都必须在分层表里登记（没登记 ⇒ 等于没人知道它属于谁）。"""
    files = module_files() if present is None else dict(present)
    return [
        f"{name}.py 没有在 platform_boundary.MODULE_LAYERS 里登记"
        for name in sorted(files)
        if name not in MODULE_LAYERS
    ]


def scope_problems() -> list[str]:
    """core 模块不许 import 目标层模块（组合入口是唯一的口子）。"""
    problems = module_layer_problems()
    targets = set(target_module_names())
    for name, path in sorted(module_files().items()):
        if MODULE_LAYERS.get(name) != LAYER_CORE:
            continue
        if name in COMPOSITION_MODULES:
            continue
        hits = sorted(_imported_stems(path) & targets)
        if hits:
            problems.append(
                f"{name}.py 是 core，却 import 了目标层模块 {hits}："
                "平台知识必须留在目标层，core 只按形状调"
                "（确实要在装配处接目标，就把这个模块登记进 COMPOSITION_MODULES 并写清理由）"
            )
    return problems


def token_hit_problems() -> list[str]:
    """core 里不许出现目标层专有的**字面量**（工具名 / 芯片族名）。"""
    problems: list[str] = []
    for name, path in sorted(module_files().items()):
        if MODULE_LAYERS.get(name) != LAYER_CORE or name in TOKEN_SCAN_EXEMPT:
            continue
        text = _code_without_prose(path)
        if not text:
            continue
        for pattern, why in _TARGET_TOKEN_RE:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                problems.append(
                    f"{name}.py 的代码里出现了 {match.group(0)!r}：{why}"
                    f"（把这份知识移进 targets/；说明文字里引到它不算）"
                )
                break
    return problems


def orchestration_problems() -> list[str]:
    """编排层里不许出现平台知识。

    本框架的编排（`hwops_runner`）只按声明做事，所以这一项目前**恒为空**；
    保留它是为了与 `hwops_check.core_leakage_problems()` 的接口一致 ——
    将来出现第二份编排实现时，这里就是它该被检查的地方。
    """
    return []


def report() -> str:
    """人读自述（`python tools/webconsole/platform_boundary.py` 直接跑）。"""
    lines = [f"模块 {len(MODULE_LAYERS)} 个；层 {len(LAYERS)} 个"]
    for layer in LAYERS:
        names = sorted(name for name, value in MODULE_LAYERS.items() if value == layer)
        lines.append(f"  {layer:<16} {len(names):>2} 个：{', '.join(names) or '（无）'}")
    problems = scope_problems() + token_hit_problems()
    lines.append("边界检查：" + ("通过（无问题）" if not problems else f"{len(problems)} 处命中"))
    lines.extend(f"  - {item}" for item in problems)
    return "\n".join(lines)


def main() -> int:
    print(report())
    return 0 if not (scope_problems() + token_hit_problems()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

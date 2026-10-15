#!/usr/bin/env python3
""" / D3：**目标扩展接口**（**core**）—— 目标怎么向体检报告"我需要什么"。

平台体检（D1）只回答"这台电脑能不能跑平台"。目标那一层的问题不一样：

```text
某个已登记的目标，在这台机器上还缺什么？（构建/烧录/符号/解码工具、工程入口、配置来源）
这个目标接入平台**还缺哪些角色**？（profile / 适配器 / 项目包 / Web 装配登记 / 宿主契约测试）
```

答案必须是**目标自己声明的**，而且要**声明式**（数据，不是"用固件 id 分支后复制路径规则"，
需求 §4.2）。所以这里定义两样东西：

1. `Requirement` —— 一条**可选依赖**的声明：查什么（工具/路径/模块/环境变量）、期望是什么、
   缺了**怎么补**。`remediation` 是必填的：它最后会变成报告里那句"下一步做什么"。
2. `TargetDeclaration` —— 一个目标（可带若干固件）的全部声明 + 它已经装配了哪些**角色**。

三条规矩（每一条都有 `test_doctor.py` 的契约测试钉着）：

1. **目标层永远不产出 `FAIL`。** 缺工具只降级该目标（需求 §2 / §8.6）：一台新电脑没有
   IAR、没有 PlatformIO，平台本身仍然完整可用。所以这里**只给 `PASS` / `WARN` / `SKIP`**。
2. **"没声明" 与 "声明为缺" 分开。** 没登记任何目标 ⇒ `SKIP`（还没接入，不是通过）；
   登记了但请求的目标对不上 ⇒ `WARN`（要什么没什么，得说清楚）。
3. **只查"接口是否装配 + 依赖是否可发现"**（需求 §4.3）。不猜芯片、不猜接线、不试波特率、
   不解析 Flash 布局、不连任何设备 —— 探针全是只读的查找（`which` / 文件是否存在 / 能否 import）。

⚠️ core 只认这些**形状**：目标是谁、查什么、缺了怎么办，全由目标层（D4）填。
这个模块不 import 任何目标模块，也不认识任何一块板。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
from typing import Callable, Iterable, Sequence

try:  # 包内 import
    from .doctor_model import Check, Scope, Status
    from .doctor_platform import can_import, path_state
except ImportError:  # 直接跑脚本/测试时的回退
    from doctor_model import Check, Scope, Status  # type: ignore[no-redef]
    from doctor_platform import can_import, path_state  # type: ignore[no-redef]


class RequirementKind:
    """一条声明要查什么。**封闭集合**：写错名字当场报错，而不是悄悄跳过。"""

    TOOL = "tool"        # 可执行工具（按名字在 PATH / 目标自己的工具目录里找）
    PATH = "path"        # 文件/目录（工程入口、产物、配置来源、字典库…）
    MODULE = "module"    # Python 模块（能不能 import）
    ENV = "env"          # 环境变量（工具装在哪、工程在哪，由它指）
    PROBE = "probe"      # 上面四类都表达不了的事实（例：USB 探针在不在），由目标给一个只读回调

    ALL = (TOOL, PATH, MODULE, ENV, PROBE)


class Expectation:
    """这一项“缺了”意味着什么（决定报 `WARN` 还是 `SKIP`）。

    - `PRESENT`：它本来就该在（工具 / 工程入口 / 配置来源）⇒ 缺了是 `WARN`，要人去补；
    - `PRODUCED`：它是**构建产物**（ELF / 镜像 / 字典）⇒ 没构建过时缺了是 `SKIP`，
      不是“依赖缺失”—— 否则新机器上的报告会被一堆“还没构建”的 WARN 淹没（）。
    """

    PRESENT = "present"
    PRODUCED = "produced"

    ALL = (PRESENT, PRODUCED)


@dataclass(frozen=True)
class ProbeOutcome:
    """目标自己的只读探测给出的结论（例：“栈上看到 ST-LINK 吗”）。"""

    ok: bool
    actual: str = ""
    reason: str = ""


#: 一个"完整接入"的目标应有的角色（需求 §4.3 的那张清单）。
#: 缺哪个角色 **不影响平台结果**，只影响这个目标能不能跑。
ROLE_NAMES: tuple[str, ...] = (
    "profile",        # Target / Firmware profile
    "build",          # 构建适配器
    "flash",          # 烧录适配器
    "reset",          # 复位（数据化的时序计划）
    "transport",      # 串口/探针的只读承载
    "identity",       # 身份核验（设备自报：拼一次还是问一次）
    "decode",         # 解码 / 精确选库
    "symbolize",      # 符号化（崩溃地址 → 源码位置）
    "project",        # Project Package（业务语义）
    "web_assembly",   # Web 装配登记（页面能选中它）
    "host_suite",     # 对应的宿主契约测试
)

#: 每个角色缺了该做什么（WARN 的 remediation 由它拼出来 —— 报告要能直接指导下一步）。
ROLE_ADVICE: dict[str, str] = {
    "profile": "登记 TargetProfile / FirmwareProfile（它是谁、连哪儿、允许做什么）",
    "build": "提供构建适配器（受控命令 + 成功判据）",
    "flash": "提供烧录适配器（写入口、镜像核对、逐次授权）",
    "reset": "提供复位计划（数据化的时序；串口调制线或受控命令）",
    "transport": "提供串口/探针的只读承载（打开时不碰调制线）",
    "identity": "声明身份怎么认（设备自报的那一行 / 问一次的命令），并用一次实测钉住它",
    "decode": "提供解码器与精确选库（按构建号，不猜字典）",
    "symbolize": "提供符号化适配器（工具在哪儿 + 受控命令行）",
    "project": "写一份 Project Package（事件/指标/断言/套件由它带来）",
    "web_assembly": "在装配处登记这条链（页面才选得中它）",
    "host_suite": "补一套宿主契约测试，并在 `platform_boundary.TEST_SUITES` 登记",
}

_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def _segment(text: str) -> str:
    """把任意 id 变成检查项 id 里合法的一段（`demo-a` → `esp32_示例项目_a`）。"""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).lower())
    return cleaned.strip("_") or "unknown"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Requirement:
    """一条**声明式**的可选依赖检查。谁声明，谁负责说清"缺了怎么办"。"""

    key: str                     # 目标内唯一：`build.tool` / `project.entry` / `decode.library`
    title: str                   # 人话标题
    kind: str                    # 见 `RequirementKind`
    expected: str                # 期望（人话）
    remediation: str             # 缺了怎么补（人话；会原样出现在报告的"下一步"里）
    #: `tool` = 工具名（PATH 查找）；`path` = 绝对路径或相对工程根的路径；
    #: `module` = 模块名；`env` = 环境变量名；`probe` = 由 `probe` 回调给出结论。
    value: str = ""
    #: 缺了这一项意味着什么（见 `Expectation`）；`produced` 的那些缺了只 `SKIP`。
    expect: str = Expectation.PRESENT
    #: `kind="probe"` 时**必须**给：一次只读探测（不碰设备、不改环境）。
    probe: Callable[[], ProbeOutcome] | None = None
    #: 这个值是**从哪儿来的**（ §6.4）：一句人话，直接拼进 `actual`。
    #: 例：`来自台架文件 bench.local.json 的 tools.iar_dir`。空 = 不提来源。
    source: str = ""

    def __post_init__(self) -> None:
        _require(bool(_KEY_PATTERN.match(self.key)), f"依赖声明的 key 形状不对：{self.key!r}")
        _require(
            self.kind in RequirementKind.ALL,
            f"{self.key}：不认识的 kind {self.kind!r}（可用：{list(RequirementKind.ALL)}）",
        )
        _require(
            self.expect in Expectation.ALL,
            f"{self.key}：不认识的 expect {self.expect!r}（可用：{list(Expectation.ALL)}）",
        )
        if self.kind == RequirementKind.PROBE:
            _require(self.probe is not None, f"{self.key}：probe 类声明必须给 probe 回调")
        else:
            _require(
                bool(self.value.strip()),
                f"{self.key}：{self.kind} 类声明必须给 value",
            )
        _require(bool(self.title.strip()), f"{self.key}：要有 title（人读报告用）")
        _require(
            bool(self.remediation.strip()),
            f"{self.key}：必须写 remediation —— 缺工具只会 WARN，报告就靠这句话指出下一步",
        )


@dataclass(frozen=True)
class FirmwareDeclaration:
    """同一块板上的**一份固件**（它自己的工程入口、产物、工具与配置来源）。"""

    firmware_id: str
    label: str
    requirements: tuple[Requirement, ...] = ()
    #: 这份固件是否已经实现（`planned` 的固件只登记位置，不能被选中去跑）。
    implemented: bool = True
    project_root: Path | None = None
    #: 这个工程根是从哪儿来的（ §6.4）：本行**所有**路径都按它解析，
    #: 所以它自己没写来源时，本行的每一项都带上它。
    source: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.firmware_id.strip()), "firmware_id 不能为空")
        _require(bool(self.label.strip()), f"{self.firmware_id}：要有 label")
        keys = [item.key for item in self.requirements]
        duplicates = {key for key in keys if keys.count(key) > 1}
        _require(not duplicates, f"{self.firmware_id}：依赖声明 key 重复 {sorted(duplicates)}")


@dataclass(frozen=True)
class TargetDeclaration:
    """一个目标向体检报告交代的全部内容（**数据**，由目标层投影出来）。"""

    target_id: str
    label: str
    #: 目标级依赖：**共享的东西只声明一次**（同一块板的串口/探针条件不写两遍）。
    requirements: tuple[Requirement, ...] = ()
    firmwares: tuple[FirmwareDeclaration, ...] = ()
    #: 这个目标**已经装配**了哪些角色（见 `ROLE_NAMES`）。
    roles: tuple[str, ...] = ()
    #: 这个目标"完整接入"应当有哪些角色（默认就是那张全表；只读目标可以少要几项）。
    expected_roles: tuple[str, ...] = ROLE_NAMES
    #: 本机工程根（`path` 类声明里的相对路径按它解析）。没工程就 `None`。
    project_root: Path | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.target_id.strip()), "target_id 不能为空")
        _require(bool(self.label.strip()), f"{self.target_id}：要有 label")
        keys = [item.key for item in self.requirements]
        duplicates = {key for key in keys if keys.count(key) > 1}
        _require(not duplicates, f"{self.target_id}：目标级依赖 key 重复 {sorted(duplicates)}")
        unknown = [role for role in self.roles if role not in ROLE_NAMES]
        _require(not unknown, f"{self.target_id}：不认识的角色 {unknown}（可用：{list(ROLE_NAMES)}）")
        unknown = [role for role in self.expected_roles if role not in ROLE_NAMES]
        _require(
            not unknown,
            f"{self.target_id}：expected_roles 里有不认识的角色 {unknown}",
        )
        firmware_ids = [item.firmware_id for item in self.firmwares]
        duplicates = {name for name in firmware_ids if firmware_ids.count(name) > 1}
        _require(not duplicates, f"{self.target_id}：固件重复登记 {sorted(duplicates)}")

    def firmware(self, firmware_id: str) -> FirmwareDeclaration | None:
        for row in self.firmwares:
            if row.firmware_id == str(firmware_id):
                return row
        return None


@dataclass(frozen=True)
class Resolvers:
    """把"声明"变成"结论"的**只读**查找（全部可注入，测试整份替换）。"""

    tool: Callable[[str], str] = lambda name: shutil.which(name) or ""
    path_state: Callable[[Path], str] = path_state
    module_available: Callable[[str], bool] = can_import
    env_value: Callable[[str], str] = lambda name: str(os.environ.get(name, "") or "")


# ===================================================================== 聚合
def target_checks(
    declarations: Sequence[TargetDeclaration] = (),
    *,
    requested_target: str = "",
    requested_firmware: str = "",
    resolvers: Resolvers | None = None,
) -> list[Check]:
    """把目标的声明聚合成本次体检的目标层检查项。

    三层降级（需求 §7 的 D3 完成定义：无目标 / 单目标 / 多目标 / 缺工具都要诚实）：

    - **没有任何声明** ⇒ 一条 `SKIP`（"还没接入"，不是通过）；
    - **请求的目标/固件对不上** ⇒ 一条 `WARN`，并列出已登记的那几个；
    - **某个目标缺工具/缺角色** ⇒ 只影响它自己（`WARN`），平台结果不受影响。
    """
    active = resolvers if resolvers is not None else Resolvers()
    declarations = tuple(declarations)
    if not declarations:
        return [_not_registered_check(requested_target, requested_firmware)]

    checks: list[Check] = []
    selected, unmatched = _select(declarations, requested_target, requested_firmware)
    if unmatched:
        checks.append(_unmatched_check(unmatched, declarations, requested_target, requested_firmware))
    seen: set[str] = set()
    for declaration in selected:
        for row in _declaration_checks(
            declaration,
            requested_firmware=requested_firmware,
            resolvers=active,
        ):
            if row.id in seen:
                raise ValueError(
                    f"两个目标/固件生成了同一个检查项 id：{row.id!r}"
                    "（目标 id 去重后撞车了，报告会说不清是谁的问题）"
                )
            seen.add(row.id)
            checks.append(row)
    if not checks:
        checks.append(_unmatched_check("", declarations, requested_target, requested_firmware))
    return checks


def _select(
    declarations: tuple[TargetDeclaration, ...],
    requested_target: str,
    requested_firmware: str,
) -> tuple[list[TargetDeclaration], str]:
    """按请求挑声明；挑不到就把"没匹配上"说清楚。"""
    selected = list(declarations)
    if requested_target:
        selected = [row for row in selected if row.target_id == requested_target]
    if requested_firmware:
        selected = [
            row for row in selected
            if any(item.firmware_id == requested_firmware for item in row.firmwares)
        ]
    unmatched = ""
    if not selected:
        want = " / ".join(part for part in (requested_target, requested_firmware) if part)
        unmatched = want or "（空请求）"
    return selected, unmatched


def _known_ids(declarations: Iterable[TargetDeclaration]) -> str:
    parts = []
    for row in declarations:
        firmwares = ", ".join(item.firmware_id for item in row.firmwares) or "（无固件登记）"
        parts.append(f"{row.target_id} [{firmwares}]")
    return "；".join(parts) or "（无）"


def _declaration_checks(
    declaration: TargetDeclaration,
    *,
    requested_firmware: str,
    resolvers: Resolvers,
) -> list[Check]:
    prefix = f"target.{_segment(declaration.target_id)}"
    rows: list[Check] = [_roles_check(declaration, prefix=prefix)]
    for requirement in declaration.requirements:
        rows.append(
            _requirement_check(
                requirement,
                check_id=f"{prefix}.{_segment(requirement.key)}",
                project_root=declaration.project_root,
                resolvers=resolvers,
                owner=declaration.label,
            )
        )
    firmwares = (
        [row for row in declaration.firmwares if row.firmware_id == requested_firmware]
        if requested_firmware
        else list(declaration.firmwares)
    )
    for firmware in firmwares:
        rows.extend(_firmware_checks(declaration, firmware, prefix=prefix, resolvers=resolvers))
    return rows


def _firmware_checks(
    declaration: TargetDeclaration,
    firmware: FirmwareDeclaration,
    *,
    prefix: str,
    resolvers: Resolvers,
) -> list[Check]:
    where = f"{prefix}.{_segment(firmware.firmware_id)}"
    rows: list[Check] = []
    if not firmware.implemented:
        rows.append(
            Check(
                id=f"{where}.implemented",
                scope=Scope.TARGET,
                status=Status.SKIP,
                title=f"{firmware.label}：实现状态",
                group=firmware.firmware_id,
                group_label=firmware.label,
                actual="只登记了位置",
                expected="可以构建/烧录/采集",
                reason="这份固件还没实现（planned）：可以被查、被展示，但不该被选中去跑",
            )
        )
    root = firmware.project_root if firmware.project_root is not None else declaration.project_root
    for requirement in firmware.requirements:
        rows.append(
            _requirement_check(
                requirement,
                check_id=f"{where}.{_segment(requirement.key)}",
                project_root=root,
                resolvers=resolvers,
                owner=firmware.label,
                fallback_source=firmware.source,
            )
        )
    return rows


def _roles_check(declaration: TargetDeclaration, *, prefix: str) -> Check:
    present = set(declaration.roles)
    missing = [role for role in declaration.expected_roles if role not in present]
    expected = f"{len(declaration.expected_roles)} 个角色齐备"
    if not missing:
        return Check(
            id=f"{prefix}.roles",
            scope=Scope.TARGET,
            status=Status.PASS,
            title=f"{declaration.label}：适配角色齐备",
            group="roles",
            group_label=declaration.label,
            actual=f"已装配 {', '.join(declaration.expected_roles)}",
            expected=expected,
        )
    advice = "；".join(ROLE_ADVICE.get(role, role) for role in missing[:3])
    if len(missing) > 3:
        advice += "；…"
    return Check(
        id=f"{prefix}.roles",
        scope=Scope.TARGET,
        status=Status.WARN,
        title=f"{declaration.label}：适配角色缺口",
        group="roles",
        group_label=declaration.label,
        actual=f"缺 {len(missing)}/{len(declaration.expected_roles)}：{', '.join(missing)}",
        expected=expected,
        reason=f"这个目标接入平台还缺这些角色：{', '.join(missing)}",
        remediation=f"按顺序补齐缺的角色 —— {advice}",
    )


def _requirement_check(
    requirement: Requirement,
    *,
    check_id: str,
    project_root: Path | None,
    resolvers: Resolvers,
    owner: str,
    fallback_source: str = "",
) -> Check:
    found, actual, reason = _resolve(requirement, project_root=project_root, resolvers=resolvers)
    note = requirement.source or fallback_source
    if note:
        # 来源是**事实**，不是解释：PASS/WARN/SKIP 都该看得见它（需求 §6.4）。
        actual = f"{actual}；{note}"
    base = {
        "id": check_id,
        "scope": Scope.TARGET,
        "title": f"{owner}：{requirement.title}",
        "group": requirement.kind,
        # 目标层的**分类就是"这条属于哪个目标/哪份固件"**（页面二级分组用它）：
        # `group` 是机器键（依赖种类），人话分类是 `owner` 的 label。
        "group_label": owner,
        "actual": actual,
        "expected": requirement.expected,
    }
    if found:
        return Check(status=Status.PASS, **base)
    if requirement.expect == Expectation.PRODUCED:
        # 构建产物（ELF/镜像/字典）：没构建过才缺 ⇒ `SKIP`，不是"依赖缺失"。
        return Check(
            status=Status.SKIP,
            reason=f"{reason}；这是构建产物，先按上面那条工具链构建一次",
            **base,
        )
    return Check(
        status=Status.WARN,
        reason=reason,
        remediation=requirement.remediation,
        **base,
    )


def _resolve(
    requirement: Requirement,
    *,
    project_root: Path | None,
    resolvers: Resolvers,
) -> tuple[bool, str, str]:
    """查一条声明：`(找到没, 实测值, 没找到的原因)`。**只读**，不猜、不试、不连设备。"""
    value = requirement.value.strip()
    if requirement.kind == RequirementKind.TOOL:
        found = str(_safe(lambda: resolvers.tool(value), "") or "")
        if found:
            return True, found, ""
        return False, f"没找到 {value}", f"PATH（或目标自己的工具目录）里没有 {value}"
    if requirement.kind == RequirementKind.MODULE:
        ok = bool(_safe(lambda: resolvers.module_available(value), False))
        if ok:
            return True, f"{value} 可导入", ""
        return False, f"{value} 导不进来", f"当前解释器里没有 {value} 这个模块"
    if requirement.kind == RequirementKind.ENV:
        configured = str(_safe(lambda: resolvers.env_value(value), "") or "")
        if configured:
            return True, f"{value}={configured}", ""
        return False, f"{value}（未设置）", f"环境变量 {value} 没有设置"
    if requirement.kind == RequirementKind.PROBE:
        outcome = _safe(lambda: requirement.probe(), None)  # type: ignore[misc]
        if isinstance(outcome, ProbeOutcome) and outcome.ok:
            return True, outcome.actual, ""
        if isinstance(outcome, ProbeOutcome):
            return False, outcome.actual, outcome.reason or "目标自己的只读探测没有通过"
        return False, "探测没给出结论", "目标自己的只读探测抛了异常或返回了空值（不猜结论）"
    # PATH
    target_path = Path(value)
    if not target_path.is_absolute():
        if project_root is None:
            return False, value, (
                f"{value} 是相对路径，但这个目标没有声明工程根 ⇒ 无法解析（不猜路径）"
            )
        target_path = Path(project_root) / value
    state = str(_safe(lambda: resolvers.path_state(target_path), "missing"))
    shown = f"{target_path}（{state}）"
    if state == "ok":
        return True, shown, ""
    if state == "missing":
        return False, shown, f"声明的东西不存在：{target_path}"
    return False, shown, f"声明的东西读不了（{state}）：{target_path}"


def _safe(action: Callable[[], object], default: object) -> object:
    """探针自己炸了 ⇒ 如实退到默认值（不猜、也不把体检整体带崩）。"""
    try:
        return action()
    except Exception:
        return default


# ------------------------------------------------------------------ 三条降级
def _not_registered_check(requested_target: str, requested_firmware: str) -> Check:
    if requested_target or requested_firmware:
        reason = (
            f"本次请求的目标 {requested_target or '-'} / 固件 {requested_firmware or '-'} "
            "还没有对应的声明（D4 接入现有目标）"
        )
    else:
        reason = "平台还没有登记任何目标声明（D4 接入现有目标）"
    return Check(
        id="target.readiness",
        scope=Scope.TARGET,
        status=Status.SKIP,
        title="目标适配就绪度检查",
        group="target",
        group_label="目标适配就绪度",
        actual="0 个目标声明",
        expected="每个已登记目标一条只读检查",
        reason=reason,
    )


def _unmatched_check(
    unmatched: str,
    declarations: tuple[TargetDeclaration, ...],
    requested_target: str,
    requested_firmware: str,
) -> Check:
    want = " / ".join(part for part in (requested_target, requested_firmware) if part)
    return Check(
        id="target.request.unmatched",
        scope=Scope.TARGET,
        status=Status.WARN,
        title="请求的目标/固件没有登记",
        group="target",
        group_label="请求核对",
        actual=f"请求 {want or unmatched}",
        expected="请求的 id 在已登记的目标/固件里",
        reason=f"没有与 {want or unmatched!r} 对得上的目标声明",
        remediation=f"检查 id 拼写；已登记的是：{_known_ids(declarations)}",
    )


__all__ = [
    "Expectation",
    "FirmwareDeclaration",
    "ProbeOutcome",
    "ROLE_ADVICE",
    "ROLE_NAMES",
    "Requirement",
    "RequirementKind",
    "Resolvers",
    "TargetDeclaration",
    "target_checks",
]

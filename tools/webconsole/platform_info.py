"""平台自述：**这一段话必须能独立说清"这是哪套组合、能做什么、按哪个 schema 写的"**
（**core**， / R2d，DEC-010）。

一份证据（会话 manifest / 诊断包 / `/api/state`）能不能被别人拿去用，取决于它有没有
把下面四件事写在自己身上：

| 问题 | 字段 |
| --- | --- |
| 这是**谁**（哪套 target + 项目组合）？ | `label` / `platform` / `project` / `targets[]` |
| 它**能做什么**？ | `capabilities`（九项）与 `session_capabilities`（四项） |
| 它用**哪些受信适配器**？ | `adapters[]`（只列 ID，参数数组不进配置） |
| 这些文件按**哪个 schema** 写的？ | `schemas`（每个载荷一个版本号） |

⚠️ 这里**不 import 目标/项目模块**：内容全部由调用方（装配处 / 会话）交进来。
core 只负责把形状定死、把版本号的**唯一出处**放在这里（别处不许各写一份）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: 本平台自述自己的 schema 版本。
SCHEMA = 1

#: 各个**载荷**的 schema 版本：一处声明，谁都不许再写第二份。
#: （`metrics_export` 的版本来自指标模型；其余是这个仓库里落盘的三种 JSON。）
SCHEMA_VERSIONS: Mapping[str, int] = {
    "platform_info": SCHEMA,
    "session_manifest": 1,
    "run_manifest": 1,
    "bundle_manifest": 1,
    "observation_snapshot": 1,
    "assertion_report": 1,
}


def versions(**overrides: int) -> dict[str, int]:
    """这一次自述里的版本表（允许调用方覆盖某项，但默认只有一份出处）。"""
    table = dict(SCHEMA_VERSIONS)
    table.update({name: int(value) for name, value in overrides.items()})
    return table


@dataclass(frozen=True)
class TargetFacts:
    """一个目标在自述里的那一行（只放"读的人需要知道"的字段）。

    ``target_id`` 由调用方给，两个视角的来源不同、不强求统一：

    - **装配处**（`/api/state`）手上有 profile ⇒ 给的是 profile 的 id（`demo-a`）；
    - **会话**（manifest / 诊断包）只知道设备登记键 ⇒ 给的是那个键（`a`）。

    两者都是"这台设备在这套组合里的名字"：profile id 属于装配，设备键属于会话。
    强行让会话去猜 profile id，就会把目标层的知识又抄回 core。
    """

    target_id: str
    device_key: str
    platform: str
    project: str
    pipeline_allowed: bool
    capability_table: Any = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target_id": self.target_id,
            "device_key": self.device_key,
            "platform": self.platform,
            "project": self.project,
            "pipeline_allowed": self.pipeline_allowed,
        }
        table = self.capability_table
        if table is not None and hasattr(table, "to_json"):
            out["capabilities"] = table.to_json()
        return out


def targets_from_profiles(profiles: Iterable[Any]) -> tuple[TargetFacts, ...]:
    """把 `TargetProfile` 投影成自述用的那几行（读不到的字段不编）。"""
    out: list[TargetFacts] = []
    for profile in profiles:
        out.append(
            TargetFacts(
                target_id=str(getattr(profile, "target_id", "") or ""),
                device_key=str(getattr(profile, "device_key", "") or ""),
                platform=str(getattr(profile, "platform", "") or ""),
                project=str(getattr(profile, "project", "") or ""),
                pipeline_allowed=bool(getattr(profile, "pipeline_allowed", False)),
                capability_table=getattr(profile, "capabilities", None),
            )
        )
    return tuple(out)


def describe(
    *,
    plugins: Any = None,
    targets: Iterable[TargetFacts] = (),
    session_capabilities: Mapping[str, bool] | None = None,
    schema_overrides: Mapping[str, int] | None = None,
    bench: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """拼出那个自述块（给 `/api/state`、会话 manifest 与诊断包共用同一份）。

    `plugins` 是装配处的插件束（`PlatformPlugins`）：它提供 label/platform/project/
    adapters/token_domain/decoder 可用性 —— core 只把它**如实**抄下来。

    `bench` 是台架块（ / B3）：**由 `bench_config.Bench.as_dict()` 产出**，
    这里只负责摆上去（core 不认识台架键叫什么）。不传就是诚实的空块。
    """
    target_rows = [row for row in targets]
    label = str(getattr(plugins, "label", "") or "")
    platform = str(getattr(plugins, "platform", "") or "")
    project = str(getattr(plugins, "project", "") or "")
    if not platform and target_rows:
        platform = target_rows[0].platform
        project = project or target_rows[0].project
        label = label or f"{platform}+{project}"
    capabilities: dict[str, Any] | None = None
    if target_rows and target_rows[0].capability_table is not None:
        table = target_rows[0].capability_table
        if hasattr(table, "to_json"):
            capabilities = table.to_json()
    adapters = list(getattr(plugins, "adapter_ids", ()) or ())
    described: dict[str, Any] = {
        "schema": SCHEMA,
        "label": label,
        "platform": platform,
        "project": project,
        "token_domain": str(getattr(plugins, "token_domain", "") or ""),
        "targets": [row.to_json() for row in target_rows],
        "capabilities": capabilities,
        "session_capabilities": dict(session_capabilities or {}),
        "adapters": adapters,
        "decoder_available": (
            bool(plugins.decoder_is_available()) if plugins is not None else False
        ),
        "schemas": versions(**(dict(schema_overrides or {}))),
        "bench": dict(bench) if bench else {},
    }
    return described


# ================================================================== 多套装配
@dataclass(frozen=True)
class AssemblyFacts:
    """**一套装配**在自述里的那一行（W4c / 2026-09-28）。

    一台服务登记的**不止一套**组合：ESP32 示例项目 一套、STM32 裸机一套、STM32
    RT-Thread 一套。它们各有自己的**目标、项目包、套件、适配器、能力表与字典 domain**，
    所以"这台服务是什么"没有**唯一**答案 —— 每个 firmware 一条链，一条链一行。

    ⚠️ 这一行只说**已有的**事实（都是装配处交进来的）；core 不去推导、不去补默认值。
    """

    #: 这份装配的选择 id（页面 POST 的那个 firmware_id），例如 `stm32f103-iar/rtthread`。
    firmware_id: str
    #: 页面上这一路叫什么（人话标签）。
    label: str
    platform: str
    project: str
    #: 选择空间里的 target id（页面 POST 的 `target_id`；不是 profile 自己的 id）。
    target_id: str
    device_key: str
    pipeline_allowed: bool = False
    requires_flash_confirmation: bool = False
    token_domain: str = ""
    suites: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    decoder_available: bool = False
    #: **这套组合自己声明的名字**（插件束的 `label`，例如 `esp32+示例项目`）。
    #: 与上面的 `label` 分开：前者是自述里的"我是谁"，后者是选择列表里的一行。
    #: 空字符串表示那一层没给自己的组合起名字（那就退到 `label`）。
    platform_label: str = ""
    capability_table: Any = None
    session_capabilities: Mapping[str, bool] | None = None
    #: 台架块（ / B3）：本链用到的台架值 + 来源，由装配处交进来。
    bench: Mapping[str, Any] | None = None

    def target_facts(self) -> TargetFacts:
        """这一套装配对应的一条 `targets[]` 行（只有一套装配时就是它）。"""
        return TargetFacts(
            target_id=self.target_id,
            device_key=self.device_key,
            platform=self.platform,
            project=self.project,
            pipeline_allowed=bool(self.pipeline_allowed),
            capability_table=self.capability_table,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "firmware_id": self.firmware_id,
            "label": self.label,
            "platform_label": self.platform_label or self.label,
            "platform": self.platform,
            "project": self.project,
            "target_id": self.target_id,
            "device_key": self.device_key,
            "pipeline_allowed": bool(self.pipeline_allowed),
            "requires_flash_confirmation": bool(self.requires_flash_confirmation),
            "token_domain": self.token_domain,
            "suites": list(self.suites),
            "adapters": list(self.adapters),
            "decoder_available": bool(self.decoder_available),
            "capabilities": (
                self.capability_table.to_json()
                if self.capability_table is not None
                and hasattr(self.capability_table, "to_json")
                else None
            ),
            "session_capabilities": dict(self.session_capabilities or {}),
            "bench": dict(self.bench or {}),
        }
        return out


@dataclass(frozen=True)
class _PluginsView:
    """`describe()` 眼里的"插件束"的最小形状（多套装配时用来聚合）。"""

    label: str = ""
    platform: str = ""
    project: str = ""
    token_domain: str = ""
    adapter_ids: tuple[str, ...] = ()
    decoder_available: bool = False

    def decoder_is_available(self) -> bool:
        return bool(self.decoder_available)


def _distinct(rows: Iterable[Any], field: str, separator: str = " + ") -> str:
    """把多行的同一个字段去重后拼起来（顺序按登记顺序，不是排序）。"""
    seen: list[str] = []
    for row in rows:
        value = str(getattr(row, field, "") or "")
        if value and value not in seen:
            seen.append(value)
    return separator.join(seen)


def _union(rows: Iterable[Any], field: str) -> tuple[str, ...]:
    seen: list[str] = []
    for row in rows:
        for value in getattr(row, field, ()) or ():
            text = str(value or "")
            if text and text not in seen:
                seen.append(text)
    return tuple(seen)


def describe_assemblies(
    *,
    assemblies: Iterable[AssemblyFacts],
    targets: Iterable[TargetFacts] | None = None,
    schema_overrides: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """多套装配的自述（`/api/state.platform`）：**一条链一行**，顶层只说"合起来是什么"。

    ⚠️ **只有一套装配时，输出与 `describe()` 逐字段相同**（外加一行 `assemblies[]`）：
    会话 manifest / 诊断包 / 历史 run 读的还是同一份形状，不会因为服务多登记了一个
    固件而换一种读法。

    多套装配时顶层 `capabilities` 是 `null`（**没有"这一个"能力表**），
    `session_capabilities` 给空表 —— 真正的能力在 `assemblies[].capabilities` /
    `assemblies[].session_capabilities` 里。给一个合成出来的"总能力"就是在
    "借用另一份固件的能力"，那正是本阶段要禁掉的。
    """
    rows = [row for row in assemblies]
    if targets is None:
        targets = [row.target_facts() for row in rows]
    target_rows = [row for row in targets]
    if not rows:
        return describe(
            targets=target_rows, schema_overrides=dict(schema_overrides or {})
        )
    single = len(rows) == 1
    view = _PluginsView(
        label=_distinct(rows, "platform_label") or _distinct(rows, "label"),
        platform=_distinct(rows, "platform"),
        project=_distinct(rows, "project"),
        token_domain=_distinct(rows, "token_domain"),
        adapter_ids=_union(rows, "adapters"),
        decoder_available=any(bool(row.decoder_available) for row in rows),
    )
    described = describe(
        plugins=view,
        targets=target_rows,
        session_capabilities=(
            dict(rows[0].session_capabilities or {}) if single else {}
        ),
        schema_overrides=dict(schema_overrides or {}),
    )
    if not single:
        described["capabilities"] = None
    described["assemblies"] = [row.to_json() for row in rows]
    return described


def problems(described: Mapping[str, Any]) -> list[str]:
    """这个自述块自己站不站得住（空列表 = 站得住）。

    ⚠️ 一个**永远为空**的字段看起来像证据。所以"缺什么"必须在测试里说出来，
    而不是让读的人自己去发现 `capabilities: null` 是什么意思。

    多套装配（`assemblies[]` 存在）时逐行核对：每一行都要说清它是谁、用哪个项目包、
    能不能跑、有哪些受信适配器；**能跑的装配还必须说清它能跑什么套件**。
    """
    issues: list[str] = []
    if not described.get("label"):
        issues.append("没说清这是哪套组合（label 为空）")
    if not described.get("targets"):
        issues.append("一个目标都没登记（targets 为空）")
    if not described.get("adapters"):
        issues.append("没有列出任何受信适配器（adapters 为空）")
    if not described.get("schemas"):
        issues.append("没有版本表（schemas 为空）")
    for name, value in (described.get("schemas") or {}).items():
        if not isinstance(value, int) or value < 1:
            issues.append(f"版本号不成立：{name}={value!r}")
    rows = list(described.get("assemblies") or ())
    if rows:
        issues.extend(_assembly_problems(rows))
        if len(rows) == 1 and described.get("capabilities") is None:
            issues.append("没有能力声明（capabilities 为 null）")
    elif described.get("capabilities") is None:
        issues.append("没有能力声明（capabilities 为 null）")
    return issues


def _assembly_problems(rows: list[Mapping[str, Any]]) -> list[str]:
    """逐条检查每一套装配（重复的 firmware_id 本身就是"没一一对应"）。"""
    issues: list[str] = []
    seen: set[str] = set()
    for row in rows:
        firmware_id = str(row.get("firmware_id") or "")
        where = firmware_id or "<没有 firmware_id 的那一套>"
        if not firmware_id:
            issues.append("有一套装配没说 firmware_id")
        elif firmware_id in seen:
            issues.append(f"同一份 firmware 登记了不止一套装配：{firmware_id}")
        seen.add(firmware_id)
        if not row.get("label"):
            issues.append(f"装配 {where} 没有 label（说不清这是哪套组合）")
        if not row.get("target_id"):
            issues.append(f"装配 {where} 没挂到任何目标（target_id 为空）")
        if not row.get("platform"):
            issues.append(f"装配 {where} 没说平台（platform 为空）")
        if not row.get("adapters"):
            issues.append(f"装配 {where} 没有列出受信适配器")
        if row.get("capabilities") is None:
            issues.append(f"装配 {where} 没有能力声明（capabilities 为 null）")
        if row.get("session_capabilities") in (None, {}):
            issues.append(f"装配 {where} 没有会话能力声明（session_capabilities 为空）")
        if row.get("pipeline_allowed"):
            if not row.get("project"):
                issues.append(
                    f"装配 {where} 可以被自动验证，却没说清它用哪个项目包（project 为空）"
                )
            if not row.get("suites"):
                issues.append(
                    f"装配 {where} 可以被自动验证，却没登记任何套件（suites 为空）"
                )
    return issues


__all__ = [
    "SCHEMA",
    "SCHEMA_VERSIONS",
    "AssemblyFacts",
    "TargetFacts",
    "describe",
    "describe_assemblies",
    "problems",
    "targets_from_profiles",
    "versions",
]

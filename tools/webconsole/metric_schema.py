"""指标的**模型与规则**（**core**， / R2c）。

一张趋势图要能单独被人看懂，每一行数据都得自己交代三件事：**是什么**（key/label）、
**量纲**（unit）、**朝哪边是好**（better）。这三件事的**规则**属于平台：

| 平台（本模块） | 项目包（注册方） |
| --- | --- |
| 一条指标是什么形状（`MetricSpec`：key/source/field/unit/better/label） | 有哪些指标（自己的表） |
| 单位取自封闭集合（拼错的单位会让轴标签骗人） | 每条指标用哪个单位 |
| 方向只有 higher/lower/neutral（没有方向的折线图只能靠猜） | 每条指标朝哪边是好 |
| 时间轴怎么定（每个记录类型一个时间字段 + 换算比例） | 每个记录类型用哪个字段当时间 |
| **被排除的字段必须写理由**（没有理由的排除 = 固件加了字段没人注意） | 每个被排除字段的理由 |

R2c 之前这套东西和 示例项目 的指标表混在同一个模块里（`metrics.py`，那本来就是项目模块），
于是"平台侧的规则"和"这个项目的指标"分不开。现在模型与校验在 core，
项目包只负责**注册一张表**（`示例项目_metrics.LOGGER_SCHEMA`）。

⚠️ 这里不 import 目标/项目模块，也不知道有 "示例项目" 这个项目。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

#: 指标导出文件的 schema 版本（进文件第一行）。
SCHEMA = 1

# 单位取自一个封闭集合：拼错的单位会让图表的轴标签骗人，而且是那种最难发现的骗法
# （"bytes" 和 "B" 看起来都对）。
UNITS = ("B", "KB", "ms", "ns", "s", "count", "percent", "celsius", "flag")
# 方向。图表要把这句话印在标题旁边：一个没有方向的折线图，看的人只能自己猜
# 向上是好还是坏 —— 而 `tx_stack` 是**剩余**字节，猜错就正好反过来。
BETTER = ("higher", "lower", "neutral")


@dataclass(frozen=True)
class MetricSpec:
    """一个可画的指标，以及"它是什么"的完整说明。

    ``key`` 带上记录类型前缀（``metric.sys.heap``）：同名字段在不同记录里是**不同的
    测量**（`fs.open.bytes` 是文件已用字节，`fs.write.bytes` 是这次写入的字节），
    只按字段名合并会把两件不同的事画成一条线。
    """

    key: str
    source: str
    field: str
    unit: str
    better: str
    label: str


@dataclass(frozen=True)
class MetricSchema:
    """某个项目包注册的指标表 + 时间轴 + 排除理由（一张纯数据的表）。

    `problems()` 是平台侧的那条规矩：一张**说不清自己**的指标表不许上图表
    （单位不在封闭集合里、方向没写、排除了字段却没写理由）。
    """

    specs: tuple[MetricSpec, ...] = ()
    #: `记录类型 -> (时间字段, 换算到秒的比例)`
    time_fields: Mapping[str, tuple[str, float]] = field(default_factory=dict)
    #: `记录类型 -> {被排除的字段: 理由}`
    excluded: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: 项目包自己的名字（进表头，方便一眼看出这份导出是谁写的）。
    label: str = ""
    #: 时间轴的人话说明（进表头与页面标题）。
    t_axis: str = "设备 uptime（秒，重启后归零）"

    # ------------------------------------------------------------------ 索引
    @property
    def by_source(self) -> dict[str, tuple[MetricSpec, ...]]:
        out: dict[str, tuple[MetricSpec, ...]] = {}
        for spec in self.specs:
            out[spec.source] = out.get(spec.source, ()) + (spec,)
        return out

    @property
    def by_key(self) -> dict[str, MetricSpec]:
        return {spec.key: spec for spec in self.specs}

    def keys(self) -> tuple[str, ...]:
        return tuple(spec.key for spec in self.specs)

    def specs_for(self, source: str) -> tuple[MetricSpec, ...]:
        return self.by_source.get(source, ())

    # ---------------------------------------------------------------- 时间轴
    def time_of(self, kind: str, fields: Mapping[str, Any]) -> int | None:
        """设备 uptime（秒）；这个记录没有时间轴、或值不是数，就返回 None。"""
        entry = self.time_fields.get(kind)
        if entry is None:
            return None
        name, scale = entry
        value = fields.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return int(value * scale)

    def excluded_fields(self, kind: str) -> Mapping[str, str]:
        return self.excluded.get(kind, {})

    # ---------------------------------------------------------------- 自述
    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "label": self.label,
            "t_axis": self.t_axis,
            "metrics": [
                {
                    "key": spec.key,
                    "source": spec.source,
                    "field": spec.field,
                    "unit": spec.unit,
                    "better": spec.better,
                    "label": spec.label,
                }
                for spec in self.specs
            ],
        }

    def problems(self) -> list[str]:
        """这张表自己站不站得住（空列表 = 站得住）。"""
        issues: list[str] = []
        seen: set[str] = set()
        for spec in self.specs:
            if spec.key in seen:
                issues.append(f"{spec.key}：同一个 key 注册了两次")
            seen.add(spec.key)
            if spec.unit not in UNITS:
                issues.append(f"{spec.key}：单位 {spec.unit!r} 不在封闭集合里")
            if spec.better not in BETTER:
                issues.append(f"{spec.key}：方向 {spec.better!r} 不在封闭集合里")
            if not spec.label.strip():
                issues.append(f"{spec.key}：没有给人读的名字")
        for kind in sorted(set(self.time_fields) | set(self.excluded)):
            for name, reason in (self.excluded.get(kind) or {}).items():
                if len(str(reason).strip()) < 6:
                    issues.append(f"{kind}.{name}：被排除了却没写清理由")
            entry = self.time_fields.get(kind)
            if entry is not None and (
                not entry[0] or not isinstance(entry[1], (int, float)) or entry[1] <= 0
            ):
                issues.append(f"{kind}：时间轴字段/比例不成立（{entry!r}）")
        for spec in self.specs:
            if spec.source not in self.time_fields:
                issues.append(
                    f"{spec.source}：有指标却没登记时间轴（这些点会全部变成 untimed）"
                )
        return issues


def spec(source: str, field: str, unit: str, better: str, label: str) -> MetricSpec:
    """一行指标的简写（key 由 source + field 拼出来，避免手写不一致）。"""
    return MetricSpec(
        key=f"{source}.{field}",
        source=source,
        field=field,
        unit=unit,
        better=better,
        label=label,
    )


def metric_row(specification: MetricSpec) -> dict[str, Any]:
    """进表头/页面的一行指标定义（`series()` 的 `by_key` 也用它）。"""
    return asdict(specification)


def specs_from(rows: Iterable[MetricSpec]) -> tuple[MetricSpec, ...]:
    return tuple(rows)


__all__ = [
    "BETTER",
    "SCHEMA",
    "UNITS",
    "MetricSchema",
    "MetricSpec",
    "metric_row",
    "spec",
    "specs_from",
]

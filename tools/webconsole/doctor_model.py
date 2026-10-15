#!/usr/bin/env python3
""" / D0：平台体检的**契约模型**（**core**）。

这个模块只做一件事：把"一次体检报告"的形状**冻结**下来 —— 状态取值、检查项字段、
顶层结果、退出码、以及四条**不许含糊**的写作规矩。它不碰任何东西：
不 import 目标/项目模块、不打开串口、不起服务、不看文件。

需求见 （见公开文档）
§5（输出契约）与 §2（三层边界）。四层语义在这里定死：

| 状态 | 含义 | 谁会给 |
| --- | --- | --- |
| `PASS` | 检查已执行且满足要求 | 三层都能给 |
| `WARN` | 平台仍可运行，但某项可选能力/目标依赖缺失 | 任何层；**不**制造非零退出码 |
| `FAIL` | 平台基本能力不可用（只允许出现在平台层） | 平台层 |
| `SKIP` | 需要硬件/额外授权，或本次未选择 | 目标/硬件层 |

三条写作规矩（每一条都有 `test_doctor.py` 的契约测试钉着，写报告的人绕不过去）：

1. **`required` 的检查不许写 `SKIP`。** "没判定" 不是 "没事"（本仓库的老教训）；
   一项必需能力如果这次没法判定，它要么 `FAIL`（阻断）要么如实降级成可选检查。
2. **`FAIL` / `WARN` 必须写 `remediation`，且 `FAIL` / `WARN` / `SKIP` 必须写 `reason`。**
   报告的价值一半在"哪里不对"，另一半在"下一步做什么"（需求 §1.3）。
3. **顶层 `result` 由检查项推出**：有 `FAIL` 就是 `FAIL`，否则有 `WARN` 就是 `WARN`，
   否则 `PASS`。**`SKIP` 不参与**（跳过不是通过，但它自己也不该把报告拉成红）。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Iterator

#: 报告 schema 版本（与 `platform_info.SCHEMA_VERSIONS` 里登记的那份一致）。
SCHEMA = 1

#: 退出码（需求 §5）：0 = 无平台 FAIL；1 = 存在平台阻断；2 = Doctor 自身无法完成报告。
EXIT_OK = 0
EXIT_PLATFORM_FAIL = 1
EXIT_INCOMPLETE = 2


class Status:
    """四种状态。**没有第五种**，也没有"默认"。"""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ALL = (PASS, WARN, FAIL, SKIP)
    #: 严重度：顶层 `result` 取最重的那个（`SKIP` 最轻，不参与判定）。
    SEVERITY = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}

    @classmethod
    def worst(cls, statuses: Iterable[str]) -> str:
        chosen = cls.SKIP
        for value in statuses:
            if cls.SEVERITY[value] > cls.SEVERITY[chosen]:
                chosen = value
        return chosen


class Scope:
    """三层边界（需求 §2）：平台基础 / 目标适配 / 硬件在线。"""

    PLATFORM = "platform"
    TARGET = "target"
    HARDWARE = "hardware"
    ALL = (PLATFORM, TARGET, HARDWARE)

    #: 人话名字（页面与 CLI 分组标题共用；**不是**判定依据）。
    #: ⚠️ 2026-10-12 用户口径：“删掉标题里的 platform / target / hardware 等代码名称，
    #:    改为「平台环境 / 开发工具 / 硬件连接」等用户语言” —— 页面不再把内部键名摆在标题里。
    LABELS = {
        PLATFORM: "平台环境",
        TARGET: "开发工具",
        HARDWARE: "硬件连接",
    }


#: 默认安全规则（需求 §3）的**机器可读副本**：谁改了探测边界，先来改这里。
#: 这些是给人和测试读的约束，不是运行时开关。
SAFETY_RULES: tuple[str, ...] = (
    "不启动常驻服务：端口检查用绑定后立即释放的方式",
    "不构建、不下载依赖、不修改 PATH 或环境变量",
    "不打开串口、不发送字节、不操作 DTR/RTS",
    "不访问调试探针、不复位、不烧录、不擦除",
    "不修改工程、用户配置或已有证据",
    "不把绝对路径、用户名等本机信息上传到网络",
)

#: 检查项 id 的形状：小写、点号分段（`platform.python.version`）。
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._][a-z0-9]+)+$")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Check:
    """一条检查项：**要么是实测值，要么写明为什么给不出来**。"""

    id: str
    scope: str
    status: str
    #: 这一项是不是"平台必需能力"（`False` = 可选/目标/硬件项）。
    #: ⚠️ `required` 的项**不许** `SKIP`（见模块 docstring 规矩 1）。
    required: bool = False
    #: 人话标题（CLI 与页面用它，不用 id 猜）。空 = 调用方自己用 id 兜底。
    title: str = ""
    #: 分组键（`python` / `files` / `port` / `serial` / `tooling` …），页面按它分组。
    group: str = ""
    #: 分类的**人话名字**（页面二级分组用它；目标层填的是"这条属于哪个目标/哪份固件"）。
    #: 留空表示调用方没给：页面退回用 `group`，**不在页面里另抄一份映射表**。
    group_label: str = ""
    #: 实测值（一句话；没有就给空串，**不要填一个看起来正常的默认值**）。
    actual: str = ""
    #: 期望（一句话）。
    expected: str = ""
    #: 为什么是这个状态（FAIL/WARN/SKIP 必填）。
    reason: str = ""
    #: 下一步该做什么（FAIL/WARN 必填）。
    remediation: str = ""

    def __post_init__(self) -> None:
        _require(bool(ID_PATTERN.match(self.id)), f"检查项 id 形状不对：{self.id!r}")
        _require(self.scope in Scope.ALL, f"{self.id}：scope {self.scope!r} 不认识")
        _require(self.status in Status.ALL, f"{self.id}：status {self.status!r} 不认识")
        if self.required:
            _require(
                self.status != Status.SKIP,
                f"{self.id}：必需检查项不许写 SKIP（没判定不是没事）——"
                "要么如实 FAIL，要么把它降级成可选检查",
            )
        if self.status in (Status.FAIL, Status.WARN):
            _require(
                bool(self.remediation.strip()),
                f"{self.id}：{self.status} 必须写 remediation（报告要告诉人下一步做什么）",
            )
        if self.status in (Status.FAIL, Status.WARN, Status.SKIP):
            _require(
                bool(self.reason.strip()),
                f"{self.id}：{self.status} 必须写 reason（说明为什么是这个状态）",
            )

    # ------------------------------------------------------------------ 导出
    @property
    def label(self) -> str:
        return self.title or self.id

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "required": bool(self.required),
            "status": self.status,
            "title": self.title,
            "group": self.group,
            "group_label": self.group_label,
            "actual": self.actual,
            "expected": self.expected,
            "reason": self.reason,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class Report:
    """一次体检的全部检查项（顶层结果与汇总由它推出，不单独存）。"""

    checks: tuple[Check, ...] = ()
    #: 这次体检跑的是哪一套组合（人读；装配处给）。
    label: str = ""
    #: 生成时刻（UTC，ISO8601；给复现与"这份报告是什么时候的"用）。
    generated_utc: str = ""
    schema: int = SCHEMA

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for check in self.checks:
            _require(check.id not in seen, f"检查项 id 重复：{check.id!r}")
            seen.add(check.id)

    # ------------------------------------------------------------------ 查询
    def result(self) -> str:
        """顶层结果：`FAIL` > `WARN` > `PASS`（`SKIP` 不参与 —— 跳过不是通过）。"""
        if any(check.status == Status.FAIL for check in self.checks):
            return Status.FAIL
        if any(check.status == Status.WARN for check in self.checks):
            return Status.WARN
        return Status.PASS

    def summary(self) -> dict[str, int]:
        """四类状态的计数（键名按需求 §5 的最小 JSON 形状：小写）。"""
        counts = {name.lower(): 0 for name in Status.ALL}
        for check in self.checks:
            counts[check.status.lower()] += 1
        return counts

    def by_scope(self, scope: str) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.scope == scope)

    def blocking(self) -> tuple[Check, ...]:
        """会造成平台阻断的检查项（= `FAIL`）——其实就是"退出码 1 的理由"。"""
        return tuple(check for check in self.checks if check.status == Status.FAIL)

    def actions(self) -> tuple[Check, ...]:
        """需要人做点什么的检查项（`FAIL` 在前、`WARN` 在后），给 CLI 的"下一步"。"""
        return tuple(
            check for check in self.checks
            if check.status in (Status.FAIL, Status.WARN) and check.remediation.strip()
        )

    def __iter__(self) -> Iterator[Check]:
        return iter(self.checks)

    # ------------------------------------------------------------------ 导出
    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "result": self.result(),
            "summary": self.summary(),
            # 三层的显示名（页面用它给分组起标题）：与 `render_lines()` 同一份出处，
            # 页面因此不用自己抄一遍"平台基础能力 / 目标适配就绪度 / 硬件在线探测"。
            "scopes": [
                {"scope": name, "label": Scope.LABELS[name]} for name in Scope.ALL
            ],
            "checks": [check.to_json() for check in self.checks],
            "label": self.label,
            "generated_utc": self.generated_utc,
        }


def exit_code(report: Report, *, complete: bool = True) -> int:
    """报告 → 退出码（需求 §5）。

    `complete=False` 表示 Doctor 自己没能把报告跑完（异常、零检查项）⇒ `2`。
    目标依赖缺失只会给出 `WARN`，因此**不会**制造非零退出码。
    """
    if not complete or not report.checks:
        return EXIT_INCOMPLETE
    return EXIT_PLATFORM_FAIL if report.blocking() else EXIT_OK


def render_lines(report: Report) -> list[str]:
    """人读输出（纯函数，CLI 直接打印；测试也用它）。

    ⚠️ 只用 ASCII 标记（`[PASS]` 之类）：本仓库的终端在重定向时走 GBK，
    `✓` / `⚠` 这类字符会让"正在打印报告"的那一行当场抛 `UnicodeEncodeError`。
    """
    counts = report.summary()
    lines = [
        f"平台体检报告（ · schema {report.schema}）",
    ]
    if report.label:
        lines.append(f"组合：{report.label}")
    if report.generated_utc:
        lines.append(f"生成：{report.generated_utc}")
    lines.append(
        f"结果：{report.result()}   "
        f"通过 {counts['pass']} · 警告 {counts['warn']} · "
        f"失败 {counts['fail']} · 跳过 {counts['skip']}"
    )
    for scope in Scope.ALL:
        checks = report.by_scope(scope)
        if not checks:
            continue
        lines.append("")
        lines.append(f"== {Scope.LABELS[scope]}（{scope}）==")
        for check in checks:
            head = f"  [{check.status}] {check.id}"
            if check.title:
                head += f"  {check.title}"
            lines.append(head)
            if check.actual:
                lines.append(f"        实测：{check.actual}")
            if check.expected:
                lines.append(f"        期望：{check.expected}")
            if check.reason:
                lines.append(f"        原因：{check.reason}")
            if check.remediation:
                lines.append(f"        修复：{check.remediation}")
    actions = report.actions()
    if actions:
        lines.append("")
        lines.append("== 下一步 ==")
        for check in actions:
            lines.append(f"  - [{check.status}] {check.remediation}")
    if report.result() == Status.PASS:
        lines.append("")
        lines.append("结论：平台基础能力齐全（没有阻断项）。")
    return lines

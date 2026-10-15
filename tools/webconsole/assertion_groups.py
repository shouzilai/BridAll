"""断言的**分组注册**（**core**， / R2c）。

一条 run 结束时，`report.json` 里那份断言表由**若干组**共同产生：

```text
link（平台链路）       构建身份、采集完整性、身份匹配、解码完整性 —— 只用平台知识，
                       判据由注入的 `LinkContract`（项目文本契约）给出
示例项目  DHT 引脚与采样、LittleFS 落盘决策、heap 下限、ESP32 panic/reset、
                       记录文件字节级核对、压力套件、指标导出 —— 需要项目业务知识
```

为什么要分组（而不是继续堆在一个列表里）：

1. **能说清"平台自己证明了什么"。** 没有项目包时，链路那一组照样跑得完
   —— 平台的核心能力不依赖某个项目包的存在（R2c 的验收）；
2. **能说清"哪条断言属于谁"。** 一个 DHT 引脚的判据不该出现在平台的能力清单里
   （同一个理由见边界清单 §1：`dht`/`littlefs` 这些词只能出现在项目层）；
3. **报告里能如实写"这一组没跑"**，而不是让读者以为"少了几条是因为没实现"。

⚠️ 这里**只是装配与排序**：不 import 任何项目模块，也不知道有哪几组。
组由装配处按顺序交进来；`evaluate()`把各组的行拼起来后按断言编号排序
（与过去同一条规则：A22 最后算、但仍在原位报告）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: 组名约定（页面与报告里出现的就是这两个字符串）。
GROUP_LINK = "link"
GROUP_PROJECT = "demo-project"

#: 断言的**结论域**：这一行在回答哪一类问题。
#:
#: 为什么要有它：一次 run 的结论经常混着两件不同的事 —— "证据/日志本身健不健康"
#: （采集、解码、完整性、帧有没有被切断）与"被测对象的业务对不对"。混成一句报出来，
#: 读者会把"日志被截断"读成"业务坏了"（真机验收里就发生过：一条被设备自己插行切断的
#: 帧，让整个存储结论看起来是红的）。
#:
#: ⚠️ 两个名字是**通用的**：core 不认识任何具体项目、设备或业务。
#: ⚠️ 分开展示**不等于**放宽判据 —— 域只是"这句话在讲哪一类事"的标签：
#:    真实失败照旧留在表里、照旧计入总分（`result()` 与 `summarize()` 一字未改）。
DOMAIN_EVIDENCE = "evidence"   # 证据与日志健康（能不能完整地读出来）
DOMAIN_BUSINESS = "business"   # 被测对象的业务结论（业务判据对不对）
DOMAINS: tuple[str, ...] = (DOMAIN_EVIDENCE, DOMAIN_BUSINESS)


def result(
    identifier: str,
    description: str,
    expected: str,
    observed: str,
    passed: bool,
    evidence: str,
    domain: str = "",
) -> dict[str, Any]:
    """一行断言的形状（`report.json` / 页面表格读的就是这六个键）。

    ⚠️ 前六个字段名与顺序是**既有契约**（切片 1 就定下来了）；R2c 只搬实现，不改形状。
    `domain` 是**追加**的第 7 个键（R2c 之后加的）：空串 = 这条判据没说自己在讲哪类事，
    页面/报告就**不替它猜**（只对声明过的行分域汇总，见 `by_domain`）。
    """
    return {
        "id": identifier,
        "description": description,
        "expected": expected,
        "observed": observed,
        "status": "pass" if passed else "fail",
        "evidence": evidence,
        "domain": domain,
    }


def contract_result(
    identifier: str, description: str, mismatches: list[str], checked: int
) -> dict[str, Any]:
    """一行"若干项全部匹配"的断言（A9/A10/A17 这类契约式判据共用）。"""
    return result(
        identifier,
        description,
        f"{checked} 项全部匹配" if checked else "至少 1 项记录",
        "全部匹配" if not mismatches else "不符合：" + "；".join(mismatches),
        not mismatches,
        "decoded.log",
    )


def assertion_number(row: dict[str, Any]) -> int:
    """Report sort key: the number in an assertion id (``A24-...`` -> 24).

    Rows that do not follow the convention keep a stable place at the end instead
    of raising: the report is produced during finalization, where an exception used
    to leave a run stuck as ``running`` ().
    """
    head = str(row.get("id") or "").split("-", 1)[0]
    if head.startswith("A") and head[1:].isdigit():
        return int(head[1:])
    return 10**6


def in_report_order(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by assertion number, stable within an id.

    A20/A21 emit several rows per crash and must stay adjacent in the order they
    were produced, which is what a stable sort guarantees. This exists because A22
    is computed last but must still be *reported* in place.
    """
    return sorted(results, key=assertion_number)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Tally a result list into pass/fail counts.

    Defensive on purpose: this runs during finalization, where an exception used
    to kill the worker thread and leave the run stuck as "running" ().
    """
    rows = [row for row in results if isinstance(row, dict)]
    failed = [row.get("id", "?") for row in rows if row.get("status") != "pass"]
    return {
        "total": len(rows),
        "passed": len(rows) - len(failed),
        "failed": len(failed),
        "failed_ids": failed,
    }


def by_domain(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按结论域分桶计数（**只对声明过域的行**分桶，不替谁猜）。

    每一桶 = `summarize()` 的那套词 + 域名。顺序：`DOMAINS` 里登记过的先上，
    之后才是没登记过的域名（按首次出现）。

    ⚠️ 一桶都没有（这次 run 的判据都没声明域）⇒ 返回**空列表**：
    页面据此退回"只报一个总数"的老样子，不凭空造一个域出来。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        name = str(row.get("domain") or "")
        if not name:
            continue
        buckets.setdefault(name, []).append(row)
    ordered = [name for name in DOMAINS if name in buckets]
    ordered += [name for name in buckets if name not in DOMAINS]
    return [{"domain": name, **summarize(buckets[name])} for name in ordered]


@dataclass(frozen=True)
class AssertionGroup:
    """一组断言：一个名字 + 一个"给我上下文、还我一串结果行"的函数。

    `evaluate` 收到的是关键字上下文（`decoded` / `manifest` / `events` / …），
    它只往结果里**加**行，不关心别的组跑了没有。
    """

    name: str
    label: str
    evaluate: Callable[..., list[dict[str, Any]]]
    #: 这一组是不是"项目业务"（页面与报告可以据此说明它为什么可能缺席）。
    project: bool = False

    def run(self, **context: Any) -> list[dict[str, Any]]:
        return list(self.evaluate(**context))


@dataclass(frozen=True)
class AssertionRegistry:
    """按顺序摆好的几组断言（装配处给什么就是什么，这里不自动发现）。"""

    groups: tuple[AssertionGroup, ...] = ()
    #: 这次运行里**被禁用**的组（报告里要如实写出来，不能装作它们不存在）。
    disabled: tuple[str, ...] = field(default=())

    def names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self.groups)

    def evaluate(self, **context: Any) -> list[dict[str, Any]]:
        """跑完所有组，按断言编号排序（与过去同一条报告顺序规则）。"""
        rows: list[dict[str, Any]] = []
        for group in self.groups:
            rows.extend(group.run(**context))
        return in_report_order(rows)

    def to_json(self) -> dict[str, Any]:
        return {
            "groups": [
                {"name": group.name, "label": group.label, "project": group.project}
                for group in self.groups
            ],
            "disabled": list(self.disabled),
        }


def registry(
    *groups: AssertionGroup, disabled: tuple[str, ...] = ()
) -> AssertionRegistry:
    return AssertionRegistry(groups=tuple(groups), disabled=disabled)

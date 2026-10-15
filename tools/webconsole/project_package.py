"""**项目包**（**core**， / R3e）。

编排（`pipeline`）需要项目的几样业务东西：记录解析、指标导出、断言表、记录文件格式、
套件名、基线位置。R2 之前它们是**直接 import**（`import assertions` / `import metrics` …），
于是"平台编排"与"示例项目 这个项目"在代码上绑死 —— 换一个项目就得改编排。

这里把那一半收成一个**可注册的对象**：

```text
ProjectPackage
  ├─ identity      key / label / token_domain
  ├─ suites        ("smoke", "stress")
  ├─ events        parse(text) -> [事件] / to_jsonl / summarize / from_jsonl
  ├─ metrics       series(events) -> [点] / to_jsonl / from_jsonl
  ├─ assertions    evaluate(...) / summarize(results) / registry
  ├─ expected_format_strings   构建字典必须含的格式串（固件与字典同步的证据）
  ├─ records       记录文件：parse_dump / parse / parse_csv_view
  ├─ baseline      load() / path
  ├─ dialogue      与设备的固定对话（等横幅 / 清记录 / 对时 / 取记录文件）—— R4
  └─ 套件策略       采集窗口 / 构建环境 / suite_run_defaults
```

⚠️ 最后两项是 ** / R4** 加进来的（）：以前它们写在编排里
（`pipeline.SUITE_ENVS` / `pipeline.DEFAULT_OBSERVE_SECONDS_BY_SUITE` /
`pipeline.TOKEN_DOMAIN` 与那整段固定观察对话）—— 那全是**项目知识**，
留着就等于"换一个项目就要改编排"。现在编排只问项目包要这些事实：

| 事实 | 字段 | 没说的时候编排怎么办 |
| --- | --- | --- |
| 与设备说什么 | `dialogue` | 一句都不说（`device_dialogue.silent_dialogue()`）——只读采集 |
| 采集窗口多长 | `default_observe_seconds` / `observe_seconds_by_suite` | 用一个**中立**的保守值，并说明它不代表任何项目的取样节奏 |
| 某个套件必须用哪个构建环境 | `suite_environments` | 不做这条检查（没有声明就没有约束） |
| 解哪个 token domain | `token_domain` | **不解码**（`SKIPPED`），并说明"没有声明 domain"—— 绝不猜一个 |
| 身份靠什么确定 | `run_defaults.probe_command` | 不发任何命令（只读采集） |
  | 要不要对时 / 取记录文件 | `run_defaults.{set_clock,dump_records}` | 都不做（`False`） |
| 本次运行参数从哪来 | `run_defaults` + `suite_run_defaults` + 显式参数 | 显式 > 套件声明 > 项目声明 > 中立默认 |

装配处（`server.py` / CLI / 测试）把它交进编排；**没装**时是一份诚实的空
（`no_project()`）：采集与证据仍然完整，解码结果照写，但**不会**有结构化事件、
指标与判据 —— 而且报告里会说清"没有项目包"，而不是给出一个 0 分的通过。

⚠️ 这里只定义**形状与契约**（`Any`-typed 的 duck-typed 模块 + 上面那份步骤表），
不 import 任何项目模块，也不知道"示例项目"这个词。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

try:
    from .embedded_test_model import EMPTY_TEST_CATALOG, TestCatalog
except ImportError:  # Top-level imports used by existing CLI entry points.
    from embedded_test_model import EMPTY_TEST_CATALOG, TestCatalog  # type: ignore[no-redef]


@dataclass(frozen=True)
class RunOption:
    """A project-owned run input rendered by generic clients."""

    key: str
    label: str
    kind: str = "boolean"
    default: Any = False
    help: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "default": self.default, "help": self.help}


@runtime_checkable
class EventsContract(Protocol):
    """项目的事件层：把解码后的明文变成结构化事件（`webconsole/app_events.py` 的形状）。"""

    def parse(self, text: bytes | str) -> list[Any]: ...

    def summarize(self, events: list[Any]) -> dict[str, Any]: ...

    def to_jsonl(self, events: list[Any]) -> bytes: ...

    def from_jsonl(self, data: bytes) -> list[Any]: ...


@runtime_checkable
class MetricsContract(Protocol):
    """项目的指标层：事件 → 指标点（`webconsole/metrics.py` 的形状）。"""

    def series(self, events: list[Any]) -> dict[str, Any]: ...

    def to_jsonl(self, series: dict[str, Any]) -> bytes: ...

    def from_jsonl(self, data: bytes) -> dict[str, Any]: ...


@runtime_checkable
class AssertionsContract(Protocol):
    """项目的判据层：一组按编号排序的断言（`webconsole/assertions.py` 的形状）。"""

    def evaluate(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def summarize(self, results: list[Any]) -> dict[str, Any]: ...


@runtime_checkable
class RecordsContract(Protocol):
    """项目的记录文件格式（宿主侧的第二实现，`webconsole/record_file.py` 的形状）。"""

    def parse_dump(self, text: str) -> dict[str, Any]: ...

    def parse(self, data: bytes) -> dict[str, Any]: ...

    def parse_csv_view(self, text: str) -> Any: ...


@runtime_checkable
class BaselineContract(Protocol):
    """项目的机器可读基线（`tools/基线模块` 的形状）。"""

    def load(self, path: Path | None = None) -> dict[str, Any]: ...


@runtime_checkable
class TestProtocolContract(Protocol):
    """Project-owned device test protocol consumed by the generic Core runner."""

    def list_command(self) -> str: ...
    def run_command(self, run_id: str, case_id: str, version: int,
                    parameters: dict[str, Any]) -> str: ...
    def cancel_command(self, run_id: str) -> str: ...
    def parse_line(self, line: str) -> dict[str, Any] | None: ...
    def validate_listing(self, events: list[dict[str, Any]]) -> list[str]: ...


@dataclass(frozen=True)
class ProjectPackage:
    """一个项目包（缺省 = 什么都没装：`no_project()`）。"""

    #: 项目标识（示例项目）与给人看的名字。
    key: str = ""
    label: str = ""
    #: 解码时按哪个 domain 过滤 token（与 `TargetProfile.token_domain` 同源）。
    token_domain: str = ""
    #: 这个项目有哪些套件（页面下拉、CLI 校验与实际跑的那一份都读它）。
    suites: tuple[str, ...] = field(default=())
    #: 下面五件都是**模块形状的对象**（各自的 Contract 见上面）。
    events: Any = None
    metrics: Any = None
    assertions: Any = None
    records: Any = None
    baseline: Any = None
    #: 这个项目的断言是否建立在**解码出来的行**上（）。
    #: 默认 `True`：绝大多数断言读的就是 `[I][boot]` / `[I][metric]` 那些解码记录，
    #: 所以"这份固件不做解码"时它们**不适用**（`SKIPPED`），而不是失败 ——
    #: 把"验不了"报成 FAIL，会在报告里留下一堆假的红灯（需求 §2：依赖项必须明确 SKIPPED）。
    #: 项目若有不依赖解码行的纯结构断言，可以把这一项显式设成 `False`。
    assertions_require_decode: bool = True
    #: 构建字典里必须出现的格式串（固件与字典不同步时，构建步骤当场失败）。
    expected_format_strings: tuple[str, ...] = field(default=())
    #: 与设备的**固定对话**（`device_dialogue.DeviceDialogue`）：等启动横幅、清记录、
    #: 对时、窗口末尾取记录文件。**没声明就是"不对设备说任何话"**（只读采集）——
    #: 编排不会替它装一套默认对话（那正是  要消掉的东西）。
    dialogue: Any = None
    #: 实时观察连接后用于查询身份的只读命令；空 = Core 的通用 `INFO JSON`。
    #: 首次等待由项目声明，避免设备启动初始化期间吞掉命令。
    identity_query: str = ""
    identity_query_delay_s: float = 0.0
    #: 采集窗口的默认秒数（**项目知识**：参考项目是"一个记录周期 30 s + 余量"）。
    #: 0 = 没声明 ⇒ 编排用一个中立的保守值，并在 run 事件里说明它不代表任何项目的节奏。
    default_observe_seconds: float = 0.0
    #: 某些套件要更长的窗口（压力固件先灌 4 s 再排空，30 s 才够）。
    observe_seconds_by_suite: Mapping[str, float] = field(default_factory=dict)
    #: 某些套件**必须**用哪个构建环境跑（不然报告会拿一个构建的数字说另一个构建的事）。
    suite_environments: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: 套件对项目运行默认值的覆盖。Core 只合并这些中性键，不理解键背后的设备协议。
    #: 例如项目可以让日常 smoke 保持轻量，把昂贵的证据采集留给专项套件。
    suite_run_defaults: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: 项目自己的运行输入；通用 Web 只按声明渲染，不认识 key 的业务含义。
    run_options: tuple[RunOption, ...] = field(default=())
    #: 项目自己声明的**运行参数**（W5 真机验收时补的，）：
      #: `probe_command`（向设备要一次明文身份横幅的命令）/ `set_clock` /
      #: `dump_records` / `clear_records`。
    #:
      #: ⚠️ 这几件以前是 `Pipeline.start()` 的**默认值**（`dump_records=True` /
    #: `dump_records=True` / `probe_command=""`）—— 那是参考项目（示例项目）的固件事实，
    #: 不是平台的。于是第二个目标走 Web 时拿到的是别人的默认值：裸机那一轮烧录+采集都
    #: 成功了，却因为`probe_command` 为空、拿不到明文身份而 **`decode: UNKNOWN_BUILD`**
    #: （拒绝猜字典是对的，但“没人告诉平台要问一句 BANNER”是本轮要修的）。
    #:
    #: 现在优先级是：**显式参数 > 项目声明 > 中立默认（什么都不做）**。
    #: 声明空 = “这个项目不需要这几样”，编排不会替它装一套。
    run_defaults: Mapping[str, Any] = field(default_factory=dict)
    #: Project-owned embedded test cases and safety policies. Execution is a later slice;
    #: A0 only freezes and validates this declaration.
    test_catalog: TestCatalog = EMPTY_TEST_CATALOG
    #: Device-autonomous protocol implementation. Core only calls this shape.
    test_protocol: TestProtocolContract | None = None

    def run_defaults_for_suite(self, suite: str = "") -> dict[str, Any]:
        """合并项目与套件默认值；不知道任何具体项目或设备语义。"""
        result = dict(self.run_defaults)
        result.update(dict(self.suite_run_defaults.get(suite, {}) or {}))
        return result

    def run_default(self, key: str, fallback: Any = None, *, suite: str = "") -> Any:
        """项目/套件声明的运行参数（没声明就退回 `fallback`）。"""
        return self.run_defaults_for_suite(suite).get(key, fallback)

    def validate_run_options(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(values or {})
        declared = {row.key: row for row in self.run_options}
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ValueError(f"未声明的项目参数：{unknown}")
        result: dict[str, Any] = {}
        for key, option in declared.items():
            value = supplied.get(key, option.default)
            if option.kind == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"项目参数 {key!r} 必须是 boolean")
            else:
                raise ValueError(f"项目参数 {key!r} 使用了不支持的类型 {option.kind!r}")
            result[key] = value
        return result

    # ------------------------------------------------------------------ 自述
    def provided(self) -> dict[str, bool]:
        """这套装配到底装了什么（诊断用；一眼看出"少了哪一件"）。"""
        return {
            "events": self.events is not None,
            "metrics": self.metrics is not None,
            "assertions": self.assertions is not None,
            "records": self.records is not None,
            "baseline": self.baseline is not None,
            "suites": bool(self.suites),
            "expected_format_strings": bool(self.expected_format_strings),
        }

    def problems(self) -> list[str]:
        """缺哪一件（编排把这份清单说给用户听，而不是静默降级）。"""
        return [
            f"项目包没提供 {name}"
            for name, present in sorted(self.provided().items())
            if not present
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "key": self.key,
            "label": self.label,
            "token_domain": self.token_domain,
            "suites": list(self.suites),
            "expected_format_strings": len(self.expected_format_strings),
            "provided": self.provided(),
            "problems": self.problems(),
            # 采集窗口与对话（R4）：只增字段，老工具读不到不会坏。
            "observe": {
                "default_seconds": float(self.default_observe_seconds or 0.0),
                "by_suite": {name: float(value) for name, value in self.observe_seconds_by_suite.items()},
                "suite_environments": {
                    name: list(envs) for name, envs in self.suite_environments.items()
                },
            },
            "dialogue": self.dialogue.to_json() if self.dialogue is not None else None,
            "identity_query": self.identity_query,
            "identity_query_delay_s": float(self.identity_query_delay_s or 0.0),
            "run_options": [row.to_json() for row in self.run_options],
            # 项目声明的运行参数（只增字段；老读法读不到不会坏）。
            "run_defaults": dict(self.run_defaults),
            "suite_run_defaults": {
                name: dict(values) for name, values in self.suite_run_defaults.items()
            },
            "test_catalog": self.test_catalog.to_json(),
            "test_protocol": self.test_protocol is not None,
        }


#: 没有项目包：采集/证据照旧，但**没有**结构化事件、指标与判据（如实降级）。
NO_PROJECT = ProjectPackage(key="", label="（未装配项目包）")


def no_project() -> ProjectPackage:
    """一份诚实的空项目包（测试与"只想看原始日志"的场景用）。"""
    return NO_PROJECT


__all__ = [
    "AssertionsContract",
    "BaselineContract",
    "EventsContract",
    "MetricsContract",
    "NO_PROJECT",
    "ProjectPackage",
    "RunOption",
    "RecordsContract",
    "TestProtocolContract",
    "no_project",
]

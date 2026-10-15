"""硬件接入契约（**core**）。

一个平台接入一条新硬件，本来要靠人翻工程、翻工具的帮助、翻历史文档。这个模块把那条
路径里**通用、可判、可复核**的那一半冻结成数据与纯函数：

```text
StepSpec            一个受控步骤：用哪个适配器、跑什么参数、拿什么判成功
SuccessCriterion    成功判据（退出码 / 日志里必须出现什么、必须不出现什么、产物必须出现）
FaultMarker         已知失败标记 → 稳定故障码 + 已知含义
FaultKnowledge      标记表 + 恢复建议：把「看到这个标记 → 下一步做什么」变成数据
CapabilityResult    supported / unsupported / skipped / refused 四态，必须带理由
HardwareDeclaration 一条接入契约：它声明的一切（工具、入口、步骤、判据、故障、能力）
```

三条规矩：

1. **判据是数据，不是分支。** 「这次构建算不算成功」由该步骤声明的
   `SuccessCriterion` 决定。适配器与编排里**不许**出现「按步骤名选判据」这种写法
   （`if step == "build": 用另一种工具的口径`）——正是那种写法让同一个闭环里
   两套工作方式各判各的。
2. **失败要自己带着下一步。** 已知标记映射到**稳定故障码**与处置；不认得的标记
   如实报 `unknown_failure` 并保留原文，不编原因。
3. **能力四态，不许含糊。** 做不到的说 `unsupported`，这一轮不做的说 `skipped`，
   安全闸门没过的说 `refused`；三者都必须有一句理由，且都不等于 `supported`。

这个模块**只有形状与纯函数**：不 import 任何目标/项目模块、不开串口、不跑命令、
不读文件、不看时钟。路径、工具、串口都由装配处（入口）交进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

#: 契约版本。**新增可选字段不改它**；破坏性变更必须提升版本并给出迁移说明。
#: 消费方（入口、证据、未来的服务外壳）拿它决定"这条声明我认不认得"。
CONTRACT_VERSION = 1

#: 统一入口的步骤顺序（也是依赖顺序）。`preflight` 由编排**合成**（它只做输入与台架
#: 配置的存在性检查），所以声明里不写它 —— 但每一条接入契约都必然有它。
STEP_ORDER: tuple[str, ...] = ("preflight", "build", "flash", "verify", "debug-check")

#: 声明里可以写、也必须能判的步骤（`preflight` 除外）。
DECLARED_STEPS: tuple[str, ...] = tuple(step for step in STEP_ORDER if step != "preflight")

#: 能力 / 步骤的结论四态。`skipped` 与 `refused` 分开：前者是"这一轮不做"，
#: 后者是"闸门不让做"——处置完全不同。
CAPABILITY_RESULTS: tuple[str, ...] = ("supported", "unsupported", "skipped", "refused")

#: 一个步骤跑完之后的状态（比能力四态多一个 `pass` / `fail`）。
STEP_STATUSES: tuple[str, ...] = (
    "pass", "fail", "unsupported", "skipped", "refused",
)

#: 整份操作的结论。
REPORT_RESULTS: tuple[str, ...] = ("pass", "fail", "refused", "skipped", "incomplete")

#: 稳定故障码（**封闭集合**）。新增一个要同时加进 `FAULT_LABELS` 与处置建议。
FAULT_NONE = "none"
FAULT_CODES: tuple[str, ...] = (
    "none",
    "tool_missing",         # 工具找不到 / 解不出来
    "input_missing",        # 声明跑之前必须存在的输入不在
    "bench_unconfigured",   # 这一趟真要用到的台架键没配置
    "build_failed",
    "flash_failed",
    "flash_refused",        # 安全闸门没过（没有显式同意）
    "verify_failed",
    "identity_mismatch",    # 拿到的身份与声明不符
    "identity_unavailable", # 拿不到设备自报 ⇒ 未核验（不判通过）
    "transport_unavailable",# 串口实现 / 端口打不开
    "no_output_timeout",    # 超时且一个字节都没输出
    "timeout",
    "fuse_open",            # 同一故障连续两次之后，第三次在调工具之前被拒
    "unknown_failure",
)

#: 故障码 → 一句人话（证据与人读摘要共用）。
FAULT_LABELS: dict[str, str] = {
    "none": "没有故障",
    "tool_missing": "工具不可用",
    "input_missing": "输入不在",
    "bench_unconfigured": "台架未配置",
    "build_failed": "构建失败",
    "flash_failed": "烧录失败",
    "flash_refused": "烧录被拒",
    "verify_failed": "身份核验失败",
    "identity_mismatch": "设备身份与声明不符",
    "identity_unavailable": "未核验（没拿到设备自报）",
    "transport_unavailable": "串口不可用",
    "no_output_timeout": "超时且零输出",
    "timeout": "超时",
    "fuse_open": "熔断（同一故障连续两次）",
    "unknown_failure": "未识别的失败",
}

#: 需要**人工处置**那一类故障（工具坏了 / 探针卡住 / 台架没配）。
#: ⚠️ 熔断对**所有**故障码生效（需求 §3 的口径：同一目标 + 同一故障码连续两次）；
#: 这张表只用来决定"把哪一条处置建议挂在拒绝的那句话上" —— 构建失败的处置是改代码，
#: 不是拔插探针，两者不该混。
HARDWARE_FAULTS: tuple[str, ...] = (
    "tool_missing", "bench_unconfigured", "transport_unavailable",
    "no_output_timeout", "flash_failed", "unknown_failure",
)

#: 判别「同一件事被写了两遍」的形状：按**步骤名**选判据的写法。
#: ⚠️ 它就是那次错法的原形（`record()` 里 `if step == "build": ok = <另一套工具的口径>`）。
#: `hwops_check` 拿它当机器检查的一项；这里给出唯一一份正则。
STEP_NAME_BRANCH = (
    r"""(?:step|name)\s*==\s*['"]"""
    r"""(?:preflight|build|flash|verify|debug-check)['"]"""
)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _reason_or_problem(result: str, reason: str, what: str) -> str:
    """四态里除了 `supported`，其余三态都**必须**带理由。"""
    if result == "supported":
        return ""
    if not _nonempty(reason):
        return f"{what} 的结论是 {result}，但没有写理由（{result} 必须说明为什么）"
    return ""


# ============================================================== 成功判据（数据）
SUCCESS_KINDS: tuple[str, ...] = ("exit", "log")


@dataclass(frozen=True)
class SuccessCriterion:
    """一次步骤的**成功判据**（纯数据）。

    - `kind="exit"`：退出码为 0，且声明的产物都出现了；
    - `kind="log"`：还要在输出里找证据 —— 有些工具即便没做成也返回 0
      （实测：写 Flash 失败只打 Warning 而退出码仍是 0），所以必须数日志。

    `error_count_pattern` 是"从输出里数一个数、必须为 0"的通用形状
    （例：工具自己打的 `Total number of errors: N`）。**数不到统计行时不许当成功**：
    `require_count=True` 表示"这条输出里必须有这个统计行"，数不到就是"无法判定"。
    """

    kind: str = "exit"
    success_markers: tuple[str, ...] = ()
    failure_markers: tuple[str, ...] = ()
    error_count_pattern: str = ""
    require_count: bool = False
    min_artifacts: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SUCCESS_KINDS:
            raise ValueError(
                f"不认识的判据种类 {self.kind!r}：可用的是 {list(SUCCESS_KINDS)}"
            )
        if self.require_count and not self.error_count_pattern:
            raise ValueError(
                "require_count=True 却没有 error_count_pattern："
                "那就没有任何东西可数，这条判据是空的"
            )
        if self.kind == "exit" and (self.success_markers or self.error_count_pattern):
            raise ValueError(
                "kind=exit 的判据不该带日志标记：它只看退出码与产物"
                "（要看日志就写 kind=log —— 判据的种类必须与它的形状一致）"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "success_markers": list(self.success_markers),
            "failure_markers": list(self.failure_markers),
            "error_count_pattern": self.error_count_pattern,
            "require_count": self.require_count,
            "min_artifacts": self.min_artifacts,
            "note": self.note,
        }


@dataclass(frozen=True)
class Verdict:
    """一个步骤的判定结果（纯函数产出，可复核）。"""

    status: str
    fault: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "fault": self.fault, "detail": self.detail}


def judge(
    criterion: SuccessCriterion | None,
    *,
    returncode: int | None = None,
    output: str = "",
    missing_artifacts: Iterable[str] = (),
    artifacts_total: int = 0,
) -> Verdict:
    """按声明的判据判这一次结果（**只有这一处**判"算不算成功"）。

    ⚠️ 这就是那次的修法：判据从声明进来，而不是由调用方按步骤名去挑。
    调用方只负责把**事实**（退出码、输出、产物在不在）交进来。
    """
    missing = [str(item) for item in missing_artifacts]
    if criterion is None:
        return Verdict("fail", "unknown_failure", "这个步骤没有声明成功判据，无法判定")

    text = str(output or "")
    failures = [marker for marker in criterion.failure_markers if marker and marker in text]
    if failures:
        return Verdict(
            "fail", "", "输出里出现了失败标记：" + "、".join(failures)
        )

    if criterion.kind == "log":
        if criterion.require_count:
            import re as _re

            found = _re.findall(criterion.error_count_pattern, text)
            if not found:
                return Verdict(
                    "fail", "unknown_failure",
                    "输出里没有那条统计行，没办法判定结果（数不到 ≠ 没事）",
                )
            bad = [value for value in found if str(value).strip() not in ("0", "0.0")]
            if bad:
                return Verdict(
                    "fail", "", f"统计行报出非零：{bad[0]}"
                )
        if criterion.success_markers:
            absent = [marker for marker in criterion.success_markers if marker not in text]
            if absent:
                return Verdict(
                    "fail", "unknown_failure",
                    "输出里缺少完成标志：" + "、".join(absent),
                )

    if returncode is None:
        return Verdict("fail", "unknown_failure", "这条命令没有返回退出码，无法判定")
    if int(returncode) != 0:
        return Verdict("fail", "", f"退出码 {returncode}")

    if missing:
        return Verdict("fail", "", "声明的产物没有出现：" + "、".join(missing))
    if artifacts_total and artifacts_total < int(criterion.min_artifacts):
        return Verdict(
            "fail", "unknown_failure",
            f"只核到 {artifacts_total} 份产物，少于声明的 {criterion.min_artifacts} 份",
        )
    return Verdict("pass", FAULT_NONE, "按声明的判据通过" + (f"（{criterion.note}）" if criterion.note else ""))


# ============================================================== 身份核验
#: 身份核验的四种结论。与 DEVICE-001 / D4 立下的口径一致：
#: 「问不了」与「说得不一样」必须分开，而且**都没问过**不许写成"核验通过"。
IDENTITY_STATUSES: tuple[str, ...] = ("pass", "fail", "skipped", "unsupported")


@dataclass(frozen=True)
class IdentitySource:
    """期望值**来自本次构建的哪一份产物**（声明，不是代码）。

    ⚠️ 需求 §3 那句“烧录成功同时满足厂工具判据与设备实际身份核验”要真的成立，
    期望值就不能是写死的常量：写死了只能证明“设备报得出某个值”，证不了
    **这次烧的就是这次编的**。这里的四个格子说清那件事：去哪个产物里、取哪个键、
    写成什么形状、对应哪个身份字段。

    `template` 是**数据化的格式**（`"0x{value:08x}"`）：怎么把产物里的整数写成
    设备会报的形态，是目标层的事实，不该在 core 里写 `if key == "build_id32"`。
    """

    artifact: str
    key: str
    field: str
    template: str = "{value}"

    def __post_init__(self) -> None:
        for name, value in (("artifact", self.artifact), ("key", self.key),
                            ("field", self.field), ("template", self.template)):
            if not _nonempty(str(value)):
                raise ValueError(f"身份期望值的来源缺了 {name}")
        if "{value" not in self.template:
            raise ValueError("template 里必须有 {value}（否则它读不出那个键）")

    def render(self, raw: Any) -> str:
        return str(self.template).format(value=raw)

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "key": self.key,
            "field": self.field,
            "template": self.template,
        }


@dataclass(frozen=True)
class IdentityExpectation:
    """烧录后要拿设备自报核对的**字段与期望值**。

    比对哪些字段由声明说（core 不发明字段名），期望值可以**在构建之后**才补上
    （`with_values()`）—— 身份头/产物哈希这类值当时才知道。
    """

    fields: tuple[str, ...] = ()
    values: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    #: 期望值从哪儿来（留空 = 只在 `values` 里给）。
    source: IdentitySource | None = None
    #: 声明了来源、但这一次**没读到**的字段（由此判“未比”而不是“没声明期望值 ”）。
    unresolved: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "fields": list(self.fields),
            "values": {key: str(value) for key, value in self.values.items()},
            "note": self.note,
            "source": None if self.source is None else self.source.to_json(),
            "unresolved": list(self.unresolved),
        }


def judge_identity(
    expectation: IdentityExpectation | None,
    observed: Mapping[str, Any] | None,
) -> Verdict:
    """拿设备自报核对身份（**四种结论分开**，绝不把"没得核对"写成通过）。"""
    if expectation is None or not expectation.fields:
        return Verdict(
            "unsupported", "identity_unavailable",
            "这条接入契约没有声明要比对的身份字段 ⇒ 平台核验不了（这不是核验通过）",
        )
    if not observed:
        return Verdict(
            "skipped", "identity_unavailable",
            "没有拿到设备自报 ⇒ 未核验（不判失败，也不冒充通过）",
        )
    compared = [name for name in expectation.fields if name in observed]
    if not compared:
        return Verdict(
            "skipped", "identity_unavailable",
            "拿到了回答，但里面没有声明要比对的字段 ⇒ 未核验",
        )
    disagreements = [
        f"{name}：期望 {expectation.values.get(name, '（未声明期望值）')!r}、"
        f"实测 {observed.get(name)!r}"
        for name in compared
        if name in expectation.values
        and str(observed.get(name)) != str(expectation.values[name])
    ]
    if disagreements:
        return Verdict("fail", "identity_mismatch", "；".join(disagreements))
    missing_values = [name for name in compared if name not in expectation.values]
    detail = "核到 " + "、".join(compared) + "（来源：设备自报）"
    if missing_values:
        detail += f"；{missing_values} 没有期望值，只证明设备报得出来"
        blind = [name for name in missing_values if name in expectation.unresolved]
        if blind:
            origin = expectation.source.artifact if expectation.source else "声明的来源"
            detail += f"（{blind} 的那一份本次没读到：{origin}）"
    return Verdict("pass", FAULT_NONE, detail)


def with_values(
    expectation: IdentityExpectation, values: Mapping[str, Any]
) -> IdentityExpectation:
    """给一份身份声明补上期望值（构建产物里的身份在**构建之后**才知道）。"""
    return with_resolved(expectation, values, expectation.unresolved)


def with_resolved(
    expectation: IdentityExpectation,
    values: Mapping[str, Any],
    unresolved: Iterable[str] = (),
) -> IdentityExpectation:
    """补期望值 + 记下“哪些字段的来源本次没读到”（两者一起换，别只换一半）。"""
    return IdentityExpectation(
        fields=tuple(expectation.fields),
        values=dict(values),
        note=expectation.note,
        source=expectation.source,
        unresolved=tuple(unresolved),
    )


# ============================================================== 故障知识（数据）
@dataclass(frozen=True)
class FaultMarker:
    """一条已知失败标记：它是哪一类故障、是什么意思。"""

    marker: str
    code: str
    meaning: str

    def __post_init__(self) -> None:
        if not _nonempty(self.marker):
            raise ValueError("失败标记不能为空")
        if self.code not in FAULT_CODES or self.code == FAULT_NONE:
            raise ValueError(f"不认识的故障码 {self.code!r}：可用的是 {list(FAULT_CODES)}")
        if not _nonempty(self.meaning):
            raise ValueError(f"标记 {self.marker!r} 没有写含义（那它就只是一串字）")

    def to_json(self) -> dict[str, Any]:
        return {"marker": self.marker, "code": self.code, "meaning": self.meaning}


@dataclass(frozen=True)
class FaultKnowledge:
    """一条接入契约的**故障知识**：标记表 + 恢复建议。

    ⚠️ 这张表存在的理由（实测过）：硬件卡住时报错**每次措辞都不一样**
    （实测四种），而处置**只有一条**。措辞看起来像新证据 ⇒ 人会一直在"收集证据"
    而不是执行已知处置。所以把"标记 → 含义 + 处置"做成数据，并且**挂在失败详情上**：
    看到标记的那一刻就拿到下一步，不需要回去翻文档。
    """

    markers: tuple[FaultMarker, ...] = ()
    recovery: tuple[str, ...] = ()
    unknown_advice: str = ""
    step_defaults: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for step, code in self.step_defaults:
            if step not in DECLARED_STEPS:
                raise ValueError(f"step_defaults 里的步骤名 {step!r} 不认识")
            if code not in FAULT_CODES or code == FAULT_NONE:
                raise ValueError(f"step_defaults 里的故障码 {code!r} 不认识")

    def markers_for(self, code: str) -> tuple[str, ...]:
        return tuple(row.marker for row in self.markers if row.code == code)

    def default_for(self, step: str) -> str:
        for name, code in self.step_defaults:
            if name == step:
                return code
        return "unknown_failure"

    def classify(self, text: str) -> FaultMarker | None:
        """输出里第一条命中的已知标记（`None` = 一个都不认得）。"""
        haystack = str(text or "")
        for row in self.markers:
            if row.marker in haystack:
                return row
        return None

    def explain(self, text: str) -> str:
        """失败详情后面该挂的那句话（认不出标记时**不编原因**，只给通用建议）。"""
        if not _nonempty(str(text or "")):
            return ""
        hit = self.classify(text)
        if hit is None:
            return self.unknown_advice
        lines = [f"{hit.marker}（{hit.meaning}）"]
        lines.extend(self.recovery)
        return "；".join(item for item in lines if item)

    def to_json(self) -> dict[str, Any]:
        return {
            "markers": [row.to_json() for row in self.markers],
            "recovery": list(self.recovery),
            "unknown_advice": self.unknown_advice,
            "step_defaults": {step: code for step, code in self.step_defaults},
        }


# ================================================================== 能力四态
@dataclass(frozen=True)
class CapabilityResult:
    name: str
    result: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not _nonempty(self.name):
            raise ValueError("能力名不能为空")
        if self.result not in CAPABILITY_RESULTS:
            raise ValueError(
                f"不认识的结论 {self.result!r}：可用的是 {list(CAPABILITY_RESULTS)}"
            )
        problem = _reason_or_problem(self.result, self.reason, f"能力 {self.name}")
        if problem:
            raise ValueError(problem)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "result": self.result, "reason": self.reason}


# ================================================================== 步骤声明
#: 参数里的占位符：**只有这几个**，各自只有一个含义（与工程既有口径一致）。
PLACEHOLDERS: tuple[str, ...] = ("{project}", "{config}", "{port}", "{tools}")


@dataclass(frozen=True)
class StepSpec:
    """一个受控步骤的**声明**：用哪个适配器、跑什么、拿什么判成功。

    `args` 是参数数组（不拼字符串），元素里可以用占位符；`tool` 是**工具键**
    （`{tool}` 由装配处解析成可执行文件）—— 核心不认识任何工具装在哪（能力边界）。
    """

    step: str
    adapter_id: str
    label: str
    tool: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    timeout_s: float = 600.0
    attach: bool = False
    success: SuccessCriterion | None = None
    inputs: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    #: `True` = 这一条的 argv **由目标层的适配器给**（不是模板）：像 `.xcl` 参数文件
    #: 这种要按本次产物算路径的命令，只能由那套工具链算。声明里仍要写清它是什么命令。
    prepared: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.step not in DECLARED_STEPS:
            raise ValueError(
                f"不认识的步骤 {self.step!r}：声明里可用的是 {list(DECLARED_STEPS)}"
                "（preflight 由编排合成，不用声明）"
            )
        if not _nonempty(self.adapter_id):
            raise ValueError(f"{self.step} 没有声明用哪个适配器")
        if not _nonempty(self.label):
            raise ValueError(f"{self.step} 没有写清这是做什么（label）")
        if not self.args and not self.prepared:
            raise ValueError(f"{self.step} 没有声明参数（args）")
        if self.prepared and not any(_nonempty(item) for item in self.notes):
            raise ValueError(
                f"{self.step} 声明了 prepared（argv 由目标层给），那就要在 notes 里"
                "写清楚它到底跑的是什么"
            )
        if self.step != "debug-check" and self.success is None:
            raise ValueError(
                f"{self.step} 没有声明成功判据："
                "只看退出码不算判据（有些工具失败时也返回 0）"
            )
        if self.step == "build" and not self.artifacts:
            raise ValueError(
                "build 必须声明产物：构建完连产物都不核，就无法判定成功"
            )
        for text in (*self.args, *self.inputs, *self.artifacts, self.cwd):
            unknown = [
                word for word in _placeholder_words(text)
                if word not in PLACEHOLDERS
            ]
            if unknown:
                raise ValueError(
                    f"{self.step} 的 {text!r} 里有不认识的占位符 {unknown}："
                    f"可用的只有 {list(PLACEHOLDERS)}"
                )

    @property
    def argv_shape(self) -> tuple[str, ...]:
        return (self.tool or "", *self.args) if self.tool else tuple(self.args)

    def to_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "adapter_id": self.adapter_id,
            "label": self.label,
            "tool": self.tool,
            "args": list(self.args),
            "cwd": self.cwd,
            "timeout_s": self.timeout_s,
            "attach": self.attach,
            "prepared": self.prepared,
            "success": self.success.to_json() if self.success else None,
            "inputs": list(self.inputs),
            "artifacts": list(self.artifacts),
            "requires": list(self.requires),
            "notes": list(self.notes),
        }


def _placeholder_words(text: str) -> list[str]:
    """`{...}` 形状的片段（**只认花括号包的**：路径里的 `{}` 极少见，写错要当场报）。"""
    out: list[str] = []
    depth = 0
    start = -1
    for index, char in enumerate(str(text)):
        if char == "{":
            depth += 1
            if depth == 1:
                start = index
        elif char == "}":
            if depth == 1 and start >= 0:
                out.append(str(text)[start: index + 1])
            depth = max(0, depth - 1)
    return out


# ============================================================ 传输 / 身份声明
@dataclass(frozen=True)
class TransportHint:
    """通信入口的声明（**只声明**，打开由装配处交进来的 transport 实现做）。"""

    kind: str = "serial"
    baud: int = 0
    line_ending: str = ""
    read_only: bool = True
    adapter_id: str = ""
    minimal_check: str = ""
    notes: tuple[str, ...] = ()

    def problems(self) -> list[str]:
        out: list[str] = []
        if not _nonempty(self.kind):
            out.append("transport.kind 不能为空")
        if not _nonempty(self.adapter_id):
            out.append("transport 没有声明它用哪个适配器（adapter_id）")
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "baud": self.baud,
            "line_ending": self.line_ending,
            "read_only": self.read_only,
            "adapter_id": self.adapter_id,
            "minimal_check": self.minimal_check,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class IdentityHint:
    """身份入口的声明：怎么问、回答里哪些字段算身份。"""

    query: str = ""
    fields: tuple[str, ...] = ()
    banner_markers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "fields": list(self.fields),
            "banner_markers": list(self.banner_markers),
            "notes": list(self.notes),
        }


# =============================================================== 接入契约本体
@dataclass(frozen=True)
class HardwareDeclaration:
    """**一条硬件接入契约**：这个目标/固件声明的一切（需求 §2）。

    它是"接入一条新硬件要做完哪些事"的**完整清单**，也是机器检查与脚手架的对象：
    缺什么由 `problems()` 一次列全（不部分采用）。
    """

    adapter_id: str
    target_id: str
    label: str
    layer: str
    module: str
    project_entry: str = ""
    build_config: str = ""
    bench_keys: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    steps: tuple[StepSpec, ...] = ()
    faults: FaultKnowledge = field(default_factory=FaultKnowledge)
    capabilities: tuple[CapabilityResult, ...] = ()
    transport: TransportHint = field(default_factory=TransportHint)
    identity: IdentityHint = field(default_factory=IdentityHint)
    identity_expectation: IdentityExpectation | None = None
    excluded: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    contract_version: int = CONTRACT_VERSION

    # ------------------------------------------------------------------ 查询
    def step(self, name: str) -> StepSpec | None:
        for row in self.steps:
            if row.step == name:
                return row
        return None

    def capability(self, name: str) -> CapabilityResult | None:
        for row in self.capabilities:
            if row.name == name:
                return row
        return None

    def declared_steps(self) -> tuple[str, ...]:
        return tuple(row.step for row in self.steps)

    # -------------------------------------------------------------- 自身一致性
    def problems(self) -> list[str]:
        """这条契约站不站得住（**空 = 站得住**）。"""
        out: list[str] = []
        if int(self.contract_version) != CONTRACT_VERSION:
            out.append(
                f"契约版本 {self.contract_version} 不是本平台认的 {CONTRACT_VERSION}："
                "要么按迁移说明改声明，要么先升平台的契约版本"
            )
        for name, value in (
            ("adapter_id", self.adapter_id),
            ("target_id", self.target_id),
            ("label", self.label),
            ("layer", self.layer),
            ("module", self.module),
        ):
            if not _nonempty(value):
                out.append(f"没有声明 {name}")
        if not self.project_entry:
            out.append("没有声明工程入口（project_entry）：换台机器就找不到工程")
        if not self.tools and any(row.tool for row in self.steps):
            out.append("步骤里用了工具键，但 `tools` 里一个都没有声明")
        seen: set[str] = set()
        for row in self.steps:
            if row.step in seen:
                out.append(f"{row.step} 声明了两次（一个步骤只能有一套判据）")
            seen.add(row.step)
            missing = [name for name in row.requires if self.capability(name) is None]
            if missing:
                out.append(
                    f"{row.step} 要求的能力没有声明：{missing}"
                    "（要求什么就必须先声明它是什么状态）"
                )
            if row.tool and row.tool not in self.tools:
                out.append(f"{row.step} 用的工具键 {row.tool!r} 不在 tools 里")
        out.extend(self.transport.problems())
        for row in self.capabilities:
            if row.result != "supported" and not row.reason:
                out.append(f"能力 {row.name} 是 {row.result} 却没有理由")
        if self.identity_expectation is not None and self.identity_expectation.fields:
            for name in self.identity_expectation.fields:
                if self.identity.fields and name not in self.identity.fields:
                    out.append(
                        f"身份核验要比对字段 {name!r}，但 identity.fields 里没有它"
                        "（比对什么必须在身份声明里出现过）"
                    )
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_id": self.adapter_id,
            "target_id": self.target_id,
            "label": self.label,
            "layer": self.layer,
            "module": self.module,
            "project_entry": self.project_entry,
            "build_config": self.build_config,
            "bench_keys": list(self.bench_keys),
            "tools": list(self.tools),
            "steps": [row.to_json() for row in self.steps],
            "faults": self.faults.to_json(),
            "capabilities": [row.to_json() for row in self.capabilities],
            "transport": self.transport.to_json(),
            "identity": self.identity.to_json(),
            "identity_expectation": (
                self.identity_expectation.to_json() if self.identity_expectation else None
            ),
            "excluded": list(self.excluded),
            "notes": list(self.notes),
            "problems": self.problems(),
        }


# ================================================================ 装配形状
@dataclass(frozen=True)
class PreparedCommand:
    """目标层**已经准备好**的一条命令（argv 由那套适配器给，不在这里拼）。

    ⚠️ 为什么需要它：有些工具的 argv 不是一个模板（要用 `.xcl` 参数文件、要按本次
    产物算路径）。那是**那套工具链**的知识，只能由目标层调自己的适配器拿；编排拿到
    的就是这里的东西 —— 于是同一条适配器的 argv 只有一个来源。
    """

    argv: tuple[str, ...]
    cwd: str = ""
    timeout_s: float = 0.0
    attach: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_s": self.timeout_s,
            "attach": self.attach,
        }


@dataclass(frozen=True)
class AdapterWiring:
    """一条接入契约 + **这一趟要用到的本机值**（装配处交进统一入口的那一份）。

    声明（`HardwareDeclaration`）是随代码走的；这里的东西是随机器走的：工程根在哪、
    工具解析成哪个可执行文件、这一趟用哪个通信口、台架键配了没有。两者分开，
    "开源内容"与"本机事实"就不会混在同一份文件里。
    """

    declaration: HardwareDeclaration
    project_dir: str = ""
    tools: Mapping[str, str] = field(default_factory=dict)
    port: str = ""
    bench_values: Mapping[str, tuple[bool, str]] = field(default_factory=dict)
    #: 这个目标登记的适配器 ID（旁路检查要对着它比）。
    registered: tuple[str, ...] = ()
    #: 目标层**已经准备好**的命令（`adapter_id -> PreparedCommand`）：有它就用它，
    #: 没有则按声明里的工具键与参数展开。
    commands: Mapping[str, PreparedCommand] = field(default_factory=dict)
    #: 串口实现（`(端口, 波特率) -> 句柄`）：**只读打开**这件事也是目标层的事
    #: （core 不认识任何串口库）。没交进来 ⇒ debug-check 如实报 unsupported。
    port_factory: Any = field(default=None, compare=False, repr=False)
    #: 身份回答的解析器（`line -> dict | None`）：**回答的格式是那块板的事实**，
    #: 所以由目标层交进来，core 只拿它给出的字段去核对。
    identity_parser: Any = field(default=None, compare=False, repr=False)
    notes: tuple[str, ...] = ()

    @property
    def adapter_id(self) -> str:
        return self.declaration.adapter_id

    def problems(self) -> list[str]:
        out = list(self.declaration.problems())
        if not self.project_dir:
            out.append("没有交出工程根（project_dir）：这一趟没法展开路径")
        for key in self.declaration.tools:
            if key not in self.tools and not self.commands:
                out.append(f"工具键 {key!r} 没有交出可执行文件")
        if not self.registered:
            out.append("没有交出适配器登记表：旁路检查判不了")
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "project_dir": self.project_dir,
            "tools": dict(sorted(self.tools.items())),
            "port": self.port,
            "bench_values": {
                key: {"configured": bool(value[0]), "detail": str(value[1])}
                for key, value in sorted(self.bench_values.items())
            },
            "registered": list(self.registered),
            "commands": {
                key: command.to_json() for key, command in sorted(self.commands.items())
            },
            "port_factory": self.port_factory is not None,
            "identity_parser": self.identity_parser is not None,
            "notes": list(self.notes),
            "problems": self.problems(),
        }


def capsule(declaration: HardwareDeclaration, **changes: Any) -> HardwareDeclaration:
    """改几个字段就得到一条新契约（**不复制一份构造**）。"""
    return replace(declaration, **changes)


def missing_steps(declaration: HardwareDeclaration) -> tuple[str, ...]:
    """这条契约**没有覆盖**的步骤（`excluded` 里写明理由的不算缺）。"""
    declared = set(declaration.declared_steps())
    excused = " ".join(declaration.excluded)
    return tuple(
        step for step in DECLARED_STEPS
        if step not in declared and step not in excused
    )


def summary_of(declaration: HardwareDeclaration) -> str:
    """一句话自述（人读摘要与命令行 `list` 共用）。"""
    steps = "、".join(declaration.declared_steps()) or "（没有声明任何步骤）"
    caps = "，".join(
        f"{row.name}={row.result}" for row in declaration.capabilities
    ) or "（没有声明能力）"
    return (
        f"{declaration.adapter_id}（{declaration.label}）：步骤 {steps}；能力 {caps}"
    )

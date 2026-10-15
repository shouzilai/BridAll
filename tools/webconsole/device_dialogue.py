"""与设备的**固定对话**（**core**， / R4）：形状在这里，内容在项目包里。

自动验证的采集窗口里，主机常常要跟设备做一个**固定的问答**：等它把启动横幅打完
（在那之前发什么都会被静默丢弃）、按需要清一次记录文件、对一次时、窗口末尾把记录文件取回来。
这些事**不是**平台的知识：`CLEAR YES` 是那块固件的命令、`已清空` 是它的回答、
记录周期是**那个项目**的取样节奏。它们以前写在编排里（`pipeline._step_observe`），
于是"换一个项目"就变成"改编排"（）。

这一层把对话变成**数据**（与 `target_adapters.ResetPlan` 同一个做法）：

```text
DeviceDialogue
  ├─ source            谁声明的（进事件流，读证据的人能查"这些话是谁教它说的"）
  ├─ note              一句人话说明
  ├─ banner_marker     等设备自己说出这个明文标记（空 = 不等）
  ├─ banner_timeout_s  等它的上限
  └─ exchanges         一串 DeviceExchange
        ├─ send            要发的那句话（模板：`{clock:%Y-%m-%d %H:%M:%S}`）
        ├─ gate            只有本轮这个开关为真才做（封闭集合，写错当场报错）
        ├─ wait_for/…      等设备回这个标记 / 等多久
        ├─ notice/…        发出去之后往事件流里写的那句话
        ├─ phase           窗口开始（open）还是窗口结束前（close）
        └─ record_clock    这句话是不是"对时"（发出时刻要进 manifest）
```

四条规矩（都有宿主回归钉着）：

1. **编排不认识任何一句具体的话**：它只渲染模板、按 `gate` 决定做不做、把 `notice`
   写进事件流。所以编排里不该出现 `CLEAR YES` / `DUMP BIN` / `已清空` 这种字。
2. **没声明对话就是"不对设备说任何话"**（`silent_dialogue()`）——只读采集，不猜一套默认对话。
   没有对话的设备（裸机 / RT-Thread）就该走这一条，而不是借用参考项目的命令。
3. **`gate` 与 `phase` 是封闭集合**：写错名字当场 `ValueError`，不静默当"不做"。
   静默跳过一次对时的后果是"整轮存储证据什么都没证明，而设备看上去一切正常"。
4. **只描述，不执行**：这里不 import 任何目标/项目模块、不开串口、不写证据。
   （只依赖同层 core 的 `step_lifecycle`，用它的 `check_stage_id` 校验阶段 id 的形状。）

⚠️ 模板占位符也是封闭集合：`send` 里只允许 `{clock:格式}`，`notice` 里只允许 `{sent}`，
`miss_notice` 里只允许 `{wait_seconds:格式}`。多一个认不出来的占位符会在**构造时**报错，
而不是渲染成一句带着花括号的乱码发到串口上。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Mapping

try:  # imported as ``webconsole.device_dialogue``
    from . import step_lifecycle
    from .step_lifecycle import TestStepError
except ImportError:  # imported as the top-level package
    import step_lifecycle  # type: ignore[no-redef]
    from step_lifecycle import TestStepError  # type: ignore[no-redef]

#: 一次交互出现在窗口的哪一段。`open` = 开始就做；`close` = 窗口结束**前**做
#: （取记录文件这类事要留出回包的时间）。
CONVERSATION_PHASES = ("open", "close")

#: 一条交互最多允许重发几次。**通用策略的硬边界**由 core 定（AGENT_WORKFLOW §5：
#: "遇到硬件断线、端口消失或供电异常时停止自动重试风暴"）；具体重试几次由项目包声明。
MAX_RETRY_PER_EXCHANGE = 3

#: 对话可以引用的 run 开关（**封闭集合**，与 `Pipeline.start()` 的实参同名）。
#: 不在这里的名字会让整份对话构造失败 —— 那是"写错了"，不是"这次不做"。
#: 对话里的开关（`DeviceExchange.gate` 只能取这几个；`run["开关"]` 为真才做）。
#: `run_sequence` 是**通用**名字：某个套件要给设备发一串固定命令（受控序列/自检）时声明它 ——
#: 具体发什么由**项目包**的对话说，core 不认识那些命令。
CONVERSATION_GATES = ("clear_records", "set_clock", "dump_records", "run_sequence")

#: 每种字段允许的模板占位符（**封闭集合**）。
_ALLOWED_TEMPLATES: dict[str, tuple[str, ...]] = {
    "send": ("clock",),
    "notice": ("sent",),
    "miss_notice": ("wait_seconds",),
}

_TEMPLATE = re.compile(r"\{(?P<name>[a-zA-Z_]+)(?::(?P<arg>[^}]*))?\}")

#: `{clock}` 不带格式时的默认格式（与人工对时的习惯一致：本地时间的刻）。
DEFAULT_CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"


class DialogueSyntaxError(ValueError):
    """这份对话写错了（占位符/动作/开关名不在封闭集合里）—— 构造时就要报出来。"""


def _validate_template(text: str, field_name: str) -> None:
    allowed = _ALLOWED_TEMPLATES[field_name]
    for match in _TEMPLATE.finditer(text):
        name = match.group("name")
        if name not in allowed:
            raise DialogueSyntaxError(
                f"{field_name} 里有认不出来的占位符 {{{name}}}（允许的是 "
                f"{list(allowed)}）：{text!r}"
            )


def render_text(template: str, *, values: Mapping[str, Any] | None = None) -> str:
    """把模板渲染成人话。

    - `{clock:FMT}` ⇒ 主机的**当前**本地时刻（`time.strftime(FMT)`）；
    - 其余名字从 `values` 取（`{sent}` = 刚发出去的那句话；`{wait_seconds:.0f}` = 等的上限）。

    ⚠️ 认不出来的占位符会抛 `DialogueSyntaxError`：宁可这一次 run 明确失败，
    也不要把 `{clock}` 原样发到串口上（那会被设备当成一条奇怪命令，而证据看起来"命令发过了"）。
    """
    table = dict(values or {})

    def substitute(match: re.Match[str]) -> str:
        name = match.group("name")
        arg = match.group("arg")
        if name == "clock":
            return time.strftime(arg or DEFAULT_CLOCK_FORMAT)
        if name not in table:
            raise DialogueSyntaxError(
                f"渲染对话时遇到没有值的占位符 {{{name}}}：{template!r}"
            )
        value = table[name]
        if arg:
            return format(value, arg)
        return str(value)

    return _TEMPLATE.sub(substitute, template)


@dataclass(frozen=True)
class DeviceExchange:
    """**一次**发给设备的话（外加"等它回什么"与"这件事怎么说给读证据的人听"）。"""

    #: 这是干什么的（人话，进事件流与文档；也是"编排里没有项目字眼"的替代说明）。
    purpose: str
    #: 要发的文本（模板见模块说明）。空 = 不发（那种场景用 `banner_marker` 表达）。
    send: str = ""
    #: 发出去之后写进事件流的话（可选；`{sent}` = 刚发出去的那句话）。
    notice: str = ""
    #: 等不到 `wait_for` 时写进事件流的话（可选；`{wait_seconds:.0f}` = 等的上限）。
    miss_notice: str = ""
    #: 只有本轮这个开关为真才做（见 `CONVERSATION_GATES`）；空 = 每次都做。
    gate: str = ""
    #: 等设备回答里出现这个标记（空 = 发完就走，不等）。
    wait_for: str = ""
    #: 等 `wait_for` 的上限（秒）。
    wait_seconds: float = 0.0
    #: 发之前额外等一会儿（例如两条命令之间要错开）。
    delay_seconds: float = 0.0
    #: `phase="close"` 时：窗口结束**前**多少秒做这件事（留出回包的时间）。
    before_end_seconds: float = 0.0
    #: 出现在窗口开始（`open`）还是结束前（`close`）。
    phase: str = "open"
    #: 这一句就是"对时"：发出去的时刻要记进 run manifest（断言拿它和设备回报比对）。
    record_clock: bool = False
    #: 它会**改设备上的数据**（清记录这类）—— 这类动作必须挂在某个开关上，
    #: 而且读证据的人要能一眼看出"这一轮动过设备的数据"。
    mutates_device: bool = False
    #: 它在自动化测试里的**阶段 id**（空 = 它只是采集期间的对话，不是被测阶段）。
    #: 形状由 `step_lifecycle.check_stage_id` 管：跨版本稳定，页面与证据按它对位
    #: （：卡片是按它铺开、按它收敛的）。
    stage_id: str = ""
    #: **只读命令的应答重试**：最多重发几次（0 = 不重发）。
    #: 为什么要它（）：偶发残帧会让一次只读查询"没等到回话"，而命令本身幂等 ——
    #: 重发一次就能拿到完整响应，于是"业务"不必替"日志健康"背锅。
    #: ⚠️ 只对**只读、幂等**的命令开放：重试策略由项目包声明，执行由 core 做。
    retry_max: int = 0
    #: 重发前的短间隔（秒）—— 设备在吐日志时连着发命令会丢字节，要错开。
    retry_gap_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.purpose:
            raise DialogueSyntaxError("每次交互都要写清 purpose（事件流里要能读懂它是干什么的）")
        if not self.send:
            raise DialogueSyntaxError(
                f"{self.purpose}：要发的文本为空 —— '只等不发'由 DeviceDialogue.banner_marker 表达"
            )
        if self.phase not in CONVERSATION_PHASES:
            raise DialogueSyntaxError(
                f"{self.purpose}：不认识的 phase {self.phase!r}（可用的是 {list(CONVERSATION_PHASES)}）"
            )
        if self.gate and self.gate not in CONVERSATION_GATES:
            raise DialogueSyntaxError(
                f"{self.purpose}：不认识的开关 {self.gate!r}（可用的是 {list(CONVERSATION_GATES)}）"
            )
        if self.wait_for and self.wait_seconds <= 0:
            raise DialogueSyntaxError(
                f"{self.purpose}：要等设备回话就必须给一个上限（wait_seconds）"
            )
        if self.wait_for and not self.miss_notice:
            raise DialogueSyntaxError(
                f"{self.purpose}：等不到设备回话时也要说一句（miss_notice）——"
                "静默地少做一步是最难发现的失败"
            )
        if self.phase == "close" and self.before_end_seconds <= 0:
            raise DialogueSyntaxError(
                f"{self.purpose}：close 阶段的交互必须写清在窗口结束前多少秒做"
            )
        if self.phase == "open" and self.before_end_seconds:
            raise DialogueSyntaxError(
                f"{self.purpose}：open 阶段不该有 before_end_seconds（那是 close 的事）"
            )
        if self.delay_seconds < 0 or self.wait_seconds < 0 or self.before_end_seconds < 0:
            raise DialogueSyntaxError(f"{self.purpose}：等待时间不能是负数")
        if self.record_clock and self.gate != "set_clock":
            raise DialogueSyntaxError(
                f"{self.purpose}：record_clock 这一句必须挂在 set_clock 开关上"
                "（否则没对时的 run 也会记一个时钟进去）"
            )
        if self.stage_id:
            # ⚠️ 阶段 id 进证据、进页面卡片，改名就等于换了一张卡 ——
            # 所以形状只在 `step_lifecycle` 里定义一次，这里照用（同层 core）。
            try:
                step_lifecycle.check_stage_id(self.stage_id)
            except TestStepError as error:
                raise DialogueSyntaxError(f"{self.purpose}：stage_id 不合法（{error}）") from error
        if self.retry_max or self.retry_gap_seconds:
            # ⚠️ 四条都在**声明时**报错，不静默包容：重试是"给只读查询补一次完整响应"，
            # 不是自动重试风暴，也不是给有副作用的命令开后门。
            if self.retry_max < 0 or self.retry_max > MAX_RETRY_PER_EXCHANGE:
                raise DialogueSyntaxError(
                    f"{self.purpose}：retry_max 只能在 0…{MAX_RETRY_PER_EXCHANGE} 之间"
                    "（重试是为偶发残帧补一次，不是自动重试风暴）"
                )
            if not self.wait_for:
                raise DialogueSyntaxError(
                    f"{self.purpose}：声明重试就必须声明等什么（wait_for）——"
                    "没有「等到了没有」的尺子，重试就无从判定"
                )
            if self.retry_gap_seconds <= 0:
                raise DialogueSyntaxError(
                    f"{self.purpose}：声明重试就必须给重发间隔（retry_gap_seconds）——"
                    "设备在吐日志时连着发命令会丢字节"
                )
            if self.mutates_device:
                raise DialogueSyntaxError(
                    f"{self.purpose}：**有副作用的命令不许自动重发**（重试只对只读、幂等的命令开放）"
                )
        for field_name in _ALLOWED_TEMPLATES:
            _validate_template(getattr(self, field_name), field_name)

    def to_json(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "send": self.send,
            "gate": self.gate,
            "wait_for": self.wait_for,
            "wait_seconds": self.wait_seconds,
            "delay_seconds": self.delay_seconds,
            "phase": self.phase,
            "before_end_seconds": self.before_end_seconds,
            "record_clock": self.record_clock,
            "mutates_device": self.mutates_device,
            "stage_id": self.stage_id,
            "retry_max": self.retry_max,
            "retry_gap_seconds": self.retry_gap_seconds,
        }


@dataclass(frozen=True)
class DeviceDialogue:
    """这个项目在采集窗口里跟设备说的**全部**话（没声明 = 什么都不说）。"""

    #: 谁声明的（进事件流与证据；例如 `project-package:示例项目`）。
    source: str
    #: 一句人话说明（进事件流）。
    note: str = ""
    #: 等设备自己说出这个明文标记（通常就是启动横幅的开头）。空 = 不等。
    banner_marker: str = ""
    #: 等 `banner_marker` 的上限（秒）。
    banner_timeout_s: float = 0.0
    #: 窗口里的交互（顺序就是执行顺序）。
    exchanges: tuple[DeviceExchange, ...] = ()

    def __post_init__(self) -> None:
        if not self.source:
            raise DialogueSyntaxError("对话必须说清是谁声明的（source）")
        if self.banner_marker and self.banner_timeout_s <= 0:
            raise DialogueSyntaxError("等启动横幅必须给一个上限（banner_timeout_s）")
        if self.banner_timeout_s and not self.banner_marker:
            raise DialogueSyntaxError("给了 banner_timeout_s 却没给 banner_marker：那是等不到的东西")
        if not self.exchanges:
            return
        for exchange in self.exchanges:
            if exchange.mutates_device and not exchange.gate:
                raise DialogueSyntaxError(
                    f"{exchange.purpose}：会改设备数据的动作必须挂在某个开关上（gate），"
                    "不能每次采集都偷偷做"
                )

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "note": self.note,
            "banner_marker": self.banner_marker,
            "banner_timeout_s": self.banner_timeout_s,
            "exchanges": [exchange.to_json() for exchange in self.exchanges],
        }


def silent_dialogue() -> DeviceDialogue:
    """**不对设备说任何话**（只读采集）。

    没有声明对话的项目（没有固定命令、没有记录文件、不吃对时）走这一条 ——
    而不是借用参考项目的那套命令。"没声明"和"声明了什么都不做"在这里是同一件事，
    但都**不是**"偷偷替它装一套默认对话"。
    """
    return DeviceDialogue(source="（未声明对话：只读采集）", note="不对设备说任何话")


def markers(dialogue: DeviceDialogue) -> tuple[str, ...]:
    """这份对话要等的所有标记（去重、保持顺序、去掉空串）。"""
    found: list[str] = []
    for text in (dialogue.banner_marker, *(item.wait_for for item in dialogue.exchanges)):
        if text and text not in found:
            found.append(text)
    return tuple(found)


def gate_is_open(exchange: DeviceExchange, flags: Mapping[str, Any]) -> bool:
    """这一轮要不要做这次交互（`gate` 为空 = 每次都做）。"""
    if not exchange.gate:
        return True
    return bool(flags.get(exchange.gate))


def open_exchanges(dialogue: DeviceDialogue) -> tuple[DeviceExchange, ...]:
    return tuple(item for item in dialogue.exchanges if item.phase == "open")


def close_exchanges(dialogue: DeviceDialogue) -> tuple[DeviceExchange, ...]:
    return tuple(item for item in dialogue.exchanges if item.phase == "close")


__all__ = [
    "CONVERSATION_GATES",
    "CONVERSATION_PHASES",
    "DEFAULT_CLOCK_FORMAT",
    "DeviceDialogue",
    "DeviceExchange",
    "DialogueSyntaxError",
    "MAX_RETRY_PER_EXCHANGE",
    "close_exchanges",
    "gate_is_open",
    "markers",
    "open_exchanges",
    "render_text",
    "silent_dialogue",
]

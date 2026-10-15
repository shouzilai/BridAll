"""受控的**目标适配器接口**（**core**，PCR-001 / R3a）。

编排（`pipeline`）只该做三件事：**什么时候**做、**把结果记成证据**、**失败时说什么**。
它不该知道"用哪个工具、加哪些参数、`.pio` 目录在哪、复位要动哪根线"——那些是**目标**的知识。

所以这里定义一组**数据形状**与**协议**：

```text
BuildRequest(pio_env, project_dir)          ├─→ CommandSpec(adapter_id, argv, cwd, purpose, timeout_s)
UploadRequest(pio_env, port, project_dir)   ├─→ CommandSpec(…)
ToolProbeRequest(project_dir)               ┘
BuildAdapter.default_env()                  出货固件用哪个构建环境（编排没指定时问它）ResetPlan(steps=(…))                        一行一个动作（数据，不是代码分支）
Discovery(paths=(…))                        构建产物 / 字典 / 分区表 / 工程文件在哪
SymbolPlan(kind, elf, addresses)            → SymbolRequest(adapter_id, argv, note)
```

**为什么强调"受控"**（DEC-003）：参数数组由**代码**生成，配置里进不来命令 ——
profile 里存的是适配器 **ID**（`platformio.build`），不是命令行。这样"换目标"是换实现，
不是换一段可被注入的字符串。

编排侧的执行顺序（也在这里写清楚，免得两边各自理解）：

1. 向适配器要 `CommandSpec`（它可能抛 `AdapterUnavailable`：工具不存在 / 参数不合法）；
2. 自己跑这条命令（流式输出进「工具输出」窗口与 `tool.log`）；
3. 判断结果（退出码 + 关键标记），把**人话结论**写进步骤说明与证据。

适配器**不许**自己开串口、不许自己写证据文件：它只提供 argv 与数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

#: 适配器 ID 的合法形状（与 `target_profile` 里的那份判据一致）。
ADAPTER_ID_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"


class AdapterUnavailable(RuntimeError):
    """这件工具在这台机器上不可用（找不到 / 版本不对 / 参数不成立）。

    编排把它当**一个状态**处理（`precheck` 报出来、run 判失败），而不是当成崩溃：
    "这台机器没装 PlatformIO" 不是代码错，是环境事实。
    """


@dataclass(frozen=True)
class CommandSpec:
    """一条**受控命令**：跑什么、在哪跑、为什么跑。

    ⚠️ 只有适配器能造出它（参数数组来自代码）；编排只负责执行与记账。
    `purpose` 是给人看的一句话（进工具窗口与证据索引），不是解析用的键。
    """

    adapter_id: str
    argv: tuple[str, ...]
    cwd: Path
    purpose: str
    timeout_s: float = 900.0
    #: 这条命令是不是**附加到目标**（探针/调试器正在目标上跑）。
    #: ⚠️ 它决定"取消与超时该怎么收场"：普通命令想 杀 就 杀，
    #: 而附加类命令**不能杀** —— 实测把握着 ST-LINK 的 C-SPY 强杀下去，探针要**拔插**才能恢复
    #: （2026-09-27 用户反馈"你操作后我再去试就得拔插"：这就是根因）。
    #: 所以这是**目标层声明**的一件事，编排只按它做事（不去猜命令里写了什么）。
    attach: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_id,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "purpose": self.purpose,
            "timeout_s": self.timeout_s,
            "attach": self.attach,
        }


@dataclass(frozen=True)
class ToolResult:
    """一条受控命令跑完之后，编排记下来的那点事实（编造不了）。"""

    adapter_id: str
    returncode: int
    seconds: float
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class BuildRequest:
    pio_env: str
    project_dir: Path


@dataclass(frozen=True)
class UploadRequest:
    pio_env: str
    port: str
    project_dir: Path


@dataclass(frozen=True)
class ToolProbeRequest:
    project_dir: Path


# ------------------------------------------------------------------ 复位时序
@dataclass(frozen=True)
class ResetStep:
    """复位时序里的**一步**（纯数据）。

    允许的动作只有这几种：``assign``（给某根调制线赋值）/ ``settle``（等一下）/
    ``flush_input``（丢掉已经读到的字节）/ ``tool``（这件事串口调制线做不到，由目标给一条
    **受控命令**去做 —— R4 的 ST-LINK 复位就是这个形状）。
    **不给**"跑一段代码"这种口子：``tool`` 带的是一条 ``CommandSpec``，参数仍由代码生成。
    """

    action: str
    line: str = ""        # assign 用：dtr / rts
    value: bool = False   # assign 用
    seconds: float = 0.0  # settle 用
    note: str = ""
    #: `action == "tool"` 时**必须**带的一条受控命令（没带 = 这份计划不成立）。
    spec: "CommandSpec | None" = None


@dataclass(frozen=True)
class ResetPlan:
    """复位时序（数据）：编排照着跑，目标决定跑什么。

    ⚠️ 这份表的形状本身是一份**教训**：赋 DTR 与赋 RTS
    各自都会经过板上的自动复位电路，挨着做会把第一次的启动横幅从中间截断。
    所以参考目标是"先只动 DTR → 等 → 丢掉 → 再单独脉冲 RTS"三步走。
    """

    adapter_id: str
    steps: tuple[ResetStep, ...] = field(default=())
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_id,
            "note": self.note,
            "steps": [
                {
                    "action": step.action,
                    "line": step.line,
                    "value": step.value,
                    "seconds": step.seconds,
                    "note": step.note,
                    # 只有真的带了命令时才多这一个键：参考目标的计划形状与 R3c 逐字相同。
                    **({"tool": step.spec.to_json()} if step.spec is not None else {}),
                }
                for step in self.steps
            ],
        }


# --------------------------------------------------------------- 发现（路径）
@dataclass(frozen=True)
class Discovery:
    """这台机器上"构建产物 / 字典 / 产物清单 / 存储布局 / 工程文件"在哪（一次问清，不留半份）。

    字段名刻意用**中性词**：core 层不知道参考目标是 ESP32、也不知道分区表叫
    `partitions_custom.csv`。`layout`（存储布局描述）在参考目标上就是那张
    分区表 CSV；换成第二目标（R4）时，这里一行不用改。
    """

    adapter_id: str
    project_ini: Path | None = None
    build_dir: Path | None = None
    dictionary: Path | None = None
    artifact_manifest: Path | None = None
    layout: Path | None = None
    tool: Path | None = None
    tool_python: Path | None = None
    #: 构建后**必须存在**的东西（路径, 人话用途）——"构建成功"的判据由**目标**给。
    #: R4 之前编排里写死的是参考工程的三样（字典、产物清单、`firmware.bin`），
    #: 换一个目标就一个都过不去。空元组 = 这个组合没有"构建后必须有什么"的要求。
    required_artifacts: tuple[tuple[Path, str], ...] = ()
    #: 这次构建的 **ELF**（崩溃回溯只能对着它解）。给不出就是 None —— 不猜路径。
    elf: Path | None = None
    #: 这次构建的**固件镜像**（要记进证据的那一个二进制）。
    binary: Path | None = None
    #: 产物尺寸行的正则（**目标工具**的输出格式，例如参考工具的 `RAM: … (used N bytes…)`）。
    #: None = 这个组合不记尺寸；core 只负责在有模式时照着扫。
    size_pattern: str | None = None
    #: 这个目标的**字典库**怎么找（返回一个 `exact` / `describe_available` / `entries_for` 的对象）。
    #: ⚠️ PCR-001 / R4：第二目标的字典不住在 PlatformIO 的构建树里
    #: （它住在 `<工程>/logging/<配置>/`），所以"去哪找字典"也是目标的知识。
    #: None = 退回参考目标的 `dictionary.discover()`。这是一个**回调**而不是路径：
    #: "一个 build id 对应哪一份字典"的规矩在那个库里，不该在编排里再写一遍。
    library: Callable[[], Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_id,
            **{
                name: (str(value) if value is not None else None)
                for name, value in (
                    ("project_ini", self.project_ini),
                    ("build_dir", self.build_dir),
                    ("dictionary", self.dictionary),
                    ("artifact_manifest", self.artifact_manifest),
                    ("layout", self.layout),
                    ("tool", self.tool),
                    ("tool_python", self.tool_python),
                    ("elf", self.elf),
                    ("binary", self.binary),
                )
            },
            "size_pattern": self.size_pattern,
            "library": self.library is not None,
            "required_artifacts": [
                {"path": str(path), "what": what} for path, what in self.required_artifacts
            ],
        }


# ------------------------------------------------------------------- 协议
@dataclass(frozen=True)
class AdapterRef:
    """一个受信适配器的**位置**：ID、实现在哪个模块的哪个符号、现在属于哪一层。

    R3a 里它是参考目标自己的东西（住在 `esp32_target`）；R4 引入第二目标之后必须共用
    —— 它是**形状**（"一个适配器住哪儿"），不是任何一块板的知识：两个目标各写一份，
    "登记表与实测一致"就会变成两套判据。

    `layer` 记的是**实现在哪一层**：实现落在 core 的适配器是一条债（core 里出现了
    具体目标的知识），`test_core_layer.py` 会盯着这张表。
    """

    adapter_id: str
    module: str        # MODULE_LAYERS 里的模块名
    symbol: str        # 模块里的符号（`Class.method` 或函数名）
    layer: str         # 实现在哪一层（core 是债，见 note）
    note: str


@runtime_checkable
class BuildAdapter(Protocol):
    """构建这一角色（`pio_env` 是**构建环境/配置名**的历史名字：它进证据键，改名要单独一片）。

    `archive` 是 R4 加的：把这次构建的产物存起来（归档目录名、产物名与布局全是**那套工具链**
    的知识）。没有"按构建号存档"这套东西的目标就**如实返回 `None`** —— 证据里于是没有
    `archive` 这一项，而不是一条假路径。
    """

    adapter_id: str

    def default_env(self) -> str: ...

    def build(self, request: BuildRequest) -> CommandSpec: ...

    def probe(self, request: ToolProbeRequest) -> CommandSpec: ...

    def archive(self, discovery: Discovery, firmware: dict, env: str) -> dict | None: ...


@runtime_checkable
class FlashAdapter(Protocol):
    adapter_id: str

    def upload(self, request: UploadRequest) -> CommandSpec: ...


@runtime_checkable
class ResetAdapter(Protocol):
    adapter_id: str

    def plan(self) -> ResetPlan: ...


@runtime_checkable
class SymbolAdapter(Protocol):
    adapter_id: str

    def locate(self) -> Path | None: ...

    def symbolize(self, elf: Path, addresses: list[str]) -> CommandSpec: ...


@runtime_checkable
class DiscoveryAdapter(Protocol):
    adapter_id: str

    def discovery(self, project_dir: Path, pio_env: str) -> Discovery: ...


def adapter_set(
    *,
    build: Any = None,
    flash: Any = None,
    reset: Any = None,
    symbol: Any = None,
    discovery: Any = None,
) -> dict[str, Any]:
    """把几件适配器收成一张表（编排按角色取，不按名字猜）。

    缺哪一件就是 `None` —— 于是"这个组合没有烧录能力"是一个**查得到的空**，
    而不是一个会在半路才炸的 `AttributeError`。
    """
    return {
        "build": build,
        "flash": flash,
        "reset": reset,
        "symbol": symbol,
        "discovery": discovery,
    }


def missing_adapters(adapters: dict[str, Any]) -> list[str]:
    """哪些角色还没装配（`precheck` 直接把它说给用户听）。"""
    return [role for role, adapter in sorted(adapters.items()) if adapter is None]


__all__ = [
    "ADAPTER_ID_PATTERN",
    "AdapterRef",
    "AdapterUnavailable",
    "BuildAdapter",
    "BuildRequest",
    "CommandSpec",
    "Discovery",
    "DiscoveryAdapter",
    "FlashAdapter",
    "ResetAdapter",
    "ResetPlan",
    "ResetStep",
    "SymbolAdapter",
    "ToolProbeRequest",
    "ToolResult",
    "UploadRequest",
    "adapter_set",
    "missing_adapters",
]

#!/usr/bin/env python3
""" / B1：**台架配置层**（**core**）—— 本机事实的唯一落点。

需求：（见公开文档）。
目标层的知识分两类：**目标知识**（IAR 工程里 `.ewp` 的相对布局、C-SPY 参数怎么拼、
`.pio/build/<env>` 的结构 —— 换台电脑不变，随代码走）与**台架事实**
（IAR 装在哪、两份 STM32 工程放在哪个盘、板子插在哪个 COM 口 —— 只属于这台电脑）。
这一层是后者的落点：写在 `bench.local.json`（**不入库**），仓库只放示例。

这一层**不认识任何一块板**：允许出现哪些键，由目标层的声明（`BenchDeclaration`）交进来；
它自己只做三件事：**读文件**、**按声明校验**、**产出带来源的值**。

四条硬规矩（`test_bench_config.py` 逐条钉住）：

1. **库代码不读本机文件。** 模块导入时台架是**空的**；只有入口 `main()` 显式
   `install_from(...)` 才装上一份。于是宿主测试永远看不到 `bench.local.json`，
   换一台电脑跑必绿结论不变 —— 有一条源码级守卫盯着"除了入口没人读文件"（含不空转证明）。
2. **配错了就整份拒绝。** 未知键、未登记的 firmware/设备、类型不对 ⇒ 抛
   `BenchFileError` 并**列出全部问题**；不做"认得的用、认不得的丢掉"
   （配错了却看着能跑，是最难查的那种错）。
3. **优先级：命令行 > 环境变量 > 台架文件**（需求 §4.3）。环境变量压在文件前面，现有脚本、
   测试与 HANDOFF 里的命令**行为不变**；每个值都带 `(value, source, detail)`。
   ⚠️ **代码里没有默认值**（ / B6，用户 2026-10-01 拍板）：三处都没给就是
   **未配置**，当场抬 `NotConfigured` 并指向示例 —— 因为"代码里的默认值"就是**原开发机的
   台架事实**（`<你的工具或工程路径>`、`COM<n>`），在别人机器上只会变成一条看着像真的错路径。
4. **不打开串口、不跑工具、不联网。** 只读文件。按序列号认板（B4）**已取消**
   （用户 2026-10-01：“认板子请问板子”，不在平台留硬件身份数据）——所以
   `devices` 只有 `port` 一个字段。

来源枚举（需求 §4.4，**封闭集合**）：`cli` / `env` / `bench` / `default`。
（`default` 仍然存在：它说的是"命令行/环境变量/台架文件都没给，取的是调用方传进来的
那个兜底值" —— B6 之后本机事实的兜底成了空值，于是它等于"未配置"。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: 台架文件的 schema 版本。涨了就说明形状变了（与报告/自述同一个写法）。
SCHEMA = 1

#: 本机文件（不进仓库）与仓库里的示例。
FILE_NAME = "bench.local.json"
EXAMPLE_NAME = "bench.example.json"

#: 查找顺序（需求 §4.1）：`--bench <路径>` > `PW_BENCH_FILE` > `bench.local.json`。
CLI_OPTION = "--bench"
FILE_ENV = "PW_BENCH_FILE"

#: 入口“**拒绝开始**”的退出码（需求 §6.1）：台架文件无效，或这一趟真要用到的格子**未配置**。
#: **服务与所有命令行工具用同一个码** —— 拿它的人一眼就能分清“配置问题”（2）与
#: “运行期失败”（打不开串口/构建没过之类）。
#: ⚠️ 这是**唯一一处定义**：入口别再写字面量 2（`test_bench_config.py` 会逐个真跑入口核对）。
EXIT_REFUSED = 2

SOURCE_CLI = "cli"
SOURCE_ENV = "env"
SOURCE_BENCH = "bench"
SOURCE_DEFAULT = "default"
SOURCES: tuple[str, ...] = (SOURCE_CLI, SOURCE_ENV, SOURCE_BENCH, SOURCE_DEFAULT)

SECTION_TOOLS = "tools"
SECTION_PROJECTS = "projects"
SECTION_DEVICES = "devices"
SECTIONS: tuple[str, ...] = (SECTION_TOOLS, SECTION_PROJECTS, SECTION_DEVICES)

#: `devices.<key>` 里允许出现的字段（封闭集合；未知字段 = 整份文件无效）。
#: ⚠️ `port` = 通信口，必须写得出来才知道要去哪儿问；
#: `target` / `flasher` 是  / D3 加的**显式绑定**（可选）：
#: - `target`：这个口后面是什么目标适配器（`demo-a` / `stm32f103-iar` …）；
#: - `flasher`：**烧录器**（例如探针）的显式标识。
#: 它们的目的是回答一个平台**不许猜**的问题：“通信口”与“烧录通路”不是同一个资源时，
#: 到底是哪一个？按枚举顺序、按“唯一在线的探针”或按相邻端口去推都是不被允许的（需求 §5）。
DEVICE_FIELDS: tuple[str, ...] = ("port", "target", "flasher")

#: 覆盖用的槽名（命令行参数落到这儿，例：`device:stm32a.port`）。
def tool_slot(key: str) -> str:
    return f"tool:{key}"


def project_slot(firmware_id: str) -> str:
    return f"project:{firmware_id}"


def device_slot(device_key: str, field_name: str = "port") -> str:
    return f"device:{device_key}.{field_name}"


def slot_device(key: str) -> str:
    """`device:stm32a.port` → `stm32a`；不是设备槽 ⇒ `""`。"""
    pieces = str(key or "").split(".", 1)
    if len(pieces) != 2 or not pieces[1]:
        return ""
    head = pieces[0].split(":", 1)
    if len(head) != 2 or head[0] != "device":
        return ""
    return head[1]


def source_note(value: "BenchValue") -> str:
    """一句人话："这个值从哪儿来"（需求 §6.4：目标检查的 `actual` 要写来源）。

    例：`来自台架文件 bench.local.json 的 projects.stm32f103-rtthread`。
    空值 ⇒ 空串（那一项有没有值由检查自己判，来源不该硬凑一句）。
    """
    if not str(value.value or "").strip():
        return ""
    if value.source == SOURCE_BENCH:
        pieces = str(value.detail).split("#", 1)
        path = pieces[0]
        slot = pieces[1] if len(pieces) > 1 else ""
        where = f" 的 {slot}" if slot else ""
        return f"来自台架文件 {path}{where}"
    if value.source == SOURCE_ENV:
        return f"来自环境变量 {value.detail}"
    if value.source == SOURCE_CLI:
        return f"来自命令行覆盖 {value.detail}"
    return "来自代码里的兜底值（不是台架文件给的）"


#: `仓库根/` —— 台架文件与示例都放在这一层（`webconsole/bench_config.py` 往上两级）。
DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def default_path(root: str | Path | None = None) -> Path:
    """本机台架文件的位置（默认 `bench.local.json`）。"""
    return Path(root) / FILE_NAME if root is not None else DEFAULT_ROOT / FILE_NAME


def example_path(root: str | Path | None = None) -> Path:
    """仓库里的示例（内容 = 当前开发机的值，给人抄的）。"""
    return Path(root) / EXAMPLE_NAME if root is not None else DEFAULT_ROOT / EXAMPLE_NAME


class BenchFileError(ValueError):
    """台架文件无效：**整份拒绝**（需求 §4.2 / §6.1），并把全部问题列出来。

    入口拿到它应该拒绝启动（退出码 2）—— 用户既然写了这份文件，就说明他想要这些值，
    悄悄退回默认值是错的。
    """

    def __init__(self, path: str | Path, problems: Sequence[str]) -> None:
        self.path = str(path)
        self.problems = tuple(str(item) for item in problems)
        detail = "；".join(self.problems) or "（没有细节）"
        super().__init__(f"台架文件无效：{self.path or '(未命名)'} —— {detail}")


class NotConfigured(LookupError):
    """这一格**没有配置**：命令行、环境变量、台架文件都没给（B6 之后代码里也没有兜底值）。

    它与 `BenchFileError` 是两件事：

    - `BenchFileError` = 用户写了台架文件但写坏了 ⇒ 入口**拒绝启动**（退出码 2）；
    - `NotConfigured` = 这台机器还没有台架文件（或文件里没写这一格）⇒ **能用平台就跑**，
      只在这一格真被用到的时候（构建/烧录/开串口/体检行）如实报出来并指向示例。

    ⚠️ 为什么要它，而不是留个默认值：代码里那个"默认值"就是**原开发机的台架事实**，
    在别人机器上是一条看着像真的错路径（`<你的工具或工程路径>`）。宁可当场说出来。
    """

    def __init__(self, slot: str) -> None:
        self.slot = str(slot)
        super().__init__(
            f"{self.slot} 没有配置：命令行、环境变量、台架文件里都没有它。"
            f"把 {EXAMPLE_NAME} 抄一份成 {FILE_NAME} 并填上这一格"
            f"（或者用 {CLI_OPTION} <路径> 指到别的台架文件）"
        )


# ===================================================================== 数据形状
@dataclass(frozen=True)
class BenchValue:
    """一个解析出来的台架值：**值 + 它是从哪来的**（需求 §4.4）。"""

    value: str = ""
    source: str = SOURCE_DEFAULT
    #: 来源细节：`env` 带变量名、`bench` 带「文件#键路径」、`cli` 带参数名。
    detail: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.value.strip())

    def as_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source, "detail": self.detail}


@dataclass(frozen=True)
class DeviceBinding:
    """一块设备在**本机台架**里的显式绑定（ / D3）。

    三个格子各有来源（`BenchValue`），所以"这个口是谁告诉我们的"永远问得清：

    | 格子 | 含义 | 缺了会怎样 |
    | --- | --- | --- |
    | `port` | 通信口 | 打不开：`need_device_port()` 直接拒（B6） |
    | `target_id` | 这个口后面是什么目标适配器 | 只是少一条可追溯性，不阻止任何事 |
    | `flasher_id` | **烧录通路**的显式标识（探针那类） | 要求它的目标一律拒烧（不猜） |

    ⚠️ 这三个格子**不是一个东西**：ESP32 那类目标通信与烧录共用同一个口，
    `flasher_id` 空着就是它的真实情况；而 STM32 那类是"串口 + SWD 探针"两个资源，
    缺 `flasher_id` 时平台**不确定该烧哪一个**，于是只能拒 —— 不按枚举顺序猜（需求 §5）。
    """

    device_key: str = ""
    port: BenchValue = field(default_factory=BenchValue)
    target_id: BenchValue = field(default_factory=BenchValue)
    flasher_id: BenchValue = field(default_factory=BenchValue)

    @property
    def flasher_bound(self) -> bool:
        """烧录通路被**点名**了吗（空串 / 没配置 ⇒ False，不是"大概就是它"）。"""
        return self.flasher_id.configured

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_key": self.device_key,
            "port": self.port.as_dict(),
            "target_id": self.target_id.as_dict(),
            "flasher_id": self.flasher_id.as_dict(),
        }

    def to_json(self) -> dict[str, Any]:
        """写进证据的那一份（人和判据看同一个形状）。"""
        return {
            "device_key": self.device_key,
            "port": self.port.value,
            "port_source": self.port.source,
            "target_id": self.target_id.value,
            "flasher_id": self.flasher_id.value,
            "flasher_bound": self.flasher_bound,
        }


@dataclass(frozen=True)
class BenchFile:
    """一份**已校验**的台架文件。

    `path == ""` 表示"没有台架文件"（**空台架，不是错误**）—— 这时一切照旧走
    环境变量；三处都没给的那一格就是**未配置**（`NotConfigured`，B6）。
    """

    schema: int = SCHEMA
    path: str = ""
    tools: Mapping[str, str] = field(default_factory=dict)
    projects: Mapping[str, str] = field(default_factory=dict)
    devices: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.path)

    def tool(self, key: str) -> str | None:
        return self.tools.get(str(key))

    def project(self, firmware_id: str) -> str | None:
        return self.projects.get(str(firmware_id))

    def device(self, device_key: str) -> Mapping[str, str]:
        return self.devices.get(str(device_key), {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "path": self.path,
            "tools": dict(self.tools),
            "projects": dict(self.projects),
            "devices": {name: dict(row) for name, row in self.devices.items()},
        }


#: 空台架（没有文件时的样子）。**不要就地改它** —— 它是共享的默认值。
EMPTY = BenchFile()


@dataclass(frozen=True)
class BenchEntry:
    """一个台架值**给人看**的样子：人话名 + 值 + **它是从哪来的**。

    B3 的 `bench` 块（`/api/state` / run manifest / 会话 manifest / Doctor）就是它的列表；
    声明与取值都在目标层（`bench_entries()`），core 只负责把同一份数据摆成同一个形状。
    """

    label: str
    key: str = ""
    owner: str = ""
    value: BenchValue = field(default_factory=BenchValue)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "key": self.key,
            "owner": self.owner,
            **self.value.as_dict(),
        }


@dataclass(frozen=True)
class BenchDeclaration:
    """一个目标**声明**自己消费哪些台架键（core 只认这个形状）。

    core 不认识 `iar_dir` 是什么，也不认识哪份固件有工程根 —— 那些由目标层交进来：

    ```python
    BenchDeclaration(
        owner="stm32",
        tool_keys=("iar_dir",),
        firmware_ids=("stm32f103-baremetal", "stm32f103-rtthread"),
        device_keys=("stm32a",),
    )
    ```

    声明之外出现的键 ⇒ 整份文件无效（需求 §4.2：不做"认得的用、认不得的丢掉"）。
    """

    owner: str
    tool_keys: tuple[str, ...] = ()
    firmware_ids: tuple[str, ...] = ()
    device_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.owner).strip():
            raise ValueError("台架声明要有 owner（哪个目标/哪一层）")

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "tool_keys": list(self.tool_keys),
            "firmware_ids": list(self.firmware_ids),
            "device_keys": list(self.device_keys),
        }


# ===================================================================== 解析与校验
def _names(items: Iterable[str]) -> str:
    rows = sorted({str(item) for item in items})
    return ", ".join(rows) if rows else "（一个都没有）"


def _want_text(raw: Any, where: str, problems: list[str]) -> str:
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"{where}：要一个非空字符串（现在是 {type(raw).__name__}）")
        return ""
    return raw.strip()


def _device_entry(raw: Any, where: str, problems: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        problems.append(f"{where}：要一个对象（现在是 {type(raw).__name__}）")
        return {}
    entry: dict[str, str] = {}
    for name in sorted(set(raw) - set(DEVICE_FIELDS)):
        problems.append(f"{where}.{name}：不认识的字段（可用：{_names(DEVICE_FIELDS)}）")
    for name in DEVICE_FIELDS:
        if name not in raw:
            continue
        value = _want_text(raw[name], f"{where}.{name}", problems)
        if value:
            entry[name] = value
    if not entry and not problems:
        problems.append(f"{where}：至少要写 {_names(DEVICE_FIELDS)} 里的一个")
    return entry


def parse(
    text: str,
    *,
    path: str | Path = "",
    declarations: Sequence[BenchDeclaration] = (),
) -> BenchFile:
    """解析 + **按声明校验**：要么拿到一份干净的文件，要么抛 `BenchFileError`（列全部问题）。

    ⚠️ `declarations` 为空时，任何键都是"没有目标声明消费" ⇒ 整份无效。这是有意的：
    校验必须知道有哪些键被消费，入口一律把声明交进来。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BenchFileError(path, [f"不是合法 JSON：{error}"]) from error
    if not isinstance(payload, dict):
        raise BenchFileError(
            path, [f"顶层要一个对象（现在是 {type(payload).__name__}）"]
        )

    problems: list[str] = []
    schema = payload.get("schema")
    if schema != SCHEMA:
        problems.append(f"schema 必须是 {SCHEMA}（现在是 {schema!r}）")

    known_tools = {key for item in declarations for key in item.tool_keys}
    known_firmwares = {name for item in declarations for name in item.firmware_ids}
    known_devices = {key for item in declarations for key in item.device_keys}

    tools: dict[str, str] = {}
    projects: dict[str, str] = {}
    devices: dict[str, dict[str, str]] = {}
    for section in SECTIONS:
        if section not in payload:
            continue
        block = payload[section]
        if not isinstance(block, dict):
            problems.append(f"{section} 要一个对象（现在是 {type(block).__name__}）")
            continue
        for raw_key, raw_value in block.items():
            key = str(raw_key)
            where = f"{section}.{key}"
            if section == SECTION_TOOLS:
                if key not in known_tools:
                    problems.append(
                        f"{where}：没有目标声明消费这个工具键（已登记：{_names(known_tools)}）"
                    )
                value = _want_text(raw_value, where, problems)
                if value:
                    tools[key] = value
            elif section == SECTION_PROJECTS:
                if key not in known_firmwares:
                    problems.append(
                        f"{where}：这个固件 id 没有登记（已登记：{_names(known_firmwares)}）"
                    )
                value = _want_text(raw_value, where, problems)
                if value:
                    projects[key] = value
            else:
                if key not in known_devices:
                    problems.append(
                        f"{where}：这个设备 key 没有登记（已登记：{_names(known_devices)}）"
                    )
                entry = _device_entry(raw_value, where, problems)
                if entry:
                    devices[key] = entry

    for extra in sorted(set(payload) - {"schema", *SECTIONS}):
        problems.append(f"顶层多了不认识的键：{extra!r}（可用：schema, {', '.join(SECTIONS)}）")

    if problems:
        raise BenchFileError(path, problems)
    return BenchFile(
        schema=SCHEMA, path=str(path), tools=tools, projects=projects, devices=devices
    )


def load(
    path: str | Path,
    *,
    declarations: Sequence[BenchDeclaration] = (),
) -> BenchFile:
    """读一份台架文件并校验。读不了（不存在/没权限）也抛 `BenchFileError`。

    ⚠️ 按 **`utf-8-sig`** 读：Windows 上写文件很容易带 BOM（PowerShell 5.1 的
    `-Encoding utf8` 与记事本的“UTF-8 with BOM”都会），那**不是内容错误**，
    没有理由让人因为编辑器习惯被拒；无 BOM 的文件照常。
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise BenchFileError(str(target), [f"读不了这个文件：{error}"]) from error
    return parse(text, path=str(target), declarations=declarations)


@dataclass(frozen=True)
class BenchLocation:
    """"台架文件在哪"的答案（需求 §4.1）。

    `path` 是"会用/会找"的那个路径（即使它不存在 —— 提示得说清找过哪儿）；
    `exists` 才是"真的能读"。**点名要的**（`cli` / `env`）文件缺了就是错，
    而默认路径上没文件只是"未配置"（不是错误，需求 §6.1）。
    """

    path: str = ""
    source: str = ""
    exists: bool = False

    @property
    def explicit(self) -> bool:
        return self.source in (SOURCE_CLI, SOURCE_ENV)

    @property
    def configured(self) -> bool:
        """真的有一份可读的台架文件（其余一切照旧走环境变量与代码默认值）。"""
        return bool(self.path) and self.exists


def find(
    *,
    cli_path: str = "",
    root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BenchLocation:
    """按 §4.1 找台架文件：命令行 > `PW_BENCH_FILE` > `bench.local.json`。"""
    environment = dict(os.environ if environ is None else environ)
    explicit = str(cli_path or "").strip()
    if explicit:
        return BenchLocation(explicit, SOURCE_CLI, Path(explicit).is_file())
    from_env = str(environment.get(FILE_ENV, "")).strip()
    if from_env:
        return BenchLocation(from_env, SOURCE_ENV, Path(from_env).is_file())
    fallback = default_path(root)
    return BenchLocation(str(fallback), SOURCE_DEFAULT, fallback.is_file())


# ===================================================================== 进程内台架
def _normalize_overrides(overrides: Mapping[str, Any] | None) -> dict[str, BenchValue]:
    """命令行覆盖：值可以是 `"COM<n>"`、`("COM<n>", "--port")` 或一个 `BenchValue`。"""
    rows: dict[str, BenchValue] = {}
    for slot, raw in dict(overrides or {}).items():
        if isinstance(raw, BenchValue):
            rows[str(slot)] = raw
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            rows[str(slot)] = BenchValue(str(raw[0]), SOURCE_CLI, str(raw[1]))
        else:
            rows[str(slot)] = BenchValue(str(raw), SOURCE_CLI, str(slot))
    return rows


class Bench:
    """**进程内当前台架**：文件值 + 命令行覆盖 + 环境。

    ⚠️ **环境是取值时读的，不是安装时快照**（`environ=None`）——旧代码就是
    `os.environ.get(...)` 在取值那一刻读的，入口里也可能在装完之后再写环境变量
    （例：`--port` 会把值写回 `PW_STM32_PORT`）。快照会让那些写入静默失效。
    测试要固定一份环境时就把 `environ=` 传进来（传了就只认它）。
    """

    def __init__(
        self,
        file: BenchFile | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
        location: "BenchLocation | None" = None,
    ) -> None:
        self.file = file if isinstance(file, BenchFile) else EMPTY
        self._overrides = _normalize_overrides(overrides)
        #: `None` = 实时读 `os.environ`；传了映射 = 只读那一份（测试用）。
        self._environ: Mapping[str, str] | None = (
            None if environ is None else dict(environ)
        )
        #: 台架文件是从哪儿来的（`cli` / `env` / `default`）—— 自述里要写明。
        self.location = location
        #: 各目标自述的台架值（B3）。⚠️ 由 `bench_setup.install_from()` 在**装好之后**
        #: 收一次（那时 `active()` 正是这一份）；库代码只读它。
        self.entries: tuple["BenchEntry", ...] = ()

    @property
    def overrides(self) -> Mapping[str, BenchValue]:
        """命令行覆盖（自述里要写明"这个值是命令行给的"）。"""
        return dict(self._overrides)

    # ------------------------------------------------------------ 取值
    def tool(self, key: str, *, env_var: str = "", env_fallback: str = "", default: str = "") -> BenchValue:
        return self._resolve(
            slot=tool_slot(key),
            where=f"{SECTION_TOOLS}.{key}",
            file_value=self.file.tool(key),
            env_vars=(env_var, env_fallback),
            default=default,
        )

    def project(self, firmware_id: str, *, env_var: str = "", default: str = "") -> BenchValue:
        return self._resolve(
            slot=project_slot(firmware_id),
            where=f"{SECTION_PROJECTS}.{firmware_id}",
            file_value=self.file.project(firmware_id),
            env_vars=(env_var,),
            default=default,
        )

    def device_port(self, device_key: str, *, env_var: str = "", default: str = "") -> BenchValue:
        return self.device_field(device_key, "port", env_var=env_var, default=default)

    def device_field(
        self, device_key: str, field_name: str, *, env_var: str = "", default: str = ""
    ) -> BenchValue:
        """`devices.<键>.<字段>` 的取值梯子（命令行 > 环境变量 > 台架文件 > 默认值）。

        ⚠️ 字段名必须是 `DEVICE_FIELDS` 里的一个：台架文件已经按封闭集合校验过了，
        这里再拦一次是为了**代码**写错名字时当场报错，而不是静默拿到一个空值。
        """
        name = str(field_name)
        if name not in DEVICE_FIELDS:
            raise KeyError(f"不认识设备字段 {name!r}（可用：{list(DEVICE_FIELDS)}）")
        return self._resolve(
            slot=device_slot(device_key, name),
            where=f"{SECTION_DEVICES}.{device_key}.{name}",
            file_value=self.file.device(device_key).get(name),
            env_vars=(env_var,),
            default=default,
        )

    def device_binding(self, device_key: str, *, env_var: str = "") -> DeviceBinding:
        """一块设备在本机台架里的**全部**绑定（ / D3）。

        ⚠️ **没配置不是错误**（`BenchValue.configured is False`）：入口只在"这一趟真要用到
        它"的时候才把 `NotConfigured` 报出来（B6 的口径）。
        """
        key = str(device_key)
        return DeviceBinding(
            device_key=key,
            port=self.device_field(key, "port", env_var=env_var),
            target_id=self.device_field(key, "target"),
            flasher_id=self.device_field(key, "flasher"),
        )

    # ------------------------------------------------ 必须配置的那几格（B6）
    def need_tool(
        self, key: str, *, env_var: str = "", env_fallback: str = ""
    ) -> BenchValue:
        """要一个**必须配置**的工具目录：三处都没给 ⇒ `NotConfigured`（不猜路径）。"""
        value = self.tool(key, env_var=env_var, env_fallback=env_fallback)
        return self._need(value, tool_slot(key))

    def need_project(self, firmware_id: str, *, env_var: str = "") -> BenchValue:
        """要一个**必须配置**的工程根 ⇒ 没配置就 `NotConfigured`。"""
        value = self.project(firmware_id, env_var=env_var)
        return self._need(value, project_slot(firmware_id))

    def need_device_port(self, device_key: str, *, env_var: str = "") -> BenchValue:
        """要一个**必须配置**的串口名 ⇒ 没配置就 `NotConfigured`。"""
        value = self.device_port(device_key, env_var=env_var)
        return self._need(value, device_slot(device_key, "port"))

    @staticmethod
    def _need(value: BenchValue, slot: str) -> BenchValue:
        if not value.configured:
            raise NotConfigured(slot)
        return value

    def _resolve(
        self,
        *,
        slot: str,
        where: str,
        file_value: str | None,
        env_vars: Sequence[str],
        default: str,
    ) -> BenchValue:
        override = self._overrides.get(slot)
        if override is not None and override.configured:
            return override
        source = os.environ if self._environ is None else self._environ
        for name in env_vars:
            if not str(name or "").strip():
                continue
            raw = str(source.get(str(name), "")).strip()
            if raw:
                return BenchValue(raw, SOURCE_ENV, str(name))
        text = str(file_value or "").strip()
        if text:
            shown = f"{self.file.path}#{where}" if self.file.path else where
            return BenchValue(text, SOURCE_BENCH, shown)
        return BenchValue(str(default or ""), SOURCE_DEFAULT, "")

    # ------------------------------------------------------------ 自述
    def selected(self, owners: Sequence[str] | None = None) -> tuple["BenchEntry", ...]:
        """按主人（目标 id）筛出"**本链用到的**"那几个值；`None` = 全部。

        匹配规则（一处定义、可判据）：

        - `owner` 与本链 id **相等** ⇒ 收（目标级链：这条链就是那个目标本身）；
        - `owner` 是本链 id 的**前缀**（`<owner>-<设备键>`，登记表里"一块板子一条链"
          的写法，例：`demo-a`）⇒ 收，并把后缀记作本链的设备键；
        - `device:<键>.<字段>` 这类条目，**只有键对得上本链的设备键才收**
          （目标级链没有设备键 ⇒ 收该目标的全部设备条目）。
        """
        if owners is None:
            return tuple(self.entries)
        wanted = {str(name) for name in owners}
        picked: list[BenchEntry] = []
        for row in self.entries:
            device_key = None
            for name in wanted:
                if name == row.owner:
                    break
                if name.startswith(f"{row.owner}-"):
                    device_key = name[len(row.owner) + 1:]
                    break
            else:
                continue
            row_device = slot_device(row.key)
            if row_device and device_key is not None and row_device != device_key:
                continue
            picked.append(row)
        return tuple(picked)


    def as_dict(self, owners: Sequence[str] | None = None) -> dict[str, Any]:
        """那个 `bench` 块（B3）：`/api/state`、run manifest、会话 manifest 共用这一个。

        ⚠️ 它是**数据**：谁都能读（core 也不用 import 任何目标模块）—— 条目是
        `bench_setup.install_from()` 装台架时收进来的。
        """
        return describe(self, self.selected(owners), location=self.location)


#: `bench` 块的顶层键（四处同源：state / run manifest / 会话 manifest / Doctor）。
def describe(
    bench: "Bench",
    entries: Sequence["BenchEntry"] = (),
    *,
    location: "BenchLocation | None" = None,
) -> dict[str, Any]:
    """**同一个函数**产出那个 `bench` 块（需求 §6.3：“口径与 R2d 自述一致”）。

    - `configured` / `file` / `schema`：有没有台架文件、找的是哪一份；
    - `from`：那个文件是命令行点的、环境变量指的，还是默认位置（`cli` / `env` / `default`）；
    - `entries`：本链用到的每个值 + 来源（`bench` / `env` / `cli` / `default`）；
    - `overrides`：命令行覆盖（没给就是空对象）。
    """
    return {
        "configured": bool(bench.file.configured),
        "file": bench.file.path,
        "schema": bench.file.schema,
        "from": str(location.source) if location is not None else "",
        "entries": [row.as_dict() for row in entries],
        "overrides": {
            slot: item.as_dict() for slot, item in sorted(bench.overrides.items())
        },
    }


#: 进程级当前台架。**只有入口 `install_from()` 会改它**；库代码一律 `active()`。
_ACTIVE: Bench | None = None


def active() -> Bench:
    """库代码取值**只**走这里。没装过 ⇒ 空台架（宿主测试永远看不到本机文件）。"""
    return _ACTIVE if _ACTIVE is not None else Bench(EMPTY)


def install(bench: Bench) -> Bench:
    """装一份台架（入口专用；测试也可以用它注入）。"""
    global _ACTIVE
    _ACTIVE = bench
    return bench


def reset() -> None:
    """回到"没装过"（测试收尾用）。"""
    global _ACTIVE
    _ACTIVE = None


def installed() -> bool:
    return _ACTIVE is not None


def block(owners: Sequence[str] | None = None) -> dict[str, Any]:
    """当前台架的 `bench` 块（ / B3）——**四处共用的那一个函数**。

    用它的是：`/api/state`（`assemblies[].bench`）、run manifest、观察会话 manifest、
    Doctor。`owners` 给"只要本链用到的"（例：传目标 id），不给就是这台机器的全部台架值。
    """
    return active().as_dict(owners)


#: `status()` 说得出的四种情形（Doctor 的 `platform.bench.file` 就按它分支）。
STATUS_UNINSTALLED = "uninstalled"
STATUS_ABSENT = "absent"
STATUS_OK = "ok"
STATUS_INVALID = "invalid"


def status(*, declarations: Sequence[BenchDeclaration] = ()) -> dict[str, Any]:
    """**这一刻**台架文件的情形（ / B3；Doctor 的 `platform.bench.file` 用它）。

    它是**库代码可以调的只读问答**：读文件这件事仍然只发生在本模块里
    （`library_code_never_reads_the_machine_file` 守着这条）。返回值：

    - `uninstalled`：本进程没装过台架（宿主测试 / 库代码单独跑）⇒ 与"没有文件"同等看待。
      ⚠️ 这里**故意不去磁盘上搜**：宿主测试在有 `bench.local.json` 的机器上必须得到同一个
      结论（需求 §9.6），一搜就会随机器而变。
    - `absent`：装了，但默认位置没有文件 ⇒ 正在用代码内置默认值；
    - `ok`：文件有效（**再**按声明校一遍，防止启动后被改坏）；
    - `invalid`：文件坏了/读不了 ⇒ 服务本该拒绝启动（退出码 2），这里如实报出来。
    """
    if not installed():
        return {"state": STATUS_UNINSTALLED, "file": "", "from": "", "problems": []}
    machine = active()
    where = str(getattr(machine.location, "source", "") or "")
    path = str(machine.file.path or "")
    if not path:
        return {
            "state": STATUS_ABSENT,
            "file": str(default_path()),
            "from": where,
            "problems": [],
        }
    try:
        # 用 `parse`（不是 `load`）：这里**只**要结论，不要读第二遍文件以外的副作用。
        parsed = parse(Path(path).read_text(encoding="utf-8-sig"), path=path, declarations=declarations)
    except BenchFileError as error:
        return {"state": STATUS_INVALID, "file": path, "from": where,
                "problems": [str(item) for item in error.problems]}
    except OSError as error:
        return {"state": STATUS_INVALID, "file": path, "from": where,
                "problems": [f"读不了这份文件：{error}"]}
    return {
        "state": STATUS_OK,
        "file": path,
        "from": where,
        "problems": [],
        "schema": parsed.schema,
        "entries": len(machine.entries),
    }


def install_from(
    *,
    cli_path: str = "",
    root: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    declarations: Sequence[BenchDeclaration] = (),
    environ: Mapping[str, str] | None = None,
) -> Bench:
    """**入口专用一行**（需求 §5）：找文件 → 校验 → 装成进程级台架。

    - 没配文件 ⇒ 装一份空台架（零行为变化，需求 §6.1）；
    - 配了但读不了/无效 ⇒ 抛 `BenchFileError`（入口拒绝启动，退出码 2）；
    - 不打开串口、不跑工具、不联网。
    """
    location = find(cli_path=cli_path, root=root, environ=environ)
    if not location.exists:
        if location.explicit:
            raise BenchFileError(
                location.path,
                [
                    f"{location.source} 指到了 {location.path}，但这个文件不存在"
                    f"（要开工就把台架值写进去；不想用就把它从 {CLI_OPTION} / {FILE_ENV} 里去掉）"
                ],
            )
        # 默认路径上没文件 = **未配置**（不是错误）：一切照旧。
        return install(Bench(EMPTY, overrides=overrides, environ=environ, location=location))
    loaded = load(location.path, declarations=declarations)
    return install(Bench(loaded, overrides=overrides, environ=environ, location=location))


__all__ = [
    "Bench",
    "BenchDeclaration",
    "BenchEntry",
    "BenchFile",
    "BenchFileError",
    "BenchLocation",
    "BenchValue",
    "CLI_OPTION",
    "DEFAULT_ROOT",
    "DEVICE_FIELDS",
    "DeviceBinding",
    "EMPTY",
    "EXAMPLE_NAME",
    "EXIT_REFUSED",
    "FILE_ENV",
    "FILE_NAME",
    "NotConfigured",
    "SCHEMA",
    "SECTIONS",
    "SECTION_DEVICES",
    "SECTION_PROJECTS",
    "SECTION_TOOLS",
    "SOURCES",
    "SOURCE_BENCH",
    "SOURCE_CLI",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "STATUS_ABSENT",
    "STATUS_INVALID",
    "STATUS_OK",
    "STATUS_UNINSTALLED",
    "active",
    "block",
    "default_path",
    "describe",
    "device_slot",
    "example_path",
    "find",
    "install",
    "install_from",
    "installed",
    "load",
    "parse",
    "project_slot",
    "reset",
    "slot_device",
    "source_note",
    "status",
    "tool_slot",
]

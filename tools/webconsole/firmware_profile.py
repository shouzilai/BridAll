"""**固件 profile**（**core**， / R4-A）。

一台板子可以跑**不止一个固件**：R4-A 是 STM32 + IAR 的裸机固件，R4-B 是同一块板、
同一套 IAR 工具链、**同一个 STM32 Target Adapter** 上的 RT-Thread 固件。
两者共用构建/烧录/复位/串口/产物发现（那是 `第二目标` 的事），
区别只在"固件那边跑的是什么、用哪份字典、认哪个 domain、装的是哪个 Project Package"。

所以这里定义的是**一层薄薄的数据**：

```text
FirmwareProfile
  ├─ firmware_id      stm32f103-baremetal / stm32f103-rtthread
  ├─ target_id        它跑在哪块目标上（← 第二目标.TARGET_ID 那一层）
  ├─ stack            baremetal / rtthread / freertos（封闭集合）
  ├─ project          它装哪个 Project Package（baremetal / rtthread / …）
  ├─ build_config     构建配置（IAR 的配置名 / 参考目标的 env 名）
  ├─ build_recipe     用**哪条命令**构建它（`BuildRecipe`；R4-B / N1 加的）
  ├─ prepare_recipe   构建前要不要先生成点什么（裸机：身份头 + 字典；RT-Thread：没有）
  ├─ flash_settings   烧录器参数文件在哪（`{config}` 可用；R4-B / N2 加的）
  ├─ flash_driver_settings  那份“驱动”参数文件（可以借用**另一个工程**的：同芯片同探针）
  ├─ flash_bytes      应用区体量（**芯片事实，由 profile 声明**；平台只拿它做体量核对）
  ├─ token_domain     解码时按哪个 domain 过滤（**每个 profile 一个**）
  ├─ dictionary / elf / binary   这份固件自己的产物（相对工程目录）
  ├─ identity_hint    设备身份横幅里应该出现的固件名（对不上就是身份串了）
  └─ status           implemented / planned
```

三条规矩（每一条都有宿主回归钉着）：

1. **`planned` 不等于 `implemented`。** R4-B 的 RT-Thread profile 现在就登记在这里、
   状态是 `planned`：它**可以**被查、被展示、被写进文档，但**不可以被选中去跑**
   （`FirmwareRegistry.select()` 当场报错）。"预留了切换能力"与"已经支持"必须分得开
   —— 与能力模型里 `unsupported` / `unknown` 不许混起来是同一个道理。
2. **同一条 profile 的两份固件不许共用产物。** 同一个目标下，两个 profile 的
   `dictionary` / `elf` / `token_domain` **必须互不相同**（`artifact_collisions()`）。
   裸机与 RT-Thread 的字典/ELF/身份/历史证据一旦混用，日志会被解成"看着合理"的错内容，
   而那种错误没有任何下游判据能发现。这条规矩现在就生效。
3. **只描述，不执行。** 这里不 import 任何目标/项目模块、不碰文件、不跑命令；
   "构建/烧录怎么跑"仍然只在 `第二目标` 那一层（R4 的核心诉求：
   两个固件 profile 共用同一个 Target Adapter，不复制第二套烧录与串口实现）。

第 4 条（R4-B / N1 加）：**"用哪条命令构建"是固件的属性，不是目标实现里的分支。**
两条固件跑在**同一个**适配器上（同一个类、同一份代码），唯一不同的是构建命令与产物 ——
所以那些差异写成 `BuildRecipe` 数据，由 profile 带出去。适配器里**不许**出现
`if firmware == "stm32f103-rtthread"` 这种写法：那种写法每加一份固件就要再改一次适配器，
而且第二个固件会悄悄继承第一个的判据（IAR 要数日志里的错误行，`make` 只看退出码）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from .embedded_test_model import TestRegion
except ImportError:  # Top-level imports used by existing CLI entry points.
    from embedded_test_model import TestRegion  # type: ignore[no-redef]

#: 固件跑在什么运行时上。**封闭集合**：写错名字当场报错，而不是悄悄接受一个错的技术栈。
#: ⚠️ 只列**真的会做成固件 profile 的**那几种：参考目标（ESP32 示例项目）跑的是 Arduino + FreeRTOS，
#: 但它不是"可以在这块板上二选一"的固件，所以不在这里；core 源码里也不该出现那个词
#: （边界判据把它当一个平台标识盯着）。R4-A 裸机 / R4-B RT-Thread 就是这两条。
FIRMWARE_STACKS: tuple[str, ...] = ("baremetal", "rtthread")

#: 这份固件做到哪一步了。
#: - `implemented`：有实现、可以选、可以构建/烧录/采集；
#: - `planned`：**只登记了位置**（R4-B 的 RT-Thread），可以被查但**不可以被选**。
FIRMWARE_STATUSES: tuple[str, ...] = ("implemented", "planned")

#: 怎么判"这次构建算成功"（**封闭集合**，写错当场报错）：
#: - `exit`：只看退出码 + 声明的产物是否真的出现（GCC/make 这一路就是这样）；
#: - `log`：还要数日志里的统计行（IAR 的 `IarBuild` 即便报错也可能返回 0，必须数错误行）。
BUILD_SUCCESS_RULES: tuple[str, ...] = ("exit", "log")


def _validate_size_pattern(pattern: str) -> None:
    """尺寸行的正则：**必须是 4 组**（名字 / 百分比 / 已用字节 / 总字节）。

    ⚠️ 为什么在 core 就把组数钉住：这条正则的消费方是编排的构建步骤
    （`Discovery.size_pattern`，同样是这 4 组）。一个少了一组的正则不会报错，
    只会让尺寸**静默地不见了** —— 那种"什么都没发生"的失败最难发现。
    """
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"size_pattern 不是合法正则（{error}）：{pattern!r}") from error
    if compiled.groups != 4:
        raise ValueError(
            f"size_pattern 必须正好有 4 个捕获组（名字/百分比/已用字节/总字节），"
            f"现在有 {compiled.groups} 个：{pattern!r}"
        )


@dataclass(frozen=True)
class BuildRecipe:
    """**一条构建命令的声明**（数据，不是行为：core 只描述它，执行在目标层）。

    ```text
    BuildRecipe
      ├─ adapter_id      这条命令登记在适配器表里的名字（证据/manifest 认它）
      ├─ label           人话：这是什么构建（进证据、进页面、进 doctor）
      ├─ tool            **工具键**（`iarbuild` / `python` / `make` …）
      ├─ args            tool 之后的参数（参数数组，不拼字符串；`{project}` = 工程根）
      ├─ cwd             工作目录（**相对工程根**；空 = 工程根。RT-Thread 必须在 Debug/ 里跑 make）
      ├─ inputs          跑之前**必须存在**的输入（相对工程根；缺了就当场报，不要等工具报错）
      ├─ artifacts       构建后**必须存在**的产物（相对工程根；`{project}` 同样可用）
      ├─ successful_when exit | log
      └─ size_pattern    从构建输出里读尺寸行的正则（4 组；空 = 这次构建不产尺寸数字）
    ```

    ⚠️ `tool` 故意写成**工具键**而不是路径：core 不认识也不该认识任何工具装在哪
    （`<你的工具或工程路径> (x86)\IAR …` 这种东西属目标层）。路径由目标层拿着键去解析，
    它才能同时尊重环境变量（`PW_STM32_IAR_DIR` / `PW_RTTHREAD_STUDIO`）与 `--project`。
    """

    adapter_id: str
    label: str
    tool: str
    args: tuple[str, ...] = ()
    cwd: str = ""
    inputs: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    successful_when: str = "exit"
    size_pattern: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.adapter_id:
            raise ValueError("BuildRecipe.adapter_id 不能为空（证据里要说清是哪条命令）")
        if not self.label:
            raise ValueError("BuildRecipe.label 不能为空（证据里要能读出这是哪条构建）")
        if not self.tool:
            raise ValueError("BuildRecipe.tool 不能为空（不知道跑什么就不是配方）")
        if self.successful_when not in BUILD_SUCCESS_RULES:
            raise ValueError(
                f"不认识的构建成功判据 {self.successful_when!r}：可用的是 {list(BUILD_SUCCESS_RULES)}"
            )
        if self.size_pattern:
            _validate_size_pattern(self.size_pattern)

    @property
    def argv_shape(self) -> tuple[str, ...]:
        """`tool` + `args`（**未解析**：里面的 `{project}` 还没换成真路径）。

        给回归与 doctor 用：要看"打算跑什么"，而不是"解析成了什么"。
        """
        return (self.tool, *self.args)

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "label": self.label,
            "tool": self.tool,
            "args": list(self.args),
            "cwd": self.cwd,
            "inputs": list(self.inputs),
            "artifacts": list(self.artifacts),
            "successful_when": self.successful_when,
            "size_pattern": self.size_pattern,
            "notes": list(self.notes),
        }


class UnknownFirmware(LookupError):
    """查不到这个 firmware_id：**没有默认固件**，调用方必须把这件事说出来。"""


class FirmwareNotImplemented(RuntimeError):
    """这份固件只有登记、没有实现（`status="planned"`）——不是崩溃，是一个状态。"""


def _validate(firmware_id: str, stack: str, status: str) -> None:
    if not firmware_id:
        raise ValueError("firmware_id 不能为空")
    if stack not in FIRMWARE_STACKS:
        raise ValueError(
            f"不认识的固件运行时 {stack!r}：可用的是 {list(FIRMWARE_STACKS)}"
            "（拼错的技术栈不该被悄悄接受）"
        )
    if status not in FIRMWARE_STATUSES:
        raise ValueError(
            f"不认识的固件状态 {status!r}：可用的是 {list(FIRMWARE_STATUSES)}"
        )


@dataclass(frozen=True)
class FirmwareProfile:
    """一份固件（同一块板上的一个"装什么跑什么"）。"""

    firmware_id: str
    target_id: str
    label: str
    stack: str
    #: 装哪个 **Project Package**（`baremetal` / `rtthread` / 示例项目）——
    #: 切换固件 profile 就是切换项目包，而不是切换一套烧录实现。
    project: str
    #: 构建配置名（IAR 的 configuration，例如 `Debug`）。planned 的 profile 可以是空串。
    build_config: str = ""
    #: 用**哪条命令**构建这份固件（`BuildRecipe`；`None` = 还没声明 —— 那就构建不了）。
    #: ⚠️ 这与 `build_config` 是两件事：后者是"工具链内部的一个配置名"，
    #: 前者是"跑哪个可执行文件、在哪个目录、产出什么"。RT-Thread 走 make，
    #: 没有 IAR 那种配置名，所以它用配方说话。
    build_recipe: "BuildRecipe | None" = None
    #: 构建**之前**要不要先生成点什么（裸机：固件身份头 + Token 字典；RT-Thread：没有这一步）。
    prepare_recipe: "BuildRecipe | None" = None
    #: 构建**之后**要不要**再**跑一次 `prepare_recipe`。
    #: 裸机需要：身份头/字典里要写进 ELF 的哈希，而那份 ELF 构建完才存在。
    prepare_after_build: bool = False
    #: 烧录器的**参数文件**在哪（相对本固件的工程根；`{config}` 可用）。
    #: ⚠️ core 只搬这两个路径，不解释它们是什么文件 —— 在 IAR 那里它们是 `.xcl`，
    #: 换个工具链就可能是别的名字。关键是：**“往哪块板上写什么”必须能由文件核对**
    #: （`.xcl` 里写着要烧的 ELF，闸门就是“它 == 本次构建产物”）。
    #: RT-Thread 工程不是 IAR 工程 ⇒ 它的那份是**派生**出来的，路径由目标层给。
    flash_settings: str = ""
    #: 参数文件里的**“驱动”那一份**：芯片型号 / 内核 / 接口 / 复位策略 / 探针型号。
    #: 它是**芯片级**的事实，所以可以借用另一个工程的（同一块板 = 同一份）。
    flash_driver_settings: str = ""
    #: 应用区体量（字节）。**芯片事实，由 profile 声明** —— 平台不维护芯片尺寸表、
    #: 也不从 ELF / 链接脚本推断它（能力边界）。`0` = 没声明 ⇒ 平台不做体量核对，
    #: 并在证据里如实记 `flash_bytes_declared: 0`（“没声明”与“声明了 0”不是一回事）。
    flash_bytes: int = 0
    #: 解码按哪个 domain 过滤 token。**每个 profile 一个**（不许共用：共用了就会串字典）。
    token_domain: str = ""
    #: 这份固件自己的产物（**相对工程目录**的路径字符串；Core 层不解析它们，只搬）。
    dictionary: str = ""
    elf: str = ""
    binary: str = ""
    #: 设备身份横幅里应当出现的固件名（对不上 ⇒ 板上跑的不是这份固件）。
    identity_hint: str = ""
    status: str = "implemented"
    notes: tuple[str, ...] = ()
    #: Firmware-owned reserved regions. An empty tuple means no destructive test is allowed.
    test_regions: tuple[TestRegion, ...] = ()

    def __post_init__(self) -> None:
        _validate(self.firmware_id, self.stack, self.status)
        if self.flash_bytes < 0:
            raise ValueError("flash_bytes 不能是负数（它是体量，不是偏移）")
        region_ids = [region.region_id for region in self.test_regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("固件 profile 的 test_regions 有重复 ID")

    # ------------------------------------------------------------------ 查询
    @property
    def implemented(self) -> bool:
        """这份固件是不是**真的有实现**（只有实现过的才允许被选中去跑）。"""
        return self.status == "implemented"

    def artifacts(self) -> dict[str, str]:
        """它声明过的产物（空的不列 —— "没有"与"空字符串"不是一回事）。"""
        return {
            name: value
            for name, value in (
                ("dictionary", self.dictionary),
                ("elf", self.elf),
                ("binary", self.binary),
            )
            if value
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "firmware_id": self.firmware_id,
            "target_id": self.target_id,
            "label": self.label,
            "stack": self.stack,
            "project": self.project,
            "build_config": self.build_config,
            "build": self.build_recipe.to_json() if self.build_recipe else None,
            "prepare": self.prepare_recipe.to_json() if self.prepare_recipe else None,
            "prepare_after_build": self.prepare_after_build,
            "flash_settings": self.flash_settings,
            "flash_driver_settings": self.flash_driver_settings,
            "flash_bytes": self.flash_bytes,
            "flash_bytes": self.flash_bytes,
            "token_domain": self.token_domain,
            "artifacts": self.artifacts(),
            "identity_hint": self.identity_hint,
            "status": self.status,
            "implemented": self.implemented,
            "notes": list(self.notes),
            "test_regions": [region.to_json() for region in self.test_regions],
        }


def artifact_collisions(profiles: Iterable[FirmwareProfile]) -> list[str]:
    """同一个目标下，两份固件共用同一份产物 / 同一个 domain 的地方。

    ⚠️ 只有**都声明了**才算碰撞：`planned` 的 profile 产物是空的，
    "两个都是空"不是"两个都一样"。
    """
    problems: list[str] = []
    rows = list(profiles)
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if left.target_id != right.target_id:
                continue
            for name in ("token_domain", "dictionary", "elf", "binary"):
                first = getattr(left, name)
                second = getattr(right, name)
                if not first or not second:
                    continue
                if first == second:
                    problems.append(
                        f"{left.firmware_id} 与 {right.firmware_id} 共用同一个 {name}"
                        f"（{first!r}）：换固件时字典/ELF/证据会互相串"
                    )
    return problems


class FirmwareRegistry:
    """手上认识哪些固件 profile。查不到就是查不到 —— 不编一份默认的出来。"""

    def __init__(self, profiles: Iterable[FirmwareProfile] = ()) -> None:
        self._profiles: dict[str, FirmwareProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: FirmwareProfile) -> FirmwareProfile:
        if profile.firmware_id in self._profiles:
            raise ValueError(f"firmware_id 重复：{profile.firmware_id!r}")
        self._profiles[profile.firmware_id] = profile
        return profile

    def get(self, firmware_id: str) -> FirmwareProfile | None:
        return self._profiles.get(str(firmware_id or ""))

    def require(self, firmware_id: str) -> FirmwareProfile:
        profile = self.get(firmware_id)
        if profile is None:
            raise UnknownFirmware(
                f"没有登记这份固件 {firmware_id!r}：已知的是 {sorted(self._profiles)}"
                "（平台不会替它编一份默认配置）"
            )
        return profile

    def for_target(self, target_id: str) -> list[FirmwareProfile]:
        return [p for p in self._profiles.values() if p.target_id == target_id]

    def implemented(self) -> list[FirmwareProfile]:
        """**可以真的跑**的那几份（页面上的选择项只该列这些）。"""
        return [p for p in self._profiles.values() if p.implemented]

    def select(self, firmware_id: str) -> FirmwareProfile:
        """选一份固件去跑；`planned` 的当场拒绝（预留 ≠ 支持）。"""
        profile = self.require(firmware_id)
        if not profile.implemented:
            raise FirmwareNotImplemented(
                f"{profile.firmware_id} 只是**登记**（status=planned）："
                f"它的实现属于 {profile.stack} 那一片，不在当前范围内 —— "
                "现在能跑的是 "
                + (", ".join(p.firmware_id for p in self.implemented()) or "（一份都没有）")
            )
        return profile

    def collisions(self) -> list[str]:
        return artifact_collisions(self._profiles.values())

    def problems(self) -> list[str]:
        """登记表自身的问题（编排/页面把这份清单说给用户听）。"""
        return self.collisions()

    def profiles(self) -> list[FirmwareProfile]:
        return list(self._profiles.values())

    def to_json(self) -> list[dict[str, Any]]:
        return [profile.to_json() for profile in self._profiles.values()]

    def __len__(self) -> int:
        return len(self._profiles)


__all__ = [
    "BUILD_SUCCESS_RULES",
    "FIRMWARE_STACKS",
    "FIRMWARE_STATUSES",
    "BuildRecipe",
    "FirmwareNotImplemented",
    "FirmwareProfile",
    "FirmwareRegistry",
    "UnknownFirmware",
    "artifact_collisions",
]

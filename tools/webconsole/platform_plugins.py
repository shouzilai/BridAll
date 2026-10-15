"""组合入口的**插件束**（**core**， / R2b）。

核心层不该知道"ESP32"或"示例项目"，更不该 import 它们的实现。可是通用逻辑确实
需要几样**只有目标/项目才有的东西**：

| 需要什么 | 谁提供 | core 拿不到时会怎样 |
| --- | --- | --- |
| 串口怎么开（`port_factory`） | 目标适配器（pyserial，只设端口/波特率/超时） | 打开失败并**如实说"没装配串口实现"**（不假装连上了） |
| 串口占用怎么探（`port_probe`） | 目标适配器（Windows 零共享探测） | 状态是 `unknown`，**不冒充空闲** |
| 设备登记（`device_registry`） | 目标/项目配置 | 登记表为空 ⇒ 没有可连的目标 |
| 字典库从哪来（`library_factory`） | 目标适配器（扫构建产物与归档） | 拒绝解码，并说明"没有字典库"（不猜字典） |
| 解码器（`decoder_factory` + `decoder_probe`） | 目标适配器（Workbench detokenizer） | 拒绝解码，raw.bin 照收 |
| 项目事件解析（`event_parser`） | **项目包**（示例项目 的记录格式） | 只解出文本，不产生结构化事件（不编事件） |
| 指标序列（`metrics_series`） | **项目包**（示例项目 的指标表） | 实时观察里「指标趋势」不动（事件照常发，不编一条假序列） |
| token domain（`token_domain`） | 项目（经 `TargetProfile.token_domain`） | 拒绝解码：不知道按哪个 domain 过滤 |
| "设备说它不认识这条命令"（`unsupported_reply`） | 项目/固件（中文回答属于这台设备） | 身份状态停在"没有回应"而不是"设备不支持" |
| 项目身份行（`identity_parser`） | 项目包（非通用 JSON 的启动身份格式） | 只接受通用 `@PWINFO {...}` / `BOOT build=...` |

这些全是**函数与数据**，由装配处（`server.py`）一次性装好交给 core：于是
`test_core_layer.py` 可以用一个假目标装出完全不同的组合，而 core 一行都不用改
（PG-* 见边界清单 §8：没有 示例项目插件时，假目标照样能跑完会话全流程）。

⚠️ 这里**只是数据容器**：不 import 目标层/项目层，也不自己查找实现（没有全局注册表、
没有"自动发现"）—— 装配处显式给什么就是什么，没给就是没给。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PlatformPlugins:
    """一次装配提供的全部平台插件（缺省 = 什么都不提供，core 照常诚实降级）。"""

    #: 这套装配叫什么（进诊断包与 `/api/state`，让人一眼看出"跑的是哪套组合"）。
    label: str = ""
    #: 参考目标的平台/项目标识（`esp32` / 示例项目）。core 只当字符串带着，不做判断。
    platform: str = ""
    project: str = ""
    #: 设备登记表**实例**（core 只调它的 `specs()` / `get()` / 状态记账接口）。
    device_registry: Any = None
    #: `(port, baud) -> 可 open/read/write/close 的端口对象`（打开时**不许**动复位线）。
    port_factory: Callable[[str, int], Any] | None = None
    #: `() -> str`：本机**串口实现**能不能用（返回一句人话，例如 `pyserial 3.5`）。
    #: **空串 = 不可用**（那是 precheck 该拦下来的事）。没装配就只能说"不知道"。
    #: ⚠️  / R4：编排以前自己 `import serial` 体检 —— 那是 transport 实现的知识。
    transport_probe: Callable[[], str] | None = None
    #: `port -> free|busy|absent|unknown`（探测不许有副作用）。
    port_probe: Callable[[str], str] | None = None
    #: `() -> 字典库`（库要提供 `exact(build_id32)` / `entries_for(source)` / `describe_available()`）。
    library_factory: Callable[[], Any] | None = None
    #: `() -> (构建号, …)`：**本机这一份固件**构建产物里登记的构建号。
    #:  / L8 的"构建标识"核对用它（受控用例的判据问：板上这个号是否是
    #: 这里构建出来的那一份）。
    #: ⚠️ 与字典库**分开**：没有 Token Log 的构建没有字典（库认不出它），它的构建号
    #: 却照样在产物清单里 —— 拿字典库当"本机构建"会把那份误判成"不是这里构建的"
    #: （真机撞到过：板上跑的确实是本机的 notoken 构建，却被判 FAIL）。
    build_ids: Callable[[], tuple[int, ...]] | None = None
    #: `(dictionary_path, domain) -> (bytes -> bytes)`：一行进、一行出，**保留行终止符**。
    decoder_factory: Callable[[Path, str], Callable[[bytes], bytes]] | None = None
    #: `() -> bool`：解码器在**本机**到底能不能用（例如 Workbench 子模块在不在）。
    decoder_probe: Callable[[], bool] | None = None
    #: **整份解码器**（一个对象/模块，要有 `make_line_decoder(path, domain)` 与
    #: `decode_bytes(raw, path, domain)`）。自动验证的 `decode` 步骤用它把整段 raw.bin
    #: 解码成 decoded.log；`decoder_factory` 只是它的一行版（实时观察用）。
    #: ⚠️  / R4：编排以前 `import token 解码模块` —— 那是参考目标的实现。
    decoder_backend: Any = None
    #: **崩溃解析器**（一个对象/模块，要有 `inspect(text, …)` 与 `summarize(finding)`）。
    #: ⚠️  / R4：编排以前 `import panic` —— 它的复位码表与符号化工具都是
    #: 某个平台的（ESP32/Xtensa）。没装配就**不做崩溃判定**（如实说，不写一份空证据）。
    panic_parser: Any = None
    #: `(text, seq=…, offset=…) -> 事件对象 | None`；事件对象要有 `to_json()`。
    event_parser: Callable[..., Any] | None = None
    #: `(events) -> 指标序列`：项目包把结构化事件折成 `metrics.jsonl` 那一份（core 只搬运）。
    #: 实时观察用它让「指标趋势」随着数据长（按事件节流重算）；没装配 ⇒ 那一栏在实时观察里
    #: 不动，而且**不编一条假序列**。
    metrics_series: Callable[[Any], dict[str, Any]] | None = None
    #: 解码时按哪个 domain 过滤 token（由项目经 `TargetProfile.token_domain` 提供）。
    token_domain: str = ""
    #: `(line) -> bool`：设备明说"不认识这条命令"时的判定规则（文本属于设备/项目）。
    unsupported_reply: Callable[[str], bool] | None = None
    #: `(line) -> fields | None`：项目自有身份格式；core 只消费通用字段，不认识其语法。
    identity_parser: Callable[[str], dict[str, Any] | None] | None = None
    #: ：这条身份协议的**回答行前缀**（例如 `@NAME `）。
    #: ⚠️ 空串 = 没有声明 ⇒ 设备嗅探**不向这个口发字节**（不猜这条协议长什么样）。
    #: 前缀是协议的文本，不是平台知识，所以它属于目标/项目。
    identity_reply_prefix: str = ""
    #: ：**设备侧可能自称的项目名**（来自 profile 的 `identity_hint`）。
    #: ⚠️ 裸机固件回的是 `"project":"stm32bm"`（它自己的线上名），而本机项目包键是 `baremetal`
    #: ⇒ 烧录后核验要与这条声明对表，否则一份**正确**的固件会被报成"身份不符"。
    #: 与 `web_catalog` 那条 `device_projects` 同源（都是 profile 的 `identity_hint`）。
    device_projects: tuple[str, ...] = field(default=())
    #: ：设备用自然语言回「不认识这条命令」时的若干特征串。
    #: 给空 = 这种回答会被归到「有回答但不是这条协议」（结论仍是身份未知，只是理由不同）。
    identity_unsupported_markers: tuple[str, ...] = field(default=())
    #: ：受控嗅探的"一问一答"实现
    #: `(port, baud, payload, max_bytes, wait_s) -> rx bytes`。
    #: ⚠️ 它只负责"把字发出去并读回来"：不重试、不说"不支持"、不碰复位线。
    #: 没装配 ⇒ 设备嗅探会如实报「这个口没有可用的传输实现」（不假装问过了）。
    sniff_exchange: Callable[[str, int, bytes, int, float], bytes] | None = None
    #: 这套装配用到的受信适配器 ID（诊断包与 `/api/state` 里如实列出）。
    adapter_ids: tuple[str, ...] = field(default=())

    # ------------------------------------------------------------------ 小工具
    def decoder_is_available(self) -> bool:
        """本机有没有可用的解码器（探测函数抛异常 ⇒ 视为不可用，不猜）。"""
        if self.decoder_probe is None:
            return False
        try:
            return bool(self.decoder_probe())
        except Exception:
            return False

    def known_build_ids(self) -> tuple[int, ...] | None:
        """本机这一份固件的构建号（`None` = 没装配 / 问不出来 / 一个都没有 ⇒ "没得核对"）。

        ⚠️ 三种回答必须分得开：`()` = 装了但确实没有；`None` = **没得核对**；
        受控用例的判据把 `None` 算 ERROR、把"不在表里"算 FAIL（不许把不知道当成错）。
        """
        if not callable(self.build_ids):
            return None
        try:
            values = self.build_ids()
        except Exception:
            return None
        out: list[int] = []
        for value in values or ():
            try:
                out.append(int(value) & 0xFFFFFFFF)
            except (TypeError, ValueError):
                continue
        return tuple(out)

    def device_is_unsupported_reply(self, line: str) -> bool:
        """设备是不是明说了"不认识这条命令"（没装配规则 ⇒ 一律 False）。"""
        if self.unsupported_reply is None:
            return False
        try:
            return bool(self.unsupported_reply(line))
        except Exception:
            return False

    def parse_project_identity(self, line: str) -> dict[str, Any] | None:
        """解析项目自有身份行（没装配或解析失败 ⇒ 不是身份，不影响采集）。"""
        if self.identity_parser is None:
            return None
        try:
            fields = self.identity_parser(line)
            return fields if isinstance(fields, dict) else None
        except Exception:
            return None

    def to_json(self) -> dict[str, Any]:
        """诊断用：**如实**说明这套装配提供了什么、没提供什么。"""
        return {
            "label": self.label,
            "platform": self.platform,
            "project": self.project,
            "token_domain": self.token_domain,
            # ：设备可能自称的项目名（来自 profile 的 `identity_hint`）。
            # 这是**声明**，不是这回真机上量到的值——诊断包只说"允许哪些名字"。
            "device_projects": list(self.device_projects),
            "adapters": list(self.adapter_ids),
            "provided": {
                "device_registry": self.device_registry is not None,
                "port_factory": self.port_factory is not None,
                "transport_probe": self.transport_probe is not None,
                "port_probe": self.port_probe is not None,
                "library_factory": self.library_factory is not None,
                "decoder_factory": self.decoder_factory is not None,
                "decoder_probe": self.decoder_probe is not None,
                "decoder_backend": self.decoder_backend is not None,
                "panic_parser": self.panic_parser is not None,
                "event_parser": self.event_parser is not None,
                "metrics_series": self.metrics_series is not None,
                "unsupported_reply": self.unsupported_reply is not None,
                "identity_parser": self.identity_parser is not None,
                "identity_probe": bool(self.identity_reply_prefix),
                "sniff_exchange": self.sniff_exchange is not None,
            },
            "decoder_available": self.decoder_is_available(),
        }


#: 没有装配任何插件：core 用这一份跑，所有"需要插件"的能力都如实降级。
NO_PLUGINS = PlatformPlugins(label="（未装配插件：只有平台核心）")

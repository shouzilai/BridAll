"""设备身份协议的**纯函数**（Phase 7.1 切片 5，需求 §6）。

这一层只做文本 ↔ 结构体的换算，不碰串口、不碰文件：

- `build_query()`：`INFO JSON` + 行结束符 → 要写出去的字节（及给人看的写法）；
- `parse_pwinfo()`：`@PWINFO {...}` 单行 → 字段字典（严格 JSON，坏行返回 `None`）；
- `normalize()`：只保留协议**白名单**字段，缺的补 `None`
  —— 需求 §6.2："字段允许为 null；不得为满足界面而伪造值"；
- `build_id32()`：把 `"0xb046a552"`（或数字）变成 32 位整数，供选库用。

⚠️ **来源必须分开标注**（§6.2）：设备自报（`@PWINFO`）与主机的启动横幅回退
（`BOOT build=0x…`）是两件不同的事。这个模块只产出"设备说了什么"，
"主机据此匹配到了哪份字典"由 `archive.py` 回答，不在这里混起来。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 需求 §6.1 定死的两个字符串：查询是 `INFO JSON`，回应以 `@PWINFO ` 开头。
IDENTITY_QUERY_TEXT = "INFO JSON"
PWINFO_PREFIX = "@PWINFO "

# 协议白名单（§6.2 的字段表）。表里没有的一律**不往 UI 传**：
# 一个我们并不理解的字段出现在界面上，比不显示更糟。
IDENTITY_FIELDS: tuple[str, ...] = (
    "schema",
    "mcu",
    "project",
    "software_version",
    "hardware_version",
    "git_commit",
    "git_dirty",
    "build_id",
    "elf_sha256",
    "dictionary_sha256",
    "protocols",
)

# 设备明确回答"不认识这条命令"时，我们把它当 `unsupported`（比"超时"更有信息）。
# ⚠️ **那个规则的文本属于设备/项目**（ / R2b）：核心里曾经写着
# `未知命令[:：]\s*(INFO|INFO JSON)` —— 那是这台 示例固件的中文台面，不是平台协议。
# 现在它由组合入口注入（`platform_plugins.PlatformPlugins.unsupported_reply`），
# core 只负责：拿到 true 就把身份状态从 `timeout` 收敛成 `unsupported`（）。

# 旧设备的回退通道：明文启动横幅里的 `build=0x…`（见 `dictionary.read_build_banner`）。
BOOT_BANNER_BUILD = re.compile(r"\bBOOT\b[^\n]*?\bbuild=0x([0-9a-fA-F]{1,8})\b")


def build_query(line_ending: object = None) -> tuple[bytes, str]:
    """`INFO JSON` 的实际字节（含行结束符）+ 给人看的写法。"""
    try:
        from .console_input import build_text_payload, render_text
    except ImportError:  # executed as a plain script
        from console_input import build_text_payload, render_text  # type: ignore[no-redef]

    payload = build_text_payload(IDENTITY_QUERY_TEXT, line_ending)
    return payload, render_text(IDENTITY_QUERY_TEXT, line_ending)


def parse_pwinfo(line: str) -> dict[str, Any] | None:
    """把一行 `@PWINFO {...}` 解析成字典；不是这条协议/JSON 坏了就返回 None。

    ⚠️ 严格：前缀必须对得上、后面必须是一个 JSON 对象。坏行只是"不是身份行"，
    **不要**抛异常 —— 设备可能在别的场合吐出像 `@PWINFO` 的文字。
    """
    text = str(line or "").rstrip("\r\n").strip()
    if not text.startswith(PWINFO_PREFIX):
        return None
    try:
        payload = json.loads(text[len(PWINFO_PREFIX):])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def normalize(fields: dict[str, Any] | None) -> dict[str, Any]:
    """只留白名单字段；缺的补 `None`（**不编值**）。"""
    source = fields or {}
    out: dict[str, Any] = {}
    for name in IDENTITY_FIELDS:
        value = source.get(name, None)
        if name == "git_dirty":
            out[name] = None if value is None else bool(value)
        elif name == "schema":
            try:
                out[name] = None if value is None else int(value)
            except (TypeError, ValueError):
                out[name] = None
        elif name == "protocols":
            # 不是对象就当"没提供"：宁可为 null，也不塞一个半懂的东西给界面。
            out[name] = value if isinstance(value, dict) else None
        elif value is None or isinstance(value, str):
            out[name] = value
        else:
            out[name] = str(value)
    return out


def build_id32(fields: dict[str, Any] | None) -> int | None:
    """从规范化字段里取出 32 位构建号（`"0x…"` / 十进制串 / 数字都认）。"""
    value = (fields or {}).get("build_id")
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip()
    try:
        return int(text, 16) & 0xFFFFFFFF if text.lower().startswith("0x") else int(text, 10) & 0xFFFFFFFF
    except ValueError:
        return None


def banner_build_id(text: str) -> int | None:
    """旧设备回退：从明文里找 `BOOT … build=0x…`。找不到返回 None（不猜）。"""
    match = BOOT_BANNER_BUILD.search(str(text or ""))
    if match is None:
        return None
    try:
        return int(match.group(1), 16) & 0xFFFFFFFF
    except ValueError:
        return None


def fields_with_values(fields: dict[str, Any] | None) -> list[str]:
    """哪些字段真的有值（UI 只展示这些，别的写"未提供"）。"""
    return [name for name, value in (fields or {}).items() if value not in (None, "", {})]

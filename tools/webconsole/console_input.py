"""命令框与终端直输的**输入模型** —— 全是纯函数，宿主可直接驱动（Phase 7.1 切片 3/4）。

需求 §4.1 的几条硬要求，这里逐条落实：

- 命令框支持**普通文本**与**纯十六进制**两种输入格式；
- 文本模式的行结束符可选 `CR` / `LF` / `CRLF`；默认值**属于设备/项目配置**（§8），
  不是平台策略 —— 所以默认值从设备登记里来，不写死在这里；
- HEX 模式按输入的十六进制字节**原样**发送：不加"高级模式"开关、不截断到 256 B；
- 非法十六进制**不得被悄悄修正或部分发送** —— 一律报错，一个字节也不发。

需求 §4.2（切片 4）的终端直输在这里的落点是 `build_keys_payload()`：

- 普通字符、回车、退格、Tab、Esc、方向键序列以及 `Ctrl+C` / `Ctrl+D` 等控制输入
  都要能发出去，并且**记录实际字节**；
- 终端直输**没有**主机侧 `↑/↓` 历史（方向键是字节，直接给设备）；
- 按键 → 字节的映射**只有这一份实现**：浏览器把按键描述（`{key, ctrl, alt, shift, meta}`）
  发过来，由这里换算字节。两份映射一定会漂，所以干脆不做第二份。

这个模块不认识任何业务命令：`STATUS` / `LS` / `DUMP` / `CLEAR YES` 在这里都只是普通字节。

⚠️ `bytes_hex` 是**证据**，`display` 只是**视图**：显示可以换一种写法、可以省略，
但字节必须与实际写进串口的那些完全一致（需求 §5.2）。
"""

from __future__ import annotations

import re

LINE_ENDINGS: dict[str, bytes] = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
LINE_ENDING_CHOICES = ("cr", "lf", "crlf")
# 没有设备配置时的兜底：CRLF 是终端世界里最常见的约定。
FALLBACK_LINE_ENDING = "crlf"

# 明确命名的控制字节（其余 1..26 按 <CTRL-X> 渲染）。
CONTROL_NAMES = {
    0x00: "<NUL>",
    0x08: "<BS>",
    0x09: "<TAB>",
    0x0A: "<LF>",
    0x0D: "<CR>",
    0x1B: "<ESC>",
}


def normalize_line_ending(value: object) -> str:
    """把任意输入收敛到三个合法值之一（不认识就用兜底）。"""
    key = str(value or "").strip().lower()
    return key if key in LINE_ENDINGS else FALLBACK_LINE_ENDING


def build_text_payload(text: str, line_ending: object = None) -> bytes:
    """文本模式的字节 = UTF-8 正文 + 行结束符。

    实际的结束符会写进 `io_events.jsonl`（需求：实际发送的行结束符必须写入记录）——
    所以这里**只**由 `line_ending` 决定，不做任何"聪明"的补充。
    """
    body = str(text if text is not None else "")
    if not body.strip():
        raise ValueError("命令不能为空")
    return body.encode("utf-8") + LINE_ENDINGS[normalize_line_ending(line_ending)]


def parse_hex(text: str) -> bytes:
    """严格解析十六进制输入：允许空格/逗号/分号分隔与 `0x` 前缀，其余一律报错。"""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise ValueError("十六进制输入不能为空")
    cleaned = re.sub(r"0[xX]", "", raw)
    cleaned = re.sub(r"[\s,;:]+", "", cleaned)
    if not cleaned:
        raise ValueError("十六进制输入不能为空")
    bad = sorted({char for char in cleaned if char not in "0123456789abcdefABCDEF"})
    if bad:
        raise ValueError("不是合法的十六进制字符：" + " ".join(bad))
    if len(cleaned) % 2:
        raise ValueError(f"十六进制必须是偶数个字符（现在 {len(cleaned)} 个）")
    return bytes.fromhex(cleaned)


def bytes_hex(payload: bytes) -> str:
    """证据形态：空格分隔的大写十六进制（与需求文档里的示例一致）。"""
    return " ".join(f"{byte:02X}" for byte in payload)


def _marker(code: int) -> str | None:
    """控制字节/字符的可读标记；普通字符返回 None（调用方原样输出）。"""
    if code in CONTROL_NAMES:
        return CONTROL_NAMES[code]
    if 1 <= code <= 26:
        return f"<CTRL-{chr(code + 64)}>"
    if code == 0x7F:
        return "<DEL>"
    return None


def render_bytes(payload: bytes) -> str:
    """可读标记：`<CTRL-C>`、`<ESC>`、`<BS>` …（需求 §5.2）。

    可打印 ASCII 原样显示；其余字节用 `<0x41>` 这种显式写法 —— **不猜**、不丢。
    """
    out: list[str] = []
    for byte in payload:
        marker = _marker(byte)
        out.append(marker if marker else (chr(byte) if 0x20 <= byte < 0x7F else f"<0x{byte:02X}>"))
    return "".join(out)


def render_text(text: str, line_ending: object = None) -> str:
    """文本模式的可读形态：把正文原样显示（含中文），只把控制字符标出来。

    ⚠️ 只有**文本**模式能这样渲染：它的字节本来就是 UTF-8 正文。
    HEX 模式一律走 `render_bytes()` —— 那里没有"正文"这回事。
    """
    body = str(text if text is not None else "")
    out: list[str] = []
    for char in body:
        marker = _marker(ord(char))
        out.append(marker if marker else char)
    return "".join(out) + render_bytes(LINE_ENDINGS[normalize_line_ending(line_ending)])


def build_payload(
    *, mode: str, text: str, line_ending: object = None
) -> tuple[bytes, str, str]:
    """一次输入的权威转换：返回 `(payload, display, source)`。

    - `mode="text"`：正文 + 所选行结束符；`source="command_text"`
    - `mode="hex"` ：严格解析出来的字节，**不加**任何行结束符；`source="command_hex"`

    非法输入直接抛 `ValueError`（调用方把它变成 400，并且不要发任何字节）。
    """
    kind = str(mode or "text").strip().lower()
    if kind == "hex":
        payload = parse_hex(text)
        return payload, render_bytes(payload), "command_hex"
    if kind != "text":
        raise ValueError(f"未知的输入模式 {mode!r}（可用的是 text / hex）")
    payload = build_text_payload(text, line_ending)
    return payload, render_text(text, line_ending), "command_text"


# ===================================================== 终端直输（切片 4，§4.2）
TERMINAL_SOURCE = "terminal"

#: 受控测试（LIVETEST-001）的命令来源：同一套写法、**另一个来源名** ——
#: 证据里一眼看得出这行是平台按用例定义发出去的，而不是人敲的。
LIVE_TEST_SOURCE = "live_test"

# 退格到底发哪个字节是**设备/项目配置**（§8），不是平台策略：
#   del = 0x7F（现代终端约定，xterm / gnome-terminal 都是它）
#   bs  = 0x08（老式/嵌入式 console 常见的 `\b`）
BACKSPACES: dict[str, bytes] = {"del": b"\x7f", "bs": b"\x08"}
BACKSPACE_CHOICES = ("del", "bs")
FALLBACK_BACKSPACE = "del"

# 只按下修饰键本身不产生字节 —— 这不是错误，只是"这次按没什么可发"。
MODIFIER_KEYS = frozenset(
    {
        "Shift",
        "Control",
        "Alt",
        "AltGraph",
        "Meta",
        "CapsLock",
        "NumLock",
        "ScrollLock",
        "Dead",
        "Compose",
        "Hyper",
        "Super",
        "OS",
        "Process",
        "Unidentified",
    }
)

# 特殊键的 xterm 兼容序列。三种形态：
#   final → `ESC [ <final>`（带修饰时 `ESC [ 1;<n><final>`）
#   tilde → `ESC [ <num> ~`（带修饰时 `ESC [ <num>;<n> ~`）
#   ss3   → `ESC O <final>`（带修饰时退化成 `ESC [ 1;<n><final>`）
CSI_FINAL_KEYS: dict[str, str] = {
    "ArrowUp": "A",
    "ArrowDown": "B",
    "ArrowRight": "C",
    "ArrowLeft": "D",
    "Home": "H",
    "End": "F",
}
CSI_TILDE_KEYS: dict[str, int] = {
    "Insert": 2,
    "Delete": 3,
    "PageUp": 5,
    "PageDown": 6,
    "F5": 15,
    "F6": 17,
    "F7": 18,
    "F8": 19,
    "F9": 20,
    "F10": 21,
    "F11": 23,
    "F12": 24,
}
SS3_KEYS: dict[str, str] = {"F1": "P", "F2": "Q", "F3": "R", "F4": "S"}

# `Ctrl+<字母>` = 0x01..0x1A。另外这几个（键盘上同一次按键的另一种叫法）
# 也是标准 C0 区间：Ctrl+@ = NUL、Ctrl+[ = ESC、Ctrl+\ = FS、Ctrl+] = GS、
# Ctrl+^ = RS、Ctrl+_ = US、Ctrl+? = DEL（需求里点名的 Ctrl+C / Ctrl+D 就在前者里）。
CTRL_CHAR_CODES: dict[str, int] = {
    "@": 0x00,
    "[": 0x1B,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "?": 0x7F,
    " ": 0x00,
}
# 数字键在 US 布局上就是被按下的那个符号：Ctrl+2 = Ctrl+@ = NUL，以此类推。
CTRL_DIGIT_CHARS: dict[str, str] = {
    "2": "@",
    "3": "[",
    "4": "\\",
    "5": "]",
    "6": "^",
    "7": "_",
    "8": "?",
}


def normalize_backspace(value: object) -> str:
    """把任意输入收敛到两个合法值之一（不认识就用兜底）。"""
    key = str(value or "").strip().lower()
    return key if key in BACKSPACES else FALLBACK_BACKSPACE


def _modifier_param(ctrl: bool, alt: bool, shift: bool, meta: bool) -> int:
    """xterm 的修饰键参数：`1 + Shift + 2*Alt + 4*Ctrl + 8*Meta`（1 表示"没按修饰键"）。"""
    return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0) + (8 if meta else 0)


def _modifier_label(ctrl: bool, alt: bool, shift: bool, meta: bool) -> str:
    return "".join(
        [
            "Ctrl+" if ctrl else "",
            "Alt+" if alt else "",
            "Shift+" if shift else "",
            "Meta+" if meta else "",
        ]
    )


def special_key_sequence(
    key: str, *, ctrl: bool = False, alt: bool = False, shift: bool = False, meta: bool = False
) -> bytes | None:
    """特殊键 → 终端序列（不认识就返回 None，**不猜**）。"""
    param = _modifier_param(ctrl, alt, shift, meta)
    if key in CSI_FINAL_KEYS:
        final = CSI_FINAL_KEYS[key]
        prefix = b"" if param == 1 else f"1;{param}".encode("ascii")
        return b"\x1b[" + prefix + final.encode("ascii")
    if key in CSI_TILDE_KEYS:
        number = str(CSI_TILDE_KEYS[key]).encode("ascii")
        tail = b"~" if param == 1 else f";{param}~".encode("ascii")
        return b"\x1b[" + number + tail
    if key in SS3_KEYS:
        final = SS3_KEYS[key].encode("ascii")
        if param == 1:
            return b"\x1bO" + final
        return b"\x1b[1;" + str(param).encode("ascii") + final
    return None


def build_key_payload(
    key: str,
    *,
    ctrl: bool = False,
    alt: bool = False,
    shift: bool = False,
    meta: bool = False,
    line_ending: object = None,
    backspace: object = None,
) -> tuple[bytes, str, str]:
    """**一次按键** → `(payload, display, label)`。

    - `payload` 是要写进串口的字节（空 bytes 表示这次按键不产生字节）；
    - `display` 是给人看的写法（`<CTRL-C>`、`<ESC>[A`、`a`）；
    - `label` 是按键本身的名字（`Ctrl+C`、`ArrowUp`），用于证据里的 `keys` 字段。

    不认识的键名（`Unidentified`、多媒体键、只按修饰键）一律**不产生字节**，
    绝不替用户猜一个字节出来 —— 调用方会把这批按键整体判为"不产生字节"。
    """
    name = str(key or "")
    if not name or name in MODIFIER_KEYS:
        return b"", "", ""
    label = _modifier_label(ctrl, alt, shift, meta) + name
    # 「Meta 发 ESC」是终端的老约定；Alt 在多数终端里也是同一个意思。
    escape_prefix = b"\x1b" if (alt or meta) else b""
    esc_text = "<ESC>" if escape_prefix else ""

    if name == "Enter":
        payload = LINE_ENDINGS[normalize_line_ending(line_ending)]
        return escape_prefix + payload, esc_text + render_bytes(payload), label
    if name == "Backspace":
        payload = BACKSPACES[normalize_backspace(backspace)]
        return escape_prefix + payload, esc_text + render_bytes(payload), label
    if name == "Tab":
        # Shift+Tab 是终端里的"往回跳"，它自己就是一个序列（`ESC [ Z`）。
        payload = b"\x1b[Z" if shift else b"\x09"
        return escape_prefix + payload, esc_text + render_bytes(payload), label
    if name == "Escape":
        return escape_prefix + b"\x1b", esc_text + "<ESC>", label

    # 特殊键（方向键、Home/End、PageUp/Down、Insert/Delete、F1～F12）：
    # 带修饰键时用 xterm 的参数形式，而不是"装作没按修饰键"。
    sequence = special_key_sequence(name, ctrl=ctrl, alt=alt, shift=shift, meta=meta)
    if sequence is not None:
        return sequence, render_bytes(sequence), label

    if len(name) == 1:
        char = name
        if ctrl:
            lookup = CTRL_DIGIT_CHARS.get(char, char)
            code = CTRL_CHAR_CODES.get(lookup)
            if code is None and lookup.isalpha():
                code = ord(lookup.upper()) - 64
            if code is None:
                return b"", "", ""  # 这个组合没有约定的 C0 字节：不猜
            payload = bytes([code])
            return escape_prefix + payload, esc_text + render_bytes(payload), label
        # 普通字符按 UTF-8 发（`event.key` 已经带上了 Shift 的效果）。
        marker = _marker(ord(char))
        return escape_prefix + char.encode("utf-8"), esc_text + (marker or char), label
    return b"", "", ""


def build_keys_payload(
    keys: object,
    *,
    line_ending: object = None,
    backspace: object = None,
) -> tuple[bytes, str, list[str]]:
    """一批按键 → `(payload, display, labels)`（需求 §4.2 / §5.2）。

    一批 = **一次 write()**：这样 `io_events.jsonl` 里一个 `tx` 事件对应一段真实字节，
    而 `keys` 字段说明这段字节是哪些按键产生的。整批都按不出字节时抛 `ValueError`
    （调用方变成 400，并且**一个字节也不写**）。
    """
    if not isinstance(keys, (list, tuple)) or not keys:
        raise ValueError("没有按键可以发送")
    payload = bytearray()
    display: list[str] = []
    labels: list[str] = []
    for item in keys:
        if not isinstance(item, dict):
            raise ValueError("按键描述必须是对象：{key, ctrl, alt, shift, meta}")
        chunk, text, label = build_key_payload(
            str(item.get("key") or ""),
            ctrl=bool(item.get("ctrl")),
            alt=bool(item.get("alt")),
            shift=bool(item.get("shift")),
            meta=bool(item.get("meta")),
            line_ending=line_ending,
            backspace=backspace,
        )
        if not chunk:
            continue
        payload += chunk
        display.append(text)
        labels.append(label)
    if not payload:
        raise ValueError("这些按键不产生字节（只按了修饰键，或键名不认识）")
    return bytes(payload), "".join(display), labels

"""Crash evidence: find the fault, then resolve its backtrace against the ELF.

A crash is the one moment where the tokenized log is useless. The ROM and the
panic handler print plain ASCII, the device reboots, and what remains is a
register dump plus a list of addresses. Those addresses only mean something when
they are resolved against the *exact* ELF that was flashed -- which is why the run
directory already records ``firmware.elf_sha256``.

This module turns "there was a crash" into "there was a crash, and here is the
function and line", and it keeps the raw invocation as evidence so a human can
reproduce the decoding by hand::

    xtensa-esp32-elf-addr2line -pfiaC -e .pio/build/demo/firmware.elf 0x400d1a2a

Three rules the implementation follows:

1. **Never invent a location.** An address addr2line cannot resolve is reported
   as unresolved, never as the nearest plausible symbol.
2. **Never guess the toolchain.** If the ELF or addr2line is missing, the finding
   says so. A crash report with no decoded frame is still evidence; a fabricated
   frame is worse than none.
3. **Stay useful without the toolchain.** Detection and address extraction are
   pure text work, so they are unit-testable on any machine.

The finding is JSON-ready and is archived as ``panic.json`` next to the raw
capture, so the verdict can be re-derived from the run directory alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 1

# Toolchain lookup. PlatformIO installs the Xtensa toolchain under the user's
# packages directory; the environment variable exists so a different toolchain
# (CI, another machine) can be pointed at without editing code.
ADDR2LINE_ENV = "PW_CONSOLE_ADDR2LINE"
ADDR2LINE_GLOBS = ("toolchain-xtensa-esp32*/bin/xtensa-esp32-elf-addr2line*",)

# (kind, marker) -- tried in order, the first match on a line decides its kind.
# These are the ASCII strings the Arduino core, ESP-IDF and the ROM actually
# print; they are stable across releases, unlike the surrounding wording.
FAULT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("panic", re.compile(r"Guru Meditation Error")),
    ("panic", re.compile(r"panic'ed \(")),
    ("panic", re.compile(r"abort\(\) was called")),
    ("panic", re.compile(r"Unhandled debug exception")),
    ("assert", re.compile(r"assert failed:")),
    ("watchdog", re.compile(r"Task watchdog got triggered")),
    ("watchdog", re.compile(r"Interrupt wdt timeout")),
    ("stack", re.compile(r"Stack canary watchpoint triggered")),
    ("stack", re.compile(r"A stack overflow in task")),
    ("heap", re.compile(r"CORRUPT HEAP")),
    ("brownout", re.compile(r"[Bb]rownout detector was triggered")),
    ("reboot", re.compile(r"^Rebooting\.\.\.$")),
)

# ESP-IDF ``esp_reset_reason()`` values that mean "the last boot was not clean".
# The numbers are the IDF enum, not the RTC one (the firmware reports
# ``esp_reset_reason()``), and the two disagree above 3 -- see .
CRASH_RESET_REASONS = {
    4: "PANIC",
    5: "INT_WDT",
    6: "TASK_WDT",
    7: "WDT",
    9: "BROWNOUT",
}
CLEAN_RESET_REASONS = {
    0: "UNKNOWN",
    1: "POWERON",
    2: "EXT_PIN",
    3: "SW",
    8: "DEEPSLEEP",
    10: "SDIO",
}

BACKTRACE_LINE = re.compile(r"^\s*Backtrace:\s*(?P<body>.*)$")
ADDR_PAIR = re.compile(r"0x[0-9a-fA-F]{8}:0x[0-9a-fA-F]{8}")
ADDR = re.compile(r"\b0x[0-9a-fA-F]{8}\b")
FRAME_LINE = re.compile(r"^\s*(?P<address>0x[0-9a-fA-F]+):\s*(?P<body>.*)$")
# With ``-p -i`` addr2line joins the inline chain onto one line, innermost first.
# Confirmed against the real toolchain by ``test_panic.py``: the variant that puts
# each inline frame on its own line is *not* what this invocation produces, but it
# is handled too, because the parser should not depend on a flag staying put.
INLINE_SEPARATOR = " (inlined by) "


@dataclass(frozen=True)
class FaultLine:
    """One plain-text line that says the firmware did not survive."""

    kind: str
    line_number: int
    offset: int
    text: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "line": self.line_number,
            "offset": self.offset,
            "text": self.text,
        }


@dataclass(frozen=True)
class Frame:
    """One resolved (or honestly unresolved) backtrace entry."""

    address: str
    symbol: str
    location: str
    resolved: bool
    inlined: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "symbol": self.symbol,
            "location": self.location,
            "resolved": self.resolved,
            "inlined": list(self.inlined),
        }


def _iter_lines(text: str) -> Iterable[tuple[int, int, str]]:
    """Yield ``(line_number, byte_offset, line_without_terminator)``.

    The offset mirrors ``app_events``: every finding can be pointed back at the
    primary evidence instead of being a floating interpretation.
    """
    offset = 0
    for index, raw in enumerate(text.splitlines(keepends=True), start=1):
        yield index, offset, raw.rstrip("\r\n")
        offset += len(raw.encode("utf-8", "replace"))


def fault_lines(text: str) -> list[FaultLine]:
    """Every line that indicates a crash, watchdog or brownout, in order."""
    found: list[FaultLine] = []
    for number, offset, line in _iter_lines(text):
        for kind, pattern in FAULT_MARKERS:
            if pattern.search(line):
                found.append(
                    FaultLine(kind=kind, line_number=number, offset=offset, text=line.strip())
                )
                break
    return found


def backtrace_bodies(text: str) -> list[tuple[int, str]]:
    """``(line_number, body)`` for every ``Backtrace:`` line in the text."""
    bodies: list[tuple[int, str]] = []
    for number, _, line in _iter_lines(text):
        match = BACKTRACE_LINE.match(line)
        if match is not None:
            bodies.append((number, match.group("body").strip()))
    return bodies


def extract_addresses(body: str) -> list[str]:
    """Pull code addresses out of one ``Backtrace:`` body.

    The ESP32 prints ``code:sp`` pairs. Only the code half is a backtrace entry;
    the stack pointer must not be mistaken for one, so when pairs are present the
    bare-address fallback is not used at all.
    """
    pairs = ADDR_PAIR.findall(body)
    if pairs:
        candidates = [pair.split(":", 1)[0].lower() for pair in pairs]
    else:
        candidates = [match.lower() for match in ADDR.findall(body)]
    return candidates


def collect_addresses(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """All backtrace addresses plus where each line came from."""
    addresses: list[str] = []
    sources: list[dict[str, Any]] = []
    for number, body in backtrace_bodies(text):
        found = extract_addresses(body)
        addresses.extend(found)
        sources.append({"line": number, "addresses": len(found), "text": f"Backtrace: {body}"})
    return addresses, sources


def parse_addr2line_output(output: str, addresses: Sequence[str]) -> list[Frame]:
    """Parse ``addr2line -pfiaC`` output into frames.

    Used with ``-a -p``, so every group starts with the address it belongs to.
    The inline chain is carried on the same line as `` (inlined by) `` segments;
    extra unprefixed lines (what plain ``-i`` produces) attach to the group too.
    Output is matched positionally against ``addresses`` so the decoded chain
    keeps the shape of the original one.
    """
    groups: list[tuple[str, list[str]]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = FRAME_LINE.match(line)
        if match is not None:
            groups.append((match.group("address").lower(), [match.group("body").strip()]))
        elif groups:
            groups[-1][1].append(line.strip())

    frames: list[Frame] = []
    for index, address in enumerate(addresses):
        if index >= len(groups):
            # addr2line produced fewer groups than addresses: report the missing
            # ones instead of silently returning a shorter chain.
            frames.append(
                Frame(address=address, symbol="", location="", resolved=False)
            )
            continue
        _, lines = groups[index]
        head = lines[0] if lines else ""
        symbol, location, inlined = _split_frame(head)
        inlined = list(inlined) + list(lines[1:])
        frames.append(
            Frame(
                address=address,
                symbol=symbol,
                location=location,
                resolved=_is_resolved(symbol, location),
                inlined=tuple(inlined),
            )
        )
    return frames


def _split_frame(line: str) -> tuple[str, str, tuple[str, ...]]:
    """Split one frame line into ``(symbol, location, inline chain)``."""
    segments = [segment.strip() for segment in line.split(INLINE_SEPARATOR) if segment.strip()]
    head = segments[0] if segments else ""
    symbol, location = _split_at(head)
    return symbol, location, tuple(segments[1:])


def _split_at(line: str) -> tuple[str, str]:
    """Split ``symbol at file:line``, or ``?? ??:0``, into its two halves."""
    marker = " at "
    if marker in line:
        symbol, location = line.split(marker, 1)
        return symbol.strip(), location.strip()
    # Unresolved frames come back as "?? ??:0": no " at ", but still two fields.
    tail = re.match(r"^(?P<head>.*\S)\s+(?P<tail>[^\s:]+:\d+)$", line.strip())
    if tail is not None:
        return tail.group("head").strip(), tail.group("tail").strip()
    return line.strip(), ""


def _is_resolved(symbol: str, location: str) -> bool:
    if not symbol or symbol.startswith("??"):
        return False
    if not location or location.startswith("??"):
        return False
    return True


def find_addr2line() -> Path | None:
    """Locate ``xtensa-esp32-elf-addr2line``, or return None."""
    override = os.environ.get(ADDR2LINE_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    packages = Path.home() / ".platformio" / "packages"
    for pattern in ADDR2LINE_GLOBS:
        for candidate in sorted(packages.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def reset_reason_name(value: Any) -> str:
    """Name an ``esp_reset_reason()`` value.

    Only the ESP-IDF numbering is accepted here. The ROM prints ``rst:0xN`` using
    the *RTC* numbering, where the same numbers mean different things (9 is
    RTCWDT_SYS there and BROWNOUT here, 15 is RTCWDT_BROWN_OUT there and does not
    exist here). Mixing the two silently mislabels crashes -- see .
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return f"未知({value!r})"
    if number in CRASH_RESET_REASONS:
        return CRASH_RESET_REASONS[number]
    if number in CLEAN_RESET_REASONS:
        return CLEAN_RESET_REASONS[number]
    return f"未登记({number})"


def is_crash_reset(value: Any) -> bool:
    """True when the ESP-IDF reset reason means the last boot was not clean."""
    try:
        return int(value) in CRASH_RESET_REASONS
    except (TypeError, ValueError):
        return False


def decode_addresses(
    addresses: Sequence[str],
    *,
    elf: Path,
    addr2line: Path | None = None,
    argv_builder: Callable[[Path, Sequence[str]], Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Run addr2line once for all addresses and describe what happened.

     / R3d：**命令行由调用方给**（一般是 `symbol` 适配器的 `symbolize()`，
    受控参数由代码生成）。没给就用本模块自己的 xtensa 形状 —— 独立使用（CLI、测试）
    时不需要先装一个适配器。
    """
    if argv_builder is None:
        if addr2line is None:
            return {
                "command": None,
                "exit_code": None,
                "stderr": "",
                "frames": [
                    Frame(address=address, symbol="", location="", resolved=False).to_json()
                    for address in addresses
                ],
                "unresolved": list(addresses),
                "error": "没有可用的符号化命令行（也没给工具路径）",
            }
        argv_builder = lambda elf_path, addrs: [  # noqa: E731  (默认形状，与历史一致)
            str(addr2line), "-pfiaC", "-e", str(elf_path), *addrs
        ]
    try:
        command = [str(part) for part in argv_builder(Path(elf), list(addresses))]
    except (OSError, ValueError, RuntimeError) as error:
        # 适配器说“我给不出命令”（没装工具链也是这一类）：这是一个要写清楚的状态。
        return {
            "command": None,
            "exit_code": None,
            "stderr": "",
            "frames": [
                Frame(address=address, symbol="", location="", resolved=False).to_json()
                for address in addresses
            ],
            "unresolved": list(addresses),
            "error": f"符号化命令不可用：{error}",
        }
    result: dict[str, Any] = {
        "command": command,
        "exit_code": None,
        "stderr": "",
        "frames": [],
        "unresolved": [],
        "error": None,
    }
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = f"addr2line 调用失败: {error!r}"
        result["frames"] = [
            Frame(address=address, symbol="", location="", resolved=False).to_json()
            for address in addresses
        ]
        result["unresolved"] = list(addresses)
        return result

    result["exit_code"] = completed.returncode
    result["stderr"] = (completed.stderr or "").strip()[:2000]
    frames = parse_addr2line_output(completed.stdout or "", addresses)
    result["frames"] = [frame.to_json() for frame in frames]
    result["unresolved"] = [frame.address for frame in frames if not frame.resolved]
    if completed.returncode != 0 and result["error"] is None:
        result["error"] = f"addr2line 退出码 {completed.returncode}"
    return result


def inspect(
    text: str,
    *,
    elf: Path | None = None,
    addr2line: Path | None = None,
    argv_builder: Callable[[Path, Sequence[str]], Sequence[str]] | None = None,
    expected_elf_sha256: str | None = None,
) -> dict[str, Any]:
    """Full crash finding for one capture. Always returns a dict, never raises.

    ``expected_elf_sha256`` comes from the run manifest. When it does not match
    the ELF on disk, the decoding used a different binary than the one that was
    flashed, so the frames are reported but explicitly marked as untrustworthy.

    ``argv_builder``（ / R3d）让调用方（编排）把**符号化命令行**交给受控适配器：
    `symbolize(elf, addresses) -> CommandSpec`。不给就用本模块自己的形状 ——
    这样单独用 `panic.py`（CLI / 宿主测试）仍然开箱可用。
    """
    faults = fault_lines(text)
    addresses, sources = collect_addresses(text)

    finding: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "verdict": "fault" if faults else "clean",
        "kinds": sorted({fault.kind for fault in faults}),
        "faults": [fault.to_json() for fault in faults],
        "addresses": addresses,
        "backtrace": {
            "sources": sources,
            "addresses": len(addresses),
            "unique_addresses": len(set(addresses)),
            "elf": str(elf) if elf else None,
            "elf_sha256": None,
            "elf_matches_firmware": None,
            "addr2line": str(addr2line) if addr2line else None,
            "command": None,
            "exit_code": None,
            "stderr": "",
            "frames": [],
            "unresolved": list(addresses),
            "error": None,
        },
    }
    backtrace = finding["backtrace"]

    if not addresses:
        if faults:
            backtrace["error"] = "发现故障，但捕获文本里没有 Backtrace 行，无法定位"
        return finding

    if elf is None or not Path(elf).is_file():
        backtrace["error"] = f"ELF 不存在，无法解码：{elf}"
        return finding
    if addr2line is None and argv_builder is None:
        backtrace["error"] = (
            f"未找到 xtensa-esp32-elf-addr2line（可用环境变量 {ADDR2LINE_ENV} 指定）"
        )
        return finding

    actual_sha256 = sha256_file(Path(elf))
    backtrace["elf_sha256"] = actual_sha256
    if expected_elf_sha256:
        backtrace["elf_matches_firmware"] = actual_sha256 == expected_elf_sha256

    decoded = decode_addresses(
        addresses, elf=Path(elf), addr2line=Path(addr2line) if addr2line else None,
        argv_builder=argv_builder,
    )
    backtrace.update(decoded)
    if backtrace["elf_matches_firmware"] is False:
        note = "⚠ ELF 哈希与 manifest 不一致，解码结果不对应本次烧录的固件"
        backtrace["error"] = f"{backtrace['error']}；{note}" if backtrace["error"] else note
    return finding


def summarize(finding: dict[str, Any] | None) -> dict[str, Any]:
    """Compact view for the manifest, the page and the run table.

    Defensive: this runs inside the orchestration layer, where an unexpected
    exception used to wedge a live run ().
    """
    if not isinstance(finding, dict):
        return {"verdict": "unknown", "kinds": [], "faults": 0, "addresses": 0,
                "resolved": 0, "unresolved": 0, "note": "无崩溃证据"}
    backtrace = finding.get("backtrace") or {}
    frames = backtrace.get("frames") or []
    resolved = [frame for frame in frames if isinstance(frame, dict) and frame.get("resolved")]
    faults = finding.get("faults") or []
    summary = {
        "verdict": finding.get("verdict", "unknown"),
        "kinds": list(finding.get("kinds") or []),
        "faults": len(faults),
        "addresses": int(backtrace.get("addresses") or 0),
        "resolved": len(resolved),
        "unresolved": len(backtrace.get("unresolved") or []),
        "elf_matches_firmware": backtrace.get("elf_matches_firmware"),
        "error": backtrace.get("error"),
        "note": "",
    }
    if faults and summary["resolved"]:
        first = next((frame for frame in frames if frame.get("resolved")), None)
        if first:
            summary["note"] = f"{first.get('symbol')} @ {first.get('location')}"
    elif not faults:
        summary["note"] = "无故障"
    return summary


__all__ = [
    "ADDR2LINE_ENV",
    "CLEAN_RESET_REASONS",
    "CRASH_RESET_REASONS",
    "FAULT_MARKERS",
    "SCHEMA_VERSION",
    "FaultLine",
    "Frame",
    "backtrace_bodies",
    "collect_addresses",
    "decode_addresses",
    "extract_addresses",
    "fault_lines",
    "find_addr2line",
    "inspect",
    "is_crash_reset",
    "parse_addr2line_output",
    "reset_reason_name",
    "sha256_file",
    "summarize",
]

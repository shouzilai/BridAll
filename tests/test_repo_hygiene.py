#!/usr/bin/env python3
"""仓库卫生：随发布的文本文件里**不许**出现下面两类东西。

**一、UTF-8 BOM**

- Python 读源文件时**容忍** BOM（PEP 263），所以 `run_all` / Doctor / 机器检查可以一路全绿，
  而文件里确实带着 BOM；
- 但把它当**文本**读的东西不容忍：`ast.parse(文件内容)` 当场 `SyntaxError`，
  grep / diff / 各种扫描器也会看到一个多余字符；
- 它还会被工具**重新生产**出来：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 无条件写 BOM。

**二、指向仓库外文档的编号**（形如 `PW-` + `I-` + 数字）

- 那种编号指向**仓库之外**的一份文档 ⇒ 读者看到一个**永远点不开**的悬空引用，
  而且它还暗示存在一份读者拿不到的文档；
- 教训本身要留下，但**编号不能留**（把理由写进注释，别只写一个编号）。“教训”应该是一句能独立读懂的话；
- 这条判据是补上的：此前的扫描器里没有这个模式，于是**一堆编号一路全绿地发布了出去**。

用法：`python tests/test_repo_hygiene.py`（退出码 0 = 干净）。
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"
#: 这类悬空编号的形状。⚠️ 拼着写：这个文件自己在扫描范围内，写成一个完整字面量会自我命中。
LEDGER_ID = re.compile("PW" + "-I-" + r"[0-9]+")
SKIP_DIRS = {".git", "__pycache__", ".pio", ".venv", "node_modules"}
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".css", ".html", ".htm", ".md",
    ".json", ".ini", ".cfg", ".toml", ".yml", ".yaml",
    ".txt", ".csv", ".ld", ".c", ".h",
}


def offenders() -> tuple[list[str], list[str]]:
    """`(带 BOM 的文件, 带悬空编号的文件)` —— 两个判据各查各的。"""
    bom_rows: list[str] = []
    id_rows: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        relative = str(path.relative_to(ROOT))
        if raw[:3] == BOM:
            bom_rows.append(relative)
        text = raw.decode("utf-8", "replace")
        for index, line in enumerate(text.splitlines(), start=1):
            if LEDGER_ID.search(line):
                id_rows.append(f"{relative}:{index}: {line.strip()[:110]}")
    return bom_rows, id_rows


def main() -> int:
    bom_rows, id_rows = offenders()
    if bom_rows:
        print("FAIL 这些文本文件以 UTF-8 BOM 开头（Python 容忍，别的工具不容忍）：")
        for name in bom_rows:
            print(f"      {name}")
    if id_rows:
        print("FAIL 这些行出现了指向仓库外文档的编号：")
        for row in id_rows:
            print(f"      {row}")
        print("      改法：删掉编号，把教训写成一句能独立读懂的话。")
    if bom_rows or id_rows:
        print("RESULT: 仓库卫生检查未通过")
        return 1
    print("PASS 随发布的文本文件里没有 UTF-8 BOM，也没有悬空编号")
    print("RESULT: 仓库卫生检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

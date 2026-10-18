#!/usr/bin/env python3
"""提交信息门禁：单行中文、整行不超过 60 个 Unicode 字符、结尾不加句号。

```powershell
python tools\check_commit_message.py <message-file>        # 单条（commit-msg 钩子用它）
python tools\check_commit_message.py --range main..HEAD    # 一段提交区间
```

规则（与 `CONTRIBUTING.md` 同一份口径）：

- 首行格式 `<类型>(<范围>): <中文动作说明>`，类型见 `ALLOWED_TYPES`；
- **整条只有这一行**，不写正文；
- 整行不超过 60 个 Unicode 字符（中文、字母、数字、标点都按一个算）；
- 结尾不加句号；
- 不使用 “update”“misc”“WIP” 这类说不清意图的词。

Git 自动生成的合并提交与回滚提交（`Merge ...` / `Revert ...`）保留标准格式，不受本规则限制。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_CHARS = 60
ALLOWED_TYPES = ("feat", "fix", "docs", "test", "refactor", "chore", "build")
HEADER = re.compile(r"^(?P<type>[a-z]+)\((?P<scope>[a-z0-9._-]+)\): (?P<what>\S.*)$")
VAGUE = ("update", "misc", "wip", "fixup", "tmp")
GENERATED = re.compile(r"^(Merge |Revert )")
CJK = re.compile(r"[\u4e00-\u9fff]")


def problems_for(message: str) -> list[str]:
    """一条提交信息的问题清单（空列表 = 通过）。纯函数：测试直接喂它。"""
    text = str(message or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return ["提交信息为空"]
    lines = [line for line in text.split("\n")]
    found: list[str] = []
    subject = lines[0].strip()
    if GENERATED.match(subject):
        return []
    if len(lines) > 1 and any(line.strip() for line in lines[1:]):
        found.append("只能有一行：不要写正文")
    if len(subject) > MAX_CHARS:
        found.append(f"整行 {len(subject)} 个字符，超过 {MAX_CHARS}")
    if subject.endswith(("。", ".")):
        found.append("结尾不加句号")
    match = HEADER.match(subject)
    if not match:
        found.append("格式应为 <类型>(<范围>): <中文动作说明>（范围必填）")
    else:
        if match.group("type") not in ALLOWED_TYPES:
            found.append(f"类型只能是 {'/'.join(ALLOWED_TYPES)}")
        what = match.group("what")
        if not CJK.search(what):
            found.append("动作说明要用中文")
        lowered = what.lower()
        if any(word == lowered or lowered.startswith(word + " ") for word in VAGUE):
            found.append("动作说明要说清做了什么（不要 update/misc/WIP）")
    return found


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _range_messages(rev_range: str) -> list[tuple[str, str]]:
    completed = subprocess.run(
        ["git", "log", f"--format=%H%x00%B%x1e", rev_range],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise SystemExit(f"读不到提交区间 {rev_range}：{completed.stderr.strip()[:200]}")
    rows: list[tuple[str, str]] = []
    for chunk in completed.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        sha, _, body = chunk.partition("\x00")
        rows.append((sha.strip(), body))
    return rows


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：check_commit_message.py <message-file> | --range <base>..<head>")
        return 2
    failures: list[str] = []
    if argv[0] == "--range":
        if len(argv) < 2:
            print("--range 需要 <base>..<head>")
            return 2
        for sha, body in _range_messages(argv[1]):
            problems = problems_for(body)
            if problems:
                failures.append(f"{sha[:8]} {body.splitlines()[0][:60]} → {'；'.join(problems)}")
    else:
        path = Path(argv[0])
        if not path.is_file():
            print(f"读不到文件：{path}")
            return 2
        problems = problems_for(_read(path))
        if problems:
            failures.append(f"{path.name} → {'；'.join(problems)}")
    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print("PASS 提交信息符合单行六十字符规则")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

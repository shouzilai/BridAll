#!/usr/bin/env python3
"""提交信息规则的回归（需求 §8.6 第 2 条）。

规则本身在 `tools/check_commit_message.py`（纯函数 `problems_for`），这里直接喂它：
一条合格的提交信息必须过，五种常见写法必须被拒。**不联网、不碰 Git**。
"""

from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_commit_message as gate  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}" + (f"：{detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    print("提交信息门禁自测（单行、中文、六十字符）")
    check(
        "合格的提交信息通过",
        gate.problems_for("feat(webconsole): 事件到五窗的归属改为显式映射") == [],
        str(gate.problems_for("feat(webconsole): 事件到五窗的归属改为显式映射")),
    )
    check(
        "带范围与不带范围都接受",
        gate.problems_for("fix: 历史列表只读工件名不算内容哈希") == []
        and gate.problems_for("docs(repo): 补充贡献规范") == [],
        "",
    )
    check(
        "超过六十个字符被拒",
        any("超过" in item for item in gate.problems_for("feat(webconsole): " + "很长的说明" * 12)),
        "",
    )
    check(
        "写正文被拒",
        any("只能有一行" in item for item in gate.problems_for("feat: 加一个能力\n\n正文说明")),
        "",
    )
    check(
        "结尾句号被拒",
        any("句号" in item for item in gate.problems_for("fix: 修掉一个问题。")),
        "",
    )
    check(
        "非中文动作说明被拒",
        any("中文" in item for item in gate.problems_for("fix: fix the thing")),
        "",
    )
    check(
        "未登记的类型被拒",
        any("类型只能是" in item for item in gate.problems_for("wip(webconsole): 临时改一下")),
        "",
    )
    check(
        "合并与回滚提交不受单行限制",
        gate.problems_for('Merge branch "main" into feature') == []
        and gate.problems_for('Revert "feat: 加一个能力"\n\nThis reverts commit abc.') == [],
        "",
    )
    check(
        "仓库里提供钩子脚本",
        (ROOT / ".githooks" / "commit-msg").is_file(),
        "缺 .githooks/commit-msg",
    )
    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} 条失败 —— " + "；".join(FAILURES))
        return 1
    print("RESULT: 提交信息门禁自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""跑一遍这个仓库里**不需要硬件**的全部检查。

```powershell
python tests\\run_all.py
```

退出码：0 = 全绿；1 = 有失败。

它跑的是这几样东西：

1. `tests/test_hwops_contract.py` —— 核心契约套件（假工具 + 假串口）；
2. `tests/target_contract_harness.py` —— 共用夹具的自检；
3. 每个 `tests/test_*_target.py` —— 参考适配的契约测试；
4. `tests/test_web_acceptance.py` —— Web 验收（页面 / 静态资源 / 三种模式 / 历史回放 / 嗅探 /
   连接生命周期 / 空候选口 / 未知身份 / 串口参数 / 公共传输实现与失败面；无硬件、无 pyserial）；
5. `tests/test_repo_hygiene.py` —— 仓库卫生（随发布的文本文件不许带 BOM）；
6. `tests/browser_e2e.py` —— **真实浏览器** E2E（桌面 + 窄屏；需本机 node + playwright，
   没有就如实记 `SKIP` —— **跳过不算通过**，汇总行会把它单独说出来）；
7. `tools/hwops.py check` 与 `tools/hwops.py opensource-audit` —— 机器检查与开源边界。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"


def _run(label: str, argv: list[str]) -> str:
    """跑一项，返回 `OK` / `SKIP` / `FAIL`。

    ⚠️ `SKIP` 只给“本机缺工具”那一种情形（目前只有浏览器 E2E），而且它**由子进程自己说**
    （输出里那行 `SKIP:`）；没工具就静默变成“通过”是把不可验证的东西算成已验证。
    """
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        argv, cwd=str(ROOT), env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    verdict = "OK" if completed.returncode == 0 else "FAIL"
    if verdict == "OK" and "SKIP:" in output:
        verdict = "SKIP"
    print(f"[{verdict}] {label}（退出码 {completed.returncode}）")
    if verdict == "FAIL":
        tail = (completed.stdout or "").strip().splitlines()[-12:]
        for line in tail:
            print(f"      {line}")
    elif verdict == "SKIP":
        for line in output.splitlines():
            if "SKIP:" in line:
                print(f"      {line.strip()}")
    return verdict


def main() -> int:
    scripts = [
        ("核心契约套件", [sys.executable, str(ROOT / "tests" / "test_hwops_contract.py")]),
        ("共用夹具自检", [sys.executable, str(ROOT / "tests" / "target_contract_harness.py")]),
    ]
    scripts.extend(
        (f"参考适配：{path.stem}", [sys.executable, str(path)])
        for path in sorted((ROOT / "tests").glob("test_*_target.py"))
    )
    # Web 验收（需求 §6.8/§6.9/§6.10/§6.11）：与上面几项一样是**必绿**入口。
    scripts.append(
        ("Web 验收", [sys.executable, str(ROOT / "tests" / "test_web_acceptance.py")])
    )
    # 仓库卫生：随发布的文本文件不许带 UTF-8 BOM —— “能跑”不等于“干净”。
    scripts.append(
        (
            "仓库卫生",
            [sys.executable, str(ROOT / "tests" / "test_repo_hygiene.py")],
        )
    )
    # 提交信息门禁：单行、中文、整行不超过 60 个 Unicode 字符。
    scripts.append(
        (
            "提交信息门禁",
            [sys.executable, str(ROOT / "tests" / "test_commit_messages.py")],
        )
    )
    # 真实浏览器 E2E（需求 §6.9）：桌面 + 窄屏。需要 node + playwright，没有就记 SKIP。
    scripts.append(
        (
            "真实浏览器 E2E（桌面 + 窄屏）",
            [sys.executable, str(ROOT / "tests" / "browser_e2e.py")],
        )
    )
    labels = [label for label, _argv in scripts]
    labels.extend(["机器检查", "开源边界（全仓）"])
    results = [_run(label, argv) for label, argv in scripts]
    results.append(
        _run("机器检查", [sys.executable, str(TOOLS_DIR / "hwops.py"), "check"])
    )
    results.append(
        _run("开源边界（全仓）",
             [sys.executable, str(TOOLS_DIR / "hwops.py"), "opensource-audit", "--scope", "all"])
    )
    passed = [label for label, item in zip(labels, results) if item == "OK"]
    failed = [label for label, item in zip(labels, results) if item == "FAIL"]
    skipped = [label for label, item in zip(labels, results) if item == "SKIP"]
    summary = f"\n{len(passed)}/{len(results)} 项通过"
    if failed:
        summary += f"；失败 {len(failed)} 项（{'、'.join(failed)}）"
    if skipped:
        summary += f"；跳过 {len(skipped)} 项（{'、'.join(skipped)}）—— 跳过不算通过"
    print(summary)
    # ⚠️ 跳过的必绿项（例如没装 node+playwright 时的真实浏览器 E2E）**不算通过**：
    # 分母照算、退出码非 0。否则“这一版整个浏览器门禁没跑”会被当成绿灯推出去。
    return 0 if not failed and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())

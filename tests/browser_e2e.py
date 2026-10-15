#!/usr/bin/env python3
"""**真实浏览器 E2E**（桌面 + 窄屏）：把真页面放在真 Chromium 里点一遍。

需求 §6.9 要的是“桌面与窄屏的真实浏览器验收”。这条以前只有我手工在集成浏览器里看过一次，
而那个浏览器**忽略**视口尺寸（上下文是 `viewport: null`），所以“窄屏”根本量不到 ——
那份证据是散的、也不可机械复跑。这个入口把它补上：

```text
tests/browser_e2e.py        起真服务（随机端口，注入假串口）→ 调 node 驱动 → 汇总 PASS/FAIL
tests/browser_e2e.js        在真 Chromium 里点真 DOM（桌面 1280×800 + 窄屏 390×844）
```

⚠️ **依赖是"有就用、没有就如实说"**：驱动要本机有 `node` + `playwright` 模块。两份都没有时
打 `SKIP:` 并以 0 退出 —— 但**跳过不等于通过**：`tests/run_all.py` 会把它单独记成 `[SKIP]`，
不会混进"全绿"里。

⚠️ 串口是**注入的假串口**（与 `test_web_acceptance.py` 同一套夹具，不另写一份）：
所以这台机器上不需要硬件、也不需要 pyserial，"连上 / 收发"仍然是真的走页面 → 服务端 →
连接状态机 → 串口 owner 那一条路。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONSOLE_DIR = ROOT / "tools" / "webconsole"
TARGETS_DIR = ROOT / "targets"
for _directory in (str(HERE), str(CONSOLE_DIR), str(ROOT / "tools"), str(TARGETS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import bench_setup  # noqa: E402
import server as console_server  # noqa: E402
import test_web_acceptance as fixtures  # noqa: E402  (同一套假串口夹具)
from fake_target import FakePort  # noqa: E402

#: 页面上要看到的那个通信口。
#: - `PORT`：**系统枚举到、台架没点名**的口 ⇒ 零配置那条路（需求 §6.11/§6.12）；
#: - `PORT_DECLARED`：**台架点了名**的口 ⇒ 适配后的正规路径（终端收发与解码走它）。
PORT = fixtures.PORT_GENERIC
PORT_DECLARED = "socket://127.0.0.1:8775"
DRIVER = HERE / "browser_e2e.js"


class EchoPort(FakePort):
    """受控假设备：身份查询回一行 `@PWINFO`，其它**整行**回显成 `[I][echo] …`。

    ⚠️ 回显按**行**而不是按“一次 write”：页面上那条“点正文直接敲键”的路
    （W6-4）是**一个键一次 write** 的，按 write 回显只会得到一串单字符回显，
    而“我发了一行、它回了一行”才是终端该有的样子。
    """

    def __init__(self, name: str, baud: int, identity: bytes = b"") -> None:
        super().__init__(name, baud)
        self._identity = bytes(identity)
        self._buffer = bytearray()

    def write(self, data) -> int:
        count = super().write(data)
        payload = bytes(data)
        self._buffer.extend(payload)
        if b"@PWINFO" in payload and self._identity:
            self.feed(self._identity)
        while True:
            found = re.search(rb"[\r\n]", self._buffer)
            if found is None:
                break
            line = bytes(self._buffer[: found.start()])
            del self._buffer[: found.end()]
            text = line.decode("utf-8", "replace").strip()
            if text and "@PWINFO" not in text:
                self.feed(f"[I][echo] {text}\r\n".encode("utf-8"))
        return count


class EchoTransport(fixtures.FakeTransport):
    """假串口 + 假枚举：`/api/sniff`、连接与终端收发都走这一份。"""

    def port_factory(self, name: str, baud: int) -> EchoPort:
        handle = EchoPort(str(name), int(baud), self.answers.get(str(name), b""))
        self.opened.append(str(name))
        self.handles[str(name)] = handle
        return handle


def find_playwright() -> tuple[str, str, str]:
    """`(node, playwright 所在模块根, 找不到时的原因)`。

    ⚠️ 里面**不写死任何绝对路径**（开源边界扫描会拦，而且换台机器就不成立）：
    最准的一手是问 npm 自己（`npm root -g`），其余几条都是从**环境变量**拼出来的候选位置。
    """
    node = shutil.which("node")
    if not node:
        return "", "", "本机没有 node"
    candidates: list[Path] = []
    configured = os.environ.get("PW_PLAYWRIGHT_MODULE", "").strip()
    if configured:
        candidates.append(Path(configured))
    npm = shutil.which("npm")
    if npm:
        try:
            done = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            if done.returncode == 0 and done.stdout.strip():
                candidates.append(Path(done.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    home = Path(os.environ.get("USERPROFILE", "") or str(Path.home()))
    candidates.append(home / "nodejs" / "node_modules")
    candidates.append(Path(os.environ.get("APPDATA", "") or ".") / "npm" / "node_modules")
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(key, "").strip()
        if root:
            candidates.append(Path(root) / "nodejs" / "node_modules")
    candidates.append(ROOT / "node_modules")
    for candidate in candidates:
        if (candidate / "playwright").is_dir():
            return node, str(candidate), ""
    return node, "", "本机 node 里没有 playwright 模块（npm i -g playwright）"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    print("真实浏览器 E2E（桌面 1280×800 + 窄屏 390×844）")
    node, module_root, reason = find_playwright()
    if not node or not module_root:
        print(f"SKIP: {reason}")
        print("      装上之后重跑即可：npm i -g playwright && npx playwright install chromium")
        print("RESULT: 跳过（没有 node + playwright，这一项**没有**验证）")
        return 0

    work = Path(tempfile.mkdtemp(prefix="pw-browser-e2e-"))
    shots = work / "shots"
    # ⚠️ 台架一律临时：不读本机任何配置。只点名**一个**口（另一个留给系统枚举）。
    bench_file = work / "bench.local.json"
    bench_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "tools": {},
                "projects": {},
                "devices": {"stm32a": {"port": PORT_DECLARED}},
            }
        ),
        encoding="utf-8",
    )
    bench_setup.install_from(cli_path=str(bench_file))
    fake = EchoTransport(
        {PORT: fixtures.IDENTITY_LINE, PORT_DECLARED: fixtures.IDENTITY_LINE},
        enumerated=(PORT,),
    )
    assembled = console_server.build_live_assembly(
        work / "runs",
        sessions_dir=work / "ses",
        imports_dir=work / "imp",
        transport=fake.as_transport(),
        sniff_dir=work / "sniff",
    )
    running = fixtures.Running(assembled)
    url = f"http://127.0.0.1:{running.port}/"
    print(f"     服务：{url}（装配自检：{assembled.problems() or '空'}）")
    try:
        environment = dict(os.environ)
        environment["NODE_PATH"] = module_root
        environment["PW_E2E_PORT"] = PORT
        environment["PW_E2E_DECLARED_PORT"] = PORT_DECLARED
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [node, str(DRIVER), url, str(shots)],
            cwd=str(ROOT), env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        print(completed.stdout.rstrip())
        if completed.stderr.strip():
            print("----- 驱动 stderr -----")
            print(completed.stderr.rstrip()[:2000])
        if completed.returncode != 0:
            print(f"      截图留在：{shots}")
        return completed.returncode
    except subprocess.TimeoutExpired:
        print("FAIL 浏览器 E2E 超时（15 分钟）")
        print("RESULT: 1 条失败 —— 浏览器 E2E 超时")
        return 1
    finally:
        running.close()


if __name__ == "__main__":
    raise SystemExit(main())

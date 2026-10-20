#!/usr/bin/env python3
"""Web 验收（无硬件、无 pyserial）：首页 / 静态资源 / 三模式 / 历史回放 / 嗅探 / 连接生命周期。

需求 §6.8 / §6.9 / §6.10 / §6.11 的**自动化**那一半：

| 这一条测试盖住 | 怎么盖的 |
| --- | --- |
| 按公开 README 启动后页面能开 | 真起 `ConsoleServer`（随机端口），GET `/`、`/doctor`、`/static/*` |
| 三种模式都在 | 首页 HTML 里三个入口 + 三个工作区都在 |
| 无硬件演示不读仓库外文档 | 演示装配跑的是仓库自带的 `demo/` 合成证据（历史回放那两条） |
| `POST /api/sniff` 有结论 | 候选口为空 ⇒ 202 + 空卡片 + **可操作的说明**（不是 503） |
| 选择 → 连接 → 断开 → 重连 | 卡片引用路径全走一遍（与页面同一条路） |
| 身份未知设备也能先连接 | 静默设备：连上后 `transparent`，理由说得出 |
| 已连接的口不被嗅探抢占 | 连接状态下再嗅探：那个口**没有重开、没有写字节** |
| 只读目标不许跑自动验证 | `/api/run` 被拒，理由点名“策略不允许” |

⚠️ 传输是**注入的假串口**（`FakePort` 系）：这台机器上没有硬件、也没有 pyserial，
所以"能连上"这件事必须靠一个可控的内存串口来证明 —— 而**页面与 API 那条路一个字没改**。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONSOLE_DIR = ROOT / "tools" / "webconsole"
TARGETS_DIR = ROOT / "targets"
for _directory in (str(CONSOLE_DIR), str(ROOT / "tools"), str(TARGETS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

import bench_config  # noqa: E402
import bench_setup  # noqa: E402
import bench_target  # noqa: E402
import device_registry  # noqa: E402
import fake_target  # noqa: E402
import server as console_server  # noqa: E402
import sniff_service  # noqa: E402
import stm32_target  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"{mark} {name}" + (f"：{detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return bool(condition)


# --------------------------------------------------------------------- 假传输
IDENTITY_LINE = b"@PWINFO project=stm32f103-demo build_id=0x1234abcd\r\n"
PORT_ANSWERS = "socket://127.0.0.1:8775"
PORT_SILENT = "socket://127.0.0.1:8776"
#: **系统枚举到、台架里没点名**的口 —— 走“通用串口”那条路（需求 §6.11/§6.12）。
PORT_GENERIC = "socket://127.0.0.1:8799"


class RespondingPort(fake_target.FakePort):
    """会（或不会）答身份的内存串口：`write` 出去的查询拿回一行 `@PWINFO`。"""

    def __init__(self, name: str, baud: int, reply: bytes = b"") -> None:
        super().__init__(name, baud)
        self._reply = bytes(reply)
        self.writes: list[bytes] = []

    def write(self, data) -> int:
        payload = bytes(data)
        self.writes.append(payload)
        count = super().write(payload)
        if self._reply and b"@PWINFO" in payload:
            self.feed(self._reply)
        return count


class FakeTransport:
    """四件传输实现（打开 / 占用探测 / 受控一问一答 / **系统串口枚举**），全部在内存里。"""

    def __init__(self, answers: dict[str, bytes], *, enumerated: tuple[str, ...] = ()) -> None:
        self.answers = dict(answers)
        self.enumerated = tuple(enumerated)
        self.opened: list[str] = []
        self.handles: dict[str, RespondingPort] = {}
        self.probes: list[str] = []
        self.exchanges: list[str] = []

    def port_factory(self, name: str, baud: int):
        self.opened.append(str(name))
        handle = RespondingPort(str(name), int(baud), self.answers.get(str(name), b""))
        self.handles[str(name)] = handle
        return handle

    def port_probe(self, name: str) -> str:
        self.probes.append(str(name))
        # 枚举得到的口都当“空闲”：这是替身的声明，不是对真机的断言。
        if str(name) in self.answers or str(name) in self.enumerated:
            return device_registry.ACCESS_FREE
        return device_registry.ACCESS_UNKNOWN

    def exchange(self, name, baud, payload, max_bytes, wait_s) -> bytes:
        del baud, payload, max_bytes, wait_s
        self.exchanges.append(str(name))
        return self.answers.get(str(name), b"")

    def list_ports(self) -> tuple[tuple[str, str], ...]:
        return tuple((port, f"假串口 {index}") for index, port in enumerate(self.enumerated))

    def as_transport(self) -> bench_target.Transport:
        return bench_target.Transport(
            port_factory=self.port_factory,
            port_probe=self.port_probe,
            exchange=self.exchange,
            list_ports=self.list_ports,
            note="注入的假串口（无硬件验收）",
            label="测试假串口",
        )


# --------------------------------------------------------------------- 服务
class Running:
    """把服务跑在随机端口上，并给一个**绕开本机代理**的 HTTP 客户端。"""

    def __init__(self, assembled) -> None:
        self.server = console_server.ConsoleServer(
            ("127.0.0.1", 0),
            console_server.ConsoleHandler,
            assembled.runners,
            assembled.observe,
            platform=assembled.platform_facts,
            catalog=assembled.catalog,
            sniff=assembled.sniff,
            assembly=assembled,
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def close(self) -> None:
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001 - 关不掉不影响结论（进程会结束）
            pass
        try:
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {"text": raw})
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"error": raw[:200]}

    def text(self, path: str) -> tuple[int, str]:
        status, payload = self.call("GET", path)
        return status, str(payload.get("text") or "")


def wait_for(predicate, *, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------- 用例
def check_demo_pages() -> None:
    """演示装配：页面 / 静态资源 / 三模式 / 历史回放（需求 §6.8、§6.9）。"""
    print("\n[1] 演示装配：页面、静态资源、三种模式、历史回放")
    console_server.use_demo_data(True)
    assembled = console_server.build_demo_assembly()
    running = Running(assembled)
    try:
        check("装配自检为空", not assembled.problems(), str(assembled.problems()))
        status, payload = running.call("GET", "/api/state")
        check("GET /api/state = 200", status == 200, str(status))
        check("状态里有装配", bool(payload.get("assemblies")), json.dumps(payload.get("assemblies"), ensure_ascii=False)[:120])

        status, html = running.text("/")
        check("GET / = 200", status == 200, str(status))
        for name in ("自动验证", "实时观察", "离线诊断"):
            check(f"首页有「{name}」入口", name in html)
        for pane in ("logWorkspace", "autoPanel", "offlinePanel"):
            check(f"首页有 {pane} 工作区", pane in html)

        for asset in ("/static/app.js", "/static/style.css", "/static/index.html"):
            status, text = running.text(asset)
            check(f"GET {asset} = 200 且有内容", status == 200 and len(text) > 100, f"{status}/{len(text)}")

        status, doctor = running.text("/doctor")
        check("GET /doctor = 200", status == 200 and "体检" in doctor, str(status))

        status, payload = running.call("GET", "/api/runs")
        runs = payload.get("runs") or payload.get("items") or []
        ids = [str(row.get("run_id") or row.get("id") or "") for row in runs] if isinstance(runs, list) else []
        check("历史 run 列表里有演示 run", any("20260101-120000-demo-demo" in rid for rid in ids), str(ids)[:200])
        demo_id = next((rid for rid in ids if "20260101-120000-demo-demo" in rid), "")
        if demo_id:
            status, detail = running.call("GET", f"/api/runs/{demo_id}")
            check("演示 run 详情可读", status == 200, str(status))
        status, sessions = running.call("GET", "/api/observe/sessions")
        rows = sessions.get("sessions") or []
        check(
            "会话列表里有演示会话",
            any("20260101-120500-live-fake" in str(row.get("session_id") or "") for row in rows),
            str(rows)[:200],
        )
    finally:
        running.close()
    console_server.use_demo_data(False)


def check_live_without_bench() -> None:
    """**零配置路径**（没有 `bench.local.json`、没有任何 Adapter）：系统枚举到的口就能用（需求 §6.10/§6.11）。"""
    print("\n[2] 零配置：只靠系统串口枚举也能选、连、收发")
    bench_setup.install_from(root=Path(tempfile.mkdtemp()))  # 有台架文件，但一格都不填
    work = Path(tempfile.mkdtemp())
    fake = FakeTransport({}, enumerated=(PORT_GENERIC,))
    assembled = console_server.build_live_assembly(
        work / "runs",
        sessions_dir=work / "ses",
        imports_dir=work / "imp",
        transport=fake.as_transport(),
        sniff_dir=work / "sniff",
    )
    running = Running(assembled)
    try:
        check("装配自检为空", not assembled.problems(), str(assembled.problems()))
        status, payload = running.call("GET", "/api/state")
        check("GET /api/state = 200", status == 200, str(status))
        ports = [str(spec.port) for row in assembled.assemblies for spec in row.pipeline.devices.specs()]
        check("台架一格都没填", ports == ["", ""], str(ports))

        status, payload = running.call("POST", "/api/sniff", {})
        check("POST /api/sniff = 202（不是 503）", status == 202, str(status))
        sniff = payload.get("sniff") or {}
        cards = list(sniff.get("cards") or [])
        check("系统枚举到的口出现在卡片里", [str(card.get("port")) for card in cards] == [PORT_GENERIC],
              json.dumps([card.get("port") for card in cards], ensure_ascii=False))
        notes = " ".join(str(item) for item in (sniff.get("notes") or ()))
        check("说明里讲了候选口来自哪儿（系统枚举 + 台架）",
              "系统串口枚举" in notes and "台架" in notes, notes[:200])

        # 未适配、身份未知的串口 ⇒ 通用设备：能连、能发、能收（需求 §6.12）。
        card = cards[0] if cards else {}
        reference = {"scan_seq": card.get("scan_seq"), "port": card.get("port")}
        status, payload = running.call("POST", "/api/observe/connect", {"device": reference})
        check("通用串口能连上（无需 Adapter）", status == 202, f"{status} {payload}")
        observe = payload.get("observe") or {}
        check("连上且保持通用串口模式",
              str(observe.get("state")) == "connected" and bool(observe.get("transparent")),
              json.dumps({k: observe.get(k) for k in ("state", "transparent")}, ensure_ascii=False))
        check("身份查询失败不阻止连接", bool(str(observe.get("transparent_reason") or "")),
              str(observe.get("transparent_reason")))

        status, payload = running.call("POST", "/api/observe/send",
                                       {"mode": "text", "text": "hello", "line_ending": "crlf"})
        check("能往通用串口发（202/200）", status in (200, 202), f"{status} {payload}")
        handle = fake.handles.get(PORT_GENERIC)
        check("发出去的字节真的到了那个口",
              bool(handle) and any(b"hello" in chunk for chunk in handle.writes),
              str(getattr(handle, "writes", None)))

        status, payload = running.call("POST", "/api/observe/disconnect", {})
        check("通用串口能断开", status == 200, str(status))
        status, payload = running.call("POST", "/api/observe/reconnect", {})
        check("通用串口能重连",
              status == 202 and str((payload.get("observe") or {}).get("state")) == "connected",
              str((payload.get("observe") or {}).get("state")))
    finally:
        running.close()


def check_live_lifecycle() -> None:
    """普通模式（配了候选口）：嗅探 → 连接 → 身份 → 断开 → 重连 + 不抢占。"""
    print("\n[3] 普通模式（有候选口）：嗅探 / 连接 / 身份 / 断开 / 重连 / 不抢占")
    work = Path(tempfile.mkdtemp())
    bench_file = work / "bench.local.json"
    bench_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "tools": {},
                "projects": {},
                "devices": {
                    "stm32a": {"port": PORT_ANSWERS},
                    "esp32a": {"port": PORT_SILENT},
                },
            }
        ),
        encoding="utf-8",
    )
    bench_setup.install_from(cli_path=str(bench_file))
    fake = FakeTransport(
        {PORT_ANSWERS: IDENTITY_LINE, PORT_SILENT: b""},
        # 第三个口**台架没点名**、只在系统枚举里 —— 走“通用串口”那条路（需求 §6.11）。
        enumerated=(PORT_GENERIC,),
    )
    assembled = console_server.build_live_assembly(
        work / "runs",
        sessions_dir=work / "ses",
        imports_dir=work / "imp",
        transport=fake.as_transport(),
        sniff_dir=work / "sniff",
    )
    running = Running(assembled)
    try:
        check("装配自检为空", not assembled.problems(), str(assembled.problems()))

        # ---- 嗅探：台架两个口 + 系统枚举到的第三个口
        status, payload = running.call("POST", "/api/sniff", {})
        check("POST /api/sniff = 202", status == 202, str(status))
        cards = (payload.get("sniff") or {}).get("cards") or []
        found = [str(card.get("port")) for card in cards]
        check("候选口 = 台架两个 + 系统枚举一个（合并去重）",
              sorted(found) == sorted([PORT_ANSWERS, PORT_SILENT, PORT_GENERIC]), json.dumps(found, ensure_ascii=False))
        by_port = {str(card.get("port")): card for card in cards}
        answered = by_port.get(PORT_ANSWERS) or {}
        silent = by_port.get(PORT_SILENT) or {}
        check("会答身份的那张卡：身份已确认", str(answered.get("identity_state")) == "confirmed",
              str(answered.get("identity_state")))
        fields = ((answered.get("observation") or {}).get("identity_fields")) or {}
        check("卡片上写出了设备自报的身份",
              fields.get("project") == "stm32f103-demo" and fields.get("build_id") == "0x1234abcd",
              json.dumps(fields, ensure_ascii=False))
        check("只读嗅探只发了一条查询",
              str((answered.get("observation") or {}).get("tx_hex") or "").startswith("40 50 57 49 4E 46 4F"),
              str((answered.get("observation") or {}).get("tx_hex")))
        check("静默那张卡：没问到身份但仍在册", str(silent.get("identity_state")) == "unknown",
              str(silent.get("identity_state")))
        check("嗅探只碰候选口", sorted(set(fake.exchanges)) == sorted([PORT_ANSWERS, PORT_SILENT, PORT_GENERIC]),
              str(fake.exchanges))

        ref_answered = {"scan_seq": answered.get("scan_seq"), "port": PORT_ANSWERS}
        ref_silent = {"scan_seq": silent.get("scan_seq"), "port": PORT_SILENT}

        # ---- 身份未知的设备：**先连接**（不被拒）
        status, payload = running.call("POST", "/api/observe/connect", {"device": ref_silent})
        check("连接静默设备 = 202（身份未知也放行）", status == 202, str(status))
        observe = payload.get("observe") or {}
        check("静默设备连上了", str(observe.get("state")) == "connected", str(observe.get("state")))
        check("静默设备走原始串口模式并说得出理由",
              bool(observe.get("transparent")) and bool(observe.get("transparent_reason")),
              json.dumps({k: observe.get(k) for k in ("transparent", "transparent_reason")}, ensure_ascii=False))
        status, payload = running.call("POST", "/api/observe/disconnect", {"reason": "测试：换设备"})
        check("断开 = 200", status == 200, str(status))

        # ---- 已连接的口：嗅探**不抢占**
        status, payload = running.call("POST", "/api/observe/connect", {"device": ref_answered})
        check("连接会答身份的卡片 = 202", status == 202, str(status))
        answered_status = payload.get("observe") or {}
        check("连上了", str(answered_status.get("state")) == "connected", str(answered_status.get("state")))

        fake.exchanges.clear()
        status, payload = running.call("POST", "/api/sniff", {})
        held = next(
            (card for card in ((payload.get("sniff") or {}).get("cards") or [])
             if str(card.get("port")) == PORT_ANSWERS),
            None,
        )
        reason = str(((held or {}).get("observation") or {}).get("reason") or "")
        tx = str(((held or {}).get("observation") or {}).get("tx_hex") or "")
        check("嗅探没有重开已连接的口", held is not None and "未重复打开" in reason, reason)
        check("嗅探没有向已连接的口写字节", tx == "", tx)
        # ⚠️ 只要求**被占着的那个口**没被重新问过：另一个口（没被占）本来就该照常嗅。
        check("嗅探没有重新打开被占着的口", PORT_ANSWERS not in fake.exchanges, str(fake.exchanges))

        # ---- 身份：**连上之后**会话自己去问
        got_identity = wait_for(
            lambda: str(
                ((running.call("GET", "/api/state")[1].get("observe") or {}).get("identity") or {}).get("state")
            ) == "ok"
        )
        state = running.call("GET", "/api/state")[1].get("observe") or {}
        check("连上后身份查询拿到结果", got_identity, str((state.get("identity") or {}).get("state")))
        check("身份字段解析正确",
              ((state.get("identity") or {}).get("fields") or {}).get("project") == "stm32f103-demo",
              json.dumps((state.get("identity") or {}).get("fields"), ensure_ascii=False))

        # ---- 断开 / 重连
        status, payload = running.call("POST", "/api/observe/disconnect", {})
        check("断开 = 200 且回到未连接", status == 200 and str((payload.get("observe") or {}).get("state")) != "connected",
              str((payload.get("observe") or {}).get("state")))
        status, payload = running.call("POST", "/api/observe/reconnect", {})
        check("重连 = 202 且又连上了",
              status == 202 and str((payload.get("observe") or {}).get("state")) == "connected",
              str((payload.get("observe") or {}).get("state")))

        # ---- 只读目标不许跑自动验证
        status, payload = running.call(
            "POST", "/api/run", {"target": "stm32f103", "firmware": "stm32f103/default", "suite": ""}
        )
        check("只读目标上 /api/run 被拒", status >= 400, f"{status} {payload}")
        check("拒绝理由说得清", "策略" in json.dumps(payload, ensure_ascii=False)
              or "未登记" in json.dumps(payload, ensure_ascii=False)
              or "套件" in json.dumps(payload, ensure_ascii=False),
              json.dumps(payload, ensure_ascii=False)[:200])
    finally:
        running.close()


def check_serial_settings() -> None:
    """需求 §6.10：基础串口参数（波特率 / 数据位 / 校验位 / 停止位）能设、能生效、非法值当场拒。"""
    print("\n[4] 基础串口参数（设置 → 打开前应用 → 非法值拒绝）")
    work = Path(tempfile.mkdtemp())
    bench_setup.install_from(root=work)
    fake = FakeTransport({PORT_GENERIC: b""}, enumerated=(PORT_GENERIC,))
    assembled = console_server.build_live_assembly(
        work / "runs", sessions_dir=work / "ses", imports_dir=work / "imp",
        transport=fake.as_transport(), sniff_dir=work / "sniff",
    )
    running = Running(assembled)
    try:
        running.call("POST", "/api/sniff", {})
        cards = (running.call("GET", "/api/state")[1].get("sniff") or {}).get("last") or {}
        reference = {
            "scan_seq": (cards.get("cards") or [{}])[0].get("scan_seq"),
            "port": PORT_GENERIC,
        }
        status, payload = running.call(
            "POST", "/api/observe/connect",
            {"device": reference,
             "serial": {"baud": 9600, "bytesize": 7, "parity": "E", "stopbits": 2}},
        )
        check("带串口参数连接 = 202", status == 202, f"{status} {payload}")
        serial = (payload.get("observe") or {}).get("serial") or {}
        check("服务端把这一套参数当成生效值",
              str((serial.get("effective") or {}).get("baud")) == "9600",
              json.dumps(serial.get("effective"), ensure_ascii=False))
        check("四个参数都有下落（设上的或如实拒绍的）",
              {"baud", "bytesize", "parity", "stopbits"}
              <= set(serial.get("applied") or {}) | set(serial.get("refused") or {}),
              json.dumps({"applied": serial.get("applied"), "refused": serial.get("refused")}, ensure_ascii=False))
        check("可选值由服务端给（页面不自己编）",
              sorted((serial.get("choices") or {}).keys()) == ["baud", "bytesize", "parity", "stopbits"],
              json.dumps(serial.get("choices"), ensure_ascii=False)[:160])
        status, payload = running.call("POST", "/api/observe/disconnect", {})
        if status == 200:
            pass
        status, payload = running.call(
            "POST", "/api/observe/connect", {"device": reference, "serial": {"baud": 123}}
        )
        check("非法波特率当场 400（不静默用默认值）", status == 400, f"{status} {payload}")
        status, payload = running.call(
            "POST", "/api/observe/connect", {"device": reference, "serial": {"nonsense": 1}}
        )
        check("不认识的参数名当场 400", status == 400, f"{status} {payload}")
    finally:
        running.close()


def check_sniff_transport_and_failure_modes() -> None:
    """嗅探的**传输来源**与**失败面**。

    这一路的处理原则是：通用口（系统枚举到、台架没点名）**不属于 STM32/ESP32 任一装配**，
    所以“拿什么去问这个口”必须来自**平台级传输实现**，不能靠“在装配清单里找到这个口”。
    这一节把那条断言钉住，并证明四种“实现坏了”的情形**都不会**变成 HTTP 500：

    | 坏在哪儿 | 期望 |
    | --- | --- |
    | 占用探测抛异常 | 202；卡片在；理由点名那句异常 |
    | 一问一答抛非约定异常 | 202；卡片在；`open_failed` + 异常类型 |
    | 资源闸门抛异常 | 202；卡片在；**没有打开那个口**（保守） |
    | 证据写不进去 | 202；卡片在；notes 里说“没能落盘” |
    """
    print("\n[5] 嗅探的传输来源与失败面（公共 Transport / 不许 500）")
    work = Path(tempfile.mkdtemp())
    bench_setup.install_from(root=work)

    # ---- 真实系统枚举的**形状**（不注入、不打开任何口）：零配置路径的基础候选源
    real = bench_target.system_ports()
    check("system_ports() 给的是 (口名, 描述) 的序列",
          isinstance(real, tuple)
          and all(isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], str)
                  for row in real),
          str(real)[:160])
    # ⚠️ 合并用例用**中性名字**（不写 COM<n> 这类字面串口名）：这个函数只是字符串
    #    去重与保序，而写死的口名会被“本机事实”扫描当成“本机结构泄露”。
    merged, note = bench_target.merge_candidates(["serial-A"], ["serial-a", "serial-B", ""])
    check("合并：去空、忽略大小写去重、台架点名的在前",
          merged == ("serial-A", "serial-B") and "台架" in note, f"{merged} / {note}")

    # ---- 接线本身：装配清单为空（通用口不属于任一装配）也要拿得到公共传输实现
    fake = FakeTransport({PORT_GENERIC: IDENTITY_LINE}, enumerated=(PORT_GENERIC,))
    transport = fake.as_transport()
    check("占用探测来自公共传输实现（装配清单为空也算）",
          console_server._sniff_access((), transport) == fake.port_probe, "")
    check("一问一答来自公共传输实现（装配清单为空也算）",
          console_server._sniff_exchange((), transport) == fake.exchange, "")
    check("完全没有传输实现时，占用探测如实回 unknown",
          console_server._sniff_access((), None)(PORT_GENERIC)
          == device_registry.ACCESS_UNKNOWN, "")
    refusal = ""
    try:
        console_server._sniff_exchange((), None)(PORT_GENERIC, 115200, b"", 8, 0.1)
    except sniff_service.ExchangeError as error:
        refusal = str(error)
    check("完全没有传输实现时，一问一答如实抛 ExchangeError", bool(refusal), refusal)

    def assemble(with_transport):
        assembled = console_server.build_live_assembly(
            work / "runs", sessions_dir=work / "ses", imports_dir=work / "imp",
            transport=with_transport, sniff_dir=work / "sniff",
        )
        return assembled

    assembled = assemble(transport)
    check("执行器用的是公共传输实现那一份",
          assembled.sniff._exchange == transport.exchange, "")
    running = Running(assembled)
    try:
        status, payload = running.call("POST", "/api/sniff", {})
        check("通用口的嗅探 = 202", status == 202, str(status))
        cards = (payload.get("sniff") or {}).get("cards") or []
        check("通用口出了一张卡片",
              [str(card.get("port")) for card in cards] == [PORT_GENERIC],
              json.dumps([card.get("port") for card in cards], ensure_ascii=False))
        check("问这个口走的就是公共传输实现", fake.exchanges == [PORT_GENERIC], str(fake.exchanges))
        check("证据落在指定的临时目录里（不往随发布的树里写）",
              any((work / "sniff").glob("*-sniff/result.json")), str(work / "sniff"))

        # ---- 身份匹配到适配链、而口是**系统枚举到的通用口**：必须也能连
        #      浏览器 E2E 抓到过：这条路上从前直接 409（“这个口不在这一路的设备登记里”），
        #      于是“身份问到了反而连不上、问不到反而连得上”。
        reference = {
            "scan_seq": (cards[0] if cards else {}).get("scan_seq"),
            "port": PORT_GENERIC,
        }
        status, payload = running.call("POST", "/api/observe/connect", {"device": reference})
        observe = payload.get("observe") or {}
        check("身份匹配到适配链的通用口也能连（不再 409）", status == 202,
              f"{status} {json.dumps(payload, ensure_ascii=False)[:200]}")
        check("连上之后走适配链（不是原始串口模式）",
              str(observe.get("state")) == "connected" and not observe.get("transparent"),
              json.dumps({k: observe.get(k) for k in ("state", "transparent")}, ensure_ascii=False))
        status, payload = running.call("POST", "/api/observe/disconnect", {})
        check("通用口（身份已确认）能断开", status == 200, str(status))
    finally:
        running.close()

    # ---- 坏实现之一：占用探测抛异常
    class ProbeBoom(FakeTransport):
        def port_probe(self, name: str) -> str:
            raise RuntimeError("占用探测自己坏了")

    probe_boom = ProbeBoom({PORT_GENERIC: IDENTITY_LINE}, enumerated=(PORT_GENERIC,))
    running = Running(assemble(probe_boom.as_transport()))
    try:
        status, payload = running.call("POST", "/api/sniff", {})
        cards = (payload.get("sniff") or {}).get("cards") or []
        reason = str(((cards[0] if cards else {}).get("observation") or {}).get("reason") or "")
        check("占用探测抛异常：仍然 202（不是 500）", status == 202, str(status))
        check("占用探测抛异常：卡片还在，理由点名那句异常",
              bool(cards) and "RuntimeError" in reason, reason[:200])
    finally:
        running.close()

    # ---- 坏实现之二：一问一答抛**非约定**异常（ValueError 不是 ExchangeError/OSError）
    class ExchangeBoom(FakeTransport):
        def exchange(self, name, baud, payload, max_bytes, wait_s) -> bytes:
            raise ValueError("传输实现自己坏了")

    exchange_boom = ExchangeBoom({}, enumerated=(PORT_GENERIC,))
    running = Running(assemble(exchange_boom.as_transport()))
    try:
        status, payload = running.call("POST", "/api/sniff", {})
        cards = (payload.get("sniff") or {}).get("cards") or []
        observation = (cards[0] if cards else {}).get("observation") or {}
        reason = str(observation.get("reason") or "")
        check("一问一答抛非约定异常：仍然 202（不是 500）", status == 202, str(status))
        check("一问一答抛非约定异常：结论是 open_failed",
              str(observation.get("outcome")) == "open_failed", str(observation.get("outcome")))
        check("一问一答抛非约定异常：理由里有异常类型", "ValueError" in reason, reason[:200])
    finally:
        running.close()

    # ---- 坏实现之三：资源闸门抛异常（保守：宁可少问一个口，也不撞上正在跑的会话）
    gate = FakeTransport({PORT_GENERIC: IDENTITY_LINE}, enumerated=(PORT_GENERIC,))
    broken = assemble(gate.as_transport())

    def gate_boom(port: str) -> str:
        raise RuntimeError("闸门坏了")

    broken.sniff._conflict = gate_boom
    running = Running(broken)
    try:
        status, payload = running.call("POST", "/api/sniff", {})
        cards = (payload.get("sniff") or {}).get("cards") or []
        observation = (cards[0] if cards else {}).get("observation") or {}
        check("资源闸门抛异常：仍然 202（不是 500）", status == 202, str(status))
        check("资源闸门抛异常：如实回“没有打开”", "资源闸门出错" in str(observation.get("reason") or ""),
              str(observation.get("reason"))[:200])
        check("资源闸门抛异常：真的一个字节都没发", gate.exchanges == [], str(gate.exchanges))
    finally:
        running.close()

    # ---- 坏实现之四：证据目录写不进去（路径上恰好是个文件）
    blocked = work / "blocked"
    blocked.write_text("我是一个文件，不是目录", encoding="utf-8")
    blocked_fake = FakeTransport({PORT_GENERIC: IDENTITY_LINE}, enumerated=(PORT_GENERIC,))
    blocked_assembly = console_server.build_live_assembly(
        work / "runs", sessions_dir=work / "ses", imports_dir=work / "imp",
        transport=blocked_fake.as_transport(), sniff_dir=blocked,
    )
    running = Running(blocked_assembly)
    try:
        status, payload = running.call("POST", "/api/sniff", {})
        sniff = payload.get("sniff") or {}
        cards = sniff.get("cards") or []
        notes = " ".join(str(item) for item in (sniff.get("notes") or ()))
        check("证据写不进去：仍然 202（不是 500）", status == 202, str(status))
        check("证据写不进去：卡片与结论照旧", bool(cards), json.dumps(sniff, ensure_ascii=False)[:200])
        check("证据写不进去：缺失写在 notes 里（不静默）", "没能落盘" in notes, notes[:200])
    finally:
        running.close()


def check_sniff_progress_and_the_concurrency_gate() -> None:
    """需求 §12.2：逐通信口进度快照 + 服务端并发闸门（真 HTTP + 真执行器）。

    这一节的每一条都对着"最容易假装做过"的地方：

    | 判据 | 怎么盖的 |
    | --- | --- |
    | 进度协议 | 与**两仓共用**的夹具 `fixtures/sniff_progress.json` 对字段、状态与派生百分比 |
    | 读状态零硬件动作 | 轮询 `/api/state` 期间传输替身的 `probes` / `exchanges` **一条都不涨** |
    | 并发第二轮 | 第二个 `POST /api/sniff` = **409**，且本轮没有被重开（`started_utc` 一字不变） |
    | 收口 | 放开闸门后本轮 `completed`、`percent=100`，第一轮照旧 202 + 卡片 |
    | 页面结构 | 原生 `<progress>` + 可访问名称 + 状态区；行高与进度条宽由 CSS 定死 |

    ⚠️ 页面那一行的**文案/进度条值**由浏览器套件（`tests/browser_e2e.js`）按**同一份夹具**核对
    —— 这里只钉服务端契约与静态结构（无浏览器的环境也跑）。
    """
    print("\n[6] 嗅探进度与并发闸门（需求 §12.2）")
    fixture = json.loads(
        (CONSOLE_DIR / "fixtures" / "sniff_progress.json").read_text(encoding="utf-8")
    )
    check("进度夹具的字段与状态和 core 一一对应",
          tuple(fixture["fields"]) == sniff_service.PROGRESS_FIELDS
          and tuple(fixture["states"]) == sniff_service.SNIFF_STATES,
          f"{fixture['fields']} / {fixture['states']}")
    check("percent 只由计数派生（零候选口不除零、超过总数不越 100）",
          all(sniff_service.derived_percent(row["completed_ports"], row["total_ports"])
              == row["percent"] for row in fixture["percent"]),
          json.dumps(fixture["percent"], ensure_ascii=False)[:200])
    check("快照带的就是夹具声明的那几个字段",
          sorted(sniff_service.SniffProgress().to_json()) == sorted(fixture["fields"]),
          str(sorted(sniff_service.SniffProgress().to_json())))

    index_html = (CONSOLE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    check("页面上有那一行：原生 progress + 可访问名称 + 状态区",
          'id="sniffProgress" role="status" aria-live="polite"' in index_html
          and '<progress id="sniffProgressBar"' in index_html
          and 'aria-label="设备嗅探进度"' in index_html,
          "")
    check("尺寸由 CSS 定死、常驻占着那一行的高度，且登记进四窗高度预算",
          # 用户实测：这一行一出现就把卡片行与四窗整体推一下（高 22 px）⇒ 显隐必须用
          # `visibility`（不参与 `hidden`/`display`），高度固定、不换行。
          'style="visibility:hidden"' in index_html
          and 'host.style.visibility = model.visible ? "visible" : "hidden";' in app_js
          and ".sniff-progress[hidden]" not in style_css
          and re.search(r"\.sniff-progress\s*\{[^}]*height:\s*\d+px", style_css) is not None
          and re.search(r"\.sniff-progress\s*\{[^}]*flex-wrap:\s*nowrap", style_css) is not None
          and re.search(r"\.sniff-progress\s+progress\s*\{[^}]*width:\s*\d+px", style_css) is not None
          and '"sniffProgress"' in re.search(r"const PANE_GAP_NODES = \[([^\]]*)\]", app_js).group(1),
          "")
    observe_js = (CONSOLE_DIR / "static" / "js" / "model" / "observe.js").read_text(encoding="utf-8")
    check("页面只从状态快照读进度（不读上一轮结果）",
          "function sniffProgressModel(sniff, busy)" in observe_js
          and "function sniffProgressModel(sniff, busy)" not in app_js
          and "sniff.last" not in observe_js.split("function sniffProgressModel(sniff, busy)", 1)[1].split("\n  }\n", 1)[0],
          "")

    work = Path(tempfile.mkdtemp())
    fake = FakeTransport({PORT_GENERIC: IDENTITY_LINE}, enumerated=(PORT_GENERIC,))
    assembled = console_server.build_live_assembly(
        work / "runs", sessions_dir=work / "ses", imports_dir=work / "imp",
        transport=fake.as_transport(), sniff_dir=work / "sniff",
    )
    entered = threading.Event()
    release = threading.Event()
    # ⚠️ 计数**只包住嗅探执行器自己的三个注入口**：`/api/state` 里的设备卡那一段
    #    本来就会探一次口（既有行为，与进度无关）—— 拿传输替身的总计数当判据会把
    #    “设备卡在读口”错算成“进度在读口”。这里量的就是“进度这条路有没有碰硬件”。
    sniff_calls = {"access": 0, "conflict": 0, "exchange": 0}
    real_access = assembled.sniff._access
    real_conflict = assembled.sniff._conflict
    real_exchange = assembled.sniff._exchange

    def counted_access(port: str) -> str:
        sniff_calls["access"] += 1
        return real_access(port)

    def counted_conflict(port: str) -> str:
        sniff_calls["conflict"] += 1
        return real_conflict(port)

    def gated(*args, **kwargs):
        # 停在这里等闸门：第一轮于是**真的**停在端口里，我们才能在它跑着的时候读状态、
        # 再打第二次 —— 一个"已经跑完"的轮次证明不了并发闸门。
        sniff_calls["exchange"] += 1
        entered.set()
        release.wait(5)
        return real_exchange(*args, **kwargs)

    assembled.sniff._access = counted_access
    assembled.sniff._conflict = counted_conflict
    assembled.sniff._exchange = gated
    running = Running(assembled)
    box: dict = {}

    def first_round() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"http://127.0.0.1:{running.port}/api/sniff", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=30) as response:
                box["status"] = response.status
                box["payload"] = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            box["status"] = error.code
            box["payload"] = json.loads(error.read().decode("utf-8") or "{}")
        except BaseException as error:  # noqa: BLE001 - 夹具：记下来再判
            box["error"] = f"{type(error).__name__}: {error}"

    worker = threading.Thread(target=first_round, daemon=True)
    worker.start()
    try:
        check("第一轮真的走进了那个口（正在等回答）", entered.wait(5), str(fake.exchanges))
        before = dict(sniff_calls)
        row: dict = {}
        for _ in range(20):
            _, state = running.call("GET", "/api/state")
            row = ((state.get("sniff") or {}).get("progress")) or {}
            if row.get("state") == "running":
                break
            time.sleep(0.02)
        check("进度是 running 且指着正在问的那个口",
              row.get("state") == "running" and row.get("current_port") == PORT_GENERIC
              and row.get("total_ports") == 1 and row.get("completed_ports") == 0
              and row.get("percent") == 0,
              json.dumps(row, ensure_ascii=False))
        check("反复读 /api/state 不产生任何端口动作（进度读的只是内存快照）",
              sniff_calls == before,
              f"嗅探自己的调用 {before} → {dict(sniff_calls)}")
        started = str(row.get("started_utc") or "")
        status, payload = running.call("POST", "/api/sniff", {})
        check("并发的第二轮 = 409（code=sniff_busy）",
              status == 409 and payload.get("code") == "sniff_busy"
              and "只允许一轮" in str(payload.get("error")),
              f"{status} {json.dumps(payload, ensure_ascii=False)[:160]}")
        check("被拒的第二轮没有开第二轮（端口访问没涨）",
              sniff_calls == before,
              f"嗅探自己的调用 {before} → {dict(sniff_calls)}")
        release.set()
        worker.join(timeout=15)
        row = {}
        for _ in range(60):
            _, state = running.call("GET", "/api/state")
            row = ((state.get("sniff") or {}).get("progress")) or {}
            if row.get("state") != "running":
                break
            time.sleep(0.05)
        check("放开闸门后收口成 completed：计数到位、百分比 100、本轮没被重开",
              row.get("state") == "completed" and row.get("completed_ports") == 1
              and row.get("percent") == 100 and str(row.get("started_utc") or "") == started,
              json.dumps(row, ensure_ascii=False))
        check("第一轮照旧 202 + 卡片（进度收口不影响结论）",
              box.get("status") == 202
              and [(card.get("num"), card.get("port"))
                   for card in ((box.get("payload") or {}).get("sniff") or {}).get("cards") or []]
              == [(1, PORT_GENERIC)],
              json.dumps(box, ensure_ascii=False)[:200])
    finally:
        release.set()
        worker.join(timeout=10)
        running.close()


def check_case_surface_and_run_state_follow_the_truth() -> None:
    """用户实测（2026-10-17）的三条：

    ① 自动验证里**不该**列出只能在实时观察屏启动的用例（`system.identity_verify` 那种零参数
       宿主用例）：勾了它要等**烧录之后**才在 observe 步报“主机场景必须声明且只声明一个文件参数”。
       现在页面不列、服务端在**启动前**就拒。
    ② 跑完那一刻 `/api/state` 以前会跳回**第一条注册的链**（空闲快照）⇒ 六个阶段永远“等待”、
       结果行停在开跑前的占位、PASS/FAIL 都没提示。现在按“在跑 / 最近跑过”的那条链给。
    ③ 服务重启后总线序号从头开始，页面手里的旧**大**号把之后每一条事件都丢掉 ⇒ 同样静默。
       现在两条判据（进程换戳 / 水位回退）任一成立就复位游标并重连。
    """
    print("\n[7] 用例入口归属 / 状态跟着最近一次 run / 重启后事件流自恢复")
    from webconsole import runner_registry, web_catalog
    from webconsole.embedded_test_model import (ParameterSpec, TestCase, TestCatalog)
    from webconsole.project_package import ProjectPackage

    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")

    check("页面不列只能在实时观察跑的用例，且与服务端同一条判据",
          "function autotestCaseRunsHere(testCase)" in app_js
          and ".filter((row) => autotestCaseRunsHere(row))" in app_js
          and "def autotest_can_run_here(case: Any) -> bool:" in
          (CONSOLE_DIR / "web_catalog.py").read_text(encoding="utf-8"),
          "")

    device_case = TestCase("demo.device", 1, "设备自主", "smoke", "device", 5.0)
    session_host = TestCase("system.identity_verify", 1, "硬件身份验证", "smoke",
                            "host_scenario", 11.0)
    image_host = TestCase("demo.image", 1, "镜像往返", "smoke", "host_scenario", 60.0,
                          parameters={"artifact": ParameterSpec("file", required=True)})
    check("设备用例与带文件参数的主机场景仍然可跑；零参数宿主用例不可跑",
          web_catalog.autotest_can_run_here(device_case)
          and web_catalog.autotest_can_run_here(image_host)
          and not web_catalog.autotest_can_run_here(session_host),
          "")

    package = ProjectPackage(
        key="demo", label="（验收自带的合成项目包）", suites=("smoke",),
        test_catalog=TestCatalog((device_case, session_host), ()),
    )

    class _Target:
        target_id = "demo-board"
        platform = "demo"
        firmware_label = "演示固件"
        device_key = "a"
        pipeline_allowed = True
        required_protocols = ()
        absent_protocols = ()
        first_flash_allowed = False
        requires_flasher_binding = False
        capabilities = None
        suites = ("smoke",)
        project = "demo"

        def to_json(self) -> dict:
            return {"target_id": self.target_id, "label": self.target_id, "platform": "demo",
                    "pipeline_allowed": True, "firmware_ids": []}

    class _Firmware:
        implemented = True
        test_regions: tuple = ()
        target_id = "demo-board"
        stack = "demo"
        firmware_id = "demo/default"
        project = "demo"
        label = "demo/default"
        identity_hint = ""

    catalog = web_catalog.WebCatalog((_Target(),), (package,), (_Firmware(),))
    selection = catalog.selection("demo-board", "demo/default", "smoke")
    refusal = ""
    try:
        catalog.prepare_tests(selection, [{"case_id": "system.identity_verify", "version": 1,
                                          "parameters": {}, "confirm_destructive": False}])
    except web_catalog.CatalogError as error:
        refusal = str(error)
    check("选它会在启动前被拒（不是烧录之后才报错）",
          "只能在实时观察屏启动" in refusal and "格式错误" not in refusal, refusal[:80])

    class _Pipeline:
        def __init__(self, name: str, store, bus) -> None:
            self.name = name
            self.store = store
            self.event_bus = bus
            self.run: dict | None = None

        def snapshot(self) -> dict:
            running = bool(self.run and self.run.get("state") == "running")
            return {"steps": [{"name": "owner", "status": self.name}], "busy": running,
                    "active_run": self.run if running else None, "last_run": self.run,
                    "devices": []}

        def active_run_port(self):
            return {"run_id": self.run["run_id"]} if self.run and self.run.get("state") == "running" else None

    class _Bus:
        pass

    store = type("S", (), {"runs_dir": Path("runs-shared")})()
    bus = _Bus()
    first, second = _Pipeline("first-chain", store, bus), _Pipeline("second-chain", store, bus)
    registry = runner_registry.RunnerRegistry(
        (runner_registry.Runner("a/first", first), runner_registry.Runner("b/second", second)),
        primary="a/first",
    )
    chosen = lambda: registry.snapshot()["steps"][0]["status"]  # noqa: E731
    empty_choice = chosen()
    second.run = {"run_id": "r-2", "state": "finished", "result": "PASS",
                  "started_utc": "2026-10-17T02:52:43.000Z",
                  "finished_utc": "2026-10-17T02:53:20.000Z"}
    finished_choice = chosen()
    second.run = {"run_id": "r-2", "state": "running", "result": None,
                  "started_utc": "2026-10-17T02:52:43.000Z", "finished_utc": None}
    running_choice = chosen()
    check("跑完之后状态仍跟着那条链（不跳回第一条的空闲快照）",
          empty_choice == "first-chain" and finished_choice == "second-chain"
          and running_choice == "second-chain",
          f"{empty_choice}/{finished_choice}/{running_choice}")

    check("服务重启后页面自己复位事件游标并重连",
          "function rebaseEventCursor()" in app_js
          and 'if (buildMovedReason === "restart" || app.lastSeq > watermarkSeq) rebaseEventCursor();'
          in app_js
          and "const liveRun = state.active_run || state.last_run || null;" in app_js,
          "")

    # ⚠️ 用户实测（2026-10-17）：1～5 窗在窄一点的窗口上会**换行**（≤1044 px 的 2×2+1、
    #    ≤640 px 的单列），而「阅读位」只按一个窗格算高度 ⇒ 整块工作区变成 2～3 屏、
    #    第一行以外全被挤到屏幕外，提示却写"已对齐"；单行时底边还会掉出视口 17 px。
    align = app_js.split("function alignReadingPosition()", 1)[1].split("\nfunction alignHint", 1)[0]
    check("阅读位按行数决定要不要伸高度，并按网格底边校正",
          "function paneRowCount()" in app_js
          and "const rows = paneRowCount();" in app_js
          and "rows," in align
          and "grid.getBoundingClientRect().bottom" in align
          and "for (let pass = 0; pass < 3; pass += 1) {" in app_js
          and "窗口排成 ${plan.rows} 行，一屏装不下" in app_js
          and app_js.count("setProperty(READING_HEIGHT_VAR") == 1,
          "多行：只对齐顶部（正文高度不动）；单行：写一次高度再按网格底边校正；高度只有一个写入口")

    # ⚠️ 用户口径（2026-10-17）：「阅读位」旁边的「分栏」按钮、它的弹出菜单、栏数徽标全部删掉 ——
    #    它看上去像能决定“硬件按几个窗口跑”，实际只改页面的排法，留着就是让人误会。
    #    收/展一个窗口现在只剩两条手势：双击窗口头、把分隔条拖到最窄。
    index_html = (CONSOLE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    check("「分栏」按钮与它的菜单已删掉，收/展只剩手势",
          "splitToggle" not in index_html and "splitPop" not in index_html
          and "split-count" not in index_html and 'class="split-col' not in index_html
          and "splitToggle" not in app_js and "splitCount" not in app_js
          and "function wireSplitMenu(" not in app_js
          and "function setSplitColumns(" not in app_js
          # 手势与几何都还在：4 根分隔条、flex 分宽度
          and index_html.count('class="split-bar"') == 4
          and "function paneView(" in (CONSOLE_DIR / "static" / "js" / "model" / "log-store.js").read_text(encoding="utf-8")
          and "function applyRowLayout(" in app_js,
          "")

    # ⚠️ 用户实测（2026-10-17）：点「阅读位」四个窗口都对上了；接着把某个窗口收成标签，
    #    工作区立刻矮回去，而阅读位**没人再对一次** —— 看上去就是“一折叠，阅读位就失效”。
    check("折叠/展开之后阅读位自动重算（不用点第二遍）",
          "function reapplyReadingPosition()" in app_js
          and "if (!app.readingEngaged) return false;" in app_js
          and "app.readingEngaged = true;" in app_js
          and "app.readingEngaged = false;" in app_js
          and "reapplyReadingPosition();" in app_js.split("function applyLayout(store)", 1)[1].split(
              "\nfunction setFocus", 1)[0]
          # 还开着时不许先清高度：清了再算中间会闪一帧（实测底边 716→700→716）
          and "if (!(app && app.readingEngaged)) clearReadingHeight();" in app_js,
          "")

    # ⚠️ 用户实测（2026-10-17）：底边对上了，但**每个窗格最底下那一行只剩半行** ——
    #    高度一变 `scrollTop` 不会自己跟到底（实测 maxScroll 567、scrollTop 558）。
    align_body = app_js.split("function alignReadingPosition()", 1)[1].split("\nfunction alignHint", 1)[0]
    check("阅读位之后把跟在尾部的窗格重新贴到底（最后一行不许只剩半行）",
          "function settlePaneTails(" in app_js
          and "if (!el || !source.followTail) continue;" in app_js
          and "if (over > 0) el.scrollTop = Math.max(0, el.scrollTop - over);" in app_js
          and "settlePaneTails();" in align_body
          and "requestAnimationFrame(() => settlePaneTails());" in align_body,
          "")

    # ⚠️ `server` 那一块说的是这台服务（进程 / 工作区锁 / 证据目录），不该跟着选中的链走。
    registry_source = (CONSOLE_DIR / "runner_registry.py").read_text(encoding="utf-8")
    check("状态里的 server 块来自主链（工作区锁不会因为跑过另一条链就消失）",
          'result["server"] = dict(snapshots[list(self._runners).index(self.primary_id)].get("server") or {})'
          in registry_source,
          "")


def check_window_source_isolation_contract() -> None:
    """五窗来源隔离（需求 §8.2）：一条事件的正文只属于一个窗口。

    结构上不再靠 `switch` 的 `default` 兜底：先过一张显式映射表，认不得的 kind 计数 +
    说一次原因，一个窗口都进不去。这里还守一条漂移：`applyEvent` 新加的 `case` 必须在表里登记。
    """
    print("\n[9] 五窗来源隔离：事件→窗口的映射与串窗自检")
    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    # 映射表与 eventToSource 住在纯模型里（`static/js/model/event-route.js`），
    # 组合根只解构使用 —— 判据跟着模型走，同时盯住 app.js 不再长出第二份。
    event_route_js = (CONSOLE_DIR / "static" / "js" / "model" / "event-route.js").read_text(encoding="utf-8")
    check(
        "事件→窗口的映射是一张显式表，未分类的不进任何窗",
        "const EVENT_WINDOW_BY_KIND = Object.freeze({" in event_route_js
        and "function eventToSource(event)" in event_route_js
        and "function eventToSource(event)" not in app_js
        and "routed.source === null" in app_js
        and "noteUnclassified(event, routed.reason)" in app_js,
        "",
    )
    check(
        "未分类事件有计数与原因（不是静默丢弃）",
        "unclassified: { count: 0, reasons: new Map() }" in app_js
        and "function noteUnclassified(event, reason)" in app_js
        and "它们不进任何窗口" in app_js,
        "",
    )
    check(
        "同一事件号进了两个窗口能被抓出来",
        "function noteRenderedEvent(event, source)" in app_js
        and "renderLedger: new Map()" in app_js
        and 'if (routed.source !== "control") noteRenderedEvent(event, routed.source);' in app_js
        and "function windowSourceViolations(store, ledger = app.renderLedger)" in app_js,
        "",
    )
    table_match = re.search(
        r"const EVENT_WINDOW_BY_KIND = Object\.freeze\(\{(.*?)\n\s*\}\);", event_route_js, re.S
    )
    mapped = set(re.findall(r'^\s*"?([a-z_.]+)"?:\s*"[a-z]+"', table_match.group(1), re.M)) if table_match else set()
    body = app_js.split("function applyEvent(event, touched) {", 1)
    handled = set(re.findall(r'case "([a-z_.]+)":', body[1].split("\n}\n", 1)[0])) if len(body) > 1 else set()
    missing = sorted(handled - mapped)
    check(
        "每个能被处理的事件 kind 都在映射表里（新增 case 必须同时登记）",
        bool(table_match) and bool(handled) and not missing,
        f"未登记：{missing}",
    )


def check_history_list_reads_names_without_hashing() -> None:
    """历史列表只读「有哪些工件」，不为展示计算内容哈希（需求 §8.2）。

    验收方式不是“看代码里有没有 iterdir”，而是：把 `sha256_file` 换成一个**一调用就报错**
    的替身，再跑 `summaries()` —— 列表这一步只要碰了内容哈希就会当场失败；
    `artifacts()` 仍然能拿到 sha256（按需的证据索引没有被删掉）。
    """
    print("\n[8] 历史列表：只读工件名，不算内容哈希")
    import runstore

    with tempfile.TemporaryDirectory(prefix="pw-runs-") as scratch:
        runs_dir = Path(scratch)
        for index in range(3):
            run = runs_dir / f"2026010{index + 1}-000000-list"
            run.mkdir()
            for name in runstore.MANDATORY_ARTIFACTS:
                (run / name).write_bytes(b"x")
            (run / "tool.log").write_text("ok\n", encoding="utf-8")
        store = runstore.RunStore(runs_dir)
        expected = {row["run_id"]: row["artifact_names"] for row in store.summaries()}

        original = runstore.sha256_file

        def _refuse(path):
            raise AssertionError(f"历史列表不该计算哈希：{path}")

        runstore.sha256_file = _refuse
        try:
            rows = store.summaries()
            raised = ""
        except AssertionError as error:
            rows, raised = [], str(error)
        finally:
            runstore.sha256_file = original

        check("列一遍历史不碰内容哈希", len(rows) == 3 and not raised, raised or f"rows={len(rows)}")
        check(
            "列表里的工件名与按需索引逐条相同（名字、顺序都不变）",
            all(row["artifact_names"] == expected.get(row["run_id"]) for row in rows),
            str([(row["run_id"], row["artifact_names"]) for row in rows])[:200],
        )
        indexed = store.artifacts(sorted(expected)[0])
        check(
            "需要哈希时仍能从按需索引里拿到",
            bool(indexed) and all("sha256" in row for row in indexed.values()),
            str(indexed)[:160],
        )


def check_warm_log_surfaces_are_light() -> None:
    """暖色工作台的五窗、阶段卡与输入区用浅暖表面（需求 §8.2），不再是大面积近黑。"""
    print("\n[10] 暖色日志表面：五窗不是近黑工作区")
    style_css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    check(
        "暖色日志表面令牌齐全",
        "--log-surface: #f7eee1" in style_css
        and "--log-text: #3b2b1e" in style_css
        and "--log-mark-bg: #ffe7ad" in style_css
        and "--stage-line: #dcc9b0" in style_css,
        "",
    )
    check(
        "五窗正文、当前行、命中与阶段卡都消费令牌",
        ":root[data-theme=\"warm\"] .log-pane { background: var(--log-surface); }" in style_css
        and ":root[data-theme=\"warm\"] .log-lines { color: var(--log-text); }" in style_css
        and ':root[data-theme="warm"] .log-line[data-current="1"] { background: var(--log-row-hover); }' in style_css
        and "background: var(--log-surface-3)" in style_css,
        "",
    )
    check(
        "没有人再把暖色日志窗涂回深色",
        re.search(r':root\[data-theme="warm"\] \.log-pane\s*\{[^}]*background:\s*#1', style_css) is None,
        "",
    )


def check_raw_terminal_view() -> None:
    """原始串口的终端视图（O14B）：按字节重放回车/退格/光标，只换显示不改证据。"""
    print("\n[11] 原始串口终端视图：按字节重放光标、回车与退格")
    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (CONSOLE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    observe_py = (CONSOLE_DIR / "observe.py").read_text(encoding="utf-8")
    style_css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    check(
        "每一行原始字节跟着行事件一起发（超长只带前一段并标截断）",
        'fields["bytes_hex"] = keep.hex()' in observe_py
        and 'fields["bytes_truncated"] = True' in observe_py
        and "RAW_BYTES_HEX_MAX = 8192" in observe_py,
        "",
    )
    check(
        "页面有终端视图容器与开关，默认仍按行显示",
        'id="termLines"' in index_html
        and 'id="terminal-raw"' in index_html
        and '#pane-raw[data-terminal="1"] > .log-lines { display: none; }' in style_css,
        "",
    )
    check(
        "回车/换行/退格/制表按字节处理，未知序列吞掉不崩",
        "if (byte === 0x0d)" in app_js
        and "if (byte === 0x0a)" in app_js
        and "if (byte === 0x08)" in app_js
        and "if (byte === 0x09)" in app_js
        and "termCsi(state, params, String.fromCharCode(feed[at]));" in app_js,
        "",
    )
    check(
        "终端只是显示层：行模型与证据一字未改",
        "termFeedEvent(event);" in app_js
        and 'add("raw", feed(store, "raw", event.line, event.ts, { defer, lineNo: event.line_no }));' in app_js,
        "",
    )
    check(
        "筛选与搜索直接作用在终端正文（与行视图同一套模型）",
        "if (filtering && hiddenReason(view, text)) continue;" in app_js
        and "if (dedupe && previousText === text && repeatNode) {" in app_js
        and "const ranges = re ? matchRanges(re, text) : [];" in app_js
        and "updateTerminalHits(hits, currentSlot, Boolean(view && view.query));" in app_js
        and '.term-row[data-repeat]::after { content: " ×" attr(data-repeat);' in
        (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8"),
        "",
    )
    check(
        "终端投影下「复制」复制的是屏幕可见文本（不是被遮住的行视图）",
        "function terminalVisibleText()" in app_js
        and 'if (id === "raw" && terminalOn()) {' in app_js
        and 'host.querySelectorAll(".term-row")' in app_js
        and "已复制终端" in app_js
        and 'trimeEnds' not in app_js,
        "",
    )
    check(
        "条件变更与当前命中都会重画终端，且计数归终端自己写",
        'if (id === "raw" && terminalOn()) renderTerminalNow();' in app_js
        and 'if (source.el.id === "lines-raw" && terminalOn()) renderTerminalNow();' in app_js
        and 'const terminalOwns = Boolean(source.el && source.el.id === "lines-raw" && terminalOn());' in app_js,
        "",
    )


def check_frontend_models_live_outside_the_page() -> None:
    """前端纯模型分文件装载：页面按序引入、app.js 不留第二份实现、静态子目录与 gzip 可用。"""
    print("\n[12] 前端纯模型：分文件装载、唯一实现、静态子目录与 gzip")
    static_dir = CONSOLE_DIR / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    model_dir = static_dir / "js" / "model"
    models = sorted(item.name for item in model_dir.glob("*.js"))
    check("模型文件在 static/js/model 下", {"sources.js", "format.js"} <= set(models), str(models))
    tags = re.findall(r'<script src="(/static/[^"]+)"', index_html)
    check(
        "页面先装模型、最后装 app.js",
        "/static/js/model/sources.js" in tags
        and "/static/js/model/format.js" in tags
        and tags.index("/static/js/model/sources.js") < tags.index("/static/app.js")
        and tags.index("/static/js/model/format.js") < tags.index("/static/app.js")
        and tags[-1] == "/static/app.js",
        str(tags),
    )
    check(
        "app.js 只解构命名空间",
        "= globalThis.PWModelFormat" in app_js and "= globalThis.PWModelSources" in app_js,
        "",
    )
    moved = (
        "toEpochMs", "beijingParts", "beijingClock", "beijingStamp", "parseLine", "isErrorLine",
        "lineTime", "formatDuration", "byteUnits", "compactDuration", "shortTime", "shortSha",
    )
    second = [name for name in moved if re.search(r"^function %s\(" % name, app_js, re.M)]
    check("app.js 里没有第二份实现", not second, "；".join(second))
    check(
        "窗口清单只有一份",
        "const SOURCES = [" not in app_js
        and "const SOURCES = [" in (model_dir / "sources.js").read_text(encoding="utf-8"),
        "",
    )
    check(
        "模型不碰 DOM / 网络 / 存储 / 定时器",
        not re.search(r"\b(document|fetch|EventSource|localStorage|sessionStorage|setTimeout|setInterval)\b",
                      (model_dir / "format.js").read_text(encoding="utf-8")
                      + (model_dir / "sources.js").read_text(encoding="utf-8")),
        "",
    )
    body = "const value = 1;\n".encode("utf-8") * 200
    packed = console_server.pack_static(body, ".js")
    check("文本资源会压得比原文小", packed is not None and len(packed) < len(body), "")
    check("二进制资源不压", console_server.pack_static(b"\x00" * 4096, ".bin") is None, "")
    check("太小的资源不压", console_server.pack_static(b"x", ".js") is None, "")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "js").mkdir()
        (root / "a.js").write_text("a", encoding="utf-8")
        before = console_server.static_stamp(root)
        (root / "js" / "m.js").write_text("m", encoding="utf-8")
        check("子目录里的新文件也会改变静态指纹", before != console_server.static_stamp(root), "")
    assembled = console_server.build_demo_assembly()
    running = Running(assembled)
    try:
        status, text = running.text("/static/js/model/format.js")
        check("GET /static/js/model/format.js = 200", status == 200 and "PWModelFormat" in text, str(status))
        status, _ = running.text("/static/js/model/../../server.py")
        check("越界的静态路径 404", status == 404, str(status))
        request = urllib.request.Request(
            f"http://127.0.0.1:{running.port}/static/app.js", headers={"Accept-Encoding": "gzip"}
        )
        with running.opener.open(request, timeout=30) as response:
            check("请求带 gzip 时压着发", response.headers.get("Content-Encoding") == "gzip", str(response.headers))
        request = urllib.request.Request(
            f"http://127.0.0.1:{running.port}/static/app.js", headers={"Accept-Encoding": "identity"}
        )
        with running.opener.open(request, timeout=30) as response:
            check("不请求 gzip 时原样发", response.headers.get("Content-Encoding") is None, "")
            check("静态资源仍然不缓存", response.headers.get("Cache-Control") == "no-store", "")
    finally:
        running.close()


def check_transport_is_the_only_way_out() -> None:
    """联网只有一个出口：页面里不再有 `fetch(` 与 `new EventSource`，全部走 transport。"""
    print("\n[13] 传输层唯一出口：页面只经 `static/js/transport/client.js` 联网")
    static_dir = CONSOLE_DIR / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    client_js = (static_dir / "js" / "transport" / "client.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    check("页面里没有直接的 fetch(", "fetch(" not in app_js, "")
    check("页面里没有直接的 EventSource", "new EventSource" not in app_js, "")
    check(
        "页面从 PWTransport 取名字",
        "} = globalThis.PWTransport;" in app_js
        and "jsonGet: getJson" in app_js and "jsonPost: postJson" in app_js,
        "",
    )
    check(
        "HTTP 与 SSE 都在 transport 里（各一处）",
        client_js.count("await fetch(") == 4
        and client_js.count("new EventSource(") == 1
        and "jsonGet" in client_js and "jsonPost" in client_js and "rangeGet" in client_js,
        "fetch=%d" % client_js.count("await fetch("),
    )
    check(
        "transport 在 app.js 之前装载",
        '/static/js/transport/client.js' in index_html
        and index_html.index('/static/js/transport/client.js') < index_html.index('/static/app.js'),
        "",
    )
    check(
        "transport 不碰 DOM / 存储 / 定时器",
        not re.search(r"\b(document|localStorage|sessionStorage|setTimeout|setInterval)\b", client_js),
        "",
    )
    check(
        "区间装载仍读得到 Content-Range 与行号基准",
        "header: (name) => response.headers.get(name)" in client_js
        and 'response.header("Content-Range")' in app_js
        and 'response.header("X-Line-Base")' in app_js,
        "",
    )


def check_preferences_keep_the_old_key_working() -> None:
    """本机偏好的新键是 `ba.*`，`pw.*` 保留为兼容别名：读 ba 优先、写两份都写。"""
    print("\n[14] 偏好键兼容：`ba.*` 是新的，`pw.*` 仍读写")
    static_dir = CONSOLE_DIR / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    doctor_html = (static_dir / "doctor.html").read_text(encoding="utf-8")
    doctor_js = (static_dir / "doctor.js").read_text(encoding="utf-8")
    check(
        "页面有键别名与读写封装",
        "function prefAliasKey(key)" in app_js
        and 'replace(/^pw\\./, "ba.")' in app_js
        and "function readPref(key)" in app_js and "function writePref(key, value)" in app_js,
        "",
    )
    direct = [line.strip() for line in app_js.splitlines()
              if "localStorage.getItem(" in line or "localStorage.setItem(" in line]
    check(
        "页面其余地方只经封装读写（不许绕过双写）",
        len(direct) == 4 and all("prefAliasKey" in line or line.endswith("getItem(key);") or line.endswith("setItem(key, value);") for line in direct),
        str(direct)[:160],
    )
    check(
        "两处主题引导都先读 ba.theme 再退 pw.theme",
        "localStorage.getItem('ba.theme')" in index_html and "localStorage.getItem('pw.theme')" in index_html
        and "localStorage.getItem('ba.theme')" in doctor_html and "localStorage.getItem('pw.theme')" in doctor_html,
        "",
    )
    check(
        "体检页写主题时两份都写",
        'THEME_ALIAS_KEY = "ba.theme"' in doctor_js
        and "setItem(THEME_ALIAS_KEY, next)" in doctor_js
        and "setItem(THEME_KEY, next)" in doctor_js
        and "getItem(THEME_ALIAS_KEY)" in doctor_js,
        "",
    )


def check_run_preview_summarizes_before_start() -> None:
    """启动前在页面内给出「本次将执行」的稳定摘要：只写文字、不触发动作、也不靠原生确认框。"""
    print("\n[15] 本次将执行：页面内摘要（与选择同源）")
    static_dir = CONSOLE_DIR / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    style_css = (static_dir / "style.css").read_text(encoding="utf-8")
    runs_js = (static_dir / "js" / "model" / "runs.js").read_text(encoding="utf-8")
    check(
        "页面有一行摘要（可访问地声明它是状态）",
        'id="runPreview"' in index_html
        and 'role="status"' in index_html and 'aria-live="polite"' in index_html
        and 'class="run-preview"' in index_html,
        "",
    )
    check(
        "文字在纯模型里，页面只递当前选择",
        "function runPreviewLine(input)" in runs_js and "function runPreviewText()" in app_js,
        "",
    )
    check(
        "只在内容变了才写 DOM（不逐轮询播报）",
        "if (box.textContent !== line) box.textContent = line;" in app_js,
        "",
    )
    check(
        "跟着选择与采集时长更新，且不触发动作",
        "renderRunPreview();" in app_js
        and '$("observeSeconds").addEventListener("input", renderRunPreview);' in app_js
        and "本次将执行：先选好固件与套件" in app_js,
        "",
    )
    check("样式定了高度与省略号（不推动下面布局）", ".run-preview {\n" in style_css and "text-overflow: ellipsis;" in style_css, "")


def check_dangerous_actions_confirm_in_page() -> None:
    """危险动作在**页面内**确认：设备 / 固件 / 危险动作 / 后果四类事实，默认焦点在「取消」。"""
    print("\n[16] 危险动作：页面内确认层（不再用原生 confirm）")
    static_dir = CONSOLE_DIR / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    style_css = (static_dir / "style.css").read_text(encoding="utf-8")
    check(
        "页面里有确认层（dialog + 取消/确认按钮）",
        'id="riskDialog"' in index_html and 'id="riskBody"' in index_html
        and 'id="riskCancel"' in index_html and 'id="riskOk"' in index_html
        and 'aria-labelledby="riskTitle"' in index_html,
        "",
    )
    check(
        "确认走页面内对话框，原生 confirm 只剩兼容兜底",
        "function confirmRisk(lines)" in app_js
        and app_js.count("window.confirm(") == 1
        and "typeof dialog.showModal" in app_js,
        "window.confirm 次数=%d" % app_js.count("window.confirm("),
    )
    check(
        "确认只说四类事实，不列项目包/探针命令",
        'dangers.push("覆盖设备 Flash")' in app_js
        and 'dangers.push("显式复位设备")' in app_js
        and "危险动作：" in app_js and "后果：" in app_js
        and "项目包：" not in app_js.split("async function startRun()", 1)[1].split("try {", 1)[0],
        "",
    )
    check(
        "默认焦点落在「取消」",
        '($("riskCancel") || dialog).focus();' in app_js
        and ".risk-dialog {" in style_css and ".risk-dialog::backdrop" in style_css,
        "",
    )


def check_empty_enter_still_sends_a_newline() -> None:
    """命令框按回车且内容为空时：发一个换行（不是静默什么都不做）。"""
    print("\n[17] 命令框：空回车也要真的发出去")
    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    body = app_js.split("async function consoleSend() {", 1)[1].split("function wireConsoleInput()", 1)[0]
    check(
        "空内容走按键路径（不是 return）",
        'if (!text.trim()) {' in body
        and 'postJson("/api/observe/keys"' in body
        and 'key: "Enter"' in body,
        "",
    )
    check(
        "没连接时说明为什么没发（不再无声）",
        "没有连接设备：先选定设备并连接，再发字节" in body and "captureConnected()" in body,
        "",
    )
    check(
        "发出去之后如实报字节数与显示",
        "已发送 ${sent.length} B：${sent.display}" in body,
        "",
    )


def check_backspace_is_selectable() -> None:
    """退格键发哪个字节可选：有的 shell 只认 0x08，有的只认 0x7F。"""
    print("\n[18] 命令框：退格键可选 DEL 或 BS")
    index_html = (CONSOLE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (CONSOLE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    observe_py = (CONSOLE_DIR / "observe.py").read_text(encoding="utf-8")
    center_py = (CONSOLE_DIR / "observe_center.py").read_text(encoding="utf-8")
    server_py = (CONSOLE_DIR / "server.py").read_text(encoding="utf-8")
    check(
        "页面有退格键选择器（del / bs 两个选项）",
        'id="observeBackspace"' in index_html
        and 'value="del"' in index_html
        and 'value="bs"' in index_html,
        "",
    )
    keys_posts = app_js.count('postJson("/api/observe/keys"')
    with_bs = app_js.count("backspace: currentBackspace()")
    check(
        "两条发键路径都带上退格键选择",
        keys_posts >= 2 and with_bs >= 2,
        f"发键 {keys_posts} 处 / 带 backspace {with_bs} 处",
    )
    check(
        "选择记进偏好（刷新后还在）",
        'saved.backspace === "bs" ? "bs" : "del"' in app_js
        and "app.prefs.backspace = currentBackspace()" in app_js,
        "",
    )
    check(
        "服务端把 backspace 透传给发键",
        "def send_keys(self, keys: object, backspace: object = None)" in observe_py
        and 'body.get("backspace")' in server_py,
        "",
    )
    check(
        "中心层也把 backspace 往下传（漏一层就是 500）",
        "def send_keys(self, keys: object, backspace: object = None) -> dict[str, Any]:\n        return self.session().send_keys(keys, backspace)"
        in center_py,
        "",
    )
    sys.path.insert(0, str(CONSOLE_DIR))
    try:
        import console_input  # noqa: PLC0415

        bs_payload, _, _ = console_input.build_keys_payload(
            [{"key": "Backspace"}], line_ending="crlf", backspace="bs"
        )
        del_payload, _, _ = console_input.build_keys_payload(
            [{"key": "Backspace"}], line_ending="crlf", backspace="del"
        )
    finally:
        if str(CONSOLE_DIR) in sys.path:
            sys.path.remove(str(CONSOLE_DIR))
    check(
        "bs 发 0x08、del 发 0x7F（不是同一个字节）",
        bs_payload == b"\x08" and del_payload == b"\x7f",
        f"bs={bs_payload!r} del={del_payload!r}",
    )


def main() -> int:
    print("Web 验收（无硬件、无 pyserial）")
    check_demo_pages()
    check_live_without_bench()
    check_live_lifecycle()
    check_serial_settings()
    check_sniff_transport_and_failure_modes()
    check_sniff_progress_and_the_concurrency_gate()
    check_case_surface_and_run_state_follow_the_truth()
    check_window_source_isolation_contract()
    check_history_list_reads_names_without_hashing()
    check_warm_log_surfaces_are_light()
    check_raw_terminal_view()
    check_frontend_models_live_outside_the_page()
    check_transport_is_the_only_way_out()
    check_preferences_keep_the_old_key_working()
    check_run_preview_summarizes_before_start()
    check_dangerous_actions_confirm_in_page()
    check_empty_enter_still_sends_a_newline()
    check_backspace_is_selectable()
    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} 条失败 —— " + "；".join(FAILURES))
        return 1
    print(
        "RESULT: 全部通过（页面 / 三模式 / 历史回放 / 枚举与配置合并 / 嗅探 / 嗅探进度与并发闸门 / 前端纯模型 / 传输层唯一出口 / 偏好键兼容 / 运行前摘要 / 危险动作确认 / 空回车 / "
        "连接生命周期 / 收发 / 串口参数 / 公共传输实现与失败面）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

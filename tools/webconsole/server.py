#!/usr/bin/env python3
"""Local web console for the Workbench 示例项目 pipeline.

The service is the single owner of PlatformIO and the COM ports. The browser
only issues intents (start / cancel) and reads state plus evidence; it never
opens a serial port itself, so it can never become a second port owner.

    python tools/webconsole/server.py --open

Everything is read-only until the user explicitly starts an allowed run.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import gzip
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

CONSOLE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = CONSOLE_DIR.parent
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import console_lock  # noqa: E402  (shared with the CLI tools)
try:  # 包内（`webconsole.server`）/ 直接跑脚本两种接法都要能拿到**同一份**台架状态
    from . import bench_config  # noqa: E402  (core： 台架配置层)
    from . import bench_setup  # noqa: E402  (core：装配处的台架声明与入口安装)
except ImportError:
    import bench_config  # noqa: E402  (core： 台架配置层)
    import bench_setup  # noqa: E402  (core：装配处的台架声明与入口安装)
# 平台体检（ / D5）：它与 CLI 是**同一个模块**（`tools/doctor.py`），
# 所以页面与终端拿到的是同一个模型算出来的同一份报告。
import doctor  # noqa: E402

# The lock lives beside the project, not in .pio (which `fullclean` wipes).
PROJECT_DIR = TOOLS_DIR.parent

try:  # imported as ``webconsole.server``
    from .core_primitives import utc_now
    from .observe import ObserveError, ObserveSession, HOLDING_STATES, OBSERVE_CONNECTED
    from .observe_store import artifact_path
    from . import bundle_export
    from . import bundle_stream_import
    from . import large_bundle
    from . import http_range
    from . import large_evidence
    from . import large_index
    from . import search_center
    from . import platform_info
    from . import runner_registry
    from . import session_review
    from . import web_catalog
    from . import web_assembly
    #  / D1：设备嗅探（core 的形状 + core 的执行器 + core 的占用状态常量）。
    from . import device_sniff
    from . import identity
    from . import sniff_service
    from .device_registry import ACCESS_FREE, ACCESS_UNKNOWN
    from .pipeline import (
        STEP_ORDER,
        EventBus,
        Pipeline,
        RequestError,
    )
except ImportError:  # executed as a plain script
    from core_primitives import utc_now  # type: ignore[no-redef]
    from observe import (  # type: ignore[no-redef]
        ObserveError, ObserveSession, HOLDING_STATES, OBSERVE_CONNECTED,
    )

#  / L4：受控测试的服务端入口（目录投影 + 启动/取消）。
# 判断仍然全在 core 模型里（`live_test_catalog` / `live_test_batch`），
# 这里只负责把"这一条会话上的事实"凑齐并做**启动前的一次再校验**。
try:
    from .live_test_batch import BatchPlan, LiveTestBatchError
    from .live_test_catalog import (
        LiveTestError, SessionFacts, binding_for, verify_start,
    )
except ImportError:  # executed as a plain script
    from live_test_batch import BatchPlan, LiveTestBatchError  # type: ignore[no-redef]
    from live_test_catalog import (  # type: ignore[no-redef]
        LiveTestError, SessionFacts, binding_for, verify_start,
    )
    from observe_store import artifact_path  # type: ignore[no-redef]
    import bundle_export  # type: ignore[no-redef]
    import bundle_stream_import  # type: ignore[no-redef]
    import large_bundle  # type: ignore[no-redef]
    import http_range  # type: ignore[no-redef]
    import large_evidence  # type: ignore[no-redef]
    import large_index  # type: ignore[no-redef]
    import search_center  # type: ignore[no-redef]
    import platform_info  # type: ignore[no-redef]
    import runner_registry  # type: ignore[no-redef]
    import session_review  # type: ignore[no-redef]
    import web_catalog  # type: ignore[no-redef]
    import web_assembly  # type: ignore[no-redef]
    import device_sniff  # type: ignore[no-redef]
    import identity  # type: ignore[no-redef]
    import sniff_service  # type: ignore[no-redef]
    from device_registry import ACCESS_FREE, ACCESS_UNKNOWN  # type: ignore[no-redef]
    from pipeline import (  # type: ignore[no-redef]
        STEP_ORDER,
        EventBus,
        Pipeline,
        RequestError,
    )

STATIC_DIR = CONSOLE_DIR / "static"


def static_stamp(static_dir: Path = STATIC_DIR) -> str:
    """静态资源的指纹：任一文件被改 / 加 / 删，它就会变（）。

    ⚠️ 不哈希内容：每 2 秒一次轮询都会跑它，`stat` 几个文件就够了。
    这里要回答的不是"内容是什么"，而是"页面手上那份还是不是最新的"。
    mtime 纳秒 + 大小 + 文件名，再加上**文件个数**（删掉一个而剩下的没变时也要变）。
    """
    items: list[str] = []
    try:
        paths = sorted(static_dir.rglob("*"))
    except OSError:
        return "missing"
    for path in paths:
        try:
            info = path.stat()
        except OSError:  # 探到一半被删：下一次轮询就准了
            continue
        try:
            label = path.relative_to(static_dir).as_posix()
        except ValueError:
            label = path.name
        items.append(f"{label}:{info.st_size}:{info.st_mtime_ns}")
    return f"{len(items)}|" + "|".join(items)

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

# 文本类静态资源可以压着发：只压“认得出来是文本”的扩展名、且压完真的更小才用。
# 拆成多个文件之后总字节变多，压缩把它补回来；`Cache-Control` 仍是 `no-store`
# （调试台改完 `static/*` 刷新就该看到新的，缓存反而是障碍）。
PACKABLE_SUFFIXES = {".html", ".js", ".css", ".svg", ".json"}
PACK_MIN_BYTES = 1024
_PACKED_CACHE: dict[tuple[str, int, int], bytes] = {}
_PACKED_CACHE_MAX = 64


def pack_static(body: bytes, suffix: str) -> bytes | None:
    """把一份静态资源压成 gzip；返回 None 表示“不值得压/不该压”。

    纯函数（不碰磁盘、不看时间），宿主回归可以直接喂它。
    """
    if suffix.lower() not in PACKABLE_SUFFIXES:
        return None
    if len(body) < PACK_MIN_BYTES:
        return None
    packed = gzip.compress(body, 6)
    return packed if len(packed) < len(body) else None

EVIDENCE_TYPES = {
    "raw.bin": "application/octet-stream",
    "manifest.json": "application/json; charset=utf-8",
    "report.json": "application/json; charset=utf-8",
    "events.jsonl": "application/x-ndjson; charset=utf-8",
    "steps.jsonl": "application/x-ndjson; charset=utf-8",
    "app_events.jsonl": "application/x-ndjson; charset=utf-8",
    "metrics.jsonl": "application/x-ndjson; charset=utf-8",
    # 实时观察会话的时序文件（TX/RX/连接状态）。它**不是** run 的证据，
    # 所以放在这里只是给下载用（会话目录由 `observe_store` 管）。
    "io_events.jsonl": "application/x-ndjson; charset=utf-8",
}


def _file_size(path: Path) -> int:
    """文件大小（拿不到就当 0）—— 只用于把 416 的 `Content-Range: bytes */size` 写对。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


#: 窗口装载的**行号基准**（）：请求带这个头（值为 `1`）⇒ 响应里也带一个
#: `X-Line-Base: <n>`，`n` = 这一段的第一行在**文件**里是第几行（1 基）。
#: 为什么是头而不是 JSON：窗口装载本来就是一次 Range GET，正文必须是原样的证据字节。
LINE_BASE_HEADER = "X-Line-Base"

#: `X-Line-Range: <a>-<b>`：**按行**要一段窗口（1 基、含端点，与证据引用同口径）。
#: 行号是给“定位”用的（阶段卡上的行号、搜索结果的行号），而窗口装载要的是**字节**。
#: 服务端拿 `offset_for_line()` 换算成字节 Range，之后走**同一条**流式路（不整读）。
LINE_RANGE_HEADER = "X-Line-Range"


def search_target_resolver(
    pipeline: object, observe: object
) -> Callable[[dict], "search_center.SearchTarget | None"]:
    """把"要搜哪份证据"解析成一个**经过白名单校验**的路径（L4）。

    两条路（run 证据、会话/导入包证据）用**同一套**规则：名字白名单就在
    `store.artifact()` 与 `observe_store.artifact_path()` 里，这里不另写一份 ——
    也就不会出现"下载那条路拦了、搜索这条路没拦"这种事。

    解析不出来就返回 `None`（路由层把它变成 404），不抛异常。
    """

    def resolve(request: dict) -> "search_center.SearchTarget | None":
        evidence = str(request.get("evidence") or "")
        if not evidence:
            return None
        session_id = str(request.get("session_id") or "")
        run_id = str(request.get("run_id") or "")
        try:
            if session_id:
                root, _origin = session_review.resolve_session(
                    observe.sessions_dir,  # type: ignore[attr-defined]
                    observe.imports_dir,  # type: ignore[attr-defined]
                    session_id,
                )
                path = artifact_path(root, session_id, evidence)
                return search_center.SearchTarget(
                    entity_id=session_id,
                    kind="session",
                    evidence=evidence,
                    path=path,
                    root=root,
                )
            if not run_id:
                return None
            path = pipeline.store.artifact(run_id, evidence)  # type: ignore[attr-defined]
        except (ValueError, OSError, session_review.SessionNotFound):
            return None
        return search_center.SearchTarget(
            entity_id=run_id,
            kind="run",
            evidence=evidence,
            path=path,
            root=path.parent,
        )

    return resolve


def bundle_root_resolver(observe: object) -> Callable[[str], Path]:
    """把会话号解析成"它住在哪个根目录"（P2）。

    本机会话在 `observe.sessions_dir`、导入包在 `observe.imports_dir` —— 哪一个是它，
    由 `session_review.resolve_session()` 回答（与回看/下载/搜索**同一个**函数），
    所以这里不会出现"下载找得到、导出找不到"这种两套规则。
    """

    def resolve(session_id: str) -> Path:
        root, _origin = session_review.resolve_session(
            observe.sessions_dir,  # type: ignore[attr-defined]
            observe.imports_dir,  # type: ignore[attr-defined]
            session_id,
        )
        return root

    return resolve


def no_sessions_resolver(session_id: str) -> Path:
    """没有观察会话的装配（例如只跑搜索那条路的宿主测试）：任何会话号都不存在。

    ⚠️ 这里回一个"拒"而不是让 `/api/state` 或构造过程炸掉：导出那几条路由本来就
    不相关，与服务能不能起来无关。
    """
    raise session_review.SessionNotFound(f"这台服务没有观察会话（要的是 {session_id}）")


#: 关连接前最多读掉多少字节（只读**已经在路上**的那一点，不追流式请求）。
DRAIN_MAX_BYTES = 256 * 1024
#: 等“还在路上的请求体”最多多久。
#:
#: ⚠️ 这个等待是必需的，不能只读“此刻已经到缓冲区里的”：服务端在**请求头一读完**就拒了
#: （这正是“先拦后读”的本意），而客户端那个小请求体往往是**紧跟着**才到的 ⇒ 不等就会在
#: 关连接时留着未读数据，Windows 就发 RST，把已经写出去的响应一起丢掉（客户端看到 10053）。
DRAIN_WAIT_S = 0.05

#: 「身份未识别，已进入原始串口模式」（用户口径 2026-10-13 点名的屏幕措词）。
#:
#: 它是**服务端**给的一句话（页面照抄，不自己拼），用在两处：
#: ① `/api/state.observe.transparent` 为真时，页面在实时观察的状态行上说一遍；
#: ② 受控用例面板在同一个状态下报 `applicable=False` —— 屏幕上那一格只说「不适用」，
#:    这句话留在它的悬停里（屏幕只留结论、原文进悬停是这轮一直在执行的口径）。
TRANSPARENT_LIVE_TEST_REASON = "身份未识别，已进入原始串口模式"


def _drain_before_close(sock, wait_s: float = DRAIN_WAIT_S) -> None:
    """关连接之前把请求体读掉（尽力而为、最多等 `wait_s`，超时就算了）。

    为什么要它（实测，非推测）：拒绝“没声明长度”的上传时，服务端**不读请求体**就
    `close_connection = True`。Windows 上“带着未读数据关 socket”会发 **RST**，
    客户端那条已经写出去的响应会被丢掉——客户端拿到的是
    `ConnectionAbortedError: [WinError 10053]` 而不是那句 JSON。
    ⇒ `test_observe` 最后那条“分块传输必被拒”的判据会**偶发红**
    （2026-10-12：全量里出现一次；第一次只修了“读已到的那点”仍然红，因为请求体
    那时还在路上）。读掉再关就退化成普通的 FIN 关闭。
    """
    if sock is None:
        return
    try:
        sock.settimeout(wait_s)
    except OSError:
        return
    remaining = DRAIN_MAX_BYTES
    try:
        while remaining > 0:
            block = sock.recv(min(65536, remaining))
            if not block:
                return
            remaining -= len(block)
    except (TimeoutError, InterruptedError):        # 包含 socket.timeout
        pass
    except OSError:
        pass


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "WorkbenchConsole/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- utilities
    @property
    def pipeline(self):
        return self.server.pipeline  # type: ignore[attr-defined]

    @property
    def search(self) -> search_center.SearchCenter:
        """搜索任务登记处（L4）：它不知道 run/session 是什么，解析由装配处交进来。"""
        return self.server.search  # type: ignore[attr-defined]

    @property
    def bundles(self) -> bundle_export.ExportCenter:
        """导出任务登记处（ / P2）：从磁盘逐块生成诊断包，再流式发出去。"""
        return self.server.bundles  # type: ignore[attr-defined]

    @property
    def imports(self) -> bundle_stream_import.ImportCenter:
        """导入任务登记处（ / P3）：上传先落盘，再预检/解压/核对/原子发布。"""
        return self.server.imports  # type: ignore[attr-defined]

    @property
    def observe(self) -> ObserveSession:
        return self.server.observe  # type: ignore[attr-defined]

    @property
    def sessions_dir(self) -> Path:
        return self.observe.sessions_dir

    @property
    def imports_dir(self) -> Path:
        """导入的诊断包住哪儿（切片 8）。与本机会话**不**在同一个目录里。"""
        return self.observe.imports_dir

    def platform_facts(self) -> dict:
        """平台自述（R2d）：装配处那套事实，`/api/state` 与会话 manifest 共用同一个函数。"""
        facts = getattr(self.server, "platform_facts", None)
        if callable(facts):
            return facts()
        return platform_info.describe(plugins=getattr(self.observe, "plugins", None))

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        return

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = 200,
        download_name: str | None = None,
        content_encoding: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        if download_name:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{download_name}"'
            )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def _send_text(self, text: str, status: int = 200) -> None:
        self._send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def _send_error_json(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        close: bool = False,
    ) -> None:
        """一条拒绝。`code` 是机器可读的错误码（ / L0 的那张表）。

        `headers` 是给 416 用的：`Content-Range: bytes */size` 必须跟着 416 一起发，
        否则客户端只知道"要不到"，不知道文件到底多大。

        ⚠️ `close=True`：这条拒绝发生在**还没把请求体收完**的时候 —— 剩下的字节还在
        连接上，不关连接的话它们会被当成下一个请求（客户端会看到一个莫名其妙的断连）。
        """
        payload: dict[str, object] = {"error": message, "status": status}
        if code:
            payload["code"] = code
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        if close:
            # 这条拒绝发生在“还没把请求体收完”的时候 ⇒ 关连接前把**已经到的那点**读掉：
            # 否则 Windows 会把未读数据引起的 RST 甩给对端，连已经写出去的响应一起丢掉
            # （客户端看到的是 10053 而不是这句 JSON —— 实测会让判据偶发红）。
            _drain_before_close(
                getattr(self, "connection", None) or getattr(self, "request", None)
            )

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length 非法") from None
        if length <= 0:
            return {}
        if length > 1 << 20:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _declared_body_length(self) -> int:
        """上传体声明的长度（分块传输不收：不知道要收多少字节就没法先拦后读）。"""
        header = self.headers.get("Content-Length")
        if header is None:
            raise bundle_stream_import.BundleError(
                "upload_length_unknown",
                "上传没有声明 Content-Length：不知道要收多少字节就没法先拦后读",
            )
        try:
            length = int(header)
        except ValueError:
            raise bundle_stream_import.BundleError(
                "upload_length_unknown", f"Content-Length 不是数字：{header!r}"
            ) from None
        if length <= 0:
            raise bundle_stream_import.BundleError("upload_incomplete", "请求体是空的")
        return length

    def _body_chunks(self, length: int, chunk_bytes: int = large_bundle.STREAM_CHUNK_BYTES):
        """把请求体按固定块交出去（**不整个读进来**）。

        连接在收满之前断了就停下来：`spool_upload` 会发现实收少于声明，报 `upload_incomplete`
        并把半包删掉 —— 半包不许落进正式目录。
        """
        remaining = int(length)
        while remaining > 0:
            block = self.rfile.read(min(chunk_bytes, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block

    # ----------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            segment = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if path == "/":
                return self._send_static("index.html")
            if path == "/doctor":
                # 体检页（D5）：一个**只读报告视图**，与工作台分开（工作台是日志工具）。
                return self._send_static("doctor.html")
            if segment[:1] == ["static"] and len(segment) >= 2:
                return self._send_static("/".join(segment[1:]))
            if path == "/api/health":
                return self._send_json({"ok": True})
            if path == "/api/doctor":
                # 平台体检（ / D5）：**只读**，与 CLI 同一份报告模型。
                # `?hardware=1` 是唯一的硬件动作（只枚举端口名，不打开串口），
                # 默认不跑 —— 页面必须由人点一下才会带上这个参数。
                payload = build_doctor_payload(
                    host=str(self.server.server_address[0] or "127.0.0.1"),
                    port=int(self.server.server_address[1]),
                    hardware="hardware" in query,
                    assembly=getattr(self.server, "assembly", None),
                )
                if "download" in query:
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    return self._send_bytes(
                        body,
                        "application/json; charset=utf-8",
                        download_name="doctor-report.json",
                    )
                return self._send_json(payload)
            if path == "/api/state":
                # 实时观察的会话状态与自动验证的状态放在同一个快照里：页面一次轮询
                # 就能把“谁拿着串口”画全，不需要两个可能对不上的请求。
                payload = self.pipeline.snapshot()
                payload["observe"] = self.server._observe_payload()
                # 受控测试的**目录**（ / L4）：服务端按这条会话算好的
                # "哪些用例能跑、为什么不能"。没连接 / 项目包没有协议 / 一条用例都没有
                # 时也给块，但 `available=false` 且写明原因 —— 页面据此决定
                # "选择受控用例"是可用还是禁用（需求 §8）。
                payload["observe"]["live_test_catalog"] = self.server._live_test_catalog_block()
                payload["server"]["sessions_dir"] = str(self.sessions_dir)
                # 平台自述（R2d / DEC-010）：这是哪套组合、能力、适配器 ID、按哪个 schema
                # 写的。⚠️ 只增字段：页面不认识就不读（向后兼容），而诊断包与会话 manifest
                # 用的是**同一个** `platform_info.describe()`，两处不会各说一套。
                payload["platform"] = self.platform_facts()
                payload["web_catalog"] = self.server.web_catalog.to_json()
                # 设备嗅探（ / D1）：**只读**一块 —— 候选口、声明的探针、
                # 上一次结果。⚠️ 读它不碰任何端口；“上一次结果”只在有人真点过嗅探之后才有值。
                if getattr(self.server, "sniff", None) is not None:
                    payload["sniff"] = self.server._sniff_state()
                # 导出任务自述（ / P2）：页面上"正在准备包"的那一根进度条
                # 就是从这里读的（与搜索任务一个路子）。⚠️ 只增字段。
                payload["exports"] = self.bundles.snapshot()
                # 导入任务自述（ / P3）：页面上的上传/校验/解压/发布四段
                # 就是从这里读的。没有观察会话的装配（observe=None）就没有导入目录，"
                # 于是这一段整个不出现。⚠️ 只增字段。
                if self.server.observe is not None:
                    payload["imports"] = self.imports.snapshot()
                # 服务登记的那几套装配（W4c）：一条链一行 —— 页面与验收脚本用它核对
                # "选了哪个 firmware"到底对应哪个项目包/套件/证据目录。
                assembly = getattr(self.server, "assembly", None)
                if assembly is not None:
                    payload["assemblies"] = assembly.to_json()
                # 构建戳（）：页面拿它判断"我还是不是最新的那一份"。
                # ⚠️ 只增字段：旧页面不认识就不读。
                payload["build"] = self.server.build_facts()
                return self._send_json(payload)
            if path == "/api/runs":
                return self._send_json({"runs": self.pipeline.runs()})
            if path == "/api/stream":
                return self._stream_events(query)
            if segment[:2] == ["api", "runs"] and len(segment) >= 3:
                run_id = segment[2]
                if len(segment) == 3:
                    # `?content=auto|none|inline`（ / L2）：默认 auto —— 只内联
                    # **小于阈值**的正文，超出的不读（需求 PERF-HIST-01）。
                    return self._send_json(
                        self.pipeline.run_detail(
                            run_id, content=str(query.get("content", ["auto"])[0])
                        )
                    )
                if len(segment) == 4 and segment[3] == "report":
                    report = self.pipeline.store.read_json(run_id, "report.json")
                    return self._send_json(report or {})
                if len(segment) == 5 and segment[3] == "evidence":
                    return self._send_evidence(
                        run_id, segment[4], download="download" in query
                    )
            if segment[:3] == ["api", "observe", "sessions"] and len(segment) == 3:
                # 会话清单（切片 6）：只读目录里的 manifest，不碰串口。
                # 切片 8 起把**导入的包**也列进来（每行带 origin，界面必须能分辨）。
                #
                # `?include=<会话号>`（P4）：这一条**必须在清单里**（超越了默认条数也带着）。
                # 刚导入的包可能是个很老的会话（号比本机近期几十次都小），按时间截断会把它
                # 默默丢掉 —— 于是"四个窗口装着它，下拉框里却没有它"。
                return self._send_json({
                    "sessions": session_review.list_all_sessions(
                        self.sessions_dir,
                        self.imports_dir,
                        active_session_id=self.observe.active_session_id(),
                        include=str(query.get("include", [""])[0]).strip() or None,
                    ),
                    "sessions_dir": str(self.sessions_dir),
                    "imports_dir": str(self.imports_dir),
                })
            if segment[:3] == ["api", "observe", "sessions"] and len(segment) == 4:
                # 回看：四个窗口的内容 + 从文件里数出来的计数（整包都不碰串口）。
                # `?content=` 与 run 详情同一份名目（ / L2）。
                try:
                    root, origin = session_review.resolve_session(
                        self.sessions_dir, self.imports_dir, segment[3]
                    )
                    return self._send_json(
                        session_review.session_detail(
                            root,
                            segment[3],
                            origin=origin,
                            content=str(query.get("content", ["auto"])[0]),
                        )
                    )
                except session_review.SessionNotFound as error:
                    return self._send_error_json(404, str(error))
                except large_evidence.EvidenceError as error:
                    return self._send_error_json(error.status, error.message, code=error.code)
            if (
                segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 5
                and segment[4] == "bundle"
            ):
                # 整个诊断包（zip）：原样的证据字节 + README.txt + session.json（带 sha256）。
                #
                #  / P2：**不再在内存里成包**。导出器把包逐块写进会话目录里的
                # 临时文件（快照边界 = 开始那一刻的大小），再由 `_send_file()` 流式发出去 ——
                # 于是 400 MiB 的会话不会变成 3 倍常驻内存，Range 与断连也走同一条已验证的路。
                # 准备是**同步**的（这条路是给"点一下就去下载"用的）；想一边准备一边看进度
                # 的调用方走 `POST .../bundle` + `GET .../bundle/<task_id>`。
                try:
                    root, _origin = session_review.resolve_session(
                        self.sessions_dir, self.imports_dir, segment[3]
                    )
                    _info, path = bundle_export.export_session(
                        root,
                        segment[3],
                        free_bytes=self.bundles.free_bytes(root),
                    )
                except session_review.SessionNotFound as error:
                    return self._send_error_json(404, str(error))
                except bundle_export.BundleError as error:
                    return self._send_error_json(error.status, error.message, code=error.code)
                return self._send_file(
                    Path(path),
                    download_name=bundle_export.bundle_name(segment[3]),
                    media_type="application/zip",
                )
            if (
                segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 6
                and segment[4] == "bundle"
            ):
                # 导出任务的进度（P2/P4）：页面按 task_id 轮询这一个接口。
                try:
                    return self._send_json(self.bundles.outcome(segment[5]).to_json())
                except KeyError:
                    return self._send_error_json(404, f"没有这个导出任务：{segment[5]}")
            if segment[:3] == ["api", "observe", "import"] and len(segment) == 4:
                # 导入任务的进度（P3/P4）：页面按 task_id 轮询这一个接口
                # （上传字节/总字节 + 阶段 + 当前成员 + 结果与错误码）。
                try:
                    return self._send_json(self.imports.outcome(segment[3]).to_json())
                except KeyError:
                    return self._send_error_json(404, f"没有这个导入任务：{segment[3]}")
            if (segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 6
                and segment[4] == "evidence"
            ):
                # 会话证据：页面上的四个窗口在实时观察时指向这里
                # （`raw.bin` / `io_events.jsonl` / `manifest.json`）。
                return self._send_session_evidence(
                    segment[3], segment[5], download="download" in query
                )
            if segment[:2] == ["api", "search"] and len(segment) == 3:
                # 搜索任务的进度与结果（L4）：页面按 task_id 轮询这一个接口。
                try:
                    return self._send_json(self.search.outcome(segment[2]))
                except KeyError:
                    return self._send_error_json(404, f"没有这个搜索任务：{segment[2]}")
            if segment[:2] == ["api", "runs"] and len(segment) == 5 and segment[3] == "index":
                return self._send_run_index(
                    segment[2], segment[4], build="build" in query
                )
            if (
                segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 6
                and segment[4] == "index"
            ):
                return self._send_session_index(
                    segment[3], segment[5], build="build" in query
                )
            return self._send_error_json(404, f"未知路径 {path}")
        except RequestError as error:
            return self._send_error_json(error.status, str(error))
        except large_evidence.EvidenceError as error:
            # 查询本身不合法（空 / 超长 / 没登记的模式）在这里退回 400 或 409。
            return self._send_error_json(error.status, error.message, code=error.code)
        except ValueError as error:
            return self._send_error_json(400, str(error))
        except OSError as error:
            return self._send_error_json(500, f"服务端文件错误: {error}")
        except Exception as error:  # noqa: BLE001 - 兜底：未预料的失败也要说出口
            # ⚠️ 不兜底的话，处理线程会直接结束 ⇒ 连接被关、页面只看到
            # “Failed to fetch”，而真正的原因只在服务端终端里（本仓库踩过：
            # 只读观察链没装配 discovery 适配器，页面与 `--stop` 一起无声失效）。
            return self._send_error_json(500, f"{type(error).__name__}: {error}")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            segment = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if path == "/api/observe/import":
                # 导入诊断包（切片 8）：请求体是**原样的 zip**，不是 JSON ——
                # 所以这条路由必须在下面那个 JSON 分支**之前**处理。
                return self._import_bundle()
            if path == "/api/search":
                # 起一个全文件搜索（L4，需求 PERF-SEARCH-02）：立刻回 202 + task_id，
                # 进度与命中由 /api/search/<task_id> 查 —— 400 MB 不会让人盯着一个请求等。
                return self._send_json(self.search.start(self._read_json_body()), status=202)
            if (
                segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 5
                and segment[4] == "bundle"
            ):
                # 异步准备一个诊断包（ / P2，需求 PACK-EXPORT-03）：
                # 立刻回 202 + task_id，进度由 `GET .../bundle/<task_id>` 查。
                # “同一会话只许一趟”在**登记**时就生效（返回 202 之前就已经拒了）。
                try:
                    task = self.bundles.start_async(segment[3])
                except bundle_export.BundleError as error:
                    return self._send_error_json(error.status, error.message, code=error.code)
                except session_review.SessionNotFound as error:
                    return self._send_error_json(404, str(error))
                return self._send_json(task.to_json(), status=202)
            if (
                segment[:3] == ["api", "observe", "sessions"]
                and len(segment) == 7
                and segment[4] == "bundle"
                and segment[6] == "cancel"
            ):
                # 取消 = **只置旗子**：导出循环每块看一眼，自己停下来并把临时文件删掉。
                try:
                    cancelled = self.bundles.cancel(segment[5])
                except KeyError:
                    return self._send_error_json(404, f"没有这个导出任务：{segment[5]}")
                return self._send_json({"task_id": segment[5], "cancelled": cancelled})
            if (
                segment[:3] == ["api", "observe", "import"]
                and len(segment) == 5
                and segment[4] == "cancel"
            ):
                # 取消一个正在跑的导入（P3）：上传/解压循环每块看一眼，自己停下来。
                try:
                    cancelled = self.imports.cancel(segment[3])
                except KeyError:
                    return self._send_error_json(404, f"没有这个导入任务：{segment[3]}")
                return self._send_json({"task_id": segment[3], "cancelled": cancelled})
            if path == "/api/sniff":
                # 设备嗅探（ / D1）：整个平台里**唯一**能发身份查询的入口。
                # ⚠️ 打开页面 / 刷新 / 切模式 / 选固件都走不到这里 —— 只有人点“设备嗅探”。
                # 请求体一律读掉（HTTP/1.1 keep-alive 下不读会让下一个请求解析错位）。
                self._read_json_body()
                executor = getattr(self.server, "sniff", None)
                if executor is None:
                    return self._send_error_json(
                        503, "本服务没有装配设备嗅探（没有可用的候选口或传输实现）"
                    )
                # ⚠️ 嗅探内部已经逐口兜住异常（`sniff_service`），
                #    这里再兜一层是因为“一个口的意外”以前会把**整轮**变成 HTTP 500，
                #    而页面只能显示“与服务的连接断了”（与真正原因毫无关系）。
                #    真到了这一步：仍然是 202 + 结果，缺的那一轮写在 notes 里。
                try:
                    result = executor.sniff()
                except sniff_service.SniffBusy as error:
                    # 并发的第二轮（需求 §12.2）：**确定性 409**。闸门就在 `sniff()` 的第一行
                    # ⇒ 走到了这里说明**一个字节都没发**、也没有多探一次口。
                    # 页面那个 `sniffBusy` 只是交互反馈，不是资源锁。
                    return self._send_error_json(409, str(error), code="sniff_busy")
                except Exception as error:  # noqa: BLE001 - 不能把一个内部异常变成“断线”
                    print(f"[sniff] 这一轮没能跑完：{type(error).__name__}: {error}",
                          file=sys.stderr)
                    candidates = getattr(executor, "candidates", None)
                    return self._send_json(
                        {
                            "ok": False,
                            "sniff": {
                                "candidates": list(getattr(candidates, "ports", ()) or ()),
                                "cards": [],
                                "notes": [
                                    "这一轮嗅探在服务端出错了"
                                    f"（{type(error).__name__}: {error}）："
                                    "这是平台的问题，不是「没有设备」——"
                                    "候选口清单仍然列在上面",
                                ],
                            },
                        },
                        status=202,
                    )
                return self._send_json({"ok": True, "sniff": result.to_json()}, status=202)
            if path == "/api/run":
                body = self._read_json_body()
                try:
                    selection = self.server.web_catalog.selection(
                        str(body.get("target_id", "")),
                        str(body.get("firmware_id", "")),
                        str(body.get("suite", "")),
                    )
                    prepared_tests, test_inputs = self.server.web_catalog.prepare_test_inputs(
                        selection, body.get("tests")
                    )
                    # 复位默认值来自**这个目标自己的声明**（`ResetPolicy.default_request`）：
                    # 串口调制线复位默认开（A1 要完整启动横幅），走 SWD 探针的复位默认关
                    # （：附加会占住探针）。调用方没明说时就按这个默认，而不是
                    # 平台写死一个 True —— 那等于替用户决定去动一块板的复位。
                    reset_default = self._reset_default(selection.firmware_id)
                    #  / D2：页面如果没有点名“哪个设备”，就按老路径跑（静态登记里
                    # 那一块板的口）；点名了就由服务端复核成 `RunTarget` 再交进去。
                    run_target = self.server._run_target(selection, body.get("device"))
                    result = self.pipeline.start(
                        selection.firmware_id,
                        confirm_flash=bool(body.get("confirm_flash", False)),
                        suite=selection.suite,
                        target=selection.device_key,
                        run_target=run_target,
                        observe_seconds=body.get("observe_seconds"),
                        reset=bool(
                            body["reset"] if "reset" in body else reset_default
                        ),
                        project_options=(
                            body.get("project_options")
                            if isinstance(body.get("project_options"), dict) else {}
                        ),
                        test_selections=list(prepared_tests),
                        test_inputs=test_inputs,
                        client=body.get("client") if isinstance(body.get("client"), dict) else None,
                    )
                except RequestError as error:
                    # Broadcast the refusal: the tab that was rejected shows the
                    # reason, and every *other* open tab learns that a second
                    # driver just tried to take the board.
                    self.pipeline.note_rejected_request(
                        str(error), status=error.status, client=body.get("client")
                    )
                    raise
                return self._send_json(result, status=202)
            if (
                segment[:2] == ["api", "runs"]
                and len(segment) == 4
                and segment[3] == "cancel"
            ):
                cancelled = self.pipeline.cancel(segment[2])
                return self._send_json({"run_id": segment[2], "cancelled": cancelled})
            if (
                segment[:2] == ["api", "search"]
                and len(segment) == 4
                and segment[3] == "cancel"
            ):
                # 取消 = **只置旗子**：扫描循环每块看一眼，自己停下来。
                try:
                    cancelled = self.search.cancel(segment[2])
                except KeyError:
                    return self._send_error_json(404, f"没有这个搜索任务：{segment[2]}")
                return self._send_json({"task_id": segment[2], "cancelled": cancelled})
            if path.startswith("/api/observe/"):
                return self._observe_action(path)
            if path == "/api/stop":
                # 优雅停服（）：它不是 observe 的子路由 —— 停的是**这台服务**。
                return self._stop_server(self._read_json_body())
            return self._send_error_json(404, f"未知路径 {path}")
        except (RequestError, ObserveError) as error:
            return self._send_error_json(error.status, str(error))
        except large_evidence.EvidenceError as error:
            # 搜索本身被拒（正在跑、查询非法、证据不认识）：状态码与错误码都来自 L0 的表。
            return self._send_error_json(error.status, error.message, code=error.code)
        except ValueError as error:
            return self._send_error_json(400, str(error))
        except Exception as error:  # noqa: BLE001 - 兜底：未预料的失败也要说出口
            # ⚠️ 不兜底的话，处理线程会直接结束 ⇒ 连接被关、页面只看到
            # “Failed to fetch”，而真正的原因只在服务端终端里（本仓库踩过：
            # 只读观察链没装配 discovery 适配器，页面与 `--stop` 一起无声失效）。
            return self._send_error_json(
                500, f"{type(error).__name__}: {error}"
            )

    def _import_bundle(self) -> None:
        """导入一个诊断包（切片 8 /  P3）。

        它**不碰串口**、不编译、不烧录：只是把一份别人给的 zip 校验一遍，然后原样存进
        `observe-imports//`。文件名走 `X-Bundle-Filename` 这个头（可选，只用于记录
        "这份包是从哪个文件来的"）—— 不解析 multipart，因为这里只需要字节。

        ⚠️ P3 起它**先流式落盘**：先看 `Content-Length`（没有就拒）→ 按固定块写进
        `<imports>/.incoming-<task>.zip` → 从中央目录预检 → 逐块解压并算哈希 → 核对包内自述
        → 同目录原子改名。于是服务端内存不再随包体或解压体量增长（PACK-SAFE-02），
        而“先拦后读”依然成立（长度/上限/盘不够 都在写第一个字节之前判）。

        ⚠️ P4：**任务号可以由客户端先给**（`X-Import-Task` 头，可选）。上传是在这个请求里
        同步跑完的，页面想知道“传到哪儿了、还要不要继续”就必须在上传**开始之前**拿到任务号 ——
        先给号、再传字节，页面才能一边传一边查进度、也能随时取消（PACK-UI-01）。
        没给就由服务端生成，行为与 P3 一样（老调用方不用改）。
        """
        filename = self.headers.get("X-Bundle-Filename") or ""
        task_id = (self.headers.get("X-Import-Task") or "").strip() or (
            f"import-{uuid.uuid4().hex[:8]}"
        )
        if not large_bundle.task_id_ok(task_id):
            # 任务号会当文件名用 ⇒ 不合法就在这里拒，而且**一个字节都还没收**（所以要关连接）。
            return self._send_error_json(
                400,
                f"任务号不合法：{task_id!r}（只收字母/数字/-/_，且不超过 "
                f"{large_bundle.TASK_ID_MAX} 字符）",
                close=True,
            )
        try:
            declared = self._declared_body_length()
            task = self.imports.register_as(
                task_id,
                source_name=filename,
                declared_bytes=declared,
            )
            self.imports.run(
                task.task_id,
                self._body_chunks(declared),
                declared_bytes=declared,
                source_name=filename,
            )
        except bundle_stream_import.BundleError as error:
            if error.code in {"upload_incomplete", "import_cancelled"}:
                # 连接断了/任务被取消：把半包与临时目录收掉，别让它们以“正在跑”的样子留着。
                self._abandon_current_import(error.message)
            # 这几条都发生在**还没把请求体收完**的时候 ⇒ 必须关连接，
            # 否则剩下的字节会被当成下一个请求（`import_task_busy` 也是：忙是**登记**时
            # 就判出来的，那时一个字节都还没收）。
            return self._send_error_json(
                error.status,
                error.message,
                code=error.code,
                close=error.code
                in {
                    "upload_length_unknown",
                    "upload_too_large",
                    "upload_incomplete",
                    "disk_insufficient",
                    "import_task_busy",
                    "import_cancelled",
                },
            )
        result = self.imports.outcome(task.task_id).result or {}
        return self._send_json({"imported": result, "task": task.to_json()}, status=202)

    def _abandon_current_import(self, why: str) -> None:
        """把最近一个**没跑完**的导入任务收拾掉（半包 + 临时目录都删掉）。"""
        for row in reversed(self.imports.snapshot()["tasks"]):
            if row["state"] == "running":
                self.imports.abandon(row["task_id"], why=why)
                return

    def _reset_default(self, firmware_id: str) -> bool:
        """这个固件在"调用方没明说复位"时的默认值（来自它自己的目标声明）。

        装配里没有这一份（例如只给了 catalog 的测试替身）时退到 `False`：
        一个会动硬件的默认值，宁可"不做"也不要"猜着做"。
        """
        assembly = getattr(self.server, "assembly", None)
        if assembly is None:
            return False
        row = assembly.by_firmware(firmware_id)
        return bool(row.reset_default) if row is not None else False

    def _observe_action(self, path: str) -> None:
        """连接 / 断开 / 重连 / 切目标。都是**显式**动作：没有任何一个是隐式的。

        请求体一律读掉（HTTP/1.1 keep-alive 下不读会让下一个请求从残留的 body
        开始解析）。打开失败 / 串口消失 / 被别的模式占用会作为**状态**返回 200，
        因为页面要把它们显示成状态（需求 §3.2），而不是把它当接口调用失败。
        """
        body = self._read_json_body()
        if path == "/api/observe/select":
            # 切"实时观察哪一个目标/固件"（W5）：只换选中的那一路，**不连接、不打开串口**。
            # 另一路还握着串口时中心会以 409 拒绝（隐式断开别人的串口更糟）。
            return self._send_json(
                {"observe": self.observe.select(str(body.get("firmware_id", "")))}
            )
        if path == "/api/observe/connect":
            #  / D6：实时观察也按**本次嗅探的那张卡片**选设备。
            # 页面点的是"哪一台设备"（`{scan_seq, port}`），而这一路连接要的是**设备键**
            # （键上挂着波特率、行结束符、字典与能力声明），所以键由**服务端**按
            # "哪一条登记的口就是这个口"定下来：**唯一匹配才算**，一个都不匹配或匹配到多条
            # 都如实拒绝并把可选的说出来 —— 平台不按枚举顺序、也不按"唯一在线"去猜。
            requested = body.get("device")
            if isinstance(requested, dict):
                # ⚠️ 这个方法在 **ConsoleServer** 上（它要用 `self.sniff` 的本次结果和
                # `self.observe` 的会话登记），handler 只是转一手 —— 漏掉 `self.server.`
                # 会在页面点卡片连接时炸成 500（真机驱动时抓到过）。
                #
                #  / D9（需求 §9.1）：固件**不由页面指定** —— 服务端拿这张卡片的
                # **实测身份**唯一匹配 firmware profile（`_observe_combination`），
                # 再据此装配观察组合。
                # ⚠️ 用户口径（2026-10-13）：**身份不是连接的前置条件**。零匹配 / 多匹配 /
                #    问不到身份一律**照连**（`transparent=True`）—— 页面说“身份未识别，已进入
                #    原始串口模式”，连上之后会话自己会去问身份，问不出来就按原始串口收字节
                #    （raw.bin / io_events.jsonl 照旧落盘）。
                #    “多匹配”那条 409 只属于**烧录**（自动验证要选哪一份固件），
                #    不该把实时观察也一起堵死（用户口径：实时观察不得因此阻塞）。
                # ⚠️ 请求体里若还带着 `firmware_id`（旧页面 / 脚本），一律**忽略**：
                #    “页面说了算”这条路正是 §9.1 要拆掉的那一条。匹配结论随响应一起回
                #    （`firmware_match`），页面只展示。
                card, match = self.server._observe_combination(requested)
                transparent = not match.usable
                self.observe.connect(
                    self.server._observe_device_key(requested, match.firmware_id),
                    firmware_id=(match.firmware_id or None),
                    transparent=transparent,
                    # 需求 §6.10：本次连接的串口参数（波特率 / 数据位 / 校验位 / 停止位）。
                    # 不传 = 沿用上一次；非法值当场 400，不会“默默用默认值”。
                    serial=body.get("serial"),
                )
                # ⚠️ `center.connect()` 返回的是**那一份会话**的快照；这里再取一次**中心**的
                # 快照，于是响应里的 `observe.firmware_id` / `targets[]` 与 `/api/state`
                # 完全一致（§9.1 要的正是“observe.firmware_id 与身份、Project Package、
                # 解码能力、catalog 同源”）。
                return self._send_json(
                    {"observe": self.server._observe_payload(),
                     "firmware_match": match.to_json()},
                    status=202,
                )
            # 兜底：`device` 直接给设备键（CLI / 宿主夹具）。这条路没有卡片，
            # 也就没有实测身份可匹配 ⇒ 固件仍按调用方给的那一份（服务端先 select）。
            snapshot = self.observe.connect(
                str(requested or ""),
                firmware_id=str(body.get("firmware_id") or "") or None,
                serial=body.get("serial"),
            )
            return self._send_json({"observe": snapshot}, status=202)
        if path == "/api/observe/disconnect":
            reason = str(body.get("reason") or "用户在页面上断开")
            return self._send_json({"observe": self.observe.disconnect(reason=reason)})
        if path == "/api/observe/reconnect":
            return self._send_json(
                {"observe": self.observe.reconnect(serial=body.get("serial"))}, status=202
            )
        if path == "/api/observe/send":
            # 一次发送：文本（正文 + 所选行结束符）或纯十六进制。
            # 非法输入会以 400 回来，并且**一个字节也不会发出去**。
            sent = self.observe.send(
                mode=str(body.get("mode") or "text"),
                text=str(body.get("text") or ""),
                line_ending=body.get("line_ending"),
            )
            return self._send_json(
                {"sent": sent, "observe": self.observe.snapshot()}, status=202
            )
        if path == "/api/observe/keys":
            # 终端直输：一批**按键描述**（不是字节）。按键 → 字节的换算只在服务端做，
            # 页面只说"按了什么"—— 于是映射只有一份实现（需求 §4.2）。
            sent = self.observe.send_keys(body.get("keys"))
            return self._send_json(
                {"sent": sent, "observe": self.observe.snapshot()}, status=202
            )
        if path == "/api/observe/tests/start":
            # 受控测试：显式启动（需求 §8：启动必须显式）。
            return self._send_json(self.server._start_live_tests(body), status=202)
        if path == "/api/observe/tests/cancel":
            return self._send_json(self.server._cancel_live_tests(body), status=202)
        if path == "/api/observe/identity":
            # 保留手动补发入口；常规身份握手已在连接/重连成功后自动执行。
            sent = self.observe.query_identity()
            return self._send_json(
                {"sent": sent, "observe": self.observe.snapshot()}, status=202
            )
        return self._send_error_json(404, f"未知路径 {path}")

    def _stop_server(self, body: dict) -> None:
        """优雅停服（）：强停会留下陈旧工作区锁，把下一次启动拒掉。

        ⚠️ 正握着串口 / 正跑着 run 时**拒绝**（除非 `force=true`）：停服不该把一次正在进行
        的采集或一轮自动验证拦腰砍断 —— 那种"半截证据"最难查。拒绝时把**是谁占着**说出来，
        于是下一步该做什么是明确的（先断开 / 先取消 run / 或者显式 force）。
        """
        force = bool(body.get("force"))
        snapshot = self.observe.snapshot()
        state = str(snapshot.get("state") or "")
        holding = state in HOLDING_STATES
        busy = bool((self.pipeline.snapshot() or {}).get("busy"))
        blockers: list[str] = []
        if holding:
            port = (snapshot.get("device") or {}).get("port") or snapshot.get("port") or "?"
            blockers.append(f"实时观察正握着 {port}（{snapshot.get('state_label') or state}）")
        if busy:
            blockers.append("有一轮自动验证正在跑")
        if blockers and not force:
            return self._send_error_json(
                409,
                "没有停："
                + "；".join(blockers)
                + "。先断开 / 先取消，或者显式 force（`--stop --force`）"
                + "—— 停服不该把正在进行的采集或验证拦腰砍断。",
            )

        reason = str(body.get("reason") or "用户在页面上停服")

        def _stop() -> None:
            # 先把锁释放掉：锁还在的话，下一次启动会被拒 —— 而进程可能还要几百毫秒才退出。
            # （`release` 只允许持有者用它自己的 pid 释放，所以不会误删别人的锁。）
            console_lock.release(PROJECT_DIR, pid=os.getpid())
            self.server.shutdown()

        # ⚠️ 顺序要紧：**先把回答写出去**，再安排退出。反过来（先起定时器再写）
        # 在机器忙的时候会变成"还没回答就把自己关了" —— 发起方读到的是一个断掉的连接
        # （宿主回归当场撞到过：`OSError: 您的主机中的软件中止了一个已建立的连接`）。
        self._send_json(
            {"stopping": True, "force": force, "reason": reason, "released_pid": os.getpid()},
            status=202,
        )
        threading.Timer(0.1, _stop).start()
        return

    # ---------------------------------------------------------------- statics
    def _send_static(self, name: str) -> None:
        """静态资源（允许子目录，例如 `js/model/format.js`）。

        路径仍必须落在 `STATIC_DIR` 之内：`..`、盘符与绝对路径一律 404。
        接受 `Accept-Encoding: gzip` 时在传输层压一下，`Cache-Control` 保持 `no-store`。
        """
        if "\\" in name or name.startswith(".") or ":" in name:
            return self._send_error_json(404, "静态资源不存在")
        parts = [part for part in name.split("/") if part]
        if not parts or any(part in (".", "..") for part in parts):
            return self._send_error_json(404, "静态资源不存在")
        root = STATIC_DIR.resolve()
        target = root.joinpath(*parts).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._send_error_json(404, "静态资源不存在")
        if not target.is_file():
            return self._send_error_json(404, "静态资源不存在")
        content_type = STATIC_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        packed = self._packed_static(target, body)
        accepts = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if packed is not None and accepts:
            self._send_bytes(packed, content_type, content_encoding="gzip")
            return
        self._send_bytes(body, content_type)

    def _packed_static(self, target: Path, body: bytes) -> bytes | None:
        """文本类静态资源的 gzip 结果（按 mtime+大小 记住：`no-store` 下页面每次都重取）。"""
        try:
            info = target.stat()
            key = (str(target), info.st_mtime_ns, info.st_size)
        except OSError:
            return pack_static(body, target.suffix)
        if key in _PACKED_CACHE:
            return _PACKED_CACHE[key] or None
        packed = pack_static(body, target.suffix)
        if len(_PACKED_CACHE) >= _PACKED_CACHE_MAX:
            _PACKED_CACHE.clear()
        _PACKED_CACHE[key] = packed or b""
        return packed

    def _send_evidence(self, run_id: str, name: str, *, download: bool) -> None:
        """run 证据（**流式**，支持单段 Range）。

        ⚠️  / L2：这里以前是 `target.read_bytes()` —— 一份 400 MB 的
        `raw.bin` 会先进内存再发。现在统一走 `_send_file()`（读一块写一块）。
        """
        target = self.pipeline.store.artifact(run_id, name)
        if not target.is_file():
            return self._send_error_json(404, f"{name} 尚不存在")
        return self._send_file(
            target,
            download_name=f"{run_id}-{name}" if download else None,
            media_type=EVIDENCE_TYPES.get(name),
        )

    def _send_session_evidence(self, session_id: str, name: str, *, download: bool) -> None:
        """会话证据（`observe-sessions/<id>/…`，导入的包在 `observe-imports/<id>/…`）。

        越界与未知文件名交给 `artifact_path` 拒绝 —— 白名单在那里，不在这里再写一份。
        两个根走**同一套**规则，所以导入的会话也是同一条流式路径（需求 §3.6：不为某块板
        或某个项目写专用分支）。
        """
        try:
            root, _origin = session_review.resolve_session(
                self.sessions_dir, self.imports_dir, session_id
            )
            target = artifact_path(root, session_id, name)
        except (ValueError, session_review.SessionNotFound) as error:
            return self._send_error_json(404, str(error))
        if not target.is_file():
            return self._send_error_json(404, f"{name} 尚不存在")
        return self._send_file(
            target,
            download_name=f"{session_id}-{name}" if download else None,
            media_type=EVIDENCE_TYPES.get(name),
        )

    def _send_run_index(self, run_id: str, name: str, *, build: bool) -> None:
        """run 证据的稀疏索引（L4）：默认只报告"有没有、对不对得上"。

        `?build=1` 才建/重建 —— 建索引是一趟顺序读，不该在"打开页面"时悄悄发生。
        指纹优先用 `report.json` 里**已经记下**的那一份 SHA-256（不现算，PERF-HIST-07）。
        """
        try:
            target = self.pipeline.store.artifact(run_id, name)
        except ValueError as error:
            return self._send_error_json(400, str(error))
        digest = self.pipeline.recorded_digests(run_id).get(name)
        return self._send_index(target.parent, name, target, build=build, digest=digest)

    def _send_session_index(self, session_id: str, name: str, *, build: bool) -> None:
        """会话证据的稀疏索引：与 run 那条路**同一套**规则（含名字白名单）。

        ⚠️ 指纹的来源**分两种，且不许混**（P5）：
        - 本机采集的会话：没有任何地方记过它的文件哈希 ⇒ 指纹只按**大小**。
          页面/调用方会看到 `fingerprint: "size"` —— 不如实说"没得核对"才是错的。
        - **导入的包**：`import.json.declared.files[]` 里带着生成那一刻的 sha256，
          而且导入时逐份重算并逐份对照过（`verified=True`）⇒ 那是一份**已记下的可信哈希**，
          直接拿它当指纹（不必为了显示摘要把 400 MiB 再读一遍）。
        """
        try:
            root, _origin = session_review.resolve_session(
                self.sessions_dir, self.imports_dir, session_id
            )
            target = artifact_path(root, session_id, name)
        except (ValueError, session_review.SessionNotFound) as error:
            return self._send_error_json(404, str(error))
        digest = session_review.recorded_digest(root, session_id, name)
        # ⚠️ 索引放**这个会话自己的目录**下（`<会话目录>/index/<证据名>.json`），
        #    与 run 那条路同形（`large_index` 的注释就是这么写的）。
        #    以前这里传的是**两个根的父目录**（`observe-sessions/`），于是所有会话的
        #    `raw.bin` 索引挤在同一个文件里互相覆盖：指纹只按大小时，另一次会话的索引
        #    如果大小碰巧一样就会被当成"可用"，锚点却是别人的字节偏移（跳转到错的行）。
        return self._send_index(target.parent, name, target, build=build, digest=digest)

    def _send_index(
        self,
        root: Path,
        name: str,
        target: Path,
        *,
        build: bool,
        digest: str | None,
    ) -> None:
        if not target.is_file():
            return self._send_error_json(404, f"{name} 尚不存在")
        size = _file_size(target)
        source: str | None = None
        if build:
            index, source = large_index.ensure_index(root, name, target, digest=digest)
            reason = "ok" if source == "ok" else source
        else:
            index, reason = large_index.usable_index(
                root, name, source_bytes=size, source_sha256=digest
            )
        payload = large_index.describe(
            index, name=name, source_bytes=size, reason=reason, source=source
        )
        payload["built_now"] = bool(build)
        payload["download_bytes"] = size
        return self._send_json(payload)

    def _send_file(
        self,
        target: Path,
        *,
        download_name: str | None = None,
        media_type: str | None = None,
    ) -> None:
        """发送一份证据文件：**流式**、单段 Range、客户端断连不算错误。

        三条证据路（run / 实时会话 / 导入包）**共用这一处** —— "大文件路径不许整读"
        这条判据只要盯住这一个函数（加 `webconsole/http_range.py`）。
        正在写的文件以**请求开始那一刻**的大小为快照边界（L0 冻结的 `Snapshot`）。
        """
        # T6：也可以**按行区间**要窗口（定位按钮手里只有行号）。行号换算成字节 Range 之后
        # 走同一条流式路 —— “大文件不整读”这条不变（换算本身读得有界，拿不到就不给窗口）。
        range_header = self.headers.get("Range")
        by_lines = (self.headers.get(LINE_RANGE_HEADER) or "").strip()
        if by_lines:
            converted = self._range_for_lines(target, by_lines)
            if converted is None:
                return self._send_error_json(
                    416,
                    f"要不到这个行区间：{by_lines}（文件里没有这些行，或行首算不出来）",
                    code=large_evidence.ERROR_RANGE_UNSATISFIABLE,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{_file_size(target)}",
                    },
                )
            range_header = converted
        try:
            plan = http_range.plan_response(
                target,
                range_header=range_header,
                download_name=download_name,
                media_type=media_type,
            )
        except large_evidence.EvidenceError as error:
            return self._send_error_json(
                error.status,
                error.message,
                code=error.code,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{_file_size(target)}",
                },
            )
        headers = dict(plan.headers)
        base = self._line_base_header(target, plan.window)
        if base is not None:
            headers[LINE_BASE_HEADER] = base
        self.send_response(plan.status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if plan.window is None:
            return
        # 客户端中途关掉连接是**正常事件**（用户点了取消/关了标签页）：
        # `safe_stream` 把它记成 aborted 交回来，不抛给路由 —— 否则一次取消会在服务端
        # 留下一个异常，而下一个请求还要照常服务。
        http_range.safe_stream(target, plan.window, self.wfile.write)

    def _range_for_lines(self, target: Path, spec: str) -> str | None:
        """把 `X-Line-Range: 34-36` 换成 `bytes=a-b`；算不出来 ⇒ `None`。

        口径与 `evidence_refs` 一致：**1 基、含端点**。`end` 的下一行行首就是这一段的末尾
        （拿不到下一行行首 ⇒ 这一段到文件末尾）。**不猜**：任何一端算不出来就不给窗口 ——
        给个估值等于把"跳错地方"换个地方发生。
        """
        if not spec:
            return None
        head, _, tail = spec.partition("-")
        try:
            first = int(head.strip())
            last = int(tail.strip()) if tail.strip() else first
        except ValueError:
            return None
        if first < 1 or last < first:
            return None
        size = _file_size(target)
        if size <= 0:
            return None
        start = large_index.offset_at_line(target, first)
        if start is None:
            return None
        stop = large_index.offset_at_line(target, last + 1)
        end = (size - 1) if stop is None else max(int(start), min(size - 1, stop - 1))
        return f"bytes={int(start)}-{int(end)}"

    def _line_base_header(self, target: Path, window: object) -> str | None:
        """这一段的第一行是**文件**第几行（1 基）；不该给或算不出来 ⇒ `None`。

        三条"不该给"：① 客户端没要（`X-Line-Base: 1` 是显式的 —— 下载与整份发送都不需要，
        别为此白扫一遍 400 MiB）；② 窗口从 0 字节开始（那一行恒为第 1 行）；③ 这份文件数不
        出来行号（空文件、读不了）。

        ⚠️ 算不出来时**什么都不给**，而不是给一个猜的数 —— 面板行号存在的意义就是跟
        搜索结果里的行号对上，给个估值等于把"对不上"换个地方发生。
        """
        if (self.headers.get(LINE_BASE_HEADER) or "").strip() != "1":
            return None
        if window is None or int(getattr(window, "start", 0)) <= 0:
            return None
        try:
            value = large_index.line_at_offset(target, int(window.start))
        except (OSError, ValueError):
            return None
        return None if not value or int(value) < 1 else str(int(value))

    # -------------------------------------------------------------------- SSE
    def _stream_events(self, query: dict[str, list[str]]) -> None:
        """新订阅者先收**一小段**积压，再收实时（）。

        三条规矩（给多少 / 怎么给 / 少了怎么说）：
         1. `since` 由调用方给（页面给的是自己的游标 `app.lastSeq`；新页面第一次是 0）。
            0 的含义是"我什么都不知道，给我最近的一段"，**不是**"把整包都给我"。
         2. 真正推下去的是 `replay_window()` 选出的**有界**那一段，且每条都带 `replay=True`：
            页面据此把历史与"这次新到的"分开 —— 实时那一屏只认**正在跑的那次会话**的行，
            旧会话的积压不再冒充实时（那是用户报的"未连接却在逐行出数据"）。
         3. **有跳过就先说**：一条 `replay` 自述（条数 / 字节 / 过大帧 / 水位范围）——
            "积压有上限"是允许的，"少了不说"不是。
        """
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        channel = self.pipeline.subscribe()
        try:
            replayed, report = self.pipeline.replay_window(since)
            if report["available"]:
                note = (
                    f"积压重放：最近 {report['kept']} 条"
                    f"（seq {report['from_seq']}–{report['to_seq']}，"
                    f"{report['bytes']} B / 预算 {report['byte_budget']} B）"
                )
                if report["skipped"]:
                    note += f"；跳过更早的 {report['skipped']} 条"
                    if report["oversize"]:
                        note += f"（其中过大帧 {report['oversize']} 条）"
                    note += " —— 完整内容在证据文件里"
                if not self._chunk_event({"kind": "replay", "ts": utc_now(), "message": note, **report}):
                    return
            for event in replayed:
                if not self._chunk_event(event):
                    return
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    if not self._write_chunk(b": keepalive\n\n"):
                        return
                    continue
                if not self._chunk_event(event):
                    return
        finally:
            self.pipeline.unsubscribe(channel)
            self._write_chunk(b"")

    def _chunk_event(self, event: dict) -> bool:
        payload = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        return self._write_chunk(payload.encode("utf-8"))

    def _write_chunk(self, data: bytes) -> bool:
        try:
            if data:
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            else:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return False


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """Treat a peer closing an idle keep-alive socket as a normal disconnect."""
        error = sys.exc_info()[1]
        if isinstance(error, http_range.DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)

    def __init__(
        self,
        address,
        handler,
        pipeline,
        observe: ObserveSession,
        platform: dict | None = None,
        catalog: object | None = None,
        assembly: object | None = None,
        search: object | None = None,
        bundles: object | None = None,
        imports: object | None = None,
        sniff: object | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.pipeline = pipeline
        self.observe = observe
        self._imports = imports
        #  / D1：设备嗅探的执行器（构造不碰端口；只有 `POST /api/sniff` 会跑它）。
        # 没装配（旧入口 / 部分宿主测试）⇒ `/api/state` 里没有 `sniff` 块，路由报 503。
        self.sniff = sniff
        # L4：搜索任务登记处。没显式给就在这里按"这份装配知道的证据路"建一个：
        # 解析（含名字白名单）走 `store.artifact()` / `artifact_path()`，不另写一份。
        self.search = search or search_center.SearchCenter(
            search_target_resolver(pipeline, observe)
        )
        # P2：导出任务登记处。它只认"会话号 → 它住在哪个根目录"这个函数，
        # 所以本机会话与导入包**走同一条**导出路（两份根都用同一套路径规则）。
        # ⚠️ `observe` 可以是 None（只跑搜索的那些宿主测试就是这样装的）：那就用一个
        # "谁都不存在"的解析器，而不是让构造函数去碰 `None.sessions_dir`。
        self.bundles = bundles or bundle_export.ExportCenter(
            bundle_root_resolver(observe) if observe is not None else no_sessions_resolver,
            roots=() if observe is None else (observe.sessions_dir, observe.imports_dir),
            free_space=(
                None if observe is None else lambda path: shutil.disk_usage(path).free
            ),
        )
        self.platform = dict(platform or {})
        self.web_catalog = catalog or web_catalog.WebCatalog((), ())
        # 多目标装配（W4c/W5）：页面与验收脚本用它核对"选中的 firmware ↔ 项目包 ↔
        # 套件 ↔ Pipeline ↔ 证据目录 ↔ 自述"是不是同一条链。没给时页面少几个只读字段，
        # 但路由行为不变（`/api/run` 的复位默认值退到"不复位"）。
        self.assembly = assembly
        self.started_utc = utc_now()

    def build_facts(self) -> dict:
        """页面用它判断"我还是不是最新的那一份"（）。

        - `pid` / `started_utc`：**服务重启过**（进程内的状态、SSE 游标全变了）
        - `static`：**前端文件变了**（静态资源是每次请求重读的，所以刷新页面就能拿到新的）

        它只说事实，不说"该不该刷新"——那句提示由页面拼（并且**不自动刷新**：
        自动刷新会把正在读的阅读位置与暂停状态弄丢）。
        """
        return {
            "pid": os.getpid(),
            "started_utc": self.started_utc,
            "static": static_stamp(),
        }

    def _observe_payload(self) -> dict:
        """`observe` 那一块（`/api/state` 与连接响应**共用**）。

        ⚠️ 用户口径（2026-10-13）：「身份未识别，已进入原始串口模式」这句话由**服务端**给
        （页面只负责在状态行上说一遍），所以原始串口模式下多带一个 `transparent_reason`。
        它同时也是受控用例块里那条 `applicable=False` 的理由 —— 两处同一个来源。
        """
        row = dict(self.observe.snapshot())
        if row.get("transparent"):
            row["transparent_reason"] = TRANSPARENT_LIVE_TEST_REASON
        # 需求 §6.10：基础串口参数（现值 / 上次实际设上的 / 可选值）由**会话**给，
        # 页面只渲染，不自己维护第二份默认值。
        reader = getattr(self.observe, "serial_settings", None)
        if callable(reader):
            row["serial"] = reader()
        return row

    def _sniff_state(self) -> dict:
        """嗅探那一块：**只读**，而且每张卡片带上服务端算好的逐固件兼容性结论。

        ⚠️ 兼容性只在这里算一次（`web_catalog.compatible_firmwares`）：页面拿到的是结论
        与理由，不是“自己去比名字”。上一次结果用的是**真对象**（执行器手里的
        `SniffResult`），所以这里不需要从 JSON 反推一张卡片。

         / D9（需求 §9.1）：每张卡片**还**带一份 `match` —— “这次实测身份
        对应哪一份固件”的唯一结论（`web_catalog.match_firmware`）。观察组合、
        `observe.firmware_id`、解码能力与 `live_test_catalog` 都按它装配：
        唯一命中自动采用，零/多匹配必须有理由且**不许挑一个**。
        """
        state = dict(self.sniff.state())
        result = getattr(self.sniff, "last_result", None)
        if result is None:
            return state
        rows = []
        for card in result.cards:
            row = card.to_json()
            row["firmwares"] = list(
                self.web_catalog.compatible_firmwares(
                    card, flasher_bound=self._flasher_bound_for_port(card.port)
                )
            )
            row["match"] = self._match_for_card(card).to_json()
            rows.append(row)
        state["last"] = dict(result.to_json(), cards=rows)
        return state

    def _match_for_card(self, card: Any) -> Any:
        """一张设备卡片 ⇒ 身份驱动的运行组合（**服务端唯一判定**，需求 §9.1）。

        判定在 core（`device_sniff.match_firmware`）；这里只把同一套装配事实凑齐。
        ⚠️ 与卡片上的逐固件兼容性用**同一个** `flasher_bound` —— 两处不一致就会出现
        “卡片说有一份可用、组合却说零匹配”这种自相矛盾的屏幕。
        """
        return self.web_catalog.match_firmware(
            card, flasher_bound=self._flasher_bound_for_port(card.port)
        )

    def _observe_combination(self, requested: dict) -> tuple[Any, Any]:
        """页面点名的设备卡片 ⇒ （卡片， 身份匹配结论）。

        卡片本身按既有那一套复核（`sniff_service.requested_card`：序号真在本次结果里、
        口对得上、口可用）；**固件不由页面给**，而是由这张卡片的实测身份唯一匹配。
        """
        executor = getattr(self, "sniff", None)
        result = getattr(executor, "last_result", None) if executor is not None else None
        card, refusal = sniff_service.requested_card(result, requested)
        if refusal:
            raise RequestError(refusal, status=409)
        if card is None:
            raise RequestError("没有点名设备：实时观察需要一张本次嗅探里的设备卡片", status=400)
        return card, self._match_for_card(card)

    def _assembly_for(self, firmware_id: str):
        """这一份 run 属于哪条链（没有装配就返回 None —— 证据里少几个字段，不是报错）。"""
        assembly = getattr(self, "assembly", None)
        if assembly is None:
            return None
        return assembly.by_firmware(str(firmware_id))

    def _flasher_bound_for_port(self, port: str) -> bool:
        """这个口上有没有**点名**的烧录器绑定（ / D3）。

        ⚠️ 绑定是按**台架里的设备键**写的，而嗅探只看到"口"；所以这里按
        `devices.<键>.port` 对上找。找不到就如实 False —— "没说"不等于"不需要"，
        要求绑定的目标会因此被拒（而不是被默认放行）。
        """
        wanted = str(port or "").strip()
        if not wanted:
            return False
        bench = bench_config.active()
        seen: set[str] = set()
        for row in getattr(self.assembly, "assemblies", ()) or ():
            for spec in row.pipeline.devices.specs():
                key = str(spec.key)
                if key in seen or str(spec.port or "") != wanted:
                    continue
                seen.add(key)
                if bench.device_binding(key).flasher_bound:
                    return True
        return False

    # ============================  / L4：受控测试的服务端入口
    def _live_test_context(self, firmware_id: str = "") -> dict:
        """跑受控测试要的几样东西（会话 + 项目包 + 协议 + 事实），缺什么就说什么。

        ⚠️ 事实全部来自**这条会话**（它的设备键、身份自报、能力声明），而不是
        页面交上来的选择。页面交的只有"选了哪些用例、参数是什么"。
        """
        center = getattr(self, "observe", None)
        if center is None:
            return {"reason": "这台服务没有实时观察这条链"}
        try:
            selected = self._observe_selected(center)
        except LiveTestError as error:
            return {"reason": str(error)}
        chosen = str(firmware_id or selected or self._live_test_default_firmware(center))
        session = center.session(chosen) if hasattr(center, "session") else center
        snapshot = session.snapshot()
        if str(snapshot.get("state") or "") != OBSERVE_CONNECTED:
            return {"reason": "实时观察还没连接设备：连接之后才能跑受控测试"}
        if getattr(center, "transparent", False):
            # 用户口径（2026-10-13）：身份未识别 ⇒ 这一条会话按**原始串口模式**工作，
            # 用例的能力要求要对着“具体一份固件 + 项目包”判，而这里没有它
            # ⇒ 受控用例在屏幕上显示「不适用」（`applicable=False`），理由进悬停。
            # ⚠️ 这个标志住在**中心**上（`ObserveCenter.transparent`），会话快照里没有它。
            return {"firmware_id": "", "applicable": False,
                    "reason": TRANSPARENT_LIVE_TEST_REASON}
        row = self._assembly_for(chosen)
        if row is None:
            return {"firmware_id": chosen, "reason": f"没找到 {chosen or '当前'} 这一路的装配"}
        package = getattr(row, "project", None)
        protocol = getattr(package, "test_protocol", None)
        if package is None:
            return {"firmware_id": chosen,
                    "reason": "这一路没有项目包：没有用例定义，也就没有受控测试"}
        if protocol is None:
            return {"firmware_id": chosen,
                    "reason": "这一路的项目包没有声明受控测试协议（`test_protocol`）"}
        return {
            "firmware_id": chosen,
            "session": session,
            "snapshot": snapshot,
            "package": package,
            "protocol": protocol,
            "facts": self._live_test_facts(chosen, row, snapshot),
        }

    def _observe_selected(self, center: Any) -> str:
        """实时观察当前选中的那一份固件（`ObserveCenter.selected`）。

        ⚠️ 真身是 **`@property`**（返回一个字符串），**不是方法**。这里只接属性的写法，
        不再"能调就调一下" —— 那层容忍让夹具比现实更宽，于是真机上跑起来才报
        `TypeError: 'str' object is not callable`（ / ，夹具当场改成属性）。
        """
        selected = getattr(center, "selected", "")
        if callable(selected):
            raise LiveTestError(
                "观察中心的 `selected` 应当是属性（返回当前选中的固件），不是方法"
            )
        return str(selected or "")

    def _live_test_default_firmware(self, center: Any) -> str:
        """没点名、也没有"当前选中"时：装配里**恰好一条**就用它；多条就不猜。

        ⚠️ 真实的观察中心永远有 `selected`（属性）；这个回退只给"单固件部署 / 宿主夹具"用。
        好几条的时候说出来（不是默认选第一条 —— 那会把 A 的目录当成 B 的）。
        """
        rows = list(getattr(getattr(self, "assembly", None), "assemblies", ()) or ())
        if len(rows) == 1:
            return str(getattr(rows[0], "firmware_id", "") or "")
        return ""

    def _live_test_facts(self, firmware_id: str, row: Any, snapshot: dict) -> Any:
        """会话事实 ⇒ `SessionFacts`（能力自报与身份一起进摘要）。"""
        device = snapshot.get("device") or {}
        session = snapshot.get("session") or {}
        fields = dict((snapshot.get("identity") or {}).get("fields") or {})
        protocols = fields.get("protocols")
        return SessionFacts(
            session_id=str(session.get("session_id") or ""),
            device_key=str(device.get("key") or ""),
            firmware_id=str(firmware_id),
            project_package_id=str(getattr(row, "project_key", "") or ""),
            identity=fields,
            # ⚠️ 没拿到自报就给空映射（模型那边会如实说"没得核对"，不会当成支持）。
            protocols=dict(protocols) if isinstance(protocols, dict) else {},
        )

    def _live_test_capabilities(self, firmware_id: str) -> Any:
        """这一路**目标**的能力声明（用例的 `cap.*` 要求问它）。"""
        row = self._assembly_for(firmware_id)
        profile = getattr(row, "profile", None)
        return getattr(profile, "capabilities", None)

    def _live_test_catalog_block(self) -> dict:
        """`/api/state.observe.live_test_catalog`：这条会话上的用例目录（只读、纯计算）。

        ⚠️ 三种"没有"要分开说：没连设备 / 项目包没协议 / 一条用例都没登记。
        页面要的就是这三句话中的一句（需求 §8 第一条：不可用时给出原因）。
        """
        context = self._live_test_context()
        if context.get("reason"):
            # ⚠️ 连不上/没声明协议时也要**点名这一路是哪一份固件**（能点就点）：
            # D9 要的是"目录与 `observe.firmware_id` 同源"，只剩一句原因会让人
            # 看不出这个目录属于谁（真机上 ESP32 那份就落在这一支）。
            # ⚠️ `applicable=False`：这个间题在**这一条会话上不成立**（身份未识别、
            # 按原始串口工作）—— 页面屏幕上只说「不适用」，完整那句话挂悬停。
            return {"firmware_id": str(context.get("firmware_id") or ""),
                    "available": False,
                    "applicable": bool(context.get("applicable", True)),
                    "reason": context["reason"], "cases": []}
        firmware_id = str(context["firmware_id"])
        capabilities = self._live_test_capabilities(firmware_id)
        if capabilities is None:
            return {"firmware_id": firmware_id, "available": False,
                    "reason": "这一路的目标没有能力声明：判不了用例的能力要求", "cases": []}
        try:
            verdicts = self.web_catalog.live_test_catalog(firmware_id, context["facts"])
        except (web_catalog.CatalogError, LiveTestError) as error:
            return {"firmware_id": firmware_id, "available": False,
                    "reason": str(error), "cases": []}
        rows = [row.to_json() for row in verdicts]
        runnable = [row for row in rows if row["selectable"]]
        return {
            "firmware_id": firmware_id,
            "available": bool(runnable),
            "reason": "" if runnable else (
                "这一路的项目包还没有登记可执行的受控用例" if not rows
                else "这一路的用例现在都不可执行（逐条的理由在 cases 里）"
            ),
            "cases": rows,
        }

    def _start_live_tests(self, body: dict) -> dict:
        """启动一批受控测试：先凑事实、再**逐条再校验一次**、最后才写命令。"""
        context = self._live_test_context(str(body.get("firmware_id") or ""))
        if context.get("reason"):
            raise RequestError(str(context["reason"]), status=409)
        facts = context["facts"]
        session = context["session"]
        capabilities = self._live_test_capabilities(context["firmware_id"])
        if capabilities is None:
            raise RequestError("这一路的目标没有能力声明：判不了用例的能力要求", status=409)
        package = context["package"]
        submitted = body.get("cases")
        if not isinstance(submitted, list) or not submitted:
            raise RequestError("没有选择任何用例", status=400)
        cases = {case.identity: case for case in package.test_catalog.cases}
        plans: list[Any] = []
        parameters_by_execution: dict[str, dict] = {}
        for index, item in enumerate(submitted, start=1):
            if not isinstance(item, dict):
                raise RequestError("cases 中每一项必须是对象", status=400)
            try:
                identity = (str(item.get("case_id", "")), int(item.get("version", 0)))
            except (TypeError, ValueError):
                raise RequestError("用例 id / 版本格式错误", status=400) from None
            case = cases.get(identity)
            parameters = item.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise RequestError("parameters 必须是对象", status=400)
            execution_id = f"{context['firmware_id']}#{index}"
            binding = (
                binding_for(facts, case, parameters) if case is not None else None
            )
            if binding is None:
                raise RequestError(
                    f"用例 {identity[0]}@{identity[1]} 不在这一路的项目包里", status=409
                )
            verdict = verify_start(
                binding, facts=facts, case=case, capabilities=capabilities,
                parameters=parameters, active_batch=self._live_test_active_batch(session),
            )
            if not verdict.allowed:
                raise RequestError(
                    f"拒绝开始 {identity[0]}@{identity[1]}（{verdict.code}）：{verdict.reason}",
                    status=409,
                )
            plans.append(BatchPlan(execution_id=execution_id, case=case,
                                   parameters=parameters,
                                   steps=self._live_test_steps(context["protocol"], case)))
            parameters_by_execution[execution_id] = dict(parameters)
        try:
            started = session.start_live_tests(
                plans=plans, protocol=context["protocol"],
            )
        except (LiveTestBatchError, LiveTestError, ObserveError) as error:
            raise RequestError(str(error), status=409) from None
        return {"started": started, "observe": session.snapshot()}

    def _live_test_steps(self, protocol: Any, case: Any) -> tuple:
        """这条用例声明的阶段（由**项目包**回答；没声明就不上阶段卡）。

        ⚠️ 阶段为什么不住在 `TestCase` 里：`embedded_test_model` 与 `step_lifecycle`
        互相 import（后者要 `RESULT_STATUSES`），把 `TestStep` 塞进用例定义就是循环依赖。
        阶段是"这台设备的动作怎么分"的知识 ⇒ 与协议同住项目包，走 `steps_for()`。
        """
        reader = getattr(protocol, "steps_for", None)
        if not callable(reader):
            return ()
        return tuple(reader(case))

    def _cancel_live_tests(self, body: dict) -> dict:
        """取消正在跑的那一批（没跑过就 409，不静默成功）。"""
        context = self._live_test_context(str(body.get("firmware_id") or ""))
        if context.get("reason"):
            raise RequestError(str(context["reason"]), status=409)
        session = context["session"]
        try:
            snapshot = session.cancel_live_tests(
                reason=str(body.get("reason") or "使用者在页面上取消了这一批")
            )
        except ObserveError as error:
            raise RequestError(str(error), status=getattr(error, "status", 409)) from None
        return {"cancelled": snapshot, "observe": session.snapshot()}

    def _live_test_active_batch(self, session: Any) -> str:
        """这条会话上**正在跑**的那一批（没有就空串）—— 启动闸门用它。

        ⚠️ 只认 `RUNNING`：已经收口的那一批不是"在跑"，不能因此把新的一批拒掉。
        """
        reader = getattr(session, "live_tests_snapshot", None)
        if not callable(reader):
            return ""
        snapshot = reader() or {}
        batch = snapshot.get("batch") or {}
        if str(batch.get("status") or "") != "RUNNING":
            return ""
        return str(batch.get("batch_id") or "")

    def _observe_device_key(self, requested: dict, firmware_id: str = "") -> str:
        """页面点名的**设备卡片** ⇒ 实时观察要连的那一条**设备登记**的键。

        卡片只带来"本次嗅探看到的序号 + 通信口"；而连接要用的是登记键（波特率、行结束符、
        字典、能力声明都挂在它上面）。**键由服务端定**，页面说了不算：

        - 先按 run 路径那同一套复核（`sniff_service.requested_card`）：这一号真在本次结果里、
          口对得上、这个口可用 —— 页面拿一个自己编的序号来是过不去的；
        - 再在这个口上找"它是哪一条设备登记"：
          - **给了 `firmware_id`**（身份唯一匹配那一条路）：只在那一条链的登记里找，
            恰好一条才算；
          - **没给**（身份未知 / 零匹配 / 多匹配 ⇒ 原始串口模式，用户口径 2026-10-13）：
            逐条链都查一遍。同一个物理设备在不同的链里用**同一个键**（同一块板的两份构建
            都登记成 `a`），所以"同一个键挂在多条链上"算唯一；出现**两个不同的键**
            才是真的说不清。
          - 一条都没有 / 有多个不同的键都**如实拒绝**并把登记摆出来
            —— 平台不按枚举顺序、也不按"唯一在线"去猜（需求 §6）。

        ⚠️ 2026-10-10 真机自测抓到的顺序错：这里从前只在"当前选中那一份"的登记里查，
        而 `firmware_id` 要等 `connect()` 里那一次 `select()` 才生效 —— 于是拿另一块板的卡片
        去连就会回一句莫名其妙的拒绝（实测：“COM<n> 不在这一路的设备登记里：可用的是 COM<n>(a)、COM<n>(b)”，
        而 COM<n> 明明登记在 RT-Thread 那一路里）。现在"哪一路"只认**请求里那一个 `firmware_id`**，
        不再依赖服务端此刻选着哪一份。
        """
        executor = getattr(self, "sniff", None)
        result = getattr(executor, "last_result", None) if executor is not None else None
        card, refusal = sniff_service.requested_card(result, requested)
        if refusal:
            raise RequestError(refusal, status=409)
        if card is None:
            raise RequestError("没有点名设备：实时观察需要一张本次嗅探里的设备卡片", status=400)
        want = str(card.port or "").strip()
        if not str(firmware_id or "").strip():
            # 身份未知 / 零匹配 / 多匹配（原始串口模式）：只按**口**找"这个口是谁的"。
            # 逐条链都查一遍 —— 同一个键挂在多条链上是正常的（同一台设备的两份构建），
            # 出现两个**不同的键**才是真的说不清。
            return self._device_key_for_port(want)
        session = self.observe.session(str(firmware_id or "") or None)
        rows = [
            spec for spec in session.registry.specs()
            if str(spec.port or "").strip() == want
        ]
        if len(rows) == 1:
            return str(rows[0].key)
        if not rows:
            # ⚠️ 浏览器 E2E 实测抓到的：**身份唯一匹配到这一条链，而这个口
            #    不是本机台架点名的** —— 它是系统枚举到的通用口。这种情况必须能连：
            #    身份匹配说明“这块板就是这一条链的项目”，口只是没写进台架而已
            #    （需求 §6.11/§6.12 说的阶梯：先当普通串口用，适配之后自然走到这里）。
            #    从前这里直接 409（“COM<n> 不在这一路的设备登记里”），于是
            #    “身份问到了反而连不上” —— 问得越多、反而不如问不到的怪事。
            return self._register_generic_device(want, firmware_id=firmware_id)
        keys = "、".join(f"{spec.key}" for spec in rows)
        raise RequestError(
            f"{want} 在本机台架里对应不止一条登记（{keys}）：先把它改成唯一一条再说",
            status=409,
        )

    def _device_key_for_port(self, port: str) -> str:
        """这个通信口是哪一条**设备登记**的（身份未知时用它承载连接）。

        用户口径（2026-10-13）：身份不是连接的前置条件 ⇒ 零匹配 / 多匹配 / 问不到身份时，
        键只能按**口**来定。逐条观察链的登记都查一遍：同一个物理设备在不同的链里用同一个键
        （同一块板的两份构建都登记成 `a`），所以"同一个键挂在多条链上"算唯一；
        出现**两个不同的键**才是真的说不清（那说明台架把两个设备写到了同一个口上）。

        ⚠️ 这里**不猜**：答不出来就如实拒绝并把登记摆出来（与 `_observe_device_key` 同一口径）。
        """
        want = str(port or "").strip()
        hosts: dict[str, list[str]] = {}
        for chain in self.observe.firmware_ids:
            for spec in self.observe.session(chain).registry.specs():
                if str(spec.port or "").strip() == want:
                    hosts.setdefault(str(spec.key), []).append(str(chain))
        available = "、".join(
            f"{spec.port}({spec.key})"
            for chain in self.observe.firmware_ids
            for spec in self.observe.session(chain).registry.specs()
        ) or "（无）"
        if len(hosts) == 1:
            return next(iter(hosts))
        if not hosts:
            # 需求 §6.11/§6.12：**系统枚举到、台架没点名**的口 = 「通用串口」，
            # 不是“拒绝连接”。给它登记一条通用设备（没有别名、没有能力声明，
            # 但走**同一套**连接状态机、串口 owner 与收发/证据路径）。
            return self._register_generic_device(want)
        keys = "、".join(sorted(hosts))
        raise RequestError(
            f"{want} 在本机台架里挂着不止一个设备键（{keys}）：先把它改成唯一一条再说",
            status=409,
        )

    def _register_generic_device(self, port: str, firmware_id: str = "") -> str:
        """把一个**没有登记过**的口登记成通用设备，返回它的键（幂等）。

        需求 §6.11/§6.12：没有 Adapter、没有 `bench.local.json` 时，系统枚举到的串口
        也要能选、能连、能收发、能断开重连；身份查询失败只是“保持通用串口模式”，
        **不是**连接的前置条件。

        ⚠️ 只登记在**一条观察链**上（通用串口没有目标、没有能力声明）：给了 `firmware_id`
        就登记在那一条（身份唯一匹配到它、或者调用方已选定它），否则登记在当前选中的那一条。
        登记进哪一条，就用那一条的会话与 ownership。
        """
        import bench_target  # noqa: PLC0415  (core：通用设备的形状)

        wanted = str(firmware_id or "").strip()
        session = self.observe.session(wanted or (self.observe.selected or None))
        spec = bench_target.generic_spec_for_port(port)
        if session.registry.get(spec.key) is None:
            session.registry.register(spec)
        return spec.key

    def _run_target(self, selection, requested):
        """页面点名的**设备卡片** ⇒ 这一趟的 `RunTarget`（服务端复核，页面说了不算）。

        `requested` 的形状是 `{"scan_seq": 3, "port": "COM<n>"}`。**没给就返回 `None`**：
        那表示"按老路径跑"（静态登记里那块板配的口），CLI 与老调用方一点不受影响。

        四条复核（缺一条都不放行）：这次嗅探结果里**真有**这一号；端口对得上；
        这个口**可用**；这份固件与这个设备**兼容**。前三条保证"选的是这一趟真看到的东西"，
        第四条保证"选的固件真能用在这个设备上"。
        """
        if not isinstance(requested, dict) or not requested:
            return None
        executor = getattr(self, "sniff", None)
        result = getattr(executor, "last_result", None) if executor is not None else None
        # “页面点的算不算数”只有一处判断（core 的三态返回）：
        # `(None, "")` = 没点名（走老路径）；`(None, 理由)` = 点名了但站不住；`(卡片, "")` = 通过。
        card, refusal = sniff_service.requested_card(result, requested)
        if refusal:
            raise RequestError(refusal, status=409)
        if card is None:
            return None
        requirement = self.web_catalog.requirement_for(selection.firmware_id)
        #  / D3：这一趟的**台架绑定**（通信口 / 目标 / 烧录通路）。
        # ⚠️ “烧录通路没点名”不是错误，而是**不允许烧**的理由：要求绑定的目标会被
        # `compatibility()` 直接拒掉，且理由里说清缺的是哪一格（不按枚举顺序猜）。
        binding = bench_config.active().device_binding(str(selection.device_key))
        verdict = device_sniff.compatibility(
            card, requirement, flasher_bound=binding.flasher_bound
        )
        if not verdict.allowed:
            raise RequestError(
                f"{requirement.label} 不能用在 {card.port} 这个设备上：{verdict.reason}",
                status=409,
            )
        observation = card.observation
        probe_block: dict = {"probe_id": observation.probe_id}
        for declaration in getattr(executor, "probes", ()) or ():
            if declaration.probe_id == observation.probe_id:
                probe_block = declaration.to_json()
                break
        # 实际写出去 / 读回来的字节是**证据**，与声明放在一起。
        probe_block["tx_hex"] = observation.tx_hex
        probe_block["rx_hex"] = observation.rx_hex
        profile = getattr(self._assembly_for(selection.firmware_id), "profile", None)
        return device_sniff.RunTarget(
            port=card.port,
            target_id=str(selection.target_id),
            firmware_id=str(selection.firmware_id),
            device_key=str(selection.device_key),
            scan_seq=int(card.num),
            identity_state=str(card.identity_state),
            identity_before=dict(observation.identity_fields or {}),
            # 证据里要能回答“这一趟用的是哪份台架事实”：既留原有的自述块，也留这次
            # 真正用到的三个格子（各带来源）。
            bench_binding={
                "device": binding.to_json(),
                "block": bench_config.block(owners=(str(selection.target_id),)),
            },
            probe=probe_block,
            adapter_id=str(getattr(profile, "flash_adapter", "") or ""),
            flasher_id=str(binding.flasher_id.value or ""),
            requires_flash=bool(requirement.requires_flash),
            first_flash=bool(verdict.first_flash),
            # ：这一烧会覆盖同板另一份固件 —— 事实要进证据。
            switches_sibling=bool(verdict.switches_sibling),
            #  / D4：把"这份固件声明自己有哪几种协议"一起带进证据，
            # 烧录后复核才有一条**声明过的**标尺可比（拿不到就空着，不猜）。
            required_protocols=tuple(requirement.required_protocols),
        )

    def platform_facts(self) -> dict:
        """这台服务自述的那一段（R2d）：`/api/state` 直接把它放进去。

        没显式给时退到"从会话的插件束现算"—— 两份事实仍然出自同一个
        `platform_info.describe()`，不会出现页面说 A、诊断包说 B。
        """
        if self.platform:
            return dict(self.platform)
        return platform_info.describe(plugins=getattr(self.observe, "plugins", None))

    @property
    def imports(self) -> bundle_stream_import.ImportCenter:
        """导入任务登记处（ / P3）。

        ⚠️ 它按需建：没有观察会话的装配（`observe=None`，那些只跑搜索的宿主测试就是这样装的）
        没有导入目录，也就没有导入这条路 —— 与其在构造时去碰 `None.imports_dir`，
        不如让它到真的要用时才要求 observe（ 就是这么踩出来的）。
        """
        if self._imports is None:
            if self.observe is None:
                raise RuntimeError("这台服务没有观察会话，没有导入目录")
            self._imports = bundle_stream_import.ImportCenter(
                self.observe.imports_dir,
                free_space=lambda path: shutil.disk_usage(path).free,
            )
        return self._imports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument(
        "--demo", action="store_true",
        help="用仓库自带的演示数据启动（无硬件）：证据与会话目录指向 demo/",
    )
    parser.add_argument(
        "--open", action="store_true", help="启动后自动打开浏览器（可选）"
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="让已经在跑的那个实例优雅退出（会释放工作区锁）；不开服务、不抢锁",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="配合 --stop：即使正握着串口/正跑着 run 也停（会把正在进行的采集或验证拦腰砍断）",
    )
    parser.add_argument(
        "--bench",
        default="",
        help="台架文件（；默认按 --bench > PW_BENCH_FILE > bench.local.json 找）",
    )
    return parser.parse_args()


def request_stop(host: str, port: int, *, force: bool) -> int:
    """叫正在跑的那个实例优雅退出（）。

    只发一个请求：已经在跑的那个自己知道它有没有握着串口（由它的 `/api/stop` 判）。
    ⚠️ 必须**绕开本机代理**：这台机器上 `HTTP_PROXY` 指向 7890，走代理去访 127.0.0.1
    会拿到 502（本仓库里踩过）。
    """
    body = json.dumps({"force": force, "reason": "命令行 --stop"}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{port}/api/stop",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        print(f"REFUSED: {detail}", file=sys.stderr)
        return 4
    except (urllib.error.URLError, OSError) as error:
        print(
            f"没有在 {host}:{port} 上找到在跑的调试台（{error}）—— 都已经停了？",
            file=sys.stderr,
        )
        return 5
    print(f"已请求停服：{payload}")
    return 0


def build_doctor_payload(
    *,
    host: str,
    port: int,
    hardware: bool = False,
    assembly: object | None = None,
) -> dict:
    """跑一次**只读**体检并把 CLI 那份报告打成 JSON（ / D5）。

    ⚠️ 页面**不自行推导结果**：它拿到的就是 `doctor.compose_report()` 算出来的
    同一个 `Report`（与终端跑 `doctor.py` 是同一个模型）。
    ⚠️ 默认 **不** 做硬件探测（`hardware=False`）：枚举串口是独立动作，
    只由页面上的明确按钮触发；这里不打开串口、不发送字节、不复位。
    """
    report = doctor.compose_report(
        host=host,
        port=int(port),
        hardware=bool(hardware),
        probe=doctor.probe_for_assembly(assembly),
    )
    return report.to_json()


def expected_identity_facts(*, firmware_id: str, project: object, plugins: object) -> dict:
    """ / L8：当前运行组合的**声明**（宿主用例的判据拿它对表）。

    只放**声明过**的事实：项目包键、设备侧可能自称的名字（profile 的 `identity_hint`，
    经插件束带过来）、这一路的 token domain。设备自报的字段**不进这里** —— 那是被核对的
    一方，不是核对依据（把两边混起来，"核过"就成了一句空话）。

    ⚠️ 与烧录后核验（`device_sniff.identity_after_flash`）同一套语义：设备自称的名字
    **可以是包键、也可以是 profile 声明的设备侧名字**，两者都算对上。
    """
    project_key = str(getattr(project, "key", "") or "")
    aliases = tuple(str(name) for name in (getattr(plugins, "device_projects", ()) or ()))
    names = tuple(dict.fromkeys((project_key, *aliases))) if project_key else aliases
    return {
        "firmware_id": str(firmware_id or ""),
        "project_package_id": project_key,
        "project_names": names,
        "token_domain": str(getattr(plugins, "token_domain", "") or ""),
    }


def _identity_probe_for(assembly: object):
    """一条链声明的**只读身份查询** ⇒ `IdentityProbe`（没声明 ⇒ `None`：**不问**）。

    三样东西各来自那里：查询文本来自**项目包**（`identity_query`，没声明就用平台协议的
    缺省查询），回答前缀与“不认识”的说法来自**插件束**（设备/项目的事实），行结束符与
    波特率来自**目标描述**。
    ⚠️ 缺前缀就不发字节 —— “不猜”在这里就是“没有声明就没有动作”。
    """
    project = getattr(assembly, "project", None)
    plugins = getattr(assembly, "plugins", None)
    profile = getattr(assembly, "profile", None)
    command = str(getattr(project, "identity_query", "") or "").strip()
    prefix = str(getattr(plugins, "identity_reply_prefix", "") or "")
    if not prefix:
        return None
    if not command:
        # 平台协议自己定死了查询文本（`identity` 里的那一份），项目包可以覆盖。
        command = identity.IDENTITY_QUERY_TEXT
    transport = getattr(profile, "transport", None)
    return device_sniff.IdentityProbe(
        probe_id=f"{getattr(assembly, 'firmware_id', '')}.identity",
        command=command,
        line_ending=str(getattr(transport, "line_ending", "") or "crlf"),
        baud=int(getattr(transport, "baud", 115200) or 115200),
        wait_s=max(0.5, float(getattr(project, "identity_query_delay_s", 0.0) or 0.0)),
        prefix=prefix,
        unsupported_markers=tuple(
            getattr(plugins, "identity_unsupported_markers", ()) or ()
        ),
        # 项目自带解析器的**名字**（实现由 `_sniff_parsers()` 注进执行器）：
        # RT-Thread 那一路的回答是 `key=value` 明文，不是 JSON。
        parser_id=(
            f"{getattr(assembly, 'firmware_id', '')}.identity"
            if getattr(plugins, "identity_parser", None) is not None
            else ""
        ),
    )


def _sniff_parsers(assemblies) -> dict:
    """项目自带的身份解析器（按 `parser_id` 交给执行器）。
    ⚠️ 这是唯一一处把“声明里的名字”接到“项目层的实现”上的地方；core 两边都不认识。
    """
    rows: dict = {}
    for row in assemblies:
        parser = getattr(row.plugins, "identity_parser", None)
        if parser is None:
            continue
        rows[f"{row.firmware_id}.identity"] = parser
    return rows


def _sniff_probes(assemblies) -> tuple:
    """所有链声明的探针，**按声明去重**（同一份源码的两条链不该问两遍）。"""
    rows = []
    seen = set()
    for row in assemblies:
        probe = _identity_probe_for(row)
        if probe is None:
            continue
        key = (probe.command, probe.line_ending, probe.baud, probe.wait_s, probe.prefix,
               probe.parser_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(probe)
    return tuple(rows)


def _sniff_access(
    assemblies, transport: object | None = None
) -> Callable[[str], str]:
    """占用探测：**先问平台自己的传输实现**，其次才退回某一条装配的插件。

    ⚠️ 通用口（系统枚举到、台架没点名）**不属于任何一条装配**。
    以前这里只从装配里找实现 ⇒ “通用口没有传输实现”这件事在结构上就是可能的；
    传输实现是**平台级**的东西（所有链共用同一份），从它拿才对。装配插件作为后备，
    是为了“调用方自己拼了一整套插件、没交 transport”那种用法。
    """
    probe = getattr(transport, "port_probe", None)
    if callable(probe):
        return probe
    for row in assemblies:
        probe = getattr(row.plugins, "port_probe", None)
        if probe is not None:
            return probe
    return lambda port: ACCESS_UNKNOWN


def _sniff_exchange(
    assemblies, transport: object | None = None
) -> Callable[[str, int, bytes, int, float], bytes]:
    """受控的一问一答实现：**先问平台自己的传输实现**，其次才退回装配插件。

    ⚠️ 同上：嗅探问的是一个**口**，不是“某条装配上的口”。所以这一路
    不能依赖“在装配清单里找到这个口的传输实现”—— 找不到时必须还有平台那一份。
    """
    exchange = getattr(transport, "exchange", None)
    if callable(exchange):
        return exchange
    for row in assemblies:
        exchange = getattr(row.plugins, "sniff_exchange", None)
        if exchange is not None:
            return exchange

    def _unavailable(port, baud, payload, max_bytes, wait_s):
        raise sniff_service.ExchangeError(
            "这套装配没有提供受控传输实现（目标层没交 `sniff_exchange`）"
        )

    return _unavailable


def _port_conflict(holder: dict) -> Callable[[str], str]:
    """资源闸门：实时观察 / 正在跑的 run 握着的口，嗅探**只读它、不抢**。

    `holder` 是延迟绑定（先造执行器、后装配观察中心）—— 两边都就绪后才生效。
    """

    def probe(port: str) -> str:
        center = holder.get("observe")
        if center is None:
            return ""
        return str(center.port_conflict(port) or "")

    return probe


def _held_observation(holder: dict) -> Callable[[str], device_sniff.PortObservation | None]:
    """Reuse an active live session instead of reopening a port already owned by this platform."""
    def resolve(port: str) -> device_sniff.PortObservation | None:
        center = holder.get("observe")
        snapshot = center.held_snapshot(port) if center is not None else None
        if not snapshot:
            return None
        identity_block = snapshot.get("identity") or {}
        fields = dict(identity_block.get("fields") or {})
        confirmed = str(identity_block.get("state") or "") == "ok" and bool(fields)
        return device_sniff.PortObservation(
            port=str(port),
            outcome=(device_sniff.PROBE_OK if confirmed else device_sniff.PROBE_NO_RESPONSE),
            access=ACCESS_FREE,
            identity_fields=fields,
            reason=(
                "平台已连接该通信口：复用当前实时观察会话身份，本轮未重复打开"
                if confirmed else
                "平台已连接该通信口：本轮未重复打开；当前会话尚未取得设备身份"
            ),
            started_utc=str(snapshot.get("connected_utc") or ""),
            ended_utc=utc_now(),
        )
    return resolve


def _port_activity_source(store, sessions_dir: Path | None, imports_dir: Path | None):
    """构造"这个口最近一次活动"的取数函数（卡片上那句 `最近活动：…`）。

    来源**只能是记录**：run 的 `manifest.json`（`run.created_utc` + 写下的通信口）与
    观察会话的 `manifest.json`（`opened_utc` + `port` + `totals`）。
    ⚠️ 不按目录名推时间、不按"看起来最近"猜：记里没写时间戳的那一条**跳过**。
    ⚠️ 取数只在**一次嗅探**里算一次（缓存一份），三个口不会把同一批记录读三遍。

    需求 §3：卡片要显示"端口最近活动"。2026-10-09 实测发现这一格**恒为空** ——
    一个永远为 null 的字段比没有字段更糟。
    """
    cache: dict[str, object] = {}

    def records() -> list[dict[str, Any]]:
        if "rows" in cache:
            return cache["rows"]  # type: ignore[return-value]
        rows: list[dict[str, Any]] = []
        if store is not None:
            try:
                for row in store.summaries(limit=40):
                    device = row.get("device") or {}
                    port = str(row.get("port") or device.get("port") or "")
                    result = str(row.get("result") or "未完成")
                    rows.append({
                        "port": port,
                        "utc": str(row.get("started_utc") or ""),
                        "note": f"run {row.get('run_id')}（{row.get('suite')} · {result}）",
                    })
            except (OSError, ValueError):
                pass
        if sessions_dir is not None:
            try:
                for row in session_review.list_all_sessions(sessions_dir, imports_dir):
                    totals = row.get("totals") or {}
                    rows.append({
                        "port": str(row.get("port") or ""),
                        "utc": str(row.get("opened_utc") or ""),
                        "note": (
                            f"实时观察 {row.get('session_id')}"
                            f"（收到 {int(totals.get('rx_bytes') or 0)} B）"
                        ),
                    })
            except (OSError, ValueError):
                pass
        cache["rows"] = rows
        return rows

    def activity(port: str) -> tuple[str | None, str]:
        return device_sniff.recent_activity(records(), port)

    return activity


#: 演示装配用的项目包键（目标行与项目包必须说同一个名字，否则冻结判据会红）。
DEMO_PROJECT_KEY = "demo"


#: `--demo`：把证据目录与会话目录指向仓库自带的**演示数据**（无硬件演示入口）。
DEMO_MODE = False


def use_demo_data(enabled: bool) -> None:
    """切换演示数据目录（`main()` 在带 `--demo` 时调用一次）。"""
    global DEMO_MODE
    DEMO_MODE = bool(enabled)


def build_demo_assembly(runs_dir: Path | None = None, *, bus: EventBus | None = None):
    """**演示装配**：只有一条演示目标（core 层假目标），不接硬件、不读本机配置。

    公开首笔不含具体平台 Adapter（需求 §3/§5）：页面上的自动验证 / 实时观察 /
    离线诊断三种模式、统一日志工作台与演示回放由**演示目标 + 演示数据**驱动；
    要接自己的板子，请按 `docs/adapter-guide.md` 登记一个真实目标。

    ⚠️ 这个函数只**读**：不构建、不烧录、不打开任何真实串口（演示目标的 transport
    是内存替身，端口名是 `FAKE0`）。
    """
    import dataclasses
    import device_registry
    import fake_target
    import platform_plugins
    import project_package
    import runner_registry
    import target_adapters
    import web_assembly

    class _DemoDiscovery:
        """演示目标**唯一**的受信适配器：设备发现。

        假目标没有构建/烧录/复位/符号化链（能力表里如实写着 `unsupported`），
        它真正能给 core 的只有"这台机器上有哪一台（替身）设备"。
        """

        adapter_id = "demo.discovery"

        def discovery(self, project_dir, pio_env):
            return target_adapters.Discovery(adapter_id=self.adapter_id)

    repository = TOOLS_DIR.parent
    shared_bus = bus or EventBus()
    default_runs = "demo/runs" if DEMO_MODE else "runs"
    default_sessions = "demo/observe-sessions" if DEMO_MODE else "observe-sessions"
    sessions_dir = repository / default_sessions
    evidence_dir = (Path(runs_dir) if runs_dir is not None
                    else repository / default_runs)

    profile = dataclasses.replace(
        fake_target.profile(), project=DEMO_PROJECT_KEY, suites=("demo",)
    )
    project = project_package.ProjectPackage(
        key=DEMO_PROJECT_KEY,
        token_domain=fake_target.FAKE_DOMAIN,
        suites=("demo",),
        default_observe_seconds=2.0,
    )
    # 演示会话：真实的 `ObserveSession`，只有"怎么打开口 / 探测结果"是替身。
    def _port(name: str, baud: int):
        return fake_target.FakePort(name, baud)

    def _probe(_port_name: str) -> str:
        return fake_target.FAKE_ACCESS_FREE

    plugins = dataclasses.replace(
        fake_target.fake_plugins(), adapter_ids=(_DemoDiscovery.adapter_id,)
    )
    # 设备登记用**产品级**的 core 登记表（`FakeRegistry` 是测试专用的最小替身，
    # 没有 `snapshot()`，页面读状态时会报 AttributeError）。
    registry = device_registry.DeviceRegistry(
        (
            device_registry.DeviceSpec(
                key=fake_target.FAKE_DEVICE_KEY,
                label="演示设备",
                port=fake_target.FAKE_PORT,
                role="demo",
                pipeline_allowed=False,
                boundary="演示目标：不接真实硬件",
                capabilities=fake_target.FAKE_CAPABILITIES,
            ),
        ),
        probe=_probe,
    )
    pipeline = Pipeline(
        project_dir=repository,
        runs_dir=evidence_dir,
        # 演示目标没有构建链：显式给出环境名，编排就不会为了"默认环境"去要 build 适配器
        # （`Pipeline.pio_env` 有值时不再向适配器提问 —— 只读快照因此也能成立）。
        pio_env="demo",
        plugins=plugins,
        project=project,
        devices=registry,
        capabilities=fake_target.FAKE_CAPABILITIES,
        event_bus=shared_bus,
        adapters={"discovery": _DemoDiscovery()},
    )
    firmware_id = f"{profile.target_id}/default"
    runners = runner_registry.RunnerRegistry(
        (runner_registry.Runner(firmware_id, pipeline),), primary=firmware_id
    )
    session = ObserveSession(
        registry=pipeline.devices,
        plugins=pipeline.plugins,
        active_run=runners.active_run_port,
        emit=runners.emit,
        sessions_dir=sessions_dir,
        imports_dir=repository / "observe-imports",
        port_factory=_port,
        probe=_probe,
        identity_query_text=project.identity_query,
        identity_query_delay_s=project.identity_query_delay_s,
    )
    assembly = web_assembly.Assembly(
        firmware_id=firmware_id,
        label=f"{profile.label} · {profile.project}",
        target_id=profile.target_id,
        device_key=str(profile.device_key or ""),
        project_key=DEMO_PROJECT_KEY,
        platform=profile.platform,
        suites=tuple(project.suites),
        pipeline=pipeline,
        session=session,
        plugins=pipeline.plugins,
        profile=profile,
        project=project,
        # 演示目标没有构建/烧录链（能力表里如实写着 unsupported）⇒ 不冒充"可跑流水线"。
        pipeline_allowed=False,
        token_domain=str(profile.token_domain or ""),
        capability_table=profile.capabilities,
        notes=("演示目标：core 层假目标，不接真实硬件、不读本机台架。",),
    )
    return web_assembly.assemble(
        assemblies=(assembly,),
        targets=(profile,),
        projects=(project,),
        primary=firmware_id,
        runners=runners,
    )


def build_live_assembly(
    runs_dir: Path | None = None,
    *,
    bus: EventBus | None = None,
    sessions_dir: Path | None = None,
    imports_dir: Path | None = None,
    transport: Any = None,
    sniff_dir: Path | None = None,
):
    """**公开运行装配**：把随仓库发布的参考适配装成两条**只读观察链**。

    这一条路是“普通模式”：**不配任何东西也能用**（候选口来自系统串口枚举）；把
    `bench.example.json` 抄成 `bench.local.json`、填上自己的通信口（`devices.<键>.port`）
    只是**增强**（别名、默认参数、与已适配目标的绑定）。之后就是枚举候选口 → 只读嗅探 →
    选择并连接 → 断开与重连；身份未知的设备**先连上、再问身份**；嗅探**不抢占**已连接的口。
    不需要改任何 Python / JavaScript，也不需要写新的装配代码。

    ⚠️ 这一路**只读**：不构建、不烧录、不动调制线、不打开任何口（只有你点“连接”才开）。
    候选口 = **系统串口枚举** ∪ 本机台架点名的口（合并去重）；台架没配不是“空列表”，
    只是少了那一份增强。平台**不按枚举顺序或“唯一在线”替你猜设备**。
    """
    import bench_target  # noqa: PLC0415  (core：公开运行装配的公共部分)

    repository = TOOLS_DIR.parent
    shared_bus = bus or EventBus()
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else repository / "observe-sessions"
    imports_dir = Path(imports_dir) if imports_dir is not None else repository / "observe-imports"
    evidence_dir = Path(runs_dir) if runs_dir is not None else repository / "runs"

    import esp32_target  # noqa: PLC0415  (目标层：随仓库发布的参考适配)
    import stm32_target  # noqa: PLC0415

    skipped: list[str] = []
    chains: list[Any] = []
    # ⚠️ 顺序固定（STM32 在前 = 主装配）：页面的默认选中项与启动横幅都读它。
    for module in (stm32_target, esp32_target):
        try:
            chains.append(
                module.web_chain(
                    runs_dir=evidence_dir,
                    repository=repository,
                    bus=shared_bus,
                    transport=transport,
                )
            )
        except bench_config.BenchFileError as error:
            # 台架文件本身站不住（入口已拒）——这里不掩盖，如实带出去。
            skipped.append(f"{module.TARGET_LABEL}：{error}")
    if not chains:
        raise RuntimeError("没有可用的公开运行装配：连一条参考目标都没装上")

    rows: list[tuple[Any, Any]] = []
    for chain in chains:
        pipe = Pipeline(
            **bench_target.pipeline_kwargs(
                chain, runs_dir=evidence_dir, repository=repository, bus=shared_bus
            )
        )
        rows.append((chain, pipe))

    # 先建 registry：观察会话要拿它的 `active_run_port` 当闸门、拿 `emit` 当广播出口。
    runners = runner_registry.RunnerRegistry(
        tuple(
            runner_registry.Runner(chain.firmware_id, pipe) for chain, pipe in rows
        ),
        primary=rows[0][0].firmware_id,
    )
    # 每一路拿**自己**的登记与插件束：串了口就多一个 owner，而且两份固件会被互相解码。
    sessions = {
        chain.firmware_id: ObserveSession(
            registry=chain.registry,
            plugins=chain.plugins,
            active_run=runners.active_run_port,
            emit=runners.emit,
            sessions_dir=sessions_dir,
            imports_dir=imports_dir,
            port_factory=chain.plugins.port_factory,
            probe=chain.plugins.port_probe,
            identity_query_text=chain.project.identity_query,
            identity_query_delay_s=chain.project.identity_query_delay_s,
        )
        for chain, _pipe in rows
    }
    assemblies = tuple(
        web_assembly.Assembly(
            firmware_id=chain.firmware_id,
            label=f"{chain.facts.label} · {chain.facts.project_label}",
            target_id=chain.facts.target_id,
            device_key=chain.facts.device_key,
            project_key=chain.facts.project_key,
            platform=chain.facts.platform,
            # 这一路没有套件（只读观察）："可选但跑不了"比"一个套件都不列"更糟。
            suites=(),
            pipeline=pipe,
            session=sessions[chain.firmware_id],
            plugins=chain.plugins,
            profile=chain.profile,
            project=chain.project,
            pipeline_allowed=False,
            requires_flash_confirmation=False,
            token_domain="",
            capability_table=chain.profile.capabilities,
            notes=tuple(chain.facts.notes),
        )
        for chain, pipe in rows
    )
    assembled = web_assembly.assemble(
        assemblies=assemblies,
        targets=tuple(chain.profile for chain, _pipe in rows),
        projects=tuple(chain.project for chain, _pipe in rows),
        primary=rows[0][0].firmware_id,
        runners=runners,
        bench_skipped=skipped,
    )

    # 嗅探接线（ / D1）：“怎么探测 / 怎么问一句 / 怎么知道口被谁拿着”全从插件束来；
    # ⚠️ 候选口 = 各链设备登记里的口（台架点名）**再加上系统串口枚举**（见下面 `discovered`）：
    #    两条合起来才是候选清单，所以“台架没配”不等于“没有候选项”。
    holder: dict = {}
    # 需求 §6.11：候选口 = **系统串口枚举**（基础候选源） + 台架点名的那些口（增强），
    # 在同一份列表里合并去重、保序（台架点名的排前面：它们带着别名与默认参数）。
    # ⚠️ 未配置、未适配、身份未知的系统串口**不许被藏掉** —— 它们就是“通用串口”那一类。
    declared = [
        str(spec.port)
        for row in assemblies
        for spec in getattr(row.pipeline.devices, "specs", lambda: ())()
    ]
    # ⚠️ 四条只读观察链共用**同一份**传输实现（`web_chain(transport=...)` 传下去的就是它）；
    #    嗅探的“怎么探占用 / 怎么发一句”从**它**拿，不从“某一条装配的插件”拿 ——
    #    通用口本来就不属于任何一条装配。
    common_transport = rows[0][0].transport
    discovered = [
        name for name, _description in bench_target.system_ports(common_transport)
    ]
    merged, note = bench_target.merge_candidates(declared, discovered)
    listed = sniff_service.candidate_ports(merged)
    if len(listed.ports) < len(merged):
        note += f"；候选口超过上限 {len(listed.ports)} 个，后面的没列出来"
    if not common_transport.ready:
        note += "；⚠️ 这台机器上没有 pyserial：系统串口枚举不出来（先 pip install pyserial）"
    assembled.sniff = sniff_service.SniffExecutor(
        candidates=sniff_service.Candidates(ports=listed.ports, note=note),
        probes=_sniff_probes(assemblies),
        parsers=_sniff_parsers(assemblies),
        access=_sniff_access(assemblies, common_transport),
        conflict=_port_conflict(holder),
        exchange=_sniff_exchange(assemblies, common_transport),
        held_observation=_held_observation(holder),
        activity=_port_activity_source(rows[0][1].store, sessions_dir, imports_dir),
        # ⚠️ 证据目录可指定：测试不该往随发布的树里写东西（以前那些 `sniff/` 就是卫生缺口）。
        evidence=sniff_service.SniffEvidence(
            Path(sniff_dir) if sniff_dir is not None else repository / "sniff"
        ),
    )
    # ⚠️ 延迟绑定：闩门要拿到观察中心，而观察中心在 `assemble()` 里才造出来。
    holder["observe"] = assembled.observe
    return assembled


def build_web_assembly(runs_dir: Path | None = None, *, bus: EventBus | None = None):
    """服务入口的那一行：**普通模式装真板，`--demo` 装演示目标**。

    ⚠️ 两种模式共用同一套 core（装配判据、工作台、证据模型、三种模式都只有一份）：
    差别只在“哪一份目标被装进来”。演示模式用于无硬件体验，
    **不能**代替普通模式下的嗅探与连接验收。
    """
    if DEMO_MODE:
        return build_demo_assembly(runs_dir, bus=bus)
    return build_live_assembly(runs_dir, bus=bus)


def main() -> int:
    args = parse_args()
    url = f"http://{args.host}:{args.port}/"
    if args.stop:
        # 只叫它停，不开服务、不抢锁（锁由它自己释放）。
        return request_stop(args.host, args.port, force=args.force)

    #  / B2：台架文件只在这里读一次（库代码自己不会去读）。
    # ⚠️ 放在 `--stop` 之后：台架文件写得不对也应该能把服务停掉。
    try:
        bench_setup.install_from(cli_path=getattr(args, "bench", ""))
    except bench_config.BenchFileError as error:
        print(f"台架文件无效，拒绝启动：{error}", file=sys.stderr)
        return bench_config.EXIT_REFUSED

    # Single-owner rule, enforced instead of merely documented: a second service
    # (or a CLI tool touching the build dir / a COM port) must not run alongside
    # this one. On Windows SO_REUSEADDR would otherwise let two servers bind the
    # same port silently, and the browser could end up talking to either one.
    try:
        owner = console_lock.acquire(
            PROJECT_DIR,
            kind="webconsole",
            port=args.port,
            url=url,
            label=f"webconsole pid {os.getpid()}",
        )
    except console_lock.OwnerConflict as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2

    # 装配只发生在服务入口：每个 firmware 有一份完整 Pipeline，但共用证据目录、事件总线，
    # 再由 RunnerRegistry 保证整个进程同时只能有一个 run。
    use_demo_data(getattr(args, "demo", False))
    assembled = build_web_assembly(args.runs_dir)
    ways = assembled.problems()
    if ways:  # 装配自己站不住就不该把服务开起来（页面会给出互相矛盾的事实）
        print("REFUSED: 装配自检没过：", file=sys.stderr)
        for line in ways:
            print(f"  - {line}", file=sys.stderr)
        console_lock.release(PROJECT_DIR, pid=os.getpid())
        return 3
    # ⚠️ B6：没配置的链**不登记**，但要说出来 —— 否则用户只会看到"页面上少了一条"。
    for line in assembled.bench_skipped:
        print(f"未配置，这条链没有登记：{line}", file=sys.stderr)
    if assembled.bench_skipped:
        print(
            f"（把 {bench_config.EXAMPLE_NAME} 抄一份成 {bench_config.FILE_NAME} 填上缺的那几格，"
            "再重启服务；`python tools\\doctor.py` 不用配置也能跑）",
            file=sys.stderr,
        )
    server = ConsoleServer(
        (args.host, args.port), ConsoleHandler, assembled.runners, assembled.observe,
        platform=assembled.platform_facts, catalog=assembled.catalog,
        sniff=assembled.sniff,
        assembly=assembled,
    )
    #  / P3：把**上一次进程**留下的导入残留清掉（`<imports>/.incoming-*`）。
    # 工作区锁保证这一刻没有别的实例在跑，所以那些一定是我们自己上次没收拾完的东西：
    # 半包不许留在盘上，更不许以"正在跑"的样子出现在清单里。
    leftover = server.imports.cleanup()
    if leftover:
        print(f"import scratch         : 清掉上次的残留 {', '.join(leftover)}")
    platform_facts = assembled.platform_facts
    print(f"platform               : {platform_facts['label']}")
    print(
        "targets                : "
        + ", ".join(
            f"{row['target_id']}({'可构建' if row['pipeline_allowed'] else '只读'})"
            for row in platform_facts["targets"]
        )
    )
    print(
        "assemblies             : "
        + ", ".join(
            f"{row['firmware_id']} -> {row['project'] or '(无项目包)'}"
            for row in platform_facts.get("assemblies") or []
        )
    )
    print("adapters               : " + ", ".join(platform_facts["adapters"]))
    print(
        "schemas                : "
        + ", ".join(f"{name}={value}" for name, value in platform_facts["schemas"].items())
    )
    print(f"Workbench 示例项目 console : {url}")
    print(f"project                : {assembled.assemblies[0].pipeline.project_dir}")
    print(f"runs dir               : {assembled.runs_dir}")
    print(f"steps                  : {' -> '.join(STEP_ORDER)}")
    print(f"suites                 : {', '.join(assembled.require(assembled.primary).suites)}")
    print(f"workspace lock         : {console_lock.lock_path(PROJECT_DIR)} (pid {owner.pid})")
    print(
        "stop                   : "
        f"python tools\\webconsole\\server.py --stop --port {args.port}"
        "（或 POST /api/stop；握着串口时会拒绝，除非 --force）"
    )
    print("read-only until the page starts an allowed run or connects in live mode")
    if args.open:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.server_close()
        console_lock.release(PROJECT_DIR, pid=os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

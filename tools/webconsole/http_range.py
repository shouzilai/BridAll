""" / L2：大文件的**流式**收发 —— 单段 Range + 固定块。

为什么单独一个模块：run 证据、实时会话证据、导入包证据今天各写一遍 `Path.read_bytes()`
（把整份文件装进内存再发）。把"怎么发"收成一处，才可能用**一条**判据盯住
"这条路上不许出现整读" —— 三条路各写一遍的话，判据只能一条一条地追。

三件事：

1. **计划与执行分开**。`plan_response()` 只看一眼 `stat()`（**快照边界**：这一趟就按这个
   大小发，中途文件变长不算数），把状态码与响应头定下来；`stream_file()` 才真的读字节。
   分开之后，"206 的头对不对"与"发出去的字节数对不对"可以各自断言。
2. **只支持单段**。多段 Range 明确拒绝（416），对齐需求 §5.2；
   端点超界夹住、起点越界拒绝 —— 与 `large_evidence.parse_range_header()` 同一套规则，
   这里不重复实现（那份是 L0 冻结的契约）。
3. **断连不是错误**。客户端中途关掉连接是**正常事件**（用户点了取消、关了标签页）：
   `safe_stream()` 把它记成 `aborted=True` 交回去，**不抛给路由** —— 否则一次取消
   会在服务端留下一个异常，而下一次请求还要照常服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # 作为包导入（``webconsole.http_range``）
    from .large_evidence import (
        DOWNLOAD_CHUNK_BYTES,
        ByteRange,
        Snapshot,
        inline_too_large,
        media_type_for,
        parse_range_header,
        snapshot_size,
    )
except ImportError:  # 直接当脚本跑
    from large_evidence import (  # type: ignore[no-redef]
        DOWNLOAD_CHUNK_BYTES,
        ByteRange,
        Snapshot,
        inline_too_large,
        media_type_for,
        parse_range_header,
        snapshot_size,
    )

#: 断连的三个形状。`OSError` 兜底是因为不同平台抛的不一样
#: （Windows 常见 `ConnectionAbortedError` / `ConnectionResetError`）。
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)


@dataclass(frozen=True)
class Response:
    """一次证据响应的**计划**：状态码、响应头、要发的区间。"""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    window: ByteRange | None = None
    size: int = 0

    @property
    def is_partial(self) -> bool:
        return self.status == 206


def plan_response(
    path: Path | str,
    *,
    range_header: str | None = None,
    download_name: str | None = None,
    media_type: str | None = None,
    cache: str = "no-store",
) -> Response:
    """看一眼文件大小，把这次要发的响应定下来（**不读内容**）。

    - 没有 `Range` ⇒ `200` + 完整长度（真正的字节由 `stream_file()` 按块发）；
    - 有合法 `Range` ⇒ `206` + `Content-Range`；
    - 非法 `Range` ⇒ `416` + `Content-Range: bytes */size`（**不是**静默忽略：
      忽略会让客户端以为拿到的就是全部）。
    """
    target = Path(path)
    snapshot: Snapshot = snapshot_size(target)
    size = snapshot.size
    name = target.name
    headers = {
        "Content-Type": media_type or media_type_for(name),
        "Accept-Ranges": "bytes",
        "Cache-Control": cache,
    }
    if download_name:
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    if size <= 0:
        # 0 字节的证据是存在的（例如这次 run 没有派生物）：照发 200 + 0 长度，
        # 但**任何** Range 都是不可满足的（416），由 parse_range_header 判。
        window = parse_range_header(range_header, size)
        headers["Content-Length"] = "0"
        return Response(status=200, headers=headers, window=None, size=size)

    window = parse_range_header(range_header, size)
    if window is None:
        headers["Content-Length"] = str(size)
        return Response(status=200, headers=headers, window=ByteRange(0, size - 1), size=size)
    headers["Content-Length"] = str(window.length)
    headers["Content-Range"] = window.content_range(size)
    return Response(status=206, headers=headers, window=window, size=size)


def refusal_response(path: Path | str, error) -> Response:
    """把 `large_evidence` 的拒绝翻成一个**可发的** 416（带 `bytes */size`）。

    416 必须带 `Content-Range: bytes */<size>`（RFC 9110）：客户端据此知道"文件多大、
    我这段要不到"。少了它，浏览器只会显示一个光秃秃的 416。
    """
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    return Response(
        status=int(getattr(error, "status", 416)),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes */{size}",
            "Cache-Control": "no-store",
        },
        window=None,
        size=size,
    )


def stream_file(
    path: Path | str,
    window: ByteRange,
    write,
    *,
    chunk_bytes: int = DOWNLOAD_CHUNK_BYTES,
) -> int:
    """把 `window` 这段字节**按块**交给 `write`；返回实际发出的字节数。

    ⚠️ 这是"内存不随文件大小增长"的**唯一机制**：任何时刻手里只有一块。
    这里不捕获断连 —— 那是 `safe_stream()` 的事（分开才能单独测"发够了没有"）。
    """
    width = max(1, int(chunk_bytes))
    sent = 0
    remaining = window.length
    with Path(path).open("rb") as handle:
        handle.seek(window.start)
        while remaining > 0:
            block = handle.read(min(width, remaining))
            if not block:
                break
            write(block)
            sent += len(block)
            remaining -= len(block)
    return sent


def safe_stream(
    path: Path | str,
    window: ByteRange,
    write,
    *,
    chunk_bytes: int = DOWNLOAD_CHUNK_BYTES,
) -> dict:
    """`stream_file()` 的"客户端随时可能消失"版本。

    返回 `{"sent_bytes":…, "aborted":bool, "error":str|None}`。**不抛异常**：
    一次断连是正常事件，吞掉它比让路由 500 更诚实（服务端什么都没坏）。
    """
    try:
        sent = stream_file(path, window, write, chunk_bytes=chunk_bytes)
    except DISCONNECT_ERRORS as error:  # noqa: PERF203 - 只有这一处需要兜
        return {"sent_bytes": 0, "aborted": True, "error": f"{type(error).__name__}: {error}"}
    return {"sent_bytes": sent, "aborted": False, "error": None}


def too_large_for_inline(name: str, size: int):
    """超阈值时交给调用方的那条结构化 413（口径住在 `large_evidence`）。"""
    return inline_too_large(name, size)

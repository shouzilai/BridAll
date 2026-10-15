""" / L0：大体量证据的**冻结契约**（阈值、Range、轻量详情、搜索与稀疏索引）。

这一份**只有形状，没有产品实现** —— 完成定义里那句"无产品实现"是刻意的：
L0 先把"四条路都照着做的契约"钉死，后面的节点才各自去实现，最后再独立复核。

为什么值得单独一个节点：这份契约同时约束**四条各不相同**的路
（HTTP 传输、服务端读取、搜索任务、前端窗口模型）。任何一处先写死一个数字，
另外三处就会各自长出第二份常量 —— 那正是本需求要消灭的东西
（"阈值与默认尾部字节数由 Core 单点配置，Web 不复制另一份常量"）。

这里有五组东西：

1. **阈值与预算**：尾部窗口、单段下载块、分块搜索块、查询长度上限、单请求内存预算。
   全部只有这一份定义。⚠️ **32 MB 只表示"单请求常驻内存上限"这一件事**：
   诊断包上传上限是另一个量、另一个名字（住在 `bundle_import`），两者不许混用。
2. **单段 HTTP Range**：解析（`bytes=a-b` / `bytes=a-` / `bytes=-N`）、合法性、
   `Content-Range` 的写法，以及"多段一律拒绝"这条决定。
3. **轻量详情**：`ArtifactMetadata` —— 名称、字节数、媒体类型、原件/派生、是否还在写、
   是否支持 Range、**可信哈希（没有就写没有，绝不为了显示摘要去现算）**。
4. **搜索任务与结果**：模式（文本 / 字节 / 十六进制）是**封闭集合**；正则明确**不在**
   首版能力内（`SUPPORTS_REGEX = False`，见需求 PERF-SEARCH-04）；任务带快照边界、
   结果上限与超时；结果带字节偏移与命中上下文。
5. **稀疏索引**：`schema + 源文件大小 + 指纹`，对不上就**拒绝使用、可重建**；
   它是给"跳转"用的，**不替代原件**。

⚠️ 三条不变量（都写在类型里，不靠调用方自觉）：

- **快照边界**：任何一次读取都先固定"这次看到的文件多大"（`Snapshot`），
  之后所有区间与进度都以它为界 —— 正在写的文件因此不会在传输中途"变长"。
- **区间不重叠**：向前翻页与分块扫描都由这里的纯函数给出，调用方不许自己算偏移。
- **跨块匹配不许漏**：分块扫描必须让相邻块**重叠 `len(pattern) - 1` 字节**，
  否则一个词恰好骑在块边界上就会被漏掉，而漏检看起来和"确实没有"一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:  # 作为包导入（``webconsole.large_evidence``）
    from .core_primitives import utc_now
except ImportError:  # 直接当脚本跑
    from core_primitives import utc_now  # type: ignore[no-redef]

#: 这份契约自己的版本。形状变了就 +1（并同时改 `test_large_evidence`）。
SCHEMA = 1

#: 轻量详情投影的版本（需求 §5.1：新增的是**版本化只读投影**，不是改老接口）。
LIGHT_DETAIL_VERSION = 1


# ===================================================================== 阈值与预算
#: 默认尾部窗口：打开一个已关闭的证据时，第一屏读最后这么多字节。
#: 需求 §4.2 给的范围是 1～5 MB，取 2 MiB —— 一屏几万行足够"看得见结尾"，
#: 又离"初始正文传输不超过 5 MB"的预算留了一半余量。
TAIL_WINDOW_BYTES = 2 * 1024 * 1024

#: 一次尾部/向前装载的**上限**。窗口参数可以调小，但不许越过它（预算就是预算）。
MAX_TAIL_WINDOW_BYTES = 5 * 1024 * 1024

#: 完整下载与单段响应的发送块。**这是"不随文件大小线性占内存"的唯一机制**：
#: 读一块、写一块，任何时刻手里只有一个块。
DOWNLOAD_CHUNK_BYTES = 256 * 1024

#: 分块搜索的块大小（服务端有界内存扫描）。越大越快、内存占用也越大。
SEARCH_BLOCK_BYTES = 1 * 1024 * 1024

#: 一次搜索最多回多少条命中（超出 ⇒ `truncated=True`，如实说"还有"）。
SEARCH_MAX_RESULTS_DEFAULT = 200
SEARCH_MAX_RESULTS_LIMIT = 2000

#: 一次搜索的墙钟上限。到点就收摊并把**已经扫到的东西**交出去（不是丢弃）。
SEARCH_TIMEOUT_S = 20.0

#: 查询本身的长度上限（字符）。搜索必须有界：不受限的输入迟早变成一次拒绝服务。
SEARCH_QUERY_MAX_LEN = 200

#: 一条命中回给页面的**上下文长度上限**（字符）。命中本身带的是字节偏移，上下文只是
#: 给人看的：截断了就用省略号标出来，绝不假装那是完整的行。
#: （L4 追加：与其它搜索阈值放在同一份契约里 —— 服务端与前端不各抄一个数。）
SEARCH_SNIPPET_MAX_BYTES = 160

#: 稀疏索引多久记一个点（字节）。越大索引越小、跳转越粗。
SPARSE_INDEX_STRIDE_BYTES = 1024 * 1024

#: 稀疏索引的形状版本。源文件大小或指纹对不上就拒绝使用（需求 PERF-INDEX-01）。
SPARSE_INDEX_SCHEMA = 1

#: **单请求额外常驻内存上限**。需求 §6 的验收目标：回看一个 400 MB run 时，
#: 单请求额外常驻内存不超过这个数，而且**不随证据总大小线性增长**。
#:
#: ⚠️ 这个名字里"REVIEW"是刻意的：它只说"回看/搜索这一条路能多占多少内存"。
#: 上传诊断包的 32 MB 是**另一个量**（那份限制住在 `bundle_import.MAX_BUNDLE_BYTES`，
#: 量的是"别人给的包最大能多大"）。两个数碰巧相等，含义完全不同 ——
#: 所以这里既不引用它、也不允许有人把它们并成一个常量。
REVIEW_MEMORY_BUDGET_BYTES = 32 * 1024 * 1024


# ===================================================================== 错误模型
#: 统一错误码 → HTTP 状态。**只有这一份**：服务端不许自己另写状态码，
#: 客户端也不许按消息文本分支（消息是给人读的，会变）。
ERROR_CODES: tuple[tuple[str, int, str], ...] = (
    ("range_unsupported", 416, "Range 用了不支持的形式（非 bytes=、或多段）"),
    ("range_malformed", 416, "Range 形状不对（缺分隔符、非数字、起点在终点之后）"),
    ("range_unsatisfiable", 416, "这个区间落在文件之外（含 0 字节文件）"),
    ("inline_too_large", 413, "超过内联阈值：改走 Range 或流式下载"),
    ("artifact_missing", 404, "这份证据现在不存在"),
    ("artifact_unknown", 404, "这个名字不在证据白名单里"),
    ("query_too_long", 400, "查询超过长度上限"),
    ("query_invalid", 400, "查询本身不合法（模式不认识 / 编码不对 / 空查询）"),
    ("search_busy", 409, "同一份证据上已经有一个搜索在跑"),
)

ERROR_STATUS: dict[str, int] = {code: status for code, status, _why in ERROR_CODES}

ERROR_RANGE_UNSUPPORTED = "range_unsupported"
ERROR_RANGE_MALFORMED = "range_malformed"
ERROR_RANGE_UNSATISFIABLE = "range_unsatisfiable"
ERROR_INLINE_TOO_LARGE = "inline_too_large"
ERROR_ARTIFACT_MISSING = "artifact_missing"
ERROR_ARTIFACT_UNKNOWN = "artifact_unknown"
ERROR_QUERY_TOO_LONG = "query_too_long"
ERROR_QUERY_INVALID = "query_invalid"
ERROR_SEARCH_BUSY = "search_busy"


class EvidenceError(Exception):
    """带**机器可读**错误码的拒绝。路由层只认 `code` 与 `status`。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        if code not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{code!r}（先加到 ERROR_CODES）")
        self.code = code
        self.status = int(status if status is not None else ERROR_STATUS[code])
        self.message = str(message)

    def to_json(self) -> dict[str, object]:
        return {"error": self.message, "code": self.code, "status": self.status}


# ===================================================================== 单段 Range
@dataclass(frozen=True)
class ByteRange:
    """一段**闭区间**字节（`start` 与 `end` 都含在内）。

    HTTP 的 `bytes=a-b` 就是闭区间，直接照它存，避免每次换算都差一个 1
    （差一错误在"下载少一字节"上是不可见的，只有在哈希核对时才炸）。
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"区间非法：{self.start}-{self.end}")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, size: int) -> str:
        """`Content-Range: bytes a-b/size`（206 响应的标准写法）。"""
        return f"bytes {self.start}-{self.end}/{int(size)}"

    def covers(self, offset: int) -> bool:
        return self.start <= offset <= self.end

    def overlaps(self, other: "ByteRange") -> bool:
        return self.start <= other.end and other.start <= self.end


def parse_range_header(value: str | None, size: int) -> ByteRange | None:
    """解析单段 `Range` 头；没有这个头 ⇒ `None`（= 完整下载）。

    ⚠️ 三条决定，都是需求里写明的：

    - **只支持 `bytes=`**：别的单位（时间区间之类）不认，回 416 而不是忽略。
    - **多段一律拒绝**："第一阶段不支持多段 Range 时必须明确拒绝"（需求 §5.2）。
    - **端点超界要夹住，起点超界要拒绝**：`bytes=0-99999999` 对 100 字节的文件
      是合法的（夹到 99），而 `bytes=100-` 对同样大小是 416（RFC 的"不可满足"）。
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    # ⚠️ 不用 `str.partition`：`partition` 是本仓库登记的平台标识（分区表），
    #    core 代码里出现它就等于一次未登记的平台命中（B3 踩过同一个坑）。
    pieces = raw.split("=", 1)
    if len(pieces) != 2 or pieces[0].strip().lower() != "bytes":
        raise EvidenceError(ERROR_RANGE_UNSUPPORTED, "只支持 `bytes=` 的单段 Range")
    spec = pieces[1].strip()
    if "," in spec:
        raise EvidenceError(
            ERROR_RANGE_UNSUPPORTED, "多段 Range 不支持：一次只发一段（请在客户端分几次取）"
        )
    if "-" not in spec:
        raise EvidenceError(ERROR_RANGE_MALFORMED, f"Range 形状不对：{value!r}")
    left, right = (part.strip() for part in spec.split("-", 1))
    if left == "" and right == "":
        raise EvidenceError(ERROR_RANGE_MALFORMED, f"Range 形状不对：{value!r}")
    if size <= 0:
        raise EvidenceError(
            ERROR_RANGE_UNSATISFIABLE, "这份证据现在是 0 字节，任何区间都取不到"
        )
    if left == "":
        # 后缀形状 `bytes=-N`：最后 N 字节。N 超过文件大小就是整个文件（RFC 允许）。
        if not right.isdigit():
            raise EvidenceError(ERROR_RANGE_MALFORMED, f"Range 形状不对：{value!r}")
        count = int(right)
        if count <= 0:
            raise EvidenceError(
                ERROR_RANGE_UNSATISFIABLE, "`bytes=-0` 取不到任何字节"
            )
        return ByteRange(max(0, size - count), size - 1)
    if not left.isdigit():
        raise EvidenceError(ERROR_RANGE_MALFORMED, f"Range 形状不对：{value!r}")
    start = int(left)
    if start >= size:
        raise EvidenceError(
            ERROR_RANGE_UNSATISFIABLE, f"起点 {start} 已经越过文件末尾（共 {size} 字节）"
        )
    if right == "":
        return ByteRange(start, size - 1)
    if not right.isdigit():
        raise EvidenceError(ERROR_RANGE_MALFORMED, f"Range 形状不对：{value!r}")
    end = int(right)
    if end < start:
        raise EvidenceError(
            ERROR_RANGE_MALFORMED, f"区间起点在终点之后：{start}-{end}"
        )
    return ByteRange(start, min(end, size - 1))


@dataclass(frozen=True)
class Snapshot:
    """**这一次**读取看到的文件边界（需求 §5.2 的"快照边界"）。

    正在写的文件每一刻都在变长。一次请求一开始就把 `size` 固定下来，
    之后的分块、进度、`Content-Range` 全以它为准 —— 否则"传了 10 MB，
    声明 8 MB"这种事就会发生，而它看起来像客户端算错了。
    """

    name: str
    size: int

    def clamp(self, window: ByteRange | None) -> ByteRange | None:
        if window is None or self.size <= 0:
            return None
        return ByteRange(min(window.start, self.size - 1), min(window.end, self.size - 1))


def snapshot_size(path: Path | str) -> Snapshot:
    """按路径取一次快照（只 `stat`，不读内容）。"""
    target = Path(path)
    return Snapshot(name=target.name, size=target.stat().st_size)


def tail_window(size: int, *, want_bytes: int = TAIL_WINDOW_BYTES) -> ByteRange | None:
    """默认尾部窗口：最后 `want_bytes` 字节（文件更小就整个文件）。0 字节 ⇒ `None`。"""
    if size <= 0:
        return None
    width = max(1, min(int(want_bytes), MAX_TAIL_WINDOW_BYTES))
    return ByteRange(max(0, size - width), size - 1)


def previous_window(
    size: int, before: int, *, want_bytes: int = TAIL_WINDOW_BYTES
) -> ByteRange | None:
    """向前翻一页：取 `before` **之前**的那一段（`before` 不含在内）。到头 ⇒ `None`。"""
    if size <= 0 or before <= 0:
        return None
    width = max(1, min(int(want_bytes), MAX_TAIL_WINDOW_BYTES))
    end = min(int(before) - 1, size - 1)
    start = max(0, end - width + 1)
    return ByteRange(start, end)


def merge_ranges(ranges: Iterable[ByteRange]) -> list[ByteRange]:
    """把若干段合并成**有序、互不重叠也不相邻**的区间（前端"已加载区间"用它去重）。

    相邻（`1-2` 与 `3-4`）与重叠都合并：它们连着就是同一段字节。
    ⚠️ 中间**缺至少一个字节**的两段绝不合并 —— 合并会把"这里没加载"说成"已加载"，
    而页面正是拿这组区间去告诉用户"你现在看到的是哪一段"。
    """
    merged: list[ByteRange] = []
    for item in sorted(ranges, key=lambda rng: (rng.start, rng.end)):
        if merged and item.start <= merged[-1].end + 1:
            last = merged[-1]
            merged[-1] = ByteRange(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


def chunk_ranges(
    size: int, *, block: int = DOWNLOAD_CHUNK_BYTES, start: int = 0, end: int | None = None
) -> list[ByteRange]:
    """把 `[start, end]` 切成固定大小的块，**恰好覆盖一次**（不多不少）。

    发送器按它读一块、写一块；分块扫描也用它。
    """
    width = max(1, int(block))
    last = (int(size) - 1) if end is None else min(int(end), int(size) - 1)
    first = max(0, int(start))
    ranges: list[ByteRange] = []
    cursor = first
    while cursor <= last:
        stop = min(cursor + width - 1, last)
        ranges.append(ByteRange(cursor, stop))
        cursor = stop + 1
    return ranges


def search_block_ranges(
    size: int, *, overlap: int = 0, block: int = SEARCH_BLOCK_BYTES
) -> list[ByteRange]:
    """分块搜索的块序（相邻块**重叠** `overlap` 字节，用来接住跨块的命中）。

    ⚠️ `overlap` 必须 ≥ `len(pattern) - 1`：否则一个词恰好骑在边界上会被两块
    各自"看见一半"而**谁都报不出**。漏检看起来和"确实没有"一模一样，
    所以这条不是优化，是正确性。

    最后一块可能在**边界刚过**就停下（不为了凑满一块而越界）。
    """
    total = int(size)
    if total <= 0:
        return []
    width = max(1, int(block))
    step = max(1, width - max(0, int(overlap)))
    ranges: list[ByteRange] = []
    cursor = 0
    while cursor < total:
        ranges.append(ByteRange(cursor, min(cursor + width - 1, total - 1)))
        if cursor + width >= total:
            break
        cursor += step
    return ranges


def overlap_for_pattern(pattern: bytes | bytearray | str) -> int:
    """接住跨块命中所需的**最少**重叠字节数（= 模式长度 - 1，空模式为 0）。"""
    length = len(pattern)
    return max(0, length - 1)


# =================================================================== 轻量详情
#: 原件 / 派生。**派生不等于不重要**：它是"由原件推出来的"，原件丢了它就没了。
ROLE_ORIGINAL = "original"
ROLE_DERIVED = "derived"
ARTIFACT_ROLES: tuple[str, ...] = (ROLE_ORIGINAL, ROLE_DERIVED)

#: 是否还在写。`open` 的意思只有一个：**这份证据此刻还会变长**，
#: 所以它的字节数是"到目前为止"，不是一个最终结论。
STATE_OPEN = "open"
STATE_CLOSED = "closed"
ARTIFACT_STATES: tuple[str, ...] = (STATE_OPEN, STATE_CLOSED)

#: 媒体类型按**扩展名**推导（一处定义）。原来散在路由层的表由它取代。
MEDIA_TYPES: dict[str, str] = {
    ".bin": "application/octet-stream",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".zip": "application/zip",
}
DEFAULT_MEDIA_TYPE = "application/octet-stream"


def media_type_for(name: str) -> str:
    """按文件名后缀给媒体类型。认不出的后缀 ⇒ 二进制（保守：别拿它当文本猜）。"""
    return MEDIA_TYPES.get(Path(str(name)).suffix.lower(), DEFAULT_MEDIA_TYPE)


def extension_is_text(name: str) -> bool:
    """这个后缀看起来是不是文本（决定前端要不要按行拆）。"""
    media = media_type_for(name)
    return (
        media.startswith("text/")
        or media.startswith("application/json")
        or media.startswith("application/x-ndjson")
    )


@dataclass(frozen=True)
class ArtifactMetadata:
    """轻量详情里的**一行产物元数据**（需求 §5.1 的返回示意）。

    ⚠️ `sha256` 默认是 `None`，意思是"**未记录**"—— 打开页面绝不许为了显示摘要
    去现算 400 MB 的哈希（PERF-HIST-07）。要哈希就由人显式点"核验"，或者用
    `hashlib` 在独立工具里流式算。
    """

    name: str
    bytes: int
    media_type: str
    role: str
    state: str
    range: bool = True
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ARTIFACT_ROLES:
            raise ValueError(f"没登记的角色：{self.role!r}（{ARTIFACT_ROLES}）")
        if self.state not in ARTIFACT_STATES:
            raise ValueError(f"没登记的状态：{self.state!r}（{ARTIFACT_STATES}）")
        if int(self.bytes) < 0:
            raise ValueError("字节数不能是负数")

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "bytes": int(self.bytes),
            "media_type": self.media_type,
            "role": self.role,
            "state": self.state,
            "range": bool(self.range),
            "sha256": self.sha256,
        }


def artifact_metadata(
    path: Path | str,
    name: str,
    *,
    role: str,
    state: str = STATE_CLOSED,
    media_type: str | None = None,
    sha256: str | None = None,
) -> ArtifactMetadata:
    """从磁盘**只取大小**，拼一行元数据。不读内容、不算哈希。"""
    target = Path(path)
    return ArtifactMetadata(
        name=str(name),
        bytes=target.stat().st_size,
        media_type=media_type or media_type_for(name),
        role=role,
        state=state,
        sha256=sha256,
    )


#: 超过它就**不准**再走"内联正文"这条老路（需求 §5.1 的小文件阈值）。
INLINE_PREVIEW_MAX_BYTES = 1 * 1024 * 1024

#: 详情响应的正文策略（封闭集合）—— L2 起 run / 会话两条详情路共用这一份名目：
#: `auto` = 按阈值自己决定；`none` = 只要元数据；`inline` = 旧的内嵌正文形式（**仅阈值内**）。
#: 写在这里是因为它描述的正是 `should_inline()` / `inline_too_large()` 那对函数的用法，
#: 两份详情各写一份的话，就会出现"一条路默认全读、另一条路默认不读"。
CONTENT_MODES: tuple[str, ...] = ("auto", "none", "inline")


def should_inline(size: int) -> bool:
    """这份证据还能不能内联进详情响应（小文件才允许，避免留下默认全量读取的公开路径）。"""
    return 0 <= int(size) <= INLINE_PREVIEW_MAX_BYTES


def inline_too_large(name: str, size: int) -> EvidenceError:
    """超阈值时的**结构化 413**：带名字、大小、阈值，并指路 Range。"""
    return EvidenceError(
        ERROR_INLINE_TOO_LARGE,
        f"{name} 有 {int(size)} B，超过内联阈值 {INLINE_PREVIEW_MAX_BYTES} B；"
        "请改用 `Range: bytes=…`（默认尾部窗口见 large_evidence.TAIL_WINDOW_BYTES）"
        "或走流式下载",
    )


# =================================================================== 搜索契约
#: 搜索模式是**封闭集合**。"文本"= 人类可读子串；"字节"= 带转义的字节串；
#: "十六进制"= 十六进制串。三者的能力分别声明，不互相冒充。
SEARCH_MODE_TEXT = "text"
SEARCH_MODE_BYTES = "bytes"
SEARCH_MODE_HEX = "hex"
SEARCH_MODES: tuple[str, ...] = (SEARCH_MODE_TEXT, SEARCH_MODE_BYTES, SEARCH_MODE_HEX)

#: **首版不做正则**（需求 PERF-SEARCH-04 只说"若开放，必须限制长度与执行时间"）。
#: 明确写"不做"比含糊的"暂不支持"好：调用方据此选模式，而不是猜。
SUPPORTS_REGEX = False

#: 一个搜索任务的状态（封闭集合）。
SEARCH_STATES: tuple[str, ...] = ("running", "done", "cancelled", "timeout", "error")


@dataclass(frozen=True)
class SearchTask:
    """一次全文件搜索的**任务描述**（需求 PERF-SEARCH-02 的进度/取消/上限/超时）。

    `size` 是任务开始那一刻的快照边界：正在写的文件"按搜索开始时的大小形成快照"。
    """

    task_id: str
    evidence: str
    mode: str
    query: str
    size: int
    started_utc: str = field(default_factory=utc_now)
    max_results: int = SEARCH_MAX_RESULTS_DEFAULT
    timeout_s: float = SEARCH_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.mode not in SEARCH_MODES:
            raise ValueError(f"没登记的搜索模式：{self.mode!r}（{SEARCH_MODES}）")
        if not (1 <= int(self.max_results) <= SEARCH_MAX_RESULTS_LIMIT):
            raise ValueError(
                f"结果上限必须在 1..{SEARCH_MAX_RESULTS_LIMIT} 之间：{self.max_results}"
            )
        if float(self.timeout_s) <= 0:
            raise ValueError("超时必须为正数")

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "evidence": self.evidence,
            "mode": self.mode,
            "query": self.query,
            "size": int(self.size),
            "started_utc": self.started_utc,
            "max_results": int(self.max_results),
            "timeout_s": float(self.timeout_s),
        }


@dataclass(frozen=True)
class SearchHit:
    """一条命中：**字节偏移**是证据（行号与时间是"如果能算就给"）。"""

    offset: int
    length: int
    text: str
    line_no: int | None = None
    at_block_start: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "offset": int(self.offset),
            "length": int(self.length),
            "text": self.text,
            "line_no": None if self.line_no is None else int(self.line_no),
            # 骑在块边界上（由两块拼出来）的命中要标出来：它的上下文可能不完整。
            "at_block_start": bool(self.at_block_start),
        }


@dataclass
class SearchOutcome:
    """一次搜索的**结果**（含进度与"是不是被截断"）。"""

    task_id: str
    state: str
    scanned_bytes: int
    total_bytes: int
    hits: list[SearchHit] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in SEARCH_STATES:
            raise ValueError(f"没登记的状态：{self.state!r}（{SEARCH_STATES}）")

    @property
    def progress(self) -> float:
        total = int(self.total_bytes)
        if total <= 0:
            return 1.0 if self.state != "running" else 0.0
        return max(0.0, min(1.0, int(self.scanned_bytes) / total))

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "scanned_bytes": int(self.scanned_bytes),
            "total_bytes": int(self.total_bytes),
            "progress": round(self.progress, 4),
            "hits": [hit.to_json() for hit in self.hits],
            "truncated": bool(self.truncated),
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "error": self.error,
        }


def validate_query(text: str) -> str:
    """查询的**有界性**：非空、不超过长度上限。返回去掉两端空白后的查询。"""
    value = str(text or "").strip()
    if not value:
        raise EvidenceError(ERROR_QUERY_INVALID, "查询是空的")
    if len(value) > SEARCH_QUERY_MAX_LEN:
        raise EvidenceError(
            ERROR_QUERY_TOO_LONG,
            f"查询太长：{len(value)} 个字符（上限 {SEARCH_QUERY_MAX_LEN}）",
        )
    return value


def compile_pattern(mode: str, text: str) -> bytes:
    """把查询编译成**要在字节流里找的那串字节**。

    三种模式的区别是实实在在的：文本模式把字符按 UTF-8 编码；字节模式认
    `\\n` / `\\xNN` 这类转义；十六进制模式只认十六进制数字。**正则不在其中**
    （`SUPPORTS_REGEX = False`）—— 传 `"regex"` 会在这里被拒绝，而不是被当成子串。
    """
    value = validate_query(text)
    if mode == SEARCH_MODE_TEXT:
        return value.encode("utf-8")
    if mode == SEARCH_MODE_HEX:
        digits = value.replace(" ", "").replace(",", "").replace("0x", "").replace("0X", "")
        if not digits or len(digits) % 2 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
            raise EvidenceError(
                ERROR_QUERY_INVALID,
                "十六进制查询必须是偶数个十六进制数字（可带空格分隔）",
            )
        return bytes.fromhex(digits)
    if mode == SEARCH_MODE_BYTES:
        return _unescape_bytes(value)
    raise EvidenceError(
        ERROR_QUERY_INVALID,
        f"没登记的搜索模式：{mode!r}（可用：{'、'.join(SEARCH_MODES)}；本版不支持正则）",
    )


def _unescape_bytes(value: str) -> bytes:
    """字节模式的转义：`\\n` `\\r` `\\t` `\\0` `\\\\` `\\xNN`，其余字符按 UTF-8 走。"""
    out = bytearray()
    index = 0
    simple = {"n": 0x0A, "r": 0x0D, "t": 0x09, "0": 0x00, "\\": 0x5C}
    while index < len(value):
        char = value[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(value):
            raise EvidenceError(ERROR_QUERY_INVALID, "字节查询以孤立的反斜杠结尾")
        marker = value[index + 1]
        if marker in simple:
            out.append(simple[marker])
            index += 2
            continue
        if marker == "x":
            digits = value[index + 2:index + 4]
            if len(digits) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
                raise EvidenceError(ERROR_QUERY_INVALID, f"`\\x` 后面要跟两个十六进制数字：{value!r}")
            out.append(int(digits, 16))
            index += 4
            continue
        raise EvidenceError(ERROR_QUERY_INVALID, f"不认识的转义：`\\{marker}`")
    if not out:
        raise EvidenceError(ERROR_QUERY_INVALID, "字节查询展开后是空的")
    return bytes(out)


# =================================================================== 稀疏索引
@dataclass(frozen=True)
class SparseIndexEntry:
    """一个锚点：**字节偏移** → 行号（与能算出来时的时间）。"""

    byte_offset: int
    line_no: int
    t_s: float | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "byte_offset": int(self.byte_offset),
            "line_no": int(self.line_no),
            "t_s": None if self.t_s is None else float(self.t_s),
        }


@dataclass(frozen=True)
class SparseIndex:
    """给"跳转"用的稀疏索引。**它不替代原件** —— 原件永远在，索引坏了就重建。"""

    source_name: str
    source_bytes: int
    entries: tuple[SparseIndexEntry, ...] = ()
    stride: int = SPARSE_INDEX_STRIDE_BYTES
    schema: int = SPARSE_INDEX_SCHEMA
    source_sha256: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "schema": int(self.schema),
            "source_name": self.source_name,
            "source_bytes": int(self.source_bytes),
            "source_sha256": self.source_sha256,
            "stride": int(self.stride),
            "entries": [entry.to_json() for entry in self.entries],
        }

    def entry_before(self, offset: int) -> SparseIndexEntry | None:
        """最靠近 `offset` 且不越过它的锚点（跳转就从这里开始顺扫）。"""
        best: SparseIndexEntry | None = None
        for entry in self.entries:
            if entry.byte_offset <= int(offset) and (best is None or entry.byte_offset > best.byte_offset):
                best = entry
        return best


def sparse_index_usable(
    index: SparseIndex, *, source_bytes: int, source_sha256: str | None = None
) -> bool:
    """这份索引还能不能用在**当前**这份源上（PERF-INDEX-01）。

    三条：schema 认得出、大小对得上、指纹对得上。

    ⚠️ 最后一条是**保守**的：索引记了指纹而这边拿不出指纹 ⇒ **不可用**。
    "没得核对"不等于"核对通过"—— 这是本仓库反复吃过的那条教训。
    """
    if int(index.schema) != SPARSE_INDEX_SCHEMA:
        return False
    if int(index.source_bytes) != int(source_bytes):
        return False
    if index.source_sha256 is not None:
        return source_sha256 is not None and index.source_sha256 == source_sha256
    return source_sha256 is None


def sparse_index_reason(
    index: SparseIndex, *, source_bytes: int, source_sha256: str | None = None
) -> str | None:
    """不可用时**说清为什么**（"未建立索引"与"索引过期"是两件事，页面要分开说）。"""
    if sparse_index_usable(index, source_bytes=source_bytes, source_sha256=source_sha256):
        return None
    if int(index.schema) != SPARSE_INDEX_SCHEMA:
        return f"索引 schema 是 {index.schema}，这一版只认 {SPARSE_INDEX_SCHEMA}"
    if int(index.source_bytes) != int(source_bytes):
        return f"索引记的是 {index.source_bytes} B，源文件现在是 {int(source_bytes)} B（重建）"
    return "索引记了源文件指纹，这次没拿到同一份指纹（重建）"


# ===================================================================== 自述
def describe() -> dict[str, object]:
    """给页面/诊断看的**自述**：这一版冻结了哪些量、支持什么。

    ⚠️ 只增字段：页面不认识就不读。它也刻意**不**暴露 `REVIEW_MEMORY_BUDGET_BYTES`
    之外的任何"预算"数字 —— 免得有人把它当成"最大能传多大"。
    """
    return {
        "schema": SCHEMA,
        "light_detail_version": LIGHT_DETAIL_VERSION,
        "tail_window_bytes": TAIL_WINDOW_BYTES,
        "max_tail_window_bytes": MAX_TAIL_WINDOW_BYTES,
        "download_chunk_bytes": DOWNLOAD_CHUNK_BYTES,
        "search_block_bytes": SEARCH_BLOCK_BYTES,
        "search_modes": list(SEARCH_MODES),
        "regex": SUPPORTS_REGEX,
        "search_max_results": SEARCH_MAX_RESULTS_DEFAULT,
        "search_timeout_s": SEARCH_TIMEOUT_S,
        "sparse_index_schema": SPARSE_INDEX_SCHEMA,
        "sparse_index_stride_bytes": SPARSE_INDEX_STRIDE_BYTES,
        "inline_preview_max_bytes": INLINE_PREVIEW_MAX_BYTES,
        "range": "single",
    }

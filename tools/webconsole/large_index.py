""" / L4：**稀疏定位索引** —— 只为"跳转"，不替代原件。

需求 PERF-INDEX-01 的三条：给文本/JSONL 建立「字节偏移 → 行号（与可得时间）」的稀疏
索引、索引带 schema 与源文件大小/指纹、不匹配就**拒绝使用并可重建**。

几条纪律：

1. **原件永远在**。索引坏了、过期了、根本没有 —— 都能退化成一趟顺扫。旧证据（PERF-HIST-08）
   因此**不判为损坏**：页面说"未建立索引"，功能照常。
2. **不敢说的就写没有**。时间字段（`t_s`）只有在调用方交来一个可信的取值口时才会被记；
   没有就写 `None`（"未记录"），**绝不拿文件修改时间或顺序猜**。指纹同理：没有可信
   SHA-256 时，索引里写 `None`，核对就只按大小来（L0 的 `sparse_index_usable` 对此
   是保守的：索引记了指纹而这边拿不出 ⇒ 不可用）。
3. **建立索引只用一趟顺序读**，每块 1 MiB 上下；索引本身是"每 1 MiB 一个锚点"，
   400 MB 也就是几百个点、几十 KB。它不是把文件搬一份。
"""
from __future__ import annotations

from pathlib import Path
import json
from typing import Callable

try:  # 作为包导入（`webconsole.large_index`）
    from . import large_evidence as ev
except ImportError:  # 与其它 webconsole 模块并排执行
    import large_evidence as ev  # type: ignore[no-redef]


#: 索引文件放哪儿：证据所在的那份目录下的 `index/`（run 目录、会话目录各一份）。
INDEX_DIRNAME = "index"
INDEX_SUFFIX = ".json"

#: 取值口：给一段**行文本**，交出这一行的时刻（秒）。没有可信来源时就不传。
TimeProbe = Callable[[str], float | None]

#: 跳转（按行号找字节偏移 / 按偏移找行号）时往前读的块大小与**总上限**。
#: 有界是刻意的：找不到就说找不到，绝不为了跳一次转而去读整份证据。
LOCATE_READ_CHUNK_BYTES = 1024 * 1024
LOCATE_READ_MAX_BYTES = 8 * 1024 * 1024


def index_dir(root: Path | str) -> Path:
    return Path(root) / INDEX_DIRNAME


def index_path(root: Path | str, name: str) -> Path:
    """索引文件名 = 证据名 + `.json`。证据名里的路径分隔符一律拒绝（不许跳目录）。"""
    clean = str(name or "")
    if not clean or clean in {".", ".."} or "/" in clean or "\\" in clean:
        raise ValueError(f"证据名不合法：{name!r}")
    return index_dir(root) / (clean + INDEX_SUFFIX)


def build_index(
    path: Path | str,
    name: str,
    *,
    stride: int = ev.SPARSE_INDEX_STRIDE_BYTES,
    block: int = ev.SEARCH_BLOCK_BYTES,
    digest: str | None = None,
    time_probe: TimeProbe | None = None,
) -> ev.SparseIndex:
    """一趟顺序扫描，每 `stride` 字节记一个锚点（锚点 = **它所在那一行的行首**）。

    "锚点"记的是**行首**而不是原样的偏移：这样拿到一个锚点就能直接跳过去，
    不会落在一行的中间。行号是**数出来的**（一路数字节里的换行），不是估的。

    `digest` 只在调用方**本来就有**可信 SHA-256 时传（例如 `report.json` 里已经记下的
    那一份）—— 建立索引不为显示摘要现算哈希（PERF-HIST-07）。
    """
    target = Path(path)
    size = target.stat().st_size
    width = max(1, int(stride))
    chunk_bytes = max(1, int(block))
    entries: list[ev.SparseIndexEntry] = []
    counted = 0  # 已经数过换行的字节数
    lines_before = 0  # [0, counted) 里的换行数
    last_nl_abs: int | None = None  # 最近一个换行的绝对偏移（可能在上一块里）
    last_nl_line: int | None = None  # 那个换行所在的行号
    anchor = 0
    with open(target, "rb") as handle:
        base = 0
        while anchor < size:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            end = base + len(chunk)
            # ⚠️ 先处理落在本块里的锚点，**再**更新"最近换行"的位置：
            # 反过来的话，本块里靠后的换行会被当成锚点之前的换行。
            while anchor < size and anchor < end:
                rel = anchor - base
                previous = chunk.rfind(b"\n", 0, rel) if rel > 0 else -1
                if previous >= 0:
                    start = base + previous + 1
                    line_no = lines_before + chunk[: previous + 1].count(b"\n") + 1
                elif last_nl_abs is not None and last_nl_line is not None:
                    start = last_nl_abs + 1
                    line_no = last_nl_line + 1
                else:
                    start = 0
                    line_no = 1
                t_s = _time_at(target, start, time_probe)
                entries.append(
                    ev.SparseIndexEntry(byte_offset=start, line_no=line_no, t_s=t_s)
                )
                anchor += width
            newest = chunk.rfind(b"\n")
            if newest >= 0:
                last_nl_abs = base + newest
                last_nl_line = lines_before + chunk[: newest + 1].count(b"\n")
            lines_before += chunk.count(b"\n")
            counted = end
            base = end
    return ev.SparseIndex(
        source_name=str(name),
        source_bytes=int(size),
        entries=tuple(entries),
        stride=width,
        source_sha256=digest,
    )


def _time_at(path: Path, offset: int, probe: TimeProbe | None) -> float | None:
    """从锚点所在的那一行里读一个时刻。**没有可信取值口就返回"未记录"。**"""
    if probe is None:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(int(offset))
            row = handle.readline(4096)
    except OSError:
        return None
    try:
        return probe(row.decode("utf-8", "replace"))
    except Exception:  # 取值口自己不靠谱时：宁可"未记录"，也不要让建索引失败
        return None


def save_index(root: Path | str, index: ev.SparseIndex) -> Path:
    target = index_path(root, index.source_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(index.to_json(), ensure_ascii=False, indent=2).encode("utf-8")
    # 先写临时文件再换名：中途失败时留下的是一个**不完整**的索引文件，
    # 而"不完整"恰恰会被 `usable_index()` 当成不可用 ⇒ 退化顺扫并重建。
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(target)
    return target


def load_index(root: Path | str, name: str) -> ev.SparseIndex | None:
    """读一份索引。**读不出来一律返回 `None`**（坏文件不是错误，是"没有索引"）。"""
    try:
        target = index_path(root, name)
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    rows = raw.get("entries")
    if not isinstance(rows, list):
        return None
    entries: list[ev.SparseIndexEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            entries.append(
                ev.SparseIndexEntry(
                    byte_offset=int(row["byte_offset"]),
                    line_no=int(row["line_no"]),
                    t_s=None if row.get("t_s") is None else float(row["t_s"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return ev.SparseIndex(
            source_name=str(raw.get("source_name") or name),
            source_bytes=int(raw["source_bytes"]),
            entries=tuple(entries),
            stride=int(raw.get("stride") or ev.SPARSE_INDEX_STRIDE_BYTES),
            schema=int(raw.get("schema") or 0),
            source_sha256=(
                None if raw.get("source_sha256") is None else str(raw["source_sha256"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def usable_index(
    root: Path | str,
    name: str,
    *,
    source_bytes: int,
    source_sha256: str | None = None,
) -> tuple[ev.SparseIndex | None, str]:
    """拿一份**能用在当前这份源上**的索引；拿不到就说明为什么。

    返回的第二项是要给页面看的**理由**，三态分得清：
    `"ok"` / `"missing"`（这份证据还没建过索引）/ `"stale"`（建过但对不上，要重建）。
    """
    index = load_index(root, name)
    if index is None:
        return None, "missing"
    if ev.sparse_index_usable(
        index, source_bytes=int(source_bytes), source_sha256=source_sha256
    ):
        return index, "ok"
    return None, "stale"


def ensure_index(
    root: Path | str,
    name: str,
    path: Path | str,
    *,
    stride: int = ev.SPARSE_INDEX_STRIDE_BYTES,
    digest: str | None = None,
    time_probe: TimeProbe | None = None,
) -> tuple[ev.SparseIndex, str]:
    """要用索引的地方的统一入口：**能用就用，不能用就（重建）交一份新的**。

    返回 `(索引, 来源)`，来源是 `"ok"` / `"built"` / `"rebuilt"` —— 页面据此说
    "用的是已登记的索引" 还是 "刚建好 / 重建过"（不匹配就重建，绝不硬用一个对不上的）。
    """
    target = Path(path)
    size = target.stat().st_size
    index, reason = usable_index(
        root, name, source_bytes=size, source_sha256=digest
    )
    if index is not None:
        return index, "ok"
    fresh = build_index(
        target, name, stride=stride, digest=digest, time_probe=time_probe
    )
    save_index(root, fresh)
    return fresh, ("rebuilt" if reason == "stale" else "built")


def line_at(index: ev.SparseIndex, path: Path | str, offset: int) -> int | None:
    """某个字节偏移在第几行（用锚点 + 从锚点顺数到那儿；读得有界）。"""
    anchor = index.entry_before(int(offset))
    if anchor is None:
        return None
    start = int(anchor.byte_offset)
    span = int(offset) - start
    if span < 0:
        return None
    newlines = 0
    for chunk in _read_forward(path, start, span):
        newlines += chunk.count(b"\n")
    return int(anchor.line_no) + newlines


#: 进程内的行号锚点缓存（**不落盘**）：键是 `(绝对路径, 大小, mtime_ns)`。
#: 见 `line_at_offset()` 的注释 —— 它与 `ensure_index()` 的分工写在那一处。
LINE_CACHE_MAX = 8
LINE_CACHE: dict[tuple[str, int, int], ev.SparseIndex] = {}


def _anchors_for(
    path: Path | str, *, cache_max: int = LINE_CACHE_MAX
) -> tuple[ev.SparseIndex, Path] | None:
    """内存锚点（**不落盘**）：`line_at_offset()` / `offset_at_line()` 共用这一处。

    为什么不落盘：窗口装载是**读请求**，顺手在证据目录里建 `index/` 等于把“我建过索引”
    写进别人的目录（run 目录凭空多一个 `index/`，别的判据还可能把它当成“多出来的文件”）。
    锚点只留在内存（每 1 MiB 一个）；键带上大小与 mtime，文件变了（实时会话会长）就重新数。
    """
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return None
    if stat.st_size <= 0:
        return None
    key = (str(target.absolute()), int(stat.st_size), int(stat.st_mtime_ns))
    fresh = LINE_CACHE.get(key)
    if fresh is None:
        fresh = build_index(target, target.name)
        LINE_CACHE[key] = fresh
        # 有界：FIFO 就好（够用且行为可预测）。先被清掉的是最早插入的那个，
        # 而“同一份路径的旧大小/旧 mtime”这类键本来就已经过期，先走正合适。
        while len(LINE_CACHE) > max(1, int(cache_max)):
            LINE_CACHE.pop(next(iter(LINE_CACHE)))
    return fresh, target


def line_at_offset(
    path: Path | str, offset: int, *, cache_max: int = LINE_CACHE_MAX
) -> int | None:
    """某个字节偏移在这份文件里是第几行（1 基）；算不出来就返回 `None`。

    用途只有一个：**窗口装载时告诉页面"你这一段的第一行是文件第几行"**（）。
    在那之前没人告诉它，于是面板左边那列显示的是"累计裁剪行数" —— 跟搜索结果里的
    文件真行号对不上，看起来就像"点了一条却跳到了别处"。

    ⚠️ 与 `ensure_index()` 的分工：这里**不落盘**（理由见 `_anchors_for()`）。
    """
    found = _anchors_for(path, cache_max=cache_max)
    if found is None:
        return None
    anchors, target = found
    return line_at(anchors, target, int(offset))


def offset_at_line(
    path: Path | str, line_no: int, *, cache_max: int = LINE_CACHE_MAX
) -> int | None:
    """第 `line_no` 行的**行首字节偏移**（1 基）；算不出来就 `None`。

    与 `line_at_offset()` 是一对反函数，共用同一份**内存**锚点（同样不落盘）。
    用途：按**行区间**要窗口（T6 阶段定位 —— 证据引用给的是行号，窗口装载要的是字节）。
    拿不到就 `None`：不知道就说不知道，不拿一个估值去充当“找到了”。
    """
    found = _anchors_for(path, cache_max=cache_max)
    if found is None:
        return None
    anchors, target = found
    return offset_for_line(anchors, target, int(line_no))


def offset_for_line(
    index: ev.SparseIndex, path: Path | str, line_no: int
) -> int | None:
    """第 `line_no` 行的**行首字节偏移**（拿最靠近且不越过的锚点顺数；读得有界）。"""
    wanted = int(line_no)
    if wanted < 1:
        return None
    best: ev.SparseIndexEntry | None = None
    for entry in index.entries:
        if int(entry.line_no) <= wanted and (
            best is None or int(entry.line_no) > int(best.line_no)
        ):
            best = entry
    if best is None:
        return None
    start = int(best.byte_offset)
    remaining = wanted - int(best.line_no)
    if remaining == 0:
        return start
    absolute = start
    for chunk in _read_forward(path, start, LOCATE_READ_MAX_BYTES):
        cursor = 0
        while True:
            found = chunk.find(b"\n", cursor)
            if found < 0:
                absolute += len(chunk)
                break
            remaining -= 1
            if remaining == 0:
                return absolute + found + 1
            cursor = found + 1
    # 往前读了上限那么多还没到那一行：老实说找不到，而不是把整份证据读下去。
    return None


def _read_forward(path: Path | str, start: int, span: int) -> list[bytes]:
    """从 `start` 往前读 `span` 字节，每次最多 `LOCATE_READ_CHUNK_BYTES`（有界）。"""
    out: list[bytes] = []
    left = min(int(span), LOCATE_READ_MAX_BYTES)
    with open(path, "rb") as handle:
        handle.seek(int(start))
        while left > 0:
            chunk = handle.read(min(LOCATE_READ_CHUNK_BYTES, left))
            if not chunk:
                break
            out.append(chunk)
            left -= len(chunk)
    return out


def describe(
    index: ev.SparseIndex | None,
    *,
    name: str,
    source_bytes: int,
    reason: str,
    source: str | None = None,
) -> dict[str, object]:
    """给页面看的**一句话事实**（"有没有索引"必须说得清，不能靠猜）。"""
    rows: dict[str, object] = {
        "name": str(name),
        "source_bytes": int(source_bytes),
        "reason": reason,
        "state": _STATE_TEXT.get(reason, reason),
        "schema": None if index is None else int(index.schema),
        "stride": None if index is None else int(index.stride),
        "anchors": None if index is None else len(index.entries),
        "fingerprint": None
        if index is None
        else ("sha256" if index.source_sha256 else "size"),
        "time": "未记录（这份证据没有可信的逐行时间来源）"
        if index is None or all(entry.t_s is None for entry in index.entries)
        else "有",
        "source": source,
    }
    return rows


_STATE_TEXT = {
    "ok": "已建立索引",
    "missing": "未建立索引（可退化顺扫）",
    "stale": "索引与当前文件对不上（已拒绝，正在重建）",
    "built": "本次刚建立索引",
    "rebuilt": "原索引对不上，已重建",
}

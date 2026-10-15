""" / L4：全文件搜索的**有界内存分块扫描**。

三件事：

1. **一块一块地扫，不整读**。块序来自 L0 冻结的 `search_block_ranges()`：相邻块重叠
   `len(模式) - 1` 字节 —— 这不是优化，是**正确性**。一个词骑在块边界上时，两块各自
   只看见一半、谁都不报，而"漏检"看起来和"确实没有"一模一样。整份证据从头到尾
   只按块读一遍，任一时刻手上只有一块（加一小段重叠）。
2. **进度 / 取消 / 超时 / 结果上限**都在扫描循环里判，不是扫完再补一句。上限到了就停，
   `truncated=True`，并且 `scanned_bytes < total_bytes` —— **进度不许假装扫完了**。
3. **行号是数出来的，不是猜的**。顺序扫描顺手数字节里的换行；重叠区里已经数过的字节
   要扣掉，否则每过一块行号就偏大一次。命中的上下文是**它所在的那一行**（按上限截断）。

`size` 一律取任务开始那一刻的**快照边界**（L0 的 `Snapshot`）：正在写的文件在扫的
过程中还会变长，扫描范围不跟着漂。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Callable, Iterator

try:  # 作为包导入（`webconsole.large_search`）
    from . import large_evidence as ev
except ImportError:  # 与其它 webconsole 模块并排执行
    import large_evidence as ev  # type: ignore[no-redef]


#: 进度回调：每扫完一块叫一次，接一个**不可变**快照（回调不许改内部状态）。
ProgressHook = Callable[["ScanProgress"], None]


@dataclass(frozen=True)
class ScanProgress:
    """扫描进度的一次快照（冻结：回调拿到的这一份不会被后面改写）。"""

    scanned_bytes: int
    total_bytes: int
    hits: int
    elapsed_ms: float

    @property
    def progress(self) -> float:
        total = int(self.total_bytes)
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, int(self.scanned_bytes) / total))

    def to_json(self) -> dict[str, object]:
        return {
            "scanned_bytes": int(self.scanned_bytes),
            "total_bytes": int(self.total_bytes),
            "hits": int(self.hits),
            "progress": round(self.progress, 4),
            "elapsed_ms": round(float(self.elapsed_ms), 3),
        }


class CancelToken:
    """取消旗子（可跨线程置位；扫描循环每块看一眼）。"""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()


def iter_blocks(
    path: Path | str, ranges: Iterator[ev.ByteRange] | list[ev.ByteRange]
) -> Iterator[tuple[ev.ByteRange, bytes]]:
    """**本模块唯一的读法**：打开一次文件，按区间逐块 `seek` + `read`。

    每块只读它那一小段（最多一块 + 重叠）。没有一处 `read_bytes()` ——
    400 MB 的证据不会因为一次搜索整份进内存。
    """
    with open(path, "rb") as handle:
        for rng in ranges:
            length = int(rng.end) - int(rng.start) + 1
            if length <= 0:
                continue
            handle.seek(int(rng.start))
            data = handle.read(length)
            if not data:
                return
            yield rng, data


def context_text(data: bytes, found: int, length: int, *, limit: int) -> str:
    """命中上下文 = 它所在的**那一行**（按 `limit` 个字符截断，两端加省略号）。

    行的边界就在这一块里找：块首那一行可能是**上一块的尾巴**，所以它的行首不在这块
    里（调用方会用 `at_block_start` 标出来，页面据此说明"上下文可能不完整"）。
    """
    line_start = data.rfind(b"\n", 0, found) + 1
    line_end = data.find(b"\n", found)
    if line_end < 0:
        line_end = len(data)
    fragment = data[line_start:line_end].decode("utf-8", "replace").rstrip("\r")
    cap = int(limit)
    if cap <= 0 or len(fragment) <= cap:
        return fragment
    match_at = len(data[line_start:found].decode("utf-8", "replace"))
    room = max(0, cap - int(length))
    start = max(0, match_at - room // 2)
    clipped = fragment[start:start + cap]
    return ("…" if start > 0 else "") + clipped + (
        "…" if start + cap < len(fragment) else ""
    )


def search_file(
    path: Path | str,
    task: ev.SearchTask,
    *,
    token: CancelToken | None = None,
    on_progress: ProgressHook | None = None,
    clock: Callable[[], float] = time.monotonic,
    block: int = ev.SEARCH_BLOCK_BYTES,
    snippet: int = ev.SEARCH_SNIPPET_MAX_BYTES,
) -> ev.SearchOutcome:
    """把一份证据按块扫一遍，交出 `SearchOutcome`（**不抛异常**，除非查询本身就非法）。

    非法查询（空、超长、没登记的模式）在 `compile_pattern()` 里就被拒 —— 那是调用方的
    输入错误，应该在起任务之前就退回 400，而不是变成一条"搜索失败"的结果。
    """
    pattern = ev.compile_pattern(task.mode, task.query)
    if not pattern:
        raise ev.EvidenceError(ev.ERROR_QUERY_INVALID, "查询展开后是空的")
    total = max(0, int(task.size))
    started = clock()
    overlap = ev.overlap_for_pattern(pattern)
    hits: list[ev.SearchHit] = []
    outcome = ev.SearchOutcome(
        task_id=task.task_id, state="running", scanned_bytes=0, total_bytes=total
    )
    counted_upto = 0  # 已经数过换行的字节边界
    lines_seen = 0  # [0, counted_upto) 里的换行数

    for rng, data in iter_blocks(path, ev.search_block_ranges(total, overlap=overlap, block=block)):
        if token is not None and token.cancelled:
            outcome.state = "cancelled"
            break
        if clock() - started > float(task.timeout_s):
            outcome.state = "timeout"
            break
        # 这一块里**还没数过**的起点：前 `already` 个字节是上一块已经数过的重叠区。
        # `lines_before` 要的是"本块开头之前一共有多少换行"，所以把重叠区那一段减掉。
        already = max(0, counted_upto - int(rng.start))
        lines_before = lines_seen - data[:already].count(b"\n")

        position = 0
        while True:
            found = data.find(pattern, position)
            if found < 0:
                break
            hits.append(
                ev.SearchHit(
                    offset=int(rng.start) + found,
                    length=len(pattern),
                    text=context_text(data, found, len(pattern), limit=snippet),
                    line_no=lines_before + 1 + data[:found].count(b"\n"),
                    # 命中落在"本块从上一块接过来"的那段里 ⇒ 它所在行的行首在块外，
                    # 上下文可能不完整。标出来，不假装完整。
                    at_block_start=bool(int(rng.start) > 0 and found < already),
                )
            )
            if len(hits) >= int(task.max_results):
                outcome.truncated = True
                break
            position = found + len(pattern)

        # ⚠️ 这里数的是**整块**（不是 `data[already:]`）。已经数过的那一段在下一块里会
        # 被 `lines_before` 减掉 —— 两边都只算一半，就会把重叠区里的那个换行丢掉：
        # 真机上表现成"行号每隔几块就少一"，而且越走越偏（本仓库实测过）。
        lines_seen = lines_before + data.count(b"\n")
        # ⚠️ `counted_upto` 是"**真正读到**的字节数"，不是"计划读到哪"：文件比计划短的
        # 时候（正在写的文件被人截断），按计划写就会得出一份"扫了 4236 B"的报告，
        # 而实际上只读到 140 B —— 那就是在报假账。
        counted_upto = int(rng.start) + len(data)
        outcome.hits = hits
        outcome.scanned_bytes = counted_upto
        outcome.elapsed_ms = (clock() - started) * 1000.0
        if on_progress is not None:
            on_progress(
                ScanProgress(counted_upto, total, len(hits), outcome.elapsed_ms)
            )
        if outcome.truncated:
            break

    if outcome.state == "running":
        outcome.state = "done"
    outcome.hits = hits
    outcome.elapsed_ms = (clock() - started) * 1000.0
    if outcome.state == "done" and counted_upto < total:
        # 扫到一半文件变短了（或被人截断）。**如实说**，不当成"扫完了没命中"。
        outcome.error = f"文件在扫描期间变短：只扫到 {counted_upto} B，计划 {total} B"
    return outcome

""" / L4：**搜索任务的登记处** —— 后台跑、进度可查、能取消。

为什么要一个登记处，而不是"一次请求扫到底"：

- 400 MB 的一份证据，一趟扫描不是一次请求可以静静等完的事（需求 PERF-SEARCH-02 要
  **进度与取消**）。同步请求下这两个都没有 —— 只能等。
- 搜索是**只读**的，可以放心在后台线程里跑；但它会吃磁盘，所以同一份证据同时只许
  一个搜索（否则两次全扫互相拖慢，看起来像"服务端很慢"）。

三件事各归各的：`large_search` 负责扫、`large_index` 负责跳转用的索引、这里只负责
"谁在扫、扫到哪了、要不要停"。**它不知道 run/session 是什么** —— 证据怎么解析由装配处
交进来的 `resolve()` 决定（白名单在那边，不在这里再写一份）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable

try:  # 作为包导入（`webconsole.search_center`）
    from . import large_evidence as ev
    from .large_search import CancelToken, ScanProgress, search_file
except ImportError:  # 与其它 webconsole 模块并排执行
    import large_evidence as ev  # type: ignore[no-redef]
    from large_search import CancelToken, ScanProgress, search_file  # type: ignore[no-redef]


#: 已经跑完的任务在这里最多留几条（有界：搜索一次不改天，但别无限留在内存里）。
MAX_KEPT_TASKS = 8


@dataclass(frozen=True)
class SearchTarget:
    """要搜的那份证据**已经解析好**的样子（路径与索引根都由装配处给）。"""

    entity_id: str
    kind: str
    evidence: str
    path: Path
    root: Path

    def to_json(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "evidence": self.evidence,
        }


@dataclass
class _Task:
    """一次搜索的全部现场（可变：它在跑）。"""

    target: SearchTarget
    task: ev.SearchTask
    token: CancelToken
    outcome: ev.SearchOutcome
    progress: ScanProgress | None = None
    error: str | None = None
    thread: threading.Thread | None = None
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, snapshot: ScanProgress) -> None:
        with self.lock:
            self.progress = snapshot

    def finish(self, outcome: ev.SearchOutcome) -> None:
        with self.lock:
            self.outcome = outcome
            self.finished = True
            total = max(0, int(outcome.total_bytes))
            self.progress = ScanProgress(
                outcome.scanned_bytes,
                total,
                len(outcome.hits),
                outcome.elapsed_ms,
            )

    def fail(self, message: str) -> None:
        with self.lock:
            self.error = str(message)
            self.outcome = ev.SearchOutcome(
                task_id=self.task.task_id,
                state="error",
                scanned_bytes=int(self.outcome.scanned_bytes),
                total_bytes=int(self.outcome.total_bytes),
                error=str(message),
            )
            self.finished = True

    def to_json(self) -> dict[str, Any]:
        with self.lock:
            payload: dict[str, Any] = {
                "task": self.task.to_json(),
                "target": self.target.to_json(),
                "state": self.outcome.state,
                "finished": bool(self.finished),
                "truncated": bool(self.outcome.truncated),
                "elapsed_ms": round(float(self.outcome.elapsed_ms), 3),
                "error": self.error or self.outcome.error,
                "progress": None if self.progress is None else self.progress.to_json(),
                "hits": [hit.to_json() for hit in self.outcome.hits],
            }
            return payload


class SearchCenter:
    """进程内的搜索任务登记处（`start` / `outcome` / `cancel` / `snapshot`）。"""

    def __init__(
        self,
        resolve: Callable[[dict[str, Any]], SearchTarget | None],
        *,
        max_tasks: int = MAX_KEPT_TASKS,
        block: int = ev.SEARCH_BLOCK_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolve = resolve
        self._max_tasks = max(1, int(max_tasks))
        self._block = max(1, int(block))
        self._clock = clock
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._order: list[str] = []

    # --------------------------------------------------------------- 对外接口
    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        """起一个搜索任务。**输入先全部验一遍**，不合格在起线程之前就退回。"""
        target = self._resolve(dict(request or {}))
        if target is None:
            raise ev.EvidenceError(ev.ERROR_ARTIFACT_UNKNOWN, "找不到这份证据")
        mode = str(request.get("mode") or ev.SEARCH_MODE_TEXT)
        query = str(request.get("query") or "")
        # 查询能不能编译、模式认不认识：在这里就定论（400），不是让它变成一条"搜索失败"。
        ev.compile_pattern(mode, query)
        max_results = int(request.get("max_results") or ev.SEARCH_MAX_RESULTS_DEFAULT)
        timeout_s = float(request.get("timeout_s") or ev.SEARCH_TIMEOUT_S)
        size = ev.snapshot_size(target.path).size  # 开始那一刻的**快照边界**
        task = ev.SearchTask(
            task_id=self._new_task_id(),
            evidence=target.evidence,
            mode=mode,
            query=query,
            size=size,
            max_results=max_results,
            timeout_s=timeout_s,
        )
        row = _Task(
            target=target,
            task=task,
            token=CancelToken(),
            outcome=ev.SearchOutcome(
                task_id=task.task_id, state="running", scanned_bytes=0, total_bytes=size
            ),
        )
        with self._lock:
            self._refuse_busy(target)
            self._tasks[task.task_id] = row
            self._order.append(task.task_id)
            # 先把新任务放进去再淘汰：否则留下的是 max_tasks + 1 条。
            self._evict()
        thread = threading.Thread(
            target=self._run, args=(row,), name=f"search-{task.task_id}", daemon=True
        )
        row.thread = thread
        thread.start()
        return row.to_json()

    def outcome(self, task_id: str) -> dict[str, Any]:
        row = self._tasks.get(str(task_id))
        if row is None:
            raise KeyError(str(task_id))
        return row.to_json()

    def cancel(self, task_id: str) -> bool:
        """取消：**只置旗子**，不 join —— 扫描循环每块看一眼，自己停下来。"""
        row = self._tasks.get(str(task_id))
        if row is None:
            raise KeyError(str(task_id))
        if row.finished:
            return False
        row.token.cancel()
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [self._tasks[key].to_json() for key in self._order if key in self._tasks]
        return {"tasks": rows, "running": sum(1 for row in rows if not row["finished"])}

    # ------------------------------------------------------------------ 内部
    def _run(self, row: _Task) -> None:
        try:
            outcome = search_file(
                row.target.path,
                row.task,
                token=row.token,
                on_progress=row.note,
                clock=self._clock,
                block=self._block,
            )
        except (ev.EvidenceError, OSError, ValueError) as error:
            row.fail(str(error))
            return
        row.finish(outcome)

    def _refuse_busy(self, target: SearchTarget) -> None:
        """同一份证据同时只许一个搜索（只读，但很吃磁盘 —— 排队的代价更大）。"""
        for key in self._order:
            other = self._tasks.get(key)
            if other is None or other.finished:
                continue
            same_file = other.target.path == target.path
            if same_file:
                raise ev.EvidenceError(
                    ev.ERROR_SEARCH_BUSY,
                    f"{target.evidence} 已经有一趟搜索在跑（{other.task.task_id}）；"
                    "先等它跑完或取消它",
                )

    def _evict(self) -> None:
        while len(self._order) > self._max_tasks:
            key = self._order[0]
            row = self._tasks.get(key)
            if row is not None and not row.finished:
                break  # 还在跑的不许丢：宁可多留一条
            self._order.pop(0)
            self._tasks.pop(key, None)

    def _new_task_id(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return f"search-{stamp}-{uuid.uuid4().hex[:6]}"

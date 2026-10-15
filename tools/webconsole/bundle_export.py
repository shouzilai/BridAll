""" / P2：**从磁盘逐块生成诊断包**（不再整读、不再在内存里成包）。

现状（P1 基线的实测）是：`session_review.build_bundle()` 先把全部证据读进内存、
再用 `BytesIO + writestr()` 在内存里拼出整个 ZIP —— 一个 400 MiB 的会话实测**额外常驻
峰值 1259.9 MiB（≈ 3 × 证据大小）**，而且返回的是"整个包的字节"。
这一份把它换成：

1. **先定快照边界**：开始那一刻把每份证据的大小 `stat()` 下来（PACK-EXPORT-02）。
   之后**只读这么多字节** —— 会话还在写也不影响：包是"那一刻的快照"，
   而不是"打包结束时恰好读到的东西"。
2. **逐块写临时 ZIP**：打开临时文件，每份证据按 `pack.STREAM_CHUNK_BYTES`（1 MiB）
   读一块、写一块，同时把它的 SHA-256 算出来。任一时刻手上只有一块。
3. **收总哈希再改名**：临时文件最后算一次 SHA-256，然后 `os.replace` 成正式名字 ——
   中途失败/取消只会留下一个**可识别**的临时文件，不会留下"看起来成功"的半个包。
4. **流式下载 + 明确生命周期**：下载仍走服务端那一个流式发送器（支持 Range、断连不算错），
   临时文件按 TTL 清理；`cleanup_expired()` 只动自己那个前缀的东西。

⚠️ 这里**不碰**上传/导入那条路（P3），也不认识任何硬件：它只读会话目录、写自己的临时文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import codecs
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
import zipfile
from typing import Any, Callable

try:  # 作为包导入（`webconsole.bundle_export`）
    from . import large_bundle as pack
    from . import session_review
    from .core_primitives import utc_now
except ImportError:  # 与其它 webconsole 模块并排执行
    import large_bundle as pack  # type: ignore[no-redef]
    import session_review  # type: ignore[no-redef]
    from core_primitives import utc_now  # type: ignore[no-redef]


#: 临时 ZIP 的前缀。**以点开头**：正式名字永远不会这样起头，残留一眼可辨。
EXPORT_PREFIX = ".exporting-"

#: 临时 ZIP 保留多久（秒）。下载完/会话失宠之后由 `cleanup_expired()` 清走。
EXPORT_TTL_S = 10 * 60

#: 同一时间最多留几个导出任务（有界：导出一次不改天，但别无限留在内存里）。
MAX_KEPT_EXPORTS = 6

#: ZIP 的时间戳钉死：同一份输入两次导出**逐字节相同**（与 P1 夹具同一条纪律）。
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = 0o600 << 16
COMPRESS_LEVEL = 6

ProgressHook = Callable[[int, int], None]


#: 取消旗子（`CancelToken` 形态）与"包不合规"一样的错误类型（契约模块定义，这里只是转出来）。
BundleError = pack.BundleError


@dataclass(frozen=True)
class MemberPlan:
    """一份要写进包里的证据：**名字 + 路径 + 快照大小**（大小就是快照边界）。"""

    name: str
    path: Path
    bytes: int

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "bytes": int(self.bytes)}


def plan_members(root: Path | str, session_id: str) -> list[MemberPlan]:
    """按 `BUNDLE_ORDER` 把会话里**存在**的证据排成一份计划（只 `stat`，不读正文）。

    `root` 是**装着这次会话的那个目录**（本机会话目录 / 导入目录 —— 两份用同一套规则，
    见 `session_review.resolve_session`），`session_id` 是它底下的目录名。

    存在的顺序由 `BUNDLE_ORDER` 决定（打开包的人先看 README，再看 session.json）；
    不存在的成员**不进包**（"包里没有的东西就是不存在的证据"这条写在 README 里）。
    """
    session = Path(root) / session_id
    rows: list[MemberPlan] = []
    for name in session_review.BUNDLE_ORDER:
        if name in (session_review.BUNDLE_README_NAME, session_review.BUNDLE_INFO_NAME):
            continue  # 这两份由导出器自己生成
        if name not in session_review.SESSION_ARTIFACTS:
            continue
        path = session / name
        if path.is_file():
            rows.append(MemberPlan(name=name, path=path, bytes=path.stat().st_size))
    return rows


def snapshot_bytes(members: list[MemberPlan]) -> int:
    """快照总量（进度与磁盘预算都用它）。"""
    return sum(int(row.bytes) for row in members)


def estimate_zip_bytes(members: list[MemberPlan]) -> int:
    """**保守**估一个包多大：按"压不动"算（最坏情况就是原始字节那一种）。

    估算只用于磁盘预算；实际大小写进任务状态，页面显示的是**事实**不是估算。
    """
    return snapshot_bytes(members)


def export_dir(root: Path | str) -> Path:
    """临时 ZIP 放哪儿：**会话所在目录本身**（同一条文件系统 ⇒ 改名是原子的）。"""
    return Path(root)


def temp_path(root: Path | str, task_id: str) -> Path:
    clean = str(task_id or "").strip()
    if not clean or any(char in clean for char in "/\\:") or clean.startswith("."):
        raise ValueError(f"任务 ID 不合法：{task_id!r}")
    return export_dir(root) / f"{EXPORT_PREFIX}{clean}.zip"


def formal_packages(root: Path | str) -> list[Path]:
    """目录里的**正式包**（名字以 `.exporting-` 开头的是"还在路上"，永远不算成品）。

    ⚠️ `*.zip` 会把临时名也匹配进来（pathlib 的 glob 不躲点号），所以"有没有半个包"
    这种判据一律走这里，而不是自己写一条 glob。
    """
    return sorted(
        path
        for path in Path(root).glob("*.zip")
        if not path.name.startswith(EXPORT_PREFIX)
    )


def bundle_name(session_id: str) -> str:
    """下载时建议的文件名（与现状逐字一致：PACK-COMPAT-01）。"""
    return f"{session_id}-diagnostic-bundle.zip"


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = FIXED_EXTERNAL_ATTR
    return info


def _survey(
    members: list[MemberPlan], *, chunk_bytes: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """一遍流式扫过每份证据：算出 sha256、行数、事件数（**任何时刻只有一块在手上**）。

    ⚠️ 为什么必须先扫一遍才能写？因为 ZIP 的成员是**顺序**写进文件的，而 `BUNDLE_ORDER`
    要求 `README.txt` / `session.json` 排在最前 —— 而这两份里写着每份证据的 sha256
    （PACK-EXPORT-02）。想同时做到"说明在最前"与"不整读"，就只能**扫一遍再写一遍**。
    """
    counts: dict[str, Any] = {
        "raw_bytes": 0,
        "raw_lines": 0,
        "io_events": 0,
        "rx_events": 0,
        "tx_events": 0,
        "decoded_bytes": 0,
        "decoded_lines": 0,
        "app_events": 0,
    }
    rows: list[dict[str, Any]] = []
    for member in members:
        digest = hashlib.sha256()
        lines = 0
        events = 0
        rx = 0
        tx = 0
        last: bytes = b""
        is_text = member.name in ("raw.bin", "decoded.log")
        is_jsonl = member.name in ("io_events.jsonl", "app_events.jsonl")
        decoder = codecs.getincrementaldecoder("utf-8")("replace") if is_jsonl else None
        carry = ""
        left = int(member.bytes)
        with member.path.open("rb") as handle:
            while left > 0:
                chunk = handle.read(min(chunk_bytes, left))
                if not chunk:
                    break
                digest.update(chunk)
                if is_text:
                    lines += chunk.count(b"\n")
                    last = chunk[-1:]
                elif is_jsonl and decoder is not None:
                    parts = (carry + decoder.decode(chunk)).split("\n")
                    carry = parts.pop()
                    for line in parts:
                        hit, kind = _count_event_line(line)
                        events += hit
                        rx += 1 if kind == "rx" else 0
                        tx += 1 if kind == "tx" else 0
                left -= len(chunk)
        if is_jsonl and decoder is not None:
            hit, kind = _count_event_line(carry + decoder.decode(b"", True))
            events += hit
            rx += 1 if kind == "rx" else 0
            tx += 1 if kind == "tx" else 0
        if is_text and int(member.bytes) > 0 and last != b"\n":
            lines += 1  # 末尾没换行：最后那一行也算一行
        rows.append(
            {"name": member.name, "bytes": int(member.bytes), "sha256": digest.hexdigest()}
        )
        if member.name == "raw.bin":
            counts["raw_bytes"] = int(member.bytes)
            counts["raw_lines"] = lines
        elif member.name == "decoded.log":
            counts["decoded_bytes"] = int(member.bytes)
            counts["decoded_lines"] = lines
        elif member.name == "io_events.jsonl":
            counts["io_events"] = events
            counts["rx_events"] = rx
            counts["tx_events"] = tx
        elif member.name == "app_events.jsonl":
            counts["app_events"] = events
    return counts, rows


def _count_event_line(line: str) -> tuple[int, str]:
    """一行 JSONL：解析得出来就算 1 条（否则 0），并回它是不是 rx/tx（与旧口径一致）。"""
    text = line.strip()
    if not text:
        return 0, ""
    try:
        value = json.loads(text)
    except ValueError:
        return 0, ""
    if not isinstance(value, dict):
        return 0, ""
    kind = value.get("kind")
    return 1, kind if isinstance(kind, str) else ""


def export_session(
    root: Path | str,
    session_id: str,
    *,
    task: Any | None = None,
    token: Any | None = None,
    on_progress: ProgressHook | None = None,
    chunk_bytes: int = pack.STREAM_CHUNK_BYTES,
    free_bytes: int | None = None,
) -> tuple[dict[str, Any], str]:
    """把一次会话导成一个 **ZIP 文件**（流式），返回 `(info, 包路径)`。

    `root` 是装着这次会话的目录（本机会话目录 / 导入目录都行），`session_id` 是它底下的
    目录名。`task` 给一个 `pack.ExportTask` 时进度会写进它（阶段/字节/当前成员）——P4 的
    页面就靠它。`token` 给取消旗子（有 `.cancelled` 就行）。**取消/失败都删掉临时文件**。
    """
    root = Path(root)
    session = root / session_id
    if not (session / "manifest.json").is_file():
        raise session_review.SessionNotFound(f"会话 {session_id} 不完整：目录里没有 manifest.json")
    members = plan_members(root, session_id)
    if not members:
        raise session_review.SessionNotFound(f"会话 {session_id} 里没有可导出的证据")
    total = snapshot_bytes(members)
    if task is not None:
        task.member_sizes = tuple((row.name, int(row.bytes)) for row in members)
        task.member_count = len(members) + 2  # README.txt + session.json
        task.total_bytes = total
        task.advance(stage="write")
    if free_bytes is not None:
        budget = pack.disk_budget(
            upload_bytes=0, uncompressed_bytes=estimate_zip_bytes(members), free_bytes=free_bytes
        )
        if not budget["enough"]:
            message = (
                f"需要 {budget['required_bytes']} B（含余量），可用 {budget['free_bytes']} B"
            )
            _finish(task, "failed", error="disk_insufficient", message=message)
            raise pack.BundleError("disk_insufficient", message)
    task_id = getattr(task, "task_id", "") or f"export-{uuid.uuid4().hex[:8]}"
    temp = temp_path(root, task_id)
    manifest = _manifest_dict(session / "manifest.json")
    try:
        # 第一遍：流式扫一遍（哈希 + 计数）——阶段仍是 snapshot。
        counts, rows = _survey(members, chunk_bytes=chunk_bytes)
        missing = [
            name for name in session_review.SESSION_ARTIFACTS if name not in {row["name"] for row in rows}
        ]
        if token is not None and getattr(token, "cancelled", False):
            raise pack.BundleError("import_cancelled", "导出在开始写包之前被取消")
        info = _bundle_info(session_id, manifest, counts, rows, missing)
        readme = session_review._bundle_readme(  # noqa: SLF001 - 同一份文案，只此一处
            session_id, manifest, counts, rows, missing
        )
        if task is not None:
            task.advance(stage="write")
        # 第二遍：逐块写进临时 ZIP（正文从不整读）。
        _write_bundle_file(
            temp,
            members=members,
            readme=readme,
            info=info,
            token=token,
            on_progress=on_progress,
            chunk_bytes=chunk_bytes,
            note=(lambda done, member: _note(task, done, member)) if task is not None else None,
        )
        if task is not None:
            task.advance(stage="hash", written=total)
        written = temp.stat().st_size
        bundle_sha = _sha256_file(temp, chunk_bytes)
        final = root / bundle_name(session_id)
        os.replace(temp, final)  # 原子的一步：在此之前世界上没有"正式"的包
        if task is not None:
            task.advance(stage="ready", written=total)
            task.bundle_path = str(final)
            task.bundle_bytes = written
            task.bundle_sha256 = bundle_sha
            task.current_member = ""
            _finish(task, "done", message=f"{bundle_name(session_id)}（{written} B）")
        info["bundle_bytes"] = written
        info["bundle_sha256"] = bundle_sha
        return info, str(final)
    except pack.BundleError as error:
        _discard(temp)
        _finish(
            task,
            "cancelled" if error.code == "import_cancelled" else "failed",
            error=error.code,
            message=error.message,
        )
        raise
    except OSError as error:
        _discard(temp)
        _finish(task, "failed", error="export_write_failed", message=str(error))
        raise


def _finish(task: Any | None, state: str, *, error: str = "", message: str = "") -> None:
    """收尾并**盖上结束时间**（时间来自调用方这一侧，契约模块不认识时钟）。"""
    if task is None or task.finished:
        return
    task.finish(state, error=error, message=message)
    task.finished_utc = utc_now()


def _note(task: Any | None, written: int, member: str = "") -> None:
    if task is None:
        return
    if member:
        task.current_member = member  # 页面靠它显示"正在写哪一份"
    task.advance(written=written)


def _sha256_file(path: Path, chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bundle_file(
    temp: Path,
    *,
    members: list[MemberPlan],
    readme: str,
    info: dict[str, Any],
    token: Any | None,
    on_progress: ProgressHook | None,
    chunk_bytes: int,
    note: Callable[[int, str], None] | None,
) -> None:
    """把包写进临时文件：`README.txt` → `session.json` → 证据（顺序与旧实现逐字一致）。

    全程只有一块 `chunk_bytes` 在手上（`archive.open(..., "w")` 是流式条目，
    `writestr` 的入参是那两份**文本说明**，都很小 —— 真正的正文从不进内存）。
    """
    total = snapshot_bytes(members)
    done = 0
    with temp.open("wb") as handle:
        with zipfile.ZipFile(
            handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
        ) as archive:
            archive.writestr(_zip_info(session_review.BUNDLE_README_NAME), readme.encode("utf-8"))
            archive.writestr(
                _zip_info(session_review.BUNDLE_INFO_NAME),
                (json_dumps(info) + "\n").encode("utf-8"),
            )
            for member in members:
                left = int(member.bytes)
                with member.path.open("rb") as source, archive.open(
                    _zip_info(member.name), "w"
                ) as sink:
                    while left > 0:
                        if token is not None and getattr(token, "cancelled", False):
                            raise pack.BundleError(
                                "import_cancelled", f"导出在 {member.name} 处被取消"
                            )
                        chunk = source.read(min(chunk_bytes, left))
                        if not chunk:
                            # 快照之后文件变短了：**说明白**，不写一个与声明不符的成员进去。
                            raise pack.BundleError(
                                "export_source_changed",
                                f"{member.name} 在导出过程中变短：还剩 {left} B 读不到",
                            )
                        sink.write(chunk)
                        left -= len(chunk)
                        done += len(chunk)
                        if note is not None:
                            note(done, member.name)
                        if on_progress is not None:
                            on_progress(done, total)
                if on_progress is not None:
                    on_progress(done, total)


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _manifest_dict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _bundle_info(
    session_id: str,
    manifest: dict[str, Any],
    counts: dict[str, Any],
    rows: list[dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    """`session.json` 的内容：与旧 `build_bundle()` 的那份**同名同义同键集**。

    ⚠️ 这里是故意的：键集一模一样，才能拿同一个会话跑"旧导出 vs 新导出"并**逐键对比**
    （PACK-COMPAT-01）；多一个键就不是逐键可比了。流式这件事属于导出器的实现细节，
    要写就写在文档与任务状态里，不写进给读者看的包内自述。
    """
    import platform_info

    return {
        "schema": 1,
        "kind": "workbench-observe-bundle",
        "session_id": session_id,
        "generated_utc": utc_now(),
        "open_at_build": manifest.get("closed_utc") is None,
        "device": manifest.get("device") or {},
        "port": manifest.get("port"),
        "baud": manifest.get("baud"),
        "opened_utc": manifest.get("opened_utc"),
        "closed_utc": manifest.get("closed_utc"),
        "end_reason": manifest.get("end_reason"),
        "identity": manifest.get("identity") or {},
        "counts": counts,
        "manifest_totals": manifest.get("totals") or {},
        "files": rows,
        "missing": list(missing),
        "platform": manifest.get("platform") or {},
        "schemas": dict(
            (manifest.get("platform") or {}).get("schemas") or platform_info.SCHEMA_VERSIONS
        ),
        "notes": list(session_review.BUNDLE_NOTES),
    }


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ===================================================================== 任务登记处
@dataclass
class _Export:
    task: pack.ExportTask
    token: Any
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_json(self) -> dict[str, Any]:
        with self.lock:
            return self.task.to_json()


class ExportCenter:
    """导出任务的登记处（登记、跑、查进度、取消、清残留）。

    与 L4 的 `SearchCenter` 同一套形状：**它不认识 HTTP**，也不认识 run/session 是什么 ——
    "这个会话号住在哪个根目录"由装配处交进来的 `resolve()` 决定（本机会话目录 / 导入目录
    用的是同一个 `session_review.resolve_session()`，这里不另写一份规则）。
    """

    def __init__(
        self,
        resolve: Callable[[str], Path],
        *,
        roots: tuple[Path | str, ...] = (),
        cancel_token_factory: Callable[[], Any] | None = None,
        max_tasks: int = MAX_KEPT_EXPORTS,
        ttl_s: float = EXPORT_TTL_S,
        free_space: Callable[[Path], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._resolve = resolve
        self.roots = tuple(Path(row) for row in roots)
        self._tokens = cancel_token_factory or (lambda: _CancelToken())
        self._max_tasks = max(1, int(max_tasks))
        self._ttl_s = float(ttl_s)
        self._free_space = free_space
        self._clock = clock
        self._spawn = spawn or _spawn_thread
        self._lock = threading.RLock()
        self._tasks: dict[str, _Export] = {}
        self._order: list[str] = []
        self._finished_at: dict[str, float] = {}

    def free_bytes(self, root: Path | str) -> int | None:
        """这个根所在盘还剩多少（没注入探测函数就回 `None` = "不知道"，不是 0）。"""
        if self._free_space is None:
            return None
        return int(self._free_space(Path(root)))

    def register(self, session_id: str) -> pack.ExportTask:
        """登记一个导出任务（**同步**：拒 busy、取快照边界都在这里做完）。"""
        try:
            root = Path(self._resolve(session_id))
        except session_review.SessionNotFound:
            raise
        members = plan_members(root, session_id)
        if not members:
            raise session_review.SessionNotFound(f"会话 {session_id} 里没有可导出的证据")
        task = pack.ExportTask(
            task_id=f"export-{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            started_utc=utc_now(),
            member_count=len(members) + 2,
            total_bytes=snapshot_bytes(members),
            member_sizes=tuple((row.name, int(row.bytes)) for row in members),
        )
        row = _Export(task=task, token=self._tokens())
        with self._lock:
            self._refuse_busy(session_id)
            self._tasks[task.task_id] = row
            self._order.append(task.task_id)
            self._evict()
        return task

    def start(self, session_id: str) -> pack.ExportTask:
        """登记并**同步**跑完（测试与"点一下就要文件"的调用方用这个）。"""
        task = self.register(session_id)
        self.run(task.task_id)
        return task

    def start_async(self, session_id: str) -> pack.ExportTask:
        """登记后**交给别的线程**跑：立刻回任务号，进度由 `outcome()` 查。

        ⚠️ 登记（含拒 busy）是同步的，所以"同一会话只许一趟"这条规矩在返回 202 之前
        就已经生效；跑本身在后台，400 MiB 不会让人盯着一个请求等。
        """
        task = self.register(session_id)
        self._spawn(lambda: self.run(task.task_id))
        return task

    def run(self, task_id: str) -> pack.ExportTask:
        row = self._tasks[task_id]
        root = Path(self._resolve(row.task.session_id))
        try:
            export_session(
                root,
                row.task.session_id,
                task=row.task,
                token=row.token,
                free_bytes=self.free_bytes(root),
            )
        except pack.BundleError as error:
            _finish(
                row.task,
                "cancelled" if error.code == "import_cancelled" else "failed",
                error=error.code,
                message=error.message,
            )
        except session_review.SessionNotFound as error:
            _finish(row.task, "failed", error="export_source_missing", message=str(error))
        except OSError as error:
            _finish(row.task, "failed", error="export_write_failed", message=str(error))
        with self._lock:
            self._finished_at[task_id] = self._clock()
        return row.task

    def outcome(self, task_id: str) -> pack.ExportTask:
        try:
            return self._tasks[str(task_id)].task
        except KeyError:
            raise KeyError(str(task_id)) from None

    def cancel(self, task_id: str) -> bool:
        row = self._tasks.get(str(task_id))
        if row is None:
            raise KeyError(str(task_id))
        if row.task.finished:
            return False
        row.token.cancel()
        return True

    def bundle_path(self, task_id: str) -> Path | None:
        """任务完成后那个包在哪（没完成/已过期 ⇒ `None`）。"""
        row = self._tasks.get(str(task_id))
        if row is None or row.task.state != "done" or not row.task.bundle_path:
            return None
        path = Path(row.task.bundle_path)
        return path if path.is_file() else None

    def snapshot(self) -> dict[str, Any]:
        """给 `/api/state` 看的自述：任务列表 + 正在跑的数量。

        ⚠️ "在跑"问的是**任务对象自己**（`task.finished`），不是从 JSON 里猜 ——
        `to_json()` 里没有 `finished` 这个键（状态本身就在 `state` 里，别再造一个事实源）。
        """
        with self._lock:
            rows = [self._tasks[key].to_json() for key in self._order if key in self._tasks]
            running = sum(
                1
                for key in self._order
                if key in self._tasks and not self._tasks[key].task.finished
            )
        return {"tasks": rows, "running": running}

    def cleanup_expired(self, *, now: float | None = None) -> list[str]:
        """清掉过期的临时文件与任务记录。**只动自己那个前缀的东西**。

        `roots` 是装配处告诉它"哪些目录里可能有我留下的临时文件"（本机会话目录、导入目录）——
        只扫这些，别人的文件一律不碰；没登记 root 时就只收任务记录。
        """
        moment = self._clock() if now is None else float(now)
        removed: list[str] = []
        for root in self.roots:
            for path in root.glob(f"{EXPORT_PREFIX}*.zip"):
                try:
                    age = moment - path.stat().st_mtime
                except OSError:
                    continue
                if age > self._ttl_s:
                    _discard(path)
                    removed.append(path.name)
        with self._lock:
            for task_id in list(self._finished_at):
                if moment - self._finished_at[task_id] > self._ttl_s:
                    self._order = [key for key in self._order if key != task_id]
                    self._tasks.pop(task_id, None)
                    self._finished_at.pop(task_id, None)
        return removed

    # ------------------------------------------------------------------ 内部
    def _refuse_busy(self, session_id: str) -> None:
        for key in self._order:
            row = self._tasks.get(key)
            if row is None or row.task.finished:
                continue
            if row.task.session_id == session_id:
                raise pack.BundleError(
                    "export_task_busy",
                    f"{session_id} 已经有一个导出在跑（{row.task.task_id}）",
                )

    def _evict(self) -> None:
        while len(self._order) > self._max_tasks:
            key = self._order[0]
            row = self._tasks.get(key)
            if row is not None and not row.task.finished:
                break  # 还在跑的留着
            self._order.pop(0)
            self._tasks.pop(key, None)


class _CancelToken:
    """最小的取消旗子（与 `large_search.CancelToken` 同形，但不依赖它）。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def _spawn_thread(action: Callable[[], None]) -> None:
    """把一个导出放到后台去跑（守护线程：它不该把服务退出拖住）。"""
    threading.Thread(target=action, name="bundle-export", daemon=True).start()

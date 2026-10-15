""" / P3：**从磁盘流式导入一个诊断包**（不再把包体或成员读进内存）。

旧路（`bundle_import`，仍然保留）先把整个上传体 `read(length)` 进内存，再用
`archive.read(entry)` 把每个成员也整体物化一遍：包体、解压体、哈希三份驻留同时存在。
它还只收 KB 级包（32 / 32 / 64 MiB 三条防呆上限）。这一份把同一件事做成**流水线**：

1. **上传先落盘**（PACK-IMPORT-01）：请求体按固定块写进 `<imports>/.incoming-<task>.zip`，
   边写边数、边算整包 SHA-256；**不知道长度就拒**（不知道要收多少字节就无法先拦后读），
   超过 `MAX_UPLOAD_BYTES` 在读正文之前就拒。
2. **从磁盘预检 ZIP**（PACK-IMPORT-02）：中央目录、成员名、重复项、目录/符号链接/加密、
   声明大小、压缩率、总量、磁盘预算 —— **全部在读正文之前**算完（判定只有一处：
   P0 的 `pack.plan_import()`，这里只补旧导入就有的两条规矩：名字白名单、必须有的条目）。
3. **逐块解压 + 同时算哈希**（PACK-IMPORT-03）：每个成员按 `STREAM_CHUNK_BYTES` 解一块、
   写一块、哈希一块；任何时刻手上只有一块（**不用 `archive.read()`**）。
4. **核对包自己的声明**：`session.json` 里写的 sha256 与第 3 步算出来的逐个对照
   （消息文本与旧路径共用 `bundle_import.verify_declared_hashes`，小包提示因此不变）。
5. **原子发布**（PACK-IMPORT-04）：临时目录与正式目录**同一个父目录**，最后一步是
   `os.replace()` 改名；改名之前世界上没有这个导入会话。取消/失败/盘满/进程重启只会留下
   `.incoming-*` 残留 —— 它们不在会话清单里，而且分类与清理只许动这一类（`residue()`）。

它**不认识 HTTP**，也不认识任何目标/项目模块：字节从哪来（请求体、CLI 管道、测试里的
字节块）由调用方按块喂进来。阶段与错误码全部来自 P0 契约（`large_bundle`），这里不新造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import zipfile
from typing import Any, Callable, Iterable, Iterator

try:  # 作为包导入（`webconsole.bundle_stream_import`）
    from . import bundle_import
    from . import large_bundle as pack
    from .core_primitives import utc_now
    from .observe_store import IMPORT_RECORD_NAME, SESSION_ARTIFACTS, artifact_path
except ImportError:  # 与其它 webconsole 模块并排执行
    import bundle_import  # type: ignore[no-redef]
    import large_bundle as pack  # type: ignore[no-redef]
    from core_primitives import utc_now  # type: ignore[no-redef]
    from observe_store import (  # type: ignore[no-redef]
        IMPORT_RECORD_NAME,
        SESSION_ARTIFACTS,
        artifact_path,
    )

BundleError = pack.BundleError

#: 一次导入允许的**额外常驻内存**目标（PACK-SAFE-02，与 P0 冻结的那个数同一个）。
MEMORY_BUDGET_BYTES = pack.IMPORT_MEMORY_BUDGET_BYTES

#: 包内自述（`session.json` / `manifest.json`）的读取上限。它们由我们自己生成、只随成员
#: 个数增长（≤256 个成员 ⇒ 几十 KB 量级），所以"大得不像一份自述"本身就是一条拒绝理由 ——
#: 否则一个恶意包可以拿它逼我们把几百 MB 读进内存（那正是这一节点要消灭的事）。
DECLARATION_MAX_BYTES = 4 * 1024**2

#: 同一时间最多留几个导入任务记录（有界：大包导入不改天，但别无限留在内存里）。
MAX_KEPT_TASKS = 4

#: 任务记录的保留时间（秒）。
TASK_TTL_S = 30 * 60

ChunkSource = Iterable[bytes] | Callable[[int], bytes]


def upload_path(imports_dir: Path | str, task_id: str) -> Path:
    """这个任务的上传体落在哪（临时，`.incoming-` 前缀 ⇒ 一眼可辨、可清理）。

    ⚠️ 任务号的合法性只有一处判定（P0 的 `pack.task_id_ok`）：临时目录、上传体、
    装配处的检查全走它，不会出现"这条路拦了、那条路没拦"。
    """
    clean = str(task_id or "").strip()
    if not pack.task_id_ok(clean):
        raise ValueError(f"任务 ID 不合法：{task_id!r}")
    return Path(imports_dir) / f"{pack.SCRATCH_PREFIX}{clean}.zip"


def _chunks(source: ChunkSource, chunk_bytes: int) -> Iterator[bytes]:
    """把"字节从哪来"统一成一块一块：可迭代的就直接迭代，可调用的按块要。"""
    if callable(source):
        while True:
            block = source(chunk_bytes)
            if not block:
                return
            yield block
        return
    for block in source:
        if block:
            yield block


def _existing(path: Path) -> Path:
    """就着往上找到第一个**真实存在**的祖先（导入目录第一次用的时候还没有它）。

    ⚠️ 这是一条真踩到的：`disk_usage()` 对不存在的路径会抛 `FileNotFoundError`，
    而导入目录是**第一次导入时才建**的 —— 早期版本于是在第一个包上就把整个 HTTP 请求搞崩了。
    """
    current = Path(path)
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


# ================================================================== 一、上传落盘
def spool_upload(
    source: ChunkSource,
    *,
    target: Path,
    declared_bytes: int | None,
    limit_bytes: int = pack.MAX_UPLOAD_BYTES,
    chunk_bytes: int = pack.STREAM_CHUNK_BYTES,
    token: Any | None = None,
    on_progress: Callable[[int], None] | None = None,
    free_bytes: int | None = None,
) -> dict[str, Any]:
    """把上传体**逐块写盘**，边写边数、边算整包 SHA-256（PACK-IMPORT-01）。

    先拦后读：长度没声明、或声明就超上限、或盘不够 —— 都在**写第一个字节之前**拒。
    收完再对一次账：实收字节数与声明的长度不一致（少了 = 断连，多了 = 发送方乱写）
    同样拒绝，而且半包会**被删掉**。
    """
    if declared_bytes is None:
        raise BundleError(
            "upload_length_unknown",
            "上传没有声明长度：不知道要收多少字节就没法先拦后读（分块传输不收）",
        )
    declared = int(declared_bytes)
    if declared > int(limit_bytes):
        raise BundleError("upload_too_large", f"上传体 {declared} B > 上限 {int(limit_bytes)} B")
    if free_bytes is not None:
        budget = pack.disk_budget(
            upload_bytes=declared, uncompressed_bytes=0, free_bytes=int(free_bytes)
        )
        if not budget["enough"]:
            raise BundleError(
                "disk_insufficient",
                f"收这个包需要 {budget['required_bytes']} B（含余量），"
                f"可用 {budget['free_bytes']} B，还差 {budget['shortfall_bytes']} B",
            )
    digest = hashlib.sha256()
    received = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as handle:
            for block in _chunks(source, chunk_bytes):
                if token is not None and getattr(token, "cancelled", False):
                    raise BundleError("import_cancelled", "上传过程中被取消")
                received += len(block)
                if received > declared:
                    raise BundleError(
                        "upload_incomplete",
                        f"收到的字节数超过声明的长度（声明 {declared} B，已收 {received} B）",
                    )
                handle.write(block)
                digest.update(block)
                if on_progress is not None:
                    on_progress(received)
    except (BundleError, OSError):
        target.unlink(missing_ok=True)
        raise
    if received != declared:
        target.unlink(missing_ok=True)
        raise BundleError(
            "upload_incomplete",
            f"连接在收满之前断了：声明 {declared} B，实收 {received} B（半包不许落进正式目录）",
        )
    return {"path": str(target), "bytes": received, "sha256": digest.hexdigest()}


# ================================================================== 二、ZIP 预检
def _member_info(entry: zipfile.ZipInfo) -> pack.MemberInfo:
    """中央目录里的一条 → 契约认识的形状（**不读任何正文**）。"""
    external = int(getattr(entry, "external_attr", 0)) >> 16
    is_dir = entry.filename.endswith("/") or (external & 0xF000) == 0x4000
    is_symlink = (external & 0xF000) == 0xA000
    return pack.MemberInfo(
        name=entry.filename,
        compressed_bytes=int(entry.compress_size),
        uncompressed_bytes=int(entry.file_size),
        is_dir=bool(is_dir),
        is_symlink=bool(is_symlink),
        encrypted=bool(int(getattr(entry, "flag_bits", 0)) & 0x1),
    )


def open_archive(path: Path) -> zipfile.ZipFile:
    """从磁盘打开 ZIP；不是 ZIP / 中央目录坏了都归 `archive_malformed`。"""
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, ValueError) as error:
        raise BundleError("archive_malformed", f"这不是一个 zip：{error}") from None
    except OSError as error:
        raise BundleError("archive_malformed", f"读不了这个文件：{error}") from None


def _whitelist_rejects(names: Iterable[str]) -> list[pack.Reject]:
    """旧导入的**更严**那一条（PACK-COMPAT-01）：只认我们自己打包时写的那些名字。

    不做"挑认识的部分解开"这种聪明事：多一个条目说明这个包不是这份工具生成的，
    而"从陌生压缩包里挑文件解出来"正是最不该做的动作。
    """
    allowed = "、".join(sorted(bundle_import.ALLOWED_ENTRIES))
    rejects: list[pack.Reject] = []
    for name in names:
        if str(name) not in bundle_import.ALLOWED_ENTRIES:
            rejects.append(
                pack.Reject(
                    "archive_member_name_invalid",
                    f"包里有一个我们不认识的条目：{name!r}；只接受我们自己打的那些名字（{allowed}）"
                    " —— 不认识的条目说明这个包不是这份工具生成的，所以整个包都不收",
                    member=str(name),
                )
            )
    return rejects


def _required_entry_rejects(names: Iterable[str]) -> list[pack.Reject]:
    """包里必须有的条目（`manifest.json`）：没有它就不知道这是哪一次会话。"""
    present = {str(name) for name in names}
    missing = [name for name in bundle_import.REQUIRED_ENTRIES if name not in present]
    if not missing:
        return []
    return [
        pack.Reject(
            "bundle_entry_missing",
            f"包不完整：缺少 {'、'.join(missing)}（没有 manifest.json 就不知道这是哪一次会话）",
        )
    ]


@dataclass(frozen=True)
class ArchiveCheck:
    """预检结果：成员声明 + 每条对应的 `ZipInfo`（解压时按它逐块读）+ 全部拒绝理由。"""

    entries: tuple[zipfile.ZipInfo, ...]
    members: tuple[pack.MemberInfo, ...]
    rejects: tuple[pack.Reject, ...]
    upload_bytes: int
    plan: pack.ImportPlan

    @property
    def accepted(self) -> bool:
        return not self.rejects

    @property
    def uncompressed_bytes(self) -> int:
        return int(self.plan.uncompressed_bytes)

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejects": [row.to_json() for row in self.rejects],
            "member_count": len(self.members),
            "upload_bytes": int(self.upload_bytes),
            "uncompressed_bytes": self.uncompressed_bytes,
            "budget": dict(self.plan.budget),
        }


def declared_session_name(archive: zipfile.ZipFile) -> str:
    """包里 `manifest.json` 自述的会话号（**只读这一份小文件**，读不出来就回空）。

    ⚠️ 会话号要进**目录名**，所以这里刻意只认 `manifest.json` 里的 `session_id`：
    它随后要过 `artifact_path()` 的白名单校验，越界的名字在那里就被拒（不新造路径规则）。
    """
    entry = next(
        (info for info in archive.infolist() if info.filename == "manifest.json"), None
    )
    if entry is None or int(entry.file_size) > DECLARATION_MAX_BYTES:
        return ""
    try:
        with archive.open(entry) as handle:
            payload = handle.read(DECLARATION_MAX_BYTES + 1)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return ""
    if len(payload) > DECLARATION_MAX_BYTES:
        return ""
    try:
        manifest = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(manifest, dict):
        return ""
    return str(manifest.get("session_id") or "")


def inspect_archive(
    archive: zipfile.ZipFile,
    *,
    upload_bytes: int,
    imports_dir: Path | str,
    task_id: str,
    free_bytes: int | None = None,
    existing: Iterable[str] = (),
) -> ArchiveCheck:
    """从中央目录把"这个包收不收"一次算清（**不读任何正文**，PACK-IMPORT-02）。

    判定只有一处：P0 的 `pack.plan_import()`（阈值 / 成员规则 / 磁盘预算）。
    这里额外补上旧导入就有的两条规矩 —— 名字白名单、必须有的条目 —— 它们同样是
    **整包拒绝**，同样一次报全部原因（只报第一条的话，剩下的规则永远没被证明过）。
    """
    entries = tuple(archive.infolist())
    members = tuple(_member_info(entry) for entry in entries)
    names = [entry.filename for entry in entries]
    plan = pack.plan_import(
        members,
        upload_bytes=int(upload_bytes),
        free_bytes=free_bytes,
        imports_dir=imports_dir,
        task_id=task_id,
        session_name=declared_session_name(archive),
        existing=existing,
    )
    rejects = _whitelist_rejects(names)
    rejects.extend(_required_entry_rejects(names))
    rejects.extend(plan.rejects)
    seen: set[str] = set()
    unique: list[pack.Reject] = []
    for row in rejects:
        key = f"{row.code}|{row.member}|{row.detail}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return ArchiveCheck(
        entries=entries,
        members=members,
        rejects=tuple(unique),
        upload_bytes=int(upload_bytes),
        plan=plan,
    )


# ================================================================== 三、流式解压
def extract_members(
    archive: zipfile.ZipFile,
    check: ArchiveCheck,
    staging: Path,
    *,
    chunk_bytes: int = pack.STREAM_CHUNK_BYTES,
    token: Any | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_member: Callable[[int, str], None] | None = None,
    on_compressed: Callable[[int], None] | None = None,
) -> dict[str, str]:
    """逐块把成员解到临时目录，同时算每份的 SHA-256（PACK-IMPORT-03）。

    返回 `{名字: sha256}`。**不用 `archive.read(entry)`** —— 那正是旧路把整份成员
    物化进内存的那一行；这里 `archive.open(entry)` 之后一次只拿一块。

    解出来的字节数与中央目录声明的对不上（截断/伪造声明）⇒ `archive_declared_mismatch`；
    解压流本身坏了 ⇒ `archive_truncated` / `archive_malformed`。任何一条都**整包拒绝**，
    而且临时目录由调用方在失败路径上删掉（这里只管写到一半就抛）。
    """
    staging.mkdir(parents=True, exist_ok=False)
    digests: dict[str, str] = {}
    written = 0
    for index, (entry, member) in enumerate(zip(check.entries, check.members), start=1):
        if token is not None and getattr(token, "cancelled", False):
            raise BundleError("import_cancelled", f"解压到 {member.name} 之前被取消")
        if on_member is not None:
            on_member(index, member.name)
        target = staging / member.name
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(entry, "r") as source, target.open("wb") as sink:
                while True:
                    if token is not None and getattr(token, "cancelled", False):
                        raise BundleError("import_cancelled", f"解压 {member.name} 时被取消")
                    block = source.read(chunk_bytes)
                    if not block:
                        break
                    sink.write(block)
                    digest.update(block)
                    size += len(block)
                    written += len(block)
                    if on_progress is not None:
                        on_progress(written)
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise BundleError("archive_malformed", f"{member.name} 解压失败：{error}") from None
        except EOFError as error:
            raise BundleError("archive_truncated", f"{member.name} 的数据没写全：{error}") from None
        except OSError as error:
            raise BundleError(
                "import_publish_failed", f"{member.name} 写临时目录失败：{error}"
            ) from None
        if size != int(member.uncompressed_bytes):
            raise BundleError(
                "archive_declared_mismatch",
                f"{member.name} 实际解出 {size} B，与包内声明 {int(member.uncompressed_bytes)} B 不一致",
            )
        digests[member.name] = digest.hexdigest()
    return digests


# ================================================================== 四、声明核对
def _read_declaration(path: Path, what: str) -> dict[str, Any] | None:
    """有界地读一份自述 JSON（不存在 ⇒ `None`；大得不像自述 / 不是 JSON ⇒ 整包拒）。"""
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > DECLARATION_MAX_BYTES:
        raise BundleError(
            "archive_declared_mismatch",
            f"{what} 有 {size} B，超过自述文件上限 {DECLARATION_MAX_BYTES} B：它不像一份声明",
        )
    try:
        value = json.loads(path.read_bytes().decode("utf-8", "replace"))
    except ValueError:
        raise BundleError("archive_declared_mismatch", f"{what} 不是合法 JSON") from None
    return value if isinstance(value, dict) else None


def manifest_of(staging: Path) -> dict[str, Any]:
    """会话自述（`manifest.json`）：必须能解析、必须有 `session_id`，否则整包不收。

    与旧导入同一条规矩（PACK-COMPAT-01），只是从**解压出来的文件**里读，
    而不是从内存里的字节读。
    """
    path = staging / "manifest.json"
    if not path.is_file():
        raise BundleError("bundle_entry_missing", "包里没有 manifest.json")
    manifest = _read_declaration(path, "manifest.json")
    if not manifest or not manifest.get("session_id"):
        raise BundleError(
            "archive_declared_mismatch", "manifest.json 不是合法 JSON，或里面没有 session_id"
        )
    return manifest


def verify_declaration(staging: Path, digests: dict[str, str]) -> dict[str, Any]:
    """把 `session.json` 的声明与实际哈希对照（消息文本走 `bundle_import` 那一处）。"""
    declared = _read_declaration(staging / bundle_import.BUNDLE_INFO_NAME, bundle_import.BUNDLE_INFO_NAME)
    try:
        verified, checks, warnings = bundle_import.verify_declared_hashes(declared, digests)
    except bundle_import.ImportRejected as error:
        raise BundleError("bundle_hash_mismatch", str(error)) from None
    return {"declared": declared, "verified": verified, "checks": checks, "warnings": warnings}


def check_declared_session_id(manifest: dict[str, Any], declaration: dict[str, Any] | None) -> None:
    """两份自述说的会话号必须一致（不猜哪个对，整个包不收）。"""
    if not declaration:
        return
    declared_id = declaration.get("session_id")
    actual_id = str(manifest.get("session_id") or "")
    if declared_id and str(declared_id) != actual_id:
        raise BundleError(
            "archive_declared_mismatch",
            f"这个包自己说的会话号（{declared_id}）与 manifest 里的（{actual_id}）不一致"
            " —— 不猜哪个对，整个包不收",
        )


def prune_non_evidence(staging: Path) -> list[str]:
    """发布前把**不是证据**的成员从临时目录里拿掉（`README.txt` / `session.json`）。

    旧导入就是这么做的：那两份是**包自己的说明**，它们的原样副本进
    `import.json.declared`，不进会话目录 —— 会话目录里只该有证据与主机的交代（PACK-COMPAT-01）。
    返回被拿掉的名字（记进任务消息用）。
    """
    removed: list[str] = []
    for entry in sorted(staging.iterdir()):
        if entry.name in SESSION_ARTIFACTS or entry.name == IMPORT_RECORD_NAME:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed.append(entry.name)
    return removed


# ================================================================== 五、原子发布
def publish(staging: Path, imports_dir: Path | str, manifest: dict[str, Any]) -> str:
    """把解压好的临时目录**原子改名**成正式导入会话（PACK-IMPORT-04）。

    名字按 P0 的 `published_name()`：`<会话号>-imported`，重名编号（不覆盖、不合并）。
    改名之前那个目录不存在；改名之后它才出现在会话清单里。
    """
    root = Path(imports_dir)
    root.mkdir(parents=True, exist_ok=True)
    existing = [row.name for row in root.iterdir() if row.is_dir()]
    name = pack.published_name(str(manifest.get("session_id") or ""), existing)
    # 先让**同一套**路径规则把会话号与目录关系验一遍（越界在这里就被拒）。
    final = artifact_path(root, name, "manifest.json").parent
    try:
        os.replace(staging, final)
    except OSError as error:
        raise BundleError("import_publish_failed", f"原子发布失败：{error}") from None
    return name


def write_import_record(
    final: Path,
    *,
    session_name: str,
    origin_session_id: str,
    source_name: str,
    upload: dict[str, Any],
    declaration: dict[str, Any] | None,
    verified: bool,
    checks: list[str],
    warnings: list[str],
    imported_utc: str | None = None,
) -> dict[str, Any]:
    """`import.json`：与旧导入**同一套键**（PACK-COMPAT-01：读它的人不用改）。

    只增不减：`bundle_sha256` / `bundle_bytes` 来自上传落盘那一步算出来的值 ——
    不必把包再读一遍（这次连包体都没有进过内存）。
    """
    record = {
        "schema": 1,
        "kind": "workbench-observe-import",
        "imported_utc": imported_utc or utc_now(),
        "imported_from": source_name or "(未记录文件名)",
        "bundle_sha256": str(upload.get("sha256") or ""),
        "bundle_bytes": int(upload.get("bytes") or 0),
        "session_id": session_name,
        "origin_session_id": origin_session_id,
        "verified": bool(verified),
        "checks": list(checks),
        "warnings": list(warnings),
        "declared": declaration,
        "notes": list(bundle_import.IMPORT_NOTES),
        # 只增：这一条是 P3 才有的 —— 包体进了临时文件、解压体也进了临时目录，
        # 所以"这份包有多大"必须留在记录里，而不是靠"再读一遍"。
        "streamed": True,
        "stream_chunk_bytes": int(pack.STREAM_CHUNK_BYTES),
    }
    (final / IMPORT_RECORD_NAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return record


# ================================================================== 六、一键流水线
def import_uploaded(
    upload_file: Path,
    *,
    imports_dir: Path | str,
    task_id: str,
    source_name: str = "",
    task: pack.ImportTask | None = None,
    token: Any | None = None,
    chunk_bytes: int = pack.STREAM_CHUNK_BYTES,
    free_bytes: int | None = None,
    upload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**上传体已经在盘上**时：预检 → 解压 → 核对 → 发布（返回与旧导入同形的结果）。

    `upload` 给的是落盘时顺手算出来的 `{bytes, sha256}`（省一次读）；不给就自己算一遍。
    """
    root = Path(imports_dir)
    root.mkdir(parents=True, exist_ok=True)
    upload_file = Path(upload_file)
    if upload is None:
        digest = hashlib.sha256()
        size = 0
        with upload_file.open("rb") as handle:
            for block in iter(lambda: handle.read(chunk_bytes), b""):
                size += len(block)
                digest.update(block)
        upload = {"path": str(upload_file), "bytes": size, "sha256": digest.hexdigest()}
    size = int(upload["bytes"])
    scratch = pack.scratch_dir(root, task_id)
    if task is not None:
        task.source_name = source_name or task.source_name
        task.total_bytes = size
        task.scratch_dir = str(scratch)
        task.advance(stage="verify", received=size)
    try:
        with open_archive(upload_file) as archive:
            check = inspect_archive(
                archive,
                upload_bytes=size,
                imports_dir=root,
                task_id=task_id,
                free_bytes=free_bytes,
                existing=[row.name for row in root.iterdir() if row.is_dir()],
            )
            if task is not None:
                task.member_count = len(check.members)
            if not check.accepted:
                raise BundleError(check.rejects[0].code, reject_summary(check.rejects))
            if task is not None:
                task.advance(stage="extract")

            def note_extract(written: int) -> None:
                if task is not None:
                    task.advance(received=written)

            def note_member(index: int, name: str) -> None:
                if task is not None:
                    task.member_index = index
                    task.current_member = name

            digests = extract_members(
                archive,
                check,
                scratch,
                chunk_bytes=chunk_bytes,
                token=token,
                on_progress=note_extract,
                on_member=note_member,
            )
        if task is not None:
            task.advance(stage="publish", received=check.uncompressed_bytes)
        manifest = manifest_of(scratch)
        declared = verify_declaration(scratch, digests)
        check_declared_session_id(manifest, declared["declared"])
        prune_non_evidence(scratch)
        name = publish(scratch, root, manifest)
    except BundleError:
        discard_scratch(scratch)
        raise
    except OSError as error:
        discard_scratch(scratch)
        raise BundleError("import_publish_failed", f"导入失败：{error}") from None
    final = root / name
    record = write_import_record(
        final,
        session_name=name,
        origin_session_id=str(manifest.get("session_id") or ""),
        source_name=source_name,
        upload=upload,
        declaration=declared["declared"],
        verified=bool(declared["verified"]),
        checks=list(declared["checks"]),
        warnings=list(declared["warnings"]),
    )
    if task is not None:
        task.session_name = name
        task.published_dir = str(final)
        task.current_member = ""
    return {
        "session_id": name,
        "origin_session_id": record["origin_session_id"],
        "imported_utc": record["imported_utc"],
        "verified": bool(record["verified"]),
        "checks": list(record["checks"]),
        "warnings": list(record["warnings"]),
        "imports_dir": str(root),
        "task_id": task_id,
        "bytes": size,
        "sha256": str(upload["sha256"]),
    }


def import_bundle_stream(
    source: ChunkSource,
    *,
    imports_dir: Path | str,
    task_id: str,
    declared_bytes: int | None,
    source_name: str = "",
    task: pack.ImportTask | None = None,
    token: Any | None = None,
    chunk_bytes: int = pack.STREAM_CHUNK_BYTES,
    free_bytes: int | None = None,
) -> dict[str, Any]:
    """**从字节流**导入：上传落盘 → 预检 → 解压 → 核对 → 发布（PACK-IMPORT-01…04）。"""
    root = Path(imports_dir)
    root.mkdir(parents=True, exist_ok=True)
    upload_file = upload_path(root, task_id)
    if task is not None:
        task.total_bytes = int(declared_bytes or 0)

    def note_upload(received: int) -> None:
        if task is not None:
            task.advance(stage="upload", received=received)

    try:
        upload = spool_upload(
            source,
            target=upload_file,
            declared_bytes=declared_bytes,
            chunk_bytes=chunk_bytes,
            token=token,
            on_progress=note_upload,
            free_bytes=free_bytes,
        )
        return import_uploaded(
            upload_file,
            imports_dir=root,
            task_id=task_id,
            source_name=source_name,
            task=task,
            token=token,
            chunk_bytes=chunk_bytes,
            free_bytes=free_bytes,
            upload=upload,
        )
    finally:
        # 上传体是**这一步**自己创建的临时文件：收完就不再需要它了（解压从临时目录走）。
        # ⚠️ `spool_upload` 自己抛（例如被往一个**已经结束**的任务上推、或者写到一半盘坏了）
        # 也要走这里 —— 否则它会把自己刚写的那一块留在盘上（实测：1 MiB 的
        # `.incoming-<任务号>.zip`，与"不留半包"的约定相反）。
        upload_file.unlink(missing_ok=True)


def reject_summary(rejects: tuple[pack.Reject, ...]) -> str:
    """拒绝理由：**一次说清全部**（只报第一条的话，剩下的规则永远没被证明过）。"""
    rows = [f"[{row.code}] {row.detail}" for row in rejects]
    return "整个包都不收：" + "；".join(rows)


def discard_scratch(path: Path) -> None:
    """删掉临时目录（失败/取消时用：不留半个包）。"""
    shutil.rmtree(path, ignore_errors=True)


# ================================================================== 七、残留
def residue(imports_dir: Path | str) -> dict[str, list[str]]:
    """导入目录里的条目按 P0 的分类摆开：`scratch` / `published` / `foreign`。

    "进程重启不产生可见半包"这件事的判据就在这份分类上：`.incoming-*` 一律是残留，
    而**只有它**可以被清理 —— 别的目录可能是用户的证据，猜错的代价是删掉别人的数据。
    """
    rows: dict[str, list[str]] = {"scratch": [], "published": [], "foreign": []}
    root = Path(imports_dir)
    if not root.is_dir():
        return rows
    for entry in sorted(root.iterdir()):
        rows[pack.classify_residue(entry.name)].append(entry.name)
    return rows


def cleanup_scratch(
    imports_dir: Path | str, *, older_than_s: float = 0.0, now: float | None = None
) -> list[str]:
    """删掉 `.incoming-*` 残留（默认全删；给了 TTL 就只删够旧的）。**别的一律不碰**。"""
    root = Path(imports_dir)
    if not root.is_dir():
        return []
    moment = time.time() if now is None else float(now)
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        if pack.classify_residue(entry.name) != "scratch":
            continue
        if older_than_s:
            try:
                if moment - entry.stat().st_mtime < float(older_than_s):
                    continue
            except OSError:
                continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed.append(entry.name)
    return removed


# ================================================================== 八、任务登记处
@dataclass
class _Job:
    task: pack.ImportTask
    token: Any
    result: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_json(self) -> dict[str, Any]:
        with self.lock:
            payload = self.task.to_json()
            payload["result"] = self.result
        return payload


class _CancelToken:
    """最小的取消旗子（与导出那条路同形）。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class ImportCenter:
    """导入任务的登记处（登记、跑、查进度、取消、残留）。

    与 `ExportCenter` / `SearchCenter` 同一套形状：它**不认识 HTTP**，
    字节从哪来、进度往哪报都由装配处决定。

    同一时间只允许一个大包导入（`import_task_busy`）—— 这是 P0 冻结的规矩：
    一个大包的代价是几百 MB 磁盘与几十秒 CPU，并行两份没有好处。
    """

    def __init__(
        self,
        imports_dir: Path | str,
        *,
        free_space: Callable[[Path], int] | None = None,
        cancel_token_factory: Callable[[], Any] | None = None,
        max_tasks: int = MAX_KEPT_TASKS,
        ttl_s: float = TASK_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.imports_dir = Path(imports_dir)
        self._tokens = cancel_token_factory or (lambda: _CancelToken())
        self._free_space = free_space
        self._max_tasks = max(1, int(max_tasks))
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._order: list[str] = []
        self._finished_at: dict[str, float] = {}

    def free_bytes(self) -> int | None:
        """这个盘还剩多少（没注入探测函数就回 `None` = "不知道"，不是 0）。"""
        if self._free_space is None:
            return None
        return int(self._free_space(_existing(self.imports_dir)))

    def register(self, *, source_name: str = "", declared_bytes: int | None = None) -> pack.ImportTask:
        """登记一个导入任务（**同步**：拒 busy 在这里就生效）。任务号由这里生成。"""
        return self.register_as(
            f"import-{uuid.uuid4().hex[:8]}",
            source_name=source_name,
            declared_bytes=declared_bytes,
        )

    def register_as(
        self, task_id: str, *, source_name: str = "", declared_bytes: int | None = None
    ) -> pack.ImportTask:
        """按**给定的**任务号登记（HTTP 层先定任务号、再收请求体；重复登记回原来那个）。

        任务号来自客户端时**必须**先过 `pack.task_id_ok()`（它会当文件名用）；
        不合法就在这里拒，不要等到写临时文件那一刻。
        """
        clean = str(task_id or "").strip()
        if not pack.task_id_ok(clean):
            raise ValueError(f"任务号不合法：{task_id!r}（只收字母/数字/-/_，且不超过 {pack.TASK_ID_MAX} 字符）")
        with self._lock:
            job = self._jobs.get(clean)
            if job is not None:
                return job.task
            self._refuse_busy()
            task = pack.ImportTask(
                task_id=clean,
                source_name=source_name,
                started_utc=utc_now(),
                total_bytes=int(declared_bytes or 0),
            )
            self._jobs[clean] = _Job(task=task, token=self._tokens())
            self._order.append(clean)
            self._evict()
        return task

    def run(
        self,
        task_id: str,
        source: ChunkSource,
        *,
        declared_bytes: int | None,
        source_name: str = "",
    ) -> pack.ImportTask:
        """把一次导入跑完（同步）：失败/取消都把理由记进任务，并把错误抛回调用方。"""
        job = self._jobs[str(task_id)]
        try:
            job.result = import_bundle_stream(
                source,
                imports_dir=self.imports_dir,
                task_id=str(task_id),
                declared_bytes=declared_bytes,
                source_name=source_name,
                task=job.task,
                token=job.token,
                free_bytes=self.free_bytes(),
            )
            _finish(job.task, "done", message=str(job.result["session_id"]))
            return job.task
        except BundleError as error:
            _finish(
                job.task,
                "cancelled" if error.code == "import_cancelled" else "failed",
                error=error.code,
                message=error.message,
            )
            raise
        except OSError as error:
            _finish(job.task, "failed", error="import_publish_failed", message=str(error))
            raise
        finally:
            with self._lock:
                self._finished_at[str(task_id)] = self._clock()

    def role_of(self, path: Path | str) -> str:
        """这个路径是不是我们自己的临时产物（HTTP 层用它区分"我们的残留"与别人的文件）。"""
        name = Path(path).name
        return pack.classify_residue(name)

    def outcome(self, task_id: str) -> _Job:
        try:
            return self._jobs[str(task_id)]
        except KeyError:
            raise KeyError(str(task_id)) from None

    def cancel(self, task_id: str) -> bool:
        job = self._jobs.get(str(task_id))
        if job is None:
            raise KeyError(str(task_id))
        if job.task.finished:
            return False
        job.token.cancel()
        return True

    def snapshot(self) -> dict[str, Any]:
        """给 `/api/state` 看的自述：任务列表 + 在跑的数量（"在跑"问任务对象自己）。"""
        with self._lock:
            rows = [self._jobs[key].to_json() for key in self._order if key in self._jobs]
            running = sum(
                1
                for key in self._order
                if key in self._jobs and not self._jobs[key].task.finished
            )
        return {"tasks": rows, "running": running}

    def residue(self) -> dict[str, list[str]]:
        return residue(self.imports_dir)

    def cleanup(self, *, older_than_s: float = 0.0) -> list[str]:
        """清残留（**只动 `.incoming-*`**）；同时收回过期的任务记录。"""
        removed = cleanup_scratch(self.imports_dir, older_than_s=older_than_s)
        moment = self._clock()
        with self._lock:
            for task_id in list(self._finished_at):
                if moment - self._finished_at[task_id] > self._ttl_s:
                    self._order = [key for key in self._order if key != task_id]
                    self._jobs.pop(task_id, None)
                    self._finished_at.pop(task_id, None)
        return removed

    def abandon(self, task_id: str, *, why: str = "连接在收满之前断了") -> None:
        """记下一个**没收完**的上传（连接断了）：状态落 failed，临时文件删掉。

        PACK-IMPORT-04 要的就是这条：半包不许落进正式目录，也不许以"正在跑"的样子留着。
        """
        clean = str(task_id)
        discard_scratch(pack.scratch_dir(self.imports_dir, clean))
        upload_path(self.imports_dir, clean).unlink(missing_ok=True)
        job = self._jobs.get(clean)
        if job is None:
            return
        _finish(job.task, "failed", error="upload_incomplete", message=why)
        with self._lock:
            self._finished_at[clean] = self._clock()

    # ------------------------------------------------------------------ 内部
    def _refuse_busy(self) -> None:
        for key in self._order:
            job = self._jobs.get(key)
            if job is None or job.task.finished:
                continue
            raise BundleError(
                "import_task_busy", f"已经有一个导入在跑（{key}）：大包导入一次只跑一个"
            )

    def _evict(self) -> None:
        while len(self._order) > self._max_tasks:
            key = self._order[0]
            job = self._jobs.get(key)
            if job is not None and not job.task.finished:
                break
            self._order.pop(0)
            self._jobs.pop(key, None)


def _finish(task: pack.ImportTask, state: str, *, error: str = "", message: str = "") -> None:
    """收尾并**盖上结束时间**（时间来自调用方这一侧，契约模块不认识时钟）。"""
    if task.finished:
        return
    task.finish(state, error=error, message=message)
    task.finished_utc = utc_now()

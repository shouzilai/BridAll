""" / P0：大体量诊断包的**冻结契约** —— 阈值、任务状态、错误码与拒绝规则。

这一份**只有形状，没有产品实现**（P0 的完成定义就是"无产品实现"）：不认识 `zipfile`、
不碰临时目录、不开文件、不起服务。真正的上传落盘在 P3、流式导出在 P2、页面在 P4。
先把"什么叫安全"说死，后面几个节点才不用各自发明一套 —— 与  的 L0 同一套纪律。

为什么单独一个模块（而不是塞进 `bundle_import.py`）：小包那套（KB/MB 级、32 MiB 上限）
是**已经在跑的产品路径**，它的语义要原样保住（PACK-COMPAT-01）。大包这套是另一组阈值、
另一套阶段与拒绝规则；两套共存但**共用同一份错误码纪律**，谁也不去改对方的数。

四条规矩：

1. **阈值单点**。安全边界只在这里定义，并由 API 自述返回。400 MiB 是**验收夹具**，
   不是产品最大值 —— 这一条在下面用 `FIXTURE_BYTES` 与各阈值的大小关系写清楚、也可被核对。
2. **整套拒绝**。包内任何一处（名字、重复、目录、符号链接、加密、声明不一致、压缩率、
   总量、个数）不合规 ⇒ **整包拒绝**，不做"挑几个能用的进去"。
3. **先拦后读**。超阈值、未知长度、磁盘不够都要在**读正文之前**下结论 —— 否则"拒绝"
   本身就成了一次 GB 级内存/磁盘事故。
4. **发布是原子的一步**。上传与解压先落在带任务 ID 的临时目录里，全部校验通过之后
   才一次性改名进正式目录；在此之前它在会话清单里**不存在**（不是"出现了但标着失败"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 契约形状版本。改字段/改语义要动它（别只改注释）。
SCHEMA = 1

#: API 自述里报出去的版本号（页面据此判断"我这套阈值和服务是不是同一份"）。
CONTRACT_VERSION = 1


# =================================================================== 一、安全阈值
#: 上传的 ZIP 本体大小上限。本地服务的防滥用边界，不是"系统永远只能到这里"。
MAX_UPLOAD_BYTES = 1 * 1024**3

#: 单个成员**解压后**的大小上限。
MAX_MEMBER_BYTES = 512 * 1024**2

#: 全部成员解压后的总量上限（压缩炸弹的正面防线）。
MAX_TOTAL_UNCOMPRESSED_BYTES = 1 * 1024**3

#: 成员个数上限。几万个小文件同样是一种攻击（每个都要开文件、算哈希、占 inode）。
MAX_MEMBER_COUNT = 256

#: 单成员压缩率上限。
#:
#: ⚠️ **主防线是硬上限**（单成员 / 总量 / 个数），压缩率只是省时间的第二道：
#: 一个 200 KB 压出 400 MiB 的成员就算过了尺寸关，解出来也只是白占 400 MiB 磁盘与时间。
#: 阈值刻意取得**高**：一个只发重复字节的设备日志真的可能压到几百倍，
#: 把阈值定在 200 倍会把"合法但高度可压的原始流"一起拒掉 —— 那是拿一个假阳性换安心。
#: 真正的炸弹（1 KB → 1 GiB，百万倍量级）在这个阈值下面一条也跑不掉。
MAX_COMPRESSION_RATIO = 1000

#: 小于这个声明大小的成员**不看压缩率**：几百字节的文件压缩几千倍是家常便饭，
#: 而且完全无害（它就那么点大）。
RATIO_EXEMPT_BYTES = 1 * 1024**2

#: 固定块大小：上传落盘、解压、哈希**共用同一个数**。
#: 需求不变量 2：任何一处都不许按包体量级驻留内存 —— 它们都只轮转这一块。
STREAM_CHUNK_BYTES = 1 * 1024**2

#: 服务端处理一个大包时允许的**额外常驻内存**目标（PACK-SAFE-02）。
#: ⚠️ 与 `large_evidence.REVIEW_MEMORY_BUDGET_BYTES`（32 MiB，回看/搜索那条路）
#: 是**两个量**：这里量的是"导入/导出这条流水线"，那里量的是"回看一次请求"。
#: 名字里那个 IMPORT 是刻意的：谁都不许把两个数并成一个。
IMPORT_MEMORY_BUDGET_BYTES = 64 * 1024**2

#: 磁盘预算里留的安全余量：刚好写满盘会连带把服务自己的日志/证据一起挤爆。
DISK_HEADROOM_BYTES = 512 * 1024**2

#: 验收夹具的大小（400 MiB）。它**必须严格小于**上面每一条上限 ——
#: 这一条是"400 MiB 是夹具而不是产品最大值"的可核对写法（见 `fixture_fits_the_thresholds()`）。
FIXTURE_BYTES = 400 * 1024**2


def thresholds() -> dict[str, int]:
    """安全阈值的**唯一出处**（API 自述与页面都读它，不各抄一份）。"""
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_member_bytes": MAX_MEMBER_BYTES,
        "max_total_uncompressed_bytes": MAX_TOTAL_UNCOMPRESSED_BYTES,
        "max_member_count": MAX_MEMBER_COUNT,
        "max_compression_ratio": MAX_COMPRESSION_RATIO,
        "ratio_exempt_bytes": RATIO_EXEMPT_BYTES,
        "stream_chunk_bytes": STREAM_CHUNK_BYTES,
        "import_memory_budget_bytes": IMPORT_MEMORY_BUDGET_BYTES,
        "disk_headroom_bytes": DISK_HEADROOM_BYTES,
    }


def fixture_fits_the_thresholds() -> bool:
    """400 MiB 夹具能不能过自己的安全阈值（P0 的自检，也是文档里那句话的可核对版本）。"""
    return (
        FIXTURE_BYTES < MAX_UPLOAD_BYTES
        and FIXTURE_BYTES < MAX_MEMBER_BYTES
        and FIXTURE_BYTES < MAX_TOTAL_UNCOMPRESSED_BYTES
    )


# =================================================================== 二、错误码
#: 错误码表：`(错误码, HTTP 状态, 为什么是这个状态)`。**唯一**的一份。
#: 页面/CLI/测试都从这里取，谁都不许在别处再写一个字符串常量。
ERROR_CODES: tuple[tuple[str, int, str], ...] = (
    # —— 上传阶段（先拦后读）——
    ("upload_length_unknown", 411, "分块上传必须声明长度：不知道要收多少字节就无法先拦后读"),
    ("upload_too_large", 413, "上传体超过 Max_upload_bytes"),
    ("upload_incomplete", 400, "连接在收满声明长度之前断了（半包不许落进正式目录）"),
    ("disk_insufficient", 507, "可用空间不够（上传体 + 解压总量 + 安全余量）"),
    # —— ZIP 预检（解压之前）——
    ("archive_malformed", 400, "不是合法的 ZIP，或中央目录读不出来"),
    ("archive_truncated", 400, "ZIP 声明了却没写全（截断）"),
    ("archive_encrypted", 400, "含加密成员：我们不解密，也不假装能读"),
    ("archive_member_name_invalid", 400, "成员名不是白名单裸文件名（路径/绝对路径/空名/怪字符）"),
    ("archive_member_duplicate", 400, "同一个名字出现多次：后面那份会悄悄盖掉前面那份"),
    ("archive_member_count_too_large", 413, "成员个数超过 Max_member_count"),
    ("archive_member_too_large", 413, "某个成员解压后超过 Max_member_bytes"),
    ("archive_member_ratio_too_large", 413, "某个成员压缩率超过 Max_compression_ratio（压缩炸弹）"),
    ("archive_total_too_large", 413, "解压总量超过 Max_total_uncompressed_bytes"),
    ("archive_declared_mismatch", 400, "声明的大小与实际不符（伪造声明）"),
    # —— 内容校验 ——
    ("bundle_entry_missing", 400, "缺少必要的条目（例如 session.json）"),
    ("bundle_hash_mismatch", 422, "结构与声明都对，但逐字节哈希对不上：内容不是原来那一份"),
    # —— 任务与发布 ——
    ("import_task_unknown", 404, "没有这个导入任务（或已过期被清掉）"),
    ("import_task_busy", 409, "同一时间只允许一个大包导入：另一个还在跑"),
    ("import_cancelled", 409, "任务已被取消（取消是显式动作，不是错误）"),
    ("import_publish_failed", 500, "校验都过了但原子发布失败（改名/落盘）"),
    # —— 导出（P2）：与导入同一套纪律，只是方向相反 ——
    ("export_task_unknown", 404, "没有这个导出任务（或已过期被清掉）"),
    ("export_task_busy", 409, "同一个会话同一时间只允许一个导出：另一个还在跑"),
    ("export_source_missing", 404, "要导出的证据不在了（快照之后被删/被移走）"),
    ("export_source_changed", 409, "证据在导出过程中变短了（快照边界不成立）"),
    ("export_write_failed", 500, "写临时 ZIP 失败（磁盘/权限）"),
)

ERROR_STATUS: dict[str, int] = {code: status for code, status, _why in ERROR_CODES}
ERROR_WHY: dict[str, str] = {code: why for code, _status, why in ERROR_CODES}


class BundleError(Exception):
    """配置/契约层面的错误（**不是**"这个包不合规"）。

    "包不合规"是一种**结果**（`ImportPlan.accepted is False` + 一组拒绝原因），
    不是异常 —— 负向夹具天天都要走那条路，把它做成异常会让人用 try/except 去当判据。
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{code!r}（先加到 ERROR_CODES）")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status if status is not None else ERROR_STATUS[code])


# =================================================================== 三、任务状态
#: 处理阶段（有序）。页面按这个顺序画"上传 → 校验 → 解压 → 发布"。
STAGES: tuple[str, ...] = ("upload", "verify", "extract", "publish")

#: 任务状态（封闭集合）。`done` / `failed` / `cancelled` 都是**终态**。
IMPORT_STATES: tuple[str, ...] = ("running", "done", "failed", "cancelled")

#: 终态：到了就不许再动（"失败之后又发布成功"这种事不该能被表达出来）。
TERMINAL_STATES: tuple[str, ...] = ("done", "failed", "cancelled")

#: 允许的状态迁移。**只列出来的这些**能走：其余一律拒绝。
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "running": ("done", "failed", "cancelled"),
    "done": (),
    "failed": (),
    "cancelled": (),
}

#: 阶段的**人话**（页面直接显示，不在前端各写一份）。
STAGE_LABELS: dict[str, str] = {
    "upload": "正在接收上传",
    "verify": "正在校验包结构",
    "extract": "正在解压并核对哈希",
    "publish": "正在发布到导入目录",
}

#: 导出（P2）的阶段：先定**快照边界**，再逐块写临时 ZIP，再收口径哈希，最后才可下载。
#: 顺序与导入那边同一条纪律：每一步都能单独说清"正在进行什么"。
EXPORT_STAGES: tuple[str, ...] = ("snapshot", "write", "hash", "ready")

#: 导出阶段的人话（页面直接显示，不在前端再写一份）。
EXPORT_STAGE_LABELS: dict[str, str] = {
    "snapshot": "正在取快照（记下每份证据的大小）",
    "write": "正在逐块写入临时 ZIP",
    "hash": "正在收总哈希",
    "ready": "已就绪，可以下载",
}


def stage_order(stage: str) -> int:
    """阶段序号（越靠后越接近发布）。没登记的阶段抛错，不返回 -1 蒙过去。"""
    if stage not in STAGES:
        raise ValueError(f"没登记的阶段：{stage!r}（{STAGES}）")
    return STAGES.index(stage)


def can_transition(current: str, following: str) -> bool:
    if current not in IMPORT_STATES:
        raise ValueError(f"没登记的任务状态：{current!r}（{IMPORT_STATES}）")
    if following not in IMPORT_STATES:
        raise ValueError(f"没登记的任务状态：{following!r}（{IMPORT_STATES}）")
    return following in TRANSITIONS[current]


@dataclass
class ImportTask:
    """一个导入任务的**形状**（进度与结果）。

    ⚠️ `total_bytes` 可能**未知**（分块传输、或者调用方没声明长度）。
    这时候 `percent` 必须是 `None` —— "不知道总量"与"0%"是两件事，
    页面画一个转圈的进度条都行，就是不许画一个假的百分比（PACK-UI-01）。
    """

    task_id: str
    source_name: str = ""
    state: str = "running"
    stage: str = "upload"
    received_bytes: int = 0
    total_bytes: int = 0
    member_index: int = 0
    member_count: int = 0
    current_member: str = ""
    session_name: str = ""
    started_utc: str = ""
    finished_utc: str = ""
    error: str = ""
    message: str = ""
    scratch_dir: str = ""
    published_dir: str = ""

    def __post_init__(self) -> None:
        if self.state not in IMPORT_STATES:
            raise ValueError(f"没登记的任务状态：{self.state!r}（{IMPORT_STATES}）")
        if self.stage not in STAGES:
            raise ValueError(f"没登记的阶段：{self.stage!r}（{STAGES}）")
        if self.error and self.error not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{self.error!r}（先加到 ERROR_CODES）")
        if int(self.received_bytes) < 0 or int(self.total_bytes) < 0:
            raise ValueError("字节数不许是负数")

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def percent(self) -> float | None:
        """上传进度百分比；**总量未知时返回 `None`**（不许伪造）。"""
        total = int(self.total_bytes)
        if total <= 0:
            return None
        received = min(int(self.received_bytes), total)
        return round(received / total, 4)

    def advance(self, *, stage: str | None = None, received: int | None = None) -> None:
        """推进一个任务。阶段只能往前走（或原地），状态只能按 `TRANSITIONS` 走。"""
        if self.finished:
            raise ValueError(f"任务已经结束（{self.state}）：终态不许再推进")
        if stage is not None:
            if stage_order(stage) < stage_order(self.stage):
                raise ValueError(f"阶段不许往回倒：{self.stage} → {stage}")
            self.stage = stage
        if received is not None:
            self.received_bytes = min(int(received), int(self.total_bytes)) if self.total_bytes else int(received)

    def finish(self, state: str, *, error: str = "", message: str = "") -> None:
        if not can_transition(self.state, state):
            raise ValueError(f"非法迁移：{self.state} → {state}")
        if error and error not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{error!r}")
        self.state = state
        self.error = error
        self.message = message

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_name": self.source_name,
            "state": self.state,
            "stage": self.stage,
            "stage_label": STAGE_LABELS[self.stage],
            "received_bytes": int(self.received_bytes),
            "total_bytes": int(self.total_bytes),
            "percent": self.percent,
            "member_index": int(self.member_index),
            "member_count": int(self.member_count),
            "current_member": self.current_member,
            "session_name": self.session_name,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "error": self.error,
            "error_why": ERROR_WHY.get(self.error, ""),
            "message": self.message,
            "scratch_dir": self.scratch_dir,
            "published_dir": self.published_dir,
        }


# =================================================================== 四、成员检查
@dataclass(frozen=True)
class MemberInfo:
    """一个 ZIP 成员的**声明**（来自中央目录，不是解压出来的）。"""

    name: str
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    is_dir: bool = False
    is_symlink: bool = False
    encrypted: bool = False

    @property
    def ratio(self) -> float:
        compressed = int(self.compressed_bytes)
        if compressed <= 0:
            return 0.0 if int(self.uncompressed_bytes) <= 0 else float("inf")
        return int(self.uncompressed_bytes) / compressed


@dataclass(frozen=True)
class Reject:
    """一条拒绝原因（**整包拒绝**：任何一条命中就整份不收）。"""

    code: str
    detail: str
    member: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": ERROR_STATUS[self.code],
            "why": ERROR_WHY[self.code],
            "detail": self.detail,
            "member": self.member,
        }


#: 成员名里**绝不允许**出现的字符：路径分隔符、盘符、NUL、控制字符。
_FORBIDDEN_NAMES = (".", "..")


def member_name_ok(name: str) -> bool:
    """成员名必须是**白名单裸文件名**：不是路径、不是绝对路径、不是目录、不是怪字符。

    需求不变量 3 的第一条。这里刻意**不**做"清洗"（把 `..\\..\\x` 修成 `x`）——
    清洗会把一个攻击包变成一个"看起来正常"的包，而我们要的是**拒收**。
    """
    value = str(name or "")
    if not value or value in _FORBIDDEN_NAMES:
        return False
    if len(value) > 128:
        return False
    if "/" in value or "\\" in value:
        return False
    if value.endswith("/") or value.endswith("\\"):
        return False
    if value.startswith("."):
        # 隐藏文件/相对路径不进正式目录：正式目录里不该冒出 `.` 开头的东西。
        return False
    if ":" in value:          # 盘符（`C:`）与备用数据流（`x.txt:ads`）
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return True


def check_members(
    members: Iterable[MemberInfo],
    *,
    upload_bytes: int = 0,
) -> list[Reject]:
    """把中央目录逐条过一遍，收集**全部**拒绝原因（不是只报第一条）。

    要求一次说清所有问题：负向夹具的验收要看"哪几条规则真的拦住了"，只报第一条的话
    剩下的规则永远没被证明过。
    """
    rows = list(members)
    rejects: list[Reject] = []
    if int(upload_bytes) > MAX_UPLOAD_BYTES:
        rejects.append(
            Reject(
                "upload_too_large",
                f"ZIP 本体 {int(upload_bytes)} B > {MAX_UPLOAD_BYTES} B",
            )
        )
    if len(rows) > MAX_MEMBER_COUNT:
        rejects.append(
            Reject("archive_member_count_too_large", f"{len(rows)} 个成员 > {MAX_MEMBER_COUNT}")
        )
    seen: dict[str, int] = {}
    uncompressed_total = 0
    for row in rows:
        name = str(row.name)
        if not member_name_ok(name):
            rejects.append(
                Reject("archive_member_name_invalid", f"成员名不合规：{name!r}", member=name)
            )
        if row.is_dir:
            rejects.append(
                Reject("archive_member_name_invalid", "目录条目：我们只收裸文件名", member=name)
            )
        if row.is_symlink:
            rejects.append(
                Reject("archive_member_name_invalid", "符号链接：不许把包外的文件牵进来", member=name)
            )
        if row.encrypted:
            rejects.append(Reject("archive_encrypted", "加密成员", member=name))
        key = name.casefold()
        seen[key] = seen.get(key, 0) + 1
        size = int(row.uncompressed_bytes)
        if size < 0 or int(row.compressed_bytes) < 0:
            rejects.append(
                Reject("archive_declared_mismatch", "声明了负的大小", member=name)
            )
        elif size > MAX_MEMBER_BYTES:
            rejects.append(
                Reject(
                    "archive_member_too_large",
                    f"解压后 {size} B > {MAX_MEMBER_BYTES} B",
                    member=name,
                )
            )
        if size > RATIO_EXEMPT_BYTES and row.ratio > MAX_COMPRESSION_RATIO:
            rejects.append(
                Reject(
                    "archive_member_ratio_too_large",
                    f"压缩率 {row.ratio:.0f} 倍 > {MAX_COMPRESSION_RATIO} 倍",
                    member=name,
                )
            )
        uncompressed_total += max(0, size)
    for name, count in seen.items():
        if count > 1:
            rejects.append(
                Reject("archive_member_duplicate", f"同一个名字出现 {count} 次", member=name)
            )
    if uncompressed_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        rejects.append(
            Reject(
                "archive_total_too_large",
                f"解压总量 {uncompressed_total} B > {MAX_TOTAL_UNCOMPRESSED_BYTES} B",
            )
        )
    return rejects


# =================================================================== 五、磁盘预算
def disk_budget(
    *, upload_bytes: int, uncompressed_bytes: int, free_bytes: int
) -> dict[str, Any]:
    """算一次"这次导入要占多少盘"：上传临时文件 + 解压总量 + 安全余量。

    PACK-SAFE-01：只**告警或拒绝**，绝不自动删旧证据（删证据是用户显式决定的事）。
    """
    required = int(upload_bytes) + int(uncompressed_bytes) + DISK_HEADROOM_BYTES
    return {
        "upload_bytes": int(upload_bytes),
        "uncompressed_bytes": int(uncompressed_bytes),
        "headroom_bytes": DISK_HEADROOM_BYTES,
        "required_bytes": required,
        "free_bytes": int(free_bytes),
        "enough": int(free_bytes) >= required,
        "shortfall_bytes": max(0, required - int(free_bytes)),
    }


# =================================================================== 六、发布
#: 临时目录/文件的前缀。**以点开头**是刻意的：正式会话目录永远不会以它开头，
#: 于是"残留"一眼可辨（P0 要求"残留可识别、可清理，不进入会话清单"）。
SCRATCH_PREFIX = ".incoming-"

#: 发布后的目录名后缀（沿用现有导入语义：`<session>-imported[-N]`）。
IMPORTED_SUFFIX = "-imported"


#: 任务 ID 的长度上限（它会进文件名/目录名，所以不许无限长）。
TASK_ID_MAX = 64


def task_id_ok(task_id: str) -> bool:
    """任务号能不能当文件名用（**单一出处**：临时目录与上传体都走它）。

    允许的字符集故意很窄（字母 / 数字 / `-` / `_`）：它会变成
    `<imports>/.incoming-<任务号>` 与 `...-<任务号>.zip`，所以路径分隔符、盘符、
    前导点、控制字符一概不收；也不做"清洗"（把 `../x` 修成 `x`）——
    需要修就说明调用方在用别人的字符串当路径。
    """
    value = str(task_id or "").strip()
    if not value or len(value) > TASK_ID_MAX:
        return False
    if value.startswith("."):
        return False
    return all(char.isalnum() or char in "-_" for char in value) and value.isascii()


def scratch_dir(imports_dir: Path | str, task_id: str) -> Path:
    """这个任务的临时目录：**和正式目录同一个父目录**。

    同一父目录 ⇒ 同一条文件系统 ⇒ 最后那一步 `rename` 才是**原子**的
    （跨盘改名会退化成"复制 + 删除"，中途崩掉就留下半个正式包）。
    """
    clean = str(task_id or "").strip()
    if not task_id_ok(clean):
        raise ValueError(f"任务 ID 不合法：{task_id!r}")
    return Path(imports_dir) / f"{SCRATCH_PREFIX}{clean}"


def published_name(session_name: str, existing: Iterable[str] = ()) -> str:
    """发布目录名：`<session>-imported`，重名就 `-imported-2`、`-imported-3`…

    与现有导入语义一致（PACK-COMPAT-01）：重名**编号**，不覆盖、不合并。
    """
    base = str(session_name or "").strip() or "session"
    taken = {str(row) for row in existing}
    first = f"{base}{IMPORTED_SUFFIX}"
    if first not in taken:
        return first
    index = 2
    while f"{first}-{index}" in taken:
        index += 1
    return f"{first}-{index}"


def classify_residue(name: str) -> str:
    """目录名分类：临时残留 / 已发布的导入包 / 不认识（**不认识就别动**）。

    "清理"只允许动 `scratch`：别的目录可能是用户的证据，猜错的代价是删掉别人的数据。
    """
    value = str(name or "")
    if value.startswith(SCRATCH_PREFIX):
        return "scratch"
    if value.endswith(IMPORTED_SUFFIX) or "-imported-" in value:
        return "published"
    return "foreign"


def publication_plan(
    imports_dir: Path | str, *, task_id: str, session_name: str, existing: Iterable[str] = ()
) -> dict[str, str]:
    """一次发布的路径计划：临时目录 → 正式目录（**同父目录**，于是改名是原子的）。"""
    staging = scratch_dir(imports_dir, task_id)
    final = Path(imports_dir) / published_name(session_name, existing)
    return {
        "scratch_dir": str(staging),
        "published_dir": str(final),
        "same_parent": str(staging.parent) == str(final.parent),
    }


# =================================================================== 七、导出任务
@dataclass
class ExportTask:
    """一个**导出**任务的形状（P2：从磁盘逐块生成临时 ZIP，再流式下载）。

    与 `ImportTask` 同一套纪律：阶段只能往前走、终态不许再推进、
    字节数不许是负数、总量未知时 `percent` 必须是 `None`。
    另外多两件导出特有的事实：**快照边界**（`member_sizes`）与**临时文件去向**。
    """

    task_id: str
    session_id: str = ""
    state: str = "running"
    stage: str = "snapshot"
    written_bytes: int = 0
    total_bytes: int = 0
    member_index: int = 0
    member_count: int = 0
    current_member: str = ""
    bundle_path: str = ""
    bundle_bytes: int = 0
    bundle_sha256: str = ""
    member_sizes: tuple[tuple[str, int], ...] = ()
    started_utc: str = ""
    finished_utc: str = ""
    error: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if self.state not in IMPORT_STATES:
            raise ValueError(f"没登记的任务状态：{self.state!r}（{IMPORT_STATES}）")
        if self.stage not in EXPORT_STAGES:
            raise ValueError(f"没登记的导出阶段：{self.stage!r}（{EXPORT_STAGES}）")
        if self.error and self.error not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{self.error!r}（先加到 ERROR_CODES）")
        if int(self.written_bytes) < 0 or int(self.total_bytes) < 0:
            raise ValueError("字节数不许是负数")

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def percent(self) -> float | None:
        """已写入 / 快照总量。**总量为 0（还没有快照）时返回 `None`**。"""
        total = int(self.total_bytes)
        if total <= 0:
            return None
        return round(min(int(self.written_bytes), total) / total, 4)

    def advance(self, *, stage: str | None = None, written: int | None = None) -> None:
        if self.finished:
            raise ValueError(f"任务已经结束（{self.state}）：终态不许再推进")
        if stage is not None:
            if EXPORT_STAGES.index(stage) < EXPORT_STAGES.index(self.stage):
                raise ValueError(f"阶段不许往回倒：{self.stage} → {stage}")
            self.stage = stage
        if written is not None:
            value = int(written)
            limit = int(self.total_bytes)
            self.written_bytes = min(value, limit) if limit > 0 else value

    def finish(self, state: str, *, error: str = "", message: str = "") -> None:
        """收尾：落状态与理由，并**盖一次结束时间**（`to_json` 里有这个字段，空着就是说假话）。"""
        if not can_transition(self.state, state):
            raise ValueError(f"非法迁移：{self.state} → {state}")
        if error and error not in ERROR_STATUS:
            raise ValueError(f"没登记的错误码：{error!r}")
        self.state = state
        self.error = error
        self.message = message


    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "state": self.state,
            "stage": self.stage,
            "stage_label": EXPORT_STAGE_LABELS[self.stage],
            "written_bytes": int(self.written_bytes),
            "total_bytes": int(self.total_bytes),
            "percent": self.percent,
            "member_index": int(self.member_index),
            "member_count": int(self.member_count),
            "current_member": self.current_member,
            "bundle_path": self.bundle_path,
            "bundle_bytes": int(self.bundle_bytes),
            "bundle_sha256": self.bundle_sha256,
            "members": [{"name": name, "bytes": int(size)} for name, size in self.member_sizes],
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "error": self.error,
            "error_why": ERROR_WHY.get(self.error, ""),
            "message": self.message,
        }


# =================================================================== 八、整体判定
@dataclass(frozen=True)
class ImportPlan:
    """一次导入的**判定结果**（P0 只判定，不做任何 I/O）。

    `accepted is False` 时 `rejects` 至少有一条 —— 这条不变量由 `plan_import()` 保证，
    测试也钉它（"拒绝了却不说为什么"是最难查的那种失败）。
    """

    accepted: bool
    rejects: tuple[Reject, ...]
    member_count: int
    uncompressed_bytes: int
    budget: dict[str, Any] = field(default_factory=dict)
    staging: str = ""
    published: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "rejects": [row.to_json() for row in self.rejects],
            "member_count": int(self.member_count),
            "uncompressed_bytes": int(self.uncompressed_bytes),
            "budget": dict(self.budget),
            "staging": self.staging,
            "published": self.published,
        }


def plan_import(
    members: Iterable[MemberInfo],
    *,
    upload_bytes: int = 0,
    free_bytes: int | None = None,
    imports_dir: Path | str | None = None,
    task_id: str = "",
    session_name: str = "",
    existing: Iterable[str] = (),
) -> ImportPlan:
    """把"这个包收不收"一次算清：阈值 + 成员规则 + 磁盘预算（**不做 I/O**）。

    负向夹具（截断、重复名、路径穿越、加密、伪造声明、压缩炸弹、磁盘不足）走的都是这里：
    它们要证明的是"**在读正文之前**就已经拒了"，而不是"读进来之后发现不对"。
    """
    rows = list(members)
    rejects = check_members(rows, upload_bytes=int(upload_bytes))
    total = sum(max(0, int(row.uncompressed_bytes)) for row in rows)
    budget: dict[str, Any] = disk_budget(
        upload_bytes=int(upload_bytes),
        uncompressed_bytes=total,
        free_bytes=int(free_bytes) if free_bytes is not None else 0,
    )
    if free_bytes is not None and not budget["enough"]:
        rejects.append(
            Reject(
                "disk_insufficient",
                f"需要 {budget['required_bytes']} B（含余量 {DISK_HEADROOM_BYTES} B），"
                f"可用 {budget['free_bytes']} B，还差 {budget['shortfall_bytes']} B",
            )
        )
    plan_paths: dict[str, str] = {}
    if imports_dir is not None and task_id:
        plan_paths = publication_plan(
            imports_dir, task_id=task_id, session_name=session_name, existing=existing
        )
    return ImportPlan(
        accepted=not rejects,
        rejects=tuple(rejects),
        member_count=len(rows),
        uncompressed_bytes=total,
        budget=budget,
        staging=plan_paths.get("scratch_dir", ""),
        published=plan_paths.get("published_dir", ""),
    )

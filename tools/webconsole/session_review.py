"""会话回看与诊断包：**只读**方向（Phase 7.1 切片 6，需求 §9.1-10）。

写入方向是 `observe_store.py`（只追加、逐块 flush）；这里是反过来读的那一半。
它有意的做法是：**不 import 写入侧的任何记账**（计数器、内存里的 manifest），
只按磁盘上的文件重新算一遍。这样"落盘的数"与"回看时看到的数"才有可能是两个数 ——
否则回看只会把写入时的记忆复述一遍，永远发现不了落盘本身有毛病。

会话目录（与写入侧同一套文件，由 `artifact_path()` 一处解析路径）：

```text
observe-sessions/<session-id>/
  manifest.json      会话、设备、端口、结束原因、计数、身份与字典匹配
  raw.bin            设备发来的完整 RX 字节（**不含** TX）
  io_events.jsonl    RX 索引、TX 字节、连接状态与身份事件（seq + 单调时钟）
  decoded.log        派生产物：由 raw.bin + 字典重建；字典精确命中时才存在
  app_events.jsonl   派生产物：由 decoded.log 重建；同样按需存在
```

三件必须说清楚的事：

1. **四个窗口的内容来自文件**：`raw.bin` / `decoded.log` 原样读，`app_events.jsonl`
   原样交给页面（它自己渲染，与 run 的历史装载同一条路径），而「工具输出」窗口
   由 `io_timeline.tool_rows_with_stats()` 从 `io_events.jsonl` 重新画一遍
   （工具窗口的文字本来就只是 `io_events.jsonl` 的视图，需求 §5.2）。它是**派生视图**：
   RX 索引不画 ⇒ 行号与文件行号不同源，所以每一行还带上它来自的那条事件的 `seq`
   （`tool_seqs`，与 `tool` 逐行对应）——阶段卡要指到这一窗的某一行就按事件号指
   （`{"source": "tool", "event_seq": N}`，）。
2. **数出来的数与 manifest 里的数分开报**（`counts` / `manifest_totals`）：它们对不上
   就是一条值得看的发现，而不是把谁抹平。
3. **诊断包是"生成那一刻的快照"**：会话还在进行时读到的只是当下的前缀，
   这一点写在包里的 `README.txt` 与 `session.json` 里，不靠读者自己猜。

⚠️ 读正在写入的文件（会话进行中）是安全的：只追加的文件读到的是一个前缀，
不会读到半行之后再被改写的内容。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile
from typing import Any

try:  # imported as a package (``webconsole.session_review``)
    from . import large_evidence
    from . import platform_info
    from .core_primitives import split_complete_lines, utc_now
    from .io_timeline import REVIEW_PREFIX, tool_rows_with_stats
    from .observe_store import (
        IMPORT_DIR_NAME,
        IMPORT_RECORD_NAME,
        SESSION_ARTIFACTS,
        SESSION_DERIVED_ARTIFACTS,
        SESSION_MANDATORY_ARTIFACTS,
        artifact_path,
    )
except ImportError:  # executed as a plain script
    import large_evidence  # type: ignore[no-redef]
    import platform_info  # type: ignore[no-redef]
    from core_primitives import split_complete_lines, utc_now  # type: ignore[no-redef]
    from io_timeline import REVIEW_PREFIX, tool_rows_with_stats  # type: ignore[no-redef]
    from observe_store import (  # type: ignore[no-redef]
        IMPORT_DIR_NAME,
        IMPORT_RECORD_NAME,
        SESSION_ARTIFACTS,
        SESSION_DERIVED_ARTIFACTS,
        SESSION_MANDATORY_ARTIFACTS,
        artifact_path,
    )

# 诊断包里的文件名（除会话自带的五份证据之外，再加两份"解释这个包"的）。
BUNDLE_INFO_NAME = "session.json"
BUNDLE_README_NAME = "README.txt"
# 打包顺序 = 打开压缩包的人应该先看什么。会话证据一律**原样**放进去，不做任何改写。
BUNDLE_ORDER = (
    BUNDLE_README_NAME,
    BUNDLE_INFO_NAME,
    "manifest.json",
    "io_events.jsonl",
    "raw.bin",
    "decoded.log",
    "app_events.jsonl",
)

BUNDLE_NOTES = (
    "raw.bin 里只有设备发来的字节：TX 与主机侧说明在 io_events.jsonl 里"
    "（两者共用同一条 seq + 单调时钟时间线，所以先后关系可以重放）。",
    "decoded.log 与 app_events.jsonl 是**派生产物**：由 raw.bin + 当时的构建字典重建，"
    "字典精确命中（build id 一致）时才会存在 —— 没有这两个文件不代表设备没说话。",
    "app_events.jsonl 里每条记录的 offset 是这条记录在 decoded.log 里的**字节偏移**。",
    "主机侧运行期的说明文字（没有记进 io_events.jsonl 的那些，例如实时解码就绪）不在证据里，"
    "因此也不在本包里：包里没有的东西就是不存在的证据，不是被省略了。",
    "这个包是**生成那一刻的快照**：会话还在进行时，之后到达的字节不在包里。",
)


class SessionNotFound(LookupError):
    """会话不存在（目录没有，或目录里没有 manifest.json）。"""


def session_root_origin(root: Path) -> str:
    """按**目录名**猜这个根里的会话是什么来的（`local` / `import`）。

    ⚠️ 这只是一个**直接调用时的兜底**（例如宿主测试给一个临时目录）：它能用，
    全靠导入目录叫 `IMPORT_DIR_NAME`。真正的路由不用它 —— `resolve_session()`
    直接把“根 ↔ 来源”配对给出，因为一个名字换了（或两份目录被拷到别处）就不应该
    把别人给的包说成本机采的。
    """
    return "import" if Path(root).name == IMPORT_DIR_NAME else "local"


def resolve_session(
    sessions_dir: Path, imports_dir: Path | None, session_id: str
) -> tuple[Path, str]:
    """会话号 → （它住在哪个根, 来源）。

    来源是**配对给出的**，不从目录名猜：哪个根被我们当成导入目录，那里的会话就是导入的。
    两个根用**同一套**路径规则（`artifact_path`），所以越界的会话号在这里就被拒；
    找不到就抛 `SessionNotFound`（路由映射成 404）。
    """
    candidates: list[tuple[Path, str]] = [(Path(sessions_dir), "local")]
    if imports_dir is not None:
        candidates.append((Path(imports_dir), "import"))
    for root, origin in candidates:
        path = artifact_path(root, session_id, "manifest.json")   # 非法会话号在这里就抛
        if path.is_file():
            return root, origin
    raise SessionNotFound(
        f"会话 {session_id} 不存在：本机会话与导入目录里都没有它"
    )


# 回看页要说清楚的事（不是免责声明，是“这个窗口里的字是哪儿来的”）。
REVIEW_NOTES = (
    "「工具输出」窗口的内容是从 io_events.jsonl 重新画出来的：它是那份时间线的视图，"
    "不是另存的一份工具日志（主机侧运行期的说明文字没有进时间线，所以不会出现）。",
    "「原始串口」窗口 = raw.bin（只有设备发来的字节），「解码日志」= decoded.log，"
    "「结构化事件」= app_events.jsonl；四份都原样读，没有任何加工。",
    "回看不会打开串口、不会编译、不会烧录、不会复位设备。",
)


# ------------------------------------------------------------------ 读取原语
def _read_bytes(sessions_dir: Path, session_id: str, name: str) -> bytes | None:
    """读一份会话文件；不存在返回 None（派生产物按需存在，不是错误）。"""
    path = artifact_path(sessions_dir, session_id, name)
    return path.read_bytes() if path.is_file() else None


def _size(sessions_dir: Path, session_id: str, name: str) -> int | None:
    """一份会话文件现在多大（清单视图用：不读内容，只回答“在不在、多大”）。"""
    path = artifact_path(sessions_dir, session_id, name)
    return path.stat().st_size if path.is_file() else None


def artifact_sizes(sessions_dir: Path, session_id: str) -> dict[str, int | None]:
    """每个会话文件**现在多大**（只 `stat`，不读内容）。不存在就是 `None`。"""
    out: dict[str, int | None] = {}
    for name in SESSION_ARTIFACTS:
        path = artifact_path(sessions_dir, session_id, name)
        out[name] = path.stat().st_size if path.is_file() else None
    return out


#: 阈值闸门**管不到**的文件。`manifest.json` 是这台主机的**元数据**（几百字节到几 KB），
#: 不是设备发来的证据 —— 把它也跳掉的话，回看会连"这是哪次会话"都答不上来
#: （第一版就是这么错的：`content=none` 直接退化成 SessionNotFound）。
ALWAYS_READ = ("manifest.json",)


def read_artifacts(sessions_dir: Path, session_id: str, *,
                   skip_over: int | None = None) -> dict[str, bytes | None]:
    """一个会话的全部文件（**一次读全**，后面所有的视图都从这一份字节派生）。

    一次读全的好处是"计数 / 哈希 / 打包"三件事看到的是同一份字节：分开读的话，
    一个正在写入的会话会给出三个互相对不上的快照。

    ⚠️  / L2 新增 `skip_over`：超过这个字节数的文件**不读**（回 `None`）。
    回看首屏不能为了一份 400 MB 的 `raw.bin` 先把整份装进内存；`None` 在这里的意思是
    "存在，但这次没装载" —— 调用方想区分"不存在"看 `artifact_sizes()`。
    """
    out: dict[str, bytes | None] = {}
    for name in SESSION_ARTIFACTS:
        path = artifact_path(sessions_dir, session_id, name)
        if not path.is_file():
            out[name] = None
            continue
        if (
            skip_over is not None
            and name not in ALWAYS_READ
            and path.stat().st_size > skip_over
        ):
            out[name] = None
            continue
        out[name] = _read_bytes(sessions_dir, session_id, name)
    if out["manifest.json"] is None:
        raise SessionNotFound(f"会话 {session_id} 不完整：目录里没有 manifest.json")
    return out


def _decode(data: bytes) -> str:
    """字节 → 文本。设备字节流可能不是合法 UTF-8，用 replace 而不是抛异常。"""
    return data.decode("utf-8", "replace")


def split_lines(data: bytes) -> list[bytes]:
    """把一整段字节切成"看得见的行" —— 与实时观察的读取线程**同一条规则**：

    完整行（保留行终止符）+ 最后那段没有换行的尾巴（拔线/断开时它也要显示出来，
    见 `observe._flush_lines()`）。共用 `pipeline.split_complete_lines()`，
    所以"实时窗口里 22 行、回看时 21 行"这种事不会发生。
    """
    buffer = bytearray(data)
    lines = split_complete_lines(buffer)
    if buffer:
        lines.append(bytes(buffer))
    return lines


def lines_text(lines: list[bytes]) -> str:
    """行 → 交给页面的文本（每行去掉行终止符，与实时推送的每一行一致）。"""
    return "\n".join(_decode(line).rstrip("\r\n") for line in lines)


def _json_lines(data: bytes | None) -> list[str]:
    if not data:
        return []
    return [line for line in _decode(data).split("\n") if line.strip()]


def _parse_events(data: bytes | None) -> list[dict[str, Any]]:
    """`io_events.jsonl` → 事件列表（坏行**如实计数**，不静默跳过）。"""
    events = []
    for line in _json_lines(data):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _manifest(data: bytes | None) -> dict[str, Any]:
    if not data:
        return {}
    try:
        payload = json.loads(_decode(data))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_dict(data: bytes | None) -> dict[str, Any]:
    """字节 → JSON 对象（读不出来就空），与 `_manifest` 同一件事、同一个口径。"""
    return _manifest(data)


def _import_record(root: Path, session_id: str) -> dict[str, Any] | None:
    """这个会话是不是导入来的？是的话给出那份记录，否则 None。

    ⚠️ `import.json` **不在证据白名单里**（它不是设备说的话），所以不能用
    `read_artifacts()` 去读；先用 `artifact_path()` 把目录校验出来，再读这一个文件。
    """
    directory = artifact_path(Path(root), session_id, "manifest.json").parent
    path = directory / IMPORT_RECORD_NAME
    if not path.is_file():
        return None
    return _json_dict(path.read_bytes())


def _import_view(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """导入记录 → 界面/包要用的那几项（不要把整份原样塞进清单）。“核对过”与“没得核对”分开。"""
    if not record:
        return None
    return {
        "imported_utc": record.get("imported_utc"),
        "imported_from": record.get("imported_from"),
        "bundle_sha256": record.get("bundle_sha256"),
        "bundle_bytes": record.get("bundle_bytes"),
        "verified": bool(record.get("verified")),
        "checks": list(record.get("checks") or []),
        "warnings": list(record.get("warnings") or []),
    }


def recorded_digest(root: Path, session_id: str, name: str) -> str | None:
    """这个会话有没有**已经记下的**可信 SHA-256？有就给出来，没有就 `None`。

    两个来源分开说（这是这个仓库里反复出现的同一条纪律：**"没得核对" 不是 "核对通过"**）：

    - **本机采集的会话**：没有任何地方记过它的文件哈希（`manifest.json` 记的是大小与计数）。
      要拿哈希就得现在读一遍整份文件 —— 那正是  花力气避免的事
      （PERF-HIST-07：建立索引不为显示摘要现算哈希）⇒ 如实返回 `None`，调用方显示"只按大小"。
    - **导入的包**：`import.json.declared.files[]` 里**每份文件都带着生成那一刻的 sha256**，
      而且 ≥ P3 起导入时会**逐份重算并逐份对照**（`verified=True` 就是"声明过的每一份都对上了"）。
      于是这份声明过的哈希就是"已记下的可信哈希"，不必再读一遍证据 ⇒ 直接返回它。

    ⚠️ 只有 `verified` 为真才认：没核对过的声明只是**别人的话**，拿它当指纹会让
    "索引是否过期"的判定跟着一句没核对过的话走。
    """
    record = _import_record(root, session_id)
    if not record or not bool(record.get("verified")):
        return None
    declaration = record.get("declared")
    if not isinstance(declaration, dict):
        return None
    for row in declaration.get("files") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "") != str(name):
            continue
        digest = str(row.get("sha256") or "")
        return digest or None
    return None


# ------------------------------------------------------------------ 会话清单
def session_ids(sessions_dir: Path) -> list[str]:
    """目录里所有会话号，**新的在前**（会话号的日期时间部分可直接比较）。"""
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    names = [path.name for path in root.iterdir() if path.is_dir()]
    return sorted(names, reverse=True)


def session_summary(sessions_dir: Path, session_id: str, *, active_session_id: str | None = None,
                    origin: str | None = None) -> dict[str, Any]:
    """一个会话的**清单视图**：够了就回答"这是哪次、有多长、结束了没有"。

    数出来的东西（`sizes`：文件现在多大）与 manifest 里记的（`totals`）分开给：
    对不上就是一条发现，不替读者抹平。

    `origin` 默认由目录名推（本机采集 / 导入的包）；导入的那种带上那份“怎么来的”记录。
    """
    root = Path(sessions_dir)
    origin = origin or session_root_origin(root)
    manifest_bytes = _read_bytes(root, session_id, "manifest.json")
    manifest = _manifest(manifest_bytes)
    files: dict[str, int] = {}
    for name in SESSION_ARTIFACTS:
        size = _size(root, session_id, name)
        if size is not None:
            files[name] = size
    identity = manifest.get("identity") or {}
    device = manifest.get("device") or {}
    return {
        "session_id": session_id,
        "opened_utc": manifest.get("opened_utc"),
        "closed_utc": manifest.get("closed_utc"),
        "end_reason": manifest.get("end_reason"),
        "device": device.get("key") or device.get("label"),
        "device_label": device.get("label"),
        "port": manifest.get("port"),
        "baud": manifest.get("baud"),
        "identity": {"state": identity.get("state"), "source": identity.get("source")},
        "sizes": files,
        "files": [name for name in SESSION_ARTIFACTS if name in files],
        "missing": [name for name in SESSION_ARTIFACTS if name not in files],
        "totals": manifest.get("totals") or {},
        # ⚠️ `totals` 是**会话结束时**才写进 manifest 的，所以正在写的那次会话它是空的。
        #   清单至少要说得出“现在有多少字节”，而文件大小是现成的、不用读内容。
        "counts": {
            "raw_bytes": files.get("raw.bin"),
            "decoded_bytes": files.get("decoded.log"),
            "io_events_bytes": files.get("io_events.jsonl"),
        },
        # `complete` = manifest 里有结束时间。**没有**结束时间的会话是存在的
        #（进程被杀、机器断电）：它仍然可以回看，但不能被说成"正常结束"。
        "complete": bool(manifest.get("closed_utc")),
        "broken": None if manifest_bytes is not None else "目录里没有 manifest.json",
        "live": session_id == active_session_id,
        # 来源（切片 8）：本机采集的，还是别人给的包。导入的那份记录里带着
        # “什么时候从哪个文件导进来的、包内声明的 sha256 核没核对过”。
        "origin": origin,
        "import": _import_view(_import_record(root, session_id)) if origin == "import" else None,
    }


def list_sessions(
    sessions_dir: Path, *, limit: int = 20, active_session_id: str | None = None,
    origin: str | None = None, include: str | None = None,
) -> list[dict[str, Any]]:
    """一个根里的会话清单（新的在前）。读不出来的也列出来并写明原因 —— 藏起来更像"证据丢了"。

    `include`：**一定要出现在这一页**的那一条（超越了 `limit` 也带着）。
    导入刚发布的那个会话号可能比默认条数老（一次导一个 9 月的包，而本机已经有几十次
    10 月的会话），按时间截断就会把它默默丢掉 —— 页面于是出现了"四个窗口装着它，
    下拉框里却没有它"。这里把"指定那一条"放在第一位，再截断。
    """
    ids = session_ids(sessions_dir)
    if include and include in ids:
        ids = [include] + [item for item in ids if item != include]
    out = []
    for session_id in ids[: max(1, int(limit))]:
        try:
            out.append(session_summary(
                sessions_dir, session_id, active_session_id=active_session_id, origin=origin
            ))
        except OSError as error:
            out.append(
                {
                    "session_id": session_id,
                    "broken": f"读不出来：{type(error).__name__}: {error}",
                    "complete": False,
                    "files": [],
                    "missing": list(SESSION_ARTIFACTS),
                    "sizes": {},
                    "counts": {},
                    "totals": {},
                    "identity": {},
                    "live": session_id == active_session_id,
                    "origin": origin or session_root_origin(Path(sessions_dir)),
                    "import": None,
                }
            )
    return out


def list_all_sessions(
    sessions_dir: Path,
    imports_dir: Path | None = None,
    *,
    limit: int = 20,
    active_session_id: str | None = None,
    include: str | None = None,
) -> list[dict[str, Any]]:
    """两个根合起来的清单（新的在前）。

    导入的会话号带 `-imported` 后缀，所以按会话号排序时它们就待在自己那次会话旁边；
    每一行都带 `origin`，界面必须能一眼看出来这是本机采的还是别人给的。

    `include`：**这次调用保证要看得到的那一条**（需求 PACK-UI-02：导入成功后自动打开
    它 —— 自动打开了、列表里却找不到它，就等于用户下次找不回来）。两个根都会找它，
    找到的那一次把行摆在第一位，于是不会被 `limit` 截掉；两个根都没有就当没提过
    （调用方只是"顺手带上"，不该因为一条不见了就让整张清单读不出来）。
    """
    rows = list_sessions(
        sessions_dir, limit=limit, active_session_id=active_session_id, origin="local",
        include=include,
    )
    if imports_dir is not None:
        rows += list_sessions(
            imports_dir, limit=limit, active_session_id=None, origin="import", include=include
        )
    rows.sort(key=lambda row: str(row.get("session_id") or ""), reverse=True)
    if include:
        wanted = [row for row in rows if row.get("session_id") == include]
        rows = wanted + [row for row in rows if row.get("session_id") != include]
    return rows[: max(1, int(limit))]


# ------------------------------------------------------------------ 回看视图
def session_detail(sessions_dir: Path, session_id: str, *, origin: str | None = None,
                   content: str = "auto") -> dict[str, Any]:
    """回看一个会话需要的全部东西：四个窗口的文本 + 数出来的计数 + 原样 manifest。

    导入来的会话多一层交代（`origin` / `import`），而且**首行就说清楚** ——
    别人给的包与自己在现场采的，不能看起来长得一样。

    `content` 三态（与 run 详情同一份名目，见 `large_evidence.CONTENT_MODES`）：
    `auto` 按阈值自己决定；`none` 只要元数据；`inline` 是旧的内嵌正文形式，
    **超阈值给结构化 413**（需求 §5.1：不留一个默认全量读取的公开路径）。
    """
    if content not in large_evidence.CONTENT_MODES:
        raise ValueError(
            f"认不出的 content={content!r}（可用：{' / '.join(large_evidence.CONTENT_MODES)}）"
        )
    root = Path(sessions_dir)
    origin = origin or session_root_origin(root)
    import_record = _import_record(root, session_id) if origin == "import" else None
    sizes = artifact_sizes(root, session_id)
    if content == "inline":
        for name, size in sizes.items():
            if size is not None and not large_evidence.should_inline(size):
                raise large_evidence.inline_too_large(name, size)
    skip_over = (
        0 if content == "none"
        else (None if content == "inline" else large_evidence.INLINE_PREVIEW_MAX_BYTES)
    )
    artifacts = read_artifacts(root, session_id, skip_over=skip_over)
    omitted = [
        {
            "name": name,
            "bytes": size,
            "reason": "超过内联阈值" if size else "本次没有这份证据",
            "url": f"/api/observe/sessions/{session_id}/evidence/{name}",
            "download_url": f"/api/observe/sessions/{session_id}/evidence/{name}?download=1",
        }
        for name, size in sizes.items()
        if size is not None
        and name not in ALWAYS_READ
        and (content == "none" or not large_evidence.should_inline(size))
    ]
    manifest = _manifest(artifacts["manifest.json"])
    # ⚠️ `None` 在这里有两个意思：不存在，或者**存在但这次没装载**。
    #    两者必须分开：把"没装载"当成"空"会让页面说"设备什么都没发"。
    raw_loaded = artifacts["raw.bin"] is not None
    decoded_loaded = artifacts["decoded.log"] is not None
    raw_bytes = artifacts["raw.bin"] or b""
    events = (
        _parse_events(artifacts["io_events.jsonl"])
        if artifacts["io_events.jsonl"] is not None else []
    )
    decoded_bytes = artifacts["decoded.log"]
    event_lines = (
        _json_lines(artifacts["app_events.jsonl"])
        if artifacts["app_events.jsonl"] is not None else []
    )

    header = f"{REVIEW_PREFIX}会话 {session_id}"
    if origin == "import":
        view = _import_view(import_record) or {}
        verified = "；包内声明的 sha256 已逐份核对" if view.get("verified") else "；没有可核对的 sha256"
        header += (
            f" · **导入的包**（{view.get('imported_utc') or '时间未记录'} 从 "
            f"{view.get('imported_from') or '未知文件'} 导入{verified}）"
        )
    counts_note = f"io 事件 {len(events)} 条"
    rx_events = sum(1 for event in events if event.get("kind") == "rx")
    tx_events = sum(1 for event in events if event.get("kind") == "tx")
    counts_note += f"（RX {rx_events} 条在 raw.bin 里、TX {tx_events} 条见下）"
    header += f" · {counts_note}"
    if manifest.get("closed_utc"):
        header += f" · 结束：{manifest.get('end_reason') or '未记录原因'}"
    else:
        header += " · 这次会话没有结束记录（manifest 里没有 closed_utc）"
    tool_rows, timeline_stats = tool_rows_with_stats(events, header=header)
    tool_lines = [text for text, _seq in tool_rows]
    # 「工具输出」是**派生视图**（RX 索引不画、这一行 header 也不是任何事件画的）⇒
    # 它每一行的身份是**事件号**而不是行号。页面拿这一列做 `data-seq`，
    # 阶段卡上的 `{"source":"tool","event_seq":N}` 就指向这里的那一行（）。
    tool_seqs = [seq for _text, seq in tool_rows]

    # 产物元数据（**只 stat**）：页面“全文件搜索”的证据下拉框就来自这一项
    # （`app.artifactsMeta`）。⚠️ 它以前**只有 run 那条路**有 —— 于是回看一个会话时，
    # 下拉框是空的，页面直接写“没有可搜的证据（这一份详情里没有列出产物）”，
    # 而需求 PACK-UI-02 明确要求导入会话“可向前加载、**全文搜索**和下载”。
    # `state` 从 manifest 自己的 `closed_utc` 读（不另加一个参数）：正在写的那次会话是 open。
    session_state = (
        large_evidence.STATE_CLOSED if manifest.get("closed_utc") else large_evidence.STATE_OPEN
    )
    artifacts_meta: list[dict[str, Any]] = []
    for name, size in sizes.items():
        if size is None:
            continue
        row = large_evidence.artifact_metadata(
            root / session_id / name,
            name,
            role=(
                large_evidence.ROLE_DERIVED
                if name in SESSION_DERIVED_ARTIFACTS
                else large_evidence.ROLE_ORIGINAL
            ),
            state=session_state,
            # 导入的包有“已记下的可信哈希”（导入时逐份核对过），本机采集的没有 ⇒ None。
            sha256=recorded_digest(root, session_id, name),
        ).to_json()
        row["url"] = f"/api/observe/sessions/{session_id}/evidence/{name}"
        row["download_url"] = f"/api/observe/sessions/{session_id}/evidence/{name}?download=1"
        artifacts_meta.append(row)

    raw_lines = split_lines(raw_bytes) if raw_loaded else []
    decoded_lines = split_lines(decoded_bytes) if decoded_loaded and decoded_bytes else []
    io_loaded = artifacts["io_events.jsonl"] is not None
    # ⚠️ 三种情况必须分开（否则页面会把"没装载"读成"设备什么都没说"）：
    #   **装载了** ⇒ 报真数；**存在但被阈值拦下** ⇒ 报 None（"没装载"）；
    #   **本来就没有这份文件** ⇒ 报 0（"这份证据确实是空的"）。
    raw_size = sizes.get("raw.bin")
    decoded_size = sizes.get("decoded.log")
    io_size = sizes.get("io_events.jsonl")
    counts = {
        "raw_bytes": len(raw_bytes) if raw_loaded else (raw_size if raw_size is not None else 0),
        # ⚠️ 按 `\n` 切的行数（与读取线程同一条规则）；页面用的是文本规则，
        #    行内出现**裸 CR** 时两者可能差一行（run 的历史装载也是同样的口径）。
        "raw_lines": len(raw_lines) if raw_loaded else (None if raw_size is not None else 0),
        "io_events": len(events) if io_loaded else (None if io_size is not None else 0),
        "rx_events": rx_events if io_loaded else (None if io_size is not None else 0),
        "tx_events": tx_events if io_loaded else (None if io_size is not None else 0),
        "decoded_bytes": (
            len(decoded_bytes or b"") if decoded_loaded
            else (decoded_size if decoded_size is not None else 0)
        ),
        "decoded_lines": (
            len(decoded_lines) if decoded_loaded
            else (None if decoded_size is not None else 0)
        ),
        "app_events": len(event_lines),
        "loaded": not omitted,
        "omitted": [row["name"] for row in omitted],
    }
    return {
        "session_id": session_id,
        "session_dir": str(root / session_id),
        # 来源（切片 8）：本机采集 / 导入的包。导入的那种带上“怎么来的”记录。
        "origin": origin,
        "import": _import_view(import_record),
        "manifest": manifest,
        "manifest_totals": manifest.get("totals") or {},
        "identity": manifest.get("identity") or {},
        # 受控测试（ / L3）：**冻结**的那一份（定义 + 结果 + 命令）。
        # 回放只认它，不去查当前用例目录 —— 否则项目包一改版，旧会话就变成另一个故事。
        "live_tests": manifest.get("live_tests") or None,
        "files": [name for name, size in sizes.items() if size is not None],
        # 产物元数据（与 run 详情同形）：页面用它填“要搜索的证据”那一栏。
        "artifacts_meta": artifacts_meta,
        "missing": [name for name in SESSION_MANDATORY_ARTIFACTS if sizes.get(name) is None],
        "counts": counts,
        "content_mode": content,
        "content_omitted": omitted,
        "evidence_contract": large_evidence.describe(),
        "timeline": {"lines": timeline_stats["rendered"], **timeline_stats},
        "notes": list(REVIEW_NOTES),
        # 四个窗口的内容。派生产物不存在、或超阈值没装载时给 None
        # （页面据此不拿它当正文，改走 Range 拉尾部）。
        "tool": "\n".join(tool_lines),
        # 上面每一行来自哪条 io 事件（同长度、逐行对应；header 那一行是 None）。
        "tool_seqs": tool_seqs,
        "raw": _decode(raw_bytes) if raw_loaded else None,
        "decoded": _decode(decoded_bytes) if decoded_loaded else None,
        "app_events": _decode(artifacts["app_events.jsonl"]) if artifacts["app_events.jsonl"] is not None else None,
    }


# ------------------------------------------------------------------ 诊断包
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_readme(session_id: str, manifest: dict[str, Any], counts: dict[str, Any],
                   files: list[dict[str, Any]], missing: list[str]) -> str:
    """给人看的说明。包里有什么、每个文件是什么、以及"这个包是什么时候的快照"。"""
    device = manifest.get("device") or {}
    lines = [
        "硬件工作台 实时观察会话诊断包",
        f"会话号：{session_id}",
        f"模式：{manifest.get('mode') or 'live'} · 端口：{manifest.get('port')} @ {manifest.get('baud')}"
        f" · 设备：{device.get('label') or device.get('key') or '未记录'}",
        f"开始：{manifest.get('opened_utc') or '未记录'}",
        f"结束：{manifest.get('closed_utc') or '（没有结束记录：这次会话没有正常收尾）'}"
        f" · 原因：{manifest.get('end_reason') or '未记录'}",
        "",
        "计数（本次打包时从文件里数出来的）：",
        f"  raw.bin {counts['raw_bytes']} B（{counts['raw_lines']} 行）"
        f" · io 事件 {counts['io_events']} 条（RX {counts['rx_events']} / TX {counts['tx_events']}）"
        f" · decoded.log {counts['decoded_bytes']} B（{counts['decoded_lines']} 行）"
        f" · app_events {counts['app_events']} 条",
        "",
        "包里的文件：",
    ]
    for row in files:
        lines.append(f"  {row['name']:<18} {row['bytes']:>9} B  sha256 {row['sha256']}")
    if missing:
        lines.append("")
        lines.append("这个包**没有**这些文件（派生产物按需存在）：" + "、".join(missing))
    lines.append("")
    lines.append("说明：")
    lines.extend(f"  - {note}" for note in BUNDLE_NOTES)
    lines.append("")
    return "\n".join(lines)


def build_bundle(
    sessions_dir: Path, session_id: str, *, generated_utc: str | None = None
) -> tuple[bytes, str]:
    """把整个会话打成一个 zip（需求 §5.1：整包下载，用户不用逐个管理文件）。

    返回 `(zip 字节, 建议的文件名)`。归档里是**原样的证据字节** + 两份解释：
    `README.txt`（给人读）与 `session.json`（给程序读，带每份文件的 sha256）。
    """
    artifacts = read_artifacts(sessions_dir, session_id)
    manifest = _manifest(artifacts["manifest.json"])
    raw_bytes = artifacts["raw.bin"] or b""
    events = _parse_events(artifacts["io_events.jsonl"])
    event_lines = _json_lines(artifacts["app_events.jsonl"])
    decoded_bytes = artifacts["decoded.log"]
    counts = {
        "raw_bytes": len(raw_bytes),
        "raw_lines": len(split_lines(raw_bytes)),
        "io_events": len(events),
        "rx_events": sum(1 for event in events if event.get("kind") == "rx"),
        "tx_events": sum(1 for event in events if event.get("kind") == "tx"),
        "decoded_bytes": len(decoded_bytes or b""),
        "decoded_lines": len(split_lines(decoded_bytes or b"")),
        "app_events": len(event_lines),
    }
    generated = generated_utc or utc_now()
    included = [
        {"name": name, "bytes": len(artifacts[name] or b""), "sha256": _sha256(artifacts[name] or b"")}
        for name in BUNDLE_ORDER
        if name in SESSION_ARTIFACTS and artifacts.get(name) is not None
    ]
    missing = [name for name in SESSION_ARTIFACTS if artifacts.get(name) is None]
    info = {
        "schema": 1,
        "kind": "workbench-observe-bundle",
        "session_id": session_id,
        "generated_utc": generated,
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
        "files": included,
        "missing": missing,
        # 平台自述（R2d / DEC-010）：会话 manifest 里已经写了一份，这里**原样转过来**，
        # 于是拿到包的人不必再问“这是哪套组合、按哪个 schema 写的”。
        # ⚠️ 只增字段：旧读者按名字读它们要的东西，不会被多出来的键影响。
        "platform": manifest.get("platform") or {},
        "schemas": dict((manifest.get("platform") or {}).get("schemas") or platform_info.SCHEMA_VERSIONS),
        "notes": list(BUNDLE_NOTES),
    }
    readme = _bundle_readme(session_id, manifest, counts, included, missing)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BUNDLE_README_NAME, readme.encode("utf-8"))
        archive.writestr(BUNDLE_INFO_NAME, json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        for name in BUNDLE_ORDER:
            if name not in SESSION_ARTIFACTS:
                continue
            data = artifacts.get(name)
            if data is not None:
                archive.writestr(name, data)
    return buffer.getvalue(), f"{session_id}-diagnostic-bundle.zip"

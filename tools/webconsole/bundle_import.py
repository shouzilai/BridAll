"""诊断包导入：把**别人给的** zip 放进本机（Phase 7.1 之后的第 8 片）。

写侧（`observe_store`）只写本机采到的会话；读侧（`session_review`）只读磁盘上的文件。
这里是第三种东西：**外部来源的字节**。它必须比前两者更小心，原因很直接 ——
前两者的输入是我们自己的串口，这里的输入是一个陌生人给的压缩包。

三条规矩：

1. **只认我们自己打包时的那几个名字**。包里出现任何其它条目（哪怕只是多一个 `.DS_Store`、
   或者一个 `../` 开头的路径）一律**拒绝整个包**，不做"挑认识的部分解开"这种聪明事 ——
   一个多出来的条目说明这个包不是我们那份工具生成的，而"从陌生压缩包里挑文件解出来"
   正是最不该做的动作（zip-slip / 覆盖本机文件都从那里来）。
2. **包自己声明过的哈希必须对得上**。诊断包里的 `session.json` 会写每份文件的 sha256；
   只要有一份对不上，这个包在传输或保存过程中就已经坏了 —— 那就**不是证据**，不入库。
   没有 `session.json`（手工打的包）时接受，但记下"没有可核对的哈希"，不假装核对过。
3. **导入的东西不冒充本机采集**。它落在**另一个目录**（`observe-imports//`），
   清单里带 `origin=import`，回看页与再次打包时都会写明它从哪来。

⚠️ 落盘的是**原样的包内字节**：不解码、不改写、不补空文件。否则"这个包说的是什么"
就不再等于"我存下来的东西"。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import zipfile
from typing import Any

try:  # imported as a package (``webconsole.bundle_import``)
    from .core_primitives import utc_now
    from .observe_store import (
        IMPORT_DIR_NAME,
        IMPORT_RECORD_NAME,
        SESSION_ARTIFACTS,
        artifact_path,
    )
    from .session_review import BUNDLE_INFO_NAME, BUNDLE_ORDER
except ImportError:  # executed as a plain script
    from core_primitives import utc_now  # type: ignore[no-redef]
    from observe_store import (  # type: ignore[no-redef]
        IMPORT_DIR_NAME,
        IMPORT_RECORD_NAME,
        SESSION_ARTIFACTS,
        artifact_path,
    )
    from session_review import BUNDLE_INFO_NAME, BUNDLE_ORDER  # type: ignore[no-redef]

# 落在这个会话目录里、记录"它是怎么来的"的文件（名字定义在 `observe_store`，读写两侧共用一处）。
# ⚠️ 它**不是**会话证据（不进 `SESSION_ARTIFACTS` 白名单）：证据是设备说的话，
#    这一份是主机对"我从哪拿到这些字节"的交代。
# 导入会话号的后缀：本机采集的会话号形如 `20260926-135533-live-a`，
# 导入的加上 `-imported`，一眼能分辨（即便有人把它们放到同一个目录里）。
IMPORT_SUFFIX = "-imported"

# 上限都是**防呆**，不是产品限制：本项目的诊断包是 KB 级（真机那次 8305 B）。
MAX_BUNDLE_BYTES = 32 * 1024 * 1024      # 上传上来的 zip 本身
MAX_MEMBER_BYTES = 32 * 1024 * 1024      # 单个成员解压后的大小（按 zip 里声明的值判）
MAX_TOTAL_BYTES = 64 * 1024 * 1024       # 所有成员解压后的总和

# 只认这些名字：就是我们自己打包时写进去的那些（`BUNDLE_ORDER` 一处定义）。
ALLOWED_ENTRIES = frozenset(BUNDLE_ORDER)
# 包里必须有的：没有 manifest 就不知道这是哪一次会话。
REQUIRED_ENTRIES = ("manifest.json",)

IMPORT_NOTES = (
    "这是**别人给的包**（origin=import）：里面的字不是本机采的，包里也没有本机的时间线。",
    "落盘的是包内的**原样字节**：不解码、不改写、不补空文件。",
    "包里没有的东西（例如会话没有解码所以没有 decoded.log）就是没有，不是被省略了。",
)


class ImportRejected(ValueError):
    """这个包不能入库（原因要说得出、要能给人看）。"""


def _parse_json(data: bytes | None) -> Any:
    """字节 → JSON；读不出来就返回 None（调用方自己去判断“没有”与“坏了”的区别）。"""
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None


def _safe_entry_name(name: str) -> str:
    """压缩包里的条目名 → 可接受的裸文件名，否则拒绝。

    ⚠️ 这是整个模块里最要紧的一段：压缩包里的名字是**别人写的**。
    只要允许一个 `/`、`..` 或盘符，解包就可能写到会话目录外面去。
    """
    text = str(name or "")
    if not text or text.endswith("/"):
        raise ImportRejected(f"包里有一个目录条目或空名字：{text!r}（我们的包只有文件）")
    if "/" in text or "\\" in text:
        raise ImportRejected(f"包里的条目带了路径：{text!r}（只接受裸文件名）")
    if text in (".", "..") or text.startswith("."):
        raise ImportRejected(f"包里的条目是个隐藏名或不安全的名字：{text!r}")
    if re.match(r"^[A-Za-z]:", text):
        raise ImportRejected(f"包里的条目像绝对路径：{text!r}")
    if text not in ALLOWED_ENTRIES:
        allowed = "、".join(sorted(ALLOWED_ENTRIES))
        raise ImportRejected(
            f"包里有一个我们不认识的条目：{text!r}；只接受我们自己打的那些名字（{allowed}）"
            " —— 不认识的条目说明这个包不是这份工具生成的，所以整个包都不收"
        )
    return text


def verify_declared_hashes(
    info: dict[str, Any] | None, digests: dict[str, str]
) -> tuple[bool, list[str], list[str]]:
    """包自己声明的 sha256 与**已算好的**哈希对照（P3 起流式导入共用这一处）。

    返回 `(verified, checks, warnings)`：
    - `verified=True` 表示**声明过的每一份**都对上了；
    - 没有声明（没有 session.json）时 `verified=False` + 一条 warning（"没法核对"不是"核对通过"）。

    ⚠️ 入参是 `{名字: sha256}` 而**不是字节**：流式导入那边解压时顺手就把哈希算出来了，
    不该为了核对再把整份成员读回内存（PACK-IMPORT-03）。这一处只做"声明 ↔ 事实"的比对，
    消息文本与旧路径逐字一致（PACK-COMPAT-01：小包与旧包的提示不变）。
    """
    checks: list[str] = []
    warnings: list[str] = []
    if not info:
        warnings.append(
            f"包里没有 {BUNDLE_INFO_NAME}：没有可核对的 sha256，只能确认它是个结构合法的包"
        )
        return False, checks, warnings
    declared = info.get("files")
    if not isinstance(declared, list) or not declared:
        warnings.append(f"{BUNDLE_INFO_NAME} 里没有 files 列表：没有可核对的 sha256")
        return False, checks, warnings
    for row in declared:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name not in digests:
            # 声明了但包里没有：这不是"坏"，是"不完整" —— 如实说，不当成校验失败。
            warnings.append(f"{BUNDLE_INFO_NAME} 声明了 {name}，但包里没有这份文件")
            continue
        expected = str(row.get("sha256") or "")
        if not expected:
            warnings.append(f"{name} 在 {BUNDLE_INFO_NAME} 里没有写 sha256，没法核对")
            continue
        actual = str(digests[name])
        if actual != expected:
            raise ImportRejected(
                f"包里 {name} 的 sha256 与它自己声明的不一致"
                f"（声明 {expected[:12]}…，实际 {actual[:12]}…）：这个包在传输或保存过程中坏了"
            )
        checks.append(f"{name} sha256 与包内声明一致（{actual[:12]}…）")
    return bool(checks), checks, warnings


def _verify_declared_hashes(
    info: dict[str, Any] | None, artifacts: dict[str, bytes]
) -> tuple[bool, list[str], list[str]]:
    """字节版（旧路径）：先把成员哈希算出来，再交给上面那一份比对。"""
    digests = {name: hashlib.sha256(payload).hexdigest() for name, payload in artifacts.items()}
    return verify_declared_hashes(info, digests)


def inspect_bundle(data: bytes) -> dict[str, Any]:
    """只检查、不落盘（纯函数：同样一份字节永远得到同一个结论）。

    通过时返回：

    ```text
    {"session_id":…, "artifacts": {name: bytes}, "declared": session.json 的内容 | None,
     "verified": bool, "checks": [...], "warnings": [...]}
    ```
    """
    if not data:
        raise ImportRejected("上传的内容是空的")
    if len(data) > MAX_BUNDLE_BYTES:
        raise ImportRejected(
            f"包太大：{len(data)} B（上限 {MAX_BUNDLE_BYTES} B）—— 本项目的诊断包是 KB 级"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ImportRejected(f"这不是一个 zip：{error}") from None

    artifacts: dict[str, bytes] = {}
    total = 0
    for entry in archive.infolist():
        name = _safe_entry_name(entry.filename)
        if entry.file_size > MAX_MEMBER_BYTES:
            raise ImportRejected(
                f"{name} 声明解压后 {entry.file_size} B，超过上限 {MAX_MEMBER_BYTES} B"
            )
        total += entry.file_size
        if total > MAX_TOTAL_BYTES:
            raise ImportRejected(f"包内文件解压后总计超过上限 {MAX_TOTAL_BYTES} B")
        payload = archive.read(entry)
        if len(payload) != entry.file_size:
            raise ImportRejected(
                f"{name} 的实际字节数（{len(payload)}）与包内声明（{entry.file_size}）不一致"
            )
        artifacts[name] = payload

    missing = [name for name in REQUIRED_ENTRIES if name not in artifacts]
    if missing:
        raise ImportRejected(
            f"包不完整：缺少 {'、'.join(missing)}（没有 manifest.json 就不知道这是哪一次会话）"
        )

    manifest = _parse_json(artifacts["manifest.json"])
    if not isinstance(manifest, dict) or not manifest.get("session_id"):
        raise ImportRejected("manifest.json 不是合法 JSON，或里面没有 session_id")
    session_id = str(manifest["session_id"])

    declared = _parse_json(artifacts.get(BUNDLE_INFO_NAME))
    if isinstance(declared, dict):
        declared_id = declared.get("session_id")
        if declared_id and str(declared_id) != session_id:
            raise ImportRejected(
                f"这个包自己说的会话号（{declared_id}）与 manifest 里的（{session_id}）不一致"
                " —— 不猜哪个对，整个包不收"
            )
    verified, checks, warnings = _verify_declared_hashes(
        declared if isinstance(declared, dict) else None, artifacts
    )
    return {
        "session_id": session_id,
        "artifacts": artifacts,
        "declared": declared if isinstance(declared, dict) else None,
        "verified": verified,
        "checks": checks,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ 落盘
# 上面那份 `inspect_bundle()` 只管检查；下面才开始动磁盘。
def unique_session_id(root: Path, session_id: str) -> str:
    """导入目录里的会话号：`<原名>-imported`，重名就 `-imported-2`……"""
    base = f"{session_id}{IMPORT_SUFFIX}"
    candidate = base
    counter = 2
    while (Path(root) / candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def import_bundle(
    data: bytes,
    *,
    imports_dir: Path,
    filename: str = "",
    imported_utc: str | None = None,
) -> dict[str, Any]:
    """把一个诊断包放进导入目录，并写下它是怎么来的。

    ⚠️ 与读写侧的**同一套路径规则**：目录名先过 `artifact_path()` 的校验，
    文件按 `SESSION_ARTIFACTS` 白名单逐个写 —— 这里不新造第二套路径规则。
    """
    inspected = inspect_bundle(data)
    root = Path(imports_dir)
    root.mkdir(parents=True, exist_ok=True)
    session_id = unique_session_id(root, inspected["session_id"])
    # 先让路径规则把会话号与目录关系验一遍（越界在这里就被拒）。
    directory = artifact_path(root, session_id, "manifest.json").parent
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in inspected["artifacts"].items():
        if name not in SESSION_ARTIFACTS:
            continue          # README.txt / session.json 进的是 import.json 里的原样副本
        target = artifact_path(root, session_id, name)
        target.write_bytes(payload)

    record = {
        "schema": 1,
        "kind": "workbench-observe-import",
        "imported_utc": imported_utc or utc_now(),
        "imported_from": filename or "(未记录文件名)",
        # 上传那份 zip 的哈希：以后想核对"是不是同一个包"，看这个值就够了。
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
        "bundle_bytes": len(data),
        "session_id": session_id,
        "origin_session_id": inspected["session_id"],
        "verified": inspected["verified"],
        "checks": inspected["checks"],
        "warnings": inspected["warnings"],
        # 包里那份 session.json / README.txt **原样**存下来：它们是包自己的说明，
        # 不适合直接当证据，但少了它们就没法解释"这个包声称自己是什么"。
        "declared": inspected["declared"],
        "notes": list(IMPORT_NOTES),
    }
    (directory / IMPORT_RECORD_NAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {
        "session_id": session_id,
        "origin_session_id": inspected["session_id"],
        "imported_utc": record["imported_utc"],
        "verified": inspected["verified"],
        "checks": inspected["checks"],
        "warnings": inspected["warnings"],
        "imports_dir": str(root),
    }

"""**通用链路断言**（core， / R2c）：构建身份、采集完整性、身份匹配、解码完整性。

这一组回答的是"这次捕获在**链路**上站不站得住"，与设备在采什么数据无关：

| 断言 | 判的是 |
| --- | --- |
| A1 明文启动横幅完整 | 捕获从完整横幅开始（折断的横幅说明我们自己复位了两次） |
| A2 窗口内只复位一次 | 没有反复重启 |
| A3 启动记录解码出约定的协议版本 | 官方 detokenizer + 协议版本 |
| A4 启动记录只出现一次 | 复位后只进一次 setup |
| A5 没有残留 token 行 | 字典与固件匹配（解码完整性） |
| A7 token 与普通日志混流保留 | 明文没有被吃掉 |
| A8 本次运行可追溯到唯一构建 | git / 上游 tokenizer / ELF / 字典哈希齐全 |
| A9 字典包含固件声明的格式串 | 字典与固件同源 |
| A23 用自己那个构建的字典解码 | 横幅里的 build id 与所选字典一致 |

⚠️ **这一组里没有任何项目/平台的字符串**（ 的边界：core 不许出现
`dht`/`littlefs`/`esp32`/固定串口名）。它需要的文本知识全部由**注入的
`LinkContract`** 给出 —— 那些模式串、期望格式串与协议版本属于**项目包**
（`assertions.py` 里的 示例项目 契约），core 只按它们判"符不符合"。

这正是 R2c 想钉的事：**平台的断言逻辑可以不依赖某个项目包存在而独立运行**；
没有项目包时这一组照样跑（只是报告的组列表里少了项目那一组）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Pattern

try:  # imported as a package (``webconsole.link_assertions``)
    from .assertion_groups import contract_result, result
except ImportError:  # executed as a plain script
    from assertion_groups import contract_result, result  # type: ignore[no-redef]


@dataclass(frozen=True)
class LinkContract:
    """判"链路"这件事所需要的、**属于项目/设备**的文本知识。

    它是一张纯数据的表：装配处（项目包）给什么，链路断言就按什么判。
    没有它的地方一律**不猜** —— 缺哪一项，对应的断言就说"没有契约，判不了"。
    """

    #: 明文启动横幅的开头（ROM 打印的那一行）。
    boot_banner_prefix: str = ""
    #: 复位行（`rst:0x…`）：数窗口内复位了几次。
    reset_line: Pattern[str] | None = None
    #: 启动记录（3 个捕获组：protocol / reset_reason / level）。
    boot_record: Pattern[str] | None = None
    #: 约定好的协议版本。
    boot_protocol_version: int = 0
    #: 明文启动横幅（命名捕获组：build / protocol / fw）—— 身份与选库的根据。
    boot_banner: Pattern[str] | None = None
    #: "还留着没解码"的 token 行形态。
    token_line: Pattern[str] | None = None
    #: 解码后应当出现的应用记录形态（用来数"还原了几条"）。
    decoded_record: Pattern[str] | None = None
    #: 必须与 token 混流保留的明文（`(标记, 人话名)`）。
    plain_text_markers: tuple[tuple[str, str], ...] = field(default=())
    #: 字典里必须存在的格式串：`(套件名或空串, 格式串…)`；空串表示"所有套件"。
    format_strings: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())
    #: 这个目标的链路里**根本没有**哪些链路断言（按断言号前缀给，例如 `("A2", "A3")`）。
    #: ⚠️ 它不等于"缺项"：缺项是**这个项目忘了声明**（那种一定判失败，见 `blocked()`）；
    #: 而这里是"这个目标的协议里压根没有那个概念"（裸机固件没有 ROM 复位行、
    #: 也没有 ESP-IDF 的启动记录 ⇒ A2/A3/A4 无从判起）。默认空 —— 参考目标一行不变。
    not_applicable: tuple[str, ...] = field(default=())
    #: 捕获是否**必须**以启动横幅开头。参考目标会主动复位（DTR/RTS）所以要求它；
    #: 第二目标刻意**不复位**（复位要走 SWD 附加），捕获可能从半路开始（心跳已经在下）。
    banner_leads_capture: bool = True

    def strings_for(self, suite: str) -> tuple[str, ...]:
        """这个套件适用的全部格式串（通用那组 + 该套件多出来的那些）。"""
        out: list[str] = []
        for name, strings in self.format_strings:
            if not name or name == suite:
                out.extend(strings)
        return tuple(out)

    def gaps(self) -> list[str]:
        """这份契约缺了哪些项（缺什么就判不了什么，**不静默跳过**）。"""
        missing: list[str] = []
        for name, value in (
            ("boot_banner_prefix", self.boot_banner_prefix),
            ("reset_line", self.reset_line),
            ("boot_record", self.boot_record),
            ("boot_banner", self.boot_banner),
            ("token_line", self.token_line),
            ("decoded_record", self.decoded_record),
            ("boot_protocol_version", self.boot_protocol_version),
            ("plain_text_markers", self.plain_text_markers),
            ("format_strings", self.format_strings),
        ):
            if not value:
                missing.append(name)
        return missing


def _suite_of(manifest: Mapping[str, Any] | None) -> str:
    """这次 run 跑的是哪个套件（读 manifest，不猜）。"""
    run = (manifest or {}).get("run") or {}
    return str(run.get("suite") or "")


def evaluate(
    *,
    decoded: bytes,
    manifest: Mapping[str, Any],
    dictionary_text: str,
    observe_seconds: float,
    contract: LinkContract,
    events: Iterable[Any] | None = None,   # 链路这一组不用结构化事件（保留签名一致）
    **_ignored: Any,
) -> list[dict[str, Any]]:
    """链路那一组的断言（A1–A5、A7–A9、A23）。"""
    text = decoded.decode("utf-8", "replace")
    suite = _suite_of(manifest)
    results: list[dict[str, Any]] = []
    missing_items = set(contract.gaps())

    def blocked(*names: str) -> str | None:
        """这一条需要的契约项缺了没有 —— 缺了就**判失败**，绝不允许静默通过。

        ⚠️ 一个"因为没声明所以找不到问题"的判据比没有判据更糟（它会让人以为查过了）。
        """
        absent = [name for name in names if name in missing_items]
        if not absent:
            return None
        return "没用项目的文本契约：判不了（缺 " + "、".join(absent) + "）"

    # --- A1: the device actually rebooted inside the observation window -----
    #
    # 顺便管一件“捕获卫生”的事：raw.bin 不应当以**截断的**启动横幅开头。实测形态是
    # 横幅开头 + 一个半字符 + 完整的横幅 —— 成因是我们自己的两次复位（先赋 DTR
    # 再赋 RTS，各自都经过自动复位电路）把第一次启动的横幅从中间截断了。
    # 它不影响任何其他断言，但读 raw 的人会以为捕获坏了；所以这里把它说出来。
    banner_count = text.count(contract.boot_banner_prefix) if contract.boot_banner_prefix else 0
    problems: list[str] = []
    head = text.splitlines()[0] if text.splitlines() else ""
    if contract.banner_leads_capture and banner_count >= 1 and not head.startswith(
        contract.boot_banner_prefix
    ):
        if contract.boot_banner_prefix in head:
            cut = head.index(contract.boot_banner_prefix)
            problems.append(
                f"捕获开头有 {cut} 字节的折断启动横幅（{head[:cut]!r}）—— "
                "这是复位做了两次、第一次的横幅被第二次从中间截断留下的，不是设备故障；"
                "raw.bin 应当从完整横幅开始"
            )
        else:
            problems.append(
                f"捕获的第一行不是启动横幅，而是 {head[:40]!r}："
                "要么捕获窗口错过了启动，要么开头混进了别的字节"
            )
    results.append(
        result(
            "A1-boot-banner-captured",
            f"观察窗口（{observe_seconds:.0f} s）内捕获到 Boot 启动横幅"
            + ("，且捕获从完整横幅开始（开头没有折断的碎片）" if contract.banner_leads_capture else ""),
            ">= 1 次，且第一行就是横幅",
            (
                blocked_reason
                if (blocked_reason := blocked("boot_banner_prefix"))
                else f"{banner_count} 次；首行 {head[:24]!r}"
                + ("；" + "；".join(problems) if problems else "")
            ),
            banner_count >= 1 and not problems and not blocked("boot_banner_prefix"),
            "decoded.log",
        )
    )

    # --- A2: exactly one reset, so the capture is a single clean boot -------
    reset_count = len(contract.reset_line.findall(text)) if contract.reset_line else 0
    results.append(
        result(
            "A2-single-reset-in-window",
            "观察窗口内只发生一次复位（没有反复重启）",
            "1",
            blocked("reset_line") or str(reset_count),
            reset_count == 1 and not blocked("reset_line"),
            "decoded.log",
        )
    )

    # --- A3: the boot record decoded to the expected protocol version -------
    boot_records = contract.boot_record.findall(text) if contract.boot_record else []
    boot_detail = ", ".join(
        f"protocol={protocol} reset_reason={reason} level={level}"
        for protocol, reason, level in boot_records
    )
    results.append(
        result(
            "A3-boot-protocol-decoded",
            "启动记录被官方 detokenizer 还原且协议版本正确",
            f"[I][boot] protocol={contract.boot_protocol_version}",
            blocked("boot_record", "boot_protocol_version")
            or (boot_detail or "未找到任何 [I][boot] 记录"),
            any(
                protocol == str(contract.boot_protocol_version)
                for protocol, _, _ in boot_records
            )
            and not blocked("boot_record", "boot_protocol_version"),
            "decoded.log",
        )
    )

    # --- A4: exactly one boot record (no duplication / re-entry) ------------
    results.append(
        result(
            "A4-boot-record-single",
            "启动记录只出现一次（复位后只进入一次 setup）",
            "1",
            blocked("boot_record") or str(len(boot_records)),
            len(boot_records) == 1 and not blocked("boot_record"),
            "decoded.log",
        )
    )

    # --- A5: no token line survived decoding --------------------------------
    rogue = (
        [line for line in text.splitlines() if contract.token_line.fullmatch(line.strip())]
        if contract.token_line
        else []
    )
    record_count = len(contract.decoded_record.findall(text)) if contract.decoded_record else 0
    results.append(
        result(
            "A5-no-unresolved-tokens",
            "所有 $ 前缀 Token 行都被解码（字典与固件匹配）",
            "0 行残留",
            blocked("token_line")
            or (
                f"{len(rogue)} 行残留"
                + (f": {rogue[0]!r}" if rogue else "")
                + f"；已还原应用日志 {record_count} 条"
            ),
            not rogue and not blocked("token_line"),
            "decoded.log",
        )
    )

    # --- A7: plain system logs still share the link -------------------------
    missing = [
        name for marker, name in contract.plain_text_markers if marker not in text
    ]
    results.append(
        result(
            "A7-system-text-preserved",
            "Token 与普通系统日志混流保留",
            "全部存在：" + "、".join(name for _, name in contract.plain_text_markers),
            blocked("plain_text_markers")
            or ("全部存在" if not missing else "缺失：" + "、".join(missing)),
            not missing and not blocked("plain_text_markers"),
            "decoded.log",
        )
    )

    # --- A8: the run can be traced back to one exact build ------------------
    repo = manifest.get("repo", {})
    firmware = manifest.get("firmware", {})
    identity = {
        "git_commit": bool(repo.get("git_commit")),
        "upstream_commit": bool(repo.get("upstream_commit")),
        "elf_sha256": bool(firmware.get("elf_sha256")),
        "dictionary_sha256": bool(firmware.get("dictionary_sha256")),
    }
    missing_identity = [name for name, present in identity.items() if not present]
    results.append(
        result(
            "A8-build-identity-recorded",
            "本次运行可追溯到唯一构建身份",
            "git / 上游 tokenizer / ELF / 字典哈希齐全",
            "齐全" if not missing_identity else "缺失：" + "、".join(missing_identity),
            not missing_identity,
            "manifest.json",
        )
    )

    # --- A9: firmware and dictionary are in sync ----------------------------
    # The stress suite compiles four extra format strings, so what "in sync" means
    # depends on the suite -- and the expectation is spelled out per suite rather
    # than loosened for everybody.
    expected_strings = contract.strings_for(suite)
    absent = [s for s in expected_strings if s not in dictionary_text]
    results.append(
        result(
            "A9-dictionary-matches-firmware",
            "构建字典包含本套件固件声明的全部格式串",
            f"{len(expected_strings)} 条全部存在（{suite} 套件）",
            blocked("format_strings")
            or ("全部存在" if not absent else "缺失：" + "；".join(absent)),
            not absent and not blocked("format_strings"),
            "manifest.json",
        )
    )

    # --- A23: the capture was decoded with the dictionary of its own build ----
    # Phase 2's point: the build id the device announces (in plain text, because the
    # identity cannot be tokenized) must select the dictionary of that exact build.
    # If those two disagree, the decoded log is not what it claims to be.
    banner = contract.boot_banner.search(text) if contract.boot_banner else None
    device_build = banner.group("build").lower() if banner else None
    used = manifest.get("dictionary_used") or {}
    used_build = used.get("build_id32")
    used_hex = f"{used_build:08x}" if isinstance(used_build, int) else None
    a23_problems: list[str] = []
    if banner is None:
        a23_problems.append("捕获里没有明文 BOOT 横幅，无法确定构建身份")
    else:
        if banner.group("protocol") != str(contract.boot_protocol_version):
            a23_problems.append(
                f"横幅里的 protocol={banner.group('protocol')}"
                f" 与约定 {contract.boot_protocol_version} 不符"
            )
        if not banner.group("fw"):
            a23_problems.append("横幅里没有固件版本")
    if used_hex is None:
        a23_problems.append("manifest 没记录本次解码用的字典身份")
    elif device_build is not None and used_hex != device_build:
        a23_problems.append(f"字典身份与设备不符：设备 0x{device_build}，字典 0x{used_hex}")
    results.append(
        result(
            "A23-dictionary-matches-build",
            "捕获用自己那个构建的字典解码（身份由设备的明文横幅确定）",
            "横幅里的 build 与所选字典的 build_id32 一致，且 protocol/fw 存在",
            (
                "；".join(a23_problems)
                if a23_problems
                else f"设备 0x{device_build} = 字典 0x{used_hex}（{used.get('origin')}）"
            ),
            not a23_problems,
            "manifest.json",
        )
    )
    # 这个目标**根本没有**的那些断言（契约声明，不是“缺项”）：不产出那一行。
    # 必须与 `gaps()` 区分开：缺项判失败（项目忘了声明），不适用就不存在。
    # ⚠️ 比较的是**断言标识的头一段**（`A23-…` 的头是 `A23`），不是前缀：
    # 用 `startswith("A2")` 会把 A23 一起干掉 —— 实测就这么漏过一条（真机 run 里 A23 整行不见了）。
    if contract.not_applicable:
        excluded = {str(item) for item in contract.not_applicable}
        results = [
            row
            for row in results
            if str(row.get("id") or "").split("-", 1)[0] not in excluded
        ]
    return results


__all__ = ["LinkContract", "evaluate"]

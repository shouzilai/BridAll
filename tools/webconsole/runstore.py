"""Append-only run directories.

Every run owns one directory that must always contain the four mandatory
artifacts ``manifest.json``, ``raw.bin``, ``decoded.log`` and ``report.json``,
even when the run dies during precheck. Historical evidence is never
overwritten: a new run always gets a fresh unique directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

try:  # imported as a package (``webconsole.runstore``)
    from .core_primitives import utc_now
    from . import device_sniff
except ImportError:  # executed as a plain script
    from core_primitives import utc_now  # type: ignore[no-redef]
    import device_sniff  # type: ignore[no-redef]

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

MANDATORY_ARTIFACTS = ("manifest.json", "raw.bin", "decoded.log", "report.json")
EXTRA_ARTIFACTS = (
    "tool.log",
    "events.jsonl",
    "steps.jsonl",
    "app_events.jsonl",
    "metrics.jsonl",
    "panic.json",
    "records.bin",
    "records.json",
    "test_results.jsonl",
    "test_input.bin",
    "test_readback.bin",
)
KNOWN_ARTIFACTS = MANDATORY_ARTIFACTS + EXTRA_ARTIFACTS

#: 哪些证据是**原件**（设备/外部给的字节），哪些是**派生**（由原件算出来的）。
#: 派生不等于不重要 —— 它只是"原件没了、它也跟着没了"。页面要的是这层关系
#: （ / PERF-HIST-07：产物列表至少给出原件/派生关系）。
#: ⚠️ `manifest.json` / `report.json` 是**主机**写的记录，既不是设备字节也不是派生物，
#: 按"主机记录"归到派生一侧（它们确实是由这次运行派生出来的）。
ORIGINAL_ARTIFACTS = ("raw.bin", "records.bin", "test_input.bin", "test_readback.bin")

# What each file is *for*. This is what makes report.json a self-describing index:
# a reader (human or agent) should not have to guess which file is authoritative.
# A host test fails if an artifact is added to KNOWN_ARTIFACTS without a purpose.
EVIDENCE_PURPOSE = {
    "manifest.json": "身份与运行元信息：git/上游 tokenizer commit、工具版本、固件哈希与尺寸、发起者",
    "raw.bin": "一手证据：串口字节流（不是固件镜像）。观察期间逐行追加落盘",
    "decoded.log": "权威解码结果：decode 步骤由 raw.bin 重新推导并覆盖",
    "report.json": "结论与索引：结果、断言、每步耗时、证据清单、崩溃与基线摘要",
    "app_events.jsonl": "结构化应用事件（含字节偏移，可回到 decoded.log）",
    "metrics.jsonl": "指标序列（长格式：表头行 + 一行一个点）。它把'heap 是不是在持续下降'"
    "这类问题从“逐行看日志”变成一条可画、可导出、可核对的数据；单位与方向写在表头行里",
    "panic.json": "崩溃证据：故障行 + backtrace 地址 + 解码帧 + 可复现命令",
    "records.bin": "设备上记录文件的**原始字节**（由 DUMP BIN 取回，base64 重组 + CRC 核对）。"
    "设备自述“丢弃了几字节尾巴”是声明；这份字节才是证据",
    "records.json": "主机侧对 records.bin 的独立解析结果（头部、每条记录、可用前缀长度、问题原因）",
    "test_results.jsonl": "逐测试用例的结构化结果：设备结论、耗时、失败原因与主机核验证据",
    "test_input.bin": "主机协同测试的原始输入；大小与 SHA-256 同时冻结在 manifest.tests",
    "test_readback.bin": "设备从受控测试区读回的原始字节；由主机独立核对 CRC32 与 SHA-256",
    "events.jsonl": "编排事件流（页面顶栏那一路，含被拒请求）",
    "steps.jsonl": "每步状态迁移记录（含耗时）",
    # ⚠️ 这句话是给人读的证据说明，不能点名任何一套工具：`tool.log` 里装的是
    # **本次组合的构建/烧录适配器**跑出来的输出（参考组合是 PlatformIO，第二目标是
    # IAR 的 C-SPY）—— 写死一个工具名就是把平台假设刻进通用证据索引（）。
    "tool.log": "构建/烧录/复位等外部工具的完整输出，用于失败复盘",
}

_TEXT_ARTIFACTS = {"manifest.json", "decoded.log", "report.json", "tool.log",
                   "events.jsonl", "steps.jsonl", "app_events.jsonl", "metrics.jsonl",
                   "panic.json", "test_results.jsonl"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


class RunStore:
    def __init__(self, runs_dir: Path, suite_targets: Iterable[str] | None = None) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.match(run_id):
            raise ValueError(f"非法 run id: {run_id!r}")
        resolved = (self.runs_dir / run_id).resolve()
        if resolved.parent != self.runs_dir.resolve():
            raise ValueError(f"run id 越出 runs 目录: {run_id!r}")
        return resolved

    def artifact(self, run_id: str, name: str) -> Path:
        if name not in KNOWN_ARTIFACTS:
            raise ValueError(f"不暴露的证据文件: {name!r}")
        directory = self.path(run_id)
        target = (directory / name).resolve()
        if target.parent != directory.resolve():
            raise ValueError(f"证据路径非法: {name!r}")
        return target

    def is_text_artifact(self, name: str) -> bool:
        return name in _TEXT_ARTIFACTS

    # ----------------------------------------------------------------- create
    def create(self, suite: str, target: str, manifest: dict[str, Any]) -> str:
        stamp = utc_now().replace("-", "").replace(":", "").replace("T", "-")[:15]
        base = f"{stamp}-{suite}-{target}"
        run_id = base
        counter = 2
        while (self.runs_dir / run_id).exists():
            run_id = f"{base}-{counter}"
            counter += 1
        directory = self.runs_dir / run_id
        directory.mkdir(parents=True)
        # The four mandatory artifacts exist from the first second of the run, so
        # an interrupted run still leaves a complete, inspectable directory.
        # report.json starts as an INCOMPLETE placeholder and is rewritten by the
        # orchestrator at the end; if that never happens, the placeholder is the
        # honest record that this run produced no verdict.
        (directory / "raw.bin").write_bytes(b"")
        (directory / "decoded.log").write_bytes(b"")
        self.write_manifest(run_id, manifest)
        self.write_report(
            run_id,
            {
                "schema": 1,
                "run_id": run_id,
                "suite": suite,
                "target": target,
                "port": manifest.get("run", {}).get("port"),
                "started_utc": manifest.get("run", {}).get("created_utc"),
                "finished_utc": None,
                "duration_ms": None,
                "result": "INCOMPLETE",
                "failure_reason": "运行尚未结束（此文件是创建时的占位报告）",
                "steps": [],
                "assertions": [],
                "assertion_summary": {},
                "firmware": {},
                "artifacts": {},
            },
        )
        return run_id

    # ------------------------------------------------------------------ write
    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        self._write_json(self.artifact(run_id, "manifest.json"), manifest)

    def write_report(self, run_id: str, report: dict[str, Any]) -> None:
        self._write_json(self.artifact(run_id, "report.json"), report)

    def write_raw(self, run_id: str, data: bytes) -> None:
        self.artifact(run_id, "raw.bin").write_bytes(data)

    def write_decoded(self, run_id: str, data: bytes) -> None:
        self.artifact(run_id, "decoded.log").write_bytes(data)

    def write_artifact(self, run_id: str, name: str, data: bytes) -> None:
        """Write any whitelisted artifact (used for the structured event log)."""
        self.artifact(run_id, name).write_bytes(data)

    # ---------------------------------------------------------------- streaming
    def open_raw_sink(self, run_id: str):
        """Open ``raw.bin`` for incremental appends (truncating what was there).

        Evidence used to be written only after the observation window closed, so a
        crash, a ``taskkill`` or a power cut lost the entire capture -- precisely
        the runs whose evidence matters most. The caller writes and flushes per
        line; ``write_raw`` still exists for the authoritative rewrite.
        """
        return self.artifact(run_id, "raw.bin").open("wb")

    def open_decoded_sink(self, run_id: str):
        """Open ``decoded.log`` for incremental appends.

        This is a best-effort live view: ``_step_decode`` re-derives decoded.log
        from raw.bin afterwards, so the incremental content must never be treated
        as the authority -- but it is what survives an interrupted run.
        """
        return self.artifact(run_id, "decoded.log").open(
            "w", encoding="utf-8", errors="replace", newline="\n"
        )

    def evidence_index(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Every known artifact with its purpose, size and hash.

        Sorted by category so the report reads as an index rather than a directory
        listing: a reader should be able to tell the primary evidence from the
        process logs without opening anything.
        """
        rows: dict[str, dict[str, Any]] = {}
        for name in KNOWN_ARTIFACTS:
            path = self.artifact(run_id, name)
            if not path.is_file():
                continue
            rows[name] = {
                "purpose": EVIDENCE_PURPOSE.get(name, ""),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        return rows

    def append_tool(self, run_id: str, line: str) -> None:
        with self.artifact(run_id, "tool.log").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        with self.artifact(run_id, "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def append_step(self, run_id: str, step: dict[str, Any]) -> None:
        with self.artifact(run_id, "steps.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(step, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # ------------------------------------------------------------------- read
    def read_json(self, run_id: str, name: str) -> dict[str, Any] | None:
        path = self.artifact(run_id, name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def read_text(self, run_id: str, name: str) -> str:
        path = self.artifact(run_id, name)
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def review_report(self, run_id: str) -> dict[str, Any]:
        """Return a display projection without rewriting historical evidence."""
        report = dict(self.read_json(run_id, "report.json") or {})
        manifest = self.read_json(run_id, "manifest.json") or {}
        summary = report.get("test_summary") or {}
        derived = str(summary.get("result") or "")
        if report.get("result") == "INCOMPLETE" and derived in {
            "PASS", "FAIL", "ERROR", "CANCELLED"
        }:
            report["result"] = derived
            report["failure_reason"] = None if derived == "PASS" else summary.get("reason")
            report["result_projection"] = {
                "derived": True,
                "source": "report.test_summary.result",
                "stored_result": "INCOMPLETE",
            }
        selection = ((manifest.get("tests") or {}).get("selection") or [])
        host_scenario = any(
            ((row.get("case") or {}).get("kind") == "host_scenario")
            for row in selection if isinstance(row, dict)
        )
        raw_empty = self.artifact(run_id, "raw.bin").stat().st_size == 0
        decoded_empty = self.artifact(run_id, "decoded.log").stat().st_size == 0
        if host_scenario and raw_empty and decoded_empty:
            report["evidence_gaps"] = {
                "raw": "旧版主机场景未保存串口 RX；raw.bin 为 0 B，无法事后恢复",
                "decoded": "旧版主机场景未保存串口文本；decoded.log 为 0 B，无法事后恢复",
                "tool": "旧版主机场景未保存 TX/工具文本；请查看结构化事件窗口",
            }
            if report.get("result") == "INCOMPLETE" and not derived:
                report["result"] = "ERROR"
                report["failure_reason"] = "主机场景进程未正常收尾，且没有可回放的串口证据"
                report["result_projection"] = {
                    "derived": True,
                    "source": "legacy host-scenario evidence gap",
                    "stored_result": "INCOMPLETE",
                }
        return report

    def artifacts(self, run_id: str) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for name in KNOWN_ARTIFACTS:
            path = self.artifact(run_id, name)
            if path.is_file():
                rows[name] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        return rows

    def summaries(self, limit: int = 40) -> list[dict[str, Any]]:
        if not self.runs_dir.is_dir():
            return []
        ids = sorted(
            (
                entry.name
                for entry in self.runs_dir.iterdir()
                # A directory name alone is not a run.  Build tools and humans
                # may leave grouping/scratch directories below ``runs/`` (the
                # first real example was an empty ``runs/stm32`` directory).
                # ``create()`` writes all four mandatory artifacts before it
                # returns, so requiring that evidence contract keeps genuine
                # interrupted runs while excluding unrelated directories.
                if entry.is_dir()
                and RUN_ID_PATTERN.match(entry.name)
                and all((entry / name).is_file() for name in MANDATORY_ARTIFACTS)
            ),
            reverse=True,
        )[:limit]
        rows: list[dict[str, Any]] = []
        for run_id in ids:
            report = self.review_report(run_id)
            manifest = self.read_json(run_id, "manifest.json") or {}
            client = report.get("client") or manifest.get("client") or {}
            rows.append(
                {
                    "run_id": run_id,
                    "suite": report.get("suite") or manifest.get("run", {}).get("suite"),
                    "target": report.get("target") or manifest.get("run", {}).get("target"),
                    "port": report.get("port") or manifest.get("run", {}).get("port"),
                    "result": report.get("result") or ("interrupted" if manifest else "unknown"),
                    "started_utc": report.get("started_utc")
                    or manifest.get("run", {}).get("created_utc"),
                    "duration_ms": report.get("duration_ms"),
                    "has_report": bool(report),
                    "failure_reason": report.get("failure_reason"),
                    "client_label": client.get("label") or "",
                    "client_id": client.get("id") or "",
                    #  / D6：**这一趟自己记下的设备**（历史行也要能看出这件事）。
                    # ⚠️ 只读那一份 manifest（不拿当前嗅探结果/当前台架去改写历史）；
                    #    旧 run 如实标 `legacy=true`，`scan_seq` 留空（历史里没有这个数，不补）。
                    #    manifest 本来就在本循环里读（suite/target/port 都从它取），多这一行不多一次 IO。
                    "device": device_sniff.recorded_target(manifest).to_json(),
                    "artifact_names": list(self.artifacts(run_id)),
                }
            )
        return rows

"""Generic bounded host/device scenario runner ( / A4)."""
from __future__ import annotations

import hashlib
import json
import time
import zlib
from typing import Any, Callable

try:
    from .core_primitives import utc_now
    from .embedded_test_model import TestResult
    from .embedded_test_runner import LineTransport, TestRunError, _verify_listing
    from .runstore import RunStore
except ImportError:
    from core_primitives import utc_now  # type: ignore[no-redef]
    from embedded_test_model import TestResult  # type: ignore[no-redef]
    from embedded_test_runner import LineTransport, TestRunError, _verify_listing  # type: ignore[no-redef]
    from runstore import RunStore  # type: ignore[no-redef]


def _event(transport: LineTransport, protocol: Any, deadline: float,
           now: Callable[[], float], wanted: set[str], *, run_id: str,
           case_id: str, version: int) -> dict[str, Any]:
    while now() < deadline:
        line = transport.read_line(max(0.0, deadline - now()))
        if line is None:
            continue
        row = protocol.parse_line(line)
        if row is None:
            continue
        identity = (row.get("run_id"), row.get("case_id"), row.get("version"))
        expected = (run_id, case_id, version)
        if identity != expected:
            raise TestRunError(
                "拒绝不属于当前主机场景的设备事件："
                f"actual={identity!r} expected={expected!r} event={row.get('event')!r}"
            )
        if row.get("event") in wanted:
            return row
    raise TestRunError(f"等待设备事件 {sorted(wanted)} 超时")


def _persist_result(store: RunStore, run_id: str, result: TestResult) -> None:
    store.write_artifact(
        run_id, "test_results.jsonl",
        (json.dumps(result.to_json(), ensure_ascii=False) + "\n").encode("utf-8"),
    )
    report = store.read_json(run_id, "report.json") or {}
    report["tests"] = [result.to_json()]
    report["test_summary"] = {
        "total": 1, "passed": int(result.status == "PASS"), "result": result.status,
    }
    report["result"] = result.status
    report["failure_reason"] = None if result.status == "PASS" else result.reason
    report["finished_utc"] = utc_now()
    report["duration_ms"] = result.duration_ms
    store.write_report(run_id, report)


def _cancel_result(*, protocol: Any, transport: LineTransport, store: RunStore,
                   run_id: str, case: dict[str, Any], started: float,
                   now: Callable[[], float]) -> TestResult:
    delivery_error = ""
    try:
        transport.write_line(protocol.cancel_command(run_id))
    except Exception as error:  # Best effort: local cancellation evidence must survive a broken link.
        delivery_error = f"{type(error).__name__}: {error}"
    result = TestResult(
        case["id"], int(case["version"]), "CANCELLED",
        max(0, int((now() - started) * 1000)), "cancel requested",
    )
    store.append_event(run_id, {
        "kind": "autotest.cancelled", "run_id": run_id,
        "case_id": case["id"], "version": int(case["version"]),
        "reason": result.reason,
        "cancel_delivery_error": delivery_error or None,
    })
    _persist_result(store, run_id, result)
    return result


def _execute_image_roundtrip(
    *, protocol: Any, transport: LineTransport, store: RunStore, run_id: str,
    prepared: dict[str, Any], input_bytes: bytes, chunk_bytes: int = 8,
    cancelled: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
) -> TestResult:
    case, safety = prepared["case"], prepared.get("safety") or {}
    limits = safety.get("limits") or {}
    maximum = min(int(limits.get("input_bytes", 0)), int(limits.get("total_bytes", 0)))
    if not input_bytes or len(input_bytes) > maximum:
        raise TestRunError(f"输入大小 {len(input_bytes)} 超出安全上限 {maximum}")
    if chunk_bytes <= 0 or chunk_bytes > int(limits.get("chunk_bytes", 0)):
        raise TestRunError("分块大小超出安全策略")
    started = now()
    digest = hashlib.sha256(input_bytes).hexdigest()
    store.write_artifact(run_id, "test_input.bin", input_bytes)
    manifest = store.read_json(run_id, "manifest.json") or {}
    manifest["tests"] = {"schema": 1, "selection": [prepared], "input": {
        "artifact": "test_input.bin", "size": len(input_bytes), "sha256": digest,
    }}
    store.write_manifest(run_id, manifest)
    store.append_event(run_id, {"kind": "autotest.stage", "stage": "PREPARE_INPUT",
                                "size": len(input_bytes), "sha256": digest})
    _verify_listing(transport, protocol, 5.0, now)
    deadline = now() + float(case["timeout_s"])
    transport.write_line(protocol.begin_command(run_id, case["id"], case["version"], len(input_bytes), digest))
    identity = {"run_id": run_id, "case_id": case["id"], "version": int(case["version"])}
    _event(transport, protocol, deadline, now, {"START", "ERROR"}, **identity)
    store.append_event(run_id, {"kind": "autotest.stage", "stage": "SEND_CHUNKS"})
    for seq, offset in enumerate(range(0, len(input_bytes), chunk_bytes)):
        if cancelled():
            return _cancel_result(
                protocol=protocol, transport=transport, store=store, run_id=run_id,
                case=case, started=started, now=now,
            )
        block = input_bytes[offset:offset + chunk_bytes]
        transport.write_line(protocol.chunk_command(run_id, seq, offset, block))
        ack = _event(transport, protocol, deadline, now, {"ACK", "ERROR"}, **identity)
        if ack.get("event") != "ACK" or (ack.get("sequence"), ack.get("offset"), ack.get("length")) != (seq, offset, len(block)):
            raise TestRunError(
                f"ACK 序号/offset/长度不匹配：actual={ack!r} "
                f"expected={(seq, offset, len(block))!r}"
            )
        store.append_event(run_id, {"kind": "autotest.chunk", "direction": "write", "sequence": seq, "offset": offset, "length": len(block), "crc32": f"{zlib.crc32(block) & 0xffffffff:08x}"})
    if cancelled():
        return _cancel_result(
            protocol=protocol, transport=transport, store=store, run_id=run_id,
            case=case, started=started, now=now,
        )
    transport.write_line(protocol.commit_command(run_id))
    verified = _event(
        transport, protocol, deadline, now, {"DEVICE_VERIFY", "ERROR"}, **identity
    )
    if verified.get("event") != "DEVICE_VERIFY":
        raise TestRunError(str(verified))
    store.append_event(run_id, {"kind": "autotest.stage", "stage": "DEVICE_VERIFY",
                                "device": verified})
    readback = bytearray()
    for seq, offset in enumerate(range(0, len(input_bytes), chunk_bytes)):
        if cancelled():
            return _cancel_result(
                protocol=protocol, transport=transport, store=store, run_id=run_id,
                case=case, started=started, now=now,
            )
        length = min(chunk_bytes, len(input_bytes) - offset)
        transport.write_line(protocol.read_command(run_id, seq, offset, length))
        data = _event(transport, protocol, deadline, now, {"DATA", "ERROR"}, **identity)
        if data.get("event") != "DATA":
            raise TestRunError(f"设备拒绝 READ：{data!r}")
        try:
            block = bytes.fromhex(str(data["data"]))
        except (KeyError, ValueError) as error:
            raise TestRunError(f"DATA payload 非法：{error}") from None
        if (data.get("sequence"), data.get("offset"), len(block)) != (seq, offset, length):
            raise TestRunError("DATA 序号/offset/长度不连续")
        if int(str(data.get("crc32", "-1")), 16) != (zlib.crc32(block) & 0xffffffff):
            raise TestRunError("DATA CRC32 不匹配")
        readback.extend(block)
        store.append_event(run_id, {"kind": "autotest.chunk", "direction": "read",
                                    "sequence": seq, "offset": offset, "length": len(block),
                                    "crc32": str(data["crc32"])})
    store.write_artifact(run_id, "test_readback.bin", bytes(readback))
    read_digest = hashlib.sha256(readback).hexdigest()
    if bytes(readback) != input_bytes or read_digest != digest:
        raise TestRunError("主机独立 SHA-256/逐字节核对失败")
    if cancelled():
        return _cancel_result(
            protocol=protocol, transport=transport, store=store, run_id=run_id,
            case=case, started=started, now=now,
        )
    transport.write_line(protocol.complete_command(run_id, read_digest))
    terminal = _event(
        transport, protocol, deadline, now, {"PASS", "FAIL", "ERROR"}, **identity
    )
    status = str(terminal["event"])
    result = TestResult(case["id"], case["version"], status,
                        max(0, int((now() - started) * 1000)),
                        "" if status == "PASS" else str(terminal),
                        device_evidence=terminal,
                        host_evidence={"input_sha256": digest, "readback_sha256": read_digest,
                                       "bytes": len(input_bytes)})
    _persist_result(store, run_id, result)
    return result


def execute_image_roundtrip(**kwargs: Any) -> TestResult:
    """Run the scenario and always make a best-effort device cancellation on error."""
    try:
        return _execute_image_roundtrip(**kwargs)
    except Exception as error:
        try:
            kwargs["transport"].write_line(kwargs["protocol"].cancel_command(kwargs["run_id"]))
        except Exception:
            pass
        store = kwargs["store"]
        run_id = kwargs["run_id"]
        report = store.read_json(run_id, "report.json") or {}
        report["tests"] = []
        report["test_summary"] = {
            "total": 0, "passed": 0, "result": "ERROR", "reason": str(error)
        }
        report["result"] = "ERROR"
        report["failure_reason"] = str(error)
        report["finished_utc"] = utc_now()
        store.write_artifact(run_id, "test_results.jsonl", b"")
        store.write_report(run_id, report)
        raise


__all__ = ["execute_image_roundtrip"]

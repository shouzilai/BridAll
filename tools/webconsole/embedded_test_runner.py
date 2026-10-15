"""Generic device-autonomous embedded test runner ( / A2).

Core owns sequencing, timeouts, cancellation, identity correlation, and evidence.
All commands and device event semantics are injected by ``ProjectPackage``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Callable, Iterable, Protocol

try:
    from .embedded_test_model import TestRegion, TestResult, TestSelection
    from .project_package import ProjectPackage
    from .runstore import RunStore
except ImportError:
    from embedded_test_model import TestRegion, TestResult, TestSelection  # type: ignore[no-redef]
    from project_package import ProjectPackage  # type: ignore[no-redef]
    from runstore import RunStore  # type: ignore[no-redef]


TERMINAL_EVENTS = frozenset(("PASS", "FAIL", "ERROR", "CANCELLED"))


class LineTransport(Protocol):
    def write_line(self, line: str) -> None: ...
    def read_line(self, timeout_s: float) -> str | None: ...


class TestRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceTestRun:
    results: tuple[TestResult, ...]
    events: tuple[dict[str, Any], ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(row.status == "PASS" for row in self.results)


def _reason(event: dict[str, Any]) -> str:
    return str(event.get("reason") or event.get("detail") or event.get("message")
               or event.get("code") or event.get("event") or "device test failed")


def _read_event(transport: LineTransport, protocol: Any, remaining: float) -> dict[str, Any] | None:
    line = transport.read_line(max(0.0, remaining))
    if line is None:
        return None
    return protocol.parse_line(line)


def _verify_listing(transport: LineTransport, protocol: Any, timeout_s: float,
                    now: Callable[[], float]) -> list[dict[str, Any]]:
    transport.write_line(protocol.list_command())
    deadline = now() + timeout_s
    rows: list[dict[str, Any]] = []
    while now() < deadline:
        event = _read_event(transport, protocol, deadline - now())
        if event is None:
            continue
        if event.get("event") != "LIST":
            continue
        rows.append(event)
        if event.get("complete") is True:
            problems = protocol.validate_listing(rows)
            if problems:
                raise TestRunError("设备 LIST 与 Project Package 不一致：" + "；".join(problems))
            return rows
    raise TestRunError("等待设备 LIST complete 超时")


def _execute_device_tests(
    *, package: ProjectPackage, firmware_regions: Iterable[TestRegion],
    selections: Iterable[TestSelection], transport: LineTransport,
    store: RunStore, run_id: str, listing_timeout_s: float = 5.0,
    cancelled: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
) -> DeviceTestRun:
    """Validate, run, and persist device-owned cases without target knowledge."""
    protocol = package.test_protocol
    if protocol is None:
        raise TestRunError("Project Package 未提供设备测试协议")
    prepared = package.test_catalog.prepare(
        selections, suites=package.suites, firmware_regions=tuple(firmware_regions)
    )
    if not prepared:
        raise TestRunError("没有选择任何测试用例")

    manifest = store.read_json(run_id, "manifest.json") or {}
    manifest["tests"] = {
        "schema": 1,
        "catalog": package.test_catalog.to_json(),
        "selection": prepared,
    }
    store.write_manifest(run_id, manifest)

    all_events = _verify_listing(transport, protocol, listing_timeout_s, now)
    for event in all_events:
        store.append_event(run_id, {"kind": "autotest.device", "device": event})
    results: list[TestResult] = []
    for row in prepared:
        case = row["case"]
        case_id, version = case["id"], int(case["version"])
        started = now()
        transport.write_line(protocol.run_command(run_id, case_id, version, row["parameters"]))
        deadline = started + float(case["timeout_s"])
        terminal: dict[str, Any] | None = None
        while now() < deadline:
            if cancelled():
                transport.write_line(protocol.cancel_command(run_id))
            event = _read_event(transport, protocol, deadline - now())
            if event is None:
                continue
            if event.get("event") == "LIST":
                continue
            all_events.append(event)
            store.append_event(run_id, {"kind": "autotest.device", "device": event})
            event_run = event.get("run_id")
            event_case = event.get("case_id")
            if event_run != run_id or (event_case is not None and event_case != case_id):
                raise TestRunError(
                    f"拒绝不属于当前用例的设备事件：run={event_run!r} case={event_case!r}"
                )
            if event.get("version") not in (None, version):
                raise TestRunError(f"设备事件版本不匹配：{event.get('version')!r}")
            if event.get("event") in TERMINAL_EVENTS:
                terminal = event
                break
        duration_ms = max(0, int((now() - started) * 1000))
        if terminal is None:
            transport.write_line(protocol.cancel_command(run_id))
            result = TestResult(case_id, version, "ERROR", duration_ms, "设备用例超时")
        else:
            status = str(terminal["event"])
            result = TestResult(
                case_id, version, status, duration_ms,
                "" if status == "PASS" else _reason(terminal),
                device_evidence=terminal,
            )
        results.append(result)

    result_bytes = b"".join(
        (json.dumps(row.to_json(), ensure_ascii=False) + "\n").encode("utf-8")
        for row in results
    )
    store.write_artifact(run_id, "test_results.jsonl", result_bytes)
    report = store.read_json(run_id, "report.json") or {}
    report["tests"] = [row.to_json() for row in results]
    report["test_summary"] = {
        "total": len(results),
        "passed": sum(row.status == "PASS" for row in results),
        "result": "PASS" if all(row.status == "PASS" for row in results) else "FAIL",
    }
    store.write_report(run_id, report)
    return DeviceTestRun(tuple(results), tuple(all_events))


def execute_device_tests(
    *, package: ProjectPackage, firmware_regions: Iterable[TestRegion],
    selections: Iterable[TestSelection], transport: LineTransport,
    store: RunStore, run_id: str, listing_timeout_s: float = 5.0,
    cancelled: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
) -> DeviceTestRun:
    try:
        return _execute_device_tests(
            package=package, firmware_regions=firmware_regions,
            selections=selections, transport=transport, store=store, run_id=run_id,
            listing_timeout_s=listing_timeout_s, cancelled=cancelled, now=now,
        )
    except (TestRunError, ValueError) as error:
        store.write_artifact(run_id, "test_results.jsonl", b"")
        store.append_event(run_id, {"kind": "autotest.error", "reason": str(error)})
        report = store.read_json(run_id, "report.json") or {}
        report["tests"] = []
        report["test_summary"] = {
            "total": 0, "passed": 0, "result": "ERROR", "reason": str(error)
        }
        store.write_report(run_id, report)
        raise


__all__ = ["DeviceTestRun", "LineTransport", "TestRunError", "execute_device_tests"]

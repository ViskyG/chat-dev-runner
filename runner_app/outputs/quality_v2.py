"""Bounded test-report facts and quality decisions, separate from process/runner outcome."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import validate_relative_path
from runner_app.outputs.parsers import (
    MAX_CASES,
    MAX_PARSE_BYTES,
    ParseResult,
    _nonnegative_int,
    _tag,
    _xml_tree,
    parse_junit,
)

_COUNT_KEYS = ("tests", "passed", "failed", "errors", "skipped")
_MAX_REPORTS = 128
_MAX_REPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class QualityResult:
    quality_gate: str
    test_outcome: str
    counts: Mapping[str, int]
    findings: tuple[str, ...]
    runner_fault: bool = False


def _junit(raw: bytes, format_name: str) -> ParseResult:
    parsed = parse_junit(raw)
    if parsed.status != "PARSED":
        return parsed
    findings: list[str] = []
    try:
        root = _xml_tree(raw)
        for case in root.iter():
            if _tag(case) == "testcase":
                states = {_tag(child) for child in case} & {"failure", "error", "skipped"}
                if len(states) > 1:
                    raise ValueError("JUNIT_CASE_STATE_CONFLICT")
        for suite in root.iter():
            if _tag(suite) not in {"testsuite", "testsuites"}:
                continue
            cases = [case for case in suite.iter() if _tag(case) == "testcase"]
            actual = {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0}
            for case in cases:
                tags = {_tag(child) for child in case}
                actual["failures"] += "failure" in tags
                actual["errors"] += "error" in tags
                actual["skipped"] += "skipped" in tags
            for key, count in actual.items():
                declared = _nonnegative_int(suite.get(key))
                if declared is not None and declared != count:
                    raise ValueError("JUNIT_COUNTER_MISMATCH")
    except ValueError as exc:
        findings.append(str(exc))
    summary = dict(parsed.summary)
    summary["report_sha256"] = hashlib.sha256(raw).hexdigest()
    return ParseResult("test_result", format_name, "UNPARSED" if findings else "PARSED",
                       summary, tuple(findings))


def parse_trx(raw: bytes) -> ParseResult:
    """Keep original TRX evidence; normalize only direct, recognized test-result facts."""
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    failed_ids: list[str] = []
    try:
        root = _xml_tree(raw)
        if _tag(root) != "TestRun":
            raise ValueError("TRX_ROOT_INVALID")
        result_containers = [element for element in root if _tag(element) == "Results"]
        if len(result_containers) > 1:
            raise ValueError("TRX_RESULTS_AMBIGUOUS")
        results = list(result_containers[0]) if result_containers else []
        if len(results) > MAX_CASES:
            raise ValueError("TRX_CASE_LIMIT")
        identities: set[str] = set()
        outcome_counts: dict[str, int] = {}
        for result in results:
            if _tag(result) != "UnitTestResult":
                raise ValueError("TRX_RESULT_UNSUPPORTED")
            if any(_tag(child) == "UnitTestResult" for child in result.iter() if child is not result):
                raise ValueError("TRX_NESTED_RESULTS_UNSUPPORTED")
            identity = result.get("executionId")
            if not identity:
                raise ValueError("TRX_RESULT_ID_MISSING")
            if identity in identities:
                raise ValueError("DUPLICATE_REPORT_CASE")
            identities.add(identity)
            outcome = result.get("outcome", "")
            states = {"Passed": "passed", "Failed": "failed", "Error": "errors",
                      "Timeout": "errors", "Aborted": "errors", "NotExecuted": "skipped",
                      "NotRunnable": "skipped", "Skipped": "skipped"}
            if outcome not in states:
                raise ValueError("TRX_OUTCOME_UNKNOWN")
            counts["tests"] += 1
            counts[states[outcome]] += 1
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            if states[outcome] in {"failed", "errors"} and len(failed_ids) < 50:
                failed_ids.append(result.get("testName", "")[:240])
            duration = result.get("duration")
            if duration is not None and not re.fullmatch(
                    r"(?:[0-9]+\.)?[0-9]{2}:[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,7})?", duration):
                raise ValueError("TRX_DURATION_INVALID")
        for element in root.iter():
            if _tag(element) == "Counters":
                expected = {"total": counts["tests"], "executed": counts["tests"] - counts["skipped"],
                            "passed": counts["passed"], "failed": counts["failed"],
                            "error": outcome_counts.get("Error", 0),
                            "timeout": outcome_counts.get("Timeout", 0),
                            "aborted": outcome_counts.get("Aborted", 0),
                            "notExecuted": outcome_counts.get("NotExecuted", 0),
                            "notRunnable": outcome_counts.get("NotRunnable", 0)}
                for key, value in expected.items():
                    declared = _nonnegative_int(element.get(key))
                    if declared is not None and declared != value:
                        raise ValueError("TRX_COUNTER_MISMATCH")
            if _tag(element) == "ResultSummary":
                summary_outcome = element.get("outcome")
                if ((summary_outcome in {"Failed", "Error", "Aborted"}
                     and not counts["failed"] + counts["errors"])
                        or (summary_outcome == "Passed" and counts["failed"] + counts["errors"])):
                    raise ValueError("TRX_RUN_SUMMARY_CONFLICT")
        summary: dict[str, Any] = dict(counts)
        summary.update(failed_ids=failed_ids, counts_source="UnitTestResult-elements",
                       report_sha256=hashlib.sha256(raw).hexdigest())
        return ParseResult("test_result", "trx", "PARSED", summary, ())
    except ValueError as exc:
        return ParseResult("test_result", "trx", "UNPARSED", {}, (str(exc),))


def parse_test_report(raw: bytes, format_name: str) -> ParseResult:
    if format_name in {"junit", "junit-xml", "surefire-xml", "failsafe-xml"}:
        return _junit(raw, format_name)
    if format_name in {"trx", "trx-xml"}:
        return parse_trx(raw)
    return ParseResult("test_result", format_name, "UNSUPPORTED", {}, ("FORMAT_NOT_REGISTERED",))


def aggregate_maven_reports(reports: Mapping[str, bytes], *, phase: str | None = None) -> ParseResult:
    """Aggregate frozen XML files by module and Surefire/Failsafe phase, never summaries.

    A directory collector passes phase='surefire'/'failsafe' and TEST-*.xml relative
    names. A whole-workspace collector passes module/target/*-reports/TEST-*.xml.
    Duplicate suite identities in one module/phase are ambiguous, never summed.
    """
    if (phase not in {None, "surefire", "failsafe"} or len(reports) > _MAX_REPORTS
            or sum(len(raw) for raw in reports.values()) > _MAX_REPORT_BYTES):
        return ParseResult("test_result", "maven-junit-xml", "UNPARSED", {}, ("REPORT_BUDGET_EXCEEDED",))
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    phases: dict[str, dict[str, int]] = {}
    seen: set[tuple[str, str, str]] = set()
    seen_digests: set[tuple[str, str, str]] = set()
    findings: list[str] = []
    report_digests: list[str] = []
    accepted = 0
    for name, raw in sorted(reports.items()):
        try:
            validate_relative_path(name)
        except DomainError:
            findings.append("UNSAFE_REPORT_PATH")
            continue
        path = PurePosixPath(name)
        if not path.name.startswith("TEST-") or path.suffix != ".xml":
            # failsafe-summary.xml is a completion summary, not another test run.
            continue
        if len(raw) > MAX_PARSE_BYTES:
            findings.append("PARSER_BYTE_LIMIT")
            continue
        selected = phase
        module = str(path.parent)
        if selected is None:
            match = re.fullmatch(r"(?:(.*)/)?target/(surefire|failsafe)-reports/TEST-[^/]+\.xml", name)
            if not match:
                findings.append("MAVEN_REPORT_SCOPE_REQUIRED")
                continue
            module, selected = match.group(1) or ".", match.group(2)
        parsed = _junit(raw, f"{selected}-xml")
        if parsed.status != "PARSED":
            findings.extend(parsed.findings)
            continue
        root = _xml_tree(raw)
        suite = root.get("name") or path.name
        identity = (module, selected, suite)
        digest_identity = (module, selected, hashlib.sha256(raw).hexdigest())
        if identity in seen or digest_identity in seen_digests:
            findings.append("DUPLICATE_REPORT")
            continue
        seen.add(identity)
        seen_digests.add(digest_identity)
        accepted += 1
        phase_counts = phases.setdefault(selected, dict.fromkeys(_COUNT_KEYS, 0))
        for key in _COUNT_KEYS:
            count = parsed.summary[key]
            counts[key] += count
            phase_counts[key] += count
        report_digests.append(name + ":" + hashlib.sha256(raw).hexdigest())
        if counts["tests"] > MAX_CASES:
            return ParseResult("test_result", "maven-junit-xml", "UNPARSED", {}, ("CASE_BUDGET_EXCEEDED",))
    if not accepted:
        findings.append("TEST_REPORT_MISSING")
    summary: dict[str, Any] = dict(counts)
    summary.update(phases=phases, report_count=accepted, counts_source="module-phase-testcase-elements",
                   report_sha256=hashlib.sha256("\n".join(report_digests).encode()).hexdigest())
    return ParseResult("test_result", "maven-junit-xml", "UNPARSED" if findings else "PARSED",
                       summary, tuple(dict.fromkeys(findings))[:100])


def evaluate_quality(gate: Mapping[str, Any], reports: Mapping[str, ParseResult], *,
                     available_outputs: Collection[str], invalid_outputs: Collection[str] = (),
                     execution_outcomes: Sequence[str] = ()) -> QualityResult:
    """execution_outcomes contains TEST-PHASE process outcomes, not compiler/build exits.

    Missing/invalid outputs, unknown counters and contradictory process/report facts
    produce insufficient evidence. They never become a runner fault or invented tests.
    """
    findings: list[str] = []
    if len(reports) > _MAX_REPORTS:
        return QualityResult("INSUFFICIENT", "UNKNOWN", MappingProxyType({}),
                             ("REPORT_BUDGET_EXCEEDED",))
    required = set(gate["required_outputs"])
    missing = required - set(available_outputs)
    invalid = required & set(invalid_outputs)
    if missing:
        findings.append("REQUIRED_OUTPUT_MISSING")
    if invalid:
        findings.append("REQUIRED_OUTPUT_INVALID")
    counts: dict[str, int] = {}
    identities: set[str] = set()
    complete = 0
    for parsed in reports.values():
        if parsed.status != "PARSED" or parsed.kind != "test_result":
            findings.extend(parsed.findings or ("TEST_REPORT_UNPARSED",))
            continue
        identity = parsed.summary.get("report_sha256")
        if identity and identity in identities:
            findings.append("DUPLICATE_REPORT")
            continue
        if identity:
            identities.add(identity)
        if any(type(parsed.summary.get(key)) is not int or parsed.summary[key] < 0 for key in _COUNT_KEYS):
            findings.append("TEST_COUNTS_UNKNOWN")
            continue
        if sum(parsed.summary[key] for key in _COUNT_KEYS if key != "tests") != parsed.summary["tests"]:
            findings.append("TEST_COUNTER_MISMATCH")
            continue
        complete += 1
        for key in _COUNT_KEYS:
            counts[key] = counts.get(key, 0) + parsed.summary[key]
    if counts.get("tests", 0) > MAX_CASES:
        return QualityResult("INSUFFICIENT", "UNKNOWN", MappingProxyType({}),
                             ("CASE_BUDGET_EXCEEDED",))
    failed = counts.get("failed", 0) + counts.get("errors", 0)
    executed = counts.get("tests", 0) - counts.get("skipped", 0)
    outcome = "FAILED" if failed else "PASSED" if executed else "NOT_RUN"
    known_bad = any(item in {"FAILED", "TIMED_OUT", "CANCELLED"} for item in execution_outcomes)
    known_good = bool(execution_outcomes) and all(item == "PASSED" for item in execution_outcomes)
    unknown = any(item in {"NOT_RUN", "UNKNOWN"} for item in execution_outcomes)
    if complete and ((known_bad and not failed) or (known_good and failed) or unknown):
        findings.append("PROCESS_REPORT_INCONSISTENT")
        outcome = "UNKNOWN"
    if gate["test_required"]:
        if not complete:
            findings.append("TEST_REPORT_MISSING")
            outcome = "UNKNOWN" if reports else "NOT_RUN"
        if executed < max(1, gate["minimum_executed"]):
            findings.append("INSUFFICIENT_EXECUTED_TESTS")
    if findings:
        quality = "INSUFFICIENT"
    elif failed:
        quality = "FAILED"
    elif not gate["test_required"] and not complete:
        quality = "NOT_APPLICABLE"
    else:
        quality = "PASSED"
    return QualityResult(quality, outcome, MappingProxyType(counts), tuple(dict.fromkeys(findings))[:100])

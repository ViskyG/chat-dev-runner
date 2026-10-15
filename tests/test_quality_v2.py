from __future__ import annotations

import pytest

from runner_app.outputs.quality_v2 import (
    aggregate_maven_reports,
    evaluate_quality,
    parse_test_report,
    parse_trx,
)

GATE = {"test_required": True, "minimum_executed": 1, "required_outputs": ["tests"]}
PASSED = b'<testsuite name="suite" tests="1" failures="0"><testcase name="ok"/></testsuite>'
FAILED = b'<testsuite name="suite" tests="1" failures="1"><testcase name="bad"><failure/></testcase></testsuite>'
SKIPPED = b'<testsuite tests="1" skipped="1"><testcase name="expected"><skipped type="pytest.xfail"/></testcase></testsuite>'


def test_junit_actual_cases_and_expected_skip_semantics_are_preserved():
    parsed = parse_test_report(PASSED, "junit-xml")
    quality = evaluate_quality(GATE, {"tests": parsed}, available_outputs=["tests"],
                               execution_outcomes=["PASSED"])
    assert quality.quality_gate == quality.test_outcome == "PASSED"
    assert quality.counts["tests"] == quality.counts["passed"] == 1
    assert not quality.runner_fault
    skipped = evaluate_quality(GATE, {"tests": parse_test_report(SKIPPED, "junit-xml")},
                                available_outputs=["tests"], execution_outcomes=["PASSED"])
    assert skipped.quality_gate == "INSUFFICIENT"
    assert skipped.counts["skipped"] == 1 and skipped.counts["passed"] == 0


def test_known_test_failure_is_not_a_runner_failure():
    quality = evaluate_quality(GATE, {"tests": parse_test_report(FAILED, "junit-xml")},
                               available_outputs=["tests"], execution_outcomes=["FAILED"])
    assert quality.quality_gate == quality.test_outcome == "FAILED"
    assert not quality.runner_fault


@pytest.mark.parametrize("raw", [b'<testsuite tests="5"/>', b'<testsuite><testcase><failure/><skipped/></testcase></testsuite>',
                                 b'<testsuite><testcase>', b'<!DOCTYPE a [<!ENTITY x "bad">]><testsuite/>'])
def test_malformed_or_inconsistent_junit_never_fabricates_a_pass(raw):
    parsed = parse_test_report(raw, "junit-xml")
    assert parsed.status == "UNPARSED"
    quality = evaluate_quality(GATE, {"tests": parsed}, available_outputs=["tests"])
    assert quality.quality_gate == "INSUFFICIENT" and quality.test_outcome == "UNKNOWN"
    assert dict(quality.counts) == {}


def test_missing_required_outputs_no_tests_and_build_only_have_explicit_quality():
    quality = evaluate_quality(GATE, {}, available_outputs=[])
    assert quality.quality_gate == "INSUFFICIENT" and quality.test_outcome == "NOT_RUN"
    assert dict(quality.counts) == {}
    assert "REQUIRED_OUTPUT_MISSING" in quality.findings
    empty = parse_test_report(b'<testsuite tests="0"/>', "junit-xml")
    quality = evaluate_quality(GATE, {"tests": empty}, available_outputs=["tests"])
    assert quality.quality_gate == "INSUFFICIENT" and quality.counts["tests"] == 0
    gate = {"test_required": False, "minimum_executed": 0, "required_outputs": ["binary"]}
    assert evaluate_quality(gate, {}, available_outputs=["binary"]).quality_gate == "NOT_APPLICABLE"
    assert evaluate_quality(gate, {}, available_outputs=["binary"],
                            invalid_outputs=["binary"]).quality_gate == "INSUFFICIENT"


@pytest.mark.parametrize("raw,process", [(PASSED, "FAILED"), (FAILED, "PASSED"), (PASSED, "NOT_RUN")])
def test_process_report_disagreement_is_unknown_without_changing_process_facts(raw, process):
    quality = evaluate_quality(GATE, {"tests": parse_test_report(raw, "junit-xml")},
                               available_outputs=["tests"], execution_outcomes=[process])
    assert quality.quality_gate == "INSUFFICIENT" and quality.test_outcome == "UNKNOWN"
    assert "PROCESS_REPORT_INCONSISTENT" in quality.findings
    assert not quality.runner_fault


def test_maven_aggregates_module_and_phase_scopes_without_summing_summaries():
    reports = {"module-a/target/surefire-reports/TEST-suite.xml": PASSED,
               "module-b/target/surefire-reports/TEST-suite.xml": PASSED,
               "module-a/target/failsafe-reports/TEST-suite.xml": FAILED,
               "module-a/target/failsafe-reports/failsafe-summary.xml": b'<failsafe-summary/>'}
    parsed = aggregate_maven_reports(reports)
    assert parsed.status == "PARSED"
    assert parsed.summary["tests"] == 3
    assert parsed.summary["phases"]["surefire"]["tests"] == 2
    assert parsed.summary["phases"]["failsafe"]["failed"] == 1
    assert parsed.summary["report_count"] == 3
    scoped = aggregate_maven_reports({"TEST-suite.xml": PASSED}, phase="surefire")
    assert scoped.status == "PARSED" and scoped.summary["tests"] == 1


def test_maven_duplicate_scope_or_unknown_paths_are_insufficient():
    duplicate = aggregate_maven_reports({"TEST-suite.xml": PASSED, "TEST-copy.xml": PASSED}, phase="surefire")
    assert duplicate.status == "UNPARSED" and duplicate.summary["tests"] == 1
    assert "DUPLICATE_REPORT" in duplicate.findings
    assert aggregate_maven_reports({"../TEST-suite.xml": PASSED}, phase="surefire").status == "UNPARSED"
    assert aggregate_maven_reports({"TEST-suite.xml": PASSED}).status == "UNPARSED"
    assert aggregate_maven_reports({f"TEST-{i}.xml": PASSED for i in range(129)},
                                   phase="surefire").findings == ("REPORT_BUDGET_EXCEEDED",)


def trx(outcomes, *, total=None):
    results = ''.join(f'<UnitTestResult executionId="{i}" testName="test-{i}" outcome="{outcome}" duration="00:00:00.0010000"/>'
                      for i, outcome in enumerate(outcomes))
    counters = '' if total is None else f'<Counters total="{total}"/>'
    return f'<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>{results}</Results><ResultSummary outcome="Completed">{counters}</ResultSummary></TestRun>'.encode()


def test_trx_uses_actual_result_elements_and_preserves_failure_and_skip():
    parsed = parse_trx(trx(["Passed", "Failed", "NotExecuted"], total=3))
    assert parsed.status == "PARSED"
    assert parsed.summary["tests"] == 3 and parsed.summary["failed"] == 1
    assert parsed.summary["skipped"] == 1
    quality = evaluate_quality(GATE, {"tests": parsed}, available_outputs=["tests"],
                               execution_outcomes=["FAILED"])
    assert quality.quality_gate == "FAILED"


@pytest.mark.parametrize("raw", [trx(["Passed"], total=9), trx(["Inconclusive"]),
                                 b'<TestRun><Results><UnitTestResult outcome="Passed"/></Results></TestRun>',
                                 b'<TestRun><Results><UnitTestResult executionId="x" outcome="Passed"/><UnitTestResult executionId="x" outcome="Passed"/></Results></TestRun>',
                                 b'<TestRun><Results>', b'<!DOCTYPE a><TestRun/>'])
def test_unknown_trx_counts_or_structure_do_not_produce_a_pass(raw):
    parsed = parse_trx(raw)
    assert parsed.status == "UNPARSED"
    assert evaluate_quality(GATE, {"tests": parsed}, available_outputs=["tests"]).quality_gate == "INSUFFICIENT"


def test_duplicate_report_outputs_are_not_counted_twice():
    parsed = parse_test_report(PASSED, "junit-xml")
    quality = evaluate_quality(GATE, {"tests": parsed, "alias": parsed}, available_outputs=["tests"])
    assert quality.counts["tests"] == 1
    assert quality.quality_gate == "INSUFFICIENT" and "DUPLICATE_REPORT" in quality.findings

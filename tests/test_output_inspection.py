"""Only sealed, registered v1 output bytes may become bounded local UI facts."""
from __future__ import annotations

import json

from runner_app.outputs.inspection import inspect_registered_output, inspection_limit


def _manifest(kind: str, format_name: str, media_type: str, *, complete: bool = True) -> dict:
    return {"output_id": "result", "kind": kind, "format": format_name,
            "media_type": media_type, "complete": complete,
            "truncated_reason": None if complete else "file_limit", "size_bytes": 0}


def test_junit_cases_come_from_elements_and_trace_stays_bounded() -> None:
    raw = (b'<testsuite tests="999" failures="0"><testcase classname="a" name="ok"/>'
           b'<testcase classname="a" name="bad"><failure message="broken">trace</failure></testcase>'
           b'<testcase name="oops"><error>exception</error></testcase></testsuite>')
    result = inspect_registered_output(_manifest("test_result", "junit-xml", "application/xml"), raw)
    assert result["status"] == "PARSED"
    assert result["summary"]["tests"] == 3
    assert result["summary"]["failed"] == result["summary"]["errors"] == 1
    assert result["failed_cases"] == [
        {"id": "a::bad", "outcome": "FAILURE", "message": "broken", "trace": "trace"},
        {"id": "oops", "outcome": "ERROR", "message": "", "trace": "exception"},
    ]
    assert result["omitted_failed_cases"] == 0


def test_junit_dtd_incomplete_and_unknown_never_claim_counts() -> None:
    output = _manifest("test_result", "junit-xml", "application/xml")
    dtd = b'<!DOCTYPE testsuite [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><testsuite>&xxe;</testsuite>'
    assert inspect_registered_output(output, dtd)["status"] == "UNPARSED"
    assert "summary" not in inspect_registered_output(output, dtd)
    output["complete"] = False
    assert inspect_registered_output(output, b'<testsuite><testcase/></testsuite>')["status"] == "INCOMPLETE"
    assert "summary" not in inspection_limit(output)
    assert inspect_registered_output(_manifest("test_result", "custom", "text/plain"), b"abc")["status"] == "UNSUPPORTED"


def test_structured_error_filter_ignores_invalid_lines_and_bounds_messages() -> None:
    def event(level: str, message: str) -> bytes:
        return json.dumps({"timestamp": "2026-09-28T00:00:00Z", "level": level,
                           "service": "demo", "event": "test", "message": message}).encode()
    raw = event("INFO", "ok") + b"\n" + event("ERROR", "bad") + b"\ninvalid\n"
    result = inspect_registered_output(_manifest("application_log", "application-log/v1", "application/x-ndjson"), raw)
    assert result["status"] == "PARSED"
    assert result["summary"]["valid_events"] == 2
    assert result["summary"]["invalid_lines"] == 1
    assert [x["level"] for x in result["error_events"]] == ["ERROR"]
    assert result["error_events"][0]["message"] == "bad"
    assert result["omitted_error_events"] == 0


def test_raster_preview_requires_registered_kind_complete_bytes_and_matching_mime() -> None:
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + (800).to_bytes(4, "big") + (600).to_bytes(4, "big"))
    output = _manifest("screenshot", "png", "image/png")
    result = inspect_registered_output(output, png)
    assert result["safe_preview"] is True
    assert (result["width"], result["height"]) == (800, 600)
    output["media_type"] = "image/svg+xml"
    assert inspect_registered_output(output, png)["safe_preview"] is False
    output["media_type"] = "image/png"
    output["complete"] = False
    assert inspect_registered_output(output, png)["status"] == "INCOMPLETE"

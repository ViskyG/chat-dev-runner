from __future__ import annotations

import json
from pathlib import Path

from runner_app.outputs.parsers import (
    inspect_image,
    parse_application_log,
    parse_cobertura,
    parse_junit,
    parse_output,
)


def test_junit_uses_cases_not_untrusted_suite_totals() -> None:
    raw = (b'<testsuite tests="999" failures="0"><testcase classname="a" name="ok" time=".2"/>'
           b'<testcase classname="a" name="bad"><failure>oops</failure></testcase>'
           b'<testcase name="skip"><skipped/></testcase></testsuite>')
    parsed = parse_junit(raw)
    assert parsed.status == "PARSED"
    assert parsed.summary["tests"] == 3
    assert (parsed.summary["passed"], parsed.summary["failed"], parsed.summary["skipped"]) == (1, 1, 1)
    assert parsed.summary["failed_ids"] == ["a::bad"]
    assert parsed.summary["counts_source"] == "testcase-elements"


def test_xml_entities_depth_and_malformed_data_are_unparsed() -> None:
    dtd = b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><testsuite>&xxe;</testsuite>'
    assert parse_junit(dtd).findings == ("XML_DTD_FORBIDDEN",)
    assert parse_junit(b"<testsuite").findings == ("XML_PARSE_ERROR",)
    deep = b"<testsuite>" + b"<a>" * 40 + b"</a>" * 40 + b"</testsuite>"
    assert parse_junit(deep).findings == ("XML_DEPTH_LIMIT",)
    assert parse_cobertura(dtd).status == "UNPARSED"


def test_utf16_xml_doctype_and_entities_are_rejected_before_expansion() -> None:
    for root, parser in (("testsuite", parse_junit), ("coverage", parse_cobertura)):
        attribute = ' line-rate="0.5"' if root == "coverage" else ""
        document = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            f'<!DOCTYPE {root} [<!ENTITY injected "EXPANDED">]>'
            f'<{root}{attribute}><testcase name="&injected;"/></{root}>'
        ).encode("utf-16")
        parsed = parser(document)
        assert parsed.status == "UNPARSED"
        assert parsed.findings == ("XML_DTD_FORBIDDEN",)


def test_cobertura_only_uses_valid_root_totals() -> None:
    result = parse_cobertura(
        b'<coverage line-rate=".5" branch-rate=".25" lines-covered="5" lines-valid="10"/>'
    )
    assert result.status == "PARSED"
    assert result.summary["lines_covered"] == 5
    assert parse_cobertura(b'<coverage line-rate="NaN"/>').status == "UNPARSED"
    assert parse_cobertura(b'<coverage lines-covered="11" lines-valid="10"/>').status == "UNPARSED"


def test_jsonl_preserves_invalid_lines_as_findings_without_fabricated_events() -> None:
    valid = {
        "timestamp": "2026-09-27T00:00:00Z", "level": "INFO",
        "service": "toy", "event": "ready", "message": "hi",
    }
    raw = json.dumps(valid).encode() + b"\ninvalid\n" + b'{"level":"BOGUS"}\n'
    result = parse_application_log(raw)
    assert result.status == "PARSED"
    assert result.summary == {
        "valid_events": 1, "invalid_lines": 2, "levels": {"INFO": 1},
    }
    assert len(result.findings) == 2
    assert parse_application_log(b"x" * (2 * 1024 * 1024 + 1)).status == "UNPARSED"


def test_unknown_format_is_opaque_and_raw_is_untouched(tmp_path: Path) -> None:
    source = tmp_path / "unknown.out"
    source.write_bytes(b"not a known format")
    parsed = parse_output("test_result", "custom", source)
    assert parsed.status == "UNSUPPORTED" and parsed.summary == {}
    assert source.read_bytes() == b"not a known format"


def test_image_headers_bound_dimensions_without_decoding(tmp_path: Path) -> None:
    png = tmp_path / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                    + (12000).to_bytes(4, "big") + (5000).to_bytes(4, "big"))
    image = inspect_image(png)
    assert image.format == "png" and image.finding == "IMAGE_DIMENSION_LIMIT"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                    + (800).to_bytes(4, "big") + (600).to_bytes(4, "big"))
    assert inspect_image(png).safe_preview

"""Strict, bounded parsers for frozen untrusted output bytes.

A parser finding never changes a known process exit code or deletes raw evidence.
No project-defined deserialization hooks or external XML entities are used.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.resources import load_schema

MAX_PARSE_BYTES = 2 * 1024 * 1024
MAX_XML_ELEMENTS = 50_000
MAX_XML_DEPTH = 32
MAX_CASES = 10_000
MAX_JSONL_LINES = 10_000
MAX_JSONL_LINE_BYTES = 128 * 1024
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 16
MAX_FINDINGS = 100

_DTD = re.compile(br"<\s*!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ParseResult:
    kind: str
    format: str
    status: str  # PARSED, UNPARSED, UNSUPPORTED
    summary: dict[str, Any]
    findings: tuple[str, ...]


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_tree(raw: bytes) -> ET.Element:
    if len(raw) > MAX_PARSE_BYTES:
        raise ValueError("PARSER_BYTE_LIMIT")
    if _DTD.search(raw):
        raise ValueError("XML_DTD_FORBIDDEN")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("XML_PARSE_ERROR") from error
    stack = [(root, 1)]
    visited = 0
    while stack:
        element, depth = stack.pop()
        visited += 1
        if visited > MAX_XML_ELEMENTS:
            raise ValueError("XML_ELEMENT_LIMIT")
        if depth > MAX_XML_DEPTH:
            raise ValueError("XML_DEPTH_LIMIT")
        stack.extend((child, depth + 1) for child in element)
    return root


def _nonnegative_float(value: str | None, *, max_value: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError("INVALID_NUMERIC_FIELD") from error
    if not math.isfinite(parsed) or parsed < 0 or (max_value is not None and parsed > max_value):
        raise ValueError("INVALID_NUMERIC_FIELD")
    return parsed


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError("INVALID_NUMERIC_FIELD") from error
    if parsed < 0:
        raise ValueError("INVALID_NUMERIC_FIELD")
    return parsed


def parse_junit(raw: bytes) -> ParseResult:
    try:
        root = _xml_tree(raw)
        if _tag(root) not in ("testsuite", "testsuites"):
            raise ValueError("JUNIT_ROOT_INVALID")
        counts = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        failed_ids: list[str] = []
        duration = 0.0
        for element in root.iter():
            if _tag(element) != "testcase":
                continue
            counts["tests"] += 1
            if counts["tests"] > MAX_CASES:
                raise ValueError("JUNIT_CASE_LIMIT")
            states = {_tag(child) for child in element}
            if "error" in states:
                key = "errors"
            elif "failure" in states:
                key = "failed"
            elif "skipped" in states:
                key = "skipped"
            else:
                key = "passed"
            counts[key] += 1
            if key in ("errors", "failed") and len(failed_ids) < 50:
                classname = element.get("classname", "")[:120]
                name = element.get("name", "")[:120]
                failed_ids.append(f"{classname}::{name}" if classname else name)
            case_duration = _nonnegative_float(element.get("time"))
            if case_duration is not None:
                duration += case_duration
        summary: dict[str, Any] = dict(counts)
        summary["duration_seconds"] = round(duration, 6)
        summary["failed_ids"] = failed_ids
        summary["counts_source"] = "testcase-elements"
        return ParseResult("test_result", "junit-xml", "PARSED", summary, ())
    except ValueError as error:
        return ParseResult("test_result", "junit-xml", "UNPARSED", {}, (str(error),))


def parse_cobertura(raw: bytes) -> ParseResult:
    try:
        root = _xml_tree(raw)
        if _tag(root) != "coverage":
            raise ValueError("COBERTURA_ROOT_INVALID")
        line_rate = _nonnegative_float(root.get("line-rate"), max_value=1)
        branch_rate = _nonnegative_float(root.get("branch-rate"), max_value=1)
        lines_covered = _nonnegative_int(root.get("lines-covered"))
        lines_valid = _nonnegative_int(root.get("lines-valid"))
        branches_covered = _nonnegative_int(root.get("branches-covered"))
        branches_valid = _nonnegative_int(root.get("branches-valid"))
        if (lines_covered is not None and lines_valid is not None
                and lines_covered > lines_valid):
            raise ValueError("COBERTURA_TOTAL_INVALID")
        if (branches_covered is not None and branches_valid is not None
                and branches_covered > branches_valid):
            raise ValueError("COBERTURA_TOTAL_INVALID")
        if line_rate is None and lines_covered is None:
            raise ValueError("COBERTURA_TOTAL_MISSING")
        summary = {
            "line_rate": line_rate, "branch_rate": branch_rate,
            "lines_covered": lines_covered, "lines_valid": lines_valid,
            "branches_covered": branches_covered, "branches_valid": branches_valid,
            "counts_source": "coverage-root-attributes",
        }
        return ParseResult("coverage", "cobertura-xml", "PARSED", summary, ())
    except ValueError as error:
        return ParseResult("coverage", "cobertura-xml", "UNPARSED", {}, (str(error),))


def _json_shape(value: Any) -> bool:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, depth + 1) for v in current)
    return True


def parse_application_log(raw: bytes) -> ParseResult:
    if len(raw) > MAX_PARSE_BYTES:
        return ParseResult("application_log", "application-log/v1", "UNPARSED", {},
                           ("PARSER_BYTE_LIMIT",))
    lines = raw.splitlines()
    if len(lines) > MAX_JSONL_LINES:
        return ParseResult("application_log", "application-log/v1", "UNPARSED", {},
                           ("JSONL_LINE_LIMIT",))
    schema = load_schema("application-log")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    levels: dict[str, int] = {}
    valid = 0
    invalid = 0
    findings: list[str] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            invalid += 1
            if len(findings) < MAX_FINDINGS:
                findings.append(f"line:{index}:BLANK_LINE")
            continue
        if len(line) > MAX_JSONL_LINE_BYTES:
            invalid += 1
            if len(findings) < MAX_FINDINGS:
                findings.append(f"line:{index}:JSONL_LINE_BYTE_LIMIT")
            continue
        try:
            value = json.loads(line.decode("utf-8", errors="strict"),
                               parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            if not _json_shape(value):
                raise ValueError("JSON_DEPTH_OR_NODE_LIMIT")
            if not isinstance(value, dict) or not validator.is_valid(value):
                raise ValueError("APPLICATION_LOG_SCHEMA_INVALID")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            invalid += 1
            if len(findings) < MAX_FINDINGS:
                findings.append(f"line:{index}:INVALID_APPLICATION_LOG")
            continue
        valid += 1
        level = value["level"]
        levels[level] = levels.get(level, 0) + 1
    if invalid > len(findings):
        findings.append(f"FINDINGS_SUPPRESSED:{invalid - len(findings)}")
    return ParseResult(
        "application_log", "application-log/v1", "PARSED",
        {"valid_events": valid, "invalid_lines": invalid, "levels": levels}, tuple(findings),
    )


def parse_output(kind: str, format_name: str, source: Path,
                 *, max_bytes: int = MAX_PARSE_BYTES) -> ParseResult:
    """Parse a private frozen file; unknown formats remain opaque evidence."""
    parsers = {
        ("test_result", "junit-xml"): parse_junit,
        ("application_log", "application-log/v1"): parse_application_log,
        ("coverage", "cobertura-xml"): parse_cobertura,
    }
    parser = parsers.get((kind, format_name))
    if parser is None:
        return ParseResult(kind, format_name, "UNSUPPORTED", {}, ("FORMAT_NOT_REGISTERED",))
    if max_bytes < 1 or source.stat().st_size > min(max_bytes, MAX_PARSE_BYTES):
        return ParseResult(kind, format_name, "UNPARSED", {}, ("PARSER_BYTE_LIMIT",))
    try:
        return parser(source.read_bytes())
    except OSError:
        return ParseResult(kind, format_name, "UNPARSED", {}, ("PARSER_READ_ERROR",))


@dataclass(frozen=True)
class ImageInspection:
    format: str | None
    width: int | None
    height: int | None
    safe_preview: bool
    finding: str | None


def inspect_image(
    source: Path, *, max_dimension: int = 10_000,
    max_pixels: int = 40_000_000, max_header_bytes: int = 256 * 1024,
) -> ImageInspection:
    """Read only bounded image headers; never decode or render untrusted pixels."""
    if min(max_dimension, max_pixels, max_header_bytes) < 1:
        raise ValueError("Image inspection limits must be positive")
    try:
        with source.open("rb") as stream:
            raw = stream.read(max_header_bytes)
    except OSError:
        return ImageInspection(None, None, None, False, "IMAGE_READ_ERROR")
    kind: str | None = None
    width: int | None = None
    height: int | None = None
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        if raw[12:16] == b"IHDR":
            kind = "png"
            width = int.from_bytes(raw[16:20], "big")
            height = int.from_bytes(raw[20:24], "big")
    elif raw.startswith(b"\xff\xd8"):
        kind = "jpeg"
        position = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while position + 4 <= len(raw):
            if raw[position] != 0xFF:
                break
            marker = raw[position + 1]
            position += 2
            if marker in (0xD8, 0xD9):
                continue
            if marker == 0xDA:
                break
            segment_length = int.from_bytes(raw[position:position + 2], "big")
            if segment_length < 2 or position + segment_length > len(raw):
                break
            if marker in sof_markers and segment_length >= 7:
                height = int.from_bytes(raw[position + 3:position + 5], "big")
                width = int.from_bytes(raw[position + 5:position + 7], "big")
                break
            position += segment_length
    elif raw.startswith(b"RIFF") and len(raw) >= 30 and raw[8:12] == b"WEBP":
        kind = "webp"
        if raw[12:16] == b"VP8X":
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
        elif raw[12:16] == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        elif raw[12:16] == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
    if width is None or height is None or width < 1 or height < 1:
        return ImageInspection(kind, width, height, False, "IMAGE_HEADER_INVALID")
    if width > max_dimension or height > max_dimension or width * height > max_pixels:
        return ImageInspection(kind, width, height, False, "IMAGE_DIMENSION_LIMIT")
    return ImageInspection(kind, width, height, True, None)

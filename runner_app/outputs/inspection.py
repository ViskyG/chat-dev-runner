"""Bounded owner-local views of already frozen, hash-checked v1 outputs.

This is a read model, not another authority for execution or publication. Unknown,
truncated and malformed formats keep their raw download without invented counts.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.resources import load_schema

from .parsers import (
    _json_shape,
    _tag,
    _xml_tree,
    inspect_image,
    parse_application_log,
    parse_junit,
)

MAX_INSPECTION_BYTES = 2 * 1024 * 1024
MAX_DISPLAY_CASES = 50
MAX_DISPLAY_EVENTS = 100
MAX_TRACE_CHARS = 4096
_RASTER_MEDIA = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _base(output: Mapping[str, Any], status: str, findings: list[str] | None = None) -> dict[str, Any]:
    return {"output_id": output["output_id"], "kind": output["kind"],
            "format": output["format"], "status": status,
            "findings": findings or []}


def inspection_limit(output: Mapping[str, Any]) -> dict[str, Any]:
    """Make an over-budget source explicit without reading it into the UI."""
    return _base(output, "UNPARSED", ["INSPECTION_BYTE_LIMIT"])


def inspect_registered_output(output: Mapping[str, Any], raw: bytes) -> dict[str, Any]:
    """Inspect only a registered v1 output; callers first seal its exact bytes."""
    if not output.get("complete"):
        return _base(output, "INCOMPLETE", [str(output.get("truncated_reason") or "OUTPUT_INCOMPLETE")])
    if len(raw) > MAX_INSPECTION_BYTES:
        return inspection_limit(output)
    kind, format_name = output["kind"], output["format"]
    if (kind, format_name) == ("test_result", "junit-xml"):
        parsed = parse_junit(raw)
        result = _base(output, parsed.status, list(parsed.findings))
        if parsed.status != "PARSED":
            return result
        cases: list[dict[str, str]] = []
        root = _xml_tree(raw)  # The same DTD, depth and element guards as parse_junit.
        for case in root.iter():
            if _tag(case) != "testcase":
                continue
            children = [child for child in case if _tag(child) in {"error", "failure"}]
            if not children:
                continue
            problem = next((child for child in children if _tag(child) == "error"), children[0])
            if len(cases) >= MAX_DISPLAY_CASES:
                continue
            classname = (case.get("classname") or "")[:120]
            name = (case.get("name") or "")[:120]
            case_id = f"{classname}::{name}" if classname else name
            trace = "".join(problem.itertext())[:MAX_TRACE_CHARS]
            cases.append({"id": case_id, "outcome": _tag(problem).upper(),
                          "message": (problem.get("message") or "")[:512],
                          "trace": trace})
        result["summary"] = parsed.summary
        result["failed_cases"] = cases
        result["omitted_failed_cases"] = max(
            0, parsed.summary["failed"] + parsed.summary["errors"] - len(cases))
        return result
    if (kind, format_name) == ("application_log", "application-log/v1"):
        parsed = parse_application_log(raw)
        result = _base(output, parsed.status, list(parsed.findings))
        if parsed.status != "PARSED":
            return result
        validator = Draft202012Validator(load_schema("application-log"),
                                          format_checker=FormatChecker())
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip() or len(line) > 128 * 1024:
                continue
            try:
                value = json.loads(line.decode("utf-8", errors="strict"),
                                   parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                continue
            if (not _json_shape(value) or not isinstance(value, dict)
                    or not validator.is_valid(value) or value["level"] not in {"ERROR", "FATAL"}):
                continue
            if len(events) < MAX_DISPLAY_EVENTS:
                message = value["message"]
                events.append({"timestamp": value["timestamp"], "level": value["level"],
                               "service": value["service"], "event": value["event"],
                               "message": message[:MAX_TRACE_CHARS],
                               "message_truncated": len(message) > MAX_TRACE_CHARS})
        result["summary"] = parsed.summary
        result["error_events"] = events
        result["omitted_error_events"] = max(
            0, sum(parsed.summary["levels"].get(level, 0) for level in ("ERROR", "FATAL"))
            - len(events))
        return result
    if kind == "screenshot":
        image = inspect_image(raw)
        media_type = _RASTER_MEDIA.get(image.format or "")
        safe = image.safe_preview and media_type == output["media_type"]
        result = _base(output, "PARSED" if safe else "UNPARSED",
                       [] if safe else [image.finding or "IMAGE_TYPE_MISMATCH"])
        result.update({"safe_preview": safe, "width": image.width, "height": image.height,
                       "preview_media_type": media_type if safe else None})
        return result
    return _base(output, "UNSUPPORTED", ["FORMAT_NOT_REGISTERED"])

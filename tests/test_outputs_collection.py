from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runner_app.outputs.collection import OutputError, OutputSpec, collect_outputs
from runner_app.outputs.redaction import redact_text_export
from runner_app.resources import load_schema


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "out"
    source.mkdir()
    return source, tmp_path / "frozen"


def test_collect_freezes_exact_and_globbed_files_with_parsed_results(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "reports").mkdir()
    (source / "reports" / "junit.xml").write_text(
        '<testsuite><testcase name="ok"/></testsuite>', encoding="utf-8",
    )
    (source / "reports" / "notes.md").write_text("# hi\n", encoding="utf-8")
    specs = (
        OutputSpec("junit", "test_result", "reports/junit.xml", "junit-xml", True),
        OutputSpec("notes", "report", "reports/*.md", "markdown", False),
    )
    result = collect_outputs(source, specs, frozen)
    assert result.complete and len(result.outputs) == 2
    assert result.parses["junit"].summary["passed"] == 1
    assert result.files["junit"].read_bytes() == (source / "reports" / "junit.xml").read_bytes()
    assert result.files["junit"].stat().st_mode & 0o777 == 0o600
    assert frozen.stat().st_mode & 0o777 == 0o700
    assert any(output["output_id"].startswith("notes-") for output in result.outputs)
    validator = Draft202012Validator(load_schema("output"))
    for output in result.outputs:
        validator.validate(output)
        assert output["sha256"] == hashlib.sha256(result.files[output["output_id"]].read_bytes()).hexdigest()
        assert output["publication"] == "local_only"


def test_required_and_optional_missing_are_distinct(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    result = collect_outputs(
        source,
        (OutputSpec("req", "report", "missing.txt", "text", True),
         OutputSpec("opt", "report", "other.txt", "text", False)),
        frozen,
    )
    assert result.outputs == ()
    assert not result.complete
    assert "req:REQUIRED_OUTPUT_MISSING" in result.findings
    assert "opt:OPTIONAL_OUTPUT_MISSING" in result.findings


def test_symlink_parent_final_and_hardlink_are_refused(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (source / "link").symlink_to(outside, target_is_directory=True)
    (source / "secret.txt").symlink_to(outside / "secret.txt")
    os.link(outside / "secret.txt", source / "hardlink.txt")
    specs = (
        OutputSpec("parent", "report", "link/secret.txt", "text", True),
        OutputSpec("final", "report", "secret.txt", "text", True),
        OutputSpec("hard", "report", "hardlink.txt", "text", True),
    )
    result = collect_outputs(source, specs, frozen)
    assert not result.complete and result.outputs == ()
    assert any("UNSAFE" in finding for finding in result.findings)
    assert (outside / "secret.txt").read_text() == "secret"


@pytest.mark.parametrize("path", [
    "../secret", "/etc/passwd", "a//b", "a/./b", ".git/config", "a/.env",
    "a/id_rsa", "a/private.pem", "a\\b",
])
def test_unsafe_profile_paths_are_refused_before_collection(tmp_path: Path, path: str) -> None:
    source, frozen = _roots(tmp_path)
    with pytest.raises(OutputError):
        collect_outputs(source, (OutputSpec("bad", "artifact", path, "binary"),), frozen)
    assert not frozen.exists()


def test_oversize_file_has_explicit_truncation_and_preserved_source(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    data = b"0123456789" * 20
    (source / "large.bin").write_bytes(data)
    result = collect_outputs(
        source, (OutputSpec("large", "artifact", "large.bin", "binary", True),),
        frozen, max_file_bytes=17,
    )
    assert not result.complete
    descriptor = result.outputs[0]
    assert descriptor["size_bytes"] == 17
    assert descriptor["complete"] is False
    assert descriptor["truncated_reason"] == "file_limit"
    assert result.files["large"].read_bytes() == data[:17]
    assert (source / "large.bin").read_bytes() == data


def test_unknown_format_stays_opaque_artifact(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "results.custom").write_text("whatever")
    result = collect_outputs(
        source, (OutputSpec("custom", "test_result", "results.custom", "custom-v1"),),
        frozen,
    )
    assert result.outputs[0]["kind"] == "artifact"
    assert result.parses == {}


def test_recursive_glob_does_not_cross_symlink(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "a").mkdir()
    (source / "a" / "x.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (source / "shortcut").symlink_to(outside, target_is_directory=True)
    result = collect_outputs(
        source, (OutputSpec("txt", "report", "**/*.txt", "text"),), frozen,
    )
    assert len(result.outputs) == 1
    assert result.provenance[result.outputs[0]["output_id"]]["source_relative_path"] == "a/x.txt"


def test_screenshot_oversize_dimensions_disable_preview(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "screenshot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + (20000).to_bytes(4, "big") + (100).to_bytes(4, "big"),
    )
    result = collect_outputs(
        source, (OutputSpec("screen", "screenshot", "screenshot.png", "png"),), frozen,
    )
    assert "screen:IMAGE_DIMENSION_LIMIT" in result.findings
    assert result.provenance["screen"]["image"]["safe_preview"] is False


def test_redacted_export_is_distinct_and_never_overwrites_raw(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    raw = "token=abc1234\nBearer abcdefghijklmnop\n"
    (source / "report.txt").write_text(raw)
    result = collect_outputs(
        source, (OutputSpec("report", "report", "report.txt", "text", publication="redacted_upload"),),
        frozen,
    )
    descriptor = result.outputs[0]
    with pytest.raises(OutputError, match="allowed"):
        redact_text_export(descriptor, result.files["report"], tmp_path / "denied", allowlisted=False)
    exported = redact_text_export(
        descriptor, result.files["report"], tmp_path / "exports", allowlisted=True,
    )
    assert exported.output["output_id"] != descriptor["output_id"]
    assert exported.export_sha256 != exported.raw_sha256
    assert exported.output["publication"] == "local_only"
    assert exported.quarantined
    assert "[REDACTED]" in exported.file.read_text()
    assert "abc1234" not in exported.file.read_text()
    assert result.files["report"].read_text() == raw
    assert exported.file.stat().st_mode & 0o777 == 0o600


def test_binary_export_is_refused(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "archive.zip").write_bytes(b"PK\x03\x04fake")
    result = collect_outputs(
        source, (OutputSpec("zip", "artifact", "archive.zip", "zip"),),
        frozen,
    )
    with pytest.raises(OutputError) as error:
        redact_text_export(result.outputs[0], result.files["zip"], tmp_path / "exports",
                           allowlisted=True)
    assert error.value.code == "EXPORT_FORMAT_UNSUPPORTED"


def test_malformed_junit_keeps_raw_file_and_parser_finding(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    malformed = b"<!DOCTYPE x [<!ENTITY a SYSTEM 'file:///etc/passwd'>]><testsuite>&a;</testsuite>"
    (source / "junit.xml").write_bytes(malformed)
    result = collect_outputs(
        source, (OutputSpec("junit", "test_result", "junit.xml", "junit-xml", True),),
        frozen,
    )
    assert result.complete  # File capture is complete despite project parser failure.
    assert result.files["junit"].read_bytes() == malformed
    assert result.parses["junit"].status == "UNPARSED"
    assert "junit:XML_DTD_FORBIDDEN" in result.findings


def test_redaction_variant_requires_local_review_for_suspected_secret(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "log.txt").write_text("token=abc1234\n")
    result = collect_outputs(
        source, (OutputSpec("log", "report", "log.txt", "plain", publication="redacted_upload"),),
        frozen,
    )
    reviewed = redact_text_export(
        result.outputs[0], result.files["log"], tmp_path / "reviewed",
        allowlisted=True, reviewed_suspicious=True,
    )
    assert reviewed.output["publication"] == "redacted_upload"
    assert not reviewed.quarantined
    assert "abc1234" not in reviewed.file.read_text()


def test_clean_text_export_has_distinct_hash_and_upload_policy(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "report.txt").write_text("No credentials here.\n")
    result = collect_outputs(
        source, (OutputSpec("report", "report", "report.txt", "plain"),),
        frozen,
    )
    exported = redact_text_export(
        result.outputs[0], result.files["report"], tmp_path / "exports",
        allowlisted=True,
    )
    assert exported.output["publication"] == "redacted_upload"
    assert exported.export_sha256 != exported.raw_sha256
    assert exported.replacements == 0


@pytest.mark.parametrize("suffix,content", [
    (".html", b"<script>alert(1)</script>"),
    (".svg", b"<svg onload='alert(1)'/>"),
])
def test_active_content_is_preserved_locally_but_not_text_redacted(
    tmp_path: Path, suffix: str, content: bytes,
) -> None:
    source, frozen = _roots(tmp_path)
    name = "report" + suffix
    (source / name).write_bytes(content)
    result = collect_outputs(
        source, (OutputSpec("active", "report", name, "opaque"),),
        frozen,
    )
    assert result.files["active"].read_bytes() == content
    assert result.outputs[0]["publication"] == "local_only"
    with pytest.raises(OutputError) as error:
        redact_text_export(
            result.outputs[0], result.files["active"], tmp_path / "exports",
            allowlisted=True,
        )
    assert error.value.code == "EXPORT_FORMAT_UNSUPPORTED"


def test_profile_cannot_reuse_capture_output_ids(tmp_path: Path) -> None:
    source, frozen = _roots(tmp_path)
    (source / "x.txt").write_text("x")
    with pytest.raises(OutputError):
        collect_outputs(
            source, (OutputSpec("stdout-raw", "report", "x.txt", "text"),), frozen,
        )

"""Race-resistant collection and freezing of approved container output paths."""

from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from runner_app.outputs.parsers import ParseResult, inspect_image, parse_output

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_KINDS = frozenset({
    "console", "application_log", "test_result", "coverage", "report",
    "screenshot", "artifact", "runner_log",
})
_SENSITIVE_NAMES = frozenset({
    ".git", ".github", ".ssh", ".aws", ".config", ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_ed25519", "token.json", "service-account.json",
    "credentials", "credentials.json", "secrets", "secrets.json",
})
_KNOWN_PARSERS = frozenset({
    ("test_result", "junit-xml"),
    ("coverage", "cobertura-xml"),
    ("application_log", "application-log/v1"),
})
_GLOB_CHARS = frozenset("*?[")
_CAPTURE_IDS = frozenset({
    "stdout-raw", "stdout-view", "stderr-raw", "stderr-view",
    "capture-events", "capture-metadata",
})


class OutputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OutputSpec:
    output_id: str
    kind: str
    path: str
    format: str
    required: bool = False
    publication: str = "local_only"

    @classmethod
    def from_profile(cls, value: Mapping[str, Any]) -> OutputSpec:
        return cls(
            output_id=value["id"], kind=value["kind"], path=value["path"],
            format=value["format"], required=value.get("required", False),
            publication=value.get("publication", "local_only"),
        )


@dataclass(frozen=True)
class CollectionResult:
    outputs: tuple[dict[str, Any], ...]
    files: dict[str, Path]
    parses: dict[str, ParseResult]
    findings: tuple[str, ...]
    provenance: dict[str, dict[str, Any]]
    complete: bool


def _parts(path: str, *, allow_glob: bool) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > 1000 or "\x00" in path:
        raise OutputError("INVALID_OUTPUT_PATH", "Output path is empty, too long or contains NUL")
    if path.startswith("/") or "\\" in path or "//" in path:
        raise OutputError("INVALID_OUTPUT_PATH", "Output path must be canonical POSIX relative")
    parts = tuple(path.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise OutputError("INVALID_OUTPUT_PATH", "Output path must be canonical POSIX relative")
    for part in parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_NAMES or lowered.startswith(".env") or lowered.endswith(".pem"):
            raise OutputError("PROTECTED_OUTPUT_PATH", "Protected output path is forbidden")
        if not allow_glob and any(character in part for character in _GLOB_CHARS):
            raise OutputError("INVALID_OUTPUT_PATH", "A concrete output path cannot contain a glob")
    return parts


def _validate_spec(spec: OutputSpec) -> tuple[str, ...]:
    if (not _ID.fullmatch(spec.output_id) or spec.kind not in _KINDS
            or spec.output_id in _CAPTURE_IDS):
        raise OutputError("INVALID_OUTPUT_SPEC", "Output ID or kind is invalid")
    if not spec.format or len(spec.format) > 1000:
        raise OutputError("INVALID_OUTPUT_SPEC", "Output format is invalid")
    if not isinstance(spec.required, bool) or spec.publication not in (
        "local_only", "redacted_upload", "approved_upload"
    ):
        raise OutputError("INVALID_OUTPUT_SPEC", "Output requirement or publication is invalid")
    return _parts(spec.path, allow_glob=True)


def _open_dir(parent_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent_fd)


def _concrete_open(root_fd: int, parts: tuple[str, ...]) -> int:
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = _open_dir(directory_fd, part)
            os.close(directory_fd)
            directory_fd = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise OutputError("UNSAFE_OUTPUT", "Output is not a single-link regular file")
        return fd
    finally:
        os.close(directory_fd)


def _list_names(directory_fd: int, *, max_entries: int) -> list[str]:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    if len(names) > max_entries:
        raise OutputError("OUTPUT_SCAN_LIMIT", "Output directory has too many entries")
    return sorted(names)


def _glob_paths(root_fd: int, parts: tuple[str, ...], *, max_files: int,
                max_depth: int, max_scan_entries: int) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    findings: list[str] = []
    scanned = 0

    def visit(directory_fd: int, prefix: tuple[str, ...], index: int) -> None:
        nonlocal scanned
        if len(prefix) > max_depth:
            raise OutputError("OUTPUT_DEPTH_LIMIT", "Output glob exceeds depth limit")
        if len(matches) > max_files:
            raise OutputError("OUTPUT_FILE_LIMIT", "Output glob has too many matches")
        if index == len(parts):
            if prefix:
                matches.append("/".join(prefix))
            return
        pattern = parts[index]
        if pattern == "**":
            if index + 1 < len(parts):
                visit(directory_fd, prefix, index + 1)
            names = _list_names(directory_fd, max_entries=max_scan_entries)
            scanned += len(names)
            if scanned > max_scan_entries:
                raise OutputError("OUTPUT_SCAN_LIMIT", "Output glob scans too many entries")
            for name in names:
                try:
                    _parts(name, allow_glob=False)
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except (OSError, OutputError):
                    continue
                if index + 1 == len(parts) and (stat.S_ISREG(info.st_mode)
                                                or stat.S_ISLNK(info.st_mode)):
                    matches.append("/".join(prefix + (name,)))
                if not stat.S_ISDIR(info.st_mode):
                    continue
                try:
                    child_fd = _open_dir(directory_fd, name)
                except OSError:
                    continue
                try:
                    visit(child_fd, prefix + (name,), index)
                finally:
                    os.close(child_fd)
            return
        names = _list_names(directory_fd, max_entries=max_scan_entries) if any(
            character in pattern for character in _GLOB_CHARS
        ) else [pattern]
        scanned += len(names)
        if scanned > max_scan_entries:
            raise OutputError("OUTPUT_SCAN_LIMIT", "Output glob scans too many entries")
        for name in names:
            if not fnmatch.fnmatchcase(name, pattern):
                continue
            try:
                _parts(name, allow_glob=False)
            except OutputError:
                findings.append("PROTECTED_OUTPUT_PATH")
                continue
            if index + 1 == len(parts):
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    matches.append("/".join(prefix + (name,)))
                if len(matches) > max_files:
                    raise OutputError("OUTPUT_FILE_LIMIT", "Output glob has too many matches")
            else:
                try:
                    child_fd = _open_dir(directory_fd, name)
                except OSError:
                    findings.append("UNSAFE_OR_MISSING_OUTPUT_PARENT")
                    continue
                try:
                    visit(child_fd, prefix + (name,), index + 1)
                finally:
                    os.close(child_fd)

    visit(root_fd, (), 0)
    return sorted(set(matches)), findings


def _media_type(path: str, kind: str, format_name: str) -> str:
    known = {
        "junit-xml": "application/xml",
        "cobertura-xml": "application/xml",
        "application-log/v1": "application/x-ndjson",
    }
    if format_name in known:
        return known[format_name]
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "application/octet-stream" if kind in ("artifact", "screenshot") else "text/plain"


def _frozen_id(spec: OutputSpec, source_path: str, *, globbed: bool) -> str:
    if not globbed:
        return spec.output_id
    suffix = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:8]
    return f"{spec.output_id[:39]}-{suffix}"


def _copy_limited(source_fd: int, destination: Path, *, limit: int) -> tuple[int, str, bool]:
    before = os.fstat(source_fd)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    destination_fd = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    copied = 0
    try:
        os.fchmod(destination_fd, 0o600)
        while copied < limit:
            chunk = os.read(source_fd, min(65_536, limit - copied))
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("Output freeze made no progress")
                digest.update(chunk[offset:offset + written])
                copied += written
                offset += written
        extra = os.read(source_fd, 1)
        after = os.fstat(source_fd)
        stable = (
            before.st_dev == after.st_dev and before.st_ino == after.st_ino
            and before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns
        )
        os.fsync(destination_fd)
        return copied, digest.hexdigest(), stable and not extra
    except BaseException:
        os.close(destination_fd)
        destination.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(destination_fd)
        except OSError:
            pass


def collect_outputs(
    output_root: Path,
    specs: Sequence[OutputSpec],
    frozen_root: Path,
    *,
    max_files: int = 128,
    max_file_bytes: int = 16 * 1024 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_depth: int = 16,
    max_scan_entries: int = 10_000,
) -> CollectionResult:
    """Freeze approved files below a dedicated output root using no-follow FDs.

    frozen_root must be a new child of a trusted private run-evidence directory.
    Profiles may request upload, but raw collected originals remain local_only.
    """
    if min(max_files, max_file_bytes, max_total_bytes, max_depth, max_scan_entries) < 1:
        raise ValueError("Collection limits must be positive")
    validated = [(spec, _validate_spec(spec)) for spec in specs]
    if len({spec.output_id for spec, _ in validated}) != len(validated):
        raise OutputError("DUPLICATE_OUTPUT_ID", "Output IDs must be unique")
    root_fd = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        frozen_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(frozen_root, 0o700)
        outputs: list[dict[str, Any]] = []
        files: dict[str, Path] = {}
        parses: dict[str, ParseResult] = {}
        provenance: dict[str, dict[str, Any]] = {}
        findings: list[str] = []
        complete = True
        total = 0
        for spec, parts in validated:
            globbed = any(any(character in part for character in _GLOB_CHARS) for part in parts)
            try:
                paths, scan_findings = _glob_paths(
                    root_fd, parts, max_files=max_files, max_depth=max_depth,
                    max_scan_entries=max_scan_entries,
                )
                findings.extend(f"{spec.output_id}:{item}" for item in scan_findings)
                if scan_findings:
                    complete = False
            except OutputError as error:
                findings.append(f"{spec.output_id}:{error.code}")
                complete = False
                continue
            if not paths:
                findings.append(
                    f"{spec.output_id}:{'REQUIRED_OUTPUT_MISSING' if spec.required else 'OPTIONAL_OUTPUT_MISSING'}"
                )
                if spec.required:
                    complete = False
                continue
            for source_path in paths:
                if len(outputs) >= max_files:
                    findings.append("OUTPUT_FILE_LIMIT")
                    complete = False
                    break
                output_id = _frozen_id(spec, source_path, globbed=globbed)
                if output_id in files:
                    findings.append(f"{output_id}:DUPLICATE_OUTPUT_ID")
                    complete = False
                    continue
                try:
                    source_fd = _concrete_open(root_fd, _parts(source_path, allow_glob=False))
                except (OSError, OutputError):
                    findings.append(f"{output_id}:UNSAFE_OR_MISSING_OUTPUT")
                    complete = False
                    continue
                suffix = PurePosixPath(source_path).suffix[:32]
                filename = f"{output_id}{suffix}"
                destination = frozen_root / filename
                allowance = min(max_file_bytes, max(0, max_total_bytes - total))
                try:
                    size, digest, file_complete = _copy_limited(
                        source_fd, destination, limit=allowance,
                    )
                except OSError:
                    findings.append(f"{output_id}:STORAGE_WRITE_FAILED")
                    complete = False
                    continue
                finally:
                    os.close(source_fd)
                total += size
                if not file_complete:
                    complete = False
                    findings.append(f"{output_id}:TRUNCATED_OR_CHANGED")
                effective_kind = (
                    "artifact" if (spec.kind, spec.format) not in _KNOWN_PARSERS
                    and spec.kind in ("test_result", "coverage", "application_log")
                    else spec.kind
                )
                descriptor = {
                    "output_id": output_id, "kind": effective_kind,
                    "format": spec.format,
                    "media_type": _media_type(source_path, effective_kind, spec.format),
                    "relative_path": f"{frozen_root.name}/{filename}",
                    "size_bytes": size, "sha256": digest,
                    "complete": file_complete,
                    "truncated_reason": None if file_complete else (
                        "file_limit" if size >= max_file_bytes else
                        "collection_budget" if total >= max_total_bytes else "source_changed"
                    ),
                    "publication": "local_only",
                }
                outputs.append(descriptor)
                files[output_id] = destination
                provenance[output_id] = {
                    "source_relative_path": source_path, "profile_output_id": spec.output_id,
                    "requested_publication": spec.publication,
                }
                if effective_kind == "screenshot" and file_complete:
                    image = inspect_image(destination)
                    provenance[output_id]["image"] = {
                        "format": image.format, "width": image.width, "height": image.height,
                        "safe_preview": image.safe_preview,
                    }
                    if image.finding is not None:
                        complete = False
                        findings.append(f"{output_id}:{image.finding}")
                elif effective_kind != "artifact" and file_complete:
                    parsed = parse_output(effective_kind, spec.format, destination)
                    parses[output_id] = parsed
                    findings.extend(f"{output_id}:{item}" for item in parsed.findings)
        return CollectionResult(
            outputs=tuple(outputs), files=files, parses=parses,
            findings=tuple(findings), provenance=provenance, complete=complete,
        )
    finally:
        os.close(root_fd)

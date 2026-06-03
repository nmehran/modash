from __future__ import annotations

import hashlib
import os
from pathlib import Path

from methods.runtime_evaluator.observation_model import (
    FINGERPRINT_CHUNK_SIZE,
    RuntimeFileFingerprint,
    RuntimeSourceObservationError,
    _schema_error,
)

def fingerprint_file(path: str | os.PathLike, roles):
    candidate = Path(path).resolve(strict=False)
    try:
        stat_result = candidate.stat()
    except OSError as exc:
        raise RuntimeSourceObservationError(
            f"unable to fingerprint runtime observation file: {candidate}: {exc}",
            code="runtime.observation.fingerprint_failed",
        ) from exc
    if not candidate.is_file():
        raise RuntimeSourceObservationError(
            f"runtime observation fingerprint target is not a file: {candidate}",
            code="runtime.observation.fingerprint_failed",
        )
    return RuntimeFileFingerprint(
        path=str(candidate),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        sha256=_file_sha256(candidate),
        roles=tuple(roles),
    )

def current_fingerprint_mismatch_details(fingerprint: RuntimeFileFingerprint):
    if not isinstance(fingerprint, RuntimeFileFingerprint):
        raise _schema_error("fingerprint must be a RuntimeFileFingerprint")
    path = Path(fingerprint.path)
    try:
        current = fingerprint_file(path, fingerprint.roles)
    except RuntimeSourceObservationError:
        return {
            "field": "missing",
            "expected": fingerprint.to_dict(),
            "current": None,
        }
    if current.size != fingerprint.size:
        return _fingerprint_field_mismatch("size", fingerprint, current)
    if current.mtime_ns != fingerprint.mtime_ns:
        return _fingerprint_field_mismatch("mtime_ns", fingerprint, current)
    if current.sha256 != fingerprint.sha256:
        return _fingerprint_field_mismatch("sha256", fingerprint, current)
    return None

def format_fingerprint_mismatch(subject: str, fingerprint: RuntimeFileFingerprint, details=None):
    details = details or current_fingerprint_mismatch_details(fingerprint)
    if details is None:
        return f"{subject} is current for {fingerprint.path}"
    role_text = ",".join(fingerprint.roles)
    field = details["field"]
    if field == "missing":
        return (
            f"{subject} is stale for {fingerprint.path} "
            f"(roles={role_text}): file is missing; "
            f"expected size={fingerprint.size} "
            f"mtime_ns={fingerprint.mtime_ns} "
            f"sha256={fingerprint.sha256}"
        )
    return (
        f"{subject} is stale for {fingerprint.path} "
        f"(roles={role_text}): {field} mismatch; "
        f"expected {field}={details['expected'][field]}; "
        f"current {field}={details['current'][field]}"
    )

def _fingerprint_field_mismatch(field: str, expected: RuntimeFileFingerprint, current: RuntimeFileFingerprint):
    return {
        "field": field,
        "expected": expected.to_dict(),
        "current": current.to_dict(),
    }

def _file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FINGERPRINT_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()

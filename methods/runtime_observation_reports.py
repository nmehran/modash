from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch,
    load_observation,
    validate_observation,
)
from methods.source_frontend import LineParserFrontend

REPORT_VERSION = 1
UNOBSERVED_SOURCE_SITE = "runtime.coverage.unobserved_source_site"


class RuntimeObservationReportError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.report.invalid"):
        super().__init__(message, code=code)


def build_observation_report(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_report_entrypoint(entrypoint_path, observation)
    if validate_fingerprints:
        _ensure_fingerprints_current(observation)

    static_sites = _static_source_sites(observation)
    observed_file_sites, process_command_sites = _observed_source_sites(observation)
    unobserved_sites = _unobserved_source_sites(static_sites, observed_file_sites)
    warnings = [_unobserved_warning(site) for site in unobserved_sites]

    return {
        "version": REPORT_VERSION,
        "entrypoint": observation.entrypoint,
        "observation_version": observation.version,
        "summary": {
            "observed_sources": len(observation.sources),
            "xtrace_source_commands": len(observation.xtrace),
            "trusted_xtrace_links": sum(1 for event in observation.sources if event.xtrace_index is not None),
            "file_backed_source_sites": len(static_sites),
            "observed_file_backed_source_sites": len(observed_file_sites),
            "unobserved_source_sites": len(unobserved_sites),
            "process_command_source_sites": len(process_command_sites),
            "warnings": len(warnings),
        },
        "warnings": warnings,
        "observed_source_sites": [site.to_dict() for site in observed_file_sites],
        "unobserved_source_sites": [site.to_dict() for site in unobserved_sites],
        "process_command_source_sites": process_command_sites,
        "xtrace_source_commands": [command.to_dict() for command in observation.xtrace],
    }


def build_observation_report_from_observation_file(entrypoint: str | os.PathLike, observation_path: str | os.PathLike):
    return build_observation_report(entrypoint, load_observation(observation_path))


def write_observation_report(report: dict, path: str | os.PathLike):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return target


def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)


def _ensure_report_entrypoint(entrypoint_path: Path, observation: RuntimeSourceObservation):
    observed_entrypoint = Path(observation.entrypoint).resolve(strict=False)
    if observed_entrypoint != entrypoint_path:
        raise RuntimeObservationReportError(
            f"observation entrypoint does not match requested entrypoint: {observed_entrypoint}",
            code="runtime.report.entrypoint_mismatch",
        )


def _ensure_fingerprints_current(observation: RuntimeSourceObservation):
    for fingerprint in observation.files:
        mismatch = current_fingerprint_mismatch(fingerprint)
        if mismatch is not None:
            raise RuntimeObservationReportError(
                f"runtime source observation is stale for {fingerprint.path}: {mismatch} mismatch",
                code="runtime.report.stale_observation",
            )


def _static_source_sites(observation: RuntimeSourceObservation):
    sites = []
    frontend = LineParserFrontend()
    for path in _file_backed_paths(observation):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeObservationReportError(
                f"unable to read fingerprinted source file for report: {path}: {exc}",
                code="runtime.report.file_unreadable",
            ) from exc
        ir = frontend.parse(path, content)
        for site in ir.source_sites:
            sites.append(_ReportSourceSite(
                file=str(path),
                line=site.location.line,
                column=site.location.column,
                command=site.text,
                source_expression=site.source_expression,
            ))
    return tuple(sorted(sites, key=lambda site: (site.file, site.line, site.column, site.command)))


def _file_backed_paths(observation: RuntimeSourceObservation):
    paths = [Path(fingerprint.path) for fingerprint in observation.files]
    return tuple(sorted(set(paths), key=lambda path: path.as_posix()))


def _observed_source_sites(observation: RuntimeSourceObservation):
    file_backed_paths = {path.as_posix() for path in _file_backed_paths(observation)}
    observed = []
    process_command_sites = []
    for event in observation.sources:
        site = _ReportSourceSite(
            file=event.call_site.file,
            line=event.call_site.line,
            column=1,
            command=event.call_site.command,
            source_expression="",
        )
        if Path(event.call_site.file).resolve(strict=False).as_posix() in file_backed_paths:
            observed.append(site)
            continue

        process = observation.processes[event.process_index]
        process_command_sites.append({
            "process_index": event.process_index,
            "process_command": process.command,
            "xtrace_index": event.xtrace_index,
            "call_site": event.call_site.to_dict(),
            "resolved_path": event.resolved_path,
            "arguments": list(event.arguments),
            "status": event.status,
        })
    return tuple(observed), process_command_sites


def _unobserved_source_sites(static_sites, observed_sites):
    observed_by_line = {}
    for site in observed_sites:
        observed_by_line.setdefault((site.file, site.line), []).append(site)

    static_count_by_line = {}
    for site in static_sites:
        static_count_by_line[(site.file, site.line)] = static_count_by_line.get((site.file, site.line), 0) + 1

    unobserved = []
    for site in static_sites:
        observed_for_line = observed_by_line.get((site.file, site.line), ())
        if not observed_for_line:
            unobserved.append(site)
            continue
        if len(observed_for_line) >= static_count_by_line[(site.file, site.line)]:
            continue
        if static_count_by_line[(site.file, site.line)] == 1:
            continue
        if not any(_commands_match(site.command, observed.command) for observed in observed_for_line):
            unobserved.append(site)
    return tuple(unobserved)


def _commands_match(left: str, right: str):
    return " ".join(left.split()) == " ".join(right.split())


def _unobserved_warning(site):
    return {
        "code": UNOBSERVED_SOURCE_SITE,
        "severity": "warning",
        "file": site.file,
        "line": site.line,
        "column": site.column,
        "command": site.command,
        "message": "source-capable site was not observed in this runtime trace",
        "hint": "Review whether this run exercised the branch before trusting the generated supplement.",
    }


class _ReportSourceSite:
    __slots__ = ("file", "line", "column", "command", "source_expression")

    def __init__(self, *, file, line, column, command, source_expression):
        self.file = str(Path(file).resolve(strict=False))
        self.line = int(line)
        self.column = int(column)
        self.command = command.strip()
        self.source_expression = source_expression.strip()

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "command": self.command,
            "source_expression": self.source_expression,
        }

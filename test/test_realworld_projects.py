import contextlib
import glob
import hashlib
import io
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import warnings
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MANIFEST_PATH = REPO_ROOT / "test" / "realworld" / "manifest.json"
FIXTURES_DIR = REPO_ROOT / "test" / "realworld" / "fixtures"
REALWORLD_DIR = REPO_ROOT / ".realworld"
CACHE_DIR = REALWORLD_DIR / "cache"
ARTIFACTS_DIR = CACHE_DIR / "artifacts"
CORPUS_DIR = CACHE_DIR / "corpus"
RESULTS_DIR = REPO_ROOT / ".realworld" / "results"
OUTPUTS_DIR = REALWORLD_DIR / "outputs"
PINNED_MODES = ("context", "executable")
EXPECTED_STATUSES = frozenset({"success", "unsupported", "timeout", "skip"})
RUNTIME_EXPECTED_STATUSES = frozenset({"match"})
RUNTIME_PROBE_KEYS = frozenset({"trace", "supplement", "graph", "observe_compile"})

LOCAL_SMOKE_PATTERNS = (
    "/etc/profile",
    "/etc/bash.bashrc",
    "/etc/profile.d/*.sh",
    "/usr/share/bash-completion/bash_completion",
    "/etc/X11/xinit/xinitrc",
)

DEFAULT_MODE_TIMEOUT_SECONDS = 3.0


def realworld_enabled():
    return os.environ.get("MODASH_REALWORLD") == "1"


def fetch_enabled():
    return os.environ.get("MODASH_REALWORLD_FETCH") == "1"


def runtime_enabled():
    return os.environ.get("MODASH_REALWORLD_RUNTIME") == "1"


def trace_enabled():
    return os.environ.get("MODASH_REALWORLD_TRACE") == "1"


def supplement_enabled():
    return os.environ.get("MODASH_REALWORLD_SUPPLEMENT") == "1"


def graph_enabled():
    return os.environ.get("MODASH_REALWORLD_GRAPH") == "1" or supplement_enabled()


def observe_compile_enabled():
    return os.environ.get("MODASH_REALWORLD_OBSERVE_COMPILE") == "1" or graph_enabled()


def report_enabled():
    return os.environ.get("MODASH_REALWORLD_REPORT") == "1"


def mode_timeout_seconds():
    raw_value = os.environ.get("MODASH_REALWORLD_TIMEOUT")
    if not raw_value:
        return DEFAULT_MODE_TIMEOUT_SECONDS
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError("MODASH_REALWORLD_TIMEOUT must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MODASH_REALWORLD_TIMEOUT must be a positive number")
    return timeout


def load_manifest(path=MANIFEST_PATH):
    with Path(path).open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    projects = manifest.get("projects")
    if not isinstance(projects, list):
        raise ValueError("real-world manifest must contain a projects list")
    for project in projects:
        validate_project(project)
    return manifest


def validate_project(project):
    if not isinstance(project, dict):
        raise ValueError("real-world manifest projects must be objects")
    for key in ("name", "kind", "version", "source", "entrypoints"):
        if key not in project:
            raise ValueError(f"real-world manifest project missing {key!r}")
    if project["kind"] != "pinned":
        raise ValueError(f"unsupported real-world project kind: {project['kind']}")
    if not isinstance(project["entrypoints"], list) or not project["entrypoints"]:
        raise ValueError(f"real-world project {project['name']} must declare entrypoints")
    for entrypoint in project["entrypoints"]:
        if not isinstance(entrypoint, dict) or not entrypoint.get("path"):
            raise ValueError(f"real-world project {project['name']} has invalid entrypoint")
        validate_entrypoint(project, entrypoint)
    validate_environment(project)
    validate_source_aliases(project)
    validate_fixture_files(project)

    source = project["source"]
    if not isinstance(source, dict):
        raise ValueError(f"real-world project {project['name']} source must be an object")
    for key in ("url", "sha256", "strip_components"):
        if key not in source:
            raise ValueError(f"real-world project {project['name']} source missing {key!r}")
    if not isinstance(source["strip_components"], int) or source["strip_components"] < 0:
        raise ValueError(f"real-world project {project['name']} has invalid strip_components")
    if len(source["sha256"]) != 64:
        raise ValueError(f"real-world project {project['name']} has invalid sha256")
    try:
        int(source["sha256"], 16)
    except ValueError as exc:
        raise ValueError(f"real-world project {project['name']} has invalid sha256") from exc


def validate_entrypoint(project, entrypoint):
    entrypoint_path = Path(entrypoint["path"])
    if entrypoint_path.is_absolute() or any(part == ".." for part in entrypoint_path.parts):
        raise ValueError(f"real-world project {project['name']} has unsafe entrypoint path")
    behavior_class = entrypoint.get("behavior_class")
    if behavior_class is not None and (
        not isinstance(behavior_class, str)
        or not behavior_class
        or any(character.isspace() for character in behavior_class)
    ):
        raise ValueError(
            f"real-world project {project['name']} entrypoint behavior_class "
            "must be a non-empty token"
        )

    modes = entrypoint.get("modes")
    if not isinstance(modes, dict):
        raise ValueError(f"real-world project {project['name']} entrypoint missing modes")
    if set(modes) != set(PINNED_MODES):
        raise ValueError(
            f"real-world project {project['name']} entrypoint must declare "
            f"{', '.join(PINNED_MODES)} modes"
        )

    for mode, expectation in modes.items():
        if not isinstance(expectation, dict):
            raise ValueError(f"real-world project {project['name']} {mode} expectation must be an object")
        expected = expectation.get("expected")
        if expected not in EXPECTED_STATUSES:
            raise ValueError(f"real-world project {project['name']} {mode} has invalid expected status")
        if expected == "unsupported":
            diagnostic = expectation.get("diagnostic")
            if not isinstance(diagnostic, dict) or not diagnostic.get("code") or not diagnostic.get("fragment"):
                raise ValueError(
                    f"real-world project {project['name']} {mode} unsupported expectation "
                    "must declare diagnostic code and fragment"
                )
        validate_source_supplement_reference(project, expectation)
    validate_runtime_probe(project, entrypoint)


def validate_source_supplement_reference(project, expectation):
    source_supplement = expectation.get("source_supplement")
    if source_supplement is None:
        return
    if not isinstance(source_supplement, str) or not source_supplement:
        raise ValueError(f"real-world project {project['name']} source_supplement must be a fixture path")
    candidate = Path(source_supplement)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"real-world project {project['name']} has unsafe source_supplement path")


def validate_runtime_probe(project, entrypoint):
    runtime = entrypoint.get("runtime")
    if runtime is None:
        return
    if not isinstance(runtime, dict):
        raise ValueError(f"real-world project {project['name']} runtime probe must be an object")
    if runtime.get("expected") is not None and runtime.get("expected") not in RUNTIME_EXPECTED_STATUSES:
        raise ValueError(f"real-world project {project['name']} runtime probe has invalid expected status")
    executable_expectation = entrypoint.get("modes", {}).get("executable", {})
    if runtime.get("expected") is not None and executable_expectation.get("expected") != "success":
        raise ValueError(
            f"real-world project {project['name']} runtime probe requires executable success expectation"
        )
    cwd = runtime.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError(f"real-world project {project['name']} runtime cwd must be a string")
    candidate = Path(cwd)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"real-world project {project['name']} has unsafe runtime cwd")
    validate_environment({"name": project["name"], "environment": runtime.get("environment", {})})
    for key in RUNTIME_PROBE_KEYS:
        validate_runtime_probe_spec(project, runtime, key)
    if runtime.get("expected") is None and not any(key in runtime for key in RUNTIME_PROBE_KEYS):
        raise ValueError(
            f"real-world project {project['name']} runtime probe must declare parity or a runtime probe"
        )


def validate_runtime_probe_spec(project, runtime, key):
    spec = runtime.get(key)
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise ValueError(f"real-world project {project['name']} runtime {key} probe must be an object")
    minimum_events = spec.get("minimum_events", 1)
    if not isinstance(minimum_events, int) or minimum_events < 0:
        raise ValueError(f"real-world project {project['name']} runtime {key} minimum_events must be non-negative")
    if key == "supplement":
        minimum_warnings = spec.get("minimum_warnings", 0)
        if not isinstance(minimum_warnings, int) or minimum_warnings < 0:
            raise ValueError(
                f"real-world project {project['name']} runtime supplement minimum_warnings must be non-negative"
            )
    suffixes = spec.get("required_source_suffixes", [])
    if not isinstance(suffixes, list):
        raise ValueError(
            f"real-world project {project['name']} runtime {key} required_source_suffixes must be a list"
        )
    for suffix in suffixes:
        if not isinstance(suffix, str) or not suffix:
            raise ValueError(
                f"real-world project {project['name']} runtime {key} required_source_suffixes entries must be strings"
            )
        candidate = Path(suffix)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise ValueError(
                f"real-world project {project['name']} runtime {key} has unsafe required source suffix"
            )


def validate_environment(project):
    environment = project.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError(f"real-world project {project['name']} environment must be an object")
    for name, value in environment.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"real-world project {project['name']} has invalid environment key")
        if not isinstance(value, str):
            raise ValueError(f"real-world project {project['name']} environment values must be strings")


def validate_source_aliases(project):
    aliases = project.get("source_aliases", [])
    if not isinstance(aliases, list):
        raise ValueError(f"real-world project {project['name']} source_aliases must be a list")
    for alias in aliases:
        if not isinstance(alias, dict):
            raise ValueError(f"real-world project {project['name']} source_aliases entries must be objects")
        from_suffix = alias.get("from_suffix")
        to_suffix = alias.get("to_suffix")
        if not isinstance(from_suffix, str) or not from_suffix:
            raise ValueError(f"real-world project {project['name']} source_aliases entry missing from_suffix")
        if not isinstance(to_suffix, str) or not to_suffix:
            raise ValueError(f"real-world project {project['name']} source_aliases entry missing to_suffix")
        if "/" in from_suffix or "/" in to_suffix:
            raise ValueError(f"real-world project {project['name']} source_aliases suffixes must not contain paths")
        if from_suffix == to_suffix:
            raise ValueError(f"real-world project {project['name']} source_aliases suffixes must differ")


def validate_fixture_files(project):
    fixture_files = project.get("fixture_files", [])
    if not isinstance(fixture_files, list):
        raise ValueError(f"real-world project {project['name']} fixture_files must be a list")
    for fixture_file in fixture_files:
        if not isinstance(fixture_file, dict):
            raise ValueError(f"real-world project {project['name']} fixture_files entries must be objects")
        for key in ("source", "path"):
            value = fixture_file.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"real-world project {project['name']} fixture_files entry missing {key}")
            candidate = Path(value)
            if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
                raise ValueError(f"real-world project {project['name']} has unsafe fixture {key}")


def local_smoke_fixtures():
    fixtures = []
    skipped = []
    seen = set()

    for pattern in LOCAL_SMOKE_PATTERNS:
        if glob.has_magic(pattern):
            matches = sorted(Path(match) for match in glob.glob(pattern))
            if not matches:
                skipped.append({
                    "pattern": pattern,
                    "reason": "no matches",
                })
            candidates = matches
        else:
            candidate = Path(pattern)
            if candidate.exists():
                candidates = [candidate]
            else:
                skipped.append({
                    "path": pattern,
                    "reason": "not present",
                })
                candidates = []

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            if not resolved.is_file():
                skipped.append({
                    "path": str(resolved),
                    "reason": "not a regular file",
                })
                continue
            fixtures.append(resolved)
            seen.add(resolved)

    return fixtures, skipped


def run_mode_with_timeout(
    entrypoint,
    mode,
    timeout_seconds,
    output_path=None,
    environment=None,
    source_supplement=None,
):
    output_artifact = Path(output_path) if output_path else None
    environment_payload = dict(environment or {})
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=mode_worker,
        args=(
            str(entrypoint),
            mode,
            str(output_artifact) if output_artifact else None,
            environment_payload,
            str(source_supplement) if source_supplement else None,
            child_conn,
        ),
    )
    started_at = time.perf_counter()
    process.start()
    child_conn.close()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join()
        if output_artifact is not None:
            remove_output_artifact(output_artifact)
        parent_conn.close()
        return {
            "mode": mode,
            "status": "timeout",
            "timeout_seconds": timeout_seconds,
            "duration_seconds": elapsed_seconds(started_at),
            "source_sites": None,
            "resolved_events": None,
            "disabled_sources": None,
            "diagnostics": [],
        }

    if parent_conn.poll(1):
        result = parent_conn.recv()
        parent_conn.close()
        result["duration_seconds"] = elapsed_seconds(started_at)
        if output_artifact is not None and result.get("status") != "success":
            remove_output_artifact(output_artifact)
        return result

    parent_conn.close()
    if output_artifact is not None:
        remove_output_artifact(output_artifact)
    return {
        "mode": mode,
        "status": "error",
        "duration_seconds": elapsed_seconds(started_at),
        "exception": {
            "type": "ProcessExit",
            "message": f"worker exited without a result, exitcode={process.exitcode}",
        },
        "source_sites": None,
        "resolved_events": None,
        "disabled_sources": None,
        "diagnostics": [],
    }


def elapsed_seconds(started_at):
    return round(time.perf_counter() - started_at, 6)


def mode_worker(entrypoint, mode, output_path, environment, source_supplement, result_sender):
    warnings.filterwarnings("ignore")
    os.environ.update(environment)
    try:
        payload = evaluate_mode(entrypoint, mode, output_path=output_path, source_supplement=source_supplement)
    except Exception as exc:
        payload = unexpected_error_result(mode, exc)

    try:
        result_sender.send(payload)
    except BrokenPipeError:
        pass
    finally:
        result_sender.close()


class CountingFrontend:
    def __init__(self):
        from methods.source_frontend import LineParserFrontend

        self.frontend = LineParserFrontend()
        self.source_sites = 0

    def parse(self, path, content):
        ir = self.frontend.parse(path, content)
        self.source_sites += len(ir.source_sites)
        return ir


def evaluate_mode(entrypoint, mode, output_path=None, source_supplement=None):
    from methods.compile import (
        context_from_source_events,
        context_paths_from_source_events,
        render_context_files,
        render_executable_script,
    )
    from methods.source_evaluator import SourceEvaluator
    from methods.source_resolver import UnsupportedSourceError
    from methods.source_supplements import load_source_supplement

    path = Path(entrypoint)
    frontend = CountingFrontend()

    try:
        supplement = load_source_supplement(source_supplement, path.parent)
        evaluation = SourceEvaluator(frontend=frontend, mode=mode, source_supplement=supplement).evaluate(path)
        context = context_from_source_events(
            evaluation.events,
            evaluation.disabled_sources,
            evaluation.line_replacements,
        )
        if mode == "executable":
            output = render_executable_script(str(path), context)
        else:
            sources = context_paths_from_source_events(str(path), evaluation.events)
            output = render_context_files(sources, str(path), context)

        content = "\n".join(output)
        result = {
            "mode": mode,
            "status": "success",
            "source_sites": frontend.source_sites,
            "resolved_events": len(evaluation.events),
            "disabled_sources": len(evaluation.disabled_sources),
            "diagnostics": [
                diagnostic_payload(diagnostic)
                for diagnostic in evaluation.diagnostics
            ],
            "output_lines": len(content.splitlines()),
        }
        if output_path:
            write_output_atomic(Path(output_path), content)
        return result
    except UnsupportedSourceError as exc:
        return unsupported_result(mode, exc, frontend.source_sites)


def unsupported_result(mode, exc, source_sites=None):
    diagnostic = getattr(exc, "diagnostic", None)
    diagnostics = []
    if diagnostic is not None:
        diagnostics.append(diagnostic_payload(diagnostic))
    else:
        diagnostics.append({
            "code": getattr(exc, "code", None),
            "fragment": str(exc),
            "message": str(exc),
            "hint": getattr(exc, "hint", None),
        })
    return {
        "mode": mode,
        "status": "unsupported",
        "source_sites": source_sites,
        "resolved_events": None,
        "disabled_sources": None,
        "diagnostics": diagnostics,
    }


def unexpected_error_result(mode, exc):
    return {
        "mode": mode,
        "status": "error",
        "source_sites": None,
        "resolved_events": None,
        "disabled_sources": None,
        "diagnostics": [],
        "exception": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }


def diagnostic_payload(diagnostic):
    location = diagnostic.location
    severity = getattr(diagnostic.severity, "value", diagnostic.severity)
    return {
        "code": diagnostic.code,
        "severity": severity,
        "path": str(location.path),
        "line": location.line,
        "column": location.column,
        "fragment": diagnostic.fragment,
        "message": diagnostic.message,
        "hint": diagnostic.hint,
    }


def write_result_file(name, payload):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = RESULTS_DIR / name
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    emit_report(name, payload, target)
    return target


def emit_report(name, payload, path):
    if not report_enabled():
        return
    summary = payload.get("summary", {})
    print(f"[realworld] {name}: {summary}", file=sys.stderr)
    print(f"[realworld] results: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    for record in payload.get("records", []):
        if record.get("matched_expectation") is False:
            print(
                f"[realworld] unmatched: {record_summary(record)}{record_details(record)}",
                file=sys.stderr,
            )


def result_summary(records):
    statuses = Counter(record.get("status") for record in records)
    summary = {
        "records": len(records),
        "statuses": dict(sorted(statuses.items())),
    }

    durations = [
        record["duration_seconds"]
        for record in records
        if isinstance(record.get("duration_seconds"), (int, float))
    ]
    if durations:
        summary["duration_seconds"] = round(sum(durations), 6)

    expected_records = [record for record in records if "expected_status" in record]
    if expected_records:
        expectations = Counter(record["expected_status"] for record in expected_records)
        matched = sum(1 for record in expected_records if record.get("matched_expectation"))
        summary.update({
            "expected_statuses": dict(sorted(expectations.items())),
            "matched_expectations": matched,
            "unmatched_expectations": len(expected_records) - matched,
            "output_artifacts": sum(1 for record in expected_records if record.get("output_path")),
        })

    behavior_classes = Counter(
        record["behavior_class"]
        for record in records
        if isinstance(record.get("behavior_class"), str)
    )
    if behavior_classes:
        summary["behavior_classes"] = dict(sorted(behavior_classes.items()))

    return summary


def write_output_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content + "\n", encoding="utf-8")
    temporary.replace(path)


def pinned_project_key(project):
    raw_key = f"{project['name']}-{project['version']}"
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in raw_key)


def artifact_filename(source):
    if source.get("archive"):
        return source["archive"]
    parsed_path = Path(urlparse(source["url"]).path)
    if parsed_path.name:
        return parsed_path.name
    raise ValueError(f"cannot infer archive filename from URL: {source['url']}")


def artifact_path(project):
    return ARTIFACTS_DIR / artifact_filename(project["source"])


def project_root(project):
    return CORPUS_DIR / pinned_project_key(project)


def project_marker_path(project):
    return project_root(project) / ".modash-realworld.json"


def marker_matches(project):
    marker = project_marker_path(project)
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        payload.get("name") == project["name"]
        and payload.get("version") == project["version"]
        and payload.get("source") == marker_source_payload(project["source"])
        and payload.get("source_aliases", []) == project.get("source_aliases", [])
        and payload.get("fixture_files", []) == project.get("fixture_files", [])
    )


def ensure_pinned_project(project):
    root = project_root(project)
    if root.is_dir() and marker_matches(project):
        apply_fixture_files(project, root)
        return root, "cached", None

    artifact = artifact_path(project)
    if artifact.is_file():
        try:
            verify_artifact(artifact, project["source"]["sha256"])
        except ValueError:
            if not fetch_enabled():
                raise
            artifact.unlink()
        else:
            extract_artifact(project, artifact)
            return root, "extracted", None

    if not fetch_enabled():
        return None, "skipped", (
            f"missing cached artifact for {project['name']} {project['version']}; "
            "set MODASH_REALWORLD_FETCH=1 to download it"
        )

    download_artifact(project["source"]["url"], artifact, project["source"]["sha256"])
    extract_artifact(project, artifact)
    return root, "fetched", None


def verify_artifact(path, expected_sha256):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"checksum mismatch for {path}: expected {expected_sha256}, got {actual_sha256}"
        )


def download_artifact(url, destination, expected_sha256):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)

    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for {url}: expected {expected_sha256}, got {actual_sha256}"
        )
    temporary.replace(destination)


def extract_artifact(project, artifact):
    root = project_root(project)
    temporary_root = root.with_name(root.name + ".tmp")
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
    temporary_root.mkdir(parents=True)

    try:
        with tarfile.open(artifact, "r:*") as archive:
            safe_extract_tar(
                archive,
                temporary_root,
                strip_components=project["source"]["strip_components"],
            )
        apply_source_aliases(project, temporary_root)
        apply_fixture_files(project, temporary_root)
        write_project_marker(project, temporary_root)
        if root.exists():
            shutil.rmtree(root)
        temporary_root.replace(root)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise


def safe_extract_tar(archive, destination, strip_components):
    destination = destination.resolve()
    for member in archive.getmembers():
        relative_path = stripped_member_path(member.name, strip_components)
        if relative_path is None:
            continue
        target = destination / relative_path
        if not target.resolve().is_relative_to(destination):
            raise ValueError(f"unsafe archive path: {member.name}")

        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def stripped_member_path(name, strip_components):
    path = Path(name)
    parts = path.parts[strip_components:]
    if not parts:
        return None
    if path.is_absolute() or any(part == ".." for part in parts):
        raise ValueError(f"unsafe archive path: {name}")
    return Path(*parts)


def write_project_marker(project, root):
    marker = root / ".modash-realworld.json"
    payload = {
        "name": project["name"],
        "version": project["version"],
        "source": marker_source_payload(project["source"]),
        "source_aliases": project.get("source_aliases", []),
        "fixture_files": project.get("fixture_files", []),
    }
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def marker_source_payload(source):
    return {
        "url": source["url"],
        "archive": artifact_filename(source),
        "sha256": source["sha256"],
        "strip_components": source["strip_components"],
    }


def apply_source_aliases(project, root):
    for alias in project.get("source_aliases", []):
        from_suffix = alias["from_suffix"]
        to_suffix = alias["to_suffix"]
        for source_path in root.rglob(f"*{from_suffix}"):
            if not source_path.is_file():
                continue
            target_name = source_path.name[:-len(from_suffix)] + to_suffix
            target_path = source_path.with_name(target_name)
            if not target_path.exists():
                shutil.copy2(source_path, target_path)


def apply_fixture_files(project, root):
    root = root.resolve()
    fixtures_root = FIXTURES_DIR.resolve()
    for fixture_file in project.get("fixture_files", []):
        source_path = (fixtures_root / fixture_file["source"]).resolve()
        if not source_path.is_relative_to(fixtures_root) or not source_path.is_file():
            raise FileNotFoundError(f"fixture source not found: {fixture_file['source']}")

        target_path = (root / fixture_file["path"]).resolve()
        if not target_path.is_relative_to(root):
            raise ValueError(f"unsafe fixture target: {fixture_file['path']}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def project_environment(project, root):
    return environment_values(project.get("environment", {}), root)


def runtime_environment(project, root, runtime):
    environment = project_environment(project, root)
    environment.update(environment_values(runtime.get("environment", {}), root))
    return environment


def environment_values(values, root):
    environment = {}
    for name, value in values.items():
        environment[name] = value.replace("{root}", str(root))
    return environment


def materialize_source_supplement(project, root, expectation):
    source_supplement = expectation.get("source_supplement")
    if not source_supplement:
        return None

    fixtures_root = FIXTURES_DIR.resolve()
    source_path = (fixtures_root / source_supplement).resolve()
    if not source_path.is_relative_to(fixtures_root) or not source_path.is_file():
        raise FileNotFoundError(f"source supplement fixture not found: {source_supplement}")

    raw_content = source_path.read_text(encoding="utf-8").replace("{root}", str(root))
    payload = json.loads(raw_content)
    target = root / ".modash-fixtures" / f"{source_path.stem}.generated.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def runtime_cwd(root, runtime):
    cwd = runtime.get("cwd", ".")
    candidate = (root / cwd).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"unsafe runtime cwd: {cwd}")
    return candidate


def runtime_probe_entries(project, probe_name):
    for entrypoint in project["entrypoints"]:
        runtime = entrypoint.get("runtime")
        if runtime is None:
            continue
        spec = runtime.get(probe_name)
        if spec is None:
            continue
        yield entrypoint, runtime, spec


def entrypoint_behavior_class(entrypoint):
    return entrypoint.get("behavior_class")


def minimum_probe_events(spec):
    return spec.get("minimum_events", 1)


def minimum_probe_warnings(spec):
    return spec.get("minimum_warnings", 0)


def required_probe_source_suffixes(spec):
    return tuple(spec.get("required_source_suffixes", ()))


def source_suffixes_match(record, spec):
    source_paths = [
        str(path).replace(os.sep, "/")
        for path in record.get("source_paths", [])
    ]
    return all(
        any(path.endswith(suffix) for path in source_paths)
        for suffix in required_probe_source_suffixes(spec)
    )


def normalize_record_paths(record, root):
    normalized = dict(record)
    normalized["entrypoint"] = relative_to_root(record["entrypoint"], root)
    normalized["diagnostics"] = [
        normalize_diagnostic_path(diagnostic, root)
        for diagnostic in record.get("diagnostics", [])
    ]
    return normalized


def normalize_runtime_payload(payload, root):
    normalized = dict(payload)
    for key in ("original", "compiled"):
        if key in normalized and "command" in normalized[key]:
            normalized[key] = dict(normalized[key])
            normalized[key]["command"] = [
                relative_to_root(command, root)
                for command in normalized[key]["command"]
            ]
    return normalized


def normalize_diagnostic_path(diagnostic, root):
    normalized = dict(diagnostic)
    path = normalized.get("path")
    if path:
        normalized["path"] = relative_to_root(path, root)
    return normalized


def relative_to_root(path, root):
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def mode_expectation(entrypoint, mode):
    return entrypoint["modes"][mode]


def skipped_mode_result(mode, reason):
    return {
        "mode": mode,
        "status": "skip",
        "duration_seconds": 0.0,
        "skip_reason": reason,
        "source_sites": None,
        "resolved_events": None,
        "disabled_sources": None,
        "diagnostics": [],
    }


def expectation_matches(record, expectation):
    if record["status"] != expectation["expected"]:
        return False
    if expectation["expected"] != "unsupported":
        return True

    diagnostics = record.get("diagnostics", [])
    if not diagnostics:
        return False
    diagnostic = diagnostics[0]
    expected_diagnostic = expectation["diagnostic"]
    return (
        diagnostic.get("code") == expected_diagnostic["code"]
        and expected_diagnostic["fragment"] in diagnostic.get("fragment", "")
    )


def expectation_failure_message(record):
    details = record_details(record)
    if record["expected_status"] == "unsupported":
        expected_diagnostic = record.get("expected_diagnostic", {})
        actual_diagnostic = (record.get("diagnostics") or [{}])[0]
        return (
            f"{record['project']} {record['entrypoint']} {record['mode']}: "
            f"expected unsupported {expected_diagnostic.get('code')} "
            f"{expected_diagnostic.get('fragment')!r}, got "
            f"{record['status']} {actual_diagnostic.get('code')} "
            f"{actual_diagnostic.get('fragment')!r}{details}"
        )
    return (
        f"{record['project']} {record['entrypoint']} {record['mode']}: "
        f"expected {record['expected_status']}, got {record['status']}{details}"
    )


def record_summary(record):
    mode = record.get("mode", "setup")
    return f"{record.get('project')} {record.get('entrypoint')} {mode}: {record.get('status')}"


def record_details(record):
    details = []
    if output_path := record.get("output_path"):
        details.append(f"output={output_path}")
    if source_supplement := record.get("source_supplement"):
        details.append(f"supplement={source_supplement}")
    if diagnostics := record.get("diagnostics"):
        diagnostic = diagnostics[0]
        details.append(
            "diagnostic="
            f"{diagnostic.get('code')} line={diagnostic.get('line')} "
            f"fragment={diagnostic.get('fragment')!r}"
        )
    if not details:
        return ""
    return " (" + "; ".join(details) + ")"


def output_artifact_path(project, entrypoint_path, mode):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / mode / relative_path
    return output_path.with_name(f"{output_path.name}.{mode}.sh")


def trace_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "trace" / relative_path
    return output_path.with_name(f"{output_path.name}.trace.json")


def generated_supplement_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "supplement" / relative_path
    return output_path.with_name(f"{output_path.name}.source-supplement.json")


def graph_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "graph" / relative_path
    return output_path.with_name(f"{output_path.name}.runtime-graph.json")


def graph_review_report_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "graph-report" / relative_path
    return output_path.with_name(f"{output_path.name}.runtime-graph.txt")


def observation_review_report_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "report" / relative_path
    return output_path.with_name(f"{output_path.name}.observation-report.json")


def supplement_replay_output_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "supplement-replay" / relative_path
    return output_path.with_name(f"{output_path.name}.supplement-replay.sh")


def graph_replay_output_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "graph-replay" / relative_path
    return output_path.with_name(f"{output_path.name}.graph-replay.sh")


def observe_compile_observation_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "observe-compile-observation" / relative_path
    return output_path.with_name(f"{output_path.name}.observe-compile.trace.json")


def observe_compile_graph_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "observe-compile-graph" / relative_path
    return output_path.with_name(f"{output_path.name}.observe-compile.runtime-graph.json")


def observe_compile_report_artifact_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "observe-compile-report" / relative_path
    return output_path.with_name(f"{output_path.name}.observe-compile.runtime-graph.txt")


def observe_compile_output_path(project, entrypoint_path):
    relative_path = Path(entrypoint_path)
    output_path = OUTPUTS_DIR / pinned_project_key(project) / "observe-compile" / relative_path
    return output_path.with_name(f"{output_path.name}.observe-compile.sh")


def clear_project_outputs(project):
    shutil.rmtree(OUTPUTS_DIR / pinned_project_key(project), ignore_errors=True)


def remove_output_artifact(path):
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)


def completed_output_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_runtime_command(script, cwd, environment, timeout_seconds):
    started_at = time.perf_counter()
    env = os.environ.copy()
    env.update(environment)
    try:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "duration_seconds": elapsed_seconds(started_at),
            "timeout_seconds": timeout_seconds,
            "stdout": completed_output_text(exc.stdout),
            "stderr": completed_output_text(exc.stderr),
        }

    return {
        "status": "complete",
        "duration_seconds": elapsed_seconds(started_at),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def run_runtime_parity_probe(entrypoint_path, compiled_path, cwd, environment, timeout_seconds):
    started_at = time.perf_counter()
    original = run_runtime_command(entrypoint_path, cwd, environment, timeout_seconds)
    if original["status"] == "timeout":
        return {
            "status": "timeout",
            "duration_seconds": elapsed_seconds(started_at),
            "timeout_seconds": timeout_seconds,
            "original": original,
        }

    compiled = run_runtime_command(compiled_path, cwd, environment, timeout_seconds)
    if compiled["status"] == "timeout":
        return {
            "status": "timeout",
            "duration_seconds": elapsed_seconds(started_at),
            "timeout_seconds": timeout_seconds,
            "original": original,
            "compiled": compiled,
        }

    matched = (
        original["returncode"] == compiled["returncode"]
        and original["stdout"] == compiled["stdout"]
        and original["stderr"] == compiled["stderr"]
    )
    return {
        "status": "match" if matched else "mismatch",
        "duration_seconds": elapsed_seconds(started_at),
        "original": original,
        "compiled": compiled,
    }


def run_runtime_trace_probe(entrypoint_path, cwd, environment, output_path, timeout_seconds):
    from methods.runtime_source_trace import RuntimeSourceTraceError, trace_sources, write_trace_observation

    started_at = time.perf_counter()
    try:
        result = trace_sources(entrypoint_path, cwd=cwd, env=environment, timeout=timeout_seconds)
        write_trace_observation(result, output_path)
    except RuntimeSourceTraceError as exc:
        if exc.code == "runtime.trace.timeout":
            return {
                "status": "timeout",
                "duration_seconds": elapsed_seconds(started_at),
                "timeout_seconds": timeout_seconds,
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }

    observation = result.observation
    return {
        "status": "success" if result.returncode == 0 else "target-nonzero",
        "duration_seconds": elapsed_seconds(started_at),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "source_events": len(observation.sources),
        "source_paths": [event.resolved_path for event in observation.sources],
        "output_path": str(output_path),
    }


def run_runtime_supplement_replay_probe(
    entrypoint_path,
    cwd,
    trace_environment,
    timeout_seconds,
    observation_path,
    supplement_path,
    report_path,
    compiled_path,
):
    from methods.runtime_observation_reports import build_observation_report, write_observation_report
    from methods.runtime_source_supplements import generate_source_supplement, write_generated_supplement
    from methods.runtime_source_trace import RuntimeSourceTraceError, trace_sources, write_trace_observation

    started_at = time.perf_counter()
    try:
        trace_result = trace_sources(entrypoint_path, cwd=cwd, env=trace_environment, timeout=timeout_seconds)
        write_trace_observation(trace_result, observation_path)
        supplement = generate_source_supplement(entrypoint_path, trace_result.observation)
        report = build_observation_report(
            entrypoint_path,
            trace_result.observation,
            validate_fingerprints=False,
        )
        write_generated_supplement(supplement, supplement_path)
        write_observation_report(report, report_path)
    except RuntimeSourceTraceError as exc:
        if exc.code == "runtime.trace.timeout":
            return {
                "status": "timeout",
                "duration_seconds": elapsed_seconds(started_at),
                "timeout_seconds": timeout_seconds,
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }

    compile_result = run_mode_with_timeout(
        entrypoint_path,
        "executable",
        timeout_seconds,
        output_path=compiled_path,
        environment={},
        source_supplement=supplement_path,
    )
    if compile_result["status"] != "success":
        return {
            "status": "compile-" + compile_result["status"],
            "duration_seconds": elapsed_seconds(started_at),
            "source_events": len(trace_result.observation.sources),
            "trace_returncode": trace_result.returncode,
            "trace_stdout": trace_result.stdout,
            "trace_stderr": trace_result.stderr,
            "observation_path": str(observation_path),
            "source_supplement": str(supplement_path),
            "review_report": str(report_path),
            "coverage_warnings": len(report["warnings"]),
            **compile_result,
        }

    original = run_runtime_command(entrypoint_path, cwd, trace_environment, timeout_seconds)
    compiled = run_runtime_command(compiled_path, cwd, trace_environment, timeout_seconds)
    if original["status"] == "timeout" or compiled["status"] == "timeout":
        status = "timeout"
    else:
        matched = (
            trace_result.returncode == original["returncode"]
            and trace_result.stdout == original["stdout"]
            and trace_result.stderr == original["stderr"]
            and original["returncode"] == compiled["returncode"]
            and original["stdout"] == compiled["stdout"]
            and original["stderr"] == compiled["stderr"]
        )
        status = "match" if matched else "mismatch"

    return {
        "status": status,
        "duration_seconds": elapsed_seconds(started_at),
        "source_events": len(trace_result.observation.sources),
        "source_paths": [event.resolved_path for event in trace_result.observation.sources],
        "trace_returncode": trace_result.returncode,
        "trace_stdout": trace_result.stdout,
        "trace_stderr": trace_result.stderr,
        "original": original,
        "compiled": compiled,
        "observation_path": str(observation_path),
        "source_supplement": str(supplement_path),
        "review_report": str(report_path),
        "coverage_warnings": len(report["warnings"]),
        "output_path": str(compiled_path),
    }


def run_runtime_graph_replay_probe(
    entrypoint_path,
    cwd,
    trace_environment,
    timeout_seconds,
    observation_path,
    graph_path,
    graph_report_path,
    compiled_path,
):
    from methods.runtime_source_graph import (
        build_observed_source_graph,
        write_observed_source_graph,
        write_observed_source_graph_review,
    )
    from methods.runtime_source_trace import RuntimeSourceTraceError, trace_sources, write_trace_observation
    from modash import compile_observed_main

    started_at = time.perf_counter()
    try:
        trace_result = trace_sources(entrypoint_path, cwd=cwd, env=trace_environment, timeout=timeout_seconds)
        write_trace_observation(trace_result, observation_path)
        graph = build_observed_source_graph(entrypoint_path, trace_result.observation)
        write_observed_source_graph(graph, graph_path)
        write_observed_source_graph_review(graph, graph_report_path)
        compiled_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stderr(io.StringIO()):
            compile_observed_main(str(entrypoint_path), str(compiled_path), graph=str(graph_path))
    except RuntimeSourceTraceError as exc:
        if exc.code == "runtime.trace.timeout":
            return {
                "status": "timeout",
                "duration_seconds": elapsed_seconds(started_at),
                "timeout_seconds": timeout_seconds,
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }

    original = run_runtime_command(entrypoint_path, cwd, trace_environment, timeout_seconds)
    compiled = run_runtime_command(compiled_path, cwd, trace_environment, timeout_seconds)
    if original["status"] == "timeout" or compiled["status"] == "timeout":
        status = "timeout"
    else:
        matched = (
            trace_result.returncode == original["returncode"]
            and trace_result.stdout == original["stdout"]
            and trace_result.stderr == original["stderr"]
            and original["returncode"] == compiled["returncode"]
            and original["stdout"] == compiled["stdout"]
            and original["stderr"] == compiled["stderr"]
        )
        status = "match" if matched else "mismatch"

    return {
        "status": status,
        "duration_seconds": elapsed_seconds(started_at),
        "source_events": len(trace_result.observation.sources),
        "source_paths": [event.resolved_path for event in trace_result.observation.sources],
        "graph_edges": len(graph["edges"]),
        "trace_returncode": trace_result.returncode,
        "trace_stdout": trace_result.stdout,
        "trace_stderr": trace_result.stderr,
        "original": original,
        "compiled": compiled,
        "observation_path": str(observation_path),
        "runtime_graph": str(graph_path),
        "graph_review_report": str(graph_report_path),
        "output_path": str(compiled_path),
    }


def run_runtime_observe_compile_probe(
    entrypoint_path,
    cwd,
    trace_environment,
    timeout_seconds,
    observation_path,
    graph_path,
    graph_report_path,
    compiled_path,
):
    from methods.runtime_source_trace import RuntimeSourceTraceError
    from modash import observe_compile_main

    started_at = time.perf_counter()
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            trace_returncode = observe_compile_main(
                str(entrypoint_path),
                str(compiled_path),
                graph_output=str(graph_path),
                report=str(graph_report_path),
                observation_output=str(observation_path),
                cwd=str(cwd),
                env=trace_environment,
                timeout=timeout_seconds,
            )
    except RuntimeSourceTraceError as exc:
        if exc.code == "runtime.trace.timeout":
            return {
                "status": "timeout",
                "duration_seconds": elapsed_seconds(started_at),
                "timeout_seconds": timeout_seconds,
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "duration_seconds": elapsed_seconds(started_at),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }

    trace_stdout = stdout.getvalue()
    trace_stderr = stderr.getvalue()
    trace_target_stderr = observe_compile_target_stderr(trace_stderr)
    original = run_runtime_command(entrypoint_path, cwd, trace_environment, timeout_seconds)
    compiled = run_runtime_command(compiled_path, cwd, trace_environment, timeout_seconds)
    if original["status"] == "timeout":
        status = "timeout"
    elif compiled["status"] == "timeout":
        status = "timeout"
    else:
        matched = (
            trace_returncode == original["returncode"]
            and trace_stdout == original["stdout"]
            and trace_target_stderr == original["stderr"]
            and original["returncode"] == compiled["returncode"]
            and original["stdout"] == compiled["stdout"]
            and original["stderr"] == compiled["stderr"]
        )
        status = "match" if matched else "mismatch"

    try:
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
        graph_edges = len(graph_payload["edges"])
        source_paths = [edge["resolved_path"] for edge in graph_payload["edges"]]
    except Exception:
        graph_edges = None
        source_paths = []

    return {
        "status": status,
        "duration_seconds": elapsed_seconds(started_at),
        "graph_edges": graph_edges,
        "source_paths": source_paths,
        "trace_returncode": trace_returncode,
        "trace_stdout": trace_stdout,
        "trace_stderr": trace_stderr,
        "trace_target_stderr": trace_target_stderr,
        "original": original,
        "compiled": compiled,
        "observation_path": str(observation_path),
        "runtime_graph": str(graph_path),
        "graph_review_report": str(graph_report_path),
        "output_path": str(compiled_path),
    }


def observe_compile_target_stderr(stderr: str):
    status_prefixes = (
        "modash: trace observation:",
        "modash: runtime source graph:",
        "modash: runtime graph review report:",
        "modash: compiled from newly observed trusted runtime graph:",
    )
    return "".join(
        line
        for line in stderr.splitlines(keepends=True)
        if not line.startswith(status_prefixes)
    )


def runtime_failure_message(record):
    if record["status"].startswith("compile-"):
        return (
            f"{record['project']} {record['entrypoint']} runtime: "
            f"expected match, got {record['status']}{record_details(record)}"
        )

    original = record.get("original", {})
    compiled = record.get("compiled", {})
    return (
        f"{record['project']} {record['entrypoint']} runtime: "
        f"expected match, got {record['status']} "
        f"original rc={original.get('returncode')} compiled rc={compiled.get('returncode')}"
        f"{runtime_timeout_details(original, compiled)}"
        f"{runtime_output_details(original, compiled)}"
    )


def runtime_timeout_details(original, compiled):
    details = []
    if original.get("status") == "timeout":
        details.append(f"original timeout={original.get('timeout_seconds')}")
    if compiled.get("status") == "timeout":
        details.append(f"compiled timeout={compiled.get('timeout_seconds')}")
    if not details:
        return ""
    return " (" + "; ".join(details) + ")"


def runtime_output_details(original, compiled):
    details = []
    for key in ("stdout", "stderr"):
        original_value = completed_output_text(original.get(key, ""))
        compiled_value = completed_output_text(compiled.get(key, ""))
        if original_value != compiled_value:
            details.append(
                f"{key} original={short_text(original_value)!r} compiled={short_text(compiled_value)!r}"
            )
    if not details:
        return ""
    return " (" + "; ".join(details) + ")"


def short_text(value, limit=160):
    value = completed_output_text(value)
    if len(value) <= limit:
        return value
    return value[:limit - 3] + "..."


class RealWorldHarnessHelperTestCase(unittest.TestCase):
    def write_trace_sensitive_runtime_project(self, root):
        entrypoint = root / "main.sh"
        dependency = root / "dep.sh"
        entrypoint.write_text(
            "\n".join([
                "source ./dep.sh",
                'if [[ -n ${MODASH_TRACE_FILE-} ]]; then',
                '  printf "mode:trace\\n"',
                "else",
                '  printf "mode:original\\n"',
                "fi",
                "",
            ]),
            encoding="utf-8",
        )
        dependency.write_text('printf "dep\\n"\n', encoding="utf-8")
        return entrypoint

    def test_manifest_rejects_invalid_behavior_class(self):
        project = {
            "name": "sample",
            "kind": "pinned",
            "version": "1",
            "source": {
                "url": "https://example.invalid/sample.tar.gz",
                "sha256": "0" * 64,
                "strip_components": 1,
            },
            "entrypoints": [{
                "path": "entry.sh",
                "behavior_class": "bad class",
                "modes": {
                    "context": {"expected": "success"},
                    "executable": {"expected": "success"},
                },
            }],
        }

        with self.assertRaisesRegex(ValueError, "behavior_class"):
            validate_project(project)

    def test_result_summary_counts_behavior_classes(self):
        summary = result_summary([
            {"status": "success", "behavior_class": "completion-loader"},
            {"status": "success", "behavior_class": "completion-loader"},
            {"status": "unsupported", "behavior_class": "dynamic-hook"},
            {"status": "success"},
        ])

        self.assertEqual(summary["behavior_classes"], {
            "completion-loader": 2,
            "dynamic-hook": 1,
        })

    def test_runtime_compile_failure_message_includes_diagnostics(self):
        message = runtime_failure_message({
            "project": "project",
            "entrypoint": "entry.sh",
            "status": "compile-unsupported",
            "diagnostics": [{
                "code": "modash.unresolved_source",
                "line": 3,
                "fragment": "source \"$target\"",
            }],
        })

        self.assertIn("expected match, got compile-unsupported", message)
        self.assertIn("diagnostic=modash.unresolved_source", message)
        self.assertIn("fragment='source \"$target\"'", message)

    def test_observe_compile_target_stderr_removes_only_status_lines(self):
        stderr = (
            "target-before\n"
            "modash: trace observation: /tmp/observation.json\n"
            "modash: runtime source graph: /tmp/graph.json\n"
            "modash: runtime graph review report: /tmp/graph.txt\n"
            "modash: compiled from newly observed trusted runtime graph: /tmp/out.sh\n"
            "target-after\n"
        )

        self.assertEqual(
            observe_compile_target_stderr(stderr),
            "target-before\ntarget-after\n",
        )

    def test_runtime_observe_compile_probe_detects_trace_stderr_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="modash-realworld-helper-") as tmpdir:
            root = Path(tmpdir)
            entrypoint = root / "main.sh"
            dependency = root / "dep.sh"
            entrypoint.write_text(
                "\n".join([
                    "source ./dep.sh",
                    '[[ -n ${MODASH_TRACE_FILE:-} ]] && printf "trace-only\\n" >&2',
                    "printf 'main\\n'",
                    "",
                ]),
                encoding="utf-8",
            )
            dependency.write_text("printf 'dep\\n'\n", encoding="utf-8")

            record = run_runtime_observe_compile_probe(
                entrypoint,
                root,
                {},
                5,
                root / "observation.json",
                root / "runtime-graph.json",
                root / "runtime-graph.txt",
                root / "compiled.sh",
            )

        self.assertEqual(record["status"], "mismatch", record)
        self.assertEqual(record["original"]["stderr"], "")
        self.assertEqual(record["compiled"]["stderr"], "")
        self.assertEqual(record["trace_target_stderr"], "trace-only\n")

    def test_runtime_failure_message_includes_timeout_side_and_output_diff(self):
        message = runtime_failure_message({
            "project": "project",
            "entrypoint": "entry.sh",
            "status": "timeout",
            "original": {
                "status": "timeout",
                "timeout_seconds": 0.1,
                "stdout": b"partial\n",
                "stderr": "",
            },
            "compiled": {
                "status": "complete",
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            },
        })

        self.assertIn("original timeout=0.1", message)
        self.assertIn("stdout original='partial\\n' compiled=''", message)

    def test_runtime_output_details_normalizes_byte_streams(self):
        self.assertEqual(
            "",
            runtime_output_details(
                {"stdout": b"same\n", "stderr": b""},
                {"stdout": "same\n", "stderr": ""},
            ),
        )

    def test_supplement_replay_probe_detects_trace_original_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entrypoint = self.write_trace_sensitive_runtime_project(root)

            probe = run_runtime_supplement_replay_probe(
                entrypoint,
                root,
                {},
                DEFAULT_MODE_TIMEOUT_SECONDS,
                root / "observation.json",
                root / "source-supplement.json",
                root / "review-report.json",
                root / "compiled.sh",
            )

        self.assertEqual(probe["status"], "mismatch")
        self.assertEqual(probe["source_events"], 1)
        self.assertEqual(probe["trace_returncode"], 0)
        self.assertEqual(probe["trace_stdout"], "dep\nmode:trace\n")
        self.assertEqual(probe["original"]["returncode"], 0)
        self.assertEqual(probe["original"]["stdout"], "dep\nmode:original\n")
        self.assertEqual(probe["compiled"]["returncode"], 0)
        self.assertEqual(probe["compiled"]["stdout"], probe["original"]["stdout"])

    def test_graph_replay_probe_detects_trace_original_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entrypoint = self.write_trace_sensitive_runtime_project(root)

            probe = run_runtime_graph_replay_probe(
                entrypoint,
                root,
                {},
                DEFAULT_MODE_TIMEOUT_SECONDS,
                root / "observation.json",
                root / "runtime-source-graph.json",
                root / "runtime-source-graph.txt",
                root / "compiled.sh",
            )

        self.assertEqual(probe["status"], "mismatch")
        self.assertEqual(probe["source_events"], 1)
        self.assertEqual(probe["graph_edges"], 1)
        self.assertEqual(probe["trace_returncode"], 0)
        self.assertEqual(probe["trace_stdout"], "dep\nmode:trace\n")
        self.assertEqual(probe["original"]["returncode"], 0)
        self.assertEqual(probe["original"]["stdout"], "dep\nmode:original\n")
        self.assertEqual(probe["compiled"]["returncode"], 0)
        self.assertEqual(probe["compiled"]["stdout"], probe["original"]["stdout"])


@unittest.skipUnless(
    realworld_enabled(),
    "set MODASH_REALWORLD=1 to run internal real-world corpus tests",
)
class RealWorldProjectTestCase(unittest.TestCase):
    def test_manifest_loads(self):
        manifest = load_manifest()

        self.assertIsInstance(manifest["projects"], list)

    def test_local_installed_smoke_fixtures(self):
        fixtures, skipped = local_smoke_fixtures()
        if not fixtures:
            self.skipTest("no local real-world smoke fixtures are present")

        timeout_seconds = mode_timeout_seconds()
        records = []
        unexpected_errors = []

        for fixture in fixtures:
            for mode in ("context", "executable"):
                with self.subTest(entrypoint=str(fixture), mode=mode):
                    result = run_mode_with_timeout(fixture, mode, timeout_seconds)
                    record = {
                        "project": "local-installed",
                        "kind": "local",
                        "entrypoint": str(fixture),
                        **result,
                    }
                    records.append(record)
                    if result["status"] == "error":
                        unexpected_errors.append(record)

        write_result_file("local-installed.json", {
            "suite": "local-installed",
            "timeout_seconds": timeout_seconds,
            "summary": result_summary(records),
            "skipped": skipped,
            "records": records,
        })

        if unexpected_errors:
            details = [
                (
                    f"{record['entrypoint']} {record['mode']}: "
                    f"{record['exception']['type']}: {record['exception']['message']}"
                )
                for record in unexpected_errors
            ]
            self.fail("unexpected real-world smoke errors:\n" + "\n".join(details))

    def test_pinned_corpus_projects(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        unexpected_errors = []
        expectation_failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue
            environment = project_environment(project, root)
            clear_project_outputs(project)

            for entrypoint in project["entrypoints"]:
                entrypoint_path = root / entrypoint["path"]
                if not entrypoint_path.is_file():
                    unexpected_errors.append({
                        "project": project["name"],
                        "version": project["version"],
                        "entrypoint": entrypoint["path"],
                        "mode": "setup",
                        "exception": {
                            "type": "FileNotFoundError",
                            "message": f"entrypoint not found: {entrypoint['path']}",
                        },
                    })
                    continue

                for mode in PINNED_MODES:
                    with self.subTest(project=project["name"], entrypoint=entrypoint["path"], mode=mode):
                        expectation = mode_expectation(entrypoint, mode)
                        if expectation["expected"] == "skip":
                            result = skipped_mode_result(mode, "manifest expectation is skip")
                            output_path = None
                            source_supplement = None
                        else:
                            output_path = output_artifact_path(project, entrypoint["path"], mode)
                            remove_output_artifact(output_path)
                            source_supplement = materialize_source_supplement(project, root, expectation)
                            result = run_mode_with_timeout(
                                entrypoint_path,
                                mode,
                                timeout_seconds,
                                output_path=output_path,
                                environment=environment,
                                source_supplement=source_supplement,
                            )
                        record = normalize_record_paths({
                            "project": project["name"],
                            "version": project["version"],
                            "kind": "pinned",
                            "entrypoint": str(entrypoint_path),
                            "behavior_class": entrypoint_behavior_class(entrypoint),
                            **result,
                        }, root)
                        record["expected_status"] = expectation["expected"]
                        if "diagnostic" in expectation:
                            record["expected_diagnostic"] = expectation["diagnostic"]
                        if source_supplement is not None:
                            record["source_supplement"] = source_supplement.relative_to(root).as_posix()
                        record["matched_expectation"] = expectation_matches(record, expectation)
                        if output_path is not None and record["status"] == "success" and output_path.is_file():
                            record["output_path"] = output_path.relative_to(REPO_ROOT).as_posix()
                        records.append(record)
                        if result["status"] == "error":
                            unexpected_errors.append(record)
                        elif not record["matched_expectation"]:
                            expectation_failures.append(record)

        write_result_file("pinned-corpus.json", {
            "suite": "pinned-corpus",
            "timeout_seconds": timeout_seconds,
            "fetch_enabled": fetch_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no pinned corpus artifacts are cached; set MODASH_REALWORLD_FETCH=1")

        if unexpected_errors:
            details = [
                (
                    f"{record['project']} {record['entrypoint']} {record['mode']}: "
                    f"{record['exception']['type']}: {record['exception']['message']}"
                )
                for record in unexpected_errors
            ]
            self.fail("unexpected pinned corpus errors:\n" + "\n".join(details))

        if expectation_failures:
            self.fail(
                "pinned corpus expectation failures:\n"
                + "\n".join(expectation_failure_message(record) for record in expectation_failures)
            )

    @unittest.skipUnless(
        runtime_enabled(),
        "set MODASH_REALWORLD_RUNTIME=1 to run runtime parity probes",
    )
    def test_pinned_runtime_parity_probes(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue

            for entrypoint in project["entrypoints"]:
                runtime = entrypoint.get("runtime")
                if runtime is None or runtime.get("expected") is None:
                    continue

                environment = runtime_environment(project, root, runtime)
                entrypoint_path = root / entrypoint["path"]
                expectation = mode_expectation(entrypoint, "executable")
                source_supplement = materialize_source_supplement(project, root, expectation)
                with tempfile.TemporaryDirectory(prefix="modash-realworld-runtime-") as temporary:
                    compiled_path = Path(temporary) / "compiled.sh"
                    compile_result = run_mode_with_timeout(
                        entrypoint_path,
                        "executable",
                        timeout_seconds,
                        output_path=compiled_path,
                        environment=environment,
                        source_supplement=source_supplement,
                    )
                    if compile_result["status"] != "success":
                        record = normalize_record_paths({
                            "project": project["name"],
                            "version": project["version"],
                            "kind": "runtime",
                            "entrypoint": str(entrypoint_path),
                            "behavior_class": entrypoint_behavior_class(entrypoint),
                            "expected_status": runtime["expected"],
                            "matched_expectation": False,
                            **compile_result,
                        }, root)
                        record["status"] = "compile-" + compile_result["status"]
                        records.append(record)
                        failures.append(record)
                        continue

                    probe = run_runtime_parity_probe(
                        entrypoint_path,
                        compiled_path,
                        runtime_cwd(root, runtime),
                        environment,
                        timeout_seconds,
                    )
                    record = normalize_record_paths({
                        "project": project["name"],
                        "version": project["version"],
                        "kind": "runtime",
                        "entrypoint": str(entrypoint_path),
                        "behavior_class": entrypoint_behavior_class(entrypoint),
                        "mode": "runtime",
                        **normalize_runtime_payload(probe, root),
                    }, root)
                    record["expected_status"] = runtime["expected"]
                    record["matched_expectation"] = record["status"] == runtime["expected"]
                    records.append(record)
                    if not record["matched_expectation"]:
                        failures.append(record)

        write_result_file("runtime-parity.json", {
            "suite": "runtime-parity",
            "timeout_seconds": timeout_seconds,
            "runtime_enabled": runtime_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no runtime parity probes are declared or cached")
        if failures:
            self.fail(
                "runtime parity failures:\n"
                + "\n".join(runtime_failure_message(record) for record in failures)
            )

    @unittest.skipUnless(
        trace_enabled(),
        "set MODASH_REALWORLD_TRACE=1 to run runtime trace smoke probes",
    )
    def test_pinned_runtime_trace_smoke_probe(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue

            for entrypoint, runtime, spec in runtime_probe_entries(project, "trace"):
                environment = runtime_environment(project, root, runtime)
                entrypoint_path = root / entrypoint["path"]
                output_path = trace_artifact_path(project, entrypoint["path"])
                remove_output_artifact(output_path)
                probe = run_runtime_trace_probe(
                    entrypoint_path,
                    runtime_cwd(root, runtime),
                    environment,
                    output_path,
                    timeout_seconds,
                )
                record = normalize_record_paths({
                    "project": project["name"],
                    "version": project["version"],
                    "kind": "runtime-trace",
                    "entrypoint": str(entrypoint_path),
                    "behavior_class": entrypoint_behavior_class(entrypoint),
                    **probe,
                }, root)
                if record.get("output_path"):
                    record["output_path"] = Path(record["output_path"]).relative_to(REPO_ROOT).as_posix()
                if record.get("source_paths"):
                    record["source_paths"] = [
                        relative_to_root(source_path, root)
                        for source_path in record["source_paths"]
                    ]
                record["expected_status"] = "success"
                record["matched_expectation"] = (
                    record["status"] == "success"
                    and record.get("source_events", 0) >= minimum_probe_events(spec)
                    and source_suffixes_match(record, spec)
                    and "MODASH_SOURCE_EVENT" not in record.get("stdout", "")
                    and "MODASH_SOURCE_EVENT" not in record.get("stderr", "")
                    and "MODASH_XTRACE" not in record.get("stdout", "")
                    and "MODASH_XTRACE" not in record.get("stderr", "")
                )
                records.append(record)
                if not record["matched_expectation"]:
                    failures.append(record)

        write_result_file("runtime-trace.json", {
            "suite": "runtime-trace",
            "timeout_seconds": timeout_seconds,
            "trace_enabled": trace_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no runtime trace smoke project is cached")
        if failures:
            self.fail(
                "runtime trace smoke failures:\n"
                + "\n".join(record_summary(record) + record_details(record) for record in failures)
            )

    @unittest.skipUnless(
        supplement_enabled(),
        "set MODASH_REALWORLD_SUPPLEMENT=1 to run runtime supplement replay probes",
    )
    def test_pinned_runtime_supplement_replay_probe(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue

            for entrypoint, runtime, spec in runtime_probe_entries(project, "supplement"):
                trace_environment = runtime_environment(project, root, runtime)
                entrypoint_path = root / entrypoint["path"]
                observation_path = trace_artifact_path(project, entrypoint["path"])
                supplement_path = generated_supplement_artifact_path(project, entrypoint["path"])
                report_path = observation_review_report_artifact_path(project, entrypoint["path"])
                compiled_path = supplement_replay_output_path(project, entrypoint["path"])
                for artifact in (observation_path, supplement_path, report_path, compiled_path):
                    remove_output_artifact(artifact)

                probe = run_runtime_supplement_replay_probe(
                    entrypoint_path,
                    runtime_cwd(root, runtime),
                    trace_environment,
                    timeout_seconds,
                    observation_path,
                    supplement_path,
                    report_path,
                    compiled_path,
                )
                record = normalize_record_paths({
                    "project": project["name"],
                    "version": project["version"],
                    "kind": "runtime-supplement",
                    "entrypoint": str(entrypoint_path),
                    "behavior_class": entrypoint_behavior_class(entrypoint),
                    **probe,
                }, root)
                for key in ("output_path", "observation_path", "source_supplement", "review_report"):
                    if record.get(key):
                        record[key] = Path(record[key]).relative_to(REPO_ROOT).as_posix()
                if record.get("source_paths"):
                    record["source_paths"] = [
                        relative_to_root(source_path, root)
                        for source_path in record["source_paths"]
                    ]
                record["expected_status"] = "match"
                record["matched_expectation"] = (
                    record["status"] == "match"
                    and record.get("source_events", 0) >= minimum_probe_events(spec)
                    and record.get("coverage_warnings", 0) >= minimum_probe_warnings(spec)
                    and source_suffixes_match(record, spec)
                    and record.get("source_supplement") is not None
                    and record.get("review_report") is not None
                )
                records.append(record)
                if not record["matched_expectation"]:
                    failures.append(record)

        write_result_file("runtime-supplement-replay.json", {
            "suite": "runtime-supplement-replay",
            "timeout_seconds": timeout_seconds,
            "supplement_enabled": supplement_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no runtime supplement replay project is cached")
        if failures:
            self.fail(
                "runtime supplement replay failures:\n"
                + "\n".join(record_summary(record) + record_details(record) for record in failures)
            )

    @unittest.skipUnless(
        graph_enabled(),
        "set MODASH_REALWORLD_GRAPH=1 or MODASH_REALWORLD_SUPPLEMENT=1 to run runtime graph replay probes",
    )
    def test_pinned_runtime_graph_replay_probe(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue

            for entrypoint, runtime, spec in runtime_probe_entries(project, "graph"):
                trace_environment = runtime_environment(project, root, runtime)
                entrypoint_path = root / entrypoint["path"]
                observation_path = trace_artifact_path(project, entrypoint["path"])
                graph_path = graph_artifact_path(project, entrypoint["path"])
                graph_report_path = graph_review_report_artifact_path(project, entrypoint["path"])
                compiled_path = graph_replay_output_path(project, entrypoint["path"])
                for artifact in (observation_path, graph_path, graph_report_path, compiled_path):
                    remove_output_artifact(artifact)

                probe = run_runtime_graph_replay_probe(
                    entrypoint_path,
                    runtime_cwd(root, runtime),
                    trace_environment,
                    timeout_seconds,
                    observation_path,
                    graph_path,
                    graph_report_path,
                    compiled_path,
                )
                record = normalize_record_paths({
                    "project": project["name"],
                    "version": project["version"],
                    "kind": "runtime-graph",
                    "entrypoint": str(entrypoint_path),
                    "behavior_class": entrypoint_behavior_class(entrypoint),
                    **probe,
                }, root)
                for key in ("output_path", "observation_path", "runtime_graph", "graph_review_report"):
                    if record.get(key):
                        record[key] = Path(record[key]).relative_to(REPO_ROOT).as_posix()
                if record.get("source_paths"):
                    record["source_paths"] = [
                        relative_to_root(source_path, root)
                        for source_path in record["source_paths"]
                    ]
                record["expected_status"] = "match"
                record["matched_expectation"] = (
                    record["status"] == "match"
                    and record.get("source_events", 0) >= minimum_probe_events(spec)
                    and record.get("graph_edges", 0) >= minimum_probe_events(spec)
                    and source_suffixes_match(record, spec)
                    and record.get("runtime_graph") is not None
                    and record.get("graph_review_report") is not None
                )
                records.append(record)
                if not record["matched_expectation"]:
                    failures.append(record)

        write_result_file("runtime-graph-replay.json", {
            "suite": "runtime-graph-replay",
            "timeout_seconds": timeout_seconds,
            "graph_enabled": graph_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no runtime graph replay project is cached")
        if failures:
            self.fail(
                "runtime graph replay failures:\n"
                + "\n".join(record_summary(record) + record_details(record) for record in failures)
            )

    @unittest.skipUnless(
        observe_compile_enabled(),
        "set MODASH_REALWORLD_OBSERVE_COMPILE=1 or MODASH_REALWORLD_GRAPH=1 to run observe-compile probes",
    )
    def test_pinned_runtime_observe_compile_probe(self):
        manifest = load_manifest()
        timeout_seconds = mode_timeout_seconds()
        setup_records = []
        records = []
        failures = []

        for project in manifest["projects"]:
            root, setup_status, reason = ensure_pinned_project(project)
            setup_record = {
                "project": project["name"],
                "version": project["version"],
                "status": setup_status,
            }
            if reason:
                setup_record["reason"] = reason
            setup_records.append(setup_record)
            if root is None:
                continue

            for entrypoint, runtime, spec in runtime_probe_entries(project, "observe_compile"):
                trace_environment = runtime_environment(project, root, runtime)
                entrypoint_path = root / entrypoint["path"]
                observation_path = observe_compile_observation_artifact_path(project, entrypoint["path"])
                graph_path = observe_compile_graph_artifact_path(project, entrypoint["path"])
                graph_report_path = observe_compile_report_artifact_path(project, entrypoint["path"])
                compiled_path = observe_compile_output_path(project, entrypoint["path"])
                for artifact in (observation_path, graph_path, graph_report_path, compiled_path):
                    remove_output_artifact(artifact)

                probe = run_runtime_observe_compile_probe(
                    entrypoint_path,
                    runtime_cwd(root, runtime),
                    trace_environment,
                    timeout_seconds,
                    observation_path,
                    graph_path,
                    graph_report_path,
                    compiled_path,
                )
                record = normalize_record_paths({
                    "project": project["name"],
                    "version": project["version"],
                    "kind": "runtime-observe-compile",
                    "entrypoint": str(entrypoint_path),
                    "behavior_class": entrypoint_behavior_class(entrypoint),
                    **probe,
                }, root)
                for key in ("output_path", "observation_path", "runtime_graph", "graph_review_report"):
                    if record.get(key):
                        record[key] = Path(record[key]).relative_to(REPO_ROOT).as_posix()
                if record.get("source_paths"):
                    record["source_paths"] = [
                        relative_to_root(source_path, root)
                        for source_path in record["source_paths"]
                    ]
                record["expected_status"] = "match"
                record["matched_expectation"] = (
                    record["status"] == "match"
                    and isinstance(record.get("graph_edges"), int)
                    and record["graph_edges"] >= minimum_probe_events(spec)
                    and source_suffixes_match(record, spec)
                    and record.get("observation_path") is not None
                    and record.get("runtime_graph") is not None
                    and record.get("graph_review_report") is not None
                    and record.get("output_path") is not None
                )
                records.append(record)
                if not record["matched_expectation"]:
                    failures.append(record)

        write_result_file("runtime-observe-compile.json", {
            "suite": "runtime-observe-compile",
            "timeout_seconds": timeout_seconds,
            "observe_compile_enabled": observe_compile_enabled(),
            "summary": result_summary(records),
            "setup": setup_records,
            "records": records,
        })

        if not records:
            self.skipTest("no observe-compile project is cached")
        if failures:
            self.fail(
                "runtime observe-compile failures:\n"
                + "\n".join(record_summary(record) + record_details(record) for record in failures)
            )


if __name__ == "__main__":
    unittest.main()

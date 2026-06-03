from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_evaluator.observation_model import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    _schema_error,
)

def validate_observation(data):
    if not isinstance(data, dict):
        raise _schema_error("runtime source observation must be a JSON object")
    return RuntimeSourceObservation.from_dict(data)

def load_observation(path: str | os.PathLike):
    observation_path = Path(path)
    if not observation_path.is_file():
        raise RuntimeSourceObservationError(
            f"runtime source observation file does not exist: {observation_path}",
            code="runtime.observation.missing",
        )

    try:
        data = json.loads(observation_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeSourceObservationError(
            f"invalid runtime source observation JSON: {observation_path}: {exc}",
            code="runtime.observation.invalid_json",
        ) from exc
    return validate_observation(data)

def write_observation(path: str | os.PathLike, observation: RuntimeSourceObservation):
    observation = _coerce_observation(observation)
    observation_path = Path(path)
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(json.dumps(observation.to_dict(), indent=2) + "\n", encoding="utf-8")
    return observation_path

def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)

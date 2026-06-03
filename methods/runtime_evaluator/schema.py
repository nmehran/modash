import os
from pathlib import Path


def require_keys(data, expected_keys, label: str, *, error_factory):
    if not isinstance(data, dict):
        raise error_factory(f"{label} must be an object")

    missing = sorted(expected_keys - set(data))
    if missing:
        raise error_factory(f"{label} missing required keys: {', '.join(missing)}")

    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise error_factory(f"{label} has unknown keys: {', '.join(unknown)}")


def object_list(value, label: str, *, error_factory):
    if not isinstance(value, list):
        raise error_factory(f"{label} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise error_factory(f"{label}[{index}] must be an object")
    return value


def string_list(value, label: str, *, error_factory):
    if not isinstance(value, list):
        raise error_factory(f"{label} must be a list")
    return tuple(exact_string(item, f"{label}[]", error_factory=error_factory) for item in value)


def sequence(value, label: str, *, error_factory):
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise error_factory(f"{label} must be a sequence")
    return tuple(value)


def absolute_path(value, label: str, *, error_factory):
    value = nonempty_string(value, label, error_factory=error_factory)
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        raise error_factory(f"{label} must be an absolute path")
    return str(candidate.resolve(strict=False))


def exact_string(value, label: str, *, error_factory):
    if not isinstance(value, str):
        raise error_factory(f"{label} values must be strings")
    if "\0" in value:
        raise error_factory(f"{label} values must not contain NUL bytes")
    return value


def nonempty_string(value, label: str, *, error_factory):
    value = exact_string(value, label, error_factory=error_factory)
    if not value:
        raise error_factory(f"{label} must not be empty")
    return value


def positive_int(value, label: str, *, error_factory):
    value = integer(value, label, error_factory=error_factory)
    if value < 1:
        raise error_factory(f"{label} must be greater than 0")
    return value


def nonnegative_int(value, label: str, *, error_factory):
    value = integer(value, label, error_factory=error_factory)
    if value < 0:
        raise error_factory(f"{label} must be greater than or equal to 0")
    return value


def integer(value, label: str, *, error_factory):
    if not isinstance(value, int) or isinstance(value, bool):
        raise error_factory(f"{label} must be an integer")
    return value

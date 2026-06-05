from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedSource:
    path: str
    source_expression: str
    source_site: str
    execution_model: str = "parent-source"
    confidence: str = "exact"
    replacement_kind: str = "source"
    source_value: str | None = None
    source_arguments: tuple[str, ...] | None = None
    source_column: int | None = None
    occurrence_model: str | None = None
    condition: str | None = None
    positional_assignment_generation: int | None = None
    sync_positionals: bool = False
    source_location_path: str | None = None
    source_location_line: int | None = None


@dataclass(frozen=True)
class GlobMatch:
    word: str
    path: str
    exists: bool = True
    is_file: bool = True


@dataclass(frozen=True)
class HeredocDelimiter:
    value: str
    strip_tabs: bool = False
    quoted: bool = False


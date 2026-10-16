from __future__ import annotations

from typing import Any, Iterable


PROVENANCE_FIELDS = [
    "generation_backend",
    "scoring_engine",
    "execution_mode",
    "fallback_reason",
]

REAL_GENERATION_BACKENDS = {"deepfrag2"}
REAL_SCORING_ENGINES = {"vina"}


def join_fallback_reasons(*values: Any) -> str:
    """Join fallback reasons without losing order or duplicating entries."""
    items: list[str] = []
    for value in values:
        for item in str(value or "").replace("\r", " ").replace("\n", " ").split(";"):
            item = item.strip()
            if item and item not in items:
                items.append(item)
    return ";".join(items)


def generation_execution_mode(generation_backend: Any) -> str:
    backend = str(generation_backend or "").strip().lower()
    if backend in REAL_GENERATION_BACKENDS:
        return "production"
    if backend:
        return "simulation"
    return "unknown"


def combined_execution_mode(generation_backend: Any, scoring_engine: Any) -> str:
    generation = str(generation_backend or "").strip().lower()
    scoring = str(scoring_engine or "").strip().lower()
    if generation in REAL_GENERATION_BACKENDS and scoring in REAL_SCORING_ENGINES:
        return "production"
    if generation and scoring:
        return "simulation"
    return "unknown"


def stamp_generation_provenance(
    rows: Iterable[dict[str, Any]],
    *,
    generation_backend: str,
    fallback_reason: str = "",
) -> list[dict[str, Any]]:
    stamped: list[dict[str, Any]] = []
    for row in rows:
        actual_backend = str(row.get("generation_backend") or row.get("backend") or generation_backend).strip()
        stamped.append(
            {
                **row,
                "generation_backend": actual_backend,
                "execution_mode": generation_execution_mode(actual_backend),
                "fallback_reason": join_fallback_reasons(row.get("fallback_reason"), fallback_reason),
            }
        )
    return stamped


def formal_result_eligible(row: dict[str, Any]) -> bool:
    return (
        str(row.get("status", "")).strip().lower() == "success"
        and str(row.get("generation_backend", "")).strip().lower() in REAL_GENERATION_BACKENDS
        and str(row.get("scoring_engine", "")).strip().lower() in REAL_SCORING_ENGINES
        and str(row.get("execution_mode", "")).strip().lower() == "production"
    )

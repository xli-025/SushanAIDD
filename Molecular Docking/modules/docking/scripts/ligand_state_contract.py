from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


MIN_SUPPORTED_PH = 0.0
MAX_SUPPORTED_PH = 14.0


def validate_ligand_ph(value: Any) -> float | None:
    """Return a finite supported pH, or ``None`` when pH adjustment is disabled."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("pH must be a finite number, not a boolean")
    try:
        ph = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("pH must be a finite number") from exc
    if not math.isfinite(ph):
        raise ValueError("pH must be finite")
    if not MIN_SUPPORTED_PH <= ph <= MAX_SUPPORTED_PH:
        raise ValueError(f"pH must be between {MIN_SUPPORTED_PH:g} and {MAX_SUPPORTED_PH:g}")
    return ph


@dataclass(frozen=True)
class LigandPDBQTConversion:
    pdbqt_text: str
    pdbqt_status: str
    failure_reason: str
    ph_requested: float | None
    ph_protonation_status: str
    ph_protonation_backend: str
    ph_protonation_fallback_reason: str
    ligand_state_mode: str
    protomer_enumeration: bool = False

    def qc_fields(self) -> dict[str, Any]:
        return {
            "pdbqt_status": self.pdbqt_status,
            "ph_requested": "" if self.ph_requested is None else self.ph_requested,
            "ph_protonation_status": self.ph_protonation_status,
            "ph_protonation_backend": self.ph_protonation_backend,
            "ph_protonation_fallback_reason": self.ph_protonation_fallback_reason,
            "ligand_state_mode": self.ligand_state_mode,
            "protomer_enumeration": self.protomer_enumeration,
        }

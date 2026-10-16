from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.reverse_design_after_docking.backends.deepfrag2_backend import DeepFrag2Backend


def run_deepfrag2(
    *,
    seed: dict[str, Any],
    round_id: int,
    attachment_points: list[dict[str, Any]],
    out_dir: Path,
    fragments_per_attachment_point: int,
) -> list[dict[str, Any]]:
    return DeepFrag2Backend().generate_fragments(
        seed=seed,
        round_id=round_id,
        attachment_points=attachment_points,
        out_dir=out_dir,
        fragments_per_attachment_point=fragments_per_attachment_point,
    )

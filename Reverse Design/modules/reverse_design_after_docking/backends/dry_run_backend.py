from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.reverse_design_after_docking.scripts.common import deterministic_seed, write_rows


FRAGMENT_FIELDS = [
    "seed_pair_id",
    "round",
    "attachment_id",
    "fragment_rank",
    "fragment_score",
    "fragment_smiles",
    "backend",
    "generation_backend",
    "execution_mode",
    "fallback_reason",
    "model_name",
    "raw_output_path",
]


FRAGMENT_LIBRARY = [
    "C",
    "CC",
    "CO",
    "CN",
    "O",
    "N",
    "F",
    "Cl",
    "C#N",
    "C(=O)O",
    "c1ccccc1",
    "OC",
    "NC",
    "C(F)(F)F",
    "S(C)(=O)=O",
    "C1CC1",
    "C=C",
    "C(C)C",
    "C(=O)N",
    "N(C)C",
    "CCO",
    "CCN",
    "CCl",
    "CBr",
    "C(C)(C)O",
    "C(C)(C)N",
    "C1=CC=CN=C1",
    "C1=CC=CO1",
    "C1=CC=CS1",
    "C1CCOC1",
]


class DryRunBackend:
    name = "dry_run"
    model_name = "deterministic_fragment_library_v1"

    def generate_fragments(
        self,
        *,
        seed: dict[str, Any],
        round_id: int,
        attachment_points: list[dict[str, Any]],
        out_dir: Path,
        fragments_per_attachment_point: int = 10,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not attachment_points:
            write_rows(out_dir / "deepfrag2_fragments.csv", rows, FRAGMENT_FIELDS)
            return rows
        offset = (round_id - 1) * fragments_per_attachment_point
        for attachment in attachment_points:
            attachment_id = str(attachment.get("attachment_id", "att"))
            base_seed = deterministic_seed(f"{seed.get('seed_pair_id')}|{round_id}|{attachment_id}")
            rotated = FRAGMENT_LIBRARY[base_seed % len(FRAGMENT_LIBRARY) :] + FRAGMENT_LIBRARY[: base_seed % len(FRAGMENT_LIBRARY)]
            for rank, fragment in enumerate(rotated[offset : offset + fragments_per_attachment_point], start=1):
                rows.append(
                    {
                        "seed_pair_id": seed.get("seed_pair_id", ""),
                        "round": round_id,
                        "attachment_id": attachment_id,
                        "fragment_rank": rank + offset,
                        "fragment_score": round(1.0 / (rank + offset), 6),
                        "fragment_smiles": fragment,
                        "backend": self.name,
                        "generation_backend": self.name,
                        "execution_mode": "simulation",
                        "fallback_reason": "",
                        "model_name": self.model_name,
                        "raw_output_path": str(out_dir),
                    }
                )
        write_rows(out_dir / "deepfrag2_fragments.csv", rows, FRAGMENT_FIELDS)
        return rows

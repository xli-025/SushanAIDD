from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class FragmentBackend(Protocol):
    name: str

    def generate_fragments(
        self,
        *,
        seed: dict[str, Any],
        round_id: int,
        attachment_points: list[dict[str, Any]],
        out_dir: Path,
        fragments_per_attachment_point: int,
    ) -> list[dict[str, Any]]:
        ...

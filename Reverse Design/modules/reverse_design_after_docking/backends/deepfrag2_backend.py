from __future__ import annotations

import csv
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from modules.reverse_design_after_docking.scripts.common import find_executable, slugify, write_rows
from modules.reverse_design_after_docking.backends.dry_run_backend import FRAGMENT_FIELDS
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_runtime import remaining_timeout

# The deepfrag2 standalone environment (Python 3.9 + deepfrag2==2.0.0)
# Both .deepfrag2_env and pretrained_models live inside the Reverse Design module folder.
_MODULE_DIR = Path(__file__).resolve().parents[2]  # backends/ → reverse_design_after_docking/ → modules/
_DEEPFRAG2_ENV_BIN = _MODULE_DIR.parent / ".deepfrag2_env" / "bin"


class DeepFrag2Backend:
    """Real DeepFrag2 CPU-only inference backend.

    Uses the standalone ``.deepfrag2_env`` virtual environment so that
    deepfrag2's pinned versions of numpy/rdkit/pandas do not collide with
    the main project venv.
    """

    name = "deepfrag2"
    model_name = "gte_4_best"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    # DeepFrag2 looks for pretrained models at {cwd}/pretrained_models/.
    # pretrained_models/ now lives inside the Reverse Design module folder.
    _PROJECT_ROOT = Path(__file__).resolve().parents[2].parent  # .../Reverse Design/

    def available(self) -> bool:
        """Check whether the deepfrag2 command is reachable."""
        return self._find_python_and_script() is not None

    def _find_python_and_script(self) -> tuple[Path, Path] | None:
        """Return (python_exe, deepfrag2_script) from the standalone env.

        We invoke deepfrag2 via its interpreter rather than the shebang,
        because the env path contains a space ("Reverse Design") which
        breaks #! interpreter lines.
        """
        py = _DEEPFRAG2_ENV_BIN / "python3"
        script = _DEEPFRAG2_ENV_BIN / "deepfrag2"
        if py.exists() and script.exists():
            return py, script
        # Fallback: try system PATH
        found = find_executable("deepfrag2")
        if found:
            return Path(sys.executable), Path(found)
        return None

    # ------------------------------------------------------------------
    # Fragment generation
    # ------------------------------------------------------------------

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

        interp = self._find_python_and_script()
        if interp is None:
            raise RuntimeError(
                "DeepFrag2 executable not found in .deepfrag2_env or PATH. "
                "Cannot run real backend."
            )
        py_exe, df2_script = interp

        seed_pair_id = str(seed.get("seed_pair_id", ""))
        receptor_pdb = Path(str(seed.get("prepared_receptor_pdb") or seed.get("receptor_pdb") or ""))
        parent_sdf = Path(str(seed.get("parent_best_pose_sdf") or seed.get("parent_ligand_sdf") or ""))

        if not receptor_pdb.exists():
            raise FileNotFoundError(f"Receptor PDB not found: {receptor_pdb}")
        if not parent_sdf.exists():
            raise FileNotFoundError(f"Parent ligand SDF not found: {parent_sdf}")

        out_dir.mkdir(parents=True, exist_ok=True)

        for att in attachment_points:
            att_id = str(att.get("attachment_id", ""))
            xyz = att.get("attachment_xyz", "")
            if isinstance(xyz, (list, tuple)):
                xyz = ",".join(str(v) for v in xyz)
            if not xyz:
                continue

            attempt_id = uuid.uuid4().hex
            tsv_path = out_dir / (
                f"df2_{slugify(seed_pair_id)}_{slugify(att_id)}_r{round_id}_{attempt_id}.tsv"
            )
            tsv_path = tsv_path.resolve()  # absolute path — cwd will differ
            # The path is unique for this invocation. Unlink defensively so even a
            # deterministic UUID test double cannot expose a stale output.
            tsv_path.unlink(missing_ok=True)
            cmd = [
                str(py_exe),
                str(df2_script),
                "--receptor", str(receptor_pdb.resolve()),
                "--ligand", str(parent_sdf.resolve()),
                f"--branch_atm_loc_xyz={xyz}",
                "--out", str(tsv_path),
                "--num_inference_predictions", str(fragments_per_attachment_point),
            ]

            try:
                result = subprocess.run(
                    cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=remaining_timeout(600),  # Existing per-attachment cap, clipped by stage budget.
                    cwd=str(self._PROJECT_ROOT),
                )
                tsv_path.with_suffix(".stdout.log").write_text(getattr(result, "stdout", "") or "", encoding="utf-8")
                tsv_path.with_suffix(".stderr.log").write_text(getattr(result, "stderr", "") or "", encoding="utf-8")
                if result.returncode != 0:
                    print(f"[DeepFrag2] WARNING: returncode={result.returncode} for {att_id}", file=sys.stderr)
                    print(f"[DeepFrag2] stderr: {result.stderr[-2000:]}", file=sys.stderr)
                    tsv_path.unlink(missing_ok=True)
                    continue
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or b""
                error = exc.stderr or b""
                tsv_path.with_suffix(".stdout.log").write_text(output.decode(errors="replace") if isinstance(output, bytes) else output, encoding="utf-8")
                tsv_path.with_suffix(".stderr.log").write_text((error.decode(errors="replace") if isinstance(error, bytes) else error) + "\nDeepFrag2 timeout\n", encoding="utf-8")
                print(f"[DeepFrag2] TIMEOUT for {att_id}", file=sys.stderr)
                tsv_path.unlink(missing_ok=True)
                continue
            except Exception as exc:
                print(f"[DeepFrag2] ERROR for {att_id}: {exc}", file=sys.stderr)
                tsv_path.unlink(missing_ok=True)
                continue

            if not tsv_path.is_file() or tsv_path.stat().st_size == 0:
                print(f"[DeepFrag2] Missing or empty current output for {att_id}: {tsv_path}", file=sys.stderr)
                tsv_path.unlink(missing_ok=True)
                continue
            parsed = self._parse_tsv(tsv_path, seed_pair_id, round_id, att_id)
            if not parsed:
                print(f"[DeepFrag2] Current output has no valid fragment rows for {att_id}: {tsv_path}", file=sys.stderr)
                tsv_path.unlink(missing_ok=True)
                continue
            rows.extend(parsed)

        write_rows(out_dir / "deepfrag2_fragments.csv", rows, FRAGMENT_FIELDS)
        return rows

    # ------------------------------------------------------------------
    # TSV parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tsv(
        tsv_path: Path,
        seed_pair_id: str,
        round_id: int,
        attachment_id: str,
    ) -> list[dict[str, Any]]:
        """Parse a DeepFrag2 output TSV into fragment rows."""
        rows: list[dict[str, Any]] = []
        try:
            with tsv_path.open("r", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                for idx, rec in enumerate(reader, start=1):
                    # DeepFrag2 TSV typically has columns like:
                    #   SMILES  Score  (and possibly others)
                    # We normalise the keys.
                    smiles = ""
                    score = 0.0
                    for key, val in rec.items():
                        kl = key.strip().lower()
                        if kl in ("smiles", "fragment", "fragment_smiles"):
                            smiles = str(val).strip()
                        elif kl in ("score", "fragment_score", "prediction_score", "rankscore"):
                            try:
                                score = float(val)
                            except (ValueError, TypeError):
                                score = 0.0

                    if not smiles:
                        continue

                    rows.append({
                        "seed_pair_id": seed_pair_id,
                        "round": round_id,
                        "attachment_id": attachment_id,
                        "fragment_rank": idx,
                        "fragment_score": round(score, 6),
                        "fragment_smiles": smiles,
                        "backend": "deepfrag2",
                        "generation_backend": "deepfrag2",
                        "execution_mode": "production",
                        "fallback_reason": "",
                        "model_name": "gte_4_best",
                        "raw_output_path": str(tsv_path),
                    })
        except Exception as exc:
            print(f"[DeepFrag2] TSV parse error: {exc}", file=sys.stderr)
        return rows

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import ensure_dir, slugify, write_rows


BOX_FIELDS = [
    "box_id",
    "target_gene",
    "pdb_id",
    "chain_id",
    "box_source",
    "box_defining_ligand_id",
    "box_defining_ligand_resname",
    "box_defining_ligand_chain",
    "box_defining_ligand_resseq",
    "box_defining_ligand_file",
    "cocrystal_ligand_id",
    "cocrystal_ligand_file",
    "cluster_id",
    "num_ligands_in_cluster",
    "center_x",
    "center_y",
    "center_z",
    "size_x",
    "size_y",
    "size_z",
    "padding",
    "min_box_size",
    "binding_site_status",
    "failure_reason",
    "warning",
    "confidence_level",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(f)]


def ligand_id_from_file(path: Path) -> str:
    stem = path.stem
    return stem.split("_")[-1] if "_" in stem else stem


def ligand_metadata_from_file(path: Path) -> dict[str, str]:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        return {
            "resname": line[17:20].strip(),
            "chain": line[21:22].strip(),
            "resseq": line[22:27].strip(),
        }
    return {"resname": ligand_id_from_file(path), "chain": "", "resseq": ""}


def read_ligand_coords(path: Path) -> list[tuple[float, float, float]]:
    coords: list[tuple[float, float, float]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        element = line[76:78].strip().upper()
        atom_name = line[12:16].strip().upper()
        if element == "H" or atom_name.startswith("H"):
            continue
        coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
    return coords


def failed_box(row: dict[str, str], reason: str) -> dict[str, Any]:
    ligand_file = row.get("cocrystal_ligand_file", "")
    return {
        "box_id": "",
        "target_gene": row.get("target_gene", ""),
        "pdb_id": row.get("pdb_id", ""),
        "chain_id": row.get("chain_id", ""),
        "box_source": "reference_pocket_ligand",
        "box_defining_ligand_id": ligand_id_from_file(Path(ligand_file)) if ligand_file else "",
        "box_defining_ligand_resname": "",
        "box_defining_ligand_chain": "",
        "box_defining_ligand_resseq": "",
        "box_defining_ligand_file": ligand_file,
        "cocrystal_ligand_id": ligand_id_from_file(Path(ligand_file)) if ligand_file else "",
        "cocrystal_ligand_file": ligand_file,
        "cluster_id": "",
        "num_ligands_in_cluster": "",
        "center_x": "",
        "center_y": "",
        "center_z": "",
        "size_x": "",
        "size_y": "",
        "size_z": "",
        "padding": "",
        "min_box_size": "",
        "binding_site_status": "failed",
        "failure_reason": reason,
        "warning": "",
        "confidence_level": "not_applicable",
    }


def centroid(coords: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    return tuple(sum(coord[i] for coord in coords) / len(coords) for i in range(3))  # type: ignore[return-value]


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def define_binding_sites(
    receptor_qc: Path,
    *,
    out_dir: Path,
    padding: float = 8.0,
    min_size: float = 18.0,
    max_boxes_per_receptor: int = 3,
    cluster_distance: float = 8.0,
) -> list[dict[str, Any]]:
    rows = read_rows(receptor_qc)
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.get("preparation_status") != "success":
            continue
        ligand_file_values = [
            value.strip()
            for value in str(row.get("reference_ligand_files") or row.get("cocrystal_ligand_file", "")).split(";")
            if value.strip()
        ]
        if not ligand_file_values:
            output_rows.append(failed_box(row, "no_valid_reference_pocket_ligand"))
            continue
        ligand_items: list[dict[str, Any]] = []
        for value in ligand_file_values:
            ligand_file = Path(value)
            fail_row = {**row, "cocrystal_ligand_file": value}
            if not ligand_file.exists():
                output_rows.append(failed_box(fail_row, "missing_cocrystal_ligand_file"))
                continue
            if ligand_file.stat().st_size == 0:
                output_rows.append(failed_box(fail_row, "empty_cocrystal_ligand_file"))
                continue
            try:
                coords = read_ligand_coords(ligand_file)
            except Exception:
                output_rows.append(failed_box(fail_row, "box_calculation_failed"))
                continue
            if not coords:
                output_rows.append(failed_box(fail_row, "no_ligand_coordinates"))
                continue
            ligand_items.append({"file": ligand_file, "coords": coords, "center": centroid(coords)})

        clusters: list[list[dict[str, Any]]] = []
        for item in ligand_items:
            for cluster in clusters:
                if distance(item["center"], cluster[0]["center"]) <= cluster_distance:
                    cluster.append(item)
                    break
            else:
                clusters.append([item])

        for cluster_index, cluster in enumerate(clusters[:max_boxes_per_receptor], start=1):
            coords = [coord for item in cluster for coord in item["coords"]]
            mins = [min(coord[i] for coord in coords) for i in range(3)]
            maxs = [max(coord[i] for coord in coords) for i in range(3)]
            centers = [sum(coord[i] for coord in coords) / len(coords) for i in range(3)]
            sizes = [max(maxs[i] - mins[i] + padding, min_size) for i in range(3)]
            warning = "large_box_warning" if any(size > 30.0 for size in sizes) else ""
            first_file = cluster[0]["file"]
            metadata = ligand_metadata_from_file(first_file)
            ligand_id = metadata["resname"] or ligand_id_from_file(first_file)
            box_id = (
                f"{slugify(row.get('target_gene', 'target'))}_{slugify(row.get('pdb_id', 'pdb'))}_"
                f"{slugify(row.get('chain_id', 'chain'))}_{slugify(ligand_id)}_box{cluster_index}"
            )
            output_rows.append(
                {
                    "box_id": box_id,
                    "target_gene": row.get("target_gene", ""),
                    "pdb_id": row.get("pdb_id", ""),
                    "chain_id": row.get("chain_id", ""),
                    "box_source": "reference_pocket_ligand",
                    "box_defining_ligand_id": ligand_id,
                    "box_defining_ligand_resname": metadata["resname"],
                    "box_defining_ligand_chain": metadata["chain"],
                    "box_defining_ligand_resseq": metadata["resseq"],
                    "box_defining_ligand_file": str(first_file),
                    "cocrystal_ligand_id": ligand_id,
                    "cocrystal_ligand_file": str(first_file),
                    "cluster_id": f"cluster_{cluster_index}",
                    "num_ligands_in_cluster": len(cluster),
                    "center_x": round(centers[0], 3),
                    "center_y": round(centers[1], 3),
                    "center_z": round(centers[2], 3),
                    "size_x": round(sizes[0], 3),
                    "size_y": round(sizes[1], 3),
                    "size_z": round(sizes[2], 3),
                    "padding": padding,
                    "min_box_size": min_size,
                    "binding_site_status": "defined",
                    "failure_reason": "",
                    "warning": warning,
                    "confidence_level": "high" if not warning else "medium",
                }
            )
    out_dir = ensure_dir(out_dir)
    write_rows(out_dir / "binding_site_boxes.csv", output_rows, BOX_FIELDS)
    return output_rows

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.network_pharmacology.scripts.common import (
    ensure_dir,
    exit_with_errors,
    load_params,
    now_utc,
    output_root,
    project_root_from_cwd,
    read_rows,
    unique_preserve,
    write_json,
    write_rows,
)


INTERSECTION_FIELDS = [
    "target_gene",
    "uniprot_id",
    "target_name",
    "disease_association_score",
    "compound_count",
    "ingredient_names",
    "source_count",
]

MATRIX_FIELDS = [
    "ingredient_name",
    "target_gene",
    "present",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Intersect compound targets with disease targets.")
    parser.add_argument("--config", default="results/network_pharmacology/00_input/params.resolved.yaml")
    args = parser.parse_args()

    project_root_from_cwd()
    params = load_params(args.config)
    out_root = output_root(params)
    intersection_dir = ensure_dir(out_root / "04_intersection")

    compound_targets = read_rows(out_root / "02_compound_targets" / "compound_targets.normalized.csv")
    disease_targets = read_rows(out_root / "03_disease_targets" / "disease_targets.normalized.csv")

    disease_by_gene = {row["target_gene"].strip().upper(): row for row in disease_targets if row.get("target_gene")}
    compound_by_gene: dict[str, list[dict]] = defaultdict(list)
    for row in compound_targets:
        gene = row.get("target_gene", "").strip().upper()
        if gene:
            compound_by_gene[gene].append(row)

    common_genes = sorted(set(compound_by_gene) & set(disease_by_gene))
    rows: list[dict] = []
    matrix_rows: list[dict] = []

    for gene in common_genes:
        compound_rows = compound_by_gene[gene]
        disease_row = disease_by_gene[gene]
        ingredient_names = unique_preserve(row.get("ingredient_name", "") for row in compound_rows)
        target_names = unique_preserve(
            [disease_row.get("target_name", "")]
            + [row.get("target_name", "") for row in compound_rows]
        )
        rows.append(
            {
                "target_gene": gene,
                "uniprot_id": next((row.get("uniprot_id", "") for row in compound_rows if row.get("uniprot_id")), ""),
                "target_name": target_names[0] if target_names else "",
                "disease_association_score": disease_row.get("disease_association_score", ""),
                "compound_count": len(ingredient_names),
                "ingredient_names": "; ".join(ingredient_names),
                "source_count": 2,
            }
        )
        for ingredient in ingredient_names:
            matrix_rows.append({"ingredient_name": ingredient, "target_gene": gene, "present": 1})

    rows.sort(
        key=lambda row: (
            float(row.get("compound_count") or 0),
            float(row.get("disease_association_score") or 0),
        ),
        reverse=True,
    )

    write_rows(intersection_dir / "intersection_targets.csv", rows, INTERSECTION_FIELDS)
    write_rows(intersection_dir / "compound_disease_target_matrix.csv", matrix_rows, MATRIX_FIELDS)
    write_json(
        intersection_dir / "intersection_summary.json",
        {
            "module": "intersect_targets",
            "status": "passed" if rows else "warning",
            "generated_at": now_utc(),
            "compound_target_unique_count": len(compound_by_gene),
            "disease_target_unique_count": len(disease_by_gene),
            "intersection_target_count": len(rows),
            "top_intersection_targets": [row["target_gene"] for row in rows[:20]],
            "generated_files": [
                str(intersection_dir / "intersection_targets.csv"),
                str(intersection_dir / "compound_disease_target_matrix.csv"),
            ],
            "warnings": [] if rows else ["No intersecting targets were found between compound targets and disease targets."],
        },
    )
    print(f"Found {len(rows)} intersecting targets.")


if __name__ == "__main__":
    exit_with_errors(main)

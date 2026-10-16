#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.network_pharmacology.scripts.common import (
    ensure_dir,
    exit_with_errors,
    http_post,
    load_params,
    now_utc,
    output_root,
    project_root_from_cwd,
    read_rows,
    write_json,
    write_rows,
)


EDGE_FIELDS = [
    "protein_a",
    "protein_b",
    "preferred_name_a",
    "preferred_name_b",
    "score",
    "source",
]

NODE_FIELDS = [
    "target_gene",
    "degree",
    "source",
]


def fetch_string_network(genes: list[str], required_score: int) -> list[dict]:
    if len(genes) < 2:
        return []
    response = http_post(
        "https://string-db.org/api/tsv/network",
        data={
            "identifiers": "\r".join(genes),
            "species": 9606,
            "required_score": required_score,
            "network_type": "functional",
            "caller_identity": "NPFlow-Agent",
        },
        timeout=80,
    )
    if response.status_code != 200:
        raise RuntimeError(f"STRING HTTP {response.status_code}; association data unavailable")
    lines = [line for line in response.text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    if not {"preferredName_A", "preferredName_B", "score"}.issubset(header):
        raise ValueError("STRING response is not a network TSV")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build STRING PPI network for intersecting targets.")
    parser.add_argument("--config", default="results/network_pharmacology/00_input/params.resolved.yaml")
    args = parser.parse_args()

    project_root_from_cwd()
    params = load_params(args.config)
    out_root = output_root(params)
    ppi_dir = ensure_dir(out_root / "05_ppi")

    intersection = read_rows(out_root / "04_intersection" / "intersection_targets.csv")
    genes = sorted({row["target_gene"].strip().upper() for row in intersection if row.get("target_gene")})
    required_score = int(float(params.get("ppi", {}).get("string_required_score", 0.7)) * 1000)
    source_error = ""
    try:
        string_rows = fetch_string_network(genes, required_score)
    except Exception as exc:
        string_rows = []
        source_error = str(exc)
    source_status = "unavailable" if source_error else ("success" if string_rows else "success_empty")

    graph = nx.Graph()
    graph.add_nodes_from(genes)
    edge_rows: list[dict] = []
    for row in string_rows:
        gene_a = (row.get("preferredName_A") or "").upper()
        gene_b = (row.get("preferredName_B") or "").upper()
        if gene_a not in genes or gene_b not in genes or gene_a == gene_b:
            continue
        score = float(row.get("score") or 0)
        graph.add_edge(gene_a, gene_b, score=score, source="STRING")
        edge_rows.append(
            {
                "protein_a": row.get("stringId_A", ""),
                "protein_b": row.get("stringId_B", ""),
                "preferred_name_a": gene_a,
                "preferred_name_b": gene_b,
                "score": f"{score:.6g}",
                "source": "STRING",
            }
        )

    node_rows = [
        {"target_gene": node, "degree": graph.degree(node) if not source_error else "", "source": "STRING", "source_status": source_status}
        for node in sorted(graph.nodes())
    ]

    write_rows(ppi_dir / "ppi_edges.tsv", edge_rows, EDGE_FIELDS)
    write_rows(ppi_dir / "ppi_nodes.tsv", node_rows, NODE_FIELDS + ["source_status"])
    nx.write_graphml(graph, ppi_dir / "ppi_network.graphml")
    write_json(
        ppi_dir / "ppi_summary.json",
        {
            "module": "build_ppi",
            "status": "passed" if graph.number_of_edges() else "warning",
            "generated_at": now_utc(),
            "source": "STRING",
            "source_status": source_status,
            "required_score": required_score,
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "average_degree": (sum(dict(graph.degree()).values()) / graph.number_of_nodes())
            if graph.number_of_nodes()
            else 0,
            "generated_files": [
                str(ppi_dir / "ppi_edges.tsv"),
                str(ppi_dir / "ppi_nodes.tsv"),
                str(ppi_dir / "ppi_network.graphml"),
            ],
            "warnings": ["STRING unavailable; missing data are not negative evidence: " + source_error] if source_error else (
                [] if graph.number_of_edges() else ["STRING returned no interactions above threshold."]),
        },
    )
    print(f"Built PPI network with {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges.")


if __name__ == "__main__":
    exit_with_errors(main)

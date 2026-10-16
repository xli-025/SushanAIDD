#!/usr/bin/env python3
"""Rank core targets and select docking ligands using weighted multi-factor scoring.

Replaces the old equal-weight mean-rank method with:
  - ligand_score = 0.15*ingredient_confidence + 0.20*structure_quality
                   + 0.30*target_link + 0.25*disease_relevance + 0.10*multi_source
  - target_score = 0.25*source_confidence + 0.25*disease_association_norm
                   + 0.20*ppi_topology + 0.20*compound_coverage + 0.10*multi_source
"""
from __future__ import annotations

import argparse
import hashlib
import math
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.network_pharmacology.scripts.common import (
    ensure_dir,
    exit_with_errors,
    fail,
    load_params,
    now_utc,
    output_root,
    project_root_from_cwd,
    read_rows,
    safe_log2,
    write_json,
    write_rows,
)


# ── updated output schemas ───────────────────────────────────────────────────

FIELDS = [
    "target_gene",
    "uniprot_id",
    "target_name",
    "disease_association_score",
    "compound_count",
    "degree",
    "betweenness",
    "closeness",
    "ppi_degree_rank",
    "disease_association_rank",
    "compound_coverage_rank",
    "core_rank",
    "target_score",
    "target_confidence",
    "source_confidence_score",
    "disease_association_norm",
    "ppi_topology_score",
    "compound_coverage_score",
    "multi_source_support_score",
    "source_list",
    "source_count",
    "eligible_edge_count",
    "high_confidence_ligand_count",
    "hub_flag",
    "hub_reason",
    "interpretation_note",
    "recommended_for_docking",
    "selection_reason",
]

DOCKING_FIELDS = [
    "target_gene",
    "uniprot_id",
    "target_name",
    "target_score",
    "target_confidence",
    "disease_association_score",
    "compound_count",
    "degree",
    "source_list",
    "source_count",
    "eligible_edge_count",
    "source_confidence_score",
    "ppi_topology_score",
    "compound_coverage_score",
    "high_confidence_ligand_count",
    "hub_flag",
    "hub_reason",
    "interpretation_note",
    "recommended_for_docking",
    "selection_reason",
]

DOCKING_LIGAND_FIELDS = [
    "contract_version",
    "selection_status",
    "selection_exclusion_reason",
    "ligand_id",
    "pubchem_cid",
    "inchikey",
    "canonical_smiles",
    "isomeric_smiles",
    "source_ingredient_ids",
    "source_ingredient_names",
    "source_herb_names",
    "source_list",
    "source_count",
    "compound_target_count",
    "docking_target_count",
    "linked_docking_targets",
    "ligand_score",
    "ligand_confidence",
    "ingredient_confidence",
    "structure_quality_score",
    "target_link_score",
    "disease_relevance_score",
    "multi_source_support_score",
    "max_target_confidence",
    "mean_target_confidence",
    "pubchem_match_status",
    "selection_reason",
]

KNOWN_PUBLIC_HUBS = {
    "TNF", "IL6", "TP53", "AKT1", "CASP3", "CASP8", "MYC", "MMP9",
    "VEGFA", "EGFR", "MAPK1", "JUN", "FOS", "APP", "ESR1", "PTGS2", "TGFB1",
}


# ── helper functions ─────────────────────────────────────────────────────────

def _float_or(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


KNOWN_EVIDENCE_SOURCES = {"TCMSP", "HerbiV", "SymMap2"}


def _sources(value: object) -> set[str]:
    return {part.strip() for part in str(value or "").split(";") if part.strip() in KNOWN_EVIDENCE_SOURCES}


def _edge_is_network_eligible(row: dict) -> bool:
    status = str(
        row.get("network_eligibility")
        or row.get("source_eligibility")
        or row.get("eligibility_category")
        or ""
    ).strip().lower()
    if not status:
        return True  # legacy versioned union files predate this column
    return status in {
        "true",
        "eligible",
        "candidate",
        "selected",
        "main_network",
        "pending_activity_filter",
    }


def _required_selection_bool(section: dict, key: str, section_name: str) -> bool:
    """Reject string/integer truthiness at the ranking entry point."""
    value = section.get(key)
    if type(value) is not bool:
        fail(f"{section_name}.{key} must be a boolean (true or false).")
    return value


def _apply_edge_override(row: dict, override: dict | None) -> dict:
    """Overlay finalized fields while retaining legacy non-empty edge data."""
    if not override:
        return row
    merged = dict(row)
    merged.update(
        {
            key: value
            for key, value in override.items()
            if value is not None and str(value).strip()
        }
    )
    return merged


def build_target_provenance(
    union_targets: list[dict],
    union_edges: list[dict],
    *,
    active_pairs: set[tuple[str, str]] | None = None,
    active_edge_overrides: dict[tuple[str, str], dict] | None = None,
    edge_contract_present: bool | None = None,
) -> dict[str, dict]:
    """Derive provider support and ligand evidence from eligible union edges."""
    has_edge_contract = bool(union_edges) if edge_contract_present is None else edge_contract_present
    output: dict[str, dict] = {}
    for row in union_targets:
        gene = str(row.get("target_gene", "")).strip().upper()
        if not gene:
            continue
        output.setdefault(
            gene,
            {"sources": set(), "target_confidence": 0.0, "high_confidence_ligands": set(), "edge_count": 0},
        )

    genes_with_eligible_edges: set[str] = set()
    for row in union_edges:
        edge_pair = (
            str(row.get("ingredient_id", "") or "").strip(),
            str(row.get("target_gene", "") or "").strip().upper(),
        )
        if active_pairs is not None and edge_pair not in active_pairs:
            continue
        if not _edge_is_network_eligible(row):
            continue
        effective_row = _apply_edge_override(
            row, (active_edge_overrides or {}).get(edge_pair)
        )
        gene = str(effective_row.get("target_gene", "")).strip().upper()
        if not gene:
            continue
        genes_with_eligible_edges.add(gene)
        item = output.setdefault(
            gene,
            {"sources": set(), "target_confidence": 0.0, "high_confidence_ligands": set(), "edge_count": 0},
        )
        item["sources"].update(
            _sources(
                effective_row.get("eligible_source_list")
                or effective_row.get("source_list")
            )
        )
        item["target_confidence"] = max(
            float(item["target_confidence"]),
            _float_or(effective_row.get("relation_confidence")),
        )
        item["edge_count"] = int(item["edge_count"]) + 1
        if _float_or(effective_row.get("ingredient_confidence")) >= 0.70:
            ligand_key = str(
                row.get("canonical_ingredient_id")
                or row.get("ingredient_id")
                or row.get("ingredient_name")
                or ""
            ).strip()
            if ligand_key:
                item["high_confidence_ligands"].add(ligand_key)

    # A legacy target file may not have an edge file. Only then use its target-
    # level sources; never let exploratory/ineligible edges inflate support.
    for row in union_targets if not has_edge_contract else []:
        gene = str(row.get("target_gene", "")).strip().upper()
        if gene and gene not in genes_with_eligible_edges:
            output[gene]["sources"].update(_sources(row.get("source_list")))
            output[gene]["target_confidence"] = max(
                float(output[gene]["target_confidence"]),
                _float_or(row.get("target_confidence")),
            )
    return output


def select_docking_ligands(candidates: list[dict], selection: dict) -> tuple[list[dict], list[dict]]:
    """Apply the complete versioned docking-ligand selection contract."""
    min_score = _float_or(selection.get("min_ligand_score"), 0.55)
    min_confidence = _float_or(selection.get("min_ligand_confidence"), 0.55)
    top_n = int(selection.get("top_n", 20))
    require_structure = _required_selection_bool(
        selection, "require_structure", "selection.docking_ligands"
    )
    require_linked_target = _required_selection_bool(
        selection,
        "require_linked_docking_target",
        "selection.docking_ligands",
    )

    audited: list[dict] = []
    eligible: list[dict] = []
    for candidate in candidates:
        row = dict(candidate)
        row["contract_version"] = "docking_ligands.v2"
        reasons: list[str] = []
        if _float_or(row.get("ligand_score")) < min_score:
            reasons.append("below_min_ligand_score")
        if _float_or(row.get("ligand_confidence")) < min_confidence:
            reasons.append("below_min_ligand_confidence")
        if require_structure and not str(
            row.get("canonical_smiles") or row.get("isomeric_smiles") or ""
        ).strip():
            reasons.append("missing_required_structure")
        if require_linked_target and not str(row.get("linked_docking_targets") or "").strip():
            reasons.append("missing_linked_docking_target")
        row["selection_status"] = "excluded" if reasons else "eligible"
        row["selection_exclusion_reason"] = "; ".join(reasons)
        audited.append(row)
        if not reasons:
            eligible.append(row)

    eligible.sort(key=lambda row: (-_float_or(row.get("ligand_score")), str(row.get("ligand_id", ""))))
    eligible_ids = {id(row) for row in eligible}
    selected_ids = {id(row) for row in eligible[:top_n]}
    selected: list[dict] = []
    for row in audited:
        if id(row) in selected_ids:
            row["selection_status"] = "selected"
            row["selection_exclusion_reason"] = ""
            selected.append(row)
        elif id(row) in eligible_ids:
            row["selection_status"] = "excluded"
            row["selection_exclusion_reason"] = "outside_top_n"
    selected.sort(key=lambda row: (-_float_or(row.get("ligand_score")), str(row.get("ligand_id", ""))))
    return selected, audited


def _norm_score(values: list[float], *, zero_is_absence: bool = False) -> list[float]:
    """Min-max normalization without inventing evidence from an all-zero vector."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        if zero_is_absence and hi == 0:
            return [0.0] * len(values)
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def target_family(gene: str) -> str:
    gene = (gene or "").upper().strip()
    if gene.startswith(("GABRA", "GABRB", "GABRG", "GABRD", "GABRE", "GABRP", "GABRQ")):
        return "GABA_receptor_family"
    if gene.startswith("HTR"):
        return "serotonin_receptor_family"
    if gene.startswith(("ADRA", "ADRB")):
        return "adrenergic_receptor_family"
    if gene.startswith("CYP"):
        return "CYP_family"
    if gene.startswith("KCN"):
        return "potassium_channel_family"
    return ""


def family_dominance(rows: list[dict], limit: int = 20) -> dict:
    counts: dict[str, int] = {}
    considered = rows[:limit]
    for row in considered:
        family = target_family(str(row.get("target_gene", "")))
        if family:
            counts[family] = counts.get(family, 0) + 1
    if not counts:
        return {"top_target_family": "", "dominant_family_target_count": 0,
                "dominant_family_fraction": 0, "target_family_counts": {}}
    top_family, top_count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    denominator = len(considered) or 1
    return {
        "top_target_family": top_family,
        "dominant_family_target_count": top_count,
        "dominant_family_fraction": top_count / denominator,
        "target_family_counts": counts,
    }


def enrichment_theme_dominance(rows: list[dict], limit: int = 20) -> dict:
    considered = rows[:limit]
    gaba_hits = []
    for row in considered:
        text = f"{row.get('name', '')} {row.get('native', '')}".upper()
        if any(token in text for token in ("GABA", "GAMMA-AMINOBUTYRIC", "BENZODIAZEPINE", "CHLORIDE CHANNEL")):
            gaba_hits.append(row.get("name", "") or row.get("native", ""))
    denominator = len(considered) or 1
    return {
        "top_enrichment_theme": "GABA_receptor_or_chloride_channel" if gaba_hits else "",
        "dominant_enrichment_theme_count": len(gaba_hits),
        "dominant_enrichment_theme_fraction": len(gaba_hits) / denominator,
        "dominant_enrichment_theme_examples": gaba_hits[:5],
    }


def load_graph(path) -> nx.Graph:
    if path.exists():
        return nx.read_graphml(path)
    return nx.Graph()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def hub_annotation(row: dict, *, degree_p75: float, degree_p90: float) -> tuple[str, str, str]:
    reasons = []
    gene = row.get("target_gene", "")
    degree = _float_or(row.get("degree"))
    compound_count = _float_or(row.get("compound_count"))
    if degree >= degree_p90 and degree > 0:
        reasons.append("high_ppi_degree")
    if gene in KNOWN_PUBLIC_HUBS:
        reasons.append("known_public_hub")
    if degree >= degree_p75 and compound_count <= 2 and degree > 0:
        reasons.append("high_degree_low_compound_support")
    if not reasons:
        return "false", "", ""
    return "true", "; ".join(reasons), "high-connectivity/public hub; interpret cautiously"


def ligand_identity(row: dict) -> str:
    for field in ("inchikey", "pubchem_cid", "canonical_smiles", "isomeric_smiles"):
        value = str(row.get(field, "")).strip()
        if value:
            return f"{field}:{value}"
    fallback = str(row.get("ingredient_id", "")).strip() or str(row.get("ingredient_name", "")).strip()
    return f"ingredient:{fallback}"


def ligand_id_from_group(group: dict) -> str:
    if group["inchikey"]:
        return group["inchikey"]
    if group["pubchem_cid"]:
        return f"CID:{group['pubchem_cid']}"
    smiles = group["canonical_smiles"] or group["isomeric_smiles"]
    if smiles:
        return f"SMILES:{hashlib.sha1(smiles.encode('utf-8')).hexdigest()[:12]}"
    return "LIGAND:" + hashlib.sha1(";".join(sorted(group["source_ingredient_ids"])).encode("utf-8")).hexdigest()[:12]


# ── weighted scoring functions ───────────────────────────────────────────────

def compute_ligand_score(
    ingredient_confidence: float,
    structure_quality_score: float,
    target_link_score: float,
    disease_relevance_score: float,
    multi_source_support_score: float,
    weights: dict,
) -> dict:
    """Compute ligand_score per NPFlow scoring addendum.

    ligand_score = 0.15*ingredient_confidence + 0.20*structure_quality
                 + 0.30*target_link + 0.25*disease_relevance + 0.10*multi_source
    """
    score = (
        weights["ingredient_confidence"] * ingredient_confidence
        + weights["structure_quality"] * structure_quality_score
        + weights["target_link"] * target_link_score
        + weights["disease_relevance"] * disease_relevance_score
        + weights["multi_source_support"] * multi_source_support_score
    )
    return {
        "ligand_score": round(score, 4),
        "ingredient_confidence": round(ingredient_confidence, 4),
        "structure_quality_score": round(structure_quality_score, 4),
        "target_link_score": round(target_link_score, 4),
        "disease_relevance_score": round(disease_relevance_score, 4),
        "multi_source_support_score": round(multi_source_support_score, 4),
        "ligand_confidence": round(ingredient_confidence, 4),  # primary confidence driver
    }


def compute_target_score(
    source_confidence: float,
    disease_association_norm: float,
    ppi_topology_score: float,
    compound_coverage_score: float,
    multi_source_support_score: float,
    weights: dict,
) -> dict:
    """Compute target_score per NPFlow scoring addendum.

    target_score = 0.25*source_confidence + 0.25*disease_association_norm
                 + 0.20*ppi_topology + 0.20*compound_coverage + 0.10*multi_source
    """
    score = (
        weights["source_confidence"] * source_confidence
        + weights["disease_association"] * disease_association_norm
        + weights["ppi_topology"] * ppi_topology_score
        + weights["compound_coverage"] * compound_coverage_score
        + weights["multi_source_support"] * multi_source_support_score
    )
    return {
        "target_score": round(score, 4),
        "source_confidence_score": round(source_confidence, 4),
        "disease_association_norm": round(disease_association_norm, 4),
        "ppi_topology_score": round(ppi_topology_score, 4),
        "compound_coverage_score": round(compound_coverage_score, 4),
        "multi_source_support_score": round(multi_source_support_score, 4),
    }


def structure_quality(row: dict) -> float:
    """Score a ligand's structural readiness for docking (0-1)."""
    has_cid = bool(str(row.get("pubchem_cid", "")).strip())
    has_smiles = bool(str(row.get("canonical_smiles", "") or row.get("smiles", "")).strip())
    has_inchikey = bool(str(row.get("inchikey", "")).strip())
    if has_cid and has_smiles and has_inchikey:
        return 1.00
    if has_smiles and has_inchikey:
        return 0.85
    if has_smiles:
        return 0.70
    return 0.00


def multi_source_score(source_list_str: str) -> float:
    """Score multi-source support: 1→0.50, 2→0.80, 3+→1.00."""
    sources = {s.strip() for s in str(source_list_str or "").split(";") if s.strip()}
    sources &= {"TCMSP", "HerbiV", "SymMap2"}
    n = len(sources)
    if n >= 3:
        return 1.00
    if n == 2:
        return 0.80
    if n == 1:
        return 0.50
    return 0.0  # no recognized source support


# ── ligand collapse with scoring ─────────────────────────────────────────────

def collapse_docking_ligands(
    ligand_ready: list[dict],
    ligand_to_targets: dict[str, set[str]],
    docking_genes: set[str],
    target_confidence_lookup: dict[str, float],
    compound_target_lookup: dict[str, list[dict]],
    disease_genes: set[str],
    lw: dict,
) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in ligand_ready:
        ingredient_id = row.get("ingredient_id", "")
        linked_targets = set(ligand_to_targets.get(ingredient_id, set())) & docking_genes
        if not linked_targets:
            continue
        key = ligand_identity(row)
        item = grouped.setdefault(
            key,
            {
                "pubchem_cid": str(row.get("pubchem_cid", "")).strip(),
                "inchikey": str(row.get("inchikey", "")).strip(),
                "canonical_smiles": str(row.get("canonical_smiles", "") or row.get("smiles", "")).strip(),
                "isomeric_smiles": str(row.get("isomeric_smiles", "")).strip(),
                "source_ingredient_ids": set(),
                "source_ingredient_names": set(),
                "source_herb_names": set(),
                "source_lists": set(),
                "ingredient_confidence": 0.0,
                "compound_targets": set(),
                "docking_targets": set(),
                "disease_intersection_targets": set(),
                "target_confidences": [],
                "pubchem_match_status": set(),
            },
        )
        for field in ("pubchem_cid", "inchikey", "canonical_smiles", "isomeric_smiles"):
            if not item[field] and row.get(field):
                item[field] = str(row.get(field, "")).strip()
        if not item["canonical_smiles"] and row.get("smiles"):
            item["canonical_smiles"] = str(row.get("smiles", "")).strip()
        item["source_ingredient_ids"].add(ingredient_id)
        item["source_ingredient_names"].add(row.get("ingredient_name", ""))
        item["source_herb_names"].add(row.get("herb_name", ""))
        item["compound_targets"].update(ligand_to_targets.get(ingredient_id, set()))
        item["docking_targets"].update(linked_targets)
        item["disease_intersection_targets"].update(linked_targets & disease_genes)
        item["pubchem_match_status"].add(row.get("pubchem_match_status", ""))
        # collect source list and confidence
        src = row.get("source_list", "") or row.get("source", "")
        for s in str(src).split(";"):
            item["source_lists"].add(s.strip())
        ic = _float_or(row.get("ingredient_confidence"))
        if ic > item["ingredient_confidence"]:
            item["ingredient_confidence"] = ic
        # collect target confidences for linked docking targets
        for tg in linked_targets:
            tc = target_confidence_lookup.get(tg, 0.0)
            if tc > 0:
                item["target_confidences"].append(tc)

    output = []
    for item in grouped.values():
        total_targets = len(item["compound_targets"])
        docking_target_count_val = len(item["docking_targets"])
        disease_target_count = len(item["disease_intersection_targets"])
        source_list_str = "; ".join(sorted(s for s in item["source_lists"] if s))
        source_count_val = len([s for s in item["source_lists"] if s in {"TCMSP", "HerbiV", "SymMap2"}]) or 1

        # ── sub-scores ──
        # Missing confidence is absence of evidence; do not manufacture a
        # medium-confidence ligand value.
        ic = item["ingredient_confidence"]
        sq = structure_quality(item)

        # target_link_score: log2(1 + high-conf docking target count) / log2(1 + cap)
        tl = safe_log2(min(docking_target_count_val, 20)) / safe_log2(21) if docking_target_count_val else 0.0

        # disease_relevance_score
        if disease_target_count >= 5:
            dr = 1.00
        elif disease_target_count >= 3:
            dr = 0.85
        elif disease_target_count >= 1:
            dr = 0.70
        else:
            dr = 0.0

        ms = multi_source_score(source_list_str)

        scored = compute_ligand_score(
            ingredient_confidence=ic,
            structure_quality_score=sq,
            target_link_score=tl,
            disease_relevance_score=dr,
            multi_source_support_score=ms,
            weights=lw,
        )

        tconfs = item["target_confidences"]
        max_tc = round(max(tconfs), 4) if tconfs else 0.0
        mean_tc = round(sum(tconfs) / len(tconfs), 4) if tconfs else 0.0

        # selection reason
        reasons = []
        if scored["ligand_score"] >= 0.55:
            reasons.append(f"ligand_score={scored['ligand_score']:.3f}")
        if ic >= 0.70:
            reasons.append("high_ingredient_confidence")
        if disease_target_count >= 1:
            reasons.append(f"disease_intersection={disease_target_count}")
        if source_count_val >= 2:
            reasons.append(f"multi_source={source_count_val}")

        output.append(
            {
                "ligand_id": ligand_id_from_group(item),
                "pubchem_cid": item["pubchem_cid"],
                "inchikey": item["inchikey"],
                "canonical_smiles": item["canonical_smiles"],
                "isomeric_smiles": item["isomeric_smiles"],
                "source_ingredient_ids": "; ".join(sorted(v for v in item["source_ingredient_ids"] if v)),
                "source_ingredient_names": "; ".join(sorted(v for v in item["source_ingredient_names"] if v)),
                "source_herb_names": "; ".join(sorted(v for v in item["source_herb_names"] if v)),
                "source_list": source_list_str,
                "source_count": source_count_val,
                "compound_target_count": total_targets,
                "docking_target_count": docking_target_count_val,
                "linked_docking_targets": "; ".join(sorted(item["docking_targets"])),
                "ligand_score": scored["ligand_score"],
                "ligand_confidence": scored["ligand_confidence"],
                "ingredient_confidence": scored["ingredient_confidence"],
                "structure_quality_score": scored["structure_quality_score"],
                "target_link_score": scored["target_link_score"],
                "disease_relevance_score": scored["disease_relevance_score"],
                "multi_source_support_score": scored["multi_source_support_score"],
                "max_target_confidence": max_tc,
                "mean_target_confidence": mean_tc,
                "pubchem_match_status": "; ".join(sorted(v for v in item["pubchem_match_status"] if v)),
                "selection_reason": "; ".join(reasons) if reasons else "selected",
            }
        )
    output.sort(key=lambda row: (-float(row["ligand_score"]), row["ligand_id"]))
    return output


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Rank core network pharmacology targets with weighted scoring.")
    parser.add_argument("--config", default="results/network_pharmacology/00_input/params.resolved.yaml")
    args = parser.parse_args()

    project_root_from_cwd()
    params = load_params(args.config)
    out_root = output_root(params)
    core_dir = ensure_dir(out_root / "07_core_targets")

    # ── load input data ──────────────────────────────────────────────────
    intersection = read_rows(out_root / "04_intersection" / "intersection_targets.csv")
    graph = load_graph(out_root / "05_ppi" / "ppi_network.graphml")
    genes = [row["target_gene"].strip().upper() for row in intersection if row.get("target_gene")]
    graph.add_nodes_from(genes)

    # load union data for confidence values
    union_edges_path = out_root / "02_compound_targets" / "union_ingredient_target_edges.csv"
    union_ingredients_path = out_root / "01_ingredients" / "union_ingredients.csv"
    union_targets_path = out_root / "02_compound_targets" / "union_targets.csv"
    compound_target_path = out_root / "02_compound_targets" / "compound_targets.normalized.csv"
    compound_targets = read_rows(compound_target_path) if compound_target_path.exists() else []
    active_pairs = {
        (
            str(row.get("ingredient_id", "") or "").strip(),
            str(row.get("target_gene", "") or "").strip().upper(),
        )
        for row in compound_targets
        if row.get("ingredient_id") and row.get("target_gene")
    }
    active_edge_overrides = {
        (
            str(row.get("ingredient_id", "") or "").strip(),
            str(row.get("target_gene", "") or "").strip().upper(),
        ): row
        for row in compound_targets
        if row.get("ingredient_id") and row.get("target_gene")
    }

    # Build target confidence lookup. If a union edge file exists, target-level
    # aggregates are ignored so exploratory edges cannot pollute confidence.
    target_confidence_lookup: dict[str, float] = {}
    ut_rows: list[dict] = []
    if union_targets_path.exists():
        ut_rows = read_rows(union_targets_path)

    union_edge_rows = read_rows(union_edges_path) if union_edges_path.exists() else []
    final_edge_contract_required = union_edges_path.exists() or compound_target_path.exists()
    target_provenance = build_target_provenance(
        ut_rows,
        union_edge_rows,
        active_pairs=active_pairs if final_edge_contract_required else None,
        active_edge_overrides=(
            active_edge_overrides if final_edge_contract_required else None
        ),
        edge_contract_present=union_edges_path.exists(),
    )
    for gene, provenance in target_provenance.items():
        target_confidence_lookup[gene] = _float_or(provenance.get("target_confidence"))

    eligible_edge_confidence_by_id: dict[str, float] = {}
    eligible_edge_sources_by_id: dict[str, set[str]] = {}
    for row in union_edge_rows:
        pair = (
            str(row.get("ingredient_id", "") or "").strip(),
            str(row.get("target_gene", "") or "").strip().upper(),
        )
        if final_edge_contract_required and pair not in active_pairs:
            continue
        if not _edge_is_network_eligible(row):
            continue
        effective_row = _apply_edge_override(row, active_edge_overrides.get(pair))
        confidence = _float_or(effective_row.get("ingredient_confidence"))
        sources = _sources(
            effective_row.get("eligible_source_list")
            or effective_row.get("source_list")
        )
        keys = {
            str(row.get("ingredient_id", "") or "").strip(),
            str(row.get("canonical_ingredient_id", "") or "").strip(),
        }
        for key in keys - {""}:
            eligible_edge_confidence_by_id[key] = max(
                eligible_edge_confidence_by_id.get(key, 0.0), confidence
            )
            eligible_edge_sources_by_id.setdefault(key, set()).update(sources)

    # Build ingredient confidence lookup from union_ingredients
    ingredient_confidence_lookup: dict[str, float] = {}
    ingredient_source_lookup: dict[str, str] = {}
    ingredient_confidence_by_id: dict[str, float] = {}
    ingredient_source_by_id: dict[str, str] = {}
    if union_ingredients_path.exists():
        ui_rows = read_rows(union_ingredients_path)
        for row in ui_rows:
            pubchem_cid = str(row.get("pubchem_cid", "")).strip()
            ic = _float_or(row.get("ingredient_confidence"))
            src = row.get("source_list", "")
            # A union ingredient is only a confidence fallback when no edge
            # audit exists. Otherwise the edge-specific eligible-source values
            # are authoritative.
            if pubchem_cid and not union_edge_rows:
                if pubchem_cid not in ingredient_confidence_lookup or ic > ingredient_confidence_lookup[pubchem_cid]:
                    ingredient_confidence_lookup[pubchem_cid] = ic
                    ingredient_source_lookup[pubchem_cid] = src
            source_ids = {
                part.strip()
                for part in str(row.get("source_ids", "") or "").split(";")
                if part.strip()
            }
            canonical_id = str(row.get("canonical_ingredient_id", "") or "").strip()
            if canonical_id:
                source_ids.add(canonical_id)
            for ingredient_id in source_ids:
                edge_confidence = eligible_edge_confidence_by_id.get(ingredient_id)
                if pubchem_cid and edge_confidence is not None:
                    ingredient_confidence_lookup[pubchem_cid] = max(
                        ingredient_confidence_lookup.get(pubchem_cid, 0.0),
                        edge_confidence,
                    )
                    ingredient_source_lookup[pubchem_cid] = "; ".join(
                        sorted(eligible_edge_sources_by_id.get(ingredient_id, set()))
                    )
                if union_edge_rows:
                    continue
                if ic >= ingredient_confidence_by_id.get(ingredient_id, 0.0):
                    ingredient_confidence_by_id[ingredient_id] = ic
                    ingredient_source_by_id[ingredient_id] = src

    # Current params (prefer new weights if present, fall back to defaults)
    lw = params.get("ranking", {}).get("ligand_score_weights", {
        "ingredient_confidence": 0.15, "structure_quality": 0.20,
        "target_link": 0.30, "disease_relevance": 0.25, "multi_source_support": 0.10,
    })
    tw = params.get("ranking", {}).get("target_score_weights", {
        "source_confidence": 0.25, "disease_association": 0.25,
        "ppi_topology": 0.20, "compound_coverage": 0.20, "multi_source_support": 0.10,
    })
    sel_lig = params.get("selection", {}).get("docking_ligands", {})
    sel_tar = params.get("selection", {}).get("docking_targets", {})
    _required_selection_bool(
        sel_lig, "require_structure", "selection.docking_ligands"
    )
    _required_selection_bool(
        sel_lig,
        "require_linked_docking_target",
        "selection.docking_ligands",
    )
    require_disease_intersection = _required_selection_bool(
        sel_tar,
        "require_disease_intersection",
        "selection.docking_targets",
    )
    require_human_target = _required_selection_bool(
        sel_tar, "require_human_target", "selection.docking_targets"
    )
    min_ligand_score = sel_lig.get("min_ligand_score", 0.55)
    min_ligand_confidence = sel_lig.get("min_ligand_confidence", 0.55)
    min_target_score = sel_tar.get("min_target_score", 0.55)
    min_target_confidence = sel_tar.get("min_target_confidence", 0.55)
    top_n = int(sel_tar.get("top_n", params.get("core_target_selection", {}).get("top_n", 20)))
    ligand_top_n = int(sel_lig.get("top_n", 20))

    # ── PPI topology ─────────────────────────────────────────────────────
    degree = dict(graph.degree())
    betweenness = nx.betweenness_centrality(graph) if graph.number_of_nodes() else {}
    closeness = nx.closeness_centrality(graph) if graph.number_of_nodes() else {}

    rows: list[dict] = []
    for row in intersection:
        gene = row["target_gene"].strip().upper()
        provenance = target_provenance.get(gene, {})
        sources = provenance.get("sources", set())
        source_list = "; ".join(sorted(sources)) if isinstance(sources, set) else ""
        rows.append({
            "target_gene": gene,
            "uniprot_id": row.get("uniprot_id", ""),
            "target_name": row.get("target_name", ""),
            "disease_association_score": _float_or(row.get("disease_association_score")),
            "compound_count": int(_float_or(row.get("compound_count"))),
            "degree": int(degree.get(gene, 0)),
            "betweenness": float(betweenness.get(gene, 0)),
            "closeness": float(closeness.get(gene, 0)),
            "source_list": source_list,
            "source_count": len(sources) if isinstance(sources, set) else 0,
            "eligible_edge_count": int(provenance.get("edge_count", 0) or 0),
            # Missing provenance is absence of evidence, not a synthetic
            # medium-confidence observation.
            "target_confidence": target_confidence_lookup.get(gene, 0.0),
            "high_confidence_ligand_count": len(provenance.get("high_confidence_ligands", set())),
        })

    if not rows:
        write_json(core_dir / "core_target_summary.json", {
            "module": "rank_core_targets", "status": "warning",
            "generated_at": now_utc(), "warnings": ["No intersecting targets found."],
        })
        return

    # ── compute normalised sub-scores for targets ────────────────────────
    degree_vals = [float(r["degree"]) for r in rows]
    betw_vals = [float(r["betweenness"]) for r in rows]
    clos_vals = [float(r["closeness"]) for r in rows]
    disease_vals = [float(r["disease_association_score"]) for r in rows]
    comp_vals = [float(r["compound_count"]) for r in rows]
    target_conf_vals = [float(r["target_confidence"]) for r in rows]

    degree_norm = _norm_score(degree_vals)
    betw_norm = _norm_score(betw_vals)
    clos_norm = _norm_score(clos_vals)
    disease_norm = _norm_score(disease_vals)
    comp_norm = _norm_score(comp_vals)
    tc_norm = _norm_score(target_conf_vals, zero_is_absence=True)

    # Collect disease genes (for later ligand scoring)
    intersection_genes_set = set(genes)
    disease_genes_set = {r["target_gene"] for r in rows if _float_or(r.get("disease_association_score")) > 0}

    degree_p75 = percentile(degree_vals, 0.75)
    degree_p90 = percentile(degree_vals, 0.90)

    for i, row in enumerate(rows):
        gene = row["target_gene"]
        ppi_top = (degree_norm[i] + betw_norm[i] + clos_norm[i]) / 3.0
        da_norm = disease_norm[i]
        cc_norm = comp_norm[i]
        sc = tc_norm[i]
        # Provider counts are derived from eligible union edges, not the
        # intersection's historical source_count column.
        sl = row.get("source_list", "")
        ms = multi_source_score(sl)
        src_count = len(_sources(sl))

        scored = compute_target_score(
            source_confidence=sc,
            disease_association_norm=da_norm,
            ppi_topology_score=ppi_top,
            compound_coverage_score=cc_norm,
            multi_source_support_score=ms,
            weights=tw,
        )
        hf, hr, hint = hub_annotation(row, degree_p75=degree_p75, degree_p90=degree_p90)

        # count high-confidence ligands (ligands with ingredient_confidence >= 0.70)
        hcl = int(row.get("high_confidence_ligand_count", 0) or 0)
        ts = scored["target_score"]
        tc = row["target_confidence"]

        # selection reason
        sel_reasons = []
        if ts >= min_target_score:
            sel_reasons.append(f"target_score={ts:.3f}>=threshold={min_target_score}")
        if sc >= 0.70:
            sel_reasons.append("high_source_confidence")
        if da_norm >= 0.50:
            sel_reasons.append("disease_associated")
        if ms >= 0.80:
            sel_reasons.append("multi_source_supported")

        row.update({
            "target_score": scored["target_score"],
            "target_confidence": tc,
            "source_confidence_score": scored["source_confidence_score"],
            "disease_association_norm": scored["disease_association_norm"],
            "ppi_topology_score": scored["ppi_topology_score"],
            "compound_coverage_score": scored["compound_coverage_score"],
            "multi_source_support_score": scored["multi_source_support_score"],
            "source_list": sl,
            "source_count": src_count,
            "high_confidence_ligand_count": hcl,
            "hub_flag": hf,
            "hub_reason": hr,
            "interpretation_note": hint,
            "selection_reason": "; ".join(sel_reasons) if sel_reasons else "selected",
        })

    # ── sort by target_score descending ──────────────────────────────────
    rows.sort(key=lambda r: (-float(r["target_score"]),
                              -float(r["disease_association_score"]),
                              -float(r["compound_count"])))

    selected_target_count = 0
    for idx, row in enumerate(rows):
        failures = []
        if union_edges_path.exists() and int(row.get("eligible_edge_count", 0) or 0) == 0:
            failures.append("missing_eligible_edge_provenance")
        if require_disease_intersection and row["target_gene"] not in intersection_genes_set:
            failures.append("missing_required_disease_intersection")
        if require_human_target and params.get("project", {}).get("taxon_id") != 9606:
            failures.append("nonhuman_target_scope")
        if float(row["target_score"]) < float(min_target_score):
            failures.append("below_min_target_score")
        if float(row["target_confidence"]) < float(min_target_confidence):
            failures.append("below_min_target_confidence")
        if not failures and selected_target_count >= top_n:
            failures.append("outside_top_n")
        if failures:
            row["recommended_for_docking"] = "false"
            row["selection_reason"] = "; ".join(failures)
        else:
            row["recommended_for_docking"] = "true"
            selected_target_count += 1
        # also compute old-style ranks for backward compatibility
        row["core_rank"] = idx + 1

    docking_rows = [r for r in rows if r["recommended_for_docking"] == "true"]
    docking_genes = {r["target_gene"] for r in docking_rows}

    # ── ligands ──────────────────────────────────────────────────────────
    ligand_ready_path = out_root / "01_ingredients" / "ligand_ready_ingredients.csv"
    ligand_ready = read_rows(ligand_ready_path) if ligand_ready_path.exists() else []
    ligand_to_targets: dict[str, set[str]] = {}
    for row in compound_targets:
        iid = row.get("ingredient_id", "")
        gene = row.get("target_gene", "").strip().upper()
        if iid and gene:
            ligand_to_targets.setdefault(iid, set()).add(gene)

    # enrich ligand_ready with union confidence data
    for row in ligand_ready:
        cid = str(row.get("pubchem_cid", "")).strip()
        ingredient_id = str(row.get("ingredient_id", "") or "").strip()
        confidence = eligible_edge_confidence_by_id.get(ingredient_id)
        source_list = "; ".join(
            sorted(eligible_edge_sources_by_id.get(ingredient_id, set()))
        )
        if confidence is None and cid:
            confidence = ingredient_confidence_lookup.get(cid)
            source_list = ingredient_source_lookup.get(cid, "")
        if confidence is None and ingredient_id and not union_edge_rows:
            confidence = ingredient_confidence_by_id.get(ingredient_id)
            source_list = ingredient_source_by_id.get(ingredient_id, "")
        if confidence is not None:
            row["ingredient_confidence"] = confidence
            row["source_list"] = source_list

    docking_ligand_candidates = collapse_docking_ligands(
        ligand_ready, ligand_to_targets, docking_genes,
        target_confidence_lookup, {}, disease_genes_set, lw,
    )
    docking_ligands, docking_ligand_audit = select_docking_ligands(
        docking_ligand_candidates, sel_lig
    )

    unique_ligand_ready_path = out_root / "01_ingredients" / "unique_ligand_ready_molecules.csv"
    unique_ligand_ready = read_rows(unique_ligand_ready_path) if unique_ligand_ready_path.exists() else []
    ligand_ready_unique_count = len(unique_ligand_ready) if unique_ligand_ready else len(ligand_ready)
    docking_ligand_count = len(docking_ligands)
    docking_ligand_coverage = (docking_ligand_count / ligand_ready_unique_count) if ligand_ready_unique_count else 0
    low_docking_ligand_warning = docking_ligand_count < 5 or docking_ligand_coverage < 0.5
    docking_ligand_warning = ""
    if low_docking_ligand_warning:
        docking_ligand_warning = (
            f"Only {docking_ligand_count} unique docking ligands were linked to prioritized targets "
            f"({docking_ligand_coverage:.1%} of unique ligand-ready molecules); docking interpretation should be cautious."
        )

    # ── QC warnings ──────────────────────────────────────────────────────
    family_qc = family_dominance(rows, limit=20)
    enrichment_path = out_root / "06_enrichment" / "enrichment_all.csv"
    enrichment_rows_list = read_rows(enrichment_path) if enrichment_path.exists() else []
    enrichment_qc = enrichment_theme_dominance(enrichment_rows_list, limit=20)
    pathway_dominance_warning = (
        family_qc["top_target_family"] == "GABA_receptor_family"
        and (family_qc["dominant_family_target_count"] >= 5 or family_qc["dominant_family_fraction"] >= 0.30)
    ) or (
        enrichment_qc["top_enrichment_theme"] == "GABA_receptor_or_chloride_channel"
        and enrichment_qc["dominant_enrichment_theme_fraction"] >= 0.25
    )
    pathway_dominance_warning_text = ""
    if pathway_dominance_warning:
        pathway_dominance_warning_text = (
            "Core targets or enrichment terms are dominated by the GABA receptor/chloride-channel family; "
            "this may reflect target-family/database bias and should not be interpreted as the primary mechanism without validation."
        )

    warnings_list = [] if rows else ["No core targets could be ranked because there were no intersecting targets."]
    if docking_ligand_warning:
        warnings_list.append(docking_ligand_warning)
    if pathway_dominance_warning_text:
        warnings_list.append(pathway_dominance_warning_text)

    # ── write outputs ────────────────────────────────────────────────────
    write_rows(core_dir / "network_metrics.csv", rows, FIELDS)
    write_rows(core_dir / "core_targets.csv", rows, FIELDS)
    write_rows(core_dir / "docking_targets.csv", docking_rows, DOCKING_FIELDS)
    write_rows(core_dir / "docking_ligand_candidates.csv", docking_ligand_audit, DOCKING_LIGAND_FIELDS)
    write_rows(core_dir / "docking_ligands.csv", docking_ligands, DOCKING_LIGAND_FIELDS)

    # source weights for QC
    sw = params.get("source_weights", {})
    write_json(core_dir / "core_target_summary.json", {
        "module": "rank_core_targets",
        "status": "passed" if docking_rows else "warning",
        "generated_at": now_utc(),
        "target_count": len(rows),
        "top_n": top_n,
        "docking_target_count": len(docking_rows),
        "ligand_ready_count": ligand_ready_unique_count,
        "docking_ligand_count": docking_ligand_count,
        "docking_ligand_coverage": round(docking_ligand_coverage, 6),
        "low_docking_ligand_warning": low_docking_ligand_warning,
        "docking_ligand_warning": docking_ligand_warning,
        "hub_target_count": len([r for r in rows if r.get("hub_flag") == "true"]),
        "top_targets": [r["target_gene"] for r in docking_rows[:20]],
        "top_ligands": [r["ligand_id"] for r in docking_ligands[:20]],
        "final_scoring": {
            "ligand_score_formula": (
                f"{lw['ingredient_confidence']}*ingredient_confidence "
                f"+ {lw['structure_quality']}*structure_quality_score "
                f"+ {lw['target_link']}*target_link_score "
                f"+ {lw['disease_relevance']}*disease_relevance_score "
                f"+ {lw['multi_source_support']}*multi_source_support_score"
            ),
            "target_score_formula": (
                f"{tw['source_confidence']}*source_confidence "
                f"+ {tw['disease_association']}*disease_association_norm "
                f"+ {tw['ppi_topology']}*ppi_topology_score "
                f"+ {tw['compound_coverage']}*compound_coverage_score "
                f"+ {tw['multi_source_support']}*multi_source_support_score"
            ),
            "ligand_min_score": min_ligand_score,
            "ligand_min_confidence": min_ligand_confidence,
            "ligand_top_n": ligand_top_n,
            "target_min_score": min_target_score,
            "target_min_confidence": min_target_confidence,
            "source_weights": {
                "TCMSP_OBDL_ACTIVE": sw.get("ingredient_confidence", {}).get("TCMSP_OBDL_ACTIVE", 1.00),
                "SYMMAP_LINKED_INGREDIENT": sw.get("ingredient_confidence", {}).get("SYMMAP_LINKED_INGREDIENT", 0.65),
                "HERBIV_NETWORK_INGREDIENT": sw.get("ingredient_confidence", {}).get("HERBIV_NETWORK_INGREDIENT", 0.75),
                "TCMSP_COMPOUND_TARGET": sw.get("compound_target_confidence", {}).get("TCMSP_COMPOUND_TARGET", 0.90),
                "SYMMAP_COMPOUND_TARGET": sw.get("compound_target_confidence", {}).get("SYMMAP_COMPOUND_TARGET", 0.60),
                "HERBIV_COMPOUND_TARGET": sw.get("compound_target_confidence", {}).get("HERBIV_COMPOUND_TARGET", 0.65),
            },
        },
        "selection_summary": {
            "contract_version": "docking_ligands.v2",
            "ligand_candidates_total": len(docking_ligand_audit),
            "ligand_candidates_selected": docking_ligand_count,
            "ligand_candidates_excluded_no_structure": len([
                row for row in docking_ligand_audit
                if "missing_required_structure" in row.get("selection_exclusion_reason", "")
            ]),
            "ligand_candidates_excluded_low_score": len([
                row for row in docking_ligand_audit
                if "below_min_ligand_score" in row.get("selection_exclusion_reason", "")
            ]),
            "ligand_candidates_excluded_low_confidence": len([
                row for row in docking_ligand_audit
                if "below_min_ligand_confidence" in row.get("selection_exclusion_reason", "")
            ]),
            "ligand_candidates_excluded_top_n": len([
                row for row in docking_ligand_audit
                if "outside_top_n" in row.get("selection_exclusion_reason", "")
            ]),
            "target_candidates_total": len(rows),
            "target_candidates_selected": len(docking_rows),
            "target_candidates_excluded_nonhuman": 0,
            "target_candidates_excluded_unmapped": 0,
            "target_candidates_excluded_low_score": len(rows) - len(docking_rows),
        },
        **family_qc,
        **enrichment_qc,
        "pathway_dominance_warning": pathway_dominance_warning,
        "pathway_dominance_warning_text": pathway_dominance_warning_text,
        "generated_files": [
            str(core_dir / "network_metrics.csv"),
            str(core_dir / "core_targets.csv"),
            str(core_dir / "docking_targets.csv"),
            str(core_dir / "docking_ligand_candidates.csv"),
            str(core_dir / "docking_ligands.csv"),
        ],
        "warnings": warnings_list,
    })
    print(
        f"Ranked {len(rows)} core targets; selected {len(docking_rows)} targets "
        f"and {len(docking_ligands)} ligands for docking."
    )


if __name__ == "__main__":
    exit_with_errors(main)

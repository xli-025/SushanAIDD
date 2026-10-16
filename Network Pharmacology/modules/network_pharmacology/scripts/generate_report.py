#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

import markdown

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.network_pharmacology.scripts.common import (
    ensure_dir,
    exit_with_errors,
    load_params,
    now_utc,
    output_root,
    project_root_from_cwd,
    read_json,
    read_rows,
    write_json,
)


SUMMARY_FILES = [
    "00_input/input_summary.json",
    "01_ingredients/ingredient_qc.json",
    "02_compound_targets/compound_target_summary.json",
    "02_compound_targets/source_merge_summary.json",
    "02_compound_targets/source_reliability_audit.json",
    "02_compound_targets/supplemental_source_audit.json",
    "02_compound_targets/target_mapping_summary.json",
    "03_disease_targets/disease_target_summary.json",
    "04_intersection/intersection_summary.json",
    "05_ppi/ppi_summary.json",
    "06_enrichment/enrichment_summary.json",
    "07_core_targets/core_target_summary.json",
]

CHEMICAL_FIELDS = {
    "smiles",
    "canonical_smiles",
    "isomeric_smiles",
    "inchikey",
    "inchi_key",
    "ligand_id",
}


def optional_json(path: Path) -> dict:
    return read_json(path) if path.exists() else {"status": "missing", "warnings": [f"Missing {path}"]}


def optional_rows(path: Path) -> list[dict]:
    return read_rows(path) if path.exists() else []


def markdown_table(rows: list[dict], fields: list[str], limit: int = 10) -> str:
    if not rows:
        return "_No rows available._"

    def cell(value: object, field: str) -> str:
        text = str(value if value is not None else "")
        escaped = html.escape(text).replace("|", "&#124;")
        if field in CHEMICAL_FIELDS and escaped:
            escaped = (
                escaped.replace("[", "&#91;")
                .replace("]", "&#93;")
                .replace("(", "&#40;")
                .replace(")", "&#41;")
            )
            return f"<code>{escaped}</code>"
        return escaped

    selected = rows[:limit]
    header = "| " + " | ".join(fields) + " |"
    sep = "| " + " | ".join(["---"] * len(fields)) + " |"
    body = []
    for row in selected:
        body.append("| " + " | ".join(cell(row.get(field, ""), field) for field in fields) + " |")
    return "\n".join([header, sep] + body)


def preview_note(rows: list[dict], *, limit: int, file_path: str) -> str:
    total = len(rows)
    shown = min(total, limit)
    if total == 0:
        return f"_No rows available. Full file: `{file_path}`._"
    if shown == total:
        return f"_Showing all {total} rows. Full file: `{file_path}`._"
    return f"_Showing first {shown} of {total} rows. Full file: `{file_path}`._"


def preview_table(rows: list[dict], fields: list[str], *, limit: int, file_path: str) -> str:
    return f"{preview_note(rows, limit=limit, file_path=file_path)}\n\n{markdown_table(rows, fields, limit)}"


def render_html_from_markdown(md: str) -> str:
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "toc"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Network Pharmacology Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 40px; line-height: 1.55; color: #17202a; }}
    code {{ background: #f3f5f7; padding: 2px 4px; border-radius: 4px; }}
    .report {{ max-width: 1040px; margin: 0 auto; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0 28px; font-size: 14px; }}
    th, td {{ border: 1px solid #d9e0e7; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f4f7fa; }}
    tr:nth-child(even) td {{ background: #fbfcfd; }}
  </style>
</head>
<body>
  <main class="report">{body}</main>
</body>
</html>
"""


def warning_summary_rows(
    *,
    pubchem_unmatched_count: int,
    unmapped_count: int,
    ambiguous_count: int,
    ignored_count: int,
    mapping_success_rate: float,
) -> list[dict]:
    return [
        {"category": "PubChem unmatched active ingredients", "count": pubchem_unmatched_count, "detail_file": "01_ingredients/pubchem_unmatched_ingredients.csv"},
        {"category": "Unmapped TCMSP target rows", "count": unmapped_count, "detail_file": "02_compound_targets/unmapped_targets.csv"},
        {"category": "Ambiguous TCMSP target rows", "count": ambiguous_count, "detail_file": "02_compound_targets/ambiguous_targets.csv"},
        {"category": "Ignored/non-human target rows", "count": ignored_count, "detail_file": "02_compound_targets/unmapped_targets.csv"},
        {"category": "Target mapping success rate", "count": f"{mapping_success_rate:.1%}", "detail_file": "02_compound_targets/target_mapping_summary.json"},
    ]


def unique_warnings(warnings: list[str], limit: int = 10) -> list[str]:
    output = []
    seen = set()
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        output.append(warning)
        if len(output) >= limit:
            break
    return output


def input_lines_for_qc(qc: dict) -> list[str]:
    lines = [
        f"- Project: {qc.get('project_name', '')}",
        f"- Input mode: {qc.get('input_mode', 'herb')}",
    ]
    if qc.get("input_mode") == "compound":
        lines.append(f"- Compound: {qc.get('compound_name', '')}")
    else:
        lines.append(f"- Herb/formula: {qc.get('herb_name', '')}")
    lines.extend([f"- Disease: {qc.get('disease_name', '')}", "- Organism: Homo sapiens / 9606"])
    return lines


def compute_docking_readiness(
    *,
    intersection_count: int,
    core_target_count: int,
    docking_target_count: int,
    docking_ligand_count: int,
    ligand_ready_count: int,
    ligand_missing_structure_count: int,
) -> dict:
    can_prioritize_targets = intersection_count > 0 and core_target_count > 0
    can_prepare_targets = docking_target_count > 0
    can_prepare_ligands = docking_ligand_count > 0
    return {
        "can_prioritize_targets": can_prioritize_targets,
        "can_prepare_targets": can_prepare_targets,
        "can_prepare_ligands": can_prepare_ligands,
        "can_enter_docking": can_prepare_targets and can_prepare_ligands,
        "docking_readiness": {
            "target_ready": can_prepare_targets,
            "ligand_ready": can_prepare_ligands,
            "target_ready_count": docking_target_count,
            "ligand_ready_count": docking_ligand_count,
            "ligand_ready_ingredients": ligand_ready_count,
            "ligand_missing_structure_count": ligand_missing_structure_count,
        },
    }


def compute_interpretation_statuses(
    *,
    tcmsp_data_quality: dict,
    ingredient_qc: dict,
    disease_summary: dict,
    core_summary: dict,
    can_enter_docking: bool,
) -> dict:
    final_source_is_live = bool(tcmsp_data_quality.get("final_tcmsp_source_is_live", False))
    fallback_snapshot_validated = bool(tcmsp_data_quality.get("fallback_snapshot_validated", False))
    tcmsp_data_source = ingredient_qc.get("tcmsp_data_source", tcmsp_data_quality.get("data_source", ""))
    if final_source_is_live:
        data_source_status = "live_validated"
    elif fallback_snapshot_validated or tcmsp_data_source == "tcmsp_full_snapshot":
        data_source_status = "snapshot_fallback"
    elif tcmsp_data_source:
        data_source_status = f"fallback_{tcmsp_data_source}"
    else:
        data_source_status = "unknown"

    herb_match_status = (
        ingredient_qc.get("herb_match_status")
        or tcmsp_data_quality.get("herb_match_status")
        or ("unknown" if ingredient_qc.get("input_mode") != "compound" else "not_applicable")
    )
    disease_mapping_status = disease_summary.get("disease_mapping_status", "specific")
    low_ligand_warning = bool(core_summary.get("low_docking_ligand_warning", False))
    docking_readiness_status = "not_ready"
    if can_enter_docking:
        docking_readiness_status = "low_ligand_warning" if low_ligand_warning else "ready"

    needs_confirmation = bool(
        ingredient_qc.get("herb_match_requires_user_confirmation", False)
        or tcmsp_data_quality.get("herb_match_requires_user_confirmation", False)
    )
    generic_warning = disease_mapping_status == "generic_warning" or bool(disease_summary.get("is_generic_disease_term", False))
    pathway_warning = bool(core_summary.get("pathway_dominance_warning", False))
    if needs_confirmation:
        interpretation_status = "needs_user_confirmation"
    elif generic_warning or low_ligand_warning or pathway_warning:
        interpretation_status = "caution"
    elif data_source_status == "snapshot_fallback":
        interpretation_status = "caution_data_source"
    else:
        interpretation_status = "validated"

    if needs_confirmation or generic_warning:
        confidence_grade = "C"
    elif not can_enter_docking:
        confidence_grade = "D"
    elif data_source_status == "snapshot_fallback" or low_ligand_warning or pathway_warning:
        confidence_grade = "B"
    else:
        confidence_grade = "A"

    return {
        "workflow_status": "passed",
        "data_source_status": data_source_status,
        "herb_match_status": herb_match_status,
        "disease_mapping_status": disease_mapping_status,
        "docking_readiness_status": docking_readiness_status,
        "interpretation_status": interpretation_status,
        "result_confidence_grade": confidence_grade,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate network pharmacology report and QC summary.")
    parser.add_argument("--config", default="results/network_pharmacology/00_input/params.resolved.yaml")
    args = parser.parse_args()

    project_root_from_cwd()
    params = load_params(args.config)
    out_root = output_root(params)
    report_dir = ensure_dir(out_root / "08_report")

    summaries = {file_name: optional_json(out_root / file_name) for file_name in SUMMARY_FILES}
    active = optional_rows(out_root / "01_ingredients" / "active_ingredients.csv")
    tcmsp_obdl_active = optional_rows(out_root / "01_ingredients" / "tcmsp_obdl_active_ingredients.csv")
    herbiv_candidates = optional_rows(out_root / "01_ingredients" / "herbiv_candidate_ingredients.csv")
    symmap_candidates = optional_rows(out_root / "01_ingredients" / "symmap_candidate_ingredients.csv")
    merged_network_candidates = optional_rows(out_root / "01_ingredients" / "merged_network_candidate_ingredients.csv")
    deduplicated_active = optional_rows(out_root / "01_ingredients" / "deduplicated_active_ingredients.csv")
    ligand_ready = optional_rows(out_root / "01_ingredients" / "ligand_ready_ingredients.csv")
    unique_ligand_ready = optional_rows(out_root / "01_ingredients" / "unique_ligand_ready_molecules.csv")
    structure_ready = optional_rows(out_root / "01_ingredients" / "structure_ready_ligands.csv")
    docking_ready = optional_rows(out_root / "01_ingredients" / "docking_ready_ligands.csv")
    union_ingredients = optional_rows(out_root / "01_ingredients" / "union_ingredients.csv")
    union_targets = optional_rows(out_root / "02_compound_targets" / "union_targets.csv")
    union_edges = optional_rows(out_root / "02_compound_targets" / "union_ingredient_target_edges.csv")
    pubchem_unmatched = optional_rows(out_root / "01_ingredients" / "pubchem_unmatched_ingredients.csv")
    pubchem_query_attempts = optional_rows(out_root / "01_ingredients" / "pubchem_query_attempts.csv")
    intersection = optional_rows(out_root / "04_intersection" / "intersection_targets.csv")
    core = optional_rows(out_root / "07_core_targets" / "core_targets.csv")
    docking = optional_rows(out_root / "07_core_targets" / "docking_targets.csv")
    docking_ligands = optional_rows(out_root / "07_core_targets" / "docking_ligands.csv")
    enrichment = optional_rows(out_root / "06_enrichment" / "enrichment_all.csv")

    warnings = []
    statuses = {}
    for name, summary in summaries.items():
        statuses[name] = summary.get("status", "unknown")
        warnings.extend(summary.get("warnings") or [])
    warnings = unique_warnings(warnings, 300)

    ingredient_qc = summaries.get("01_ingredients/ingredient_qc.json", {})
    source_merge_summary = summaries.get("02_compound_targets/source_merge_summary.json", {})
    source_reliability_audit = summaries.get("02_compound_targets/source_reliability_audit.json", {})
    supplemental_source_audit = summaries.get("02_compound_targets/supplemental_source_audit.json", {})
    structure_readiness_summary = optional_json(out_root / "01_ingredients" / "structure_readiness_summary.json")
    herb_name_standardization = optional_json(out_root / "00_input" / "herb_name_standardization.json")
    mapping_summary = summaries.get("02_compound_targets/target_mapping_summary.json", {})
    core_summary = summaries.get("07_core_targets/core_target_summary.json", {})
    disease_summary = summaries.get("03_disease_targets/disease_target_summary.json", {})
    active_with_smiles = int(ingredient_qc.get("active_ingredients_with_smiles", 0) or 0)
    active_without_smiles = int(ingredient_qc.get("active_ingredients_without_smiles", 0) or 0)
    deduplicated_active_count = len(deduplicated_active) if deduplicated_active else int(
        ingredient_qc.get("deduplicated_active_ingredients", len(active)) or len(active)
    )
    ligand_ready_count = len(ligand_ready)
    unique_ligand_ready_count = len(unique_ligand_ready) if unique_ligand_ready else ligand_ready_count
    tcmsp_obdl_active_count = len(tcmsp_obdl_active) if tcmsp_obdl_active else int(ingredient_qc.get("tcmsp_obdl_active_ingredients", 0) or 0)
    herbiv_candidate_count = len(herbiv_candidates) if herbiv_candidates else int(ingredient_qc.get("herbiv_candidate_ingredients", 0) or 0)
    symmap_candidate_count = len(symmap_candidates) if symmap_candidates else int(ingredient_qc.get("symmap_candidate_ingredients", 0) or 0)
    merged_candidate_count = len(merged_network_candidates) if merged_network_candidates else int(ingredient_qc.get("merged_network_candidate_ingredients", len(active)) or len(active))
    structure_ready_count = len(structure_ready) if structure_ready else int(structure_readiness_summary.get("smiles_ready", 0) or ingredient_qc.get("smiles_ready_ligands", 0) or 0)
    docking_ready_count = len(docking_ready) if docking_ready else int(structure_readiness_summary.get("docking_ready", 0) or ingredient_qc.get("docking_ready_ligands", 0) or 0)
    ligand_missing_structure_count = max(len(active) - active_with_smiles, active_without_smiles)
    target_mapping_total = int(mapping_summary.get("target_mapping_total", 0) or ingredient_qc.get("target_mapping_total", 0) or 0)
    target_mapping_success = int(mapping_summary.get("target_mapping_success", 0) or ingredient_qc.get("target_mapping_success", 0) or 0)
    target_mapping_failed = int(mapping_summary.get("target_mapping_failed", 0) or ingredient_qc.get("target_mapping_failed", 0) or 0)
    target_mapping_ambiguous = int(mapping_summary.get("target_mapping_ambiguous", 0) or ingredient_qc.get("target_mapping_ambiguous", 0) or 0)
    target_mapping_ignored = int(mapping_summary.get("target_mapping_ignored", 0) or ingredient_qc.get("target_mapping_ignored", 0) or 0)
    target_mapping_success_rate = float(
        mapping_summary.get("target_mapping_success_rate", 0) or ingredient_qc.get("target_mapping_success_rate", 0) or 0
    )
    tcmsp_data_source = ingredient_qc.get("tcmsp_data_source", "remote")
    tcmsp_fallback_used = bool(ingredient_qc.get("tcmsp_fallback_used", False))
    official_download_method = ingredient_qc.get("official_download_method", "")
    download_file_paths = ingredient_qc.get("download_file_paths", [])
    tcmsp_data_quality = ingredient_qc.get("tcmsp_data_quality", {})
    tcmsp_source_conclusion = tcmsp_data_quality.get("tcmsp_source_conclusion") or ingredient_qc.get("tcmsp_source_conclusion", "")
    if not tcmsp_source_conclusion:
        tcmsp_source_conclusion = "Live TCMSP scrape succeeded and was used."
    if not bool(tcmsp_data_quality.get("final_tcmsp_source_is_live", False)) and not tcmsp_data_quality.get("tcmsp_source_conclusion"):
        if bool(tcmsp_data_quality.get("fallback_snapshot_validated", False)):
            tcmsp_source_conclusion = "Live TCMSP scrape failed or was unparseable; local full snapshot was used."
        elif tcmsp_fallback_used:
            tcmsp_source_conclusion = f"Live TCMSP scrape failed or was unparseable; fallback source `{tcmsp_data_source}` was used."
        else:
            tcmsp_source_conclusion = "Live TCMSP scrape was not validated."
    active_explanation = ingredient_qc.get("active_ingredient_count_explanation", "")
    warning_summary = warning_summary_rows(
        pubchem_unmatched_count=len(pubchem_unmatched),
        unmapped_count=target_mapping_failed,
        ambiguous_count=target_mapping_ambiguous,
        ignored_count=target_mapping_ignored,
        mapping_success_rate=target_mapping_success_rate,
    )

    readiness = compute_docking_readiness(
        intersection_count=len(intersection),
        core_target_count=len(core),
        docking_target_count=len(docking),
        docking_ligand_count=len(docking_ligands),
        ligand_ready_count=ligand_ready_count,
        ligand_missing_structure_count=ligand_missing_structure_count,
    )
    can_prioritize_targets = readiness["can_prioritize_targets"]
    can_prepare_targets = readiness["can_prepare_targets"]
    can_prepare_ligands = readiness["can_prepare_ligands"]
    can_enter_docking = readiness["can_enter_docking"]
    source_coverage_status = source_merge_summary.get("source_coverage_status", "") if isinstance(source_merge_summary, dict) else ""
    if not source_coverage_status:
        source_coverage_status = "covered" if (
            len(union_edges)
            or len(active)
            or len(optional_rows(out_root / "02_compound_targets" / "compound_targets.normalized.csv"))
        ) else "no_valid_source_coverage"
    no_source_coverage = source_coverage_status == "no_valid_source_coverage"
    interpretation_statuses = compute_interpretation_statuses(
        tcmsp_data_quality=tcmsp_data_quality,
        ingredient_qc=ingredient_qc,
        disease_summary=disease_summary,
        core_summary=core_summary,
        can_enter_docking=can_enter_docking,
    )
    if no_source_coverage:
        interpretation_statuses.update(
            {
                "workflow_status": "failed",
                "data_source_status": "no_herb_compound_target_source",
                "herb_match_status": interpretation_statuses.get("herb_match_status") if interpretation_statuses.get("herb_match_status") not in {"", "unknown"} else "no_match",
                "docking_readiness_status": "not_ready",
                "interpretation_status": "no_source_coverage",
                "result_confidence_grade": "D",
            }
        )
    extra_warning_rows = []
    herb_match_warning = ingredient_qc.get("herb_match_warning") or tcmsp_data_quality.get("herb_match_warning", "")
    disease_mapping_warning = disease_summary.get("disease_mapping_warning", "")
    docking_ligand_warning = core_summary.get("docking_ligand_warning", "")
    pathway_dominance_warning_text = core_summary.get("pathway_dominance_warning_text", "")
    if herb_match_warning:
        warnings.append(herb_match_warning)
        extra_warning_rows.append({"category": "Herb match requires confirmation", "count": "yes", "detail_file": "01_ingredients/tcmsp_fetch_diagnostic.json"})
    if no_source_coverage:
        no_source_message = (
            "No valid direct herb-compound-target relationships were found across TCMSP, HerbiV, and SymMap2; "
            "network analysis cannot be biologically interpreted for this herb input."
        )
        warnings.append(no_source_message)
        extra_warning_rows.append({"category": "No herb compound-target source coverage", "count": "yes", "detail_file": "02_compound_targets/source_merge_summary.json"})
    if disease_mapping_warning:
        warnings.append(disease_mapping_warning)
        extra_warning_rows.append({"category": "Generic disease mapping warning", "count": "yes", "detail_file": "03_disease_targets/disease_target_summary.json"})
    if docking_ligand_warning:
        warnings.append(docking_ligand_warning)
        extra_warning_rows.append({"category": "Low docking ligand coverage", "count": core_summary.get("docking_ligand_count", len(docking_ligands)), "detail_file": "07_core_targets/core_target_summary.json"})
    if pathway_dominance_warning_text:
        warnings.append(pathway_dominance_warning_text)
        extra_warning_rows.append({"category": "Target family/pathway dominance", "count": core_summary.get("top_target_family", ""), "detail_file": "07_core_targets/core_target_summary.json"})
    warnings = unique_warnings(warnings, 300)
    warning_summary.extend(extra_warning_rows)
    source_qc_rows = source_merge_summary.get("source_rows", []) if isinstance(source_merge_summary, dict) else []
    qc = {
        "module": "network_pharmacology",
        "status": "failed" if no_source_coverage else "passed" if can_prioritize_targets else "warning",
        **interpretation_statuses,
        "generated_at": now_utc(),
        "project_name": params.get("project", {}).get("name", ""),
        "input_mode": params.get("input", {}).get("mode", "herb"),
        "herb_name": params.get("input", {}).get("herb_name", ""),
        "compound_name": params.get("input", {}).get("compound_name", ""),
        "disease_name": params.get("input", {}).get("disease_name", ""),
        "step_statuses": statuses,
        "counts": {
            "active_ingredients": len(active),
            "active_ingredient_records": len(active),
            "tcmsp_obdl_active_ingredients": tcmsp_obdl_active_count,
            "herbiv_candidate_ingredients": herbiv_candidate_count,
            "symmap_candidate_ingredients": symmap_candidate_count,
            "merged_network_candidate_ingredients": merged_candidate_count,
            "deduplicated_active_ingredients": deduplicated_active_count,
            "active_ingredients_total": len(active),
            "active_ingredients_with_smiles": active_with_smiles,
            "active_ingredients_without_smiles": active_without_smiles,
            "ligand_ready_ingredients": ligand_ready_count,
            "ligand_ready_ingredient_records": ligand_ready_count,
            "unique_ligand_ready_molecules": unique_ligand_ready_count,
            "structure_ready_ligands": structure_ready_count,
            "docking_ready_ligands": docking_ready_count,
            "union_unique_ingredients": len(union_ingredients) or source_merge_summary.get("union_unique_ingredients", 0),
            "union_unique_targets": len(union_targets) or source_merge_summary.get("union_unique_targets", 0),
            "union_unique_ingredient_target_edges": len(union_edges) or source_merge_summary.get("union_unique_ingredient_target_edges", 0),
            "pubchem_unmatched_ingredients": len(pubchem_unmatched),
            "pubchem_query_attempts": len(pubchem_query_attempts),
            "target_mapping_total": target_mapping_total,
            "target_mapping_success": target_mapping_success,
            "target_mapping_failed": target_mapping_failed,
            "target_mapping_ambiguous": target_mapping_ambiguous,
            "target_mapping_ignored": target_mapping_ignored,
            "target_mapping_success_rate": target_mapping_success_rate,
            "tcmsp_data_source": tcmsp_data_source,
            "tcmsp_fallback_used": tcmsp_fallback_used,
            "tcmsp_source_conclusion": tcmsp_source_conclusion,
            "official_download_method": official_download_method,
            "download_file_paths": download_file_paths,
            "input_herb_name": ingredient_qc.get("input_herb_name", ""),
            "herb_match_field": ingredient_qc.get("herb_match_field", ""),
            "matched_herb_id": ingredient_qc.get("matched_herb_id", ""),
            "matched_herb_name": ingredient_qc.get("matched_herb_name", ""),
            "raw_ingredient_count": ingredient_qc.get("raw_ingredient_count", len(active)),
            "herb_match_type": ingredient_qc.get("herb_match_type", ""),
            "herb_exact_match": ingredient_qc.get("herb_exact_match", False),
            "herb_alias_match": ingredient_qc.get("herb_alias_match", False),
            "herb_fuzzy_match_used": ingredient_qc.get("herb_fuzzy_match_used", False),
            "herb_match_requires_user_confirmation": ingredient_qc.get("herb_match_requires_user_confirmation", False),
            "ob_dl_pass_ingredient_count": ingredient_qc.get("ob_dl_pass_ingredient_count", ""),
            "ob_dl_filtered_ingredient_count": ingredient_qc.get("ob_dl_filtered_ingredient_count", ""),
            "no_target_filtered_ingredient_count": ingredient_qc.get("no_target_filtered_ingredient_count", ""),
            "smiles_filtered_ingredient_count": ingredient_qc.get("smiles_filtered_ingredient_count", ""),
            "raw_target_relation_count": ingredient_qc.get("raw_target_relation_count", ""),
            "normalized_target_count": ingredient_qc.get("normalized_target_count", ""),
            "intersection_targets": len(intersection),
            "core_targets": len(core),
            "docking_targets": len(docking),
            "docking_ligands": len(docking_ligands),
            "enrichment_terms": len(enrichment),
            "disease_requested_target_count": disease_summary.get("requested_target_count", ""),
            "disease_retrieved_target_count": disease_summary.get("retrieved_target_count", ""),
            "disease_id": disease_summary.get("disease_id", ""),
            "disease_label": disease_summary.get("disease_label", disease_summary.get("resolved_disease_name", "")),
            "disease_pagination_completed": disease_summary.get("pagination_completed", ""),
            "disease_fewer_than_requested_reason": disease_summary.get("fewer_than_requested_reason", ""),
            "docking_ligand_coverage": core_summary.get("docking_ligand_coverage", ""),
            "low_docking_ligand_warning": core_summary.get("low_docking_ligand_warning", False),
            "top_target_family": core_summary.get("top_target_family", ""),
            "dominant_family_target_count": core_summary.get("dominant_family_target_count", ""),
            "dominant_family_fraction": core_summary.get("dominant_family_fraction", ""),
            "pathway_dominance_warning": core_summary.get("pathway_dominance_warning", False),
        },
        "can_prioritize_targets": can_prioritize_targets,
        "can_prepare_targets": can_prepare_targets,
        "can_prepare_ligands": can_prepare_ligands,
        "can_enter_docking": can_enter_docking,
        "docking_readiness": readiness["docking_readiness"],
        "source_coverage_status": source_coverage_status,
        "failure_reason": source_merge_summary.get("failure_reason", "") if no_source_coverage else "",
        "tcmsp_data_source": tcmsp_data_source,
        "tcmsp_fallback_used": tcmsp_fallback_used,
        "tcmsp_source_conclusion": tcmsp_source_conclusion,
        "tcmsp_data_quality": tcmsp_data_quality,
        "herb_compound_target_source_qc": source_merge_summary,
        "source_reliability_audit": source_reliability_audit,
        "supplemental_source_audit": supplemental_source_audit,
        "herb_name_standardization": herb_name_standardization,
        "source_confidence_grade": source_reliability_audit.get("source_confidence_grade", interpretation_statuses.get("result_confidence_grade", "")),
        "network_candidate_count_by_source": {
            "TCMSP": tcmsp_obdl_active_count,
            "HerbiV": herbiv_candidate_count,
            "SymMap2": symmap_candidate_count,
        },
        "supplemental_sources_used": source_reliability_audit.get("sources_used", []),
        "sources_diagnostic_only": source_reliability_audit.get("sources_diagnostic_only", []),
        "supplemented_by_backup_sources": bool(source_merge_summary.get("supplemented_by_backup_sources", False))
        if isinstance(source_merge_summary, dict)
        else False,
        "herb_match_warning": herb_match_warning,
        "disease_mapping_diagnostic": disease_summary.get("disease_mapping_diagnostic", {}),
        "disease_mapping_warning": disease_mapping_warning,
        "docking_ligand_coverage": core_summary.get("docking_ligand_coverage", ""),
        "low_docking_ligand_warning": core_summary.get("low_docking_ligand_warning", False),
        "docking_ligand_warning": docking_ligand_warning,
        "pathway_dominance_warning": core_summary.get("pathway_dominance_warning", False),
        "pathway_dominance_warning_text": pathway_dominance_warning_text,
        "warnings": warnings[:300],
        "warning_summary": warning_summary,
        "generated_files": [
            str(report_dir / "network_pharmacology_report.md"),
            str(report_dir / "network_pharmacology_report.html"),
            str(report_dir / "qc_report.json"),
        ],
        "final_scoring": {
            "ligand_score_formula": "0.30*ingredient_confidence + 0.25*structure_quality + 0.20*target_link + 0.15*disease_relevance + 0.10*multi_source_support",
            "target_score_formula": "0.25*source_confidence + 0.25*disease_association_norm + 0.20*ppi_topology + 0.20*compound_coverage + 0.10*multi_source_support",
            "ligand_min_score": 0.55,
            "target_min_score": 0.55,
            "source_weights": {
                "TCMSP_OBDL_ACTIVE": 1.00,
                "HERBIV_NETWORK_INGREDIENT": 0.75,
                "SYMMAP_LINKED_INGREDIENT": 0.65,
                "TCMSP_COMPOUND_TARGET": 0.90,
                "HERBIV_COMPOUND_TARGET": 0.65,
                "SYMMAP_COMPOUND_TARGET": 0.60,
            },
        },
    }

    input_lines = input_lines_for_qc(qc)
    selected_warnings = unique_warnings(warnings, 10)
    mapping_qc_rows = [
        {
            "total_rows": target_mapping_total,
            "mapped_rows": target_mapping_success,
            "unmapped_rows": target_mapping_failed,
            "ambiguous_rows": target_mapping_ambiguous,
            "ignored_rows": target_mapping_ignored,
            "unique_mapped_genes": mapping_summary.get("unique_mapped_genes", ""),
            "success_rate": f"{target_mapping_success_rate:.1%}",
        }
    ]
    source_reliability_rows = source_reliability_audit.get("source_reliability", []) if isinstance(source_reliability_audit, dict) else []
    union_qc_rows = [
        {
            "union_ingredients": len(union_ingredients) or source_merge_summary.get("union_unique_ingredients", 0),
            "union_targets": len(union_targets) or source_merge_summary.get("union_unique_targets", 0),
            "union_edges": len(union_edges) or source_merge_summary.get("union_unique_ingredient_target_edges", 0),
            "duplicates_ingredients_removed": source_merge_summary.get("duplicate_ingredients_removed", ""),
            "duplicates_edges_removed": source_merge_summary.get("duplicate_edges_removed", ""),
            "sqlite": source_merge_summary.get("sqlite_path", ""),
        }
    ]
    active_candidate_rows = [
        {"category": "TCMSP OB/DL active ingredients", "count": tcmsp_obdl_active_count, "definition": "TCMSP records passing OB/DL and target availability filters"},
        {"category": "HerbiV candidate ingredients", "count": herbiv_candidate_count, "definition": "Only counted when HerbiV has verifiable herb-ingredient-target relations"},
        {"category": "SymMap2 candidate ingredients", "count": symmap_candidate_count, "definition": "Direct herb->ingredient and ingredient->target relations only"},
        {"category": "Merged network candidate ingredients", "count": merged_candidate_count, "definition": "Deduplicated multi-source records used for network analysis"},
    ]
    structure_qc_rows = [
        {"category": "PubChem CID-ready records", "count": structure_readiness_summary.get("pubchem_cid_ready", ingredient_qc.get("pubchem_cid_ready_ligands", "")), "definition": "CID present, not necessarily SMILES-ready"},
        {"category": "Structure-ready ligands", "count": structure_ready_count, "definition": "SMILES available from PubChem CID lookup or source property"},
        {"category": "Docking-ready ligands", "count": docking_ready_count, "definition": "SMILES-ready only; PDBQT generation is not performed in this workflow"},
        {"category": "Docking ligands linked to targets", "count": len(docking_ligands), "definition": "Unique structure-ready ligands linked to prioritized targets"},
    ]

    md = f"""# Network Pharmacology Report

Generated at: {qc["generated_at"]}

## Input

{chr(10).join(input_lines)}

## QC Summary

- Status: {qc["status"]}
- Workflow status: {qc["workflow_status"]}
- Data source status: {qc["data_source_status"]}
- Source coverage status: {source_coverage_status}
- Herb match status: {qc["herb_match_status"]}
- Disease mapping status: {qc["disease_mapping_status"]}
- Docking readiness status: {qc["docking_readiness_status"]}
- Interpretation status: {qc["interpretation_status"]}
- Result confidence grade: {qc["result_confidence_grade"]}
- Source confidence grade: {qc["source_confidence_grade"]}
- Merged network candidate ingredient records: {merged_candidate_count}
- TCMSP OB/DL active ingredients: {tcmsp_obdl_active_count}
- HerbiV candidate ingredients: {herbiv_candidate_count}
- SymMap2 candidate ingredients: {symmap_candidate_count}
- Deduplicated active ingredients: {deduplicated_active_count}
- Active ingredients with SMILES: {active_with_smiles}
- Active ingredients without SMILES: {active_without_smiles}
- Ligand-ready ingredient records: {ligand_ready_count}
- Unique ligand-ready molecules: {unique_ligand_ready_count}
- Structure-ready ligands: {structure_ready_count}
- Docking-ready ligands: {docking_ready_count}
- TCMSP data source: {tcmsp_data_source}
- TCMSP source conclusion: {tcmsp_source_conclusion}
- Original herb input: {herb_name_standardization.get("input_herb_name", qc.get("herb_name", ""))}
- Standardized herb for lookup: {herb_name_standardization.get("standardized_herb_for_lookup", qc.get("herb_name", ""))}
- Herb standardization status: {herb_name_standardization.get("standardization_status", "")}
- Herb standardization warning: {herb_name_standardization.get("warning", "") or "None"}
- Workflow status passed means the workflow completed; it does not mean live TCMSP scrape succeeded.
- TCMSP fallback used: {str(tcmsp_fallback_used).lower()}
- Supplemented by HerbiV/SymMap2: {str(bool(source_merge_summary.get("supplemented_by_backup_sources", False)) if isinstance(source_merge_summary, dict) else False).lower()}
- TCMSP remote attempted: {str(bool(tcmsp_data_quality.get("remote_attempted", False))).lower()}
- TCMSP remote success: {str(bool(tcmsp_data_quality.get("remote_success", False))).lower()}
- TCMSP source detail: {tcmsp_data_quality.get("source_detail", official_download_method)}
- TCMSP full snapshot: {str(bool(tcmsp_data_quality.get("is_full_snapshot", False))).lower()}
- TCMSP seed cache: {str(bool(tcmsp_data_quality.get("is_seed_cache", False))).lower()}
- TCMSP matched herb: {ingredient_qc.get("matched_herb_name", "")} {ingredient_qc.get("matched_herb_id", "")}
- Herb match type: {ingredient_qc.get("herb_match_type", "")}
- Herb exact match: {str(bool(ingredient_qc.get("herb_exact_match", False))).lower()}
- Herb match requires user confirmation: {str(bool(ingredient_qc.get("herb_match_requires_user_confirmation", False))).lower()}
- Raw ingredients / active ingredients: {ingredient_qc.get("raw_ingredient_count", len(active))}/{len(active)}
- Raw target relations / normalized target rows: {ingredient_qc.get("raw_target_relation_count", "")}/{ingredient_qc.get("normalized_target_count", "")}
- Intersection targets: {len(intersection)}
- Docking targets: {len(docking)}
- Docking ligands: {len(docking_ligands)}
- Docking ligand coverage: {core_summary.get("docking_ligand_coverage", "")}
- Low docking ligand warning: {str(bool(core_summary.get("low_docking_ligand_warning", False))).lower()}
- Target family dominance warning: {str(bool(core_summary.get("pathway_dominance_warning", False))).lower()}
- Enrichment terms: {len(enrichment)}
- Disease targets requested/retrieved: {disease_summary.get("requested_target_count", "")}/{disease_summary.get("retrieved_target_count", "")}
- Disease ID: {disease_summary.get("disease_id", "")}
- Can prioritize targets: {str(can_prioritize_targets).lower()}
- Can prepare targets: {str(can_prepare_targets).lower()}
- Can prepare ligands: {str(can_prepare_ligands).lower()}
- Can enter docking: {str(can_enter_docking).lower()}

## Herb Match QC

- Input herb: {ingredient_qc.get("input_herb_name", qc.get("herb_name", ""))}
- Original input herb: {herb_name_standardization.get("input_herb_name", qc.get("herb_name", ""))}
- Standardized lookup herb: {herb_name_standardization.get("standardized_herb_for_lookup", qc.get("herb_name", ""))}
- Standardization status: {herb_name_standardization.get("standardization_status", "")}
- Standardization candidates: {"; ".join(item.get("name", "") for item in herb_name_standardization.get("standardized_candidates", []) if isinstance(item, dict)) if isinstance(herb_name_standardization.get("standardized_candidates", []), list) else ""}
- Matched herb: {ingredient_qc.get("matched_herb_name", "")} {ingredient_qc.get("matched_herb_id", "")}
- Match type: {ingredient_qc.get("herb_match_type", "")}
- Match field: {ingredient_qc.get("herb_match_field", "")}
- Match score: {ingredient_qc.get("herb_match_score", "")}
- Exact match: {str(bool(ingredient_qc.get("herb_exact_match", False))).lower()}
- Alias match: {str(bool(ingredient_qc.get("herb_alias_match", False))).lower()}
- Fuzzy match used: {str(bool(ingredient_qc.get("herb_fuzzy_match_used", False))).lower()}
- Requires user confirmation: {str(bool(ingredient_qc.get("herb_match_requires_user_confirmation", False))).lower()}
- Warning: {herb_match_warning or "None"}

## No-Source Coverage QC

- Source coverage status: {source_coverage_status}
- Failure reason: {qc.get("failure_reason", "") or "None"}
- Recommended next input: {herb_name_standardization.get("canonical_herb_name", "")}
- Note: if this section reports no coverage, the workflow generated diagnostics but the herb-side network is not interpretable.

## Disease Mapping QC

- Input disease: {disease_summary.get("input_disease", disease_summary.get("input_disease_name", ""))}
- OpenTargets query used: {disease_summary.get("open_targets_query_used", "")}
- Resolved disease: {disease_summary.get("selected_disease_name", disease_summary.get("resolved_disease_name", ""))}
- Disease ID: {disease_summary.get("selected_disease_id", disease_summary.get("disease_id", ""))}
- Ontology source: {disease_summary.get("ontology_source", "")}
- Mapping score: {disease_summary.get("mapping_score", "")}
- Generic disease term: {str(bool(disease_summary.get("is_generic_disease_term", False))).lower()}
- Disease mapping warning: {disease_mapping_warning or "None"}

## Docking Coverage QC

- Unique ligand-ready molecules: {unique_ligand_ready_count}
- Unique docking ligands: {len(docking_ligands)}
- Docking ligand coverage: {core_summary.get("docking_ligand_coverage", "")}
- Low ligand warning: {docking_ligand_warning or "None"}

## Target Family / Pathway Dominance QC

- Top target family: {core_summary.get("top_target_family", "")}
- Dominant family target count: {core_summary.get("dominant_family_target_count", "")}
- Dominant family fraction: {core_summary.get("dominant_family_fraction", "")}
- Top enrichment theme: {core_summary.get("top_enrichment_theme", "")}
- Dominance warning: {pathway_dominance_warning_text or "None"}

## Target Mapping QC

{markdown_table(mapping_qc_rows, ["total_rows", "mapped_rows", "unmapped_rows", "ambiguous_rows", "ignored_rows", "unique_mapped_genes", "success_rate"], 1)}

## TCMSP Source QC

- Conclusion: {tcmsp_source_conclusion}
- Remote primary search URL: {tcmsp_data_quality.get("remote_primary_search_url", tcmsp_data_quality.get("remote_search_url", ""))}
- Remote last attempt URL: {tcmsp_data_quality.get("remote_last_attempt_url", "")}
- Remote search URL: {tcmsp_data_quality.get("remote_search_url", "")}
- Remote candidate count: {tcmsp_data_quality.get("remote_candidate_count", "")}
- Remote candidate failure reason: {tcmsp_data_quality.get("remote_candidate_failure_reason", "")}
- Remote selected candidate URL: {tcmsp_data_quality.get("remote_selected_candidate_url", "")}
- Remote selected candidate field: {tcmsp_data_quality.get("remote_selected_candidate_match_field", "")}
- Remote selected candidate score: {tcmsp_data_quality.get("remote_selected_candidate_match_score", "")}
- Remote detail URL: {tcmsp_data_quality.get("remote_detail_url", "")}
- Remote HTTP status: {tcmsp_data_quality.get("remote_http_status", "")}
- Remote response length: {tcmsp_data_quality.get("remote_response_length", "")}
- Remote failure reason: {tcmsp_data_quality.get("remote_failure_reason", "") or "None"}
- Remote page type: {tcmsp_data_quality.get("remote_page_type", "")}
- Remote HTTP OK: {str(bool(tcmsp_data_quality.get("remote_http_ok", False))).lower()}
- Remote HTML valid: {str(bool(tcmsp_data_quality.get("remote_html_valid", False))).lower()}
- Remote grid found: {str(bool(tcmsp_data_quality.get("remote_grid_found", False))).lower()}
- Remote grid2 found: {str(bool(tcmsp_data_quality.get("remote_grid2_found", False))).lower()}
- Remote grid rows: {tcmsp_data_quality.get("remote_grid_row_count", "")}
- Remote grid2 rows: {tcmsp_data_quality.get("remote_grid2_row_count", "")}
- Remote grid required fields valid: {str(bool(tcmsp_data_quality.get("remote_grid_required_fields_valid", False))).lower()}
- Remote grid2 required fields valid: {str(bool(tcmsp_data_quality.get("remote_grid2_required_fields_valid", False))).lower()}
- Remote parse success: {str(bool(tcmsp_data_quality.get("remote_parse_success", False))).lower()}
- Live TCMSP scrape validated: {str(bool(tcmsp_data_quality.get("live_tcmsp_scrape_validated", False))).lower()}
- Fallback snapshot validated: {str(bool(tcmsp_data_quality.get("fallback_snapshot_validated", False))).lower()}
- Final TCMSP source is live: {str(bool(tcmsp_data_quality.get("final_tcmsp_source_is_live", False))).lower()}
- Actual source attempt order: {" > ".join(tcmsp_data_quality.get("actual_source_attempt_order", [])) if isinstance(tcmsp_data_quality.get("actual_source_attempt_order", []), list) else tcmsp_data_quality.get("actual_source_attempt_order", "")}
- Fallback after remote: {str(bool(tcmsp_data_quality.get("fallback_after_remote", False))).lower()}
- Download/cache files: {"; ".join(download_file_paths) if isinstance(download_file_paths, list) else download_file_paths}
- Snapshot DB: {tcmsp_data_quality.get("snapshot_db_path", "")}
- Snapshot manifest: {tcmsp_data_quality.get("snapshot_manifest_path", "")}
- Snapshot audit: {tcmsp_data_quality.get("snapshot_audit_path", "")}
- Snapshot source type: {tcmsp_data_quality.get("snapshot_source_type", "")}
- Source completeness level: {tcmsp_data_quality.get("source_completeness_level", "")}
- Raw ingredients / compound targets actual source: {tcmsp_data_source}
- Herb match field: {ingredient_qc.get("herb_match_field", "")}
- Herb match score: {ingredient_qc.get("herb_match_score", "")}
- Active ingredient count explanation: {active_explanation}
- Completeness warning: {tcmsp_data_quality.get("completeness_warning", "") or "None"}

## Herb Compound/Target Source QC

{markdown_table(source_qc_rows, ["source", "status", "raw_ingredients", "raw_target_relations", "source_detail", "diagnostic_file"], 10)}

- Allowed herb compound/target sources: TCMSP; HerbiV; SymMap 2.0
- PubChem is used only for structure enrichment. OpenTargets is used only for disease targets.
- Source merge summary file: `02_compound_targets/source_merge_summary.json`

## Source Reliability QC

{markdown_table(source_reliability_rows, ["source_database", "source_availability", "source_reproducibility", "source_relation_quality", "source_evidence_level", "source_confidence_grade", "contributed_ingredients", "contributed_target_relations"], 10)}

- Supplemental source audit: `02_compound_targets/supplemental_source_audit.json`
- Sources diagnostic only: {"; ".join(qc.get("sources_diagnostic_only", [])) if isinstance(qc.get("sources_diagnostic_only", []), list) else qc.get("sources_diagnostic_only", "")}

## Union Data QC

{markdown_table(union_qc_rows, ["union_ingredients", "union_targets", "union_edges", "duplicates_ingredients_removed", "duplicates_edges_removed", "sqlite"], 1)}

{preview_table(union_ingredients, ["canonical_ingredient_id", "ingredient_name", "pubchem_cid", "cas", "source_list", "confidence_level", "match_type"], limit=12, file_path="01_ingredients/union_ingredients.csv")}

{preview_table(union_edges, ["ingredient_name", "target_gene", "source_list", "relation_type", "evidence_types", "confidence_level", "ingredient_confidence", "relation_confidence"], limit=12, file_path="02_compound_targets/union_ingredient_target_edges.csv")}

## Final Scoring Strategy

NPFlow uses multi-factor weighted scoring to rank docking candidates, replacing the old equal-weight mean-rank method.

### Ligand Score Formula

```
ligand_score = 0.30 × ingredient_confidence + 0.25 × structure_quality + 0.20 × target_link + 0.15 × disease_relevance + 0.10 × multi_source_support
```

| Factor | Weight | Description |
|--------|--------|-------------|
| ingredient_confidence | 0.30 | Source-weighted confidence (TCMSP=1.00, HerbiV=0.75–0.85, SymMap2=0.65–0.75) |
| structure_quality | 0.25 | PubChem CID + SMILES + InChIKey completeness |
| target_link | 0.20 | Number of high-confidence docking targets linked |
| disease_relevance | 0.15 | Mean disease association score of linked intersection targets |
| multi_source_support | 0.10 | Bonus for corroboration across multiple databases |

### Target Score Formula

```
target_score = 0.25 × source_confidence + 0.25 × disease_association_norm + 0.20 × ppi_topology + 0.20 × compound_coverage + 0.10 × multi_source_support
```

| Factor | Weight | Description |
|--------|--------|-------------|
| source_confidence | 0.25 | Max relation confidence across all linked ingredients |
| disease_association_norm | 0.25 | OpenTargets disease association score (0–1 normalized) |
| ppi_topology | 0.20 | Mean of normalized degree, betweenness, and closeness ranks |
| compound_coverage | 0.20 | Log-scaled count of high-confidence ligands |
| multi_source_support | 0.10 | Bonus for corroboration across multiple databases |

### Source Confidence Weights

| Source | Ingredient Confidence | Relation Confidence |
|--------|----------------------|---------------------|
| TCMSP OB/DL Active | 1.00 | 0.90 |
| HerbiV Network | 0.75–0.85 | 0.65 (score-driven) |
| SymMap2 Linked | 0.65–0.75 | 0.60 |

## Active vs Candidate Counts

{markdown_table(active_candidate_rows, ["category", "count", "definition"], 4)}

## Structure/Docking Readiness

{markdown_table(structure_qc_rows, ["category", "count", "definition"], 4)}

## Warning Summary

{markdown_table(warning_summary, ["category", "count", "detail_file"], 10)}

## Top Active Ingredients

{preview_table(active, ["ingredient_id", "ingredient_name", "ob", "dl", "pubchem_cid", "smiles", "pubchem_match_status"], limit=15, file_path="01_ingredients/active_ingredients.csv")}

## Deduplicated Active Ingredients

{preview_table(deduplicated_active, ["deduplicated_ingredient_id", "identity_type", "ingredient_name", "pubchem_cid", "inchikey", "active_record_count", "pubchem_match_status"], limit=15, file_path="01_ingredients/deduplicated_active_ingredients.csv")}

## Ligand-Ready Ingredient Records

{preview_table(ligand_ready, ["ingredient_id", "ingredient_name", "pubchem_cid", "smiles", "compound_target_count", "pubchem_match_status"], limit=15, file_path="01_ingredients/ligand_ready_ingredients.csv")}

## PubChem Query Attempts

{preview_table(pubchem_query_attempts, ["ingredient_id", "ingredient_name", "candidate", "candidate_type", "http_status", "accepted", "decision_reason", "pubchem_cid"], limit=20, file_path="01_ingredients/pubchem_query_attempts.csv")}

## Top Intersection Targets

{preview_table(intersection, ["target_gene", "target_name", "disease_association_score", "compound_count"], limit=20, file_path="04_intersection/intersection_targets.csv")}

## Core Targets For Docking (ranked by target_score)

{preview_table(docking, ["target_gene", "target_name", "target_score", "target_confidence", "disease_association_score", "compound_count", "source_list", "selection_reason"], limit=20, file_path="07_core_targets/docking_targets.csv")}

## Unique Ligands For Docking (ranked by ligand_score)

{preview_table(docking_ligands, ["ligand_id", "ligand_score", "ligand_confidence", "source_ingredient_names", "docking_target_count", "source_list", "selection_reason"], limit=20, file_path="07_core_targets/docking_ligands.csv")}

## Top Enrichment Terms

{preview_table(enrichment, ["source", "native", "name", "raw_p_value", "adjusted_p_value", "significant", "p_value_kind", "intersection_size", "intersections"], limit=20, file_path="06_enrichment/enrichment_all.csv")}

## Warnings

{chr(10).join("- " + warning for warning in selected_warnings) if selected_warnings else "- None"}

Full details are written to `01_ingredients/pubchem_unmatched_ingredients.csv`, `02_compound_targets/unmapped_targets.csv`, `02_compound_targets/ambiguous_targets.csv`, and `02_compound_targets/target_mapping_summary.json`.

## Methodological Caveats

- `workflow_status=passed` means the software pipeline completed. Use `interpretation_status` and `result_confidence_grade` for biological interpretation readiness.
- Snapshot fallback is reproducible, but it is not the same as a validated live TCMSP scrape. Reports keep `final_tcmsp_source_is_live` explicit.
- Fuzzy herb matches, broad disease terms, low docking ligand coverage, and target-family dominance should be treated as interpretation warnings for article-level analysis.
- Common hub targets can appear because they are broadly studied or highly connected, not necessarily because they are specific to the herb, compound, or disease.
- Docking readiness is split from target prioritization. `docking_targets.csv` means target-side prioritization is available; `docking_ligands.csv` additionally requires ligand structure readiness.
- PubChem-unmatched active ingredients can still be used in network analysis when they have mapped targets, but they are excluded from docking ligand preparation until a CID or SMILES is available.
- No-overlap runs are reported as warning results rather than software failures. This can indicate limited database coverage or a true absence of shared targets in the selected sources.
"""

    (report_dir / "network_pharmacology_report.md").write_text(md, encoding="utf-8")
    (report_dir / "network_pharmacology_report.html").write_text(render_html_from_markdown(md), encoding="utf-8")
    write_json(report_dir / "qc_report.json", qc)
    print(f"Report generated in {report_dir}.")


if __name__ == "__main__":
    exit_with_errors(main)

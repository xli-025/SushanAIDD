#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import os
import json
import sys
from pathlib import Path

import requests

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


FIELDS = [
    "source",
    "native",
    "name",
    "p_value",
    "significant",
    "description",
    "term_size",
    "query_size",
    "intersection_size",
    "effective_domain_size",
    "precision",
    "recall",
    "intersections",
    "raw_p_value", "adjusted_p_value", "p_value_kind", "evidences", "gene_mapping_status", "provider",
]

SOURCE_FILES = {
    "GO:BP": "go_bp.csv",
    "GO:MF": "go_mf.csv",
    "GO:CC": "go_cc.csv",
    "KEGG": "kegg.csv",
    "REAC": "reactome.csv",
}

ENRICHR_LIBRARIES = {
    "GO_Biological_Process_2023": "GO:BP",
    "GO_Molecular_Function_2023": "GO:MF",
    "GO_Cellular_Component_2023": "GO:CC",
    "KEGG_2021_Human": "KEGG",
    "Reactome_2022": "REAC",
}


def empty_counts() -> dict[str, int]:
    return {source: 0 for source in SOURCE_FILES}


def parse_native(term: str, source: str) -> str:
    if source.startswith("GO:"):
        match = re.search(r"(GO:\d+)", term)
        return match.group(1) if match else ""
    if source == "REAC":
        match = re.search(r"(R-HSA-\d+)", term)
        return match.group(1) if match else ""
    return ""


def row_from_enrichr(item: list, source: str, query_size: int) -> dict:
    term = str(item[1] if len(item) > 1 else "")
    p_value = item[2] if len(item) > 2 else ""
    overlap = item[5] if len(item) > 5 and isinstance(item[5], list) else []
    adjusted_p = item[6] if len(item) > 6 else p_value
    intersection_size = len(overlap)
    try:
        significant = float(adjusted_p) <= 0.05
    except (TypeError, ValueError):
        significant = False
    precision = (intersection_size / query_size) if query_size else ""
    return {
        "source": source,
        "native": parse_native(term, source),
        "name": term,
        "p_value": p_value,
        "raw_p_value": p_value, "adjusted_p_value": adjusted_p, "p_value_kind": "raw",
        "provider": "enrichr", "gene_mapping_status": "provider_gene_names",
        "significant": significant,
        "description": term,
        "term_size": "",
        "query_size": query_size,
        "intersection_size": intersection_size,
        "effective_domain_size": "",
        "precision": precision,
        "recall": "",
        "intersections": "; ".join(str(value) for value in overlap),
    }


def row_from_reactome(pathway: dict, query_size: int) -> dict:
    entities = pathway.get("entities") or {}
    total = entities.get("total") or ""
    found = entities.get("found") or ""
    p_value = entities.get("pValue", "")
    fdr = entities.get("fdr", "")
    try:
        significant = float(fdr) <= 0.05
    except (TypeError, ValueError):
        significant = False
    try:
        precision = (float(found) / query_size) if query_size else ""
    except (TypeError, ValueError):
        precision = ""
    try:
        recall = (float(found) / float(total)) if total else ""
    except (TypeError, ValueError):
        recall = ""
    return {
        "source": "REAC",
        "native": pathway.get("stId", ""),
        "name": pathway.get("name", ""),
        "p_value": p_value,
        "raw_p_value": p_value, "adjusted_p_value": fdr, "p_value_kind": "raw",
        "provider": "reactome", "gene_mapping_status": "not_returned",
        "significant": significant,
        "description": pathway.get("name", ""),
        "term_size": total,
        "query_size": query_size,
        "intersection_size": found,
        "effective_domain_size": "",
        "precision": precision,
        "recall": recall,
        "intersections": "",
    }


def decode_gprofiler_genes(item: dict, meta: dict) -> tuple[list[str], list, str]:
    """Decode evidence arrays aligned with meta.ensgs (official client contract)."""
    evidence = item.get("intersections") or []
    if evidence and not all(isinstance(group, list) for group in evidence):
        return [], evidence, "unresolved_unexpected_evidence_shape"
    data = meta.get("genes_metadata", {}).get("query", {}).get(item.get("query", "query_1"), {})
    ensgs = data.get("ensgs") or []
    if len(ensgs) != len(evidence):
        return [], evidence, "unresolved_missing_or_misaligned_metadata"
    reverse = {}
    for incoming, mapped in (data.get("mapping") or {}).items():
        for ensg in mapped:
            reverse[ensg] = incoming if len(mapped) == 1 else ensg
    return ([reverse.get(ensg, ensg) for ensg, ev in zip(ensgs, evidence) if ev],
            [ev for ev in evidence if ev], "mapped_from_response_metadata")


def run_gprofiler(genes: list[str]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    if len(genes) < 2:
        return [], ["Fewer than two genes available for enrichment analysis."]
    try:
        timeout = int(os.environ.get("NPFLOW_GPROFILER_TIMEOUT", "15"))
        retries = int(os.environ.get("NPFLOW_GPROFILER_RETRIES", "0"))
        response = http_post(
            "https://biit.cs.ut.ee/gprofiler/api/gost/profile/",
            json_body={
                "organism": "hsapiens",
                "query": genes,
                "sources": list(SOURCE_FILES),
                "user_threshold": 0.05,
                "no_evidences": False,
            },
            timeout=timeout,
            retries=retries,
        )
        if response.status_code != 200:
            return [], [f"g:Profiler returned HTTP {response.status_code}."]
        payload = response.json()
        if "result" not in payload:
            raise ValueError("g:Profiler response has no result field")
        result = payload["result"]
        meta = payload.get("meta") or {}
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ValueError("g:Profiler result must be a list of objects")
        for item in result:
            intersections = item.get("intersections") or []
            if not isinstance(intersections, list):
                raise ValueError("g:Profiler intersections must be a list")
            if intersections and isinstance(intersections[0], list):
                if any(not isinstance(group, list) for group in intersections):
                    raise ValueError("g:Profiler intersections contain mixed group types")
    except Exception as exc:
        return [], [f"富集 API 暂不可用，已降级继续。g:Profiler request failed: {exc}"]

    rows: list[dict] = []
    for item in result:
        flat_intersections, evidences, mapping_status = decode_gprofiler_genes(item, meta)
        if mapping_status.startswith("unresolved"):
            warnings.append("g:Profiler gene mapping unavailable; evidence codes are not genes: " + str(item.get("native", "")))
        rows.append(
            {
                "source": item.get("source", ""),
                "native": item.get("native", ""),
                "name": item.get("name", ""),
                "p_value": item.get("p_value", ""),
                "raw_p_value": "", "adjusted_p_value": item.get("p_value", ""),
                "p_value_kind": "adjusted_g_SCS", "provider": "gprofiler",
                "evidences": json.dumps(evidences, ensure_ascii=False),
                "gene_mapping_status": mapping_status,
                "significant": item.get("significant", ""),
                "description": item.get("description", ""),
                "term_size": item.get("term_size", ""),
                "query_size": item.get("query_size", ""),
                "intersection_size": item.get("intersection_size", ""),
                "effective_domain_size": item.get("effective_domain_size", ""),
                "precision": item.get("precision", ""),
                "recall": item.get("recall", ""),
                "intersections": "; ".join(flat_intersections),
            }
        )
    return rows, warnings


def run_enrichr(genes: list[str]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    timeout = int(os.environ.get("NPFLOW_ENRICHR_TIMEOUT", "25"))
    session = requests.Session()
    session.headers.update({"User-Agent": "NPFlow-Agent/0.1.0 (network pharmacology research workflow)"})
    response = session.post(
        "https://maayanlab.cloud/Enrichr/addList",
        files={
            "list": (None, "\n".join(genes)),
            "description": (None, "NPFlow network pharmacology targets"),
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Enrichr addList returned HTTP {response.status_code}: {response.text[:200]}")
    user_list_id = response.json().get("userListId")
    if not user_list_id:
        raise RuntimeError(f"Enrichr addList did not return userListId: {response.text[:200]}")

    rows: list[dict] = []
    reactome_rows: list[dict] = []
    reactome_failed = False
    for library, source in ENRICHR_LIBRARIES.items():
        try:
            enrich_response = session.get(
                "https://maayanlab.cloud/Enrichr/enrich",
                params={"userListId": user_list_id, "backgroundType": library},
                timeout=timeout,
            )
            if enrich_response.status_code != 200:
                raise RuntimeError(f"HTTP {enrich_response.status_code}: {enrich_response.text[:200]}")
            values = enrich_response.json().get(library, [])
            source_rows = [row_from_enrichr(item, source, len(genes)) for item in values]
            if source == "REAC":
                reactome_rows = source_rows
            rows.extend(source_rows)
        except Exception as exc:
            warnings.append(f"{library} Enrichr fallback failed: {exc}")
            if source == "REAC":
                reactome_failed = True

    if reactome_failed:
        try:
            reactome_rows = run_reactome_official(genes)
            rows.extend(reactome_rows)
            if reactome_rows:
                warnings.append("Reactome_2022 Enrichr fallback failed; used Reactome AnalysisService.")
        except Exception as exc:
            warnings.append(f"Reactome AnalysisService fallback failed: {exc}")
    return rows, warnings


def run_reactome_official(genes: list[str]) -> list[dict]:
    timeout = int(os.environ.get("NPFLOW_REACTOME_TIMEOUT", "25"))
    response = requests.post(
        "https://reactome.org/AnalysisService/identifiers/projection",
        params={"pageSize": 500, "page": 1},
        data="\n".join(genes).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "text/plain",
            "User-Agent": "NPFlow-Agent/0.1.0 (network pharmacology research workflow)",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Reactome AnalysisService returned HTTP {response.status_code}: {response.text[:200]}")
    pathways = response.json().get("pathways") or []
    return [row_from_reactome(pathway, len(genes)) for pathway in pathways]


def run_enrichment(genes: list[str]) -> tuple[list[dict], list[str], str]:
    if len(genes) < 2:
        return [], ["Fewer than two genes available for enrichment analysis."], "none"

    provider = os.environ.get("NPFLOW_ENRICHMENT_PROVIDER", "auto").strip().lower() or "auto"
    warnings: list[str] = []
    gprofiler_warnings: list[str] = []
    if provider not in {"auto", "gprofiler", "enrichr"}:
        warnings.append(f"Unknown NPFLOW_ENRICHMENT_PROVIDER={provider}; using auto.")
        provider = "auto"

    if provider in {"auto", "gprofiler"}:
        rows, gprofiler_warnings = run_gprofiler(genes)
        if rows or not gprofiler_warnings or provider == "gprofiler":
            return rows, gprofiler_warnings, "gprofiler" if (rows or not gprofiler_warnings) else "none"

    if provider in {"auto", "enrichr"}:
        try:
            rows, enrichr_warnings = run_enrichr(genes)
            warnings.extend(enrichr_warnings)
            if rows:
                if provider == "auto":
                    detail = "; ".join(warning.replace("富集 API 暂不可用，已降级继续。", "").strip() for warning in gprofiler_warnings)
                    message = "gProfiler 不可用，已使用 Enrichr/Reactome 备用富集。"
                    if detail:
                        message += f" 原始错误: {detail}"
                    warnings.append(message)
                return rows, warnings, "enrichr_fallback"
            if not enrichr_warnings:
                return [], gprofiler_warnings, "enrichr_fallback"
        except Exception as exc:
            warnings.extend(gprofiler_warnings)
            warnings.append(f"Enrichr fallback failed: {exc}")

    if not any("富集 API 暂不可用" in warning for warning in warnings):
        warnings.append("富集 API 暂不可用，已降级继续。")
    return [], warnings, "none"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GO/KEGG/Reactome enrichment via g:Profiler.")
    parser.add_argument("--config", default="results/network_pharmacology/00_input/params.resolved.yaml")
    args = parser.parse_args()

    project_root_from_cwd()
    params = load_params(args.config)
    out_root = output_root(params)
    enrichment_dir = ensure_dir(out_root / "06_enrichment")

    intersection = read_rows(out_root / "04_intersection" / "intersection_targets.csv")
    genes = sorted({row["target_gene"].strip().upper() for row in intersection if row.get("target_gene")})
    rows, warnings, provider = run_enrichment(genes)
    rows.sort(key=lambda row: float(row["adjusted_p_value"] if row.get("adjusted_p_value") not in (None, "") else row.get("p_value", 1)))

    write_rows(enrichment_dir / "enrichment_all.csv", rows, FIELDS)
    for source, file_name in SOURCE_FILES.items():
        source_rows = [row for row in rows if row.get("source") == source]
        write_rows(enrichment_dir / file_name, source_rows, FIELDS)

    counts_by_source = {source: len([row for row in rows if row.get("source") == source]) for source in SOURCE_FILES}
    write_json(
        enrichment_dir / "enrichment_summary.json",
        {
            "module": "run_enrichment",
            "status": "passed" if rows else "warning",
            "provider": provider,
            "source_status": ("success" if rows else "success_empty") if provider != "none" else (
                "insufficient_input" if len(genes) < 2 else "unavailable"),
            "generated_at": now_utc(),
            "gene_count": len(genes),
            "term_count": len(rows),
            "counts_by_source": counts_by_source,
            "top_terms": rows[:10],
            "generated_files": [str(enrichment_dir / "enrichment_all.csv")]
            + [str(enrichment_dir / name) for name in SOURCE_FILES.values()],
            "warnings": warnings,
        },
    )
    print(f"Generated {len(rows)} enrichment rows.")


if __name__ == "__main__":
    exit_with_errors(main)

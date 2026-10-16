#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from openpyxl import load_workbook

from modules.network_pharmacology.scripts.common import ensure_dir, now_utc, unique_preserve, write_json, write_rows


SYM_MAP_BASE = "http://www.symmap.org"
SYM_MAP_DOWNLOAD_BASE = f"{SYM_MAP_BASE}/static/download/V2.0"
SYM_MAP_FILES = {
    "herbs": "SymMap v2.0, SMHB file.xlsx",
    "ingredients": "SymMap v2.0, SMIT file.xlsx",
    "targets": "SymMap v2.0, SMTT file.xlsx",
}

SOURCE_PRIORITY = {
    "TCMSP": 1,
    "TCMSP-compatible snapshot": 2,
    "HerbiV": 3,
    "SymMap2": 4,
}
DEFAULT_OB_MIN = 30.0

UNION_INGREDIENT_FIELDS = [
    "canonical_ingredient_id",
    "ingredient_name",
    "normalized_name",
    "alias_list",
    "pubchem_cid",
    "cas",
    "source_list",
    "source_ids",
    "evidence_types",
    "confidence_level",
    "ingredient_confidence",
    "source_eligibility",
    "source_eligibility_reason",
    "herbiv_combined_score",
    "relation_type",
    "match_type",
    "herb_name",
    "ob",
    "dl",
]

UNION_TARGET_FIELDS = [
    "canonical_target_id",
    "target_gene",
    "target_name",
    "uniprot_id",
    "organism",
    "organism_taxid",
    "source_list",
    "source_target_ids",
    "evidence_types",
    "evidence_class",
    "mapping_validated",
    "confidence_level",
    "target_confidence",
]

UNION_EDGE_FIELDS = [
    "canonical_ingredient_id",
    "canonical_target_id",
    "ingredient_id",
    "ingredient_name",
    "target_gene",
    "source_list",
    "eligible_source_list",
    "all_source_list",
    "source_edge_ids",
    "relation_type",
    "evidence_types",
    "evidence_class",
    "mapping_validated",
    "confidence_level",
    "ingredient_confidence",
    "relation_confidence",
    "network_eligibility",
    "network_eligibility_reason",
    "eligibility_status_by_source",
    "eligibility_reason_by_source",
    "is_predicted",
    "is_experimental",
    "is_literature_curated",
    "source_url",
]


def _normalize_text(value: object) -> str:
    text = str(value if value is not None else "").strip().lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\s_\-;,，/()（）]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"none", "nan", "null"}:
        return ""
    return text


def _strict_true(value: object) -> bool:
    """Interpret only explicit boolean true markers as true."""
    return value is True or (
        isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"}
    )


def _evidence_class(value: object) -> str:
    """Classify the source label without upgrading it to validation evidence."""
    text = _clean(value).casefold()
    if not text:
        return "source_record_unspecified"
    if any(token in text for token in ("predict", "computational", "svm", "random forest")):
        return "computational_prediction"
    if any(
        token in text
        for token in ("experimental", "experimentally", "assay", "in vitro", "in_vitro")
    ):
        return "experimental_source_label"
    if any(token in text for token in ("literature", "curated")):
        return "literature_curated_source_label"
    if "related_component" in text or "related component" in text:
        return "database_related_component"
    return "source_record_unclassified"


def _float_or_none(value: object) -> float | None:
    try:
        text = _clean(value)
        return float(text) if text else None
    except ValueError:
        return None


def _first_pubchem_cid(value: object) -> str:
    text = _clean(value)
    match = re.search(r"\d+", text)
    return match.group(0) if match else ""


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8')).hexdigest()[:16]}"


def _split_joined(value: str) -> list[str]:
    return [part.strip() for part in re.split(r";\s*", str(value or "")) if part.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_xlsx(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 100:
        return False
    with path.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


def _download_xlsx(url: str, path: Path, *, timeout: int = 60) -> dict[str, Any]:
    ensure_dir(path.parent)
    meta: dict[str, Any] = {"url": url, "path": str(path), "downloaded": False, "valid_xlsx": False}
    if _looks_like_xlsx(path):
        meta.update({"downloaded": False, "valid_xlsx": True, "sha256": _sha256(path), "bytes": path.stat().st_size})
        return meta
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "NPFlow-Agent/0.1.0"})
        meta.update(
            {
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(response.content),
                "response_preview": response.text[:200] if "text" in response.headers.get("content-type", "") else "",
            }
        )
        path.write_bytes(response.content)
        meta["downloaded"] = True
        meta["valid_xlsx"] = _looks_like_xlsx(path)
        if meta["valid_xlsx"]:
            meta["sha256"] = _sha256(path)
    except Exception as exc:  # noqa: BLE001 - diagnostic-only downloader
        meta.update({"error": str(exc), "valid_xlsx": False})
    return meta


def _read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_clean(value) for value in rows[0]]
    output: list[dict[str, str]] = []
    for values in rows[1:]:
        item = {headers[index]: _clean(value) for index, value in enumerate(values) if index < len(headers)}
        if any(item.values()):
            output.append(item)
    workbook.close()
    return output


def _symmap_rrid(prefix: str, value: object) -> str:
    text = _clean(value)
    if text.upper().startswith(prefix):
        return text.upper()
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return text
    return f"{prefix}{int(digits):06d}"


def _match_aliases(row: dict[str, str]) -> list[str]:
    aliases = []
    for field in ("Chinese_name", "Pinyin_name", "Latin_name", "English_name", "Alias"):
        value = row.get(field, "")
        if not value:
            continue
        aliases.append(value)
        aliases.extend(part.strip() for part in re.split(r"[;,，/|]", value) if part.strip())
    return unique_preserve(aliases)


def _resolve_symmap_herb(rows: list[dict[str, str]], herb_name: str, aliases: list[str]) -> dict[str, Any]:
    query_values = unique_preserve([herb_name, *aliases])
    query_norms = {_normalize_text(value) for value in query_values if value}
    for row in rows:
        for field in ("Chinese_name", "Pinyin_name", "Latin_name", "English_name"):
            if _normalize_text(row.get(field, "")) in query_norms:
                return {"row": row, "match_status": "exact_match", "match_field": field, "match_score": 1.0}
        for alias in _match_aliases(row):
            if _normalize_text(alias) in query_norms:
                return {"row": row, "match_status": "alias_match", "match_field": "Alias", "match_score": 0.95}
    compact_queries = {value.replace(" ", "") for value in query_norms if value}
    best: tuple[float, dict[str, str], str] | None = None
    for row in rows:
        for field in ("Chinese_name", "Pinyin_name", "Latin_name", "English_name", "Alias"):
            value = _normalize_text(row.get(field, ""))
            if not value:
                continue
            score = 0.0
            compact = value.replace(" ", "")
            if compact and any(compact in query or query in compact for query in compact_queries):
                score = min(len(compact), max(len(q) for q in compact_queries or {compact})) / max(
                    len(compact), max(len(q) for q in compact_queries or {compact}), 1
                )
            if best is None or score > best[0]:
                best = (score, row, field)
    if best and best[0] >= 0.70:
        return {"row": best[1], "match_status": "fuzzy_match", "match_field": best[2], "match_score": round(best[0], 4)}
    return {"row": None, "match_status": "no_match", "match_field": "", "match_score": 0}


def _post_symmap_related(rrid: str, table_name: str, cache_dir: Path, diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_dir(cache_dir)
    cache_path = cache_dir / f"{rrid}_{table_name}.json"
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return payload.get("data", []) if isinstance(payload, dict) else []
        except json.JSONDecodeError:
            cache_path.unlink(missing_ok=True)
    url = f"{SYM_MAP_BASE}/related_components/"
    try:
        response = requests.post(
            url,
            data={"rrid": rrid, "table_name": table_name, "filter": "0"},
            timeout=15,
            headers={
                "User-Agent": "NPFlow-Agent/0.1.0",
                "Referer": f"{SYM_MAP_BASE}/detail/{quote(rrid)}",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        diagnostic.setdefault("api_attempts", []).append(
            {
                "rrid": rrid,
                "table_name": table_name,
                "http_status": response.status_code,
                "bytes": len(response.content),
                "content_type": response.headers.get("content-type", ""),
            }
        )
        payload = response.json()
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload.get("data", []) if isinstance(payload, dict) else []
    except Exception as exc:  # noqa: BLE001 - keep workflow alive with diagnostics
        diagnostic.setdefault("api_errors", []).append({"rrid": rrid, "table_name": table_name, "error": str(exc)})
        return []


def fetch_symmap2_source(project_root: Path, herb_name: str, aliases: list[str], out_root: Path) -> dict[str, Any]:
    source_root = ensure_dir(project_root / "data" / "herb_sources" / "symmap")
    raw_dir = ensure_dir(source_root / "raw")
    cache_dir = ensure_dir(source_root / "cache" / "related_components")
    diagnostic: dict[str, Any] = {
        "source": "SymMap2",
        "started_at": now_utc(),
        "status": "started",
        "download_files": {},
        "input_herb_name": herb_name,
        "relation_scope": "direct_herb_to_mol_then_mol_to_gene",
        "excluded_relation_scopes": [
            "disease_to_ingredient",
            "symptom_to_ingredient",
            "formula_to_ingredient",
            "related_herb_to_ingredient",
            "generic_network_without_selected_herb",
        ],
    }

    paths: dict[str, Path] = {}
    for key, filename in SYM_MAP_FILES.items():
        url = f"{SYM_MAP_DOWNLOAD_BASE}/{quote(filename)}"
        path = raw_dir / filename
        diagnostic["download_files"][key] = _download_xlsx(url, path)
        paths[key] = path

    valid_files = {key: path for key, path in paths.items() if _looks_like_xlsx(path)}
    manifest = {
        "source": "SymMap2",
        "generated_at": now_utc(),
        "files": {
            key: {
                "path": str(path),
                "valid_xlsx": _looks_like_xlsx(path),
                "sha256": _sha256(path) if _looks_like_xlsx(path) else "",
                "bytes": path.stat().st_size if path.exists() else 0,
            }
            for key, path in paths.items()
        },
    }
    write_json(source_root / "symmap_manifest.json", manifest)
    if "herbs" not in valid_files:
        diagnostic.update({"status": "unavailable", "reason": "valid_symmap_herb_file_not_available"})
        write_json(out_root / "01_ingredients" / "symmap_download_diagnostic.json", diagnostic)
        return {"ingredient_rows": [], "target_rows": [], "diagnostic": diagnostic}

    herb_rows = _read_xlsx_rows(valid_files["herbs"])
    diagnostic["herb_file_row_count"] = len(herb_rows)
    match = _resolve_symmap_herb(herb_rows, herb_name, aliases)
    if not match.get("row"):
        diagnostic.update({"status": "no_herb_match", "match_status": "no_match"})
        write_json(out_root / "01_ingredients" / "symmap_download_diagnostic.json", diagnostic)
        return {"ingredient_rows": [], "target_rows": [], "diagnostic": diagnostic}

    herb_row = match["row"]
    herb_rrid = _symmap_rrid("SMHB", herb_row.get("Herb_id", ""))
    diagnostic.update(
        {
            "match_status": match["match_status"],
            "match_field": match["match_field"],
            "match_score": match["match_score"],
            "matched_herb_id": herb_rrid,
            "matched_herb_name": herb_row.get("Chinese_name", "") or herb_row.get("English_name", ""),
            "matched_herb_aliases": _match_aliases(herb_row),
            "herb_match_requires_user_confirmation": match["match_status"] == "fuzzy_match",
        }
    )
    if match["match_status"] == "fuzzy_match":
        diagnostic.update(
            {
                "status": "fuzzy_match_not_imported",
                "reason": "SymMap2 fuzzy herb match requires user confirmation; records excluded from union.",
                "raw_ingredient_count": 0,
                "raw_target_relation_count": 0,
                "direct_herb_ingredient_rows": 0,
                "indirect_rows_excluded": 0,
                "excluded_reason_counts": {"fuzzy_match_requires_confirmation": 1},
            }
        )
        write_json(out_root / "01_ingredients" / "symmap_download_diagnostic.json", diagnostic)
        write_json(out_root / "02_compound_targets" / "symmap_source_audit.json", diagnostic)
        return {"ingredient_rows": [], "target_rows": [], "diagnostic": diagnostic}

    mol_items = _post_symmap_related(herb_rrid, "Mol", cache_dir, diagnostic)
    ingredient_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    mol_records: list[tuple[dict[str, Any], str, str]] = []
    for item in mol_items:
        mol_rrid = _clean(item.get("MOL_id") or item.get("Mol_id") or item.get("SMIT_id"))
        if not mol_rrid:
            continue
        name = _clean(item.get("Molecule_name") or item.get("Molecule_Name"))
        database_ids = unique_preserve(
            [
                f"SymMap:{mol_rrid}",
                f"TCMSP:{_clean(item.get('TCMSP_id'))}" if _clean(item.get("TCMSP_id")) else "",
                f"PubChem:{_clean(item.get('PubChem_CID'))}" if _clean(item.get("PubChem_CID")) else "",
                f"CAS:{_clean(item.get('CAS_id'))}" if _clean(item.get("CAS_id")) else "",
            ]
        )
        ingredient_rows.append(
            {
                "MOL_ID": f"SYMMAP:{mol_rrid}",
                "molecule_ID": mol_rrid,
                "molecule_name": name,
                "ob": _clean(item.get("OB_score")),
                "dl": "",
                "PubChem_ID": _first_pubchem_cid(item.get("PubChem_CID")),
                "CAS": _clean(item.get("CAS_id")),
                "herb_name": diagnostic["matched_herb_name"] or herb_name,
                "source_list": "SymMap2",
                "primary_source": "SymMap2",
                "evidence_types": _clean(item.get("type")) or "symmap_related_component",
                "database_ids": "; ".join(database_ids),
                "activity_filter_basis": "supplemental_source_target_evidence",
                "relation_type": "direct_herb_ingredient",
                "match_type": match["match_status"],
                "confidence_level": "C",
                "source_url": f"{SYM_MAP_BASE}/detail/{herb_rrid}",
                "source_file": str(valid_files.get("ingredients", "")),
                "symmap_original_herb_id": herb_rrid,
                "symmap_original_herb_name": diagnostic["matched_herb_name"] or herb_name,
                "symmap_ingredient_id": mol_rrid,
                "symmap_ingredient_name": name,
                "parser_rule": "SMHB direct Mol relation, then SMIT Gene relation",
            }
        )
        ob_score = _float_or_none(item.get("OB_score"))
        if ob_score is not None and ob_score >= DEFAULT_OB_MIN:
            mol_records.append((item, mol_rrid, name))

    gene_payloads: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_post_symmap_related, mol_rrid, "Gene", cache_dir, diagnostic): mol_rrid
            for _, mol_rrid, _ in mol_records
        }
        for future in as_completed(futures):
            mol_rrid = futures[future]
            try:
                gene_payloads[mol_rrid] = future.result()
            except Exception as exc:  # noqa: BLE001
                diagnostic.setdefault("api_errors", []).append({"rrid": mol_rrid, "table_name": "Gene", "error": str(exc)})

    for _, mol_rrid, _ in mol_records:
        gene_items = gene_payloads.get(mol_rrid, [])
        for gene_item in gene_items:
            gene_symbol = _clean(gene_item.get("Gene_symbol")).upper()
            if not gene_symbol:
                continue
            source_target_ids = unique_preserve(
                [
                    f"SymMap:{_clean(gene_item.get('Gene_id'))}" if _clean(gene_item.get("Gene_id")) else "",
                    f"UniProt:{_clean(gene_item.get('UniProt_id'))}" if _clean(gene_item.get("UniProt_id")) else "",
                    f"TCMSP:{_clean(gene_item.get('TCMSP_id'))}" if _clean(gene_item.get("TCMSP_id")) else "",
                    f"NCBI:{_clean(gene_item.get('NCBI_id'))}" if _clean(gene_item.get("NCBI_id")) else "",
                ]
            )
            target_rows.append(
                {
                    "MOL_ID": f"SYMMAP:{mol_rrid}",
                    "target_name": _clean(gene_item.get("Protein_name")) or _clean(gene_item.get("Gene_name")) or gene_symbol,
                    "target_gene": gene_symbol,
                    "uniprot_id": _clean(gene_item.get("UniProt_id")),
                    # This is a direct database relation, not an independently
                    # reproduced biological validation or identifier mapping.
                    "validated": "false",
                    "SVM_score": _clean(gene_item.get("score")),
                    "RF_score": "",
                    "source_list": "SymMap2",
                    "target_evidence_types": _clean(gene_item.get("type")) or "symmap_related_component",
                    "evidence_class": _evidence_class(
                        _clean(gene_item.get("type")) or "symmap_related_component"
                    ),
                    "mapping_validated": "false",
                    "source_target_ids": "; ".join(source_target_ids),
                    "source": "SymMap2",
                    "relation_type": "direct_ingredient_target",
                    "match_type": match["match_status"],
                    "confidence_level": "C",
                    "source_url": f"{SYM_MAP_BASE}/detail/{mol_rrid}",
                    "source_file": str(valid_files.get("targets", "")),
                    "symmap_original_herb_id": herb_rrid,
                    "symmap_original_herb_name": diagnostic["matched_herb_name"] or herb_name,
                    "symmap_ingredient_id": mol_rrid,
                    "symmap_ingredient_name": next((r[2] for r in mol_records if r[1] == mol_rrid), ""),
                    "symmap_target_id": _clean(gene_item.get("Gene_id")),
                    "parser_rule": "SMIT direct Gene relation for selected herb ingredient",
                }
            )

    diagnostic.update(
        {
            "status": "available" if ingredient_rows and target_rows else "no_relationship_rows",
            "raw_ingredient_count": len(ingredient_rows),
            "target_api_ingredient_count": len(mol_records),
            "target_api_filter": f"OB_score >= {DEFAULT_OB_MIN:g}; rows without OB skipped for active network supplementation",
            "raw_target_relation_count": len(target_rows),
            "total_rows_read": len(mol_items),
            "direct_herb_ingredient_rows": len(ingredient_rows),
            "indirect_rows_excluded": 0,
            "exact_match_rows": len(ingredient_rows) if match["match_status"] == "exact_match" else 0,
            "alias_match_rows": len(ingredient_rows) if match["match_status"] == "alias_match" else 0,
            "fuzzy_match_rows": 0,
            "excluded_reason_counts": {},
            "unique_ingredients_before_dedup": len(ingredient_rows),
            "unique_targets_before_dedup": len({row.get("target_gene") for row in target_rows if row.get("target_gene")}),
            "parser_warnings": [],
            "finished_at": now_utc(),
        }
    )
    write_json(out_root / "01_ingredients" / "symmap_download_diagnostic.json", diagnostic)
    write_json(out_root / "02_compound_targets" / "symmap_source_audit.json", diagnostic)
    return {"ingredient_rows": ingredient_rows, "target_rows": target_rows, "diagnostic": diagnostic}


def _compute_herbiv_confidence(
    combined_score: float,
    *,
    candidate_threshold: float = 900.0,
    exploratory_min_score: float = 700.0,
    exploratory_max_score: float = 899.0,
) -> dict[str, Any]:
    """Compute HerbiV numeric confidence from raw combined_score (0-1000).

    Formula per NPFlow scoring spec:
      normalized_score = min(1.0, combined_score / 1000.0)
      relation_confidence = min(0.90, 0.45 + 0.45 * normalized_score)
      ingredient_confidence = min(0.85, 0.75 + 0.10 * normalized_score)
    """
    if not math.isfinite(combined_score) or not 0.0 <= combined_score <= 1000.0:
        return {
            "normalized_score": 0.0,
            "relation_confidence": 0.0,
            "ingredient_confidence": 0.0,
            "category": "excluded",
            "eligibility_reason": "invalid_combined_score_outside_0_1000",
        }
    normalized = max(0.0, min(1.0, combined_score / 1000.0))
    relation_confidence = round(min(0.90, 0.45 + 0.45 * normalized), 4)
    ingredient_confidence = round(min(0.85, 0.75 + 0.10 * normalized), 4)
    if combined_score >= candidate_threshold:
        category = "candidate"
        eligibility_reason = "meets_herbiv_candidate_threshold"
    elif exploratory_min_score <= combined_score <= exploratory_max_score:
        category = "exploratory"
        eligibility_reason = "exploratory_only_not_main_network"
    else:
        category = "excluded"
        eligibility_reason = "outside_configured_herbiv_eligibility_ranges"
    return {
        "normalized_score": round(normalized, 4),
        "relation_confidence": relation_confidence,
        "ingredient_confidence": ingredient_confidence,
        "category": category,
        "eligibility_reason": eligibility_reason,
    }


def fetch_herbiv_source(
    project_root: Path,
    herb_name: str,
    aliases: list[str],
    out_root: Path,
    *,
    eligibility_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch herb-ingredient-target relations from a local HerbiV CSV file.

    HerbiV is a non-public curated database. Users must provide a local CSV:
      data/herb_sources/herbiv/raw/herbiv_compound_targets.csv

    Required columns: herb_name, ingredient_name, target_gene, combined_score
    Optional columns: pubchem_cid, cas, uniprot_id, target_name, evidence_type

    Scoring:
      - combined_score >= 900  -> candidate (enters main pool)
      - 700 <= combined_score < 900 -> exploratory (marked, does not enter docking ranking)
      - combined_score < 700   -> excluded
      - relation_confidence = min(0.90, 0.45 + 0.45 * score/1000)
      - ingredient_confidence = min(0.85, 0.75 + 0.10 * score/1000)
    """
    policy = eligibility_policy or {}
    candidate_threshold = float(policy.get("min_score_for_candidate", 900))
    exploratory_min = float(policy.get("exploratory_min_score", 700))
    exploratory_max = float(policy.get("exploratory_max_score", 899))
    if not all(math.isfinite(value) for value in (candidate_threshold, exploratory_min, exploratory_max)):
        raise ValueError("HerbiV eligibility thresholds must be finite numbers")
    if exploratory_min > exploratory_max or exploratory_max >= candidate_threshold:
        raise ValueError(
            "HerbiV thresholds must satisfy exploratory_min_score <= exploratory_max_score < min_score_for_candidate"
        )

    source_root = ensure_dir(project_root / "data" / "herb_sources" / "herbiv")
    raw_dir = ensure_dir(source_root / "raw")
    diagnostic: dict[str, Any] = {
        "source": "HerbiV",
        "started_at": now_utc(),
        "input_herb_name": herb_name,
        "status": "unavailable",
        "reason": "no_herbiv_local_file_available",
        "local_file_schema": (
            "Required local CSV: data/herb_sources/herbiv/raw/herbiv_compound_targets.csv "
            "with columns: herb_name, ingredient_name, target_gene, combined_score "
            "and optional: pubchem_cid, cas, uniprot_id, target_name, evidence_type."
        ),
        "scoring": {
            "combined_score_range": "0-1000",
            "candidate_threshold": candidate_threshold,
            "exploratory_min_score": exploratory_min,
            "exploratory_max_score": exploratory_max,
            "relation_confidence_formula": "min(0.90, 0.45 + 0.45 * combined_score/1000)",
            "ingredient_confidence_formula": "min(0.85, 0.75 + 0.10 * combined_score/1000)",
        },
    }

    local_csv = raw_dir / "herbiv_compound_targets.csv"
    ingredient_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    eligibility_rows: list[dict[str, Any]] = []
    if local_csv.exists():
        with local_csv.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        query_norms = {_normalize_text(value) for value in unique_preserve([herb_name, *aliases]) if value}
        matched = [row for row in rows if _normalize_text(row.get("herb_name", "")) in query_norms]
        ingredient_seen: set[str] = set()
        ingredient_by_id: dict[str, dict[str, Any]] = {}
        n_candidate = n_exploratory = n_excluded = 0
        for index, row in enumerate(matched, start=1):
            name = _clean(row.get("ingredient_name") or row.get("compound_name"))
            gene = _clean(row.get("target_gene")).upper()
            if not name or not gene:
                continue
            parsed_score = _float_or_none(row.get("combined_score"))
            raw_score = parsed_score if parsed_score is not None else float("nan")
            conf = _compute_herbiv_confidence(
                raw_score,
                candidate_threshold=candidate_threshold,
                exploratory_min_score=exploratory_min,
                exploratory_max_score=exploratory_max,
            )
            cat = conf["category"]
            raw_evidence_type = _clean(row.get("evidence_type")) or "herbiv_local_file"
            if cat == "candidate":
                n_candidate += 1
            elif cat == "exploratory":
                n_exploratory += 1
            else:
                n_excluded += 1
            eligibility_rows.append(
                {
                    "herb_name": _clean(row.get("herb_name")) or herb_name,
                    "ingredient_name": name,
                    "target_gene": gene,
                    "combined_score": "" if parsed_score is None else parsed_score,
                    "source_eligibility": cat,
                    "source_eligibility_reason": conf["eligibility_reason"],
                    "eligible_for_main_network": "true" if cat == "candidate" else "false",
                }
            )
            # Excluded evidence remains in the audit file but not union tables.
            if cat == "excluded":
                continue
            mol_id = row.get("ingredient_id") or f"HerbiV:{_normalize_text(name).replace(' ', '_')}"
            if mol_id not in ingredient_seen:
                ingredient_record = {
                        "MOL_ID": mol_id,
                        "molecule_name": name,
                        "ob": _clean(row.get("ob")),
                        "dl": _clean(row.get("dl")),
                        "PubChem_ID": _clean(row.get("pubchem_cid")),
                        "CAS": _clean(row.get("cas")),
                        "herb_name": _clean(row.get("herb_name")) or herb_name,
                        "source_list": "HerbiV",
                        "primary_source": "HerbiV",
                        "evidence_types": raw_evidence_type,
                        "database_ids": _clean(row.get("database_ids")) or f"HerbiV:{index}",
                        "activity_filter_basis": "supplemental_source_target_evidence",
                        "relation_type": "direct_herb_ingredient",
                        "match_type": "exact_or_alias_local_file",
                        "confidence_level": "B" if cat == "candidate" else "C",
                        "ingredient_confidence": conf["ingredient_confidence"],
                        "source_url": "",
                        "source_file": str(local_csv),
                        "parser_rule": "local HerbiV compound-target CSV with combined_score weighting",
                        "herbiv_combined_score": raw_score,
                        "herbiv_category": cat,
                        "source_eligibility": cat,
                        "source_eligibility_reason": conf["eligibility_reason"],
                    }
                ingredient_rows.append(ingredient_record)
                ingredient_by_id[str(mol_id)] = ingredient_record
                ingredient_seen.add(mol_id)
            else:
                existing_ingredient = ingredient_by_id.get(str(mol_id))
                if existing_ingredient is not None:
                    _merge_source_eligibility(existing_ingredient, {
                        "source_eligibility": cat,
                        "herbiv_category": cat,
                        "source_eligibility_reason": conf["eligibility_reason"],
                        "herbiv_combined_score": raw_score,
                    })
                    existing_ingredient["ingredient_confidence"] = max(
                        _float_or_none(existing_ingredient.get("ingredient_confidence")) or 0.0,
                        float(conf["ingredient_confidence"]),
                    )
            target_rows.append(
                {
                    "MOL_ID": mol_id,
                    "target_name": _clean(row.get("target_name")) or gene,
                    "target_gene": gene,
                    "uniprot_id": _clean(row.get("uniprot_id")),
                    # A local or computational source row is not automatically
                    # experimental validation. Preserve its raw label below.
                    "validated": "false",
                    "source_list": "HerbiV",
                    "target_evidence_types": raw_evidence_type,
                    "evidence_class": _evidence_class(raw_evidence_type),
                    "mapping_validated": "false",
                    "source_target_ids": _clean(row.get("source_target_ids")),
                    "source": "HerbiV",
                    "relation_type": "direct_ingredient_target",
                    "match_type": "exact_or_alias_local_file",
                    "confidence_level": "B" if cat == "candidate" else "C",
                    "ingredient_confidence": conf["ingredient_confidence"],
                    "relation_confidence": conf["relation_confidence"],
                    "source_url": "",
                    "source_file": str(local_csv),
                    "parser_rule": "local HerbiV compound-target CSV with combined_score weighting",
                    "herbiv_combined_score": raw_score,
                    "herbiv_category": cat,
                    "source_eligibility": cat,
                    "source_eligibility_reason": conf["eligibility_reason"],
                }
            )
        diagnostic.update(
            {
                "status": "available_local_file" if target_rows else "local_file_no_matching_rows",
                "local_file": str(local_csv),
                "raw_ingredient_count": len(ingredient_rows),
                "raw_target_relation_count": len(target_rows),
                "herbiv_candidate_count": n_candidate,
                "herbiv_exploratory_count": n_exploratory,
                "herbiv_excluded_count": n_excluded,
            }
        )
    else:
        diagnostic["status"] = "no_local_file"
        diagnostic["reason"] = (
            "HerbiV is a curated database requiring a local CSV. "
            "Place data at data/herb_sources/herbiv/raw/herbiv_compound_targets.csv "
            "with columns: herb_name, ingredient_name, target_gene, combined_score."
        )
    write_json(out_root / "01_ingredients" / "herbiv_download_diagnostic.json", diagnostic)
    write_rows(
        out_root / "01_ingredients" / "herbiv_eligibility_audit.csv",
        eligibility_rows,
        [
            "herb_name",
            "ingredient_name",
            "target_gene",
            "combined_score",
            "source_eligibility",
            "source_eligibility_reason",
            "eligible_for_main_network",
        ],
    )
    write_json(out_root / "02_compound_targets" / "herbiv_source_audit.json", diagnostic)
    write_json(source_root / "herbiv_manifest.json", diagnostic)
    return {
        "ingredient_rows": ingredient_rows,
        "target_rows": target_rows,
        "eligibility_rows": eligibility_rows,
        "diagnostic": diagnostic,
    }


def _source_set(value: str) -> set[str]:
    return {part.strip() for part in str(value or "").split(";") if part.strip()}


def _join_sources(values: list[str]) -> str:
    parts: set[str] = set()
    for value in values:
        parts.update(_source_set(value))
    return "; ".join(sorted(parts, key=lambda item: SOURCE_PRIORITY.get(item, 99)))


def _ingredient_key(row: dict[str, Any]) -> str:
    pubchem = _clean(row.get("PubChem_ID") or row.get("pubchem_cid"))
    if pubchem:
        return f"pubchem:{pubchem}"
    cas = _clean(row.get("CAS") or row.get("cas"))
    if cas:
        return f"cas:{cas.lower()}"
    name = _normalize_text(row.get("molecule_name") or row.get("ingredient_name"))
    if name:
        return f"name:{name}"
    return f"id:{_clean(row.get('MOL_ID'))}"


def _source_combo(row: dict[str, Any]) -> str:
    sources = _source_set(row.get("source_list", "") or row.get("source", ""))
    return "+".join(sorted(sources, key=lambda item: SOURCE_PRIORITY.get(item, 99))) or "unknown"


def _merge_source_eligibility(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    priority = {"": 0, "excluded": 1, "exploratory": 2, "candidate": 3}
    current = str(existing.get("source_eligibility") or existing.get("herbiv_category") or "").strip().lower()
    candidate = str(incoming.get("source_eligibility") or incoming.get("herbiv_category") or "").strip().lower()
    if priority.get(candidate, 0) > priority.get(current, 0):
        existing["source_eligibility"] = candidate
        existing["herbiv_category"] = candidate
        existing["source_eligibility_reason"] = incoming.get("source_eligibility_reason", "")
        existing["herbiv_combined_score"] = incoming.get("herbiv_combined_score", "")


def _ingredient_confidence_for_row(row: dict[str, Any]) -> float:
    """Compute numeric ingredient confidence (0-1) from source provenance.

    Per-source base weights (overridden by row-level values when present):
      TCMSP_OB_DL_ACTIVE: 1.00
      HerbiV network ingredient: 0.75 (boosted by combined_score up to 0.85)
      SymMap2 linked ingredient: 0.65 (boosted by structure success up to 0.75)
      Multi-source overlap: capped at 1.00
    """
    # If the fetcher already set an explicit ingredient_confidence, use it
    explicit = row.get("ingredient_confidence")
    if explicit not in (None, "", 0):
        return min(1.0, float(explicit))
    sources = _source_set(row.get("source_list", ""))
    confidence = 0.0
    if "TCMSP" in sources:
        confidence = max(confidence, 1.00)
    if "HerbiV" in sources:
        herbiv_conf = row.get("ingredient_confidence") or 0.75
        confidence = max(confidence, min(0.85, float(herbiv_conf)))
    if "SymMap2" in sources:
        confidence = max(confidence, 0.65)
    # Multi-source bonus: +0.05 per additional source
    n_sources = len(sources & {"TCMSP", "HerbiV", "SymMap2"})
    if n_sources >= 2:
        confidence = min(1.0, confidence + 0.05 * (n_sources - 1))
    if confidence == 0.0:
        confidence = 0.50  # unknown source floor
    return round(confidence, 4)


def _relation_confidence_for_row(row: dict[str, Any]) -> float:
    """Compute numeric relation confidence (0-1) for an ingredient-target edge.

    Per-source base weights:
      TCMSP compound-target: 0.90
      HerbiV compound-target: 0.65 (formula-driven, set by fetcher)
      SymMap2 compound-target: 0.60
      Multi-source: +0.10 per corroborating source, capped at 1.00
    """
    explicit = row.get("relation_confidence")
    if explicit not in (None, "", 0):
        return min(1.0, float(explicit))
    sources = _source_set(row.get("source_list", ""))
    confidence = 0.0
    if "TCMSP" in sources:
        confidence = max(confidence, 0.90)
    if "HerbiV" in sources:
        herbiv_rel = row.get("relation_confidence") or 0.65
        confidence = max(confidence, float(herbiv_rel))
    if "SymMap2" in sources:
        confidence = max(confidence, 0.60)
    n_sources = len(sources & {"TCMSP", "HerbiV", "SymMap2"})
    if n_sources >= 2:
        confidence = min(1.0, confidence + 0.10 * (n_sources - 1))
    if confidence == 0.0:
        confidence = 0.50
    return round(confidence, 4)


def _confidence_for_sources(source_list: str) -> str:
    sources = _source_set(source_list)
    if "TCMSP" in sources and ("HerbiV" in sources or "SymMap2" in sources):
        return "A"
    if "TCMSP" in sources:
        return "B"
    if "HerbiV" in sources or "SymMap2" in sources:
        return "C"
    return "D"


def _source_edge_semantics(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve eligibility independently for every source on one raw edge."""
    sources = _source_set(row.get("source_list", "") or row.get("source", ""))
    if not sources:
        return [
            {
                "source": "unknown",
                "eligible": False,
                "status": "excluded",
                "reason": "unknown_source_policy",
            }
        ]

    output: list[dict[str, Any]] = []
    category = str(
        row.get("source_eligibility") or row.get("herbiv_category") or ""
    ).strip().casefold()
    explicit_status = str(row.get("network_eligibility") or "").strip().casefold()
    explicit_reason = _clean(
        row.get("network_eligibility_reason") or row.get("source_eligibility_reason")
    )
    for source in sorted(sources, key=lambda item: SOURCE_PRIORITY.get(item, 99)):
        if source == "TCMSP":
            output.append(
                {
                    "source": source,
                    "eligible": True,
                    "status": "pending_activity_filter",
                    "reason": "eligible_after_tcmsp_ob_dl_and_target_filter",
                }
            )
        elif source == "SymMap2":
            output.append(
                {
                    "source": source,
                    "eligible": True,
                    "status": "eligible",
                    "reason": "eligible_symmap_direct_target_relation",
                }
            )
        elif source == "HerbiV":
            eligible = category == "candidate"
            status = category if category in {"candidate", "exploratory", "excluded"} else "excluded"
            reason = explicit_reason or (
                "meets_herbiv_candidate_threshold"
                if eligible
                else "exploratory_only_not_main_network"
                if status == "exploratory"
                else "herbiv_not_candidate"
            )
            output.append(
                {
                    "source": source,
                    "eligible": eligible,
                    "status": status,
                    "reason": reason,
                }
            )
        else:
            eligible = explicit_status in {
                "true",
                "eligible",
                "candidate",
                "selected",
                "main_network",
                "pending_activity_filter",
            }
            output.append(
                {
                    "source": source,
                    "eligible": eligible,
                    "status": explicit_status or ("eligible" if eligible else "excluded"),
                    "reason": explicit_reason or "explicit_source_edge_policy",
                }
            )
    return output


def _edge_record_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "_source_edge_records"}


def _aggregate_edge_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a canonical edge without leaking ineligible-source confidence."""
    assessments: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in records:
        assessments.extend((record, item) for item in _source_edge_semantics(record))

    eligible_records: list[dict[str, Any]] = []
    eligible_sources: set[str] = set()
    all_sources: set[str] = set()
    statuses_by_source: dict[str, set[str]] = {}
    reasons_by_source: dict[str, set[str]] = {}
    for record, assessment in assessments:
        source = str(assessment["source"])
        all_sources.add(source)
        statuses_by_source.setdefault(source, set()).add(str(assessment["status"]))
        reasons_by_source.setdefault(source, set()).add(str(assessment["reason"]))
        if assessment["eligible"]:
            eligible_sources.add(source)
            if not any(existing is record for existing in eligible_records):
                eligible_records.append(record)

    confidence_records = eligible_records or records
    ingredient_confidence = max(
        (_ingredient_confidence_for_row(record) for record in confidence_records),
        default=0.0,
    )
    relation_confidence = max(
        (_relation_confidence_for_row(record) for record in confidence_records),
        default=0.0,
    )
    all_statuses = {
        status for statuses in statuses_by_source.values() for status in statuses
    }
    if eligible_sources:
        network_eligibility = (
            "pending_activity_filter"
            if eligible_sources == {"TCMSP"}
            else "eligible"
        )
    elif "exploratory" in all_statuses:
        network_eligibility = "exploratory"
    else:
        network_eligibility = "excluded"

    evidence_values = unique_preserve(
        _clean(record.get("target_evidence_types") or record.get("evidence_type"))
        for record in records
    )
    evidence_classes = unique_preserve(
        _clean(record.get("evidence_class"))
        or _evidence_class(record.get("target_evidence_types") or record.get("evidence_type"))
        for record in records
    )
    status_text = "; ".join(
        f"{source}={'/'.join(sorted(statuses_by_source[source]))}"
        for source in sorted(statuses_by_source, key=lambda item: SOURCE_PRIORITY.get(item, 99))
    )
    reason_text = "; ".join(
        f"{source}={'/'.join(sorted(reasons_by_source[source]))}"
        for source in sorted(reasons_by_source, key=lambda item: SOURCE_PRIORITY.get(item, 99))
    )
    all_source_list = _join_sources(list(all_sources))
    eligible_source_list = _join_sources(list(eligible_sources))
    return {
        "source_list": all_source_list,
        "source": all_source_list,
        "all_source_list": all_source_list,
        "eligible_source_list": eligible_source_list,
        "target_evidence_types": "; ".join(evidence_values),
        "evidence_class": "; ".join(evidence_classes),
        "mapping_validated": "true"
        if any(_strict_true(record.get("mapping_validated")) for record in records)
        else "false",
        "validated": "true"
        if any(_strict_true(record.get("validated")) for record in records)
        else "false",
        "confidence_level": _confidence_for_sources(
            eligible_source_list or all_source_list
        ),
        "ingredient_confidence": round(ingredient_confidence, 4),
        "relation_confidence": round(relation_confidence, 4),
        "network_eligibility": network_eligibility,
        "network_eligibility_reason": reason_text,
        "eligibility_status_by_source": status_text,
        "eligibility_reason_by_source": reason_text,
        "source_eligibility": network_eligibility,
        "source_eligibility_reason": reason_text,
    }


def _ensure_sqlite_schema(path: Path) -> sqlite3.Connection:
    ensure_dir(path.parent)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingredients (
          canonical_ingredient_id TEXT PRIMARY KEY,
          ingredient_name TEXT,
          normalized_name TEXT,
          alias_list TEXT,
          pubchem_cid TEXT,
          cas TEXT,
          source_list TEXT,
          source_ids TEXT,
          evidence_types TEXT,
          confidence_level TEXT,
          ingredient_confidence REAL,
          source_eligibility TEXT,
          source_eligibility_reason TEXT,
          herbiv_combined_score REAL,
          relation_type TEXT,
          match_type TEXT,
          herb_name TEXT,
          ob TEXT,
          dl TEXT
        );
        CREATE TABLE IF NOT EXISTS targets (
          canonical_target_id TEXT PRIMARY KEY,
          target_gene TEXT,
          target_name TEXT,
          uniprot_id TEXT,
          organism TEXT,
          organism_taxid TEXT,
          source_list TEXT,
          source_target_ids TEXT,
          evidence_types TEXT,
          evidence_class TEXT,
          mapping_validated TEXT,
          confidence_level TEXT,
          target_confidence REAL
        );
        CREATE TABLE IF NOT EXISTS ingredient_target_edges (
          canonical_ingredient_id TEXT,
          canonical_target_id TEXT,
          ingredient_id TEXT,
          ingredient_name TEXT,
          target_gene TEXT,
          source_list TEXT,
          eligible_source_list TEXT,
          all_source_list TEXT,
          source_edge_ids TEXT,
          relation_type TEXT,
          evidence_types TEXT,
          evidence_class TEXT,
          mapping_validated TEXT,
          confidence_level TEXT,
          ingredient_confidence REAL,
          relation_confidence REAL,
          network_eligibility TEXT,
          network_eligibility_reason TEXT,
          eligibility_status_by_source TEXT,
          eligibility_reason_by_source TEXT,
          is_predicted TEXT,
          is_experimental TEXT,
          is_literature_curated TEXT,
          source_url TEXT,
          PRIMARY KEY (canonical_ingredient_id, canonical_target_id)
        );
        CREATE TABLE IF NOT EXISTS source_provenance (
          source_database TEXT PRIMARY KEY,
          source_version TEXT,
          download_url TEXT,
          downloaded_at TEXT,
          sha256 TEXT,
          file_size TEXT,
          row_counts TEXT,
          parser_name TEXT,
          parser_version TEXT,
          parse_status TEXT,
          parse_warnings TEXT,
          citation TEXT
        );
        """
    )
    migrations = {
        "ingredients": {
            "source_eligibility": "TEXT",
            "source_eligibility_reason": "TEXT",
            "herbiv_combined_score": "REAL",
        },
        "ingredient_target_edges": {
            "network_eligibility": "TEXT",
            "network_eligibility_reason": "TEXT",
            "eligible_source_list": "TEXT",
            "all_source_list": "TEXT",
            "evidence_class": "TEXT",
            "mapping_validated": "TEXT",
            "eligibility_status_by_source": "TEXT",
            "eligibility_reason_by_source": "TEXT",
        },
        "targets": {
            "evidence_class": "TEXT",
            "mapping_validated": "TEXT",
        },
    }
    for table, columns in migrations.items():
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return conn


def merge_herb_source_rows(
    tcmsp_ingredients: list[dict[str, Any]],
    tcmsp_targets: list[dict[str, Any]],
    supplemental_results: list[dict[str, Any]],
    *,
    tcmsp_source_name: str,
    out_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    all_ingredients: list[dict[str, Any]] = []
    all_targets: list[dict[str, Any]] = []
    source_rows = [
        {
            "source": "TCMSP",
            "status": "available" if tcmsp_ingredients and tcmsp_targets else "unavailable",
            "raw_ingredients": len(tcmsp_ingredients),
            "raw_target_relations": len(tcmsp_targets),
            "main_network_eligible_target_relations": len(tcmsp_targets),
            "source_detail": tcmsp_source_name or "unavailable",
        }
    ]

    for row in tcmsp_ingredients:
        item = dict(row)
        item.setdefault("source_list", "TCMSP")
        item.setdefault("primary_source", "TCMSP")
        item.setdefault("evidence_types", "TCMSP_OB_DL_target")
        item.setdefault("database_ids", item.get("MOL_ID", ""))
        item.setdefault("activity_filter_basis", "TCMSP_OB_DL")
        item.setdefault("relation_type", "direct_herb_ingredient")
        item.setdefault("match_type", "tcmsp_herb_match")
        item.setdefault("confidence_level", "B")
        item.setdefault("source_url", "")
        item.setdefault("source_file", tcmsp_source_name)
        all_ingredients.append(item)
    for row in tcmsp_targets:
        item = dict(row)
        item.setdefault("source_list", "TCMSP")
        item.setdefault("target_evidence_types", item.get("evidence_type", "TCMSP_target"))
        item.setdefault("source_target_ids", item.get("target_ID", "") or item.get("uniprot_id", ""))
        item.setdefault("source", "TCMSP")
        item.setdefault("relation_type", "direct_ingredient_target")
        item.setdefault("match_type", "tcmsp_herb_match")
        item.setdefault("confidence_level", "B")
        item.setdefault("source_url", "")
        item.setdefault("source_file", tcmsp_source_name)
        all_targets.append(item)

    for result in supplemental_results:
        diagnostic = result.get("diagnostic", {})
        source = diagnostic.get("source", "unknown")
        ing = result.get("ingredient_rows", [])
        tar = result.get("target_rows", [])
        eligible_target_relations = sum(
            1
            for target_row in tar
            if any(
                assessment["eligible"]
                for assessment in _source_edge_semantics(target_row)
            )
        )
        source_rows.append(
            {
                "source": source,
                "status": diagnostic.get("status", "unknown"),
                "raw_ingredients": len(ing),
                "raw_target_relations": len(tar),
                "main_network_eligible_target_relations": eligible_target_relations,
                "source_detail": diagnostic.get("reason", "") or diagnostic.get("match_status", ""),
                "diagnostic_file": "01_ingredients/herbiv_download_diagnostic.json"
                if source == "HerbiV"
                else "01_ingredients/symmap_download_diagnostic.json"
                if source == "SymMap2"
                else "",
            }
        )
        all_ingredients.extend(ing)
        all_targets.extend(tar)

    grouped: dict[str, dict[str, Any]] = {}
    mol_id_remap: dict[str, str] = {}
    for row in all_ingredients:
        key = _ingredient_key(row)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = dict(row)
            mol_id_remap[_clean(row.get("MOL_ID"))] = _clean(row.get("MOL_ID"))
            continue
        old_mol_id = _clean(row.get("MOL_ID"))
        new_mol_id = _clean(existing.get("MOL_ID"))
        mol_id_remap[old_mol_id] = new_mol_id
        existing["source_list"] = _join_sources([existing.get("source_list", ""), row.get("source_list", "")])
        existing["evidence_types"] = "; ".join(
            unique_preserve([*str(existing.get("evidence_types", "")).split("; "), *str(row.get("evidence_types", "")).split("; ")])
        )
        existing["database_ids"] = "; ".join(
            unique_preserve([*str(existing.get("database_ids", "")).split("; "), *str(row.get("database_ids", "")).split("; ")])
        )
        existing["activity_filter_basis"] = "; ".join(
            unique_preserve([existing.get("activity_filter_basis", ""), row.get("activity_filter_basis", "")])
        )
        for field in ("relation_type", "match_type", "source_url", "source_file"):
            existing[field] = "; ".join(unique_preserve([existing.get(field, ""), row.get(field, "")]))
        existing["confidence_level"] = _confidence_for_sources(existing.get("source_list", ""))
        _merge_source_eligibility(existing, row)
        # Propagate numeric ingredient_confidence (max across sources)
        existing_ic = _ingredient_confidence_for_row(existing)
        row_ic = _ingredient_confidence_for_row(row)
        existing["ingredient_confidence"] = round(max(existing_ic, row_ic), 4)
        for field in ("PubChem_ID", "CAS", "ob", "dl"):
            if not existing.get(field) and row.get(field):
                existing[field] = row.get(field)

    merged_ingredients = list(grouped.values())
    target_grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in all_targets:
        item = dict(row)
        original_mol_id = _clean(item.get("MOL_ID"))
        item["MOL_ID"] = mol_id_remap.get(original_mol_id, original_mol_id)
        item["_source_edge_records"] = [_edge_record_snapshot(item)]
        gene = _clean(item.get("target_gene")).upper()
        target_key = gene or _normalize_text(item.get("target_name"))
        key = (item["MOL_ID"], target_key)
        existing = target_grouped.get(key)
        if existing is None:
            target_grouped[key] = item
            continue
        existing.setdefault("_source_edge_records", []).extend(
            item.get("_source_edge_records", [])
        )
        existing["source_list"] = _join_sources([existing.get("source_list", ""), item.get("source_list", "")])
        existing["source"] = _join_sources([existing.get("source", ""), item.get("source", "")])
        existing["target_evidence_types"] = "; ".join(
            unique_preserve(
                [
                    *str(existing.get("target_evidence_types", "")).split("; "),
                    *str(item.get("target_evidence_types", "")).split("; "),
                ]
            )
        )
        existing["source_target_ids"] = "; ".join(
            unique_preserve([*str(existing.get("source_target_ids", "")).split("; "), *str(item.get("source_target_ids", "")).split("; ")])
        )
        if not existing.get("uniprot_id") and item.get("uniprot_id"):
            existing["uniprot_id"] = item.get("uniprot_id")
        for field in ("relation_type", "match_type", "source_url", "source_file"):
            existing[field] = "; ".join(unique_preserve([existing.get(field, ""), item.get(field, "")]))
        existing["confidence_level"] = _confidence_for_sources(existing.get("source_list", ""))
        _merge_source_eligibility(existing, item)

    merged_targets = list(target_grouped.values())
    for row in merged_targets:
        records = row.get("_source_edge_records") or [_edge_record_snapshot(row)]
        row.update(_aggregate_edge_records(records))

    ingredient_lookup: dict[str, dict[str, Any]] = {}
    union_ingredients: list[dict[str, Any]] = []
    for row in merged_ingredients:
        key = _ingredient_key(row)
        source_list = row.get("source_list", "") or row.get("source", "")
        union = {
            "canonical_ingredient_id": _stable_id("ing", key),
            "ingredient_name": _clean(row.get("molecule_name") or row.get("ingredient_name")),
            "normalized_name": _normalize_text(row.get("molecule_name") or row.get("ingredient_name")),
            "alias_list": row.get("alias_list", ""),
            "pubchem_cid": _first_pubchem_cid(row.get("PubChem_ID") or row.get("pubchem_cid")),
            "cas": _clean(row.get("CAS") or row.get("cas")),
            "source_list": source_list,
            "source_ids": row.get("database_ids", "") or row.get("MOL_ID", ""),
            "evidence_types": row.get("evidence_types", ""),
            "confidence_level": row.get("confidence_level") or _confidence_for_sources(source_list),
            "ingredient_confidence": _ingredient_confidence_for_row(row),
            "source_eligibility": row.get("source_eligibility", "") or row.get("herbiv_category", ""),
            "source_eligibility_reason": row.get("source_eligibility_reason", ""),
            "herbiv_combined_score": row.get("herbiv_combined_score", ""),
            "relation_type": row.get("relation_type", "direct_herb_ingredient"),
            "match_type": row.get("match_type", ""),
            "herb_name": row.get("herb_name", ""),
            "ob": row.get("ob", ""),
            "dl": row.get("dl", ""),
        }
        ingredient_lookup[_clean(row.get("MOL_ID"))] = union
        union_ingredients.append(union)

    target_by_gene: dict[str, dict[str, Any]] = {}
    for row in merged_targets:
        gene = _clean(row.get("target_gene")).upper()
        if not gene:
            continue
        source_list = row.get("source_list", "") or row.get("source", "")
        target = target_by_gene.setdefault(
            gene,
            {
                "canonical_target_id": _stable_id("target", gene),
                "target_gene": gene,
                "target_name": row.get("target_name", "") or gene,
                "uniprot_id": row.get("uniprot_id", ""),
                "organism": "Homo sapiens",
                "organism_taxid": "9606",
                "source_list": "",
                "source_target_ids": "",
                "evidence_types": "",
                "evidence_class": "",
                "mapping_validated": "false",
                "confidence_level": "",
                "target_confidence": 0.0,
            },
        )
        target["source_list"] = _join_sources([target.get("source_list", ""), source_list])
        target["source_target_ids"] = "; ".join(
            unique_preserve(_split_joined(target.get("source_target_ids", "")) + _split_joined(row.get("source_target_ids", "")))
        )
        target["evidence_types"] = "; ".join(
            unique_preserve(_split_joined(target.get("evidence_types", "")) + _split_joined(row.get("target_evidence_types", "") or row.get("evidence_type", "")))
        )
        target["evidence_class"] = "; ".join(
            unique_preserve(
                _split_joined(target.get("evidence_class", ""))
                + _split_joined(row.get("evidence_class", ""))
            )
        )
        target["mapping_validated"] = (
            "true"
            if _strict_true(target.get("mapping_validated"))
            or _strict_true(row.get("mapping_validated"))
            else "false"
        )
        target["confidence_level"] = _confidence_for_sources(target["source_list"])
        # Target confidence is driven only by an edge that may enter the main
        # network. Exploratory rows remain visible in the union audit.
        if row.get("network_eligibility") in {
            "eligible",
            "candidate",
            "pending_activity_filter",
        }:
            existing_tc = target.get("target_confidence", 0.0) or 0.0
            row_tc = _float_or_none(row.get("relation_confidence")) or 0.0
            target["target_confidence"] = round(max(existing_tc, row_tc), 4)
        if not target.get("uniprot_id") and row.get("uniprot_id"):
            target["uniprot_id"] = row.get("uniprot_id")

    union_targets = list(target_by_gene.values())
    union_edges: list[dict[str, Any]] = []
    edge_seen: set[tuple[str, str]] = set()
    for row in merged_targets:
        ingredient = ingredient_lookup.get(_clean(row.get("MOL_ID")))
        gene = _clean(row.get("target_gene")).upper()
        target = target_by_gene.get(gene)
        if not ingredient or not target:
            continue
        key = (ingredient["canonical_ingredient_id"], target["canonical_target_id"])
        if key in edge_seen:
            continue
        edge_seen.add(key)
        source_list = row.get("source_list", "") or row.get("source", "")
        evidence = row.get("target_evidence_types", "") or row.get("evidence_type", "")
        evidence_class = row.get("evidence_class", "") or _evidence_class(evidence)
        union_edges.append(
            {
                "canonical_ingredient_id": ingredient["canonical_ingredient_id"],
                "canonical_target_id": target["canonical_target_id"],
                "ingredient_id": row.get("MOL_ID", ""),
                "ingredient_name": ingredient["ingredient_name"],
                "target_gene": gene,
                "source_list": source_list,
                "eligible_source_list": row.get("eligible_source_list", ""),
                "all_source_list": row.get("all_source_list", source_list),
                "source_edge_ids": row.get("source_target_ids", ""),
                "relation_type": row.get("relation_type", "direct_ingredient_target"),
                "evidence_types": evidence,
                "evidence_class": evidence_class,
                "mapping_validated": row.get("mapping_validated", "false"),
                "confidence_level": row.get("confidence_level") or _confidence_for_sources(source_list),
                # Confidence comes from the edge's eligible source records,
                # never the cross-source merged ingredient maximum.
                "ingredient_confidence": row.get("ingredient_confidence", 0.0),
                "relation_confidence": row.get("relation_confidence", 0.0),
                "network_eligibility": row.get("network_eligibility", "excluded"),
                "network_eligibility_reason": row.get("network_eligibility_reason", ""),
                "eligibility_status_by_source": row.get("eligibility_status_by_source", ""),
                "eligibility_reason_by_source": row.get("eligibility_reason_by_source", ""),
                "is_predicted": "true"
                if "predict" in evidence.lower()
                or "computational_prediction" in evidence_class
                else "false",
                "is_experimental": "true"
                if "experimental" in evidence.lower()
                or "experimental_source_label" in evidence_class
                else "false",
                "is_literature_curated": "true"
                if any(token in evidence.lower() for token in ("curated", "literature"))
                or "literature_curated_source_label" in evidence_class
                else "false",
                "source_url": row.get("source_url", ""),
            }
        )

    union_ingredients.sort(key=lambda item: (item["source_list"], item["ingredient_name"]))
    union_targets.sort(key=lambda item: item["target_gene"])
    union_edges.sort(key=lambda item: (item["target_gene"], item["ingredient_name"]))
    eligible_union_edges = [
        row
        for row in union_edges
        if row.get("network_eligibility")
        in {"eligible", "candidate", "pending_activity_filter"}
    ]
    ingredient_dir = ensure_dir(out_root / "01_ingredients")
    target_dir = ensure_dir(out_root / "02_compound_targets")
    write_rows(ingredient_dir / "union_ingredients.csv", union_ingredients, UNION_INGREDIENT_FIELDS)
    write_rows(target_dir / "union_targets.csv", union_targets, UNION_TARGET_FIELDS)
    write_rows(target_dir / "union_ingredient_target_edges.csv", union_edges, UNION_EDGE_FIELDS)

    sqlite_path = Path.cwd() / "data" / "herb_sources" / "sqlite" / "herb_sources.sqlite"
    conn = _ensure_sqlite_schema(sqlite_path)
    try:
        conn.execute("DELETE FROM ingredients")
        conn.execute("DELETE FROM targets")
        conn.execute("DELETE FROM ingredient_target_edges")
        conn.executemany(
            f"INSERT OR REPLACE INTO ingredients ({','.join(UNION_INGREDIENT_FIELDS)}) VALUES ({','.join(['?'] * len(UNION_INGREDIENT_FIELDS))})",
            [[row.get(field, "") for field in UNION_INGREDIENT_FIELDS] for row in union_ingredients],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO targets ({','.join(UNION_TARGET_FIELDS)}) VALUES ({','.join(['?'] * len(UNION_TARGET_FIELDS))})",
            [[row.get(field, "") for field in UNION_TARGET_FIELDS] for row in union_targets],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO ingredient_target_edges ({','.join(UNION_EDGE_FIELDS)}) VALUES ({','.join(['?'] * len(UNION_EDGE_FIELDS))})",
            [[row.get(field, "") for field in UNION_EDGE_FIELDS] for row in union_edges],
        )
        conn.commit()
    finally:
        conn.close()

    source_combo_counts: dict[str, int] = {}
    for row in union_ingredients:
        combo = _source_combo(row)
        source_combo_counts[combo] = source_combo_counts.get(combo, 0) + 1
    tcmsp_only = source_combo_counts.get("TCMSP", 0)
    herbiv_only = source_combo_counts.get("HerbiV", 0)
    symmap_only = source_combo_counts.get("SymMap2", 0)

    summary = {
        "generated_at": now_utc(),
        "status": "passed" if eligible_union_edges else "warning",
        "allowed_herb_compound_target_sources": ["TCMSP", "HerbiV", "SymMap2"],
        "tcmsp_role": "primary_source_live_or_snapshot",
        "supplement_sources": ["HerbiV", "SymMap2"],
        "source_coverage_status": "covered" if eligible_union_edges else "no_valid_source_coverage",
        "failure_reason": ""
        if eligible_union_edges
        else "no_main_network_eligible_herb_compound_target_edge_across_allowed_sources",
        "source_rows": source_rows,
        "raw_ingredient_records_before_merge": len(all_ingredients),
        "raw_target_relations_before_merge": len(all_targets),
        "unique_ingredient_records_after_merge": len(merged_ingredients),
        "unique_target_relations_after_merge": len(merged_targets),
        "union_unique_ingredients": len(union_ingredients),
        "union_unique_targets": len(union_targets),
        "union_unique_ingredient_target_edges": len(union_edges),
        "union_main_network_eligible_edges": len(eligible_union_edges),
        "union_exploratory_or_excluded_edges": len(union_edges) - len(eligible_union_edges),
        "TCMSP-only ingredients": tcmsp_only,
        "HerbiV-only ingredients": herbiv_only,
        "SymMap2-only ingredients": symmap_only,
        "shared_TCMSP_HerbiV ingredients": source_combo_counts.get("TCMSP+HerbiV", 0),
        "shared_TCMSP_SymMap2 ingredients": source_combo_counts.get("TCMSP+SymMap2", 0),
        "shared_HerbiV_SymMap2 ingredients": source_combo_counts.get("HerbiV+SymMap2", 0),
        "shared_all_three ingredients": source_combo_counts.get("TCMSP+HerbiV+SymMap2", 0),
        "source_combo_counts": source_combo_counts,
        "duplicate_ingredients_removed": max(len(all_ingredients) - len(merged_ingredients), 0),
        "duplicate_targets_removed": max(len(merged_targets) - len(union_targets), 0),
        "duplicate_edges_removed": max(len(all_targets) - len(union_edges), 0),
        "excluded_ambiguous_ingredients": 0,
        "excluded_non_human_targets": 0,
        "excluded_indirect_relations": 0,
        "union_files": [
            str(ingredient_dir / "union_ingredients.csv"),
            str(target_dir / "union_targets.csv"),
            str(target_dir / "union_ingredient_target_edges.csv"),
        ],
        "sqlite_path": str(sqlite_path),
        "supplemented_by_backup_sources": any(
            row.get("source") in {"HerbiV", "SymMap2"}
            and int(row.get("main_network_eligible_target_relations") or 0) > 0
            for row in source_rows
        ),
    }
    reliability_rows = []
    for row in source_rows:
        source = row.get("source", "")
        available = int(row.get("raw_target_relations") or 0) > 0
        used_in_main_network = int(
            row.get("main_network_eligible_target_relations") or 0
        ) > 0
        if source == "TCMSP":
            grade = "B" if "snapshot" in tcmsp_source_name else "A"
            reproducibility = "local_snapshot" if "snapshot" in tcmsp_source_name else "live_web_only"
            feasibility = "stable" if "snapshot" in tcmsp_source_name else "usable_with_cache"
            evidence_level = "mixed"
        elif source == "SymMap2" and available:
            grade = "C"
            reproducibility = "official_download_plus_live_relation_api"
            feasibility = "usable_with_cache"
            evidence_level = "database_relation_labels_preserved_not_independently_validated"
        elif source == "HerbiV" and available:
            grade = "C"
            reproducibility = "local_structured_file"
            feasibility = "usable_with_cache"
            evidence_level = "local_source_labels_preserved_not_independently_validated"
        else:
            grade = "D"
            reproducibility = "diagnostic_only"
            feasibility = "diagnostic_only"
            evidence_level = "unknown"
        reliability_rows.append(
            {
                "source_database": source,
                "source_availability": "available" if available else "partially_available" if source == "HerbiV" else "unavailable",
                "source_reproducibility": reproducibility,
                "source_relation_quality": "direct_relation" if available else "unknown_relation",
                "source_evidence_level": evidence_level,
                "source_confidence_grade": grade,
                "source_feasibility_for_workflow": feasibility,
                "contributed_ingredients": row.get("raw_ingredients", 0),
                "raw_target_relations": row.get("raw_target_relations", 0),
                "contributed_target_relations": row.get(
                    "main_network_eligible_target_relations", 0
                ),
                "used_in_main_network": used_in_main_network,
                "diagnostic_file": row.get("diagnostic_file", ""),
            }
        )
    reliability_audit = {
        "generated_at": now_utc(),
        "status": "passed" if eligible_union_edges else "warning",
        "source_reliability": reliability_rows,
        "source_confidence_grade": "B" if any(row["source_confidence_grade"] == "B" for row in reliability_rows) else "C",
        "sources_used": [row["source_database"] for row in reliability_rows if int(row.get("contributed_target_relations") or 0) > 0],
        "sources_diagnostic_only": [row["source_database"] for row in reliability_rows if int(row.get("contributed_target_relations") or 0) == 0],
    }
    write_json(target_dir / "source_reliability_audit.json", reliability_audit)
    write_json(
        target_dir / "supplemental_source_audit.json",
        {
            "generated_at": now_utc(),
            "status": "passed",
            "supplemental_sources": [row for row in source_rows if row.get("source") in {"HerbiV", "SymMap2"}],
            "symmap_direct_relation_enforced": True,
            "herbiv_requires_structured_relations": True,
        },
    )
    write_json(target_dir / "source_merge_summary.json", summary)
    return merged_ingredients, merged_targets, summary


def fetch_supplemental_herb_sources(
    project_root: Path,
    herb_name: str,
    aliases: list[str],
    out_root: Path,
    *,
    source_weights: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    herbiv_policy = (source_weights or {}).get("herbiv", {})
    return [
        fetch_herbiv_source(
            project_root,
            herb_name,
            aliases,
            out_root,
            eligibility_policy=herbiv_policy,
        ),
        fetch_symmap2_source(project_root, herb_name, aliases, out_root),
    ]

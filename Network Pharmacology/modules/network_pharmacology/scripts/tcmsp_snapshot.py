#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from modules.network_pharmacology.scripts.common import ensure_dir, now_utc, read_rows, write_json


SNAPSHOT_RELATIVE_PATH = Path("data") / "tcmsp" / "tcmsp.sqlite"
MANIFEST_RELATIVE_PATH = Path("data") / "tcmsp" / "snapshot_manifest.json"


def snapshot_db_path(project_root: Path) -> Path:
    return project_root / SNAPSHOT_RELATIVE_PATH


def snapshot_manifest_path(project_root: Path) -> Path:
    return project_root / MANIFEST_RELATIVE_PATH


def normalize_lookup(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\u00a0\s_-]+", "", text)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def split_aliases(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[|;,]", value) if item.strip()]


def clean_snapshot_value(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    return text


def read_manifest(project_root: Path) -> dict:
    path = snapshot_manifest_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def snapshot_available(project_root: Path) -> bool:
    path = snapshot_db_path(project_root)
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with sqlite3.connect(path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
        return {
            "herbs",
            "molecules",
            "targets",
            "herb_compound_relationships",
            "compound_target_relationships",
        }.issubset(tables)
    except sqlite3.Error:
        return False


def herb_query_aliases(herb_name: str, aliases: list[str] | None = None) -> list[str]:
    values = [herb_name]
    values.extend(aliases or [])
    normalized = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        key = normalize_lookup(value)
        if key and key not in seen:
            seen.add(key)
            normalized.append(value)
    return normalized


def herb_match_metadata(
    *,
    herb_name: str,
    candidate: dict,
    field: str,
    score: float,
    match_type: str,
    queries: list[str],
) -> dict:
    matched_name = (
        candidate.get("herb_cn_name")
        or candidate.get("herb_pinyin")
        or candidate.get("herb_en_name")
        or candidate.get("herb_latin_name")
        or ""
    )
    input_norm = normalize_lookup(herb_name)
    matched_norm = normalize_lookup(matched_name)
    requires_confirmation = match_type == "fuzzy_match" and bool(input_norm and matched_norm and input_norm != matched_norm)
    status = {
        "exact_match": "exact",
        "alias_match": "alias",
        "fuzzy_match": "fuzzy_needs_confirmation" if requires_confirmation else "fuzzy",
        "no_match": "no_match",
    }.get(match_type, "unknown")
    warning = ""
    if requires_confirmation:
        warning = (
            f"Input herb '{herb_name}' was not exactly found in the TCMSP snapshot; "
            f"snapshot herb '{matched_name}' was used by fuzzy match (score={score:.2f}) "
            "and requires manual confirmation before biological interpretation."
        )
    return {
        "input_herb_name": herb_name,
        "matched_herb_name": matched_name,
        "herb_match_type": match_type,
        "herb_match_field": field,
        "herb_match_score": score,
        "herb_exact_match": match_type == "exact_match",
        "herb_alias_match": match_type == "alias_match",
        "herb_fuzzy_match_used": match_type == "fuzzy_match",
        "herb_match_requires_user_confirmation": requires_confirmation,
        "herb_match_warning": warning,
        "herb_match_status": status,
        "herb_match_queries": queries,
    }


def resolve_herb(
    conn: sqlite3.Connection,
    herb_name: str,
    aliases: list[str] | None = None,
) -> dict | None:
    queries = herb_query_aliases(herb_name, aliases)
    query_norms = {normalize_lookup(value) for value in queries}
    rows = conn.execute(
        """
        select herb_id, herb_cn_name, herb_pinyin, herb_en_name, herb_latin_name, aliases, source
        from herbs
        """
    ).fetchall()
    best: dict | None = None
    best_score = -1.0
    for row in rows:
        candidate = {
            "herb_id": clean_snapshot_value(row[0]),
            "herb_cn_name": clean_snapshot_value(row[1]),
            "herb_pinyin": clean_snapshot_value(row[2]),
            "herb_en_name": clean_snapshot_value(row[3]),
            "herb_latin_name": clean_snapshot_value(row[4]),
            "aliases": clean_snapshot_value(row[5]),
            "source": clean_snapshot_value(row[6]),
        }
        fields = {
            "herb_cn_name": candidate["herb_cn_name"],
            "herb_pinyin": candidate["herb_pinyin"],
            "herb_en_name": candidate["herb_en_name"],
            "herb_latin_name": candidate["herb_latin_name"],
        }
        alias_values = split_aliases(candidate["aliases"])
        scored_fields: list[tuple[str, str, float]] = []
        for field, value in fields.items():
            scored_fields.append((field, value, 1.0))
        for value in alias_values:
            scored_fields.append(("aliases", value, 0.98))
        for field, value, score in scored_fields:
            norm = normalize_lookup(value)
            if not norm:
                continue
            if norm in query_norms:
                if score > best_score:
                    best_score = score
                    match_type = "alias_match" if field == "aliases" else "exact_match"
                    best = {
                        **candidate,
                        **herb_match_metadata(
                            herb_name=herb_name,
                            candidate=candidate,
                            field=field,
                            score=score,
                            match_type=match_type,
                            queries=queries,
                        ),
                    }
            elif any(norm and (norm in q or q in norm) for q in query_norms):
                score = min(score, 0.82)
                if score > best_score:
                    best_score = score
                    best = {
                        **candidate,
                        **herb_match_metadata(
                            herb_name=herb_name,
                            candidate=candidate,
                            field=field,
                            score=score,
                            match_type="fuzzy_match",
                            queries=queries,
                        ),
                    }
    return best


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [item[0] for item in cursor.description]
    return [{column: clean_snapshot_value(value) for column, value in zip(columns, row)} for row in cursor.fetchall()]


def load_snapshot_herb_data(
    project_root: Path,
    herb_name: str,
    aliases: list[str] | None = None,
    seed_cache_dir: Path | None = None,
) -> dict | None:
    if not snapshot_available(project_root):
        return None

    db_path = snapshot_db_path(project_root)
    manifest = read_manifest(project_root)
    with sqlite3.connect(db_path) as conn:
        herb = resolve_herb(conn, herb_name, aliases)
        if not herb:
            return None
        ingredient_rows = _rows_to_dicts(
            conn.execute(
                """
                select
                  m.molecule_id as MOL_ID,
                  m.molecule_numeric_id as molecule_ID,
                  m.molecule_name as molecule_name,
                  m.ob as ob,
                  m.dl as dl,
                  m.pubchem_cid as PubChem_ID,
                  h.herb_cn_name as herb_name
                from herb_compound_relationships hcr
                join herbs h on h.herb_id = hcr.herb_id
                join molecules m on m.molecule_id = hcr.molecule_id
                where hcr.herb_id = ?
                order by m.molecule_id
                """,
                (herb["herb_id"],),
            )
        )
        target_rows = _rows_to_dicts(
            conn.execute(
                """
                select distinct
                  ctr.molecule_id as MOL_ID,
                  coalesce(t.target_name, ctr.target_name, t.gene_symbol) as target_name,
                  coalesce(t.gene_symbol, ctr.target_gene) as target_gene,
                  t.uniprot_id as uniprot_id,
                  ctr.evidence_type as validated,
                  ctr.score as SVM_score,
                  '' as RF_score,
                  ? as herb_name
                from herb_compound_relationships hcr
                join compound_target_relationships ctr on ctr.molecule_id = hcr.molecule_id
                left join targets t on t.target_id = ctr.target_id
                where hcr.herb_id = ?
                  and coalesce(t.gene_symbol, ctr.target_gene, '') != ''
                order by ctr.molecule_id, target_gene, target_name
                """,
                (herb["herb_cn_name"] or herb_name, herb["herb_id"]),
            )
        )
    if not ingredient_rows or not target_rows:
        return None

    source_type = clean_snapshot_value(manifest.get("source_type", "unknown_snapshot"))
    source_files = manifest.get("source_files", [])
    source_urls = manifest.get("source_urls", [])
    is_official = source_type.startswith("tcmsp_official")
    completeness_warning = ""
    if not is_official:
        completeness_warning = (
            "Using local TCMSP-compatible snapshot built from a traced secondary package source; "
            "this is reproducible and far more complete than seed cache, but it is not the live TCMSP herb page."
        )

    result = {
        "source": "tcmsp_full_snapshot",
        "source_detail": source_type,
        "official_download_method": "local_sqlite_snapshot",
        "html": "",
        "ingredient_rows": ingredient_rows,
        "target_rows": target_rows,
        "path": str(db_path),
        "download_file_paths": [str(path) for path in source_files],
        "source_urls": source_urls,
        "snapshot_manifest_path": str(snapshot_manifest_path(project_root)),
        "snapshot_db_path": str(db_path),
        "snapshot_source_type": source_type,
        "snapshot_row_counts": manifest.get("row_counts", {}),
        "is_full_snapshot": True,
        "is_seed_cache": False,
        "is_official_download_files": is_official,
        "source_completeness_level": "full_snapshot",
        "completeness_warning": completeness_warning,
        "herb_match_field": herb.get("herb_match_field", ""),
        "herb_match_score": herb.get("herb_match_score", ""),
        "input_herb_name": herb.get("input_herb_name", herb_name),
        "herb_match_type": herb.get("herb_match_type", ""),
        "herb_exact_match": bool(herb.get("herb_exact_match", False)),
        "herb_alias_match": bool(herb.get("herb_alias_match", False)),
        "herb_fuzzy_match_used": bool(herb.get("herb_fuzzy_match_used", False)),
        "herb_match_requires_user_confirmation": bool(herb.get("herb_match_requires_user_confirmation", False)),
        "herb_match_warning": herb.get("herb_match_warning", ""),
        "herb_match_status": herb.get("herb_match_status", ""),
        "herb_match_queries": herb.get("herb_match_queries", herb_query_aliases(herb_name, aliases)),
        "matched_herb_id": herb.get("herb_id", ""),
        "matched_herb_name": herb.get("matched_herb_name") or herb.get("herb_cn_name") or herb.get("herb_pinyin") or herb_name,
        "aliases_used": herb_query_aliases(herb_name, aliases),
        "searched_paths": [str(db_path), str(snapshot_manifest_path(project_root))],
        "raw_ingredient_count": len(ingredient_rows),
        "raw_target_relation_count": len(target_rows),
    }
    audit_path = write_snapshot_audit(project_root, herb_name, result, seed_cache_dir)
    result["snapshot_audit_path"] = str(audit_path)
    return result


def write_snapshot_audit(
    project_root: Path,
    herb_name: str,
    result: dict,
    seed_cache_dir: Path | None = None,
) -> Path:
    audit_dir = ensure_dir(project_root / "results" / "tcmsp_snapshot_audit")
    safe_name = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "_", herb_name).strip("_") or "herb"
    path = audit_dir / f"{safe_name}_audit_report.json"
    seed_ingredient_count = ""
    seed_target_relation_count = ""
    seed_cache_files: list[str] = []
    if seed_cache_dir:
        ingredient_path = seed_cache_dir / "raw_ingredients.csv"
        target_path = seed_cache_dir / "raw_targets.csv"
        seed_cache_files = [str(ingredient_path), str(target_path)]
        if ingredient_path.exists():
            try:
                seed_ingredient_count = len(read_rows(ingredient_path))
            except Exception:
                seed_ingredient_count = ""
        if target_path.exists():
            try:
                seed_target_relation_count = len(read_rows(target_path))
            except Exception:
                seed_target_relation_count = ""
    snapshot_molecules = {
        clean_snapshot_value(row.get("MOL_ID"))
        for row in result.get("ingredient_rows", [])
        if clean_snapshot_value(row.get("MOL_ID"))
    }
    audit = {
        "herb_name": herb_name,
        "generated_at": now_utc(),
        "snapshot_source": result.get("source", ""),
        "snapshot_source_type": result.get("snapshot_source_type", ""),
        "snapshot_db_path": result.get("snapshot_db_path", ""),
        "snapshot_manifest_path": result.get("snapshot_manifest_path", ""),
        "matched_herb_id": result.get("matched_herb_id", ""),
        "matched_herb_name": result.get("matched_herb_name", ""),
        "herb_match_field": result.get("herb_match_field", ""),
        "herb_match_score": result.get("herb_match_score", ""),
        "seed_cache_raw_ingredient_count": seed_ingredient_count,
        "seed_cache_raw_target_relation_count": seed_target_relation_count,
        "seed_cache_files": seed_cache_files,
        "full_snapshot_raw_ingredient_count": result.get("raw_ingredient_count", 0),
        "full_snapshot_raw_target_relation_count": result.get("raw_target_relation_count", 0),
        "full_snapshot_unique_molecule_count": len(snapshot_molecules),
        "source_files": result.get("download_file_paths", []),
        "source_urls": result.get("source_urls", []),
        "snapshot_row_counts": result.get("snapshot_row_counts", {}),
        "conclusion": result.get("completeness_warning", "") or "Local full snapshot was used for herb compound-target retrieval.",
    }
    write_json(path, audit)
    return path


def stable_id(prefix: str, *values: Any) -> str:
    raw = "|".join(clean_snapshot_value(value) for value in values)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"

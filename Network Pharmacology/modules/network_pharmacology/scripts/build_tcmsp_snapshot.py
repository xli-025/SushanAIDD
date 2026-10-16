#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.network_pharmacology.scripts.common import ensure_dir, now_utc, project_root_from_cwd, write_json
from modules.network_pharmacology.scripts.tcmsp_snapshot import (
    clean_snapshot_value,
    snapshot_db_path,
    snapshot_manifest_path,
    stable_id,
)


TCMNP_SYS_DATA_URL = "https://raw.githubusercontent.com/tcmlab/TCMNP/main/R/sysdata.rda"
TCMSP_BASE = "https://tcmsp-e.com"
TCMSP_DOWNLOAD_FILES = {
    "herbs": "02_Info_Herbs_Name.xlsx",
    "molecules": "03_Info_Molecules.xlsx",
    "targets": "04_Info_Targets.xlsx",
    "herb_molecules": "23_Herbs_Molecules_Relationships.xlsx",
    "molecule_targets": "34_Molecules_Targets_Relationships.xlsx",
}
TCMSP_DOWNLOAD_URLS = {
    name: f"{TCMSP_BASE}/attachment/tcmspDB/{filename}"
    for name, filename in TCMSP_DOWNLOAD_FILES.items()
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_xlsx(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 4:
        return False
    with path.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


def probe_official_downloads(project_root: Path) -> list[dict]:
    probe_dir = ensure_dir(project_root / "data" / "tcmsp" / "download_probe")
    probes = []
    for key, url in TCMSP_DOWNLOAD_URLS.items():
        filename = TCMSP_DOWNLOAD_FILES[key]
        target = probe_dir / filename
        meta: dict[str, Any] = {
            "name": key,
            "filename": filename,
            "url": url,
            "saved_path": str(target),
            "status": "",
            "content_type": "",
            "size_bytes": 0,
            "magic_bytes_hex": "",
            "is_valid_xlsx": False,
            "error": "",
        }
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NPFlow-Agent/0.1.0"})
            with urllib.request.urlopen(request, timeout=40) as response:
                content = response.read()
                meta["status"] = getattr(response, "status", "")
                meta["content_type"] = response.headers.get("content-type", "")
            target.write_bytes(content)
            meta["size_bytes"] = len(content)
            meta["magic_bytes_hex"] = content[:8].hex()
            meta["is_valid_xlsx"] = is_xlsx(target)
            if not meta["is_valid_xlsx"] and content[:1] == b"<":
                html_path = target.with_suffix(target.suffix + ".html")
                target.replace(html_path)
                meta["saved_path"] = str(html_path)
                meta["error"] = "downloaded content is HTML, not XLSX"
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
        probes.append(meta)
    write_json(probe_dir / "official_download_probe.json", probes)
    return probes


def download_tcmnp_sysdata(project_root: Path) -> Path:
    cache_dir = ensure_dir(project_root / "data" / "cache" / "tcmsp" / "tcmnp")
    path = cache_dir / "TCMNP_sysdata.rda"
    if path.exists() and path.stat().st_size > 0:
        return path
    request = urllib.request.Request(TCMNP_SYS_DATA_URL, headers={"User-Agent": "NPFlow-Agent/0.1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        path.write_bytes(response.read())
    return path


def cache_tcmnp_sysdata(project_root: Path, source_path: Path) -> Path:
    cache_dir = ensure_dir(project_root / "data" / "cache" / "tcmsp" / "tcmnp")
    target = cache_dir / "TCMNP_sysdata.rda"
    if source_path.resolve() != target.resolve():
        shutil.copy2(source_path, target)
    return target


def load_tcmnp_tcmsp_table(path: Path) -> pd.DataFrame:
    try:
        import rdata
    except ImportError as exc:
        raise SystemExit("rdata is required to import TCMNP RDA files. Run: pip install rdata") from exc
    parsed = rdata.parser.parse_file(path)
    converted = rdata.conversion.convert(parsed, default_encoding="utf-8", force_default_encoding=True)
    table = converted.get("tcmsp") if isinstance(converted, dict) else None
    if table is None or not isinstance(table, pd.DataFrame):
        raise SystemExit(f"No tcmsp data.frame found in {path}")
    return table


def _molecule_numeric_id(molecule_id: str) -> str:
    value = clean_snapshot_value(molecule_id)
    if value.upper().startswith("MOL"):
        return value[3:]
    if value.lower().startswith("tcm"):
        return value[3:]
    return value


def _target_id(gene: str, full_name: str) -> str:
    gene = clean_snapshot_value(gene)
    if gene and gene.lower() != "none":
        return gene.upper()
    return stable_id("target", full_name)


def build_from_tcmnp_sysdata(sysdata_path: Path, out_db: Path) -> dict:
    tcmsp = load_tcmnp_tcmsp_table(sysdata_path)
    tcmsp = tcmsp.rename(columns={column: str(column) for column in tcmsp.columns})
    required = {
        "Herb_cn_name",
        "HERB_NAME_PIN_YIN",
        "Herb_name_pin_yin",
        "Molecule_name",
        "MOL_ID",
        "OB",
        "DL",
        "Target_full_name",
        "Target",
    }
    missing = sorted(required - set(tcmsp.columns))
    if missing:
        raise SystemExit(f"TCMNP tcmsp table missing columns: {', '.join(missing)}")

    tcmsp = tcmsp.fillna("")
    for column in required:
        tcmsp[column] = tcmsp[column].map(clean_snapshot_value)
    tcmsp = tcmsp[tcmsp["Herb_cn_name"] != ""].copy()

    herb_records = []
    herb_id_by_key: dict[tuple[str, str], str] = {}
    for cn, group in tcmsp.groupby("Herb_cn_name", dropna=False):
        first = group.iloc[0]
        cn = clean_snapshot_value(cn or first["Herb_cn_name"])
        pinyin_values = sorted(
            {
                clean_snapshot_value(value)
                for value in list(group["Herb_name_pin_yin"].unique()) + list(group["HERB_NAME_PIN_YIN"].unique())
                if clean_snapshot_value(value)
            }
        )
        pinyin = pinyin_values[0] if pinyin_values else ""
        herb_id = stable_id("herb", cn)
        aliases = sorted(
            {
                value
                for value in [cn, *pinyin_values, *(item.replace(" ", "") for item in pinyin_values), *(item.title() for item in pinyin_values)]
                if value
            }
        )
        herb_records.append(
            {
                "herb_id": herb_id,
                "herb_cn_name": cn,
                "herb_pinyin": pinyin,
                "herb_en_name": "",
                "herb_latin_name": "",
                "aliases": "|".join(aliases),
                "source": "tcmnp_package_sysdata_tcmsp",
                "source_row_id": "",
            }
        )
        for pinyin_key in pinyin_values or [""]:
            herb_id_by_key[(cn, pinyin_key)] = herb_id

    molecule_source = tcmsp[tcmsp["MOL_ID"] != ""].copy()
    molecule_source = molecule_source.sort_values(["MOL_ID", "Molecule_name"]).drop_duplicates("MOL_ID")
    molecules = pd.DataFrame(
        [
            {
                "molecule_id": row["MOL_ID"],
                "molecule_numeric_id": _molecule_numeric_id(row["MOL_ID"]),
                "molecule_name": row["Molecule_name"],
                "molecule_cn_name": "",
                "ob": row["OB"],
                "dl": row["DL"],
                "mw": "",
                "alogp": "",
                "hdon": "",
                "hacc": "",
                "caco2": "",
                "bbb": "",
                "smiles": "",
                "inchikey": "",
                "pubchem_cid": "",
                "source": "tcmnp_package_sysdata_tcmsp",
            }
            for _, row in molecule_source.iterrows()
        ]
    )

    target_source = tcmsp[
        (tcmsp["Target"] != "")
        & (tcmsp["Target"].str.lower() != "none")
        & (tcmsp["MOL_ID"] != "")
    ].copy()
    target_source["target_id"] = target_source.apply(lambda row: _target_id(row["Target"], row["Target_full_name"]), axis=1)
    target_dedup = target_source.sort_values(["target_id", "Target_full_name"]).drop_duplicates("target_id")
    targets = pd.DataFrame(
        [
            {
                "target_id": row["target_id"],
                "target_name": row["Target_full_name"] or row["Target"],
                "gene_symbol": row["Target"].upper(),
                "uniprot_id": "",
                "organism": "Homo sapiens",
                "source": "tcmnp_package_sysdata_tcmsp",
            }
            for _, row in target_dedup.iterrows()
        ]
    )

    hcr_rows = []
    for _, row in tcmsp[["Herb_cn_name", "Herb_name_pin_yin", "MOL_ID"]].drop_duplicates().iterrows():
        molecule_id = clean_snapshot_value(row["MOL_ID"])
        if not molecule_id:
            continue
        herb_id = herb_id_by_key.get((row["Herb_cn_name"], row["Herb_name_pin_yin"])) or herb_id_by_key.get((row["Herb_cn_name"], ""))
        if not herb_id:
            continue
        hcr_rows.append(
            {
                "herb_id": herb_id,
                "molecule_id": molecule_id,
                "evidence_source": "tcmnp_package_sysdata_tcmsp",
            }
        )
    herb_compound = pd.DataFrame(hcr_rows).drop_duplicates()

    compound_target = target_source[["MOL_ID", "target_id", "Target", "Target_full_name"]].drop_duplicates()
    compound_target = pd.DataFrame(
        [
            {
                "molecule_id": row["MOL_ID"],
                "target_id": row["target_id"],
                "target_gene": row["Target"].upper(),
                "target_name": row["Target_full_name"] or row["Target"],
                "evidence_type": "predicted",
                "score": "",
                "evidence_source": "tcmnp_package_sysdata_tcmsp",
            }
            for _, row in compound_target.iterrows()
        ]
    ).drop_duplicates()

    ensure_dir(out_db.parent)
    with sqlite3.connect(out_db) as conn:
        conn.execute("pragma journal_mode=OFF")
        conn.execute("pragma synchronous=OFF")
        pd.DataFrame(herb_records).to_sql("herbs", conn, if_exists="replace", index=False)
        molecules.to_sql("molecules", conn, if_exists="replace", index=False)
        targets.to_sql("targets", conn, if_exists="replace", index=False)
        herb_compound.to_sql("herb_compound_relationships", conn, if_exists="replace", index=False)
        compound_target.to_sql("compound_target_relationships", conn, if_exists="replace", index=False)
        conn.execute("create index if not exists idx_herbs_cn on herbs(herb_cn_name)")
        conn.execute("create index if not exists idx_hcr_herb on herb_compound_relationships(herb_id)")
        conn.execute("create index if not exists idx_hcr_mol on herb_compound_relationships(molecule_id)")
        conn.execute("create index if not exists idx_ctr_mol on compound_target_relationships(molecule_id)")
        conn.execute("create index if not exists idx_targets_gene on targets(gene_symbol)")

    return {
        "herbs": len(herb_records),
        "molecules": len(molecules),
        "targets": len(targets),
        "herb_compound_relationships": len(herb_compound),
        "compound_target_relationships": len(compound_target),
        "source_rows": len(tcmsp),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local TCMSP-compatible SQLite snapshot for NPFlow-Agent.")
    parser.add_argument("--out", default=None, help="Output SQLite path. Default: data/tcmsp/tcmsp.sqlite")
    parser.add_argument("--tcmnp-sysdata", default="", help="Local TCMNP R/sysdata.rda path. If omitted, it is downloaded.")
    parser.add_argument("--skip-official-probe", action="store_true", help="Do not probe TCMSP official Excel URLs.")
    args = parser.parse_args()

    project_root = project_root_from_cwd()
    out_db = Path(args.out) if args.out else snapshot_db_path(project_root)
    official_probe = [] if args.skip_official_probe else probe_official_downloads(project_root)
    sysdata_path = cache_tcmnp_sysdata(project_root, Path(args.tcmnp_sysdata)) if args.tcmnp_sysdata else download_tcmnp_sysdata(project_root)

    tmp_db = Path(tempfile.mkstemp(prefix="tcmsp_snapshot_", suffix=".sqlite")[1])
    try:
        row_counts = build_from_tcmnp_sysdata(sysdata_path, tmp_db)
        ensure_dir(out_db.parent)
        shutil.move(str(tmp_db), out_db)
    finally:
        tmp_db.unlink(missing_ok=True)

    manifest = {
        "schema_version": "1.0",
        "created_at": now_utc(),
        "source_type": "tcmnp_package_sysdata_tcmsp",
        "source_files": [str(sysdata_path)],
        "source_urls": [TCMNP_SYS_DATA_URL],
        "source_file_sha256": {str(sysdata_path): sha256_file(sysdata_path)},
        "official_download_probe": official_probe,
        "row_counts": row_counts,
        "import_warnings": [
            "TCMSP official Excel links were probed and may return HTML instead of XLSX; see official_download_probe.",
            "Snapshot is built from TCMNP package sysdata object named tcmsp, a TCMSP-compatible secondary source.",
        ],
        "import_errors": [],
        "snapshot_db_path": str(out_db),
    }
    write_json(snapshot_manifest_path(project_root), manifest)
    print(f"Built TCMSP-compatible snapshot: {out_db}")
    print(f"Manifest: {snapshot_manifest_path(project_root)}")
    print(json.dumps(row_counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

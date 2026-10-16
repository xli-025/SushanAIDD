"""Small, fail-closed target/chain identity contract for automatic receptors."""
from __future__ import annotations

import json
from pathlib import Path

import requests

IDENTITY_FIELDS = ["uniprot_id", "polymer_entity_id", "label_asym_id", "auth_asym_id",
                   "target_identity_status", "target_identity_evidence", "mutation_status",
                   "identity_warning", "selected_chain_sequence", "prepared_identity_status"]

# Explicitly reviewed ATP-site variant; do not generalize this to every mutant/domain.
KNOWN_OBJECT_CHANGES = {("1PME", "P28482"): "ATP_site_five_mutation_variant_not_wild_type"}


def _data(kind: str, key: str, cache: Path) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (kind + "_" + key.replace("/", "_") + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    response = requests.get(f"https://data.rcsb.org/rest/v1/core/{kind}/{key}", timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("invalid RCSB identity response")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def target_chains(pdb_id: str, uniprot: str, cache: Path) -> dict[str, dict]:
    """Return author-chain keys only after entity AND instance verification."""
    pdb_id = pdb_id.upper()
    if (pdb_id, uniprot) in KNOWN_OBJECT_CHANGES:
        raise ValueError(KNOWN_OBJECT_CHANGES[pdb_id, uniprot])
    entry = _data("entry", pdb_id, cache)
    matches = {}
    for entity_id in entry.get("rcsb_entry_container_identifiers", {}).get("polymer_entity_ids", []):
        entity = _data("polymer_entity", f"{pdb_id}/{entity_id}", cache)
        ids = entity.get("rcsb_polymer_entity_container_identifiers", {})
        refs = ids.get("reference_sequence_identifiers") or []
        accessions = {r.get("database_accession") for r in refs if r.get("database_name") == "UniProt"}
        if uniprot not in accessions:
            continue
        if accessions != {uniprot}:
            continue  # Unresolved chimeric entity is not a verified target.
        info = entity.get("rcsb_polymer_entity", {})
        mutation = str(info.get("pdbx_mutation") or "").strip()
        seq = str(entity.get("entity_poly", {}).get("pdbx_seq_one_letter_code_can") or "").replace("\n", "")
        if uniprot == "P01308" and len(seq) < 40:
            continue  # Current single-chain preparer cannot represent mature insulin A+B.
        warning = "domain_or_missing_residues_not_fully_assessed"
        if mutation and mutation.lower() not in {"none", "no", "?", "."}:
            warning += ";annotated_mutant:" + mutation
        for label in ids.get("asym_ids", []):
            instance = _data("polymer_entity_instance", f"{pdb_id}/{label}", cache)
            inst = instance.get("rcsb_polymer_entity_instance_container_identifiers", {})
            auth = inst.get("auth_asym_id", "")
            if str(inst.get("entity_id")) != str(entity_id) or auth not in ids.get("auth_asym_ids", []):
                continue
            matches[auth] = {"uniprot_id": uniprot, "polymer_entity_id": str(entity_id),
                "label_asym_id": label, "auth_asym_id": auth,
                "target_identity_status": "verified_rcsb_entity_instance",
                "target_identity_evidence": str((cache / f"polymer_entity_{pdb_id}_{entity_id}.json").resolve()),
                "mutation_status": mutation or "not_annotated", "identity_warning": warning}
    if not matches:
        raise ValueError("target_chain_identity_unconfirmed_or_required_chains_missing")
    return matches


def _residue_name(name):
    return {"HID": "HIS", "HIE": "HIS", "HIP": "HIS", "HSD": "HIS", "HSE": "HIS", "HSP": "HIS",
            "ASH": "ASP", "GLH": "GLU", "LYN": "LYS", "CYM": "CYS", "CYX": "CYS"}.get(name, name)


def protein_signature(chain) -> str:
    return ";".join(_residue_name(res.get_resname().strip()) for res in chain if res.id[0] == " " and "CA" in res)


def prepared_signature(path: Path) -> str:
    residues = []
    seen = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ATOM") and line[12:16].strip() == "CA":
            key = line[21:27]
            if key not in seen:
                seen.add(key)
                residues.append(_residue_name(line[17:20].strip()))
    return ";".join(residues)

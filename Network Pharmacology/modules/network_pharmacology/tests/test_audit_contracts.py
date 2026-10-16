from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest
import yaml


NETWORK_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = NETWORK_ROOT / "modules" / "network_pharmacology" / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert rows
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


rank = load_module("audit_rank_core_targets", SCRIPTS / "rank_core_targets.py")
disease = load_module("audit_fetch_disease_targets", SCRIPTS / "fetch_disease_targets.py")
integrator = load_module("audit_herb_source_integrator", SCRIPTS / "herb_source_integrator.py")
compounds = load_module("audit_fetch_herb_compounds", SCRIPTS / "fetch_herb_compounds.py")
manager = load_module(
    "audit_manager_agent", NETWORK_ROOT / "modules" / "manager_agent" / "qwen_npflow_agent.py"
)


def test_default_config_validates_and_materializes_legacy_contract(tmp_path: Path):
    config = yaml.safe_load((NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))
    config["output"]["dir"] = "results/test_default_config"
    config_path = tmp_path / "params.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_input.py"), "--config", str(config_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    resolved_path = tmp_path / "results" / "test_default_config" / "00_input" / "params.resolved.yaml"
    resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    assert resolved["schema_version"] == "npflow.network.v2"
    assert resolved["core_target_selection"]["top_n"] == resolved["selection"]["docking_targets"]["top_n"]


def test_manager_uses_default_effective_config_and_collision_resistant_run_ids(
    tmp_path: Path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    default_config = yaml.safe_load((NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))
    default_config["ranking"]["ligand_score_weights"]["target_link"] = 0.31
    default_config["ranking"]["ligand_score_weights"]["disease_relevance"] = 0.24
    (config_dir / "params.yaml").write_text(
        yaml.safe_dump(default_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)

    first = manager.write_params_yaml("herb", "黄芩", "hepatocellular carcinoma")
    second = manager.write_params_yaml("herb", "黄芩", "hepatocellular carcinoma")

    assert first != second
    generated = yaml.safe_load(first.read_text(encoding="utf-8"))
    assert generated["ranking"]["ligand_score_weights"]["target_link"] == 0.31
    assert generated["selection"]["docking_ligands"]["min_ligand_confidence"] == 0.55
    assert generated["config_provenance"]["base_config"] == str(config_dir / "params.yaml")


def test_manager_writes_provisional_web_binding_before_child_work(tmp_path: Path, monkeypatch):
    output_dir = tmp_path / "results" / "network_pharmacology_runs" / "failed-before-child"
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_51")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", "server-issued-run-id-000051")

    assert manager.write_provisional_web_binding(output_dir)

    manifest = json.loads((output_dir / manager.RUN_BINDING_FILENAME).read_text(encoding="utf-8"))
    assert manifest == {
        **manifest,
        "schema_version": 1,
        "kind": "npflow_network_run_binding",
        "task_id": "task_51",
        "run_id": "server-issued-run-id-000051",
        "stage": "network",
        "status": "provisional",
        "output_dir": str(output_dir.resolve()),
    }


def test_manager_provisional_manifest_failure_removes_empty_run_dir(tmp_path: Path, monkeypatch):
    output_dir = tmp_path / "results" / "network_pharmacology_runs" / "manifest-failure"
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_52")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", "server-issued-run-id-000052")

    def fail_replace(*args, **kwargs):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(manager.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture replace failure"):
        manager.write_provisional_web_binding(output_dir)
    assert not output_dir.exists()


def test_manager_does_not_claim_cli_or_invalid_web_runs(tmp_path: Path, monkeypatch):
    cli_output = tmp_path / "cli"
    assert not manager.write_provisional_web_binding(cli_output)
    assert not cli_output.exists()

    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_1")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", "short")
    with __import__("pytest").raises(ValueError, match="RUN_ID"):
        manager.write_provisional_web_binding(tmp_path / "invalid")


def test_manager_structured_cli_preserves_assignment_words_as_data(monkeypatch):
    captured = []
    monkeypatch.setattr(manager, "run_workflow", lambda *values: captured.append(values))
    entity = "Herb 疾病=wrong, disease=also-wrong"
    disease_name = "True disease disease=still-data"

    manager.main(
        [
            "--input-mode",
            "herb",
            "--entity-name",
            entity,
            "--disease-name",
            disease_name,
        ]
    )

    assert captured == [("herb", entity, disease_name)]


def test_disease_lookup_never_silently_uses_first_nonexact_hit(monkeypatch):
    hits = [
        {"id": "EFO_WRONG", "name": "inflammation"},
        {"id": "EFO_EXACT", "name": "non-classical inflammation"},
    ]
    monkeypatch.setattr(disease, "graphql", lambda query, variables: {"search": {"hits": hits}})

    selected_id, selected_name, _ = disease.resolve_disease_id("non-classical inflammation")
    assert (selected_id, selected_name) == ("EFO_EXACT", "non-classical inflammation")
    assert disease.disease_query_for_opentargets("non-classical inflammation") == "non-classical inflammation"

    with __import__("pytest").raises(RuntimeError):
        disease.resolve_disease_id("qualified inflammatory syndrome")

    selected_id, _, _ = disease.resolve_disease_id(
        "qualified inflammatory syndrome", expected_disease_id="EFO_WRONG"
    )
    assert selected_id == "EFO_WRONG"


def test_sole_nonexact_disease_hit_requires_explicit_permissive_policy(monkeypatch):
    hits = [{"id": "EFO_ONLY", "name": "different syndrome"}]
    monkeypatch.setattr(disease, "graphql", lambda query, variables: {"search": {"hits": hits}})

    with pytest.raises(RuntimeError):
        disease.resolve_disease_id(
            "requested syndrome", require_exact=True, allow_nonexact=False
        )

    selected_id, selected_name, _ = disease.resolve_disease_id(
        "requested syndrome", require_exact=False, allow_nonexact=True
    )
    assert (selected_id, selected_name) == ("EFO_ONLY", "different syndrome")


@pytest.mark.parametrize(
    ("policy_update", "expected_error"),
    [
        ({"allow_nonexact_match": "false"}, "must be a boolean"),
        (
            {"require_exact_match": True, "allow_nonexact_match": True},
            "cannot both be true",
        ),
    ],
)
def test_disease_mapping_config_rejects_truthy_strings_and_conflicts(
    tmp_path: Path, policy_update: dict, expected_error: str
):
    config = yaml.safe_load(
        (NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8")
    )
    config["output"]["dir"] = "results/test_disease_policy"
    config["disease_mapping"].update(policy_update)
    config_path = tmp_path / "params.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_input.py"), "--config", str(config_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize(
    ("section_name", "key"),
    [
        ("docking_ligands", "require_structure"),
        ("docking_ligands", "require_linked_docking_target"),
        ("docking_targets", "require_disease_intersection"),
        ("docking_targets", "require_human_target"),
    ],
)
def test_selection_config_rejects_string_false_booleans(
    tmp_path: Path, section_name: str, key: str
):
    config = yaml.safe_load(
        (NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8")
    )
    config["output"]["dir"] = "results/test_selection_bool"
    config["selection"][section_name][key] = "false"
    config_path = tmp_path / "params.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_input.py"), "--config", str(config_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"selection.{section_name}.{key} must be a boolean" in result.stderr


def test_ligand_selection_runtime_rejects_string_false():
    with pytest.raises(RuntimeError, match="require_structure must be a boolean"):
        rank.select_docking_ligands(
            [],
            {
                "top_n": 1,
                "min_ligand_score": 0.5,
                "min_ligand_confidence": 0.5,
                "require_structure": "false",
                "require_linked_docking_target": True,
            },
        )


def test_ligand_selection_enforces_score_confidence_structure_and_top_n():
    base = {
        "canonical_smiles": "CCO",
        "linked_docking_targets": "TNF",
        "ligand_confidence": 0.8,
    }
    candidates = [
        {**base, "ligand_id": "best", "ligand_score": 0.9},
        {**base, "ligand_id": "top_n_cut", "ligand_score": 0.8},
        {**base, "ligand_id": "low_conf", "ligand_score": 0.95, "ligand_confidence": 0.49},
        {**base, "ligand_id": "low_score", "ligand_score": 0.49},
        {**base, "ligand_id": "no_structure", "ligand_score": 0.75, "canonical_smiles": ""},
    ]
    selected, audited = rank.select_docking_ligands(
        candidates,
        {
            "top_n": 1,
            "min_ligand_score": 0.5,
            "min_ligand_confidence": 0.5,
            "require_structure": True,
            "require_linked_docking_target": True,
        },
    )

    assert [row["ligand_id"] for row in selected] == ["best"]
    by_id = {row["ligand_id"]: row for row in audited}
    assert by_id["best"]["contract_version"] == "docking_ligands.v2"
    assert by_id["top_n_cut"]["selection_exclusion_reason"] == "outside_top_n"
    assert "below_min_ligand_confidence" in by_id["low_conf"]["selection_exclusion_reason"]
    assert "below_min_ligand_score" in by_id["low_score"]["selection_exclusion_reason"]
    assert "missing_required_structure" in by_id["no_structure"]["selection_exclusion_reason"]


def test_rank_main_writes_only_versioned_selected_ligands(tmp_path: Path):
    output = tmp_path / "run"
    write_csv(
        output / "04_intersection" / "intersection_targets.csv",
        [
            {
                "target_gene": "TNF",
                "uniprot_id": "P01375",
                "target_name": "tumor necrosis factor",
                "disease_association_score": 0.8,
                "compound_count": 1,
            }
        ],
    )
    graph = nx.Graph()
    graph.add_node("TNF")
    (output / "05_ppi").mkdir(parents=True)
    nx.write_graphml(graph, output / "05_ppi" / "ppi_network.graphml")
    write_csv(
        output / "02_compound_targets" / "union_targets.csv",
        [{"target_gene": "TNF", "source_list": "TCMSP", "target_confidence": 0.9}],
    )
    write_csv(
        output / "02_compound_targets" / "union_ingredient_target_edges.csv",
        [
            {
                "canonical_ingredient_id": "canonical-A",
                "ingredient_id": "A",
                "target_gene": "TNF",
                "source_list": "TCMSP",
                "ingredient_confidence": 0.9,
                "relation_confidence": 0.9,
                "network_eligibility": "pending_activity_filter",
            }
        ],
    )
    write_csv(
        output / "02_compound_targets" / "compound_targets.normalized.csv",
        [{"ingredient_id": "A", "target_gene": "TNF"}],
    )
    write_csv(
        output / "01_ingredients" / "union_ingredients.csv",
        [
            {
                "canonical_ingredient_id": "canonical-A",
                "source_ids": "A",
                "pubchem_cid": "1",
                "ingredient_confidence": 0.9,
                "source_list": "TCMSP",
            }
        ],
    )
    write_csv(
        output / "01_ingredients" / "ligand_ready_ingredients.csv",
        [
            {
                "ingredient_id": "A",
                "ingredient_name": "fixture ligand",
                "herb_name": "fixture herb",
                "pubchem_cid": "1",
                "inchikey": "FIXTURE-INCHIKEY",
                "canonical_smiles": "CCO",
                "source_list": "TCMSP",
                "pubchem_match_status": "matched",
            }
        ],
    )
    config = yaml.safe_load((NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))
    config["runtime"] = {"output_dir": str(output)}
    config["selection"]["docking_targets"]["min_target_score"] = 0.4
    config_path = tmp_path / "resolved.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "rank_core_targets.py"), "--config", str(config_path)],
        cwd=NETWORK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with (output / "07_core_targets" / "docking_ligands.csv").open(encoding="utf-8") as handle:
        selected = list(csv.DictReader(handle))
    assert len(selected) == 1
    assert selected[0]["contract_version"] == "docking_ligands.v2"
    assert selected[0]["selection_status"] == "selected"


def write_exploratory_only_rank_fixture(tmp_path: Path) -> tuple[Path, dict]:
    output = tmp_path / "exploratory_run"
    write_csv(
        output / "04_intersection" / "intersection_targets.csv",
        [
            {
                "target_gene": "TNF",
                "uniprot_id": "P01375",
                "target_name": "tumor necrosis factor",
                "disease_association_score": 0.8,
                "compound_count": 1,
            }
        ],
    )
    graph = nx.Graph()
    graph.add_node("TNF")
    (output / "05_ppi").mkdir(parents=True)
    nx.write_graphml(graph, output / "05_ppi" / "ppi_network.graphml")
    write_csv(
        output / "02_compound_targets" / "union_targets.csv",
        [{"target_gene": "TNF", "source_list": "HerbiV", "target_confidence": 0.99}],
    )
    write_csv(
        output / "02_compound_targets" / "union_ingredient_target_edges.csv",
        [
            {
                "canonical_ingredient_id": "canonical-X",
                "ingredient_id": "X",
                "target_gene": "TNF",
                "source_list": "HerbiV",
                "eligible_source_list": "",
                "ingredient_confidence": 0.99,
                "relation_confidence": 0.99,
                "network_eligibility": "exploratory",
            }
        ],
    )
    compound_path = output / "02_compound_targets" / "compound_targets.normalized.csv"
    compound_path.write_text("ingredient_id,target_gene\n", encoding="utf-8")
    config = yaml.safe_load(
        (NETWORK_ROOT / "config" / "params.yaml").read_text(encoding="utf-8")
    )
    config["runtime"] = {"output_dir": str(output)}
    config["selection"]["docking_targets"]["min_target_score"] = 0.4
    config["selection"]["docking_targets"]["min_target_confidence"] = 0.0
    return output, config


def test_exploratory_only_intersection_never_enters_docking_ranking(tmp_path: Path):
    output, config = write_exploratory_only_rank_fixture(tmp_path)
    config_path = tmp_path / "exploratory.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "rank_core_targets.py"), "--config", str(config_path)],
        cwd=NETWORK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with (output / "07_core_targets" / "docking_targets.csv").open(encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    with (output / "07_core_targets" / "core_targets.csv").open(encoding="utf-8") as handle:
        core_row = next(csv.DictReader(handle))
    assert float(core_row["source_confidence_score"]) == 0.0
    assert int(core_row["eligible_edge_count"]) == 0
    assert core_row["recommended_for_docking"] == "false"
    assert "missing_eligible_edge_provenance" in core_row["selection_reason"]
    assert rank._norm_score([0.0], zero_is_absence=True) == [0.0]


def test_rank_entry_rejects_string_target_selection_bool(tmp_path: Path):
    _output, config = write_exploratory_only_rank_fixture(tmp_path)
    config["selection"]["docking_targets"]["require_human_target"] = "false"
    config_path = tmp_path / "invalid_target_bool.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "rank_core_targets.py"), "--config", str(config_path)],
        cwd=NETWORK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "require_human_target must be a boolean" in result.stderr


def test_target_provenance_counts_real_sources_and_unique_high_confidence_ligands():
    targets = [{"target_gene": "TNF", "source_list": "TCMSP; HerbiV; SymMap2", "target_confidence": 0.99}]
    edges = [
        {
            "ingredient_id": "A",
            "canonical_ingredient_id": "canonical-A",
            "target_gene": "TNF",
            "source_list": "TCMSP",
            "ingredient_confidence": 0.9,
            "relation_confidence": 0.8,
            "network_eligibility": "pending_activity_filter",
        },
        {
            "ingredient_id": "A",
            "canonical_ingredient_id": "canonical-A",
            "target_gene": "TNF",
            "source_list": "TCMSP",
            "ingredient_confidence": 0.9,
            "relation_confidence": 0.8,
            "network_eligibility": "pending_activity_filter",
        },
        {
            "ingredient_id": "B",
            "canonical_ingredient_id": "canonical-B",
            "target_gene": "TNF",
            "source_list": "HerbiV",
            "ingredient_confidence": 0.85,
            "relation_confidence": 0.7,
            "network_eligibility": "exploratory",
        },
        {
            "ingredient_id": "C",
            "canonical_ingredient_id": "canonical-C",
            "target_gene": "TNF",
            "source_list": "SymMap2",
            "ingredient_confidence": 0.65,
            "relation_confidence": 0.65,
            "network_eligibility": "pending_activity_filter",
        },
    ]
    provenance = rank.build_target_provenance(
        targets, edges, active_pairs={("A", "TNF"), ("C", "TNF")}
    )["TNF"]

    assert provenance["sources"] == {"TCMSP", "SymMap2"}
    assert provenance["high_confidence_ligands"] == {"canonical-A"}
    assert provenance["target_confidence"] == 0.8


def test_target_provenance_is_zero_when_edges_exist_but_none_support_target():
    provenance = rank.build_target_provenance(
        [{"target_gene": "TNF", "source_list": "HerbiV", "target_confidence": 0.99}],
        [
            {
                "ingredient_id": "X",
                "target_gene": "IL6",
                "source_list": "SymMap2",
                "ingredient_confidence": 0.65,
                "relation_confidence": 0.6,
                "network_eligibility": "eligible",
            }
        ],
    )["TNF"]

    assert provenance["sources"] == set()
    assert provenance["target_confidence"] == 0.0


def test_target_level_confidence_is_legacy_fallback_only_without_edge_contract():
    targets = [
        {"target_gene": "TNF", "source_list": "TCMSP", "target_confidence": 0.9}
    ]
    current = rank.build_target_provenance(
        targets, [], edge_contract_present=True
    )["TNF"]
    legacy = rank.build_target_provenance(
        targets, [], edge_contract_present=False
    )["TNF"]

    assert current["target_confidence"] == 0.0
    assert current["sources"] == set()
    assert legacy["target_confidence"] == 0.9
    assert legacy["sources"] == {"TCMSP"}


def test_herbiv_policy_separates_candidate_exploratory_and_excluded(tmp_path: Path):
    raw_dir = tmp_path / "data" / "herb_sources" / "herbiv" / "raw"
    raw_dir.mkdir(parents=True)
    csv_path = raw_dir / "herbiv_compound_targets.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["herb_name", "ingredient_name", "target_gene", "combined_score"])
        writer.writeheader()
        writer.writerows(
            [
                {"herb_name": "测试药", "ingredient_name": "candidate", "target_gene": "TNF", "combined_score": "850"},
                {"herb_name": "测试药", "ingredient_name": "exploratory", "target_gene": "IL6", "combined_score": "600"},
                {"herb_name": "测试药", "ingredient_name": "excluded", "target_gene": "TP53", "combined_score": "100"},
            ]
        )

    result = integrator.fetch_herbiv_source(
        tmp_path,
        "测试药",
        [],
        tmp_path / "run",
        eligibility_policy={
            "min_score_for_candidate": 800,
            "exploratory_min_score": 500,
            "exploratory_max_score": 799,
        },
    )
    categories = {row["molecule_name"]: row["source_eligibility"] for row in result["ingredient_rows"]}
    assert categories == {"candidate": "candidate", "exploratory": "exploratory"}
    assert len(result["eligibility_rows"]) == 3

    target_ids = {row["MOL_ID"] for row in result["target_rows"]}
    by_name = {row["molecule_name"]: row for row in result["ingredient_rows"]}
    assert compounds.is_active_ingredient(by_name["candidate"], target_ids, {}) is True
    assert compounds.is_active_ingredient(by_name["exploratory"], target_ids, {}) is False
    by_target = {row["target_gene"]: row for row in result["target_rows"]}
    assert by_target["TNF"]["validated"] == "false"
    assert by_target["TNF"]["mapping_validated"] == "false"
    assert by_target["TNF"]["evidence_class"] == "source_record_unclassified"
    assert integrator._compute_herbiv_confidence(float("inf"))["category"] == "excluded"
    assert integrator._compute_herbiv_confidence(1001)["category"] == "excluded"


def test_mixed_source_and_mixed_edge_eligibility_stays_edge_local(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    herbiv_ingredient = {
        "MOL_ID": "HerbiV:shared",
        "molecule_name": "shared compound",
        "PubChem_ID": "123",
        "source_list": "HerbiV",
        "primary_source": "HerbiV",
        "source_eligibility": "candidate",
        "herbiv_category": "candidate",
        "source_eligibility_reason": "meets_herbiv_candidate_threshold",
        "ingredient_confidence": 0.84,
    }
    herbiv_targets = [
        {
            "MOL_ID": "HerbiV:shared",
            "target_gene": "TNF",
            "target_name": "TNF",
            "source_list": "HerbiV",
            "source": "HerbiV",
            "source_eligibility": "candidate",
            "herbiv_category": "candidate",
            "source_eligibility_reason": "meets_herbiv_candidate_threshold",
            "ingredient_confidence": 0.84,
            "relation_confidence": 0.86,
            "target_evidence_types": "computational_prediction",
            "evidence_class": "computational_prediction",
            "mapping_validated": "false",
        },
        {
            "MOL_ID": "HerbiV:shared",
            "target_gene": "IL6",
            "target_name": "IL6",
            "source_list": "HerbiV",
            "source": "HerbiV",
            "source_eligibility": "exploratory",
            "herbiv_category": "exploratory",
            "source_eligibility_reason": "exploratory_only_not_main_network",
            "ingredient_confidence": 0.83,
            "relation_confidence": 0.82,
            "target_evidence_types": "predicted",
            "evidence_class": "computational_prediction",
            "mapping_validated": "false",
        },
        {
            "MOL_ID": "HerbiV:shared",
            "target_gene": "TP53",
            "target_name": "TP53",
            "source_list": "HerbiV",
            "source": "HerbiV",
            "source_eligibility": "exploratory",
            "herbiv_category": "exploratory",
            "source_eligibility_reason": "exploratory_only_not_main_network",
            "ingredient_confidence": 0.83,
            "relation_confidence": 0.82,
            "target_evidence_types": "predicted",
            "evidence_class": "computational_prediction",
            "mapping_validated": "false",
        },
    ]
    symmap_ingredient = {
        "MOL_ID": "SYMMAP:shared",
        "molecule_name": "shared compound",
        "PubChem_ID": "123",
        "source_list": "SymMap2",
        "primary_source": "SymMap2",
    }
    symmap_target = {
        "MOL_ID": "SYMMAP:shared",
        "target_gene": "IL6",
        "target_name": "IL6",
        "source_list": "SymMap2",
        "source": "SymMap2",
        "target_evidence_types": "symmap_related_component",
        "evidence_class": "database_related_component",
        "mapping_validated": "false",
    }
    run_root = tmp_path / "run"
    merged_ingredients, merged_targets, _ = integrator.merge_herb_source_rows(
        [],
        [],
        [
            {
                "ingredient_rows": [herbiv_ingredient],
                "target_rows": herbiv_targets,
                "diagnostic": {"source": "HerbiV", "status": "available"},
            },
            {
                "ingredient_rows": [symmap_ingredient],
                "target_rows": [symmap_target],
                "diagnostic": {"source": "SymMap2", "status": "available"},
            },
        ],
        tcmsp_source_name="",
        out_root=run_root,
    )

    by_gene = {row["target_gene"]: row for row in merged_targets}
    assert by_gene["TNF"]["network_eligibility"] == "eligible"
    assert by_gene["TP53"]["network_eligibility"] == "exploratory"
    assert compounds.target_edge_eligibility(by_gene["TP53"])[0] is False
    assert by_gene["IL6"]["network_eligibility"] == "eligible"
    assert by_gene["IL6"]["eligible_source_list"] == "SymMap2"
    assert by_gene["IL6"]["ingredient_confidence"] == 0.65
    assert by_gene["IL6"]["relation_confidence"] == 0.6
    assert "HerbiV=exploratory" in by_gene["IL6"]["eligibility_status_by_source"]

    assert compounds.ingredient_eligibility(
        {
            "MOL_ID": "mixed",
            "source_list": "HerbiV; SymMap2",
            "source_eligibility": "exploratory",
        },
        {"mixed"},
        {},
    )[0] is True

    with (run_root / "02_compound_targets" / "union_ingredient_target_edges.csv").open(
        encoding="utf-8"
    ) as handle:
        union_edges = {row["target_gene"]: row for row in csv.DictReader(handle)}
    assert union_edges["TP53"]["network_eligibility"] == "exploratory"
    assert union_edges["IL6"]["eligible_source_list"] == "SymMap2"
    assert float(union_edges["IL6"]["ingredient_confidence"]) == 0.65
    assert union_edges["IL6"]["is_literature_curated"] == "false"
    assert union_edges["IL6"]["mapping_validated"] == "false"
    assert len(merged_ingredients) == 1


def test_ob_dl_qc_uses_only_tcmsp_denominator(tmp_path: Path):
    ingredient_dir = tmp_path / "01_ingredients"
    target_dir = tmp_path / "02_compound_targets"
    ingredient_dir.mkdir()
    target_dir.mkdir()
    raw_rows = [
        {
            "ingredient_id": "T",
            "ingredient_name": "tcmsp",
            "source_list": "TCMSP",
            "ob": "35",
            "dl": "0.2",
            "passed_filter": "true",
            "network_eligibility_reason": "eligible_tcmsp_ob_dl",
        },
        {
            "ingredient_id": "S",
            "ingredient_name": "symmap",
            "source_list": "SymMap2",
            "ob": "",
            "dl": "",
            "passed_filter": "true",
            "network_eligibility_reason": "eligible_symmap_direct_target_relation",
        },
    ]
    target_rows = [
        {"ingredient_id": "T", "target_gene": "TNF"},
        {"ingredient_id": "S", "target_gene": "IL6"},
    ]
    source_summary = {
        "source_rows": [
            {"source": "TCMSP", "raw_ingredients": 1, "raw_target_relations": 1},
            {"source": "HerbiV", "raw_ingredients": 3, "raw_target_relations": 4},
            {"source": "SymMap2", "raw_ingredients": 1, "raw_target_relations": 1},
        ],
        "raw_target_relations_before_merge": 2,
    }
    compounds.write_module_outputs(
        params={
            "input": {"mode": "herb", "herb_name": "test"},
            "compound_filter": {"ob_min": 30, "dl_min": 0.18},
        },
        ingredient_dir=ingredient_dir,
        target_dir=target_dir,
        pubchem_cache_path=tmp_path / "pubchem.json",
        pubchem_cache={},
        raw_output=raw_rows,
        active_rows=raw_rows,
        compound_targets=target_rows,
        unmapped_targets=[],
        ambiguous_targets=[],
        ligand_ready_rows=[],
        warnings=[],
        pubchem_enabled=False,
        detail_url="",
        source_merge_summary=source_summary,
    )

    qc = json.loads((ingredient_dir / "ingredient_qc.json").read_text(encoding="utf-8"))
    assert qc["tcmsp_ob_dl_evaluated_ingredient_count"] == 1
    assert qc["ob_dl_pass_ingredient_count"] == 1
    assert qc["ob_dl_filtered_ingredient_count"] == 0
    assert qc["herbiv_raw_ingredient_count"] == 3
    assert qc["symmap_raw_ingredient_count"] == 1


def test_tcmsp_failed_ob_dl_cannot_inflate_symmap_edge_confidence(tmp_path: Path):
    ingredient = {
        "MOL_ID": "mixed",
        "source_list": "TCMSP; SymMap2",
        "ob": "10",
        "dl": "0.1",
    }
    edge = {
        "ingredient_id": "mixed",
        "canonical_ingredient_id": "canonical-mixed",
        "target_gene": "TNF",
        "source_list": "TCMSP; SymMap2",
        "eligible_source_list": "TCMSP; SymMap2",
        "ingredient_confidence": 1.0,
        "relation_confidence": 0.9,
        "network_eligibility": "eligible",
        "_source_edge_records": [
            {"source_list": "TCMSP"},
            {"source_list": "SymMap2"},
        ],
    }
    ingredient_decisions = compounds.ingredient_source_eligibility(ingredient, {})
    final_sources = compounds.target_edge_eligible_sources(edge) & {
        source
        for source, (eligible, _reason) in ingredient_decisions.items()
        if eligible
    }
    ingredient_confidence, relation_confidence = compounds.edge_confidence_for_sources(
        edge, final_sources
    )

    assert final_sources == {"SymMap2"}
    assert ingredient_confidence == 0.65
    assert relation_confidence == 0.6

    final_edge = {
        **edge,
        "source_list": "SymMap2",
        "eligible_source_list": "SymMap2",
        "ingredient_confidence": ingredient_confidence,
        "relation_confidence": relation_confidence,
    }
    provenance = rank.build_target_provenance(
        [{"target_gene": "TNF", "target_confidence": 0.99}],
        [edge],
        active_pairs={("mixed", "TNF")},
        active_edge_overrides={("mixed", "TNF"): final_edge},
    )["TNF"]
    assert provenance["sources"] == {"SymMap2"}
    assert provenance["target_confidence"] == 0.6
    assert provenance["high_confidence_ligands"] == set()

    audit_edge = {key: value for key, value in edge.items() if not key.startswith("_")}
    write_csv(
        tmp_path / "02_compound_targets" / "union_ingredient_target_edges.csv",
        [audit_edge],
    )
    write_csv(
        tmp_path / "02_compound_targets" / "union_targets.csv",
        [{"target_gene": "TNF", "target_confidence": 0.99}],
    )
    compounds.finalize_union_edge_audit(tmp_path, [final_edge])
    with (tmp_path / "02_compound_targets" / "union_ingredient_target_edges.csv").open(
        encoding="utf-8"
    ) as handle:
        finalized = next(csv.DictReader(handle))
    assert finalized["eligible_source_list"] == "SymMap2"
    assert float(finalized["ingredient_confidence"]) == 0.65
    assert float(finalized["relation_confidence"]) == 0.6
    with (tmp_path / "02_compound_targets" / "union_targets.csv").open(
        encoding="utf-8"
    ) as handle:
        target = next(csv.DictReader(handle))
    assert float(target["target_confidence"]) == 0.6


def test_predicted_binding_label_is_not_classified_as_experimental():
    assert integrator._evidence_class("predicted binding") == "computational_prediction"
    assert compounds.classify_target_evidence("predicted binding") == "computational_prediction"

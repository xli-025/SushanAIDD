import csv
import json
import math
import types
from pathlib import Path

import pytest

import npflow_agent as agent


def valid_params(**overrides):
    params = {
        "herbs": ["Herb A"],
        "compound": None,
        "disease": "Disease X",
        "mode": "network",
        "ph": 7.4,
        "extra_targets": [],
        "extra_ligands": [],
    }
    params.update(overrides)
    return params


def test_failed_network_log_preserves_first_error(monkeypatch, capsys):
    output = "FIRST_DISEASE_LOOKUP_ERROR\n" + "diagnostic detail\n" * 400
    result = types.SimpleNamespace(stdout=output, stderr="", returncode=7)
    monkeypatch.setattr(agent, "_captured_command", lambda *args, **kwargs: result)
    from contextlib import nullcontext
    monkeypatch.setattr(agent, "_manager_run_lock", lambda *args: nullcontext())
    with pytest.raises(RuntimeError, match="exit=7"):
        agent.run_network_pharm("丹参", None, "高血压")
    assert "FIRST_DISEASE_LOOKUP_ERROR" in capsys.readouterr().out


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "AIDD root with spaces"
    network_runs = root / "Network Pharmacology" / "results" / "network_pharmacology_runs"
    results = root / "results"
    network_runs.mkdir(parents=True)
    results.mkdir(parents=True)
    monkeypatch.setattr(agent, "PROJECT_ROOT", root)
    monkeypatch.setattr(agent, "NETWORK_RUNS_ROOT", network_runs)
    monkeypatch.setattr(agent, "RESULTS_ROOT", results)
    monkeypatch.setattr(agent, "WEB_RESULTS_ROOT", results / "web_tasks")
    monkeypatch.setattr(agent, "NP_LATEST", str(network_runs / "latest_run.json"))
    return root


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def make_bound_network_run(
    root: Path,
    context: dict,
    name: str,
    herb: str,
    *,
    ligand_id: str,
    inchikey: str = "",
    cid: str = "",
) -> Path:
    run_dir = root / "Network Pharmacology" / "results" / "network_pharmacology_runs" / name
    write_csv(
        run_dir / "04_intersection" / "intersection_targets.csv",
        ["target_gene"],
        [{"target_gene": "T1"}],
    )
    write_csv(
        run_dir / "07_core_targets" / "docking_targets.csv",
        ["target_gene", "target_score"],
        [{"target_gene": "T1", "target_score": "0.8"}],
    )
    write_csv(
        run_dir / "07_core_targets" / "docking_ligands.csv",
        ["inchikey", "pubchem_cid", "ligand_id", "canonical_smiles", "ligand_score"],
        [
            {
                "inchikey": inchikey,
                "pubchem_cid": cid,
                "ligand_id": ligand_id,
                "canonical_smiles": "",
                "ligand_score": "0.8",
            }
        ],
    )
    qc = {
        "module": "network_pharmacology",
        "status": "passed",
        "workflow_status": "passed",
        "counts": {"intersection_targets": 1},
    }
    (run_dir / "08_report").mkdir(parents=True)
    agent._atomic_write_json(run_dir / "08_report" / "qc_report.json", qc)
    binding = agent._network_binding_payload(
        context,
        entity_kind="herb",
        entity=herb,
        disease="Disease X",
        source_index=1,
        output_dir=run_dir,
    )
    agent._atomic_write_json(run_dir / agent.RUN_BINDING_FILENAME, binding)
    return run_dir


@pytest.mark.parametrize("bad_ph", ["7.4", True, math.nan, math.inf, -0.1, 14.1])
def test_llm_params_reject_non_numeric_non_finite_or_out_of_range_ph(bad_ph) -> None:
    with pytest.raises(ValueError):
        agent.validate_pipeline_params(valid_params(ph=bad_ph))


def test_llm_params_reject_wrong_collection_type_and_unknown_mode() -> None:
    with pytest.raises(ValueError, match="herbs"):
        agent.validate_pipeline_params(valid_params(herbs="Herb A"))
    with pytest.raises(ValueError, match="未知 mode"):
        agent.validate_pipeline_params(valid_params(mode="full; unsafe"))


def test_llm_params_reject_unknown_or_ambiguous_schema_fields() -> None:
    with pytest.raises(ValueError, match="未声明字段"):
        agent.validate_pipeline_params(valid_params(injected_command="do not accept"))
    with pytest.raises(ValueError, match="不能同时"):
        agent.validate_pipeline_params(valid_params(herb="legacy duplicate"))


def test_extract_params_applies_strict_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(valid_params(ph="7.4"), ensure_ascii=False),
                }
            }
        ]
    }
    response = types.SimpleNamespace(read=lambda: json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *args, **kwargs: response)
    with pytest.raises(ValueError, match="JSON 数字"):
        agent.extract_params("fixture")


def test_web_structured_values_override_llm_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_99")
    monkeypatch.setenv("NPFLOW_REQUESTED_HERBS_JSON", '["Herb A", "Herb B"]')
    monkeypatch.setenv("NPFLOW_REQUESTED_DISEASE", "Validated Disease")
    monkeypatch.setenv("NPFLOW_REQUESTED_PH", "6.8")
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_TARGETS_JSON", '["egfr", "AKT1"]')
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON", '["aspirin"]')
    llm = valid_params(
        herbs={"malformed": True},
        disease="LLM changed disease",
        mode="full",
        ph="malicious; value",
        extra_targets={"malformed": True},
        extra_ligands={"malformed": True},
    )
    validated = agent.validate_pipeline_params(agent.apply_requested_mode_contract(llm))
    assert validated["herbs"] == ["Herb A", "Herb B"]
    assert validated["disease"] == "Validated Disease"
    assert validated["ph"] == 6.8
    assert validated["mode"] == "network"
    assert validated["extra_targets"] == ["EGFR", "AKT1"]
    assert validated["extra_ligands"] == ["aspirin"]


def test_web_structured_extras_are_required_and_must_be_json_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_99")
    monkeypatch.setenv("NPFLOW_REQUESTED_HERBS_JSON", '["Herb A"]')
    monkeypatch.setenv("NPFLOW_REQUESTED_DISEASE", "Disease X")
    monkeypatch.setenv("NPFLOW_REQUESTED_PH", "7.4")
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    with pytest.raises(agent.PipelineContractError, match="EXTRA_TARGETS"):
        agent.apply_requested_mode_contract(valid_params())

    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_TARGETS_JSON", '"EGFR"')
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON", "[]")
    with pytest.raises(ValueError, match="NPFLOW_REQUESTED_EXTRA_TARGETS_JSON"):
        agent.apply_requested_mode_contract(valid_params())


def test_run_accepts_only_argv_and_never_enables_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    argv = ["python", "path with ' quote.py", "--ph", "7.4; AUDIT_SENTINEL"]
    assert agent.run(argv) == "ok"
    assert captured["args"][0] == argv
    assert "shell" not in captured["kwargs"]
    with pytest.raises(TypeError, match="argv"):
        agent.run("python unsafe.py")


def test_network_marker_path_is_contained_and_supports_quotes(
    isolated_roots: Path,
) -> None:
    safe = agent.NETWORK_RUNS_ROOT / "run with space and ' quote"
    safe.mkdir()
    marker = f"noise\nNPFLOW_NETWORK_RUN_DIR={safe}\n"
    assert Path(agent.parse_network_run_dir(marker)) == safe.resolve()

    outside = isolated_roots / "outside"
    outside.mkdir()
    with pytest.raises(agent.PipelineContractError, match="越界"):
        agent.parse_network_run_dir(f"NPFLOW_NETWORK_RUN_DIR={outside}")


def test_network_manager_receives_structured_argv_without_reparsing_fields(
    isolated_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=2, stdout="", stderr="fixture stop")

    monkeypatch.setattr(agent, "_captured_command", fake_run)
    entity = "Herb 疾病=wrong, disease=also-wrong"
    disease = "True Disease disease=still-data"
    with pytest.raises(RuntimeError, match="异常退出"):
        agent.run_network_pharm(entity, None, disease, context={"task_id": "task_8", "run_id": "8" * 32})

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--entity-name") + 1] == entity
    assert argv[argv.index("--disease-name") + 1] == disease
    assert "input" not in captured["kwargs"]


def test_manager_run_and_config_markers_are_bound_without_using_wrong_latest(
    isolated_roots: Path,
) -> None:
    run_name = "Herb_A_Disease_X_unique_run"
    source_run = agent.NETWORK_RUNS_ROOT / run_name
    source_run.mkdir()
    config_root = isolated_roots / "Network Pharmacology" / "config" / "generated"
    config_root.mkdir(parents=True)
    correct_config = config_root / f"{run_name}.yaml"
    correct_config.write_text("fixture: correct\n", encoding="utf-8")
    wrong_config = config_root / "different_run.yaml"
    wrong_config.write_text("fixture: wrong\n", encoding="utf-8")
    agent._atomic_write_json(
        Path(agent.NP_LATEST),
        {"output_dir": str(source_run), "config_path": str(wrong_config), "stale_key": "reject"},
    )

    output = (
        f"NPFLOW_NETWORK_CONFIG_PATH={correct_config.resolve()}\n"
        f"NPFLOW_NETWORK_RUN_DIR={source_run.resolve()}\n"
    )
    parsed_run, parsed_config = agent.parse_network_manager_contract(output)
    assert Path(parsed_run) == source_run.resolve()
    assert Path(parsed_config) == correct_config.resolve()

    context = {"task_id": "task_41", "run_id": "4" * 32}
    agent._atomic_write_json(
        source_run / agent.RUN_BINDING_FILENAME,
        {
            "schema_version": agent.PIPELINE_SCHEMA_VERSION,
            "kind": "npflow_network_run_binding",
            "task_id": context["task_id"],
            "run_id": context["run_id"],
            "stage": "network",
            "status": "provisional",
            "output_dir": str(source_run.resolve()),
        },
    )
    claimed = agent._claim_manager_run(
        parsed_run,
        parsed_config,
        context,
        entity_kind="herb",
        entity="Herb A",
        disease="Disease X",
        source_index=1,
    )
    assert claimed == source_run.resolve()
    assert (claimed / "run_config.yaml").read_text(encoding="utf-8") == "fixture: correct\n"
    assert not correct_config.exists()
    binding = json.loads((claimed / agent.RUN_BINDING_FILENAME).read_text(encoding="utf-8"))
    assert binding["status"] == "claimed"
    assert binding["output_dir"] == str(claimed)
    assert [path.resolve() for path in agent.NETWORK_RUNS_ROOT.iterdir() if path.is_dir()] == [claimed]
    latest = json.loads(Path(agent.NP_LATEST).read_text(encoding="utf-8"))
    assert latest["config_path"] == str(claimed / "run_config.yaml")
    assert "stale_key" not in latest

    fallback_run = agent.NETWORK_RUNS_ROOT / "fallback_unique_run"
    fallback_run.mkdir()
    fallback_config = config_root / "fallback_unique_run.yaml"
    fallback_config.write_text("fixture: fallback\n", encoding="utf-8")
    fallback_parsed = agent.parse_network_manager_contract(
        f"NPFLOW_NETWORK_RUN_DIR={fallback_run.resolve()}\n"
    )
    assert Path(fallback_parsed[1]) == fallback_config.resolve()


def test_manager_contract_rejects_mismatched_config_marker(isolated_roots: Path) -> None:
    run_dir = agent.NETWORK_RUNS_ROOT / "expected_run"
    run_dir.mkdir()
    config_root = isolated_roots / "Network Pharmacology" / "config" / "generated"
    config_root.mkdir(parents=True)
    wrong_config = config_root / "other_run.yaml"
    wrong_config.write_text("fixture: wrong\n", encoding="utf-8")
    with pytest.raises(agent.PipelineContractError, match="不属于同一次"):
        agent.parse_network_manager_contract(
            f"NPFLOW_NETWORK_CONFIG_PATH={wrong_config.resolve()}\n"
            f"NPFLOW_NETWORK_RUN_DIR={run_dir.resolve()}\n"
        )


def test_blank_inchikey_rows_remain_distinct_and_duplicate_cid_keeps_all_herbs(
    tmp_path: Path,
) -> None:
    fields = ["inchikey", "pubchem_cid", "ligand_id", "canonical_smiles", "ligand_score"]
    first = tmp_path / "a" / "ligands.csv"
    second = tmp_path / "b" / "ligands.csv"
    write_csv(
        first,
        fields,
        [
            {"inchikey": "", "pubchem_cid": "", "ligand_id": "L1", "canonical_smiles": "", "ligand_score": "0.6"},
            {"inchikey": "", "pubchem_cid": "123", "ligand_id": "A-CID", "canonical_smiles": "", "ligand_score": "0.7"},
        ],
    )
    write_csv(
        second,
        fields,
        [
            {"inchikey": "", "pubchem_cid": "", "ligand_id": "L2", "canonical_smiles": "", "ligand_score": "0.8"},
            {"inchikey": "", "pubchem_cid": "123", "ligand_id": "B-CID", "canonical_smiles": "", "ligand_score": "0.9"},
        ],
    )
    output = tmp_path / "task-owned" / "merged.csv"
    agent.merge_csvs(
        [str(first), str(second)],
        "inchikey",
        "ligand_score",
        output_path=output,
        source_labels={str(first.resolve()): "Herb A", str(second.resolve()): "Herb B"},
    )
    rows = list(csv.DictReader(output.open(encoding="utf-8", newline="")))
    assert {row["ligand_id"] for row in rows} == {"L1", "L2", "B-CID"}
    duplicate = next(row for row in rows if row["pubchem_cid"] == "123")
    assert duplicate["merge_identity_basis"] == "pubchem_cid"
    assert duplicate["formula_source_herbs"] == "Herb A; Herb B"
    assert not list(tmp_path.glob("_merged_*"))


def test_merge_identity_preserves_stereoisomers_without_inchikey_or_cid(tmp_path: Path) -> None:
    fields = [
        "inchikey",
        "pubchem_cid",
        "ligand_id",
        "canonical_smiles",
        "isomeric_smiles",
        "ligand_score",
    ]
    first = tmp_path / "a" / "ligands.csv"
    second = tmp_path / "b" / "ligands.csv"
    write_csv(
        first,
        fields,
        [{
            "inchikey": "",
            "pubchem_cid": "",
            "ligand_id": "stereo-a",
            "canonical_smiles": "CC(O)C(=O)O",
            "isomeric_smiles": "C[C@H](O)C(=O)O",
            "ligand_score": "0.7",
        }],
    )
    write_csv(
        second,
        fields,
        [{
            "inchikey": "",
            "pubchem_cid": "",
            "ligand_id": "stereo-b",
            "canonical_smiles": "CC(O)C(=O)O",
            "isomeric_smiles": "C[C@@H](O)C(=O)O",
            "ligand_score": "0.8",
        }],
    )

    output = tmp_path / "task-owned" / "merged.csv"
    agent.merge_csvs([str(first), str(second)], "inchikey", "ligand_score", output_path=output)

    rows = list(csv.DictReader(output.open(encoding="utf-8", newline="")))
    assert len(rows) == 2
    assert {row["ligand_id"] for row in rows} == {"stereo-a", "stereo-b"}
    assert {row["merge_identity_basis"] for row in rows} == {"isomeric_smiles"}


def test_user_exceptions_do_not_forge_scores_and_unresolved_ligands_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path = tmp_path / "network_run"

    def fake_lookup(entry: str) -> dict:
        if entry == "resolved":
            return {
                "ligand_id": "PubChem:1",
                "compound_name": "Resolved",
                "canonical_smiles": "CCO",
                "pubchem_cid": "1",
                "inchikey": "",
                "source_list": "user_requested_pubchem",
            }
        return {"compound_name": entry, "selection_reason": "user_requested_unresolved_structure"}

    monkeypatch.setattr(agent, "fetch_pubchem_ligand", fake_lookup)
    agent.append_user_requested_ligands(run_path, ["resolved", "unresolved"])
    ligand_rows = list(
        csv.DictReader(
            (run_path / "07_core_targets" / "docking_ligands.csv").open(encoding="utf-8", newline="")
        )
    )
    assert len(ligand_rows) == 1
    assert ligand_rows[0]["contract_version"] == "docking_ligands.v2"
    assert ligand_rows[0]["selection_status"] == "selected_exception"
    assert ligand_rows[0]["selection_reason"] == "explicit_user_request_not_score_qualified"
    assert ligand_rows[0]["ligand_score"] == ""
    rejected = list(
        csv.DictReader(
            (run_path / "07_core_targets" / "rejected_user_requested_ligands.csv").open(
                encoding="utf-8", newline=""
            )
        )
    )
    assert rejected == [
        {
            "requested_name": "unresolved",
            "resolution_status": "rejected",
            "rejection_reason": "pubchem_structure_not_resolved",
        }
    ]

    agent.append_user_requested_targets(run_path, ["egfr"])
    target_rows = list(
        csv.DictReader(
            (run_path / "07_core_targets" / "docking_targets.csv").open(encoding="utf-8", newline="")
        )
    )
    assert target_rows[0]["target_gene"] == "EGFR"
    assert target_rows[0]["selection_status"] == "selected_exception"
    assert target_rows[0]["target_score"] == ""


def test_formula_merge_is_unique_atomic_task_bound_and_has_aggregate_qc(
    isolated_roots: Path,
) -> None:
    context = {"task_id": "task_7", "run_id": "a" * 32}
    first = make_bound_network_run(isolated_roots, context, "source_a", "Herb A", ligand_id="L1")
    second = make_bound_network_run(isolated_roots, context, "source_b", "Herb B", ligand_id="L2")

    merged = Path(
        agent.merge_network_runs(
            [str(first), str(second)],
            "Formula One",
            "Disease X",
            context=context,
            source_entities=["Herb A", "Herb B"],
        )
    )
    assert merged.parent == agent.NETWORK_RUNS_ROOT
    assert "task_7" in merged.name and context["run_id"][:12] in merged.name
    assert not list(agent.NETWORK_RUNS_ROOT.glob("*.staging"))
    binding = json.loads((merged / agent.RUN_BINDING_FILENAME).read_text(encoding="utf-8"))
    assert binding["task_id"] == "task_7" and binding["run_id"] == context["run_id"]
    qc = json.loads((merged / "08_report" / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["module"] == "network_pharmacology_formula_candidate_union"
    assert qc["scope"] == "candidate_union_only"
    assert qc["source_run_count"] == 2
    ligand_rows = list(
        csv.DictReader((merged / "07_core_targets" / "docking_ligands.csv").open(encoding="utf-8", newline=""))
    )
    assert {row["formula_source_herbs"] for row in ligand_rows} == {"Herb A", "Herb B"}
    latest = json.loads(Path(agent.NP_LATEST).read_text(encoding="utf-8"))
    assert Path(latest["output_dir"]) == merged


def test_pipeline_tracker_persists_explicit_no_result_in_task_directory(
    isolated_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_11")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", "server-issued-run-id-000011")
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    tracker = agent.PipelineTracker(agent.validate_pipeline_params(valid_params()))
    tracker.start("network")
    tracker.finish("network", "no_result", reason="valid_empty_fixture")
    tracker.start("packaging")
    agent._atomic_write_text(tracker.result_dir / "说明书.txt", "fixture\n")
    tracker.finish(
        "packaging",
        "completed",
        output_dir=tracker.result_dir,
        required_artifacts=["说明书.txt"],
        artifact_paths=["说明书.txt"],
    )
    assert tracker.complete() == "no_result"
    status_path = agent.WEB_RESULTS_ROOT / "task_11" / agent.PIPELINE_STATUS_FILENAME
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "no_result"
    assert payload["stages"]["network"]["status"] == "no_result"
    assert payload["stages"]["packaging"]["artifacts"][0]["path"] == "说明书.txt"


def test_completed_manifest_matches_web_contract(
    isolated_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webapp import server as web

    monkeypatch.setattr(web, "ROOT", isolated_roots)
    run_id = "server-issued-run-id-000013"
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_13")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", run_id)
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    tracker = agent.PipelineTracker(agent.validate_pipeline_params(valid_params()))
    tracker.start("network")
    tracker.finish("network", "completed", reason="fixture_completed")
    network_dest = tracker.result_dir / "01_network_pharmacology"
    network_dest.mkdir()
    agent._atomic_write_json(network_dest / "qc_report.json", {"status": "passed"})
    tracker.bind_packaged_stage(
        "network",
        "01_network_pharmacology",
        ["01_network_pharmacology/qc_report.json"],
    )
    tracker.start("packaging")
    agent._atomic_write_text(tracker.result_dir / "说明书.txt", "fixture\n")
    tracker.finish(
        "packaging",
        "completed",
        output_dir=tracker.result_dir,
        required_artifacts=["说明书.txt"],
        artifact_paths=["说明书.txt"],
    )
    tracker.complete()

    status, reason, stages = web.validate_pipeline_status(
        {"id": 13, "run_id": run_id, "requested_mode": "network"},
        tracker.result_dir,
    )
    assert status == "completed", reason
    assert stages["network"]["status"] == "completed"


def test_pipeline_exception_is_failed_not_completed(
    isolated_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_12")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", "server-issued-run-id-000012")
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    monkeypatch.setenv("NPFLOW_REQUESTED_HERBS_JSON", '["Herb A"]')
    monkeypatch.setenv("NPFLOW_REQUESTED_DISEASE", "Disease X")
    monkeypatch.setenv("NPFLOW_REQUESTED_PH", "7.4")
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_TARGETS_JSON", "[]")
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON", "[]")
    monkeypatch.setattr(
        agent,
        "run_network_pharm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    with pytest.raises(RuntimeError, match="fixture failure"):
        agent.run_pipeline(valid_params())
    status_path = agent.WEB_RESULTS_ROOT / "task_12" / agent.PIPELINE_STATUS_FILENAME
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["stages"]["network"]["status"] == "failed"
    assert payload["stages"]["packaging"]["status"] == "skipped"


def test_network_pipeline_end_to_end_manifest_is_accepted_by_web(
    isolated_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webapp import server as web

    monkeypatch.setattr(web, "ROOT", isolated_roots)
    run_id = "server-issued-run-id-000014"
    monkeypatch.setenv("NPFLOW_WEB_TASK_ID", "task_14")
    monkeypatch.setenv("NPFLOW_WEB_RUN_ID", run_id)
    monkeypatch.setenv("NPFLOW_REQUESTED_MODE", "network")
    monkeypatch.setenv("NPFLOW_REQUESTED_HERBS_JSON", '["Herb A"]')
    monkeypatch.setenv("NPFLOW_REQUESTED_DISEASE", "Disease X")
    monkeypatch.setenv("NPFLOW_REQUESTED_PH", "7.4")
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_TARGETS_JSON", "[]")
    monkeypatch.setenv("NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON", "[]")
    context = {"task_id": "task_14", "run_id": run_id}
    network_run = make_bound_network_run(
        isolated_roots,
        context,
        "pipeline_source",
        "Herb A",
        ligand_id="L1",
    )
    monkeypatch.setattr(agent, "run_network_pharm", lambda *args, **kwargs: str(network_run))

    payload = agent.run_pipeline(valid_params())
    assert payload["status"] == "completed"
    assert payload["run_id"] == run_id
    assert payload["stages"]["packaging"]["requested"] is False
    assert payload["stages"]["network"]["required_artifacts"] == [
        "01_network_pharmacology/qc_report.json"
    ]
    status, reason, _ = web.validate_pipeline_status(
        {"id": 14, "run_id": run_id, "requested_mode": "network"},
        agent.WEB_RESULTS_ROOT / "task_14",
    )
    assert status == "completed", reason


def test_network_binding_cannot_be_reused_by_another_task(isolated_roots: Path) -> None:
    owner = {"task_id": "task_21", "run_id": "1" * 32}
    run_dir = make_bound_network_run(isolated_roots, owner, "owned", "Herb A", ligand_id="L1")
    with pytest.raises(agent.PipelineContractError, match="不属于当前"):
        agent._verify_network_binding(run_dir, {"task_id": "task_22", "run_id": "2" * 32})


def test_building_formula_binding_is_cleanup_only_until_claimed(isolated_roots: Path) -> None:
    owner = {"task_id": "task_22", "run_id": "2" * 32}
    run_dir = agent.NETWORK_RUNS_ROOT / "formula-building"
    run_dir.mkdir()
    agent._atomic_write_json(
        run_dir / agent.RUN_BINDING_FILENAME,
        agent._network_binding_payload(
            owner,
            entity_kind="formula",
            entity="Herb A+Herb B",
            disease="Disease X",
            source_index=0,
            output_dir=run_dir,
            status="building",
        ),
    )
    with pytest.raises(agent.PipelineContractError, match="尚未完成原子提交"):
        agent._verify_network_binding(run_dir, owner)


@pytest.mark.parametrize("field,bad_value", [("schema_version", 2), ("stage", "docking")])
def test_network_binding_rejects_wrong_schema_or_stage(
    isolated_roots: Path, field: str, bad_value: object
) -> None:
    owner = {"task_id": "task_23", "run_id": "3" * 32}
    run_dir = make_bound_network_run(isolated_roots, owner, f"bad_{field}", "Herb A", ligand_id="L1")
    manifest_path = run_dir / agent.RUN_BINDING_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = bad_value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(agent.PipelineContractError, match="类型无效"):
        agent._verify_network_binding(run_dir, owner)


def test_network_contract_distinguishes_valid_empty_from_missing_qc(isolated_roots: Path) -> None:
    empty = agent.NETWORK_RUNS_ROOT / "valid_empty"
    (empty / "08_report").mkdir(parents=True)
    agent._atomic_write_json(
        empty / "08_report" / "qc_report.json",
        {"status": "passed", "workflow_status": "passed", "counts": {"intersection_targets": 0}},
    )
    assert agent.classify_network_run(empty)[0] == "no_result"

    missing = agent.NETWORK_RUNS_ROOT / "missing_qc"
    missing.mkdir()
    with pytest.raises(agent.PipelineContractError, match="缺少必要产物"):
        agent.classify_network_run(missing)

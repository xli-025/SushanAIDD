"""Small portable evidence bundle built only from already-owned run directories."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


def collect_evidence(run_root: Path, dock_root: Path | None, reverse_root: Path | None, destination: Path) -> dict:
    evidence = destination / "evidence"
    files = evidence / "files"
    files.mkdir(parents=True, exist_ok=True)
    candidates = []
    if dock_root:
        for name in ("00_input_manifest", "01_target_structures/identity_evidence", "02_receptor_prepared",
                     "03_ligand_prepared", "04_binding_sites", "06_scores", "smoke_vina"):
            directory = dock_root / name
            if directory.is_dir():
                candidates.extend(p for p in directory.rglob("*") if p.is_file())
        for name in ("selected_receptors.csv", "target_structure_selection_qc.csv"):
            candidates.append(dock_root / "01_target_structures" / name)
    if reverse_root:
        candidates.extend(p for p in reverse_root.rglob("*") if p.is_file() and (
            p.suffix.lower() in {".pdb", ".pdbqt", ".sdf", ".json", ".log"} or
            p.name in {"config.txt", "selected_for_vina.csv", "generated_raw.csv", "generated_filtered.csv", "redocking_results.csv", "result.csv"}))
    candidates.extend(run_root / name for name in ("npflow_run_manifest.json", "run_config.yaml", "08_report/qc_report.json"))
    index, paths = [], {}
    for source in sorted(set(candidates)):
        if not source.is_file():
            continue
        resolved = source.resolve(strict=True)
        if source.is_symlink() or not resolved.is_relative_to(run_root.resolve(strict=True)):
            raise ValueError(f"Evidence path escapes owned run: {source}")
        if source.suffix.lower() not in {".pdb", ".pdbqt", ".sdf", ".json", ".csv", ".log", ".txt", ".yaml"}:
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        target = files / (digest + source.suffix.lower())
        if not target.exists():
            shutil.copy2(source, target)
        relative = target.relative_to(evidence).as_posix()
        paths[str(source.resolve())] = relative
        index.append({"source_relative_to_run": source.relative_to(run_root).as_posix(),
                      "path": relative, "sha256": digest, "size": source.stat().st_size})
    replay = []
    for source in sorted(set(candidates)):
        if source.name != "config.txt" or not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        lines, portable = [], True
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"receptor", "ligand"}:
                mapped = paths.get(str(Path(value.strip()).resolve()))
                if not mapped:
                    portable = False
                    break
                line = f"{key.strip()} = {mapped}"
            elif sep and key.strip() == "out":
                line = "out = replay_pose.pdbqt"
            lines.append(line)
        if portable:
            name = f"replay_{len(replay) + 1:03d}.txt"
            (evidence / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
            replay.append({"config": name, "source_relative_to_run": source.relative_to(run_root).as_posix()})
    manifest = {"contract_version": "npflow_evidence.v1", "files": index, "replay_configs": replay,
                "unique_files": len({item["path"] for item in index}), "run_identity_source": "npflow_run_manifest.json"}
    (evidence / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence / "README.txt").write_text(
        "Evidence files are content-addressed and shared only once. index.json maps original run-relative names.\n"
        "To replay docking offline: unpack ZIP, cd evidence, use the recorded Vina version and run vina --config replay_NNN.txt.\n"
        "No model, virtualenv or database cache is bundled. Old result tables may retain original paths; use index.json.\n"
        "Scores express computational screening priority, not measured efficacy. See candidate assembly and comparison fields.\n", encoding="utf-8")
    return manifest

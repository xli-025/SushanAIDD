from __future__ import annotations

import argparse
import csv
import copy
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = "results/network_pharmacology"
RUNS_BASE_DIR = "results/network_pharmacology_runs"
RUN_BINDING_FILENAME = "npflow_run_manifest.json"

COMPOUND_ALIASES = {
    "补骨脂素": "psoralen",
}

DISEASE_ALIASES = {
    "肝毒性": "hepatotoxicity",
    "肝病": "liver disease",
}


def clean_text(value: str) -> str:
    return (
        value.strip()
        .strip('"')
        .strip("'")
        .strip("“")
        .strip("”")
        .strip("。")
        .strip()
    )


def normalize_disease_name(name: str) -> str:
    return DISEASE_ALIASES.get(name.strip(), name.strip())


def normalize_compound_name(name: str) -> str:
    return COMPOUND_ALIASES.get(name.strip(), name.strip())


def parse_query(query: str) -> tuple[str | None, str | None, str | None]:
    """
    支持这些输入：
    1. herb_name=黄芩, disease_name=hepatocellular carcinoma
    2. 中药=黄芩，疾病=hepatocellular carcinoma
    3. 黄芩 + hepatocellular carcinoma
    4. compound_name=psoralen, disease_name=hepatotoxicity
    5. 化合物=补骨脂素，疾病=肝毒性
    """
    text = query.strip()

    herb_match = re.search(
        r"(?:herb_name|herb|中药|草药)\s*[=:：]\s*([^,，\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    compound_match = re.search(
        r"(?:compound_name|compound|化合物|单体|成分)\s*[=:：]\s*([^,，\n]+)",
        text,
        flags=re.IGNORECASE,
    )

    disease_match = re.search(
        r"(?:disease_name|disease|疾病)\s*[=:：]\s*(.+)$",
        text,
        flags=re.IGNORECASE,
    )

    if compound_match and disease_match:
        compound_name = normalize_compound_name(clean_text(compound_match.group(1)))
        disease_name = normalize_disease_name(clean_text(disease_match.group(1)))
        return "compound", compound_name, disease_name

    if herb_match and disease_match:
        herb_name = clean_text(herb_match.group(1))
        disease_name = normalize_disease_name(clean_text(disease_match.group(1)))
        return "herb", herb_name, disease_name

    if "+" in text:
        left, right = text.split("+", 1)
        left = clean_text(left)
        mode = "compound" if left in COMPOUND_ALIASES else "herb"
        entity_name = normalize_compound_name(left) if mode == "compound" else left
        return mode, entity_name, normalize_disease_name(clean_text(right))

    return None, None, None


def safe_project_name(entity_name: str, disease_name: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_suffix = uuid4().hex[:10]
    raw = f"{entity_name}_{disease_name}_{timestamp}_{run_suffix}"
    safe = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", raw).strip("_")
    return safe or f"npflow_run_{timestamp}"


def write_params_yaml(input_mode: str, entity_name: str, disease_name: str) -> Path:
    config_dir = PROJECT_ROOT / "config" / "generated"
    config_dir.mkdir(parents=True, exist_ok=True)

    project_name = safe_project_name(entity_name, disease_name)
    config_path = config_dir / f"{project_name}.yaml"
    output_dir = f"{RUNS_BASE_DIR}/{project_name}"
    input_section = {
        "mode": input_mode,
        "disease_name": disease_name,
    }
    if input_mode == "compound":
        input_section["compound_name"] = entity_name
    else:
        input_section["herb_name"] = entity_name

    default_config_path = PROJECT_ROOT / "config" / "params.yaml"
    with default_config_path.open("r", encoding="utf-8") as f:
        default_params = yaml.safe_load(f)
    if not isinstance(default_params, dict):
        raise ValueError(f"Default configuration must be an object: {default_config_path}")
    params = copy.deepcopy(default_params)
    params["schema_version"] = str(params.get("schema_version") or "npflow.network.v2")
    params["project"] = {
        **params.get("project", {}),
        "name": project_name,
        "organism": "Homo sapiens",
        "taxon_id": 9606,
    }
    params["input"] = input_section
    params["output"] = {**params.get("output", {}), "dir": output_dir}
    params["config_provenance"] = {
        "base_config": str(default_config_path),
        "base_schema_version": params["schema_version"],
        "override_fields": ["project.name", "input", "output.dir"],
        "generated_at": datetime.now().isoformat(),
    }

    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(params, f, allow_unicode=True, sort_keys=False)

    return config_path


def read_output_dir_from_config(config_path: Path) -> Path:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    output_dir = data.get("output", {}).get("dir", DEFAULT_OUTPUT_DIR)
    return PROJECT_ROOT / output_dir


def write_provisional_web_binding(output_dir: Path) -> bool:
    """Declare a Web-owned run before child work starts so failed runs remain cleanable."""
    task_id = os.environ.get("NPFLOW_WEB_TASK_ID", "").strip()
    run_id = os.environ.get("NPFLOW_WEB_RUN_ID", "").strip()
    if not task_id and not run_id:
        return False
    if not re.fullmatch(r"task_[1-9][0-9]*", task_id):
        raise ValueError(f"Invalid NPFLOW_WEB_TASK_ID: {task_id!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", run_id):
        raise ValueError("Invalid NPFLOW_WEB_RUN_ID")
    output_dir = output_dir.absolute()
    output_root = (PROJECT_ROOT / RUNS_BASE_DIR).resolve(strict=False)
    resolved = output_dir.resolve(strict=False)
    if resolved.parent != output_root or resolved.is_symlink():
        raise ValueError(f"Web network output directory must be a direct child of {output_root}")
    created = False
    temporary: Path | None = None
    payload = {
        "schema_version": 1,
        "kind": "npflow_network_run_binding",
        "task_id": task_id,
        "run_id": run_id,
        "stage": "network",
        "status": "provisional",
        "output_dir": str(resolved),
        "created_at": datetime.now().isoformat(),
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        created = True
        resolved = output_dir.resolve(strict=True)
        manifest = resolved / RUN_BINDING_FILENAME
        temporary = resolved / f".{RUN_BINDING_FILENAME}.{uuid4().hex}.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, manifest)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if created and output_dir.is_dir() and not output_dir.is_symlink():
            try:
                output_dir.rmdir()
            except OSError:
                # A child workflow may have started writing; leave the
                # directory for manifest-based cleanup instead of recursing.
                pass
    return True


def write_latest_run(output_dir: Path, config_path: Path, input_mode: str, entity_name: str, disease_name: str) -> None:
    latest_path = PROJECT_ROOT / RUNS_BASE_DIR / "latest_run.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "output_dir": str(output_dir),
        "config_path": str(config_path),
        "input_mode": input_mode,
        "entity_name": entity_name,
        "herb_name": entity_name if input_mode == "herb" else "",
        "compound_name": entity_name if input_mode == "compound" else "",
        "disease_name": disease_name,
        "updated_at": datetime.now().isoformat(),
    }
    with latest_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_csv_preview(path: Path, max_rows: int = 8) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
        return rows


def print_file_status(path: Path, started_at: float) -> None:
    if not path.exists():
        print(f"❌ 未找到文件：{path}")
        return

    mtime = path.stat().st_mtime
    status = "✅ 本次已刷新" if mtime >= started_at else "⚠️ 文件存在，但不是本次刷新"
    readable_time = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{status}：{path}")
    print(f"   修改时间：{readable_time}")


def run_workflow(input_mode: str, entity_name: str, disease_name: str) -> None:
    started_at = time.time()
    print(f"NPFLOW_MANAGER_VERSION=disease-compat-v2; file={Path(__file__).resolve()}", flush=True)

    config_path = write_params_yaml(input_mode, entity_name, disease_name)
    print("\n已生成配置文件：")
    print(config_path)

    output_dir = read_output_dir_from_config(config_path)
    write_provisional_web_binding(output_dir)

    cmd = [
        sys.executable,
        "modules/network_pharmacology/agent.py",
        "run",
        "--config",
        str(config_path),
    ]

    print("\n开始强制调用你的网络药理学工作流：")
    print(" ".join(cmd))
    print()

    process = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)

    returncode = process.wait()

    docking_path = output_dir / "07_core_targets" / "docking_targets.csv"
    docking_ligands_path = output_dir / "07_core_targets" / "docking_ligands.csv"
    qc_path = output_dir / "08_report" / "qc_report.json"
    report_md_path = output_dir / "08_report" / "network_pharmacology_report.md"
    report_html_path = output_dir / "08_report" / "network_pharmacology_report.html"

    print("\n\n========== 运行结果检查 ==========")
    print(f"退出码：{returncode}")

    print_file_status(docking_path, started_at)
    print_file_status(docking_ligands_path, started_at)
    print_file_status(qc_path, started_at)
    print_file_status(report_md_path, started_at)
    print_file_status(report_html_path, started_at)

    if returncode != 0:
        # Keep a failed Web-owned run self-contained so TTL cleanup removes
        # its configuration together with the module output directory.
        failed_config = output_dir / "run_config.yaml"
        if config_path.is_file() and not failed_config.exists():
            try:
                os.replace(config_path, failed_config)
            except OSError:
                pass
        qc = read_json(qc_path)
        if qc:
            print("\n========== 工作流失败解释 ==========")
            print(f"状态：{qc.get('workflow_status', qc.get('status', 'failed'))}")
            print(f"数据源状态：{qc.get('data_source_status', '')}")
            print(f"解释状态：{qc.get('interpretation_status', '')}")
            if qc.get("failure_reason"):
                print(f"失败原因：{qc.get('failure_reason')}")
            standardization = qc.get("herb_name_standardization") or {}
            if standardization.get("warning"):
                print(f"药名提示：{standardization.get('warning')}")
            if standardization.get("canonical_herb_name") and standardization.get("canonical_herb_name") != entity_name:
                print(f"建议确认后尝试标准名：{standardization.get('canonical_herb_name')}")
        diagnostic_path = output_dir / "01_ingredients" / "tcmsp_fetch_diagnostic.json"
        if diagnostic_path.exists():
            print("\n========== TCMSP 抓取诊断 ==========")
            print(f"诊断文件：{diagnostic_path}")
            diagnostic = read_json(diagnostic_path)
            if diagnostic:
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2)[:3000])
        print("\n❌ 工作流执行失败。上面的日志就是主要排错依据。")
        raise SystemExit(returncode)

    write_latest_run(output_dir, config_path, input_mode, entity_name, disease_name)
    print(f"NPFLOW_NETWORK_CONFIG_PATH={config_path.resolve()}")
    print(f"NPFLOW_NETWORK_RUN_DIR={output_dir}")

    qc = read_json(qc_path)
    docking_preview = read_csv_preview(docking_path)
    docking_ligands_preview = read_csv_preview(docking_ligands_path)

    print("\n========== QC 摘要 ==========")
    if qc:
        print(
            "关键状态："
            f" workflow={qc.get('workflow_status', qc.get('status', ''))},"
            f" data_source={qc.get('data_source_status', '')},"
            f" herb_match={qc.get('herb_match_status', '')},"
            f" disease_mapping={qc.get('disease_mapping_status', '')},"
            f" docking={qc.get('docking_readiness_status', '')},"
            f" interpretation={qc.get('interpretation_status', '')}"
        )
        if qc.get("herb_match_warning"):
            print(f"⚠️ 药名匹配警告：{qc.get('herb_match_warning')}")
        if qc.get("disease_mapping_warning"):
            print(f"⚠️ 疾病映射警告：{qc.get('disease_mapping_warning')}")
        if qc.get("docking_ligand_warning"):
            print(f"⚠️ Docking 配体覆盖警告：{qc.get('docking_ligand_warning')}")
        if qc.get("pathway_dominance_warning_text"):
            print(f"⚠️ 靶点家族/通路偏倚警告：{qc.get('pathway_dominance_warning_text')}")
        if qc.get("source_coverage_status") == "no_valid_source_coverage":
            print("❌ 数据源覆盖失败：TCMSP、HERB2、SymMap2 都没有找到可用于主网络的可靠成分-靶点关系。")
            standardization = qc.get("herb_name_standardization") or {}
            if standardization.get("warning"):
                print(f"⚠️ 药名标准化提示：{standardization.get('warning')}")
            if standardization.get("canonical_herb_name") and standardization.get("canonical_herb_name") != entity_name:
                print(f"建议确认药材后用标准名重跑：中药={standardization.get('canonical_herb_name')}，疾病={disease_name}")
        print(json.dumps(qc, ensure_ascii=False, indent=2)[:3000])
    else:
        print("没有读到 qc_report.json 内容。")

    print("\n========== docking_targets.csv 预览 ==========")
    if docking_preview:
        for i, row in enumerate(docking_preview, start=1):
            print(f"\n[{i}]")
            print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        print("没有读到 docking_targets.csv 内容，或文件为空。")

    print("\n========== docking_ligands.csv 预览 ==========")
    if docking_ligands_preview:
        for i, row in enumerate(docking_ligands_preview, start=1):
            print(f"\n[{i}]")
            print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        print("没有读到 docking_ligands.csv 内容，或文件为空。")

    print("\n✅ 如果上面显示 docking_targets.csv、docking_ligands.csv 和 qc_report.json 为“本次已刷新”，就说明已经真正接通并跑通。")


def structured_cli_values(
    input_mode: str | None,
    entity_name: str | None,
    disease_name: str | None,
) -> tuple[str, str, str] | None:
    values = (input_mode, entity_name, disease_name)
    if not any(value is not None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("--input-mode, --entity-name and --disease-name must be provided together")
    mode = str(input_mode).strip().lower()
    entity = str(entity_name).strip()
    disease = str(disease_name).strip()
    if mode not in {"herb", "compound"}:
        raise ValueError("--input-mode must be herb or compound")
    if not entity or not disease:
        raise ValueError("--entity-name and --disease-name cannot be empty")
    if any(ord(char) < 32 for value in (entity, disease) for char in value):
        raise ValueError("Structured input cannot contain control characters")
    if mode == "compound":
        entity = normalize_compound_name(entity)
    disease = normalize_disease_name(disease)
    return mode, entity, disease


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NPFlow network-pharmacology manager")
    parser.add_argument("--input-mode", choices=("herb", "compound"))
    parser.add_argument("--entity-name")
    parser.add_argument("--disease-name")
    args = parser.parse_args(argv)
    try:
        structured = structured_cli_values(args.input_mode, args.entity_name, args.disease_name)
    except ValueError as exc:
        parser.error(str(exc))
    if structured is not None:
        run_workflow(*structured)
        return

    print("NPFlow Manager 已启动。")
    print("当前版本：强制 Python 调用工作流，不依赖模型自由 tool calling。")
    print("输入示例 1：中药=黄芩，疾病=hepatocellular carcinoma")
    print("输入示例 2：黄芩 + hepatocellular carcinoma")
    print("输入示例 3：化合物=补骨脂素，疾病=肝毒性")
    print("退出请输入：exit\n")

    while True:
        query = input("user> ").strip()

        if query.lower() in {"exit", "quit", "q"}:
            break

        input_mode, entity_name, disease_name = parse_query(query)

        if not input_mode or not entity_name or not disease_name:
            print("\n没有解析到中药/化合物和疾病。请用下面任意一种格式：")
            print("中药=黄芩，疾病=hepatocellular carcinoma")
            print("化合物=补骨脂素，疾病=肝毒性")
            print("黄芩 + hepatocellular carcinoma\n")
            continue

        print(f"\n解析到输入：mode={input_mode}, entity={entity_name}, disease_name={disease_name}")
        run_workflow(input_mode, entity_name, disease_name)


if __name__ == "__main__":
    main()

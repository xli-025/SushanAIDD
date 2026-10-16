#!/usr/bin/env python3
"""NPFlow Agent — 自然语言驱动的全流程自动化总控。

用法：
    /Users/a502/npflow-agent/.venv/bin/python npflow_agent.py "用丹参和冠心病跑网药加对接"
    /Users/a502/npflow-agent/.venv/bin/python npflow_agent.py "黄连+diabetes 跑全流程"
    /Users/a502/npflow-agent/.venv/bin/python npflow_agent.py "只跑网药：金银花+influenza"
"""

import csv
import hashlib
import json
import math
import os
try:
    import readline  # noqa: F401 - optional terminal editing support on POSIX
except ImportError:  # pragma: no cover - Windows has no stdlib readline module.
    readline = None
import shutil
import shlex
import subprocess
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from npflow_runtime import remaining_timeout, run_logged, stage_budget
from npflow_evidence import collect_evidence

# ── 常量 ───────────────────────────────────────────────
PROJECT_ROOT = Path(os.environ.get("NPFLOW_ROOT", str(Path(__file__).resolve().parent)))
PYTHON = str(PROJECT_ROOT / ".venv/bin/python")
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = os.environ.get("NPFLOW_MODEL", "qwen3.5:latest")

NP_AGENT = str(PROJECT_ROOT / "Network Pharmacology/modules/manager_agent/qwen_npflow_agent.py")
NP_LATEST = str(PROJECT_ROOT / "Network Pharmacology/results/network_pharmacology_runs/latest_run.json")

DOCKING_AGENT = str(PROJECT_ROOT / "Molecular Docking/modules/docking/agent.py")
REVERSE_AGENT = str(PROJECT_ROOT / "Reverse Design/modules/reverse_design_after_docking/agent.py")

NETWORK_RUNS_ROOT = PROJECT_ROOT / "Network Pharmacology/results/network_pharmacology_runs"
WEB_RESULTS_ROOT = PROJECT_ROOT / "results/web_tasks"
RESULTS_ROOT = PROJECT_ROOT / "results"
PIPELINE_STATUS_FILENAME = "pipeline_status.json"
RUN_BINDING_FILENAME = "npflow_run_manifest.json"
PIPELINE_SCHEMA_VERSION = 1
PIPELINE_PARAM_FIELDS = {
    "herbs",
    "herb",  # documented compatibility alias for one legacy caller
    "compound",
    "disease",
    "mode",
    "ph",
    "extra_targets",
    "extra_ligands",
    "formula_name",
}

VALID_MODES = {"network", "network+docking", "docking", "docking+reverse", "reverse", "full"}
MODE_ALIASES = {
    "auto": "full",
    "network+dock": "network+docking",
    "network_docking": "network+docking",
    "network-docking": "network+docking",
    "docking_only": "docking",
    "reverse_only": "reverse",
}
MODE_STAGES = {
    "network": {"network"},
    "network+docking": {"network", "docking"},
    "docking": {"docking"},
    "docking+reverse": {"docking", "reverse"},
    "reverse": {"reverse"},
    "full": {"network", "docking", "reverse"},
}
STAGE_NAMES = ("network", "docking", "reverse", "packaging")
TERMINAL_STAGE_STATUSES = {"completed", "no_result", "skipped", "failed"}


class PipelineContractError(RuntimeError):
    """Raised when a stage output cannot satisfy its declared contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_mode(mode: str) -> str:
    if not isinstance(mode, str):
        raise ValueError("mode 必须是字符串枚举值")
    raw = mode.strip().lower()
    if not raw:
        raw = "full"
    normalized = MODE_ALIASES.get(raw, raw)
    if normalized not in VALID_MODES:
        allowed = ", ".join(sorted(VALID_MODES))
        raise ValueError(f"未知 mode={mode!r}；允许值：{allowed}")
    return normalized


def _validated_text(value: Any, field: str, *, allow_empty: bool = False, max_length: int = 300) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} 不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field} 过长（最多 {max_length} 字符）")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} 不能包含换行或控制字符")
    return text


def validate_ph(value: Any) -> float:
    """Validate pH from the model boundary; booleans/strings/NaN/Inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ph 必须是 JSON 数字，不能是字符串或布尔值")
    ph = float(value)
    if not math.isfinite(ph):
        raise ValueError("ph 必须是有限数值")
    if not 0.0 <= ph <= 14.0:
        raise ValueError("ph 必须位于 0.0 到 14.0 之间")
    return ph


def _validated_string_list(value: Any, field: str, *, uppercase: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是字符串数组")
    if len(value) > 100:
        raise ValueError(f"{field} 最多允许 100 项")
    cleaned: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _validated_text(item, f"{field}[{index}]", max_length=200)
        normalized = text.upper() if uppercase else text
        key = normalized.casefold()
        if key not in seen:
            cleaned.append(normalized)
            seen.add(key)
    return cleaned


def validate_pipeline_params(params: Any) -> dict[str, Any]:
    """Return a canonical, strictly typed copy of untrusted LLM parameters."""
    if not isinstance(params, dict):
        raise ValueError("模型输出的 JSON 顶层必须是对象")

    unknown = [key for key in params if key not in PIPELINE_PARAM_FIELDS]
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        raise ValueError(f"模型输出包含未声明字段：{rendered}")

    candidate = dict(params)
    legacy_herb = candidate.pop("herb", None)
    if legacy_herb is not None and "herbs" in candidate:
        raise ValueError("herb 与 herbs 不能同时提供")
    if "herbs" not in candidate and legacy_herb is not None:
        if not isinstance(legacy_herb, str):
            raise ValueError("旧格式 herb 必须是字符串")
        candidate["herbs"] = [legacy_herb] if legacy_herb.strip() else []

    herbs = _validated_string_list(candidate.get("herbs", []), "herbs")
    compound_value = candidate.get("compound")
    if compound_value is None:
        compound = None
    else:
        compound = _validated_text(compound_value, "compound", max_length=200)
    if herbs and compound:
        raise ValueError("herbs 与 compound 不能同时提供")
    if not herbs and not compound:
        raise ValueError("必须提供至少一味 herbs 或一个 compound")

    disease = _validated_text(candidate.get("disease"), "disease", max_length=200)
    mode = normalize_mode(candidate.get("mode", "full"))
    ph = validate_ph(candidate.get("ph", 7.4))
    extra_targets = _validated_string_list(candidate.get("extra_targets", []), "extra_targets", uppercase=True)
    extra_ligands = _validated_string_list(candidate.get("extra_ligands", []), "extra_ligands")

    formula_name = candidate.get("formula_name")
    if formula_name is not None:
        formula_name = _validated_text(formula_name, "formula_name", max_length=200)

    canonical = {
        "herbs": herbs,
        "compound": compound,
        "disease": disease,
        "mode": mode,
        "ph": ph,
        "extra_targets": extra_targets,
        "extra_ligands": extra_ligands,
    }
    if formula_name is not None:
        canonical["formula_name"] = formula_name
    return canonical


def apply_requested_mode_contract(params: Any) -> dict[str, Any]:
    """Override re-extracted values with the server-validated structured request."""
    if not isinstance(params, dict):
        raise ValueError("模型输出的 JSON 顶层必须是对象")
    if not os.environ.get("NPFLOW_WEB_TASK_ID", "").strip():
        return dict(params)

    missing = [
        name
        for name in (
            "NPFLOW_REQUESTED_HERBS_JSON",
            "NPFLOW_REQUESTED_DISEASE",
            "NPFLOW_REQUESTED_PH",
            "NPFLOW_REQUESTED_MODE",
            "NPFLOW_REQUESTED_EXTRA_TARGETS_JSON",
            "NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON",
        )
        if name not in os.environ
    ]
    if missing:
        raise PipelineContractError(f"Web 任务缺少结构化输入环境变量：{', '.join(missing)}")
    try:
        herbs_value = json.loads(os.environ["NPFLOW_REQUESTED_HERBS_JSON"])
    except json.JSONDecodeError as exc:
        raise PipelineContractError("NPFLOW_REQUESTED_HERBS_JSON 不是有效 JSON") from exc
    herbs = _validated_string_list(herbs_value, "NPFLOW_REQUESTED_HERBS_JSON")
    try:
        extra_targets_value = json.loads(os.environ["NPFLOW_REQUESTED_EXTRA_TARGETS_JSON"])
    except json.JSONDecodeError as exc:
        raise PipelineContractError("NPFLOW_REQUESTED_EXTRA_TARGETS_JSON 不是有效 JSON") from exc
    try:
        extra_ligands_value = json.loads(os.environ["NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON"])
    except json.JSONDecodeError as exc:
        raise PipelineContractError("NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON 不是有效 JSON") from exc
    extra_targets = _validated_string_list(
        extra_targets_value,
        "NPFLOW_REQUESTED_EXTRA_TARGETS_JSON",
        uppercase=True,
    )
    extra_ligands = _validated_string_list(
        extra_ligands_value,
        "NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON",
    )
    disease = _validated_text(
        os.environ["NPFLOW_REQUESTED_DISEASE"],
        "NPFLOW_REQUESTED_DISEASE",
        max_length=200,
    )
    raw_ph = os.environ["NPFLOW_REQUESTED_PH"].strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw_ph):
        raise PipelineContractError("NPFLOW_REQUESTED_PH 必须是有限十进制数")
    ph = validate_ph(float(raw_ph))
    requested = os.environ.get("NPFLOW_REQUESTED_MODE", "").strip().lower()
    if requested not in VALID_MODES | {"auto"}:
        raise PipelineContractError(f"Web 任务缺少合法 NPFLOW_REQUESTED_MODE：{requested!r}")
    canonical = dict(params)
    canonical.update(
        {
            "herbs": herbs,
            "compound": None,
            "disease": disease,
            "ph": ph,
            "extra_targets": extra_targets,
            "extra_ligands": extra_ligands,
        }
    )
    if requested != "auto":
        canonical["mode"] = normalize_mode(requested)
    return canonical


def _resolved_within(path: Path | str, root: Path | str, *, must_exist: bool = True, allow_root: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PipelineContractError(f"路径必须为绝对路径：{candidate}")
    try:
        resolved_root = Path(root).resolve(strict=True)
    except FileNotFoundError:
        resolved_root = Path(root).resolve(strict=False)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise PipelineContractError(f"路径不存在：{candidate}") from exc
    if (not allow_root and resolved == resolved_root) or resolved_root not in resolved.parents and resolved != resolved_root:
        raise PipelineContractError(f"路径越界：{resolved} 不属于 {resolved_root}")
    return resolved


def validate_network_run_dir(path: Path | str, *, must_exist: bool = True) -> Path:
    resolved = _resolved_within(path, NETWORK_RUNS_ROOT, must_exist=must_exist)
    if must_exist and not resolved.is_dir():
        raise PipelineContractError(f"网药 run 不是目录：{resolved}")
    return resolved


def validate_network_config_path(path: Path | str) -> Path:
    config_root = PROJECT_ROOT / "Network Pharmacology/config/generated"
    resolved = _resolved_within(path, config_root)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise PipelineContractError(f"网药配置不是非空普通文件：{resolved}")
    if resolved.suffix.lower() != ".yaml":
        raise PipelineContractError(f"网药配置扩展名必须是 .yaml：{resolved}")
    return resolved


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_write_csv_rows(path: Path, rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_copy(src: Path, dest: Path) -> None:
    if not src.is_file():
        raise PipelineContractError(f"待复制产物不是普通文件：{src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise PipelineContractError(f"缺少必要产物 {label}：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineContractError(f"必要产物 {label} 不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise PipelineContractError(f"必要产物 {label} 的 JSON 顶层必须是对象：{path}")
    return value


def _csv_row_count(path: Path) -> int:
    if not path.is_file() or path.stat().st_size <= 0:
        return 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            return sum(1 for row in reader if any(cell.strip() for cell in row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PipelineContractError(f"CSV 产物不可读：{path}") from exc


# ── NLP：调 Ollama 提取参数 ────────────────────────────
def _is_default_web_input(user_input: str, params: dict[str, Any]) -> bool:
    """Recognize only the server's no-notes full-workflow template.

    All captured values must agree with the validated structured fields. An
    unfamiliar template or any supplemental prose still needs mode inference.
    This also supports the existing Web launcher without restarting its service.
    """
    if "用户补充：" in user_input:
        return False
    match = re.fullmatch(
        r"中药=(?P<herbs>.+?)，疾病=(?P<disease>.+?)，pH=(?P<ph>[^，]+)，跑全流程"
        r"(?:，额外加入靶点/蛋白：(?P<targets>.+?))?"
        r"(?:，额外加入配体：(?P<ligands>.+?))?",
        user_input,
    )
    if match is None:
        return False
    fields = match.groupdict()

    def split_values(value: str | None, *, herbs: bool = False, uppercase: bool = False) -> list[str]:
        separator = r"[+,，、;；\r\n]+" if herbs else r"[,，、;；\r\n]+"
        values = [item.strip() for item in re.split(separator, value or "") if item.strip()]
        return _validated_string_list(values, "Web template", uppercase=uppercase)

    try:
        return (
            split_values(fields["herbs"], herbs=True) == params["herbs"]
            and fields["disease"] == params["disease"]
            and validate_ph(float(fields["ph"])) == params["ph"]
            and split_values(fields["targets"], uppercase=True) == params["extra_targets"]
            and split_values(fields["ligands"]) == params["extra_ligands"]
        )
    except ValueError:
        return False


def extract_params(user_input: str) -> dict:
    """Use validated Web fields directly; infer only unresolved natural language."""
    if os.environ.get("NPFLOW_WEB_TASK_ID", "").strip():
        # Validate before any network call, including when auto mode needs NLP.
        structured = validate_pipeline_params(apply_requested_mode_contract({"mode": "full"}))
        requested = os.environ["NPFLOW_REQUESTED_MODE"].strip().lower()
        if requested != "auto" or _is_default_web_input(user_input, structured):
            print(
                f"NPFLOW_INPUT_SOURCE=web_structured; requested_mode={requested}; "
                f"resolved_mode={structured['mode']}; ollama_skipped=true",
                flush=True,
            )
            return structured

    prompt = f"""你是中药网络药理学专家。从用户输入中提取以下信息，只返回 JSON，不要解释。

用户输入："{user_input}"

⚠️ 先移除用户输入中的语气词和动词前缀："跑"、"跑个"、"跑一下"、"帮我跑"、"用"、"做"、"分析"、"研究" ——这些不是药名。

⚠️ 复方识别：用户可能用逗号、顿号、"和"连接多味药，例如"黄芪、当归、赤芍"→ herbs:["黄芪","当归","赤芍"]。单味药也用 herbs 数组，长度=1。

疾病缩写展开：
- VaD → vascular dementia
- AD → Alzheimer's disease
- PD → Parkinson's disease
- CVD → cardiovascular disease
- NAFLD → non-alcoholic fatty liver disease
- HCC → hepatocellular carcinoma
- IBD → inflammatory bowel disease
- UC → ulcerative colitis
- RA → rheumatoid arthritis
- CAD → coronary artery disease
- T2DM → type 2 diabetes mellitus

中文疾病直接翻译为英文医学术语，例如：
- 感冒/流感 → influenza
- 普通感冒 → common cold
- 肝病/肝毒性 → hepatotoxicity / liver disease
- 冠心病 → coronary artery disease
- 糖尿病 → diabetes mellitus

其他常见中文也请翻译为英文医学术语。

返回格式：
{{"herbs": ["药1","药2"], "compound": null, "disease": "疾病英文全称", "mode": "full", "extra_targets": [], "extra_ligands": [], "ph": 7.4}}

ph: 对接 pH 值，默认 7.4。用户提到特定 pH 时才改，如"pH=2.0"、"pH 5.5"、"胃内pH=2"→2.0。
  没有明确说 pH 则始终填 7.4。必须是数字不是字符串。

extra_targets: 用户提到要额外加的靶点/蛋白/基因名（如 PKL、PIK3CA、EGFR），始终大写。用户说"蛋白名"、"靶点名"、"target"、"protein"都归入 extra_targets。没有则为[]。
extra_ligands: 用户提到要额外加的配体/小分子/化合物名（如 aspirin、quercetin），保留用户输入名称。没有则为[]。

mode 规则（严格遵守！）：
- "network"         → 用户说"只跑网药"、"只要网药"、"网药就够了"
- "network+docking"  → 用户说"不跑反向设计"、"不要反向设计"、"到对接为止"、"跑网药和对接"、"跑网药加分子对接"
- "docking"          → 用户说"只跑对接"
- "docking+reverse"  → 用户说"跑对接和反向"
- "reverse"          → 用户说"只跑反向"
- "full"             → 用户完全没限制，或只说"跑全流程"

⚠️ 关键：用户提到"不跑反向设计"、"不要反向" → 必须是 "network+docking"，不是 "full"！
"""


    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.1,
    }).encode()

    print(f"NPFLOW_INPUT_SOURCE=ollama; model={MODEL}; timeout_seconds=120", flush=True)
    try:
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        if not isinstance(resp, dict):
            raise ValueError("Ollama 响应顶层不是对象")
        choices = resp.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Ollama 响应缺少 choices[0]")
        msg = choices[0].get("message")
        if not isinstance(msg, dict):
            raise ValueError("Ollama 响应缺少 message 对象")
        raw = msg.get("content") or msg.get("reasoning") or ""
        if not isinstance(raw, str):
            raise ValueError("Ollama message 内容必须是字符串")
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            raise ValueError(
                "Ollama 参数解析等待超过 120 秒；可能是模型加载、推理耗时或服务繁忙，"
                "不代表服务未启动。尚未启动科学计算阶段。"
                "网页任务可明确选择任务模式；自动模式的补充说明不会被静默忽略。"
            ) from e
        raise ValueError(f"Ollama 请求失败，尚未启动科学计算阶段：{e}") from e
    except Exception as e:
        raise ValueError(f"Ollama 参数解析响应异常，尚未启动科学计算阶段：{e}") from e

    # R1 think 标签和中文格式兼容
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)
    if " response" in raw:
        raw = raw.split(" response")[-1]
    # 提取 JSON — 取最大的 {...} 块
    matches = list(re.finditer(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]+\}", raw, re.DOTALL))
    if matches:
        json_str = matches[-1].group()
        json_str = re.sub(r"```(?:json)?\s*", "", json_str)
        result = json.loads(json_str)
        if not isinstance(result, dict):
            raise ValueError("模型输出的 JSON 顶层必须是对象")
        for k in ("herb", "compound"):
            if result.get(k) in ("null", "Null", "NULL", "none", "None"):
                result[k] = None
        # 兼容旧格式：herb → herbs
        if "herb" in result and "herbs" not in result:
            result["herbs"] = [result.pop("herb")] if result.get("herb") else []
        if "herbs" not in result:
            result["herbs"] = []
        result.setdefault("ph", 7.4)
        result.setdefault("extra_targets", [])
        result.setdefault("extra_ligands", [])
        return validate_pipeline_params(apply_requested_mode_contract(result))
    # fallback: try simple regex
    m = re.search(r"\{[^}]+\}", raw, re.DOTALL)
    if m:
        return validate_pipeline_params(apply_requested_mode_contract(json.loads(m.group())))
    raise ValueError(f"无法从模型输出中提取 JSON：{raw[:300]}")


# ── 子进程执行 ─────────────────────────────────────────
def run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: int = 600, allow_fail: bool = False) -> str:
    """Execute an argv-only command and return stdout+stderr."""
    if isinstance(cmd, (str, bytes)) or not isinstance(cmd, Sequence) or not cmd:
        raise TypeError("run() 只接受非空 argv 序列，禁止字符串命令")
    argv: list[str] = []
    for index, item in enumerate(cmd):
        if not isinstance(item, (str, os.PathLike)):
            raise TypeError(f"argv[{index}] 必须是字符串或路径")
        value = os.fspath(item)
        if not isinstance(value, str):
            raise TypeError(f"argv[{index}] 必须解析为字符串路径")
        if not value or "\x00" in value:
            raise ValueError(f"argv[{index}] 不能为空或包含 NUL")
        argv.append(value)
    nl, eq = "\n", "=" * 60
    display = shlex.join(argv)
    print(f"{nl}{eq}{nl}▶ {display}{nl}{eq}", flush=True)
    result = _captured_command(
        argv, capture_output=True, text=True,
        cwd=cwd or str(PROJECT_ROOT), timeout=timeout
    )
    output = result.stdout + result.stderr
    print(output, flush=True)
    if result.returncode != 0:
        if not allow_fail:
            raise RuntimeError(f"命令失败 (exit={result.returncode}): {display[:300]}")
    return output


def _captured_command(argv, *, cwd, timeout, capture_output=True, text=True):
    if os.environ.get("NPFLOW_STAGE_DEADLINE"):
        return run_logged(argv, cwd=cwd, timeout=timeout,
            log_dir=os.environ.get("NPFLOW_STAGE_LOG_DIR") or PROJECT_ROOT / "results" / "stage_logs")
    return subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=capture_output, text=text)


def read_latest_run() -> str:
    """读取最新网药 run 的 output_dir。"""
    data = _read_json_object(Path(NP_LATEST), "latest_run.json")
    output_dir = data.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise PipelineContractError("latest_run.json 缺少非空 output_dir")
    run_dir = validate_network_run_dir(output_dir)
    binding_path = run_dir / RUN_BINDING_FILENAME
    if binding_path.is_file():
        binding = _read_json_object(binding_path, RUN_BINDING_FILENAME)
        if binding.get("task_id"):
            raise PipelineContractError("全局 latest_run 属于 Web 任务，CLI 禁止继承或修改其产物")
    return str(run_dir)


def parse_network_run_dir(output: str) -> str:
    for line in reversed(output.splitlines()):
        if line.startswith("NPFLOW_NETWORK_RUN_DIR="):
            run_dir = line.split("=", 1)[1].strip()
            if run_dir:
                return str(validate_network_run_dir(run_dir))
    raise RuntimeError("未从网药管理器日志中解析到本次 NPFLOW_NETWORK_RUN_DIR，拒绝回退到全局 latest_run。")


def parse_network_manager_contract(output: str) -> tuple[str, str]:
    """Bind this Manager invocation to its own run and generated configuration."""
    run_path = Path(parse_network_run_dir(output))
    config_value: Optional[str] = None
    for line in reversed(output.splitlines()):
        if line.startswith("NPFLOW_NETWORK_CONFIG_PATH="):
            config_value = line.split("=", 1)[1].strip()
            break

    # Compatibility with Manager versions predating the explicit config marker.
    # Both paths are generated from the exact same unique project_name stem.
    if not config_value:
        config_value = str(
            (PROJECT_ROOT / "Network Pharmacology/config/generated" / f"{run_path.name}.yaml").absolute()
        )
    config_path = validate_network_config_path(config_value)
    if config_path.stem != run_path.name:
        raise PipelineContractError(
            "网药 Manager 的 run/config 标记不属于同一次调用："
            f"run={run_path.name!r}, config={config_path.name!r}"
        )
    return str(run_path), str(config_path)


def _safe_component(value: str, *, fallback: str, max_length: int = 60) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", value).strip("._-")
    return (cleaned[:max_length].rstrip("._-") or fallback)


def _pipeline_context(params: dict[str, Any]) -> dict[str, Any]:
    raw_task_id = os.environ.get("NPFLOW_WEB_TASK_ID", "").strip()
    if raw_task_id:
        if not re.fullmatch(r"task_[1-9][0-9]*", raw_task_id):
            raise PipelineContractError(f"非法 NPFLOW_WEB_TASK_ID：{raw_task_id!r}")
        run_id = os.environ.get("NPFLOW_WEB_RUN_ID", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", run_id):
            raise PipelineContractError("Web 任务缺少合法 NPFLOW_WEB_RUN_ID")
        task_id: Optional[str] = raw_task_id
        result_dir = WEB_RESULTS_ROOT / raw_task_id
    else:
        run_id = uuid.uuid4().hex
        task_id = None
        herbs = params.get("herbs") or []
        entity = "+".join(herbs[:3]) if herbs else (params.get("compound") or "run")
        label = _safe_component(f"{entity}_{params['disease']}", fallback="npflow")
        result_dir = RESULTS_ROOT / "runs" / f"{label}__{run_id[:12]}"

    result_dir = _resolved_within(result_dir.absolute(), RESULTS_ROOT, must_exist=False)
    if result_dir.exists():
        if result_dir.is_symlink():
            raise PipelineContractError(f"任务结果目录不能是符号链接：{result_dir}")
        if any(result_dir.iterdir()):
            raise PipelineContractError(f"任务结果目录非空，拒绝混入旧产物：{result_dir}")
    result_dir.mkdir(parents=True, exist_ok=True)
    status_path = result_dir / PIPELINE_STATUS_FILENAME
    if status_path.exists():
        previous = _read_json_object(status_path, PIPELINE_STATUS_FILENAME)
        if previous.get("run_id") != run_id:
            raise PipelineContractError(f"任务结果目录已有其他 run 的状态文件：{status_path}")
    return {
        "task_id": task_id,
        "run_id": run_id,
        "result_dir": result_dir,
        "status_path": status_path,
    }


def _artifact_records(output_dir: Path, relative_paths: Sequence[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = output_dir.resolve(strict=True)
    for rel_text in relative_paths:
        rel = Path(rel_text)
        if rel.is_absolute() or ".." in rel.parts:
            raise PipelineContractError(f"产物路径必须是安全的相对路径：{rel_text}")
        candidate = (root / rel).resolve(strict=True)
        if root not in candidate.parents:
            raise PipelineContractError(f"产物路径越界：{candidate}")
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise PipelineContractError(f"必要产物为空或不是文件：{candidate}")
        records.append({"path": rel.as_posix(), "size_bytes": candidate.stat().st_size})
    return records


class PipelineTracker:
    """Atomically persist the requested-stage contract in the task-owned result dir."""

    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.context = _pipeline_context(params)
        os.environ["NPFLOW_STAGE_LOG_DIR"] = str(self.context["result_dir"] / "stage_logs")
        requested = MODE_STAGES[params["mode"]]
        stages: dict[str, dict[str, Any]] = {}
        for name in STAGE_NAMES:
            is_requested = name in requested
            is_auxiliary = name == "packaging"
            stages[name] = {
                "requested": is_requested,
                "status": "pending" if is_requested or is_auxiliary else "skipped",
                "reason": "" if is_requested or is_auxiliary else "not_requested",
                "output_dir": "",
                "required_artifacts": [],
                "artifacts": [],
                "started_at": None,
                "finished_at": None if is_requested or is_auxiliary else utc_now(),
            }
        self.payload: dict[str, Any] = {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "kind": "npflow_pipeline_status",
            "task_id": self.context["task_id"],
            "run_id": self.context["run_id"],
            "requested_mode": params["mode"],
            "status": "running",
            "result_dir": str(self.context["result_dir"]),
            "created_at": utc_now(),
            "finished_at": None,
            "stages": stages,
            "packaged_artifacts": [],
        }
        self.active_stage: Optional[str] = None
        self.write()
        print(f"NPFLOW_PIPELINE_STATUS_FILE={self.context['status_path']}", flush=True)

    @property
    def result_dir(self) -> Path:
        return self.context["result_dir"]

    def write(self) -> None:
        status_path = _resolved_within(
            self.context["status_path"], self.result_dir, must_exist=False
        )
        _atomic_write_json(status_path, self.payload)

    def start(self, name: str) -> None:
        stage = self.payload["stages"][name]
        if (not stage["requested"] and name != "packaging") or stage["status"] != "pending":
            raise PipelineContractError(f"阶段 {name} 不能从 {stage['status']} 转为 running")
        if self.active_stage is not None:
            raise PipelineContractError(f"阶段 {self.active_stage} 尚未结束")
        stage["status"] = "running"
        stage["started_at"] = utc_now()
        self.active_stage = name
        if name in {"network", "docking", "reverse"}:
            seconds = 3600 if name == "reverse" else 1800
            stage["hard_timeout_seconds"] = seconds
            self.context[name + "_deadline"] = time.time() + seconds
        self.write()

    def finish(
        self,
        name: str,
        status: str,
        *,
        reason: str = "",
        output_dir: Optional[Path] = None,
        required_artifacts: Sequence[str] = (),
        artifact_paths: Sequence[str] = (),
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if status not in TERMINAL_STAGE_STATUSES - {"failed", "skipped"}:
            raise PipelineContractError(f"阶段 {name} 的成功终态无效：{status}")
        stage = self.payload["stages"][name]
        if stage["status"] != "running" or self.active_stage != name:
            raise PipelineContractError(f"阶段 {name} 当前不在运行")
        if output_dir is not None:
            resolved_output = output_dir.resolve(strict=True)
            stage["output_dir"] = str(resolved_output)
            stage["required_artifacts"] = list(required_artifacts)
            stage["artifacts"] = _artifact_records(resolved_output, artifact_paths)
        stage["status"] = status
        stage["reason"] = reason
        stage["finished_at"] = utc_now()
        if details:
            stage["details"] = details
        self.active_stage = None
        self.write()

    def fail(self, exc: BaseException) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        if self.active_stage:
            stage = self.payload["stages"][self.active_stage]
            stage["status"] = "failed"
            stage["reason"] = reason[:2000]
            stage["finished_at"] = utc_now()
            self.active_stage = None
        for stage in self.payload["stages"].values():
            if stage["status"] == "pending":
                stage["status"] = "skipped"
                stage["reason"] = "blocked_by_failed_stage"
                stage["finished_at"] = utc_now()
        self.payload["status"] = "failed"
        self.payload["reason"] = reason[:2000]
        self.payload["failure_reason"] = reason[:2000]
        self.payload["finished_at"] = utc_now()
        self.write()
        print("NPFLOW_PIPELINE_STATUS=failed", flush=True)

    def bind_packaged_stage(
        self,
        name: str,
        output_relative: str,
        required_artifacts: Sequence[str],
    ) -> None:
        """Replace module paths with task-root-contained packaged artifact paths."""
        stage = self.payload["stages"][name]
        if stage["status"] not in TERMINAL_STAGE_STATUSES:
            raise PipelineContractError(f"阶段 {name} 尚未进入终态，不能绑定打包产物")
        rel_dir = Path(output_relative)
        if rel_dir.is_absolute() or ".." in rel_dir.parts:
            raise PipelineContractError(f"非法打包阶段路径：{output_relative}")
        output_dir = (self.result_dir / rel_dir).resolve(strict=True)
        result_root = self.result_dir.resolve(strict=True)
        if result_root not in output_dir.parents:
            raise PipelineContractError(f"打包阶段路径越界：{output_dir}")
        old_output = stage.get("output_dir")
        if old_output:
            stage.setdefault("details", {})["module_output_dir"] = old_output
        records: list[dict[str, Any]] = []
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            if result_root not in resolved.parents:
                raise PipelineContractError(f"打包产物越界：{resolved}")
            records.append(
                {
                    "path": resolved.relative_to(result_root).as_posix(),
                    "size_bytes": resolved.stat().st_size,
                }
            )
        for required in required_artifacts:
            required_path = (result_root / required).resolve(strict=True)
            if result_root not in required_path.parents or not required_path.is_file() or required_path.stat().st_size <= 0:
                raise PipelineContractError(f"打包后的必要产物缺失或越界：{required}")
        stage["output_dir"] = rel_dir.as_posix()
        stage["required_artifacts"] = list(required_artifacts)
        stage["artifacts"] = records
        self.write()

    def complete(self) -> str:
        if self.payload["stages"]["packaging"]["status"] != "completed":
            raise PipelineContractError("结果打包阶段尚未完成")
        requested = [
            stage for stage in self.payload["stages"].values() if stage["requested"]
        ]
        if any(stage["status"] == "failed" for stage in requested):
            status = "failed"
        elif any(stage["status"] == "no_result" for stage in requested):
            status = "no_result"
        elif all(stage["status"] == "completed" for stage in requested):
            status = "completed"
        else:
            raise PipelineContractError("请求阶段尚未全部进入合法终态")
        self.payload["status"] = status
        self.payload["reason"] = (
            "all_requested_stages_completed"
            if status == "completed"
            else "one_or_more_requested_stages_returned_valid_no_result"
        )
        self.payload["finished_at"] = utc_now()
        self.payload["packaged_artifacts"] = [
            path.relative_to(self.result_dir).as_posix()
            for path in sorted(self.result_dir.rglob("*"))
            if path.is_file()
            and path.name != PIPELINE_STATUS_FILENAME
            and self.result_dir.resolve(strict=True) in path.resolve(strict=True).parents
        ]
        self.write()
        print(f"NPFLOW_PIPELINE_STATUS={status}", flush=True)
        print(f"NPFLOW_RESULT_DIR={self.result_dir}", flush=True)
        return status


# ── 各阶段 ─────────────────────────────────────────────
@contextmanager
def _manager_run_lock(entity_kind: str, entity: str, disease: str, timeout: float = 660.0):
    """Serialize only identical Manager run names across orchestrator processes."""
    NETWORK_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    manager_name_key = re.sub(
        r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", f"{entity}_{disease}"
    ).strip("_")
    digest = hashlib.sha256(manager_name_key.encode("utf-8")).hexdigest()[:24]
    lock_dir = NETWORK_RUNS_ROOT / ".npflow_locks"
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / f"{digest}.lock"
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + remaining_timeout(timeout)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while not acquired:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等待相同输入的网药 Manager 锁超时：{entity} / {disease}")
                    time.sleep(0.2)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等待相同输入的网药 Manager 锁超时：{entity} / {disease}")
                    time.sleep(0.2)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _network_binding_payload(
    context: dict[str, Any],
    *,
    entity_kind: str,
    entity: str,
    disease: str,
    source_index: int,
    output_dir: Path,
    source_runs: Optional[list[dict[str, Any]]] = None,
    scope: str = "single_entity_network",
    status: str = "claimed",
) -> dict[str, Any]:
    if status not in {"building", "claimed"}:
        raise ValueError(f"不支持的网药 run binding 状态：{status}")
    return {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "kind": "npflow_network_run_binding",
        "task_id": context.get("task_id"),
        "run_id": context["run_id"],
        "stage": "network",
        "status": status,
        "scope": scope,
        "entity_kind": entity_kind,
        "entity": entity,
        "disease": disease,
        "source_index": source_index,
        "output_dir": str(output_dir),
        "source_runs": source_runs or [],
        "created_at": utc_now(),
    }


def _verify_network_binding(run_dir: Path | str, context: dict[str, Any]) -> dict[str, Any]:
    run_path = validate_network_run_dir(run_dir)
    manifest = _read_json_object(run_path / RUN_BINDING_FILENAME, RUN_BINDING_FILENAME)
    if (
        manifest.get("schema_version") != PIPELINE_SCHEMA_VERSION
        or manifest.get("kind") != "npflow_network_run_binding"
        or manifest.get("stage") != "network"
    ):
        raise PipelineContractError(f"网药 run manifest 类型无效：{run_path}")
    if manifest.get("run_id") != context.get("run_id") or manifest.get("task_id") != context.get("task_id"):
        raise PipelineContractError(f"网药 run 不属于当前 task/run：{run_path}")
    if manifest.get("status") != "claimed":
        raise PipelineContractError(f"网药 run 尚未完成原子提交：{run_path}")
    manifest_output = manifest.get("output_dir")
    if not isinstance(manifest_output, str) or Path(manifest_output).resolve(strict=False) != run_path:
        raise PipelineContractError(f"网药 run manifest 的 output_dir 不匹配：{run_path}")
    return manifest


def _update_latest_run(final_dir: Path, context: dict[str, Any], **metadata: Any) -> None:
    latest_path = Path(NP_LATEST)
    payload: dict[str, Any] = dict(metadata)
    payload.update(
        {
            "output_dir": str(final_dir),
            "task_id": context.get("task_id"),
            "run_id": context["run_id"],
            "updated_at": utc_now(),
        }
    )
    _atomic_write_json(latest_path, payload)


def _claim_manager_run(
    manager_run_dir: Path | str,
    manager_config_path: Path | str,
    context: dict[str, Any],
    *,
    entity_kind: str,
    entity: str,
    disease: str,
    source_index: int,
) -> Path:
    source = validate_network_run_dir(manager_run_dir)
    config_resolved = validate_network_config_path(manager_config_path)
    if config_resolved.stem != source.name:
        raise PipelineContractError(
            "拒绝归档不属于本次 Manager run 的配置："
            f"run={source.name!r}, config={config_resolved.name!r}"
        )
    archived_config = source / "run_config.yaml"
    if archived_config.exists():
        raise PipelineContractError(f"网药 run 已存在归档配置：{archived_config}")
    try:
        os.replace(config_resolved, archived_config)
    except OSError as exc:
        # os.replace is atomic on this same-volume project tree.  If an unusual
        # implementation moved and then raised, restore the generated source so
        # the configuration is never lost on a failed claim.
        if not config_resolved.exists() and archived_config.is_file():
            try:
                os.replace(archived_config, config_resolved)
            except OSError as rollback_exc:
                raise PipelineContractError(
                    f"网药配置归档失败且无法恢复原配置：{config_resolved}"
                ) from rollback_exc
        raise PipelineContractError(f"无法将本次网药配置归档到 run：{config_resolved}") from exc

    binding = _network_binding_payload(
        context,
        entity_kind=entity_kind,
        entity=entity,
        disease=disease,
        source_index=source_index,
        output_dir=source,
    )
    _atomic_write_json(source / RUN_BINDING_FILENAME, binding)
    _verify_network_binding(source, context)
    _update_latest_run(
        source,
        context,
        config_path=str(source / archived_config.name),
        input_mode=entity_kind,
        entity_name=entity,
        herb_name=entity if entity_kind == "herb" else "",
        compound_name=entity if entity_kind == "compound" else "",
        disease_name=disease,
    )
    return source


def classify_network_run(run_dir: Path | str) -> tuple[str, str]:
    run_path = validate_network_run_dir(run_dir)
    qc = _read_json_object(run_path / "08_report/qc_report.json", "network qc_report.json")
    workflow_status = str(qc.get("workflow_status") or qc.get("status") or "").strip().lower()
    if workflow_status in {"failed", "failure", "error", "invalid"} or "fail" in workflow_status:
        reason = str(qc.get("failure_reason") or workflow_status)
        raise PipelineContractError(f"网药 QC 判定失败：{reason}")
    if not workflow_status:
        raise PipelineContractError("网药 QC 缺少 workflow_status/status")

    intersection_count = _csv_row_count(run_path / "04_intersection/intersection_targets.csv")
    target_count = _csv_row_count(run_path / "07_core_targets/docking_targets.csv")
    ligand_count = _csv_row_count(run_path / "07_core_targets/docking_ligands.csv")
    counts = qc.get("counts") if isinstance(qc.get("counts"), dict) else {}
    declared_intersection = counts.get("intersection_targets", 0)
    try:
        declared_intersection = int(declared_intersection or 0)
    except (TypeError, ValueError):
        raise PipelineContractError("网药 QC counts.intersection_targets 必须是整数")
    if declared_intersection > 0 and intersection_count == 0 and qc.get("scope") != "candidate_union_only":
        raise PipelineContractError("网药 QC 声明有交集靶点，但必要 CSV 缺失或为空")
    if intersection_count > 0 or (target_count > 0 and ligand_count > 0):
        return "completed", "network_results_available"
    return "no_result", "valid_network_run_without_eligible_results"


def network_artifacts(run_dir: Path | str) -> tuple[list[str], list[str]]:
    run_path = validate_network_run_dir(run_dir)
    required = ["08_report/qc_report.json"]
    optional = [
        "08_report/network_pharmacology_report.html",
        "04_intersection/intersection_targets.csv",
        "07_core_targets/docking_targets.csv",
        "07_core_targets/docking_ligands.csv",
        RUN_BINDING_FILENAME,
    ]
    artifacts = required + [
        rel for rel in optional if (run_path / rel).is_file() and (run_path / rel).stat().st_size > 0
    ]
    return required, list(dict.fromkeys(artifacts))


@stage_budget("network", 1800)
def run_network_pharm(
    herb: str,
    compound: Optional[str],
    disease: str,
    *,
    context: Optional[dict[str, Any]] = None,
    source_index: int = 0,
) -> str:
    """Stage A：网络药理学。返回 run_dir。"""
    if herb:
        entity_kind, entity = "herb", herb
    else:
        entity_kind, entity = "compound", compound or ""

    print(
        f"\n{'=' * 60}\n▶ 网络药理学结构化输入："
        f"mode={entity_kind}, entity={entity!r}, disease={disease!r}\n{'=' * 60}",
        flush=True,
    )
    if context is None:
        context = {"task_id": None, "run_id": uuid.uuid4().hex}
    print(f"NPFLOW_ORCHESTRATOR_VERSION=disease-compat-v2; manager={NP_AGENT}", flush=True)
    with _manager_run_lock(entity_kind, entity, disease):
        result = _captured_command(
            [
                PYTHON,
                NP_AGENT,
                "--input-mode",
                entity_kind,
                "--entity-name",
                entity,
                "--disease-name",
                disease,
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=remaining_timeout(1800),
        )
        output = result.stdout + result.stderr
        print(output, flush=True)
        if result.returncode != 0:
            raise RuntimeError(f"网药管理器异常退出 (exit={result.returncode})")
        manager_run_dir, manager_config_path = parse_network_manager_contract(output)
        run_dir = _claim_manager_run(
            manager_run_dir,
            manager_config_path,
            context,
            entity_kind=entity_kind,
            entity=entity,
            disease=disease,
            source_index=source_index,
        )
    print(f"\n✅ 网药完成 → {run_dir}")
    status, reason = classify_network_run(run_dir)
    if status == "no_result":
        print(f"⚠️ 网药合法无结果：{reason}")
    print(f"NPFLOW_NETWORK_RUN_DIR={run_dir}", flush=True)
    return str(run_dir)


def normalize_extra_entries(entries, *, uppercase: bool = False) -> list[str]:
    """Normalize user-specified targets or ligands from model/list/string input."""
    if not entries:
        return []
    if isinstance(entries, str):
        entries = re.split(r"[,，;；、\n]+", entries)
    cleaned = []
    seen = set()
    for item in entries:
        text = str(item).strip()
        if not text:
            continue
        text = re.sub(r"^(额外)?(加入|添加)?(靶点|蛋白|蛋白名|靶点名|target|protein|配体|配体名|ligand)[:：=]\s*", "", text, flags=re.IGNORECASE).strip()
        if not text:
            continue
        value = text.upper() if uppercase else text
        key = value.upper()
        if key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return cleaned


def fetch_pubchem_ligand(entry: str) -> dict:
    """Best-effort PubChem lookup for user-specified ligand names."""
    url_name = urllib.parse.quote(entry)
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
        f"{url_name}/property/Title,CanonicalSMILES,IsomericSMILES,InChIKey/JSON"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        props = (data.get("PropertyTable", {}).get("Properties") or [{}])[0]
        cid = str(props.get("CID") or "")
        canonical = props.get("CanonicalSMILES") or props.get("ConnectivitySMILES") or props.get("SMILES") or ""
        isomeric = props.get("IsomericSMILES") or props.get("SMILES") or canonical
        if canonical:
            print(f"  🔎 PubChem 已解析配体 {entry}: CID={cid}")
            return {
                "ligand_id": f"PubChem:{cid}" if cid else entry,
                "compound_name": props.get("Title") or entry,
                "source_ingredient_names": entry,
                "canonical_smiles": canonical,
                "isomeric_smiles": isomeric,
                "pubchem_cid": cid,
                "inchikey": props.get("InChIKey") or "",
                "source_list": "user_requested_pubchem",
                "selection_reason": "user_requested_pubchem_resolved",
            }
    except Exception as exc:
        print(f"  ⚠️ PubChem 未解析到配体 {entry}: {exc}")
    return {
        "compound_name": entry,
        "source_ingredient_names": entry,
        "source_list": "user_requested",
        "selection_reason": "user_requested_unresolved_structure",
    }


def append_extra_to_csv(
    csv_path: str,
    entries: list,
    col_map: dict,
    id_fields: list[str] = None,
    uppercase: bool = False,
    prepend: bool = False,
    row_factory=None,
    ensure_fields: Optional[list[str]] = None,
    existing_defaults: Optional[dict[str, str]] = None,
):
    """向 CSV 写入额外行（靶点或配体），可将用户指定项提升到前排。"""
    entries = normalize_extra_entries(entries, uppercase=uppercase)
    if not entries or not Path(csv_path).exists():
        return
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = list(csv.reader(f))
    if not reader:
        return
    header = reader[0]
    ensure_fields = ensure_fields or []
    for field in ensure_fields:
        if field not in header:
            header.append(field)
    existing_defaults = existing_defaults or {}
    existing_rows = []
    for source_row in reader[1:]:
        padded = list(source_row) + [""] * max(0, len(header) - len(source_row))
        for field, value in existing_defaults.items():
            if field in header:
                index = header.index(field)
                if not padded[index]:
                    padded[index] = value
        existing_rows.append(padded)
    id_fields = [field for field in (id_fields or [header[0]]) if field in header]
    if not id_fields:
        id_fields = [header[0]]
    id_indexes = [header.index(field) for field in id_fields if field in header]
    entry_keys = {entry.upper() for entry in entries}

    def matches_entry(row_values: list[str]) -> bool:
        for index in id_indexes:
            if len(row_values) > index and row_values[index].strip().upper() in entry_keys:
                return True
        return False

    preserved_rows = [row for row in existing_rows if not matches_entry(row)]
    new_rows = []
    for entry in entries:
        row = dict(col_map)
        for field in id_fields:
            row[field] = entry
        if "target_name" in header and not row.get("target_name"):
            row["target_name"] = entry
        if "compound_name" in header and not row.get("compound_name"):
            row["compound_name"] = entry
        if "source_ingredient_names" in header and not row.get("source_ingredient_names"):
            row["source_ingredient_names"] = entry
        if row_factory:
            row.update(row_factory(entry))
        new_rows.append([row.get(h, "") for h in header])
        action = "已提升到" if prepend else "已追加到"
        print(f"  ➕ {action} {Path(csv_path).name}: {entry}")

    rows = [header] + (new_rows + preserved_rows if prepend else preserved_rows + new_rows)
    _atomic_write_csv_rows(Path(csv_path), rows)


def append_user_requested_ligands(run_path: Path, entries: list[str]) -> None:
    entries = normalize_extra_entries(entries)
    if not entries:
        return
    resolved: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, str]] = []
    for entry in entries:
        row = fetch_pubchem_ligand(entry)
        if row.get("canonical_smiles"):
            row.update(
                {
                    "contract_version": "docking_ligands.v2",
                    "selection_status": "selected_exception",
                    "selection_reason": "explicit_user_request_not_score_qualified",
                    "ligand_score": "",
                    "ligand_confidence": "",
                }
            )
            resolved[entry] = row
        else:
            rejected.append(
                {
                    "requested_name": entry,
                    "resolution_status": "rejected",
                    "rejection_reason": "pubchem_structure_not_resolved",
                }
            )
            print(f"  ⚠️ 用户指定配体未解析到真实结构，未加入对接：{entry}")

    ligand_csv = run_path / "07_core_targets/docking_ligands.csv"
    if resolved:
        if not ligand_csv.is_file():
            _atomic_write_csv_rows(
                ligand_csv,
                [[
                    "ligand_id",
                    "compound_name",
                    "source_ingredient_names",
                    "canonical_smiles",
                    "isomeric_smiles",
                    "pubchem_cid",
                    "inchikey",
                    "source_list",
                    "contract_version",
                    "selection_status",
                    "selection_reason",
                    "ligand_score",
                    "ligand_confidence",
                ]],
            )
        append_extra_to_csv(
            str(ligand_csv),
            list(resolved),
            {
                "ligand_id": "",
                "compound_name": "",
                "source_ingredient_names": "",
                "ligand_score": "",
                "ligand_confidence": "",
                "canonical_smiles": "",
                "pubchem_cid": "",
                "inchikey": "",
                "source_list": "user_requested_pubchem",
                "contract_version": "docking_ligands.v2",
                "selection_status": "selected_exception",
                "selection_reason": "explicit_user_request_not_score_qualified",
            },
            id_fields=["ligand_id", "compound_name", "source_ingredient_names"],
            uppercase=False,
            prepend=True,
            row_factory=lambda entry: resolved[entry],
            ensure_fields=[
                "contract_version",
                "selection_status",
                "selection_reason",
                "ligand_score",
                "ligand_confidence",
            ],
            existing_defaults={
                "contract_version": "legacy_unversioned",
                "selection_status": "legacy_accepted",
                "selection_reason": "legacy_file_without_selection_contract",
            },
        )
    if rejected:
        rejected_path = run_path / "07_core_targets/rejected_user_requested_ligands.csv"
        rows: list[list[str]] = [["requested_name", "resolution_status", "rejection_reason"]]
        rows.extend(
            [[row["requested_name"], row["resolution_status"], row["rejection_reason"]] for row in rejected]
        )
        _atomic_write_csv_rows(rejected_path, rows)


def append_user_requested_targets(run_path: Path, entries: list[str]) -> None:
    entries = normalize_extra_entries(entries, uppercase=True)
    if not entries:
        return
    target_csv = run_path / "07_core_targets/docking_targets.csv"
    if not target_csv.is_file():
        _atomic_write_csv_rows(
            target_csv,
            [[
                "target_gene",
                "target_name",
                "target_score",
                "target_confidence",
                "recommended_for_docking",
                "contract_version",
                "selection_status",
                "selection_reason",
            ]],
        )
    append_extra_to_csv(
        str(target_csv),
        entries,
        {
            "target_gene": "",
            "target_name": "",
            "target_score": "",
            "target_confidence": "",
            "recommended_for_docking": "true",
            "contract_version": "docking_targets.v2",
            "selection_status": "selected_exception",
            "selection_reason": "explicit_user_request_not_score_qualified",
        },
        id_fields=["target_gene", "target_name"],
        uppercase=True,
        prepend=True,
        ensure_fields=[
            "contract_version",
            "selection_status",
            "selection_reason",
            "target_score",
            "target_confidence",
        ],
        existing_defaults={
            "contract_version": "legacy_unversioned",
            "selection_status": "legacy_accepted",
            "selection_reason": "legacy_file_without_selection_contract",
        },
    )


def docking_inputs_ready(run_dir: Path | str) -> tuple[bool, dict[str, int]]:
    run_path = validate_network_run_dir(run_dir)
    counts = {
        "targets": _csv_row_count(run_path / "07_core_targets/docking_targets.csv"),
        "ligands": _csv_row_count(run_path / "07_core_targets/docking_ligands.csv"),
    }
    return counts["targets"] > 0 and counts["ligands"] > 0, counts


def docking_artifacts(dock_dir: Path | str) -> tuple[list[str], list[str]]:
    dock_path = Path(dock_dir).resolve(strict=True)
    required = [
        "06_scores/docking_scores_ranked.csv",
        "validation/validation_status.json",
        "validation/validation_summary.txt",
    ]
    validation = _read_json_object(dock_path / required[1], "docking validation_status.json")
    if validation.get("status") != "PASS":
        raise PipelineContractError("对接 validation_status.json 未通过 PASS")
    if _csv_row_count(dock_path / required[0]) <= 0:
        raise PipelineContractError("对接排序表没有科学可用结果")
    optional = [
        "06_scores/docking_pair_qc.csv",
        "08_docking_report/docking_report.html",
        "08_docking_report/docking_qc_report.json",
    ]
    artifacts = required + [
        rel for rel in optional if (dock_path / rel).is_file() and (dock_path / rel).stat().st_size > 0
    ]
    return required, artifacts


@stage_budget("docking", 1800)
def run_docking(
    run_dir: str,
    extra_targets: list = None,
    extra_ligands: list = None,
    ph: float = 7.4,
    *,
    context: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Stage B：分子对接。返回 dock_dir。"""
    run_path = validate_network_run_dir(run_dir)
    if context is not None:
        _verify_network_binding(run_path, context)
    ph = validate_ph(ph)
    dock_path = _resolved_within((run_path / "09_docking").absolute(), run_path, must_exist=False)

    # 追加额外靶点
    if extra_targets:
        append_user_requested_targets(run_path, extra_targets)
    # 追加额外配体
    if extra_ligands:
        append_user_requested_ligands(run_path, extra_ligands)

    ready, counts = docking_inputs_ready(run_path)
    if not ready:
        print(f"\n⚠️ 无合格对接输入（targets={counts['targets']}, ligands={counts['ligands']}），合法跳过")
        return None
    if dock_path.exists() and any(dock_path.iterdir()):
        raise PipelineContractError(f"对接输出目录非空，拒绝把旧文件当成本次结果：{dock_path}")

    run(
        [
            PYTHON,
            DOCKING_AGENT,
            "run-from-network",
            "--network-run-dir",
            str(run_path),
            "--max-targets",
            "3",
            "--max-ligands",
            "5",
            "--max-candidates-per-target",
            "3",
            "--ph",
            str(ph),
        ], timeout=remaining_timeout(1800),
    )
    dock_path = _resolved_within(dock_path, run_path)
    if (dock_path / "docking_no_result.json").is_file():
        outcome = _read_json_object(dock_path / "docking_no_result.json", "docking no-result")
        if outcome.get("status") != "no_result" or outcome.get("pairs_scored") != 0:
            raise PipelineContractError("Invalid no-result docking contract")
        print("⚠️ 对接正常收尾但无合格输入：" + str(outcome.get("reason")), flush=True)
        return None
    run([PYTHON, DOCKING_AGENT, "validate-network-run", "--dock-dir", str(dock_path)])
    docking_artifacts(dock_path)
    print(f"\n✅ 对接完成 → {dock_path}")
    return str(dock_path)


def classify_reverse_design(reverse_dir: Path | str) -> tuple[str, str, list[str], list[str]]:
    reverse_path = Path(reverse_dir).resolve(strict=True)
    required = [
        "reverse_design_qc.json",
        "reverse_design_accepted_candidates.csv",
        "reverse_design_report.html",
    ]
    qc = _read_json_object(reverse_path / required[0], "reverse_design_qc.json")
    for rel in required[1:]:
        path = reverse_path / rel
        if not path.is_file() or path.stat().st_size <= 0:
            raise PipelineContractError(f"反向设计缺少必要产物：{path}")
    errors = qc.get("errors") or []
    if not isinstance(errors, list):
        raise PipelineContractError("反向设计 QC errors 必须是数组")
    try:
        accepted = int(qc.get("n_accepted", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise PipelineContractError("反向设计 QC n_accepted 必须是整数") from exc
    accepted_rows = _csv_row_count(reverse_path / required[1])
    if accepted != accepted_rows:
        raise PipelineContractError(
            f"反向设计 QC 与接受候选表数量不一致：qc={accepted}, csv={accepted_rows}"
        )
    qc_status = str(qc.get("status") or "").lower()
    if qc_status in {"failed", "failure", "error"} or (errors and accepted == 0 and not qc.get("valid_result")):
        raise PipelineContractError(f"反向设计执行失败：{'; '.join(map(str, errors)) or qc_status}")
    optional = ["natural_vs_optimized_comparison.csv", "reverse_design_all_candidates.csv"]
    artifacts = required + [
        rel for rel in optional if (reverse_path / rel).is_file() and (reverse_path / rel).stat().st_size > 0
    ]
    if accepted == 0:
        return "no_result", "valid_reverse_design_without_accepted_candidates", required, artifacts
    return "completed", "accepted_candidates_available", required, artifacts


@stage_budget("reverse_design", 3600)
def run_reverse_design(dock_dir: str) -> str:
    """Stage C：反向设计。返回 reverse_dir。"""
    dock_path = _resolved_within(Path(dock_dir).absolute(), NETWORK_RUNS_ROOT)
    reverse_path = _resolved_within(
        (dock_path / "09_reverse_design_after_docking").absolute(), dock_path, must_exist=False
    )
    if reverse_path.exists() and any(reverse_path.iterdir()):
        raise PipelineContractError(f"反向设计目录非空，拒绝误用旧结果：{reverse_path}")
    run(
        [
            PYTHON,
            REVERSE_AGENT,
            "run",
            "--dock-dir",
            str(dock_path),
            "--backend",
            "deepfrag2",
            "--skip-deepfrag2-test",
            "--vina-score-cutoff",
            "-6.0",
            "--max-seed-pairs",
            "3",
            "--fragments-per-attachment-point",
            "10",
            "--top-k-attachment-points",
            "6",
            "--max-rounds",
            "3",
            "--improvement-margin-kcal",
            "0.8",
            "--max-total-vina-jobs",
            "30",
            "--timeout-per-vina",
            "120",
        ], timeout=remaining_timeout(3600),
    )
    reverse_path = _resolved_within(reverse_path, dock_path)
    classify_reverse_design(reverse_path)
    print(f"\n✅ 反向设计完成 → {reverse_path}")
    return str(reverse_path)


# ── 报告 ───────────────────────────────────────────────
def generate_report(run_dir: str, dock_dir: Optional[str], reverse_dir: Optional[str], params: dict) -> str:
    """汇总简约报告。"""
    lines = ["\n" + "=" * 60, "  📊 NPFlow 执行报告", "=" * 60]
    herbs = params.get("herbs", [])
    herb_label = "+".join(herbs[:3]) if herbs else (params.get("herb") or params.get("compound") or "?")
    if len(herbs) > 3:
        herb_label += f" 等{len(herbs)}味"
    lines.append(f"  研究目标：{herb_label} / {params['disease']}")
    lines.append(f"  执行模式：{params['mode']}")

    # 网药报告
    qc_path = f"{run_dir}/08_report/qc_report.json"
    ligands_csv = f"{run_dir}/07_core_targets/docking_ligands.csv"
    targets_csv = f"{run_dir}/07_core_targets/docking_targets.csv"
    if Path(qc_path).exists():
        qc = json.loads(Path(qc_path).read_text())
        counts = qc.get("counts", {})
        lines.append("\n── 网络药理学 ──")
        if qc.get("scope") == "candidate_union_only":
            source_entities = [
                str(item.get("entity"))
                for item in qc.get("source_runs", [])
                if isinstance(item, dict) and item.get("entity")
            ]
            lines.append("  范围：各单味药筛选候选的并集（未重建完整复方网络）")
            lines.append(f"  来源药材：{'、'.join(source_entities) or '?'}")
            lines.append(f"  合并靶点候选：{counts.get('docking_targets', 0)} 个")
            lines.append(f"  合并配体候选：{counts.get('docking_ligands', 0)} 个")
            lines.append("  未重算：PPI、富集分析、跨药材网络 QC")
        else:
            lines.append(f"  活性成分：{counts.get('active_ingredients', '?')} 个")
            lines.append(f"  交集靶点：{counts.get('intersection_targets', '?')} 个")
            ds = qc.get("data_source_status", "?")
            ds_map = {"snapshot_fallback": "TCMSP本地快照 + SymMap2 + HerbiV",
                      "live": "TCMSP在线 + SymMap2 + HerbiV",
                      "supplemental": "SymMap2 + HerbiV (TCMSP不可用)"}
            lines.append(f"  数据来源：{ds_map.get(ds, ds)}")
        # 从 CSV 读 top3（用 csv 模块处理引号逗号）
        if Path(ligands_csv).exists():
            with open(ligands_csv, newline="") as f:
                reader = list(csv.reader(f))
            if len(reader) > 1:
                header = reader[0]
                name_idx = header.index("source_ingredient_names") if "source_ingredient_names" in header else 0
                score_idx = header.index("ligand_score") if "ligand_score" in header else -2
                lines.append(f"  对接配体：{len(reader)-1} 个 | Top3：")
                for row in reader[1:4]:
                    name = row[name_idx] if name_idx < len(row) else "?"
                    score = row[score_idx] if score_idx < len(row) else "?"
                    lines.append(f"    • {name[:40]}  score={score}")
        if Path(targets_csv).exists():
            with open(targets_csv, newline="") as f:
                reader = list(csv.reader(f))
            if len(reader) > 1:
                header = reader[0]
                name_idx = header.index("target_gene") if "target_gene" in header else 0
                score_idx = header.index("target_score") if "target_score" in header else -2
                lines.append(f"  对接靶点：{len(reader)-1} 个 | Top3：")
                for row in reader[1:4]:
                    name = row[name_idx] if name_idx < len(row) else "?"
                    score = row[score_idx] if score_idx < len(row) else "?"
                    lines.append(f"    • {name[:40]}  score={score}")

    # 对接报告
    if dock_dir:
        ranked = f"{dock_dir}/06_scores/docking_scores_ranked.csv"
        validation = f"{dock_dir}/validation/validation_summary.txt"

        # 构建配体 InChIKey → (名称, 来源药材) 映射
        ligand_lookup = {}
        if Path(ligands_csv).exists():
            with open(ligands_csv, newline="") as f:
                lr = list(csv.reader(f))
            if len(lr) > 1:
                lh = lr[0]
                ikey_idx = lh.index("inchikey") if "inchikey" in lh else 0
                name_idx = lh.index("source_ingredient_names") if "source_ingredient_names" in lh else 0
                herb_idx = lh.index("source_herb_names") if "source_herb_names" in lh else None
                for row in lr[1:]:
                    ikey = row[ikey_idx] if ikey_idx < len(row) else ""
                    cname = row[name_idx] if name_idx < len(row) else ""
                    herb = row[herb_idx] if herb_idx is not None and herb_idx < len(row) else ""
                    if ikey:
                        ligand_lookup[ikey] = (cname, herb)

        lines.append("\n── 分子对接 ──")
        if Path(validation).exists():
            vtext = Path(validation).read_text().strip()
            for vl in vtext.split("\n")[:3]:
                lines.append(f"  {vl}")
        if Path(ranked).exists():
            with open(ranked, newline="") as f:
                rr = list(csv.reader(f))
            if len(rr) > 1:
                rh = rr[0]
                target_idx = rh.index("target_gene") if "target_gene" in rh else 0
                ligand_idx = rh.index("ligand_id") if "ligand_id" in rh else 1
                score_idx = rh.index("best_score") if "best_score" in rh else 3
                usable_idx = rh.index("usable_for_scientific_ranking") if "usable_for_scientific_ranking" in rh else -1
                usable = [r for r in rr[1:] if usable_idx < 0 or r[usable_idx].lower() == "true"]
                lines.append(f"  成功对接：{len(rr)-1} 对（科学可用 {len(usable)} 对）")
                lines.append("  Top 5：")
                for row in (usable or rr[1:])[:5]:
                    target = row[target_idx] if target_idx < len(row) else "?"
                    ikey = row[ligand_idx] if ligand_idx < len(row) else "?"
                    score = row[score_idx] if score_idx < len(row) else "?"
                    cname, herb = ligand_lookup.get(ikey, ("?", ""))
                    label = f"{herb}的{cname}" if herb else cname
                    lines.append(f"    • {label:35s} vs {target:8s}  score={score} kcal/mol")

    # 反向设计报告
    if reverse_dir:
        qc_path = f"{reverse_dir}/reverse_design_qc.json"
        lines.append("\n── 反向设计 ──")
        if Path(qc_path).exists():
            qc = json.loads(Path(qc_path).read_text())
            lines.append(f"  生成候选：{qc.get('n_raw_candidates', '?')} 个")
            lines.append(f"  过滤后：{qc.get('n_filtered_candidates', '?')} 个")
            lines.append(f"  接受：{qc.get('n_accepted', '?')} 个")
            best = qc.get("best_improvement_kcal")
            best_status = qc.get("best_improvement_status", "")
            status_label = {
                "accepted": "已接受",
                "not_accepted": "未接受",
                "below_acceptance_threshold": f"未达 {qc.get('improvement_margin_kcal', 0.8)} kcal/mol 接受阈值",
                "worse_than_parent": "候选较原分子变差",
                "no_redocking_result": "无可用重对接结果",
            }.get(best_status, best_status)
            if best is None or best == "":
                lines.append(f"  最佳改善：{status_label or '无可用重对接结果'}")
            else:
                try:
                    best_text = f"{float(best):.4f}".rstrip("0").rstrip(".")
                except (TypeError, ValueError):
                    best_text = str(best)
                suffix = f"（{status_label}）" if status_label else ""
                lines.append(f"  最佳改善：{best_text} kcal/mol{suffix}")

    lines.append("\n" + "=" * 60)
    report = "\n".join(lines)
    print(report)
    return report


def _result_file_description(rel_path: str) -> tuple[str, str, bool]:
    """Return module, human description, core-result flag for a packaged result file."""
    name = Path(rel_path).name
    if rel_path.startswith("01_network_pharmacology/"):
        module = "01 网络药理学"
        descriptions = {
            "intersection_targets.csv": "疾病靶点与中药成分靶点的交集靶点清单，是后续 PPI、富集和对接靶点选择的重要依据。",
            "intersection_summary.json": "交集靶点统计摘要，记录靶点数量、输入信息和筛选概况。",
            "ingredients_for_docking.csv": "进入分子对接候选池的活性成分信息。",
            "ingredient_target_edges.csv": "成分-靶点关系边表，可用于网络可视化和 Cytoscape 分析。",
            "ppi_edges.csv": "蛋白互作网络边表，表示交集靶点之间的相互作用关系。",
            "ppi_network.graphml": "PPI 网络图文件，可导入 Cytoscape、Gephi 等工具继续分析。",
            "ppi_summary.json": "PPI 网络统计摘要，包括节点、边和网络质量信息。",
            "enrichment_all.csv": "GO、KEGG、Reactome 的合并富集结果总表。",
            "go_bp.csv": "GO Biological Process 富集结果，解释靶点涉及的生物过程。",
            "go_mf.csv": "GO Molecular Function 富集结果，解释靶点相关分子功能。",
            "go_cc.csv": "GO Cellular Component 富集结果，解释靶点所在细胞组分。",
            "kegg.csv": "KEGG 通路富集结果，用于观察候选靶点关联的经典信号通路。",
            "reactome.csv": "Reactome 通路富集结果，用于补充机制通路解释。",
            "enrichment_summary.json": "富集分析摘要，记录实际使用的 API 来源、term 数量和 warning。",
            "docking_targets.csv": "筛选后用于分子对接的核心靶点列表。",
            "docking_ligands.csv": "筛选后用于分子对接的候选配体列表。",
            "rejected_user_requested_ligands.csv": "用户额外指定但未解析到真实结构、因此未进入对接的配体记录。",
            "qc_report.json": "网络药理模块质量控制摘要。",
            "network_run_manifest.json": "本任务与网药 run 的结构化归属清单，用于核验 task/run 身份。",
        }
        core = name in {
            "intersection_targets.csv",
            "enrichment_all.csv",
            "kegg.csv",
            "reactome.csv",
            "docking_targets.csv",
            "docking_ligands.csv",
        }
        return module, descriptions.get(name, "网络药理学模块产生的辅助或中间结果文件。"), core
    if rel_path.startswith("02_molecular_docking/"):
        module = "02 分子对接"
        descriptions = {
            "docking_scores_ranked.csv": "分子对接打分排序表，包含靶点-配体组合和 Vina 结合能，是对接模块最核心结果。",
            "docking_pair_qc.csv": "每个靶点-配体对的质量控制记录，用于判断对接是否可靠。",
            "docking_report.html": "可在浏览器打开的分子对接可视化报告。",
            "docking_qc_report.json": "分子对接模块整体 QC 摘要。",
            "validation_summary.txt": "对接验证摘要；PASS 表示对接环境和关键输出通过检查。",
            "ligands.sdf": "准备后的候选配体结构文件，可用于复查或二次建模。",
            "ligand_identity_map.csv": "配体 ID 与来源成分名称、InChIKey 等身份信息的映射表。",
        }
        core = name in {"docking_scores_ranked.csv", "docking_report.html", "validation_summary.txt"}
        return module, descriptions.get(name, "分子对接模块产生的辅助或中间结果文件。"), core
    if rel_path.startswith("03_reverse_design/"):
        module = "03 反向设计"
        descriptions = {
            "accepted_candidates.csv": "通过筛选的优化候选分子表，是反向设计模块的核心结果。",
            "accepted_smiles.txt": "通过筛选候选分子的 SMILES 文本，便于复制到其他化学信息工具中。",
            "comparison.csv": "天然分子与优化候选分子的性质或打分对比表。",
            "qc_report.json": "反向设计模块质量控制摘要。",
            "report.html": "可在浏览器打开的反向设计报告。",
            "reverse_design_not_run.txt": "本任务未执行反向设计时的说明文件。",
        }
        if "/accepted_sdf/" in rel_path:
            return module, "通过筛选候选分子的 SDF 结构文件，可用于后续三维结构查看或复算。", True
        core = name in {"accepted_candidates.csv", "accepted_smiles.txt", "comparison.csv", "report.html"}
        return module, descriptions.get(name, "反向设计模块产生的辅助或中间结果文件。"), core
    if name == "task.log":
        return "运行日志", "网站任务运行日志副本，用于排查报错和复现实验过程。", False
    if name == "说明书.txt":
        return "说明书", "本文件，解释下载包中每个结果文件的用途。", True
    if name == PIPELINE_STATUS_FILENAME:
        return "运行契约", "结构化记录所请求阶段、终态、原因和必要产物。", True
    return "其他", "系统打包的辅助文件。", False


def generate_manual(dest: Path, summary_report: str, params: dict) -> str:
    """Create a Chinese manual that explains every file in the downloadable result folder."""
    files = sorted(
        p for p in dest.rglob("*")
        if p.is_file() and p.name not in {"说明书.txt", "report.txt"}
    )
    herbs = params.get("herbs", [])
    herb_label = "、".join(herbs) if herbs else (params.get("herb") or params.get("compound") or "未识别")
    disease = params.get("disease", "未识别")
    ph = params.get("ph", 7.4)
    task_threads = os.environ.get("NPFLOW_TASK_CPU_LIMIT", "24")
    lines = [
        "# SushanAIDD-agent 结果说明书",
        "",
        "## 结果声明",
        "- 本网站按固定化工作流自动输出结果，使用固定数据库，细节方面把控有限，仅作为快速筛选一键自动化平台，若结果差强人意，敬请见谅!",
        "- 本结果仅用于科研探索和快速筛选，不构成医学诊断、治疗建议或药效证明；关键结论需人工复核和实验验证。",
        *( ["- 复方的网络药理学汇总仅是各单味药筛选候选并集；未重新构建完整复方 PPI、富集或跨药材网络 QC。"] if len(herbs) > 1 else [] ),
        "",
        "## 任务信息",
        f"- 中药/天然产物：{herb_label}",
        f"- 疾病：{disease}",
        f"- 对接 pH：{ph}",
        "- 下载包有效期：任务结束后默认保留 2 天，过期后服务器会自动清理结果文件。",
        f"- 计算资源：网站任务按 12 物理核 / {task_threads} 线程 / 不限内存调度。",
        "",
        "## 快速阅读建议",
        "- 先看 `01_network_pharmacology/enrichment_all.csv`、`kegg.csv`、`reactome.csv` 理解机制通路。",
        "- 再看 `02_molecular_docking/docking_scores_ranked.csv` 和 `docking_report.html` 判断候选成分与靶点的结合情况。",
        "- 如果存在 `03_reverse_design/accepted_candidates.csv`，再查看优化候选分子。",
        "- `qc_report.json`、`validation_summary.txt` 用于判断每一步是否通过质量检查。",
        "",
        "## 文件清单",
        "",
        "| 文件 | 模块 | 是否核心 | 用途 |",
        "| --- | --- | --- | --- |",
        "| `说明书.txt` | 说明书 | 是 | 本文件，解释下载包中每个结果文件的用途。 |",
    ]
    for path in files:
        rel = path.relative_to(dest).as_posix()
        module, desc, core = _result_file_description(rel)
        lines.append(f"| `{rel}` | {module} | {'是' if core else '否'} | {desc} |")
    lines.extend(["", "## 原始执行摘要", "", "```text", summary_report.strip(), "```", ""])
    return "\n".join(lines)


# ── 合并工具 ───────────────────────────────────────────
def _split_provenance_values(value: Any) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in re.split(r"[;,|、]+", str(value)) if part.strip()]


def _merge_identity(row: dict[str, str], key_col: str, source_path: Path, row_number: int) -> tuple[str, str]:
    primary = (row.get(key_col) or "").strip()
    if primary:
        normalized = primary.upper() if key_col.lower() == "inchikey" else primary.casefold()
        return key_col, normalized

    fallback_fields = (
        ("pubchem_cid", False),
        ("isomeric_smiles", False),
        ("canonical_smiles", False),
        ("smiles", False),
        ("ligand_id", True),
        ("compound_name", True),
        ("source_ingredient_names", True),
        ("target_name", True),
    )
    for field, casefold in fallback_fields:
        value = (row.get(field) or "").strip()
        if value:
            return field, value.casefold() if casefold else value

    # Missing identity has no deduplication semantics. The source row remains unique.
    return "source_row", f"{source_path.resolve(strict=False)}#{row_number}"


def merge_csvs(
    csv_paths: list,
    key_col: str,
    score_col: str,
    *,
    output_path: Optional[Path | str] = None,
    source_labels: Optional[dict[str, str]] = None,
) -> str:
    """Merge CSVs atomically, using non-empty molecular identity fallbacks."""
    if not isinstance(csv_paths, list) or not csv_paths:
        raise PipelineContractError("merge_csvs 至少需要一个源 CSV")
    source_labels = source_labels or {}
    paths = [Path(path).resolve(strict=True) for path in csv_paths]
    if len(paths) == 1 and output_path is None:
        return str(paths[0])
    if output_path is None:
        output_path = paths[0].parent / f".{paths[0].stem}.merged.{uuid.uuid4().hex}.csv"
    out = Path(output_path).resolve(strict=False)

    header: Optional[list[str]] = None
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    provenance_fields = ("source_herb_names", "formula_source_herbs", "source_ingredient_names", "source_list")
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            if not fields:
                raise PipelineContractError(f"CSV 缺少表头：{path}")
            if key_col not in fields:
                raise PipelineContractError(f"CSV 缺少去重字段 {key_col}：{path}")
            if header is None:
                header = fields
            elif fields != header:
                raise PipelineContractError(f"复方源 CSV 列结构不一致：{path}")

            source_label = source_labels.get(str(path), "")
            for row_number, row in enumerate(reader, start=2):
                if not any((value or "").strip() for value in row.values()):
                    continue
                try:
                    score = float((row.get(score_col) or "0").strip() or 0) if score_col in fields else 0.0
                except ValueError as exc:
                    raise PipelineContractError(f"CSV 评分不是数值：{path}:{row_number}") from exc
                if not math.isfinite(score):
                    raise PipelineContractError(f"CSV 评分必须是有限值：{path}:{row_number}")
                basis, identity = _merge_identity(row, key_col, path, row_number)
                identity_key = (basis, identity)
                provenance: dict[str, list[str]] = {}
                for field in provenance_fields:
                    provenance[field] = _split_provenance_values(row.get(field))
                if source_label:
                    provenance["source_herb_names"].append(source_label)
                    provenance["formula_source_herbs"].append(source_label)

                existing = merged.get(identity_key)
                if existing is None:
                    merged[identity_key] = {
                        "row": dict(row),
                        "score": score,
                        "basis": basis,
                        "identity": identity,
                        "provenance": provenance,
                    }
                else:
                    for field in provenance_fields:
                        existing["provenance"][field].extend(provenance[field])
                    if score > existing["score"]:
                        existing["row"] = dict(row)
                        existing["score"] = score

    if header is None:
        raise PipelineContractError("没有可合并的 CSV 表头")
    final_header = list(header)
    for field in ("source_herb_names", "formula_source_herbs", "merge_identity_basis"):
        if field not in final_header:
            final_header.append(field)

    output_rows: list[list[Any]] = [final_header]
    for item in sorted(merged.values(), key=lambda record: -record["score"]):
        row = item["row"]
        for field in provenance_fields:
            values = []
            seen: set[str] = set()
            for value in item["provenance"][field]:
                key = value.casefold()
                if key not in seen:
                    values.append(value)
                    seen.add(key)
            if values:
                row[field] = "; ".join(values)
        row["merge_identity_basis"] = item["basis"]
        output_rows.append([row.get(field, "") for field in final_header])
    _atomic_write_csv_rows(out, output_rows)
    print(f"  🔗 合并 {len(paths)} 个 CSV → {len(merged)} 行 → {out.name}")
    return str(out)


def merge_network_runs(
    run_dirs: list,
    formula_name: str,
    disease: str,
    *,
    context: Optional[dict[str, Any]] = None,
    source_entities: Optional[list[str]] = None,
) -> str:
    """Atomically commit a task-owned formula candidate-union run."""
    if len(run_dirs) < 2:
        raise PipelineContractError("复方合并至少需要两个单味药 run")
    if context is None:
        context = {"task_id": None, "run_id": uuid.uuid4().hex}
    paths = [validate_network_run_dir(run_dir) for run_dir in run_dirs]
    bindings: list[dict[str, Any]] = []
    for path in paths:
        if context.get("task_id") is not None:
            bindings.append(_verify_network_binding(path, context))
        elif (path / RUN_BINDING_FILENAME).is_file():
            bindings.append(_read_json_object(path / RUN_BINDING_FILENAME, RUN_BINDING_FILENAME))
        else:
            bindings.append({})
    if source_entities is None:
        source_entities = [str(binding.get("entity") or path.name) for binding, path in zip(bindings, paths)]
    if len(source_entities) != len(paths):
        raise PipelineContractError("source_entities 数量必须与 run_dirs 一致")

    owner = context.get("task_id") or "cli"
    label = _safe_component(f"{formula_name}_{disease}", fallback="formula")
    final = validate_network_run_dir(
        (NETWORK_RUNS_ROOT / f"{label}__{_safe_component(owner, fallback='cli')}__{context['run_id'][:12]}__formula").absolute(),
        must_exist=False,
    )
    source_run_refs = [
        {
            "run_dir": str(path),
            "entity": entity,
            "run_id": binding.get("run_id"),
            "task_id": binding.get("task_id"),
        }
        for path, entity, binding in zip(paths, source_entities, bindings)
    ]

    final.mkdir(parents=True, exist_ok=False)
    building_binding = _network_binding_payload(
        context,
        entity_kind="formula",
        entity=formula_name,
        disease=disease,
        source_index=0,
        output_dir=final,
        source_runs=source_run_refs,
        scope="formula_candidate_union",
        status="building",
    )
    try:
        # Establish task/run ownership before creating any partial formula
        # payload.  Web cleanup may remove a killed build, while consumers reject
        # it until the final atomic manifest transition to ``claimed``.
        _atomic_write_json(final / RUN_BINDING_FILENAME, building_binding)
        staging = validate_network_run_dir(
            (final / f".payload.{uuid.uuid4().hex}.staging").absolute(),
            must_exist=False,
        )
        staging.mkdir(parents=False, exist_ok=False)
        core_dir = staging / "07_core_targets"
        report_dir = staging / "08_report"
        core_dir.mkdir(parents=True)
        report_dir.mkdir(parents=True)

        source_labels: dict[str, str] = {}
        target_csvs: list[str] = []
        ligand_csvs: list[str] = []
        source_summaries: list[dict[str, Any]] = []
        for path, entity, binding in zip(paths, source_entities, bindings):
            target = path / "07_core_targets/docking_targets.csv"
            ligand = path / "07_core_targets/docking_ligands.csv"
            if target.is_file():
                target_resolved = _resolved_within(target, path)
                target_csvs.append(str(target_resolved))
                source_labels[str(target_resolved)] = entity
            if ligand.is_file():
                ligand_resolved = _resolved_within(ligand, path)
                ligand_csvs.append(str(ligand_resolved))
                source_labels[str(ligand_resolved)] = entity
            source_status, source_reason = classify_network_run(path)
            source_summaries.append(
                {
                    "entity": entity,
                    "run_dir": str(path),
                    "run_id": binding.get("run_id"),
                    "status": source_status,
                    "reason": source_reason,
                    "target_candidates": _csv_row_count(target),
                    "ligand_candidates": _csv_row_count(ligand),
                }
            )

        if target_csvs:
            merge_csvs(
                target_csvs,
                "target_gene",
                "target_score",
                output_path=core_dir / "docking_targets.csv",
                source_labels=source_labels,
            )
        if ligand_csvs:
            merge_csvs(
                ligand_csvs,
                "inchikey",
                "ligand_score",
                output_path=core_dir / "docking_ligands.csv",
                source_labels=source_labels,
            )

        target_count = _csv_row_count(core_dir / "docking_targets.csv")
        ligand_count = _csv_row_count(core_dir / "docking_ligands.csv")
        can_enter_docking = target_count > 0 and ligand_count > 0
        qc_status = "passed" if can_enter_docking else "no_result"
        qc = {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "module": "network_pharmacology_formula_candidate_union",
            "scope": "candidate_union_only",
            "status": qc_status,
            "workflow_status": qc_status,
            "formula_name": formula_name,
            "disease_name": disease,
            "task_id": context.get("task_id"),
            "run_id": context["run_id"],
            "source_run_count": len(paths),
            "source_runs": source_summaries,
            "counts": {
                "source_herbs": len(source_entities),
                "docking_targets": target_count,
                "docking_ligands": ligand_count,
            },
            "can_enter_docking": can_enter_docking,
            "merge_identity_policy": [
                "non_empty_inchikey",
                "pubchem_cid",
                "isomeric_smiles_before_canonical_smiles",
                "ligand_id_or_name",
                "unique_source_row",
            ],
            "not_recomputed": [
                "compound_disease_network",
                "ppi_network",
                "enrichment",
                "cross_herb_qc",
            ],
            "warning": "该目录是各单味药筛选候选的复方并集，不代表已重建完整复方网络。",
            "generated_at": utc_now(),
        }
        _atomic_write_json(report_dir / "qc_report.json", qc)
        os.replace(core_dir, final / "07_core_targets")
        os.replace(report_dir, final / "08_report")
        staging.rmdir()

        binding = _network_binding_payload(
            context,
            entity_kind="formula",
            entity=formula_name,
            disease=disease,
            source_index=0,
            output_dir=final,
            source_runs=source_run_refs,
            scope="formula_candidate_union",
        )
        _atomic_write_json(final / RUN_BINDING_FILENAME, binding)
        _verify_network_binding(final, context)
        _update_latest_run(
            final,
            context,
            input_mode="formula",
            entity_name=formula_name,
            herb_name=formula_name,
            compound_name="",
            disease_name=disease,
            config_path="",
        )
    except Exception:
        if final.exists():
            checked_final = validate_network_run_dir(final)
            shutil.rmtree(checked_final)
        raise

    print(f"\n📦 复方候选并集合并完成 → {final}")
    return str(final)

# ── 主流程 ─────────────────────────────────────────────
def run_pipeline(params: dict) -> dict[str, Any]:
    """Execute requested stages and persist a machine-verifiable outcome contract."""
    params = validate_pipeline_params(apply_requested_mode_contract(params))
    herbs = params["herbs"]
    compound = params["compound"]
    disease = params["disease"]
    mode = params["mode"]
    print(f"📋 提取结果：{json.dumps(params, ensure_ascii=False)}")

    tracker = PipelineTracker(params)
    context = tracker.context
    run_dir: Optional[str] = None
    dock_dir: Optional[str] = None
    reverse_dir: Optional[str] = None
    try:
        if "network" in MODE_STAGES[mode]:
            tracker.start("network")
            source_dirs: list[str] = []
            source_statuses: list[dict[str, str]] = []
            if herbs:
                for source_index, herb in enumerate(herbs, start=1):
                    print(f"\n🌿 处理复方成分：{herb}")
                    source_dir = run_network_pharm(
                        herb,
                        None,
                        disease,
                        context=context,
                        source_index=source_index,
                    )
                    source_dirs.append(source_dir)
                    status, reason = classify_network_run(source_dir)
                    source_statuses.append({"entity": herb, "status": status, "reason": reason})
                if len(source_dirs) > 1:
                    formula = params.get("formula_name") or "+".join(herbs[:3])
                    run_dir = merge_network_runs(
                        source_dirs,
                        formula,
                        disease,
                        context=context,
                        source_entities=herbs,
                    )
                else:
                    run_dir = source_dirs[0]
            else:
                run_dir = run_network_pharm(
                    "",
                    compound,
                    disease,
                    context=context,
                    source_index=1,
                )
                status, reason = classify_network_run(run_dir)
                source_statuses.append({"entity": compound or "", "status": status, "reason": reason})

            network_status, network_reason = classify_network_run(run_dir)
            _, artifacts = network_artifacts(run_dir)
            tracker.finish(
                "network",
                network_status,
                reason=network_reason,
                output_dir=Path(run_dir),
                required_artifacts=["01_network_pharmacology/qc_report.json"],
                artifact_paths=artifacts,
                details={"source_runs": source_statuses},
            )
        else:
            if context.get("task_id") is not None:
                raise PipelineContractError("Web 任务禁止从全局 latest_run 继承其他任务产物")
            run_dir = read_latest_run()
            print(f"📂 CLI 模式使用已验证路径的最新网药结果：{run_dir}")

        if "docking" in MODE_STAGES[mode]:
            tracker.start("docking")
            dock_dir = run_docking(
                run_dir,
                params["extra_targets"],
                params["extra_ligands"],
                params["ph"],
                context=context if "network" in MODE_STAGES[mode] else None,
            )
            if dock_dir is None:
                tracker.finish(
                    "docking",
                    "no_result",
                    reason="no_eligible_target_ligand_inputs",
                    details={"upstream_network_run": run_dir},
                )
            else:
                _, artifacts = docking_artifacts(dock_dir)
                tracker.finish(
                    "docking",
                    "completed",
                    reason="validated_scientific_docking_results_available",
                    output_dir=Path(dock_dir),
                    required_artifacts=[
                        "02_molecular_docking/docking_scores_ranked.csv",
                        "02_molecular_docking/validation_summary.txt",
                    ],
                    artifact_paths=artifacts,
                )

        if "reverse" in MODE_STAGES[mode]:
            tracker.start("reverse")
            if mode == "reverse":
                candidate_dock_dir = _resolved_within(
                    (Path(run_dir) / "09_docking").absolute(), Path(run_dir)
                )
                docking_artifacts(candidate_dock_dir)
                dock_dir = str(candidate_dock_dir)
            if dock_dir is None:
                tracker.finish(
                    "reverse",
                    "no_result",
                    reason="upstream_docking_produced_no_scientific_result",
                )
            else:
                reverse_dir = run_reverse_design(dock_dir)
                reverse_status, reverse_reason, _, artifacts = classify_reverse_design(reverse_dir)
                tracker.finish(
                    "reverse",
                    reverse_status,
                    reason=reverse_reason,
                    output_dir=Path(reverse_dir),
                    required_artifacts=[
                        "03_reverse_design/qc_report.json",
                        "03_reverse_design/accepted_candidates.csv",
                        "03_reverse_design/report.html",
                    ],
                    artifact_paths=artifacts,
                )

        tracker.start("packaging")
        collect_results(
            run_dir,
            dock_dir,
            reverse_dir,
            params,
            destination=tracker.result_dir,
            context=context if "network" in MODE_STAGES[mode] else None,
        )
        if "network" in MODE_STAGES[mode]:
            tracker.bind_packaged_stage(
                "network",
                "01_network_pharmacology",
                ["01_network_pharmacology/qc_report.json"],
            )
        if "docking" in MODE_STAGES[mode]:
            docking_required = []
            if tracker.payload["stages"]["docking"]["status"] == "completed":
                docking_required = [
                    "02_molecular_docking/docking_scores_ranked.csv",
                    "02_molecular_docking/validation_summary.txt",
                ]
            tracker.bind_packaged_stage("docking", "02_molecular_docking", docking_required)
        if "reverse" in MODE_STAGES[mode]:
            reverse_required = []
            if tracker.payload["stages"]["reverse"]["status"] == "completed":
                reverse_required = [
                    "03_reverse_design/qc_report.json",
                    "03_reverse_design/accepted_candidates.csv",
                    "03_reverse_design/report.html",
                ]
            tracker.bind_packaged_stage("reverse", "03_reverse_design", reverse_required)
        tracker.finish(
            "packaging",
            "completed",
            reason="task_owned_result_package_committed",
            output_dir=tracker.result_dir,
            required_artifacts=["说明书.txt"],
            artifact_paths=["说明书.txt"],
        )
        tracker.complete()
        return tracker.payload
    except Exception as exc:
        tracker.fail(exc)
        raise


def collect_results(
    run_dir: str,
    dock_dir: Optional[str],
    reverse_dir: Optional[str],
    params: dict,
    *,
    destination: Optional[Path | str] = None,
    context: Optional[dict[str, Any]] = None,
) -> Path:
    """把各阶段核心结果文件复制到项目根目录 results/ 下，分门别类。"""
    herbs = params.get("herbs", [])
    herb_label = "+".join(herbs[:3]) if herbs else (params.get("herb") or params.get("compound") or "unknown")
    disease = params.get("disease", "unknown")
    run_path = validate_network_run_dir(run_dir)
    if context is not None:
        _verify_network_binding(run_path, context)
    if dock_dir:
        dock_path = _resolved_within(Path(dock_dir).absolute(), run_path)
    else:
        dock_path = None
    if reverse_dir:
        if dock_path is None:
            raise PipelineContractError("反向设计目录存在但对接目录为空")
        reverse_path = _resolved_within(Path(reverse_dir).absolute(), dock_path)
    else:
        reverse_path = None
    if destination is not None:
        dest = _resolved_within(Path(destination).absolute(), RESULTS_ROOT, must_exist=False)
    else:
        legacy_label = _safe_component(f"{herb_label}_{disease}", fallback="npflow")
        dest = _resolved_within(
            (RESULTS_ROOT / "runs" / f"{legacy_label}__{uuid.uuid4().hex[:12]}").absolute(),
            RESULTS_ROOT,
            must_exist=False,
        )
    dest.mkdir(parents=True, exist_ok=True)

    # ── 01_network_pharmacology ──
    np_dest = dest / "01_network_pharmacology"
    np_dest.mkdir(exist_ok=True)
    np_files = [
        "04_intersection/intersection_targets.csv",
        "04_intersection/intersection_summary.json",
        "04_intersection/compound_disease_target_matrix.csv",
        "05_ppi/ppi_edges.tsv",
        "05_ppi/ppi_nodes.tsv",
        "05_ppi/ppi_network.graphml",
        "05_ppi/ppi_summary.json",
        "06_enrichment/enrichment_all.csv",
        "06_enrichment/kegg.csv",
        "06_enrichment/go_bp.csv",
        "06_enrichment/go_cc.csv",
        "06_enrichment/go_mf.csv",
        "06_enrichment/reactome.csv",
        "06_enrichment/enrichment_summary.json",
        "07_core_targets/docking_targets.csv",
        "07_core_targets/docking_ligands.csv",
        "07_core_targets/rejected_user_requested_ligands.csv",
        "08_report/qc_report.json",
    ]
    np_files.append(RUN_BINDING_FILENAME)
    for f in np_files:
        src = run_path / f
        if src.is_file():
            src = _resolved_within(src, run_path)
            dest_name = "network_run_manifest.json" if f == RUN_BINDING_FILENAME else Path(f).name
            _atomic_copy(src, np_dest / dest_name)

    # ── 02_molecular_docking ──
    dock_dest = dest / "02_molecular_docking"
    dock_dest.mkdir(exist_ok=True)
    dock_files = [
        "06_scores/docking_scores_ranked.csv",
        "06_scores/docking_pair_qc.csv",
        "08_docking_report/docking_report.html",
        "08_docking_report/docking_qc_report.json",
        "validation/validation_summary.txt",
        "03_ligand_prepared/ligands.sdf",
        "03_ligand_prepared/ligand_identity_map.csv",
    ]
    if dock_path:
        for f in dock_files:
            src = dock_path / f
            if src.is_file():
                src = _resolved_within(src, dock_path)
                _atomic_copy(src, dock_dest / Path(f).name)

    # ── 03_reverse_design ──
    rev_dest = dest / "03_reverse_design"
    rev_dest.mkdir(exist_ok=True)
    if reverse_path:
        # 接受候选 CSV + SMILES
        acc = reverse_path / "reverse_design_accepted_candidates.csv"
        if acc.is_file():
            acc = _resolved_within(acc, reverse_path)
            _atomic_copy(acc, rev_dest / "accepted_candidates.csv")
            with acc.open(newline="", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
            if len(reader) > 1:
                hdr = reader[0]
                smi_col = hdr.index("candidate_smiles") if "candidate_smiles" in hdr else (hdr.index("smiles") if "smiles" in hdr else -1)
                parent_smi_col = hdr.index("parent_smiles") if "parent_smiles" in hdr else -1
                name_col = hdr.index("parent_ligand_name") if "parent_ligand_name" in hdr else 0
                smiles_lines = ["parent_ligand\tparent_smiles\tcandidate_smiles"]
                for row in reader[1:]:
                    name = row[name_col] if name_col < len(row) else "?"
                    psmi = row[parent_smi_col] if parent_smi_col >= 0 and parent_smi_col < len(row) else ""
                    csmi = row[smi_col] if smi_col >= 0 and smi_col < len(row) else ""
                    if csmi:
                        smiles_lines.append(f"{name}\t{psmi}\t{csmi}")
                if len(smiles_lines) > 1:
                    _atomic_write_text(rev_dest / "accepted_smiles.txt", "\n".join(smiles_lines) + "\n")
        # 接受候选 SDF — 只复制通过筛选的
        accepted_ids = set()
        for pair_dir in reverse_path.glob("pair_*"):
            for rd in sorted(pair_dir.glob("round_*")):
                acc_csv = rd / "accepted_candidates.csv"
                if acc_csv.is_file():
                    acc_csv = _resolved_within(acc_csv, reverse_path)
                    with acc_csv.open(newline="", encoding="utf-8-sig") as f:
                        acc_rows = list(csv.reader(f))
                    if len(acc_rows) > 1:
                        hdr = acc_rows[0]
                        id_col = hdr.index("candidate_id") if "candidate_id" in hdr else 0
                        sdf_dest = rev_dest / "accepted_sdf"
                        sdf_dest.mkdir(exist_ok=True)
                        for row in acc_rows[1:]:
                            cid = row[id_col] if id_col < len(row) else ""
                            if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid or "") or ".." in cid:
                                continue
                            sdf_file = rd / "candidate_sdf" / f"{cid}.sdf"
                            if sdf_file.is_file() and cid not in accepted_ids:
                                sdf_file = _resolved_within(sdf_file, reverse_path)
                                _atomic_copy(sdf_file, sdf_dest / sdf_file.name)
                                accepted_ids.add(cid)
        # 整体对比表
        comp = reverse_path / "natural_vs_optimized_comparison.csv"
        if comp.is_file():
            _atomic_copy(_resolved_within(comp, reverse_path), rev_dest / "comparison.csv")
        # 报告
        qc = reverse_path / "reverse_design_qc.json"
        if qc.is_file():
            _atomic_copy(_resolved_within(qc, reverse_path), rev_dest / "qc_report.json")
        html = reverse_path / "reverse_design_report.html"
        if html.is_file():
            _atomic_copy(_resolved_within(html, reverse_path), rev_dest / "report.html")
    else:
        if params.get("mode") in ("full", "reverse", "docking+reverse"):
            message = "本任务请求反向设计，但因缺少可用对接结果或候选种子，反向设计未执行。\n"
        else:
            message = "本任务模式未执行反向设计。\n"
        _atomic_write_text(rev_dest / "reverse_design_not_run.txt", message)

    # 后处理 HTML 报告：InChIKey → 来源药材的化合物名 + 靶点标注
    html_path = dock_dest / "docking_report.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        ligands_csv = run_path / "07_core_targets/docking_ligands.csv"
        ikey_map = {}
        if ligands_csv.is_file():
            ligands_csv = _resolved_within(ligands_csv, run_path)
            with ligands_csv.open(newline="", encoding="utf-8-sig") as f:
                lr = list(csv.reader(f))
            if len(lr) > 1:
                lh = lr[0]
                ikey_idx = lh.index("inchikey") if "inchikey" in lh else 0
                name_idx = lh.index("source_ingredient_names") if "source_ingredient_names" in lh else 0
                herb_idx = lh.index("source_herb_names") if "source_herb_names" in lh else None
                for row in lr[1:]:
                    ikey = row[ikey_idx] if ikey_idx < len(row) else ""
                    cname = row[name_idx] if name_idx < len(row) else ""
                    herb = row[herb_idx] if herb_idx is not None and herb_idx < len(row) else ""
                    if ikey:
                        ikey_map[ikey] = (cname, herb)
        # 匹配 <td>TARGET__INCHIKEY__PDB_boxN</td> 格式并替换
        def annotate_pair(m):
            target = m.group(1)
            ikey = m.group(2)
            pdb = m.group(3)
            cname, herb = ikey_map.get(ikey, (ikey[:20], ""))
            label = f"{herb}的{cname}" if herb else cname
            return f"<td>{target}__{label}__{pdb} | 靶点:{target}</td>"
        html = re.sub(
            r"<td>([A-Z0-9]+)__([A-Z]{14}-[A-Z]{10}-[A-Z](?:-[A-Z]+)?)__([A-Za-z0-9_]+)</td>",
            annotate_pair,
            html
        )
        _atomic_write_text(html_path, html)
        print("  🏷️ HTML 报告已标注化合物名和靶点")

    evidence_dock = dock_path
    if evidence_dock is None and (run_path / "09_docking/docking_no_result.json").is_file():
        evidence_dock = _resolved_within(run_path / "09_docking", run_path)
        _atomic_copy(evidence_dock / "docking_no_result.json", dock_dest / "docking_no_result.json")
    collect_evidence(run_path, evidence_dock, reverse_path, dest)
    # 保存结果说明书
    report = generate_report(run_dir, dock_dir, reverse_dir, params)
    _atomic_write_text(dest / "说明书.txt", generate_manual(dest, report, params))
    total = sum(1 for _ in dest.rglob("*") if _.is_file())
    print(f"\n📁 结果已汇总到 → {dest}  ({total} 个文件)")
    return dest


def interactive():
    """交互模式：持续对话，输入 quit/exit 退出。"""
    print("=" * 60)
    print("  🌿 NPFlow Agent — 交互模式")
    print("  输入自然语言即可，例如：")
    print("    > 用丹参和冠心病跑网药和对接")
    print("    > 金银花+influenza 跑全流程")
    print("    > 只跑网药：黄连+糖尿病")
    print("  输入 quit 或 exit 退出")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n💬 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("👋 再见！")
            break

        print("🤖 分析中...")
        try:
            params = extract_params(user_input)
            run_pipeline(params)
        except Exception as e:
            print(f"❌ 出错：{e}")


if __name__ == "__main__":
    try:
        if len(sys.argv) < 2:
            interactive()
        else:
            params = extract_params(" ".join(sys.argv[1:]))
            run_pipeline(params)
    except Exception as e:
        print(f"❌ 错误：{e}")
        sys.exit(1)

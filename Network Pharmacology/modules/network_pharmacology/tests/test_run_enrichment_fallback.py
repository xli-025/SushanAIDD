from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_enrichment.py"
SPEC = importlib.util.spec_from_file_location("run_enrichment", MODULE_PATH)
run_enrichment = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(run_enrichment)


def test_gprofiler_success_skips_fallback(monkeypatch):
    expected = [{"source": "GO:BP", "p_value": 0.01}]
    called = {"enrichr": False}

    monkeypatch.setattr(run_enrichment, "run_gprofiler", lambda genes: (expected, []))

    def fail_if_called(genes):
        called["enrichr"] = True
        raise AssertionError("fallback should not run when gProfiler succeeds")

    monkeypatch.setattr(run_enrichment, "run_enrichr", fail_if_called)
    rows, warnings, provider = run_enrichment.run_enrichment(["TP53", "TNF"])

    assert rows == expected
    assert warnings == []
    assert provider == "gprofiler"
    assert called["enrichr"] is False


def test_enrichr_fallback_success_replaces_empty_gprofiler(monkeypatch):
    fallback_rows = [{"source": "KEGG", "p_value": 0.02, "name": "Apoptosis"}]
    monkeypatch.setattr(
        run_enrichment,
        "run_gprofiler",
        lambda genes: ([], ["富集 API 暂不可用，已降级继续。g:Profiler request failed: timeout"]),
    )
    monkeypatch.setattr(run_enrichment, "run_enrichr", lambda genes: (fallback_rows, []))

    rows, warnings, provider = run_enrichment.run_enrichment(["TP53", "TNF"])

    assert rows == fallback_rows
    assert provider == "enrichr_fallback"
    assert any("gProfiler 不可用，已使用 Enrichr/Reactome 备用富集" in warning for warning in warnings)
    assert not any("富集 API 暂不可用" in warning for warning in warnings)


def test_all_providers_fail_keeps_degraded_warning(monkeypatch):
    monkeypatch.setattr(run_enrichment, "run_gprofiler", lambda genes: ([], ["gProfiler failed"]))

    def fail_enrichr(genes):
        raise RuntimeError("enrichr failed")

    monkeypatch.setattr(run_enrichment, "run_enrichr", fail_enrichr)
    rows, warnings, provider = run_enrichment.run_enrichment(["TP53", "TNF"])

    assert rows == []
    assert provider == "none"
    assert any("富集 API 暂不可用，已降级继续" in warning for warning in warnings)

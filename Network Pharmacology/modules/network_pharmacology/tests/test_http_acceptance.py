"""Offline transport regressions; keep the real retry and provider logic."""
from types import SimpleNamespace

import pytest
import requests

from modules.network_pharmacology.scripts import common, run_enrichment as enrichment


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected real network request")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("method", ["get", "post"])
def test_zero_retries_still_performs_initial_request(monkeypatch, method):
    calls = []
    response = SimpleNamespace(status_code=200)
    def send(url, **kwargs):
        calls.append((url, kwargs))
        return response
    monkeypatch.setattr(requests, method, send)
    result = getattr(common, "http_" + method)("https://fixture.invalid", timeout=7, retries=0)
    assert result is response
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 7


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("failure", ["timeout", "network", "server"])
def test_http_exhaustion_is_bounded_and_reported(monkeypatch, method, failure):
    calls = []
    def send(url, **kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise requests.Timeout("fixture timeout")
        if failure == "network":
            raise requests.ConnectionError("fixture disconnect")
        return SimpleNamespace(status_code=503, url=url)
    monkeypatch.setattr(requests, method, send)
    with pytest.raises(common.WorkflowError, match="failed"):
        getattr(common, "http_" + method)("https://fixture.invalid", retries=2)
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_response_is_preserved(monkeypatch, status):
    response = SimpleNamespace(status_code=status)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    assert common.http_post("https://fixture.invalid") is response


def test_gprofiler_default_config_reaches_transport_and_parses(monkeypatch):
    monkeypatch.delenv("NPFLOW_GPROFILER_RETRIES", raising=False)
    monkeypatch.setenv("NPFLOW_GPROFILER_TIMEOUT", "7")
    calls = []
    def send(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {
            "result": [{"query": "query_1", "source": "GO:BP", "intersections": [["IDA"], ["IEA"]]}],
            "meta": {"genes_metadata": {"query": {"query_1": {
                "ensgs": ["ENSG1", "ENSG2"], "mapping": {"TP53": ["ENSG1"], "TNF": ["ENSG2"]}}}}}})
    monkeypatch.setattr(requests, "post", send)
    rows, warnings = enrichment.run_gprofiler(["TP53", "TNF"])
    assert len(calls) == 1
    assert calls[0]["timeout"] == 7
    assert calls[0]["json"]["query"] == ["TP53", "TNF"]
    assert rows[0]["intersections"] == "TP53; TNF"
    assert warnings == []


@pytest.mark.parametrize("payload", [[], {}, {"result": None}, {"result": "bad"}, {"result": [None]}, {"result": [{"intersections": 3}]}, {"result": [{"intersections": [["TP53"], None]}]}])
def test_malformed_gprofiler_response_reaches_existing_fallback(monkeypatch, payload):
    monkeypatch.setenv("NPFLOW_ENRICHMENT_PROVIDER", "auto")
    monkeypatch.setattr(enrichment, "http_post", lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    expected = [{"source": "KEGG", "p_value": 0.02}]
    monkeypatch.setattr(enrichment, "run_enrichr", lambda genes: (expected, []))
    rows, warnings, provider = enrichment.run_enrichment(["TP53", "TNF"])
    assert rows == expected
    assert provider == "enrichr_fallback"
    assert warnings


@pytest.mark.parametrize("payload", [{"result": []}])
def test_empty_gprofiler_response_is_valid_empty(monkeypatch, payload):
    monkeypatch.setattr(enrichment, "http_post", lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    assert enrichment.run_gprofiler(["TP53", "TNF"]) == ([], [])


def test_tcmsp_zero_budget_performs_warmup_and_request(monkeypatch):
    calls = []
    response = SimpleNamespace(status_code=200)
    def send(url, **kwargs):
        calls.append(url)
        return response
    monkeypatch.setattr(common._TCMSP_SESSION, "get", send)
    assert common.http_get_tcmsp("https://fixture.invalid", retries=0) is response
    assert calls == [common.TCMSP_HEADERS["Referer"], "https://fixture.invalid"]

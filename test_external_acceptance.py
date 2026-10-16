"""Transport and import-time configuration acceptance, without live services."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

import npflow_agent as agent


@pytest.mark.parametrize("error", [TimeoutError("fixture"), URLError("fixture"), HTTPError("https://fixture.invalid", 401, "fixture", {}, None), HTTPError("https://fixture.invalid", 503, "fixture", {}, None)])
def test_llm_transport_failure_is_actionable(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(agent.urllib.request, "urlopen", fail)
    with pytest.raises(ValueError, match="Ollama"):
        agent.extract_params("金银花：只跑网药")


@pytest.mark.parametrize("payload", [[], {}, {"choices": []}, {"choices": [{"message": {"content": 42}}]}])
def test_llm_malformed_response_is_rejected(monkeypatch, payload):
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *a, **k: SimpleNamespace(read=lambda: json.dumps(payload).encode()))
    with pytest.raises(ValueError, match="Ollama"):
        agent.extract_params("fixture")


def test_llm_request_and_unicode_round_trip(monkeypatch):
    params = {"herbs": ["金银花"], "disease": "influenza", "mode": "network", "ph": 7.4}
    def respond(request, timeout):
        body = json.loads(request.data)
        assert body["model"] == agent.MODEL
        assert "金银花" in body["messages"][0]["content"]
        assert timeout == 120
        return SimpleNamespace(read=lambda: json.dumps({"choices": [{"message": {"content": json.dumps(params)}}]}).encode())
    monkeypatch.setattr(agent.urllib.request, "urlopen", respond)
    result = agent.extract_params("金银花 influenza 只跑网药")
    assert result["herbs"] == ["金银花"]
    assert result["extra_targets"] == []
    assert result["mode"] == "network"


def test_web_config_overrides_do_not_initialize_database(tmp_path, monkeypatch):
    values = {"NPFLOW_ROOT": str(tmp_path / "中文 root"), "NPFLOW_WEB_DB": str(tmp_path / "fixture.sqlite3"), "NPFLOW_WEB_PORT": "19000", "NPFLOW_MAX_CONCURRENCY": "2", "NPFLOW_HTTP_BIND_HOST": "127.0.0.1"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("acceptance_server", Path(__file__).parent / "webapp/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROOT == Path(values["NPFLOW_ROOT"]).resolve()
    assert module.DEFAULT_PORT == 19000
    assert module.DEFAULT_CONCURRENCY == 2
    assert module.DB_PATH == Path(values["NPFLOW_WEB_DB"])
    assert not module.DB_PATH.exists()


def test_active_entrypoints_and_static_references_exist():
    import re
    from urllib.parse import urlsplit
    root = Path(__file__).parent
    for value in [agent.NP_AGENT, agent.DOCKING_AGENT, agent.REVERSE_AGENT]:
        assert Path(value).is_file(), value
    static = root / "webapp/static"
    for page in static.glob("*.html"):
        for reference in re.findall(r'(?:src|href)=["\']([^"\']+)', page.read_text(encoding="utf-8")):
            path = urlsplit(reference).path
            if path.startswith("/static/"):
                assert (static / path.removeprefix("/static/")).is_file(), (page, reference)

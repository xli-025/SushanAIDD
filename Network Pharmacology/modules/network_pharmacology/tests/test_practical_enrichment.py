from modules.network_pharmacology.scripts import run_enrichment as enrichment
from modules.network_pharmacology.scripts import build_ppi
from types import SimpleNamespace
import pytest


def test_evidence_codes_are_not_gene_names():
    item = {"query": "query_1", "intersections": [["IDA"], [], ["IEA"]]}
    meta = {"genes_metadata": {"query": {"query_1": {"ensgs": ["E1", "E2", "E3"],
        "mapping": {"TP53": ["E1"], "TNF": ["E2"], "IL6": ["E3"]}}}}}
    genes, codes, status = enrichment.decode_gprofiler_genes(item, meta)
    assert genes == ["TP53", "IL6"] and codes == [["IDA"], ["IEA"]]
    assert status == "mapped_from_response_metadata"
    assert enrichment.decode_gprofiler_genes(item, {})[0] == []


def test_empty_success_does_not_search_until_positive(monkeypatch):
    monkeypatch.setattr(enrichment, "run_gprofiler", lambda genes: ([], []))
    monkeypatch.setattr(enrichment, "run_enrichr", lambda genes: pytest.fail("must not call fallback"))
    rows, warnings, provider = enrichment.run_enrichment(["TP53", "TNF"])
    assert not rows and not warnings and provider == "gprofiler"


def test_raw_adjusted_and_zero_are_preserved():
    row = enrichment.row_from_enrichr([1, "term", .0001, 0, 0, ["TP53"], .2], "GO:BP", 2)
    assert row["raw_p_value"] == .0001 and row["adjusted_p_value"] == .2 and not row["significant"]
    row = enrichment.row_from_reactome({"entities": {"pValue": 0, "fdr": 0}}, 2)
    assert row["raw_p_value"] == 0 and row["adjusted_p_value"] == 0 and row["significant"]


def test_rate_limit_is_not_zero_edges(monkeypatch):
    monkeypatch.setattr(build_ppi, "http_post", lambda *a, **k: SimpleNamespace(status_code=429))
    with pytest.raises(RuntimeError, match="429"):
        build_ppi.fetch_string_network(["TP53", "TNF"], 700)

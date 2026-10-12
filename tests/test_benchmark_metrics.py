import pytest
from benchmarks.run import metrics, latency


def test_rank_metrics_deduplicate_occurrences_and_penalize_rank():
    result = metrics(['wrong','a','a','b'], {'a','b','missing'})
    assert result['mrr10'] == .5
    assert result['recall10'] == pytest.approx(2/3)
    assert 0 < result['ndcg10'] < 1


def test_perfect_and_empty_rankings():
    assert metrics(['a','b'], {'a','b'}) == {'ndcg10':1,'recall10':1,'mrr10':1}
    assert metrics([], {'a'}) == {'ndcg10':0,'recall10':0,'mrr10':0}
    assert latency(list(range(1,101))) == {'n':100,'p50_ms':50.5,'p95_ms':95}

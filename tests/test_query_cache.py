from src.models import SearchRequest, SearchResponse, SearchHit, DocumentAnchor
from src.query_cache import QueryResultCache


def hit(did,score=1,version=1):
    return SearchHit(document_id=did,target=DocumentAnchor(version=version,start=0,end=1),context='x',score=score)


def test_admission_count_lru_idle_and_memory_bounds():
    now=[0.]
    cache=QueryResultCache(2,100000,10,clock=lambda:now[0])
    calls=[]
    def execute(ids):
        calls.append(ids)
        return SearchResponse(hits=[hit('a')])
    def query(text):return cache.search(SearchRequest(text=text),lambda:1,lambda _:[],execute)
    query('one');assert not cache.entries and len(cache.probation)==1
    query('one');assert len(cache.entries)==1
    query('two');query('two');query('one')
    query('three');query('three')
    assert [key[0] for key in cache.entries]==['one','three']
    before=len(calls);query('one');assert len(calls)==before
    now[0]=11
    query('new');assert not cache.entries
    for i in range(10):query(str(i))
    assert len(cache.probation)<=2 and cache.bytes<=cache.max_bytes
    # Fresh tiny budget bypasses entries rather than retaining oversized payloads.
    tiny=QueryResultCache(2,1)
    for _ in range(3):tiny.search(SearchRequest(text='one'),lambda:1,lambda _:[],execute)
    assert tiny.bytes==0 and not tiny.entries and not tiny.probation


def test_incremental_new_match_refresh_and_refill():
    cache=QueryResultCache()
    request=SearchRequest(text='x',limit=1)
    generation=[1]; changed=[]; rows={'a':hit('a',2),'b':hit('b',1)};calls=[]
    def execute(ids):
        calls.append(ids)
        values=[v for k,v in rows.items() if ids is None or k in ids]
        values.sort(key=lambda h:(-h.score,h.document_id))
        return SearchResponse(hits=values if ids is not None else values[:1])
    def query():return cache.search(request,lambda:generation[0],lambda _:changed,execute)
    query();query();calls.clear()
    assert query().hits[0].document_id=='a' and calls==[]
    rows['b']=hit('b',3);changed[:]=['b'];generation[0]+=1
    assert query().hits[0].document_id=='b' and calls==[['b']]
    calls.clear();rows['b']=hit('b',3,2);generation[0]+=1
    assert query().hits[0].target.version==2 and calls==[['b']]
    calls.clear();del rows['b'];generation[0]+=1
    assert query().hits[0].document_id=='a' and calls==[['b'],None]
    generation[0]+=1;calls.clear()
    cache.search(request,lambda:generation[0],lambda _:None,execute)
    assert calls==[None]  # Lost history safely rebuilds.
    # An older database snapshot cannot reuse or overwrite a newer entry.
    cache.search(request,lambda:1,lambda _:[],execute)
    assert next(iter(cache.entries.values())).generation==generation[0]


def test_payload_budget_and_disabled_cache():
    request=SearchRequest(text='x')
    large=SearchResponse(hits=[hit('a')])
    large.hits[0].context='x'*10000
    cache=QueryResultCache(2,2000)
    for _ in range(4):
        cache.search(request,lambda:1,lambda _:[],lambda _:large)
        assert cache.bytes <= 2000
    assert not cache.entries
    disabled=QueryResultCache(max_queries=0)
    def unexpected():raise AssertionError('Disabled cache must not read generation')
    assert disabled.search(request,unexpected,lambda _:[],lambda _:large)==large

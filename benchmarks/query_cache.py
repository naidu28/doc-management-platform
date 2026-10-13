"""Corpus query-result cache benchmark using the real API and PostgreSQL."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import time

from fastapi.testclient import TestClient
from benchmarks.run import Stack, corpus, latency, SHA
from src.api import create_app
from src.models import CreateEditReservationRequest, DocumentAnchor
from src.postgres import PostgresStore
from src.query_cache import QueryResultCache
from src.runtime import DocumentService
from src.services import ReplaceCommand


def run(output, repeats, dsn):
    docs,qrels=corpus()
    trials=[]
    with Stack(output,dsn) as stack:
        print('Indexing all 510 CUAD contracts',flush=True)
        for did,text,title in docs:
            stack.store.create_document(did,text,title)
            stack.store.index_revision(did,1)
        stores={name:PostgresStore(stack.dsn)
                for name in ('disabled','cold','admission','warm')}
        stores['disabled'].query_cache=QueryResultCache(max_queries=0)
        clients={name:TestClient(create_app(service:=DocumentService(store),service)) for name,store in stores.items()}
        def measure(name,payload,workload,repeat):
            cache=stores[name].query_cache
            counters=(cache.hits,cache.refreshes,cache.refills)
            start=time.perf_counter()
            response=clients[name].post('/search',json=payload)
            elapsed=(time.perf_counter()-start)*1000
            response.raise_for_status()
            trials.append(dict(workload=workload,query=payload['text'],mode=payload['mode'],variant=name,
                repeat=repeat,milliseconds=elapsed,hits=len(response.json()['hits']),
                cache_hits=cache.hits-counters[0],refreshes=cache.refreshes-counters[1],
                refills=cache.refills-counters[2],entries=len(cache.entries),probation=len(cache.probation),
                estimated_bytes=cache.bytes))
            return response.json()
        def request(name,payload):
            response=clients[name].post('/search',json=payload)
            response.raise_for_status()
            return response.json()
        try:
            for category in sorted(qrels):
                payload={'text':' '.join(re.findall(r'[^\W_]+',category)).lower(),'mode':'terms','limit':10}
                print('Query: '+payload['text'],flush=True)
                expected=request('disabled',payload)  # Warm PostgreSQL for this query.
                request('warm',payload);request('warm',payload)
                for repeat in range(repeats):
                    names=['disabled','cold','admission','warm']
                    names=names[repeat%4:]+names[:repeat%4]
                    for name in names:
                        if name in ('cold','admission'):
                            stores[name].query_cache=QueryResultCache()
                            if name=='admission':assert request(name,payload)==expected
                        assert measure(name,payload,'steady',repeat)==expected,(category,name)
            # Follow actual projection updates from a separate store/connection.
            payload={'text':'agreement','mode':'terms','limit':10}
            request('warm',payload);request('warm',payload)
            def pair(workload,repeat):
                order=['disabled','warm'] if repeat%2==0 else ['warm','disabled']
                results={name:measure(name,payload,workload,repeat) for name in order}
                assert results['disabled']==results['warm'],workload
                return results['warm']
            def replace(did,text):
                doc=stack.store.get_document(did)
                reservation=stack.store.reserve('benchmark',did,CreateEditReservationRequest(
                    target=DocumentAnchor(version=doc.version,start=0,end=len(doc.text))))
                oid=stack.store.admit(ReplaceCommand(reservation.reservation_id,text))
                while stack.store.operation(oid)['status']=='queued':stack.store.process_next()
                assert stack.store.operation(oid)['status']=='completed'
                stack.store.index_revision(did,doc.version+1)
            for repeat in range(repeats):
                print(f'Projection maintenance round {repeat+1}',flush=True)
                did=f'cache-unmatched-{repeat}'
                stack.store.create_document(did,'unrelated marker');stack.store.index_revision(did,1)
                pair('unrelated_update',repeat)
                did=f'cache-new-match-{repeat}'
                stack.store.create_document(did,'agreement '*1000);stack.store.index_revision(did,1)
                results=pair('new_match',repeat)
                assert did in {hit['document_id'] for hit in results['hits']}
                previous=next(hit for hit in results['hits'] if hit['document_id']==did)
                replace(did,'😀 agreement '+('agreement '*999))
                results=pair('anchor_update',repeat)
                fresh=next(hit for hit in results['hits'] if hit['document_id']==did)
                assert fresh['score']==previous['score'] and fresh['target']['start']==2
                replace(did,'unrelated marker')
                results=pair('winner_removed',repeat)
                assert did not in {hit['document_id'] for hit in results['hits']}
            assert all(row['entries']<=64 and row['probation']<=64 and row['estimated_bytes']<=8*1024*1024 for row in trials)
        finally:
            for client in clients.values():client.close()
    summaries=[]
    for workload in ('steady','unrelated_update','new_match','anchor_update','winner_removed'):
        for variant in ('disabled','cold','admission','warm'):
            rows=[row for row in trials if row['workload']==workload and row['variant']==variant]
            if rows:summaries.append(dict(workload=workload,variant=variant,**latency([r['milliseconds'] for r in rows]),
                cache_hits=sum(r['cache_hits'] for r in rows),refreshes=sum(r['refreshes'] for r in rows),refills=sum(r['refills'] for r in rows)))
    result=dict(timestamp=datetime.now(timezone.utc).isoformat(),environment=platform.platform(),python=platform.python_version(),
        corpus_sha256=SHA,contracts=len(docs),source_bytes=sum(len(t.encode()) for _,t,_ in docs),repeats=repeats,
        max_queries=64,max_bytes=8*1024*1024,idle_seconds=300,exact_response_equality=True,
        source_hashes={f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in ['src/query_cache.py','src/postgres.py']},
        summaries=summaries,trials=trials)
    (output/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    rows=['# Corpus query-result cache benchmark','',f"Run: {result['timestamp']}; Python {result['python']}; {result['environment']}.",'',
        '## Method','',
        'All 510 pinned CUAD contracts, '+str(result['source_bytes'])+' UTF-8 bytes, in one isolated PostgreSQL database. '
        'Forty-one category-label queries use terms mode and limit 10 across the complete corpus. '
        'Every measured cached response equals its uncached counterpart, including ranking, snippets, versions and offsets. '
        'Document-scoped search is uncached; this experiment isolates corpus result caching.', '',
        'Timings use FastAPI TestClient through real ASGI routing and threaded service calls to PostgreSQL. '
        'They include database connections and response serialization but exclude real HTTP network latency. '
        'PostgreSQL is warmed before comparison. Arms execute serially in rotating order; no concurrent load.', '',
        'Disabled: query cache off. Cold: first request after an empty application cache. '
        'Admission: second identical request, which populates the cache (first request excluded from this timing). '
        'Warm: third or later request after both admission searches. Warm retains queries across the run. '
        'Each query has '+str(repeats)+' measured repetitions per arm. Bounds: 64 results, 64 admission records, '
        '8 MiB combined estimated memory, five-minute idle expiry. Retained bytes are not process RSS.', '',
        'After steady measurements, '+str(repeats)+' rounds modify the projection through a separate store: add an unrelated document, '
        'add a new top result, change its version/anchor while preserving score, then remove its match through replacement. '
        'Update/execution/indexing time is excluded; each timed request reconciles one committed update. '
        'Maintenance queries use “agreement”; they are not directly comparable to the pooled 41-query distribution.', '',
        '## Results','', '| Workload | Arm | n | p50 ms | p95 ms | Cache hits | Incremental refreshes | Full refills |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in summaries:rows.append(f"| {r['workload']} | {r['variant']} | {r['n']} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['cache_hits']} | {r['refreshes']} | {r['refills']} |")
    base=next(r for r in summaries if r['workload']=='steady' and r['variant']=='disabled')
    warm=next(r for r in summaries if r['workload']=='steady' and r['variant']=='warm')
    rows+=['',f"Warm pooled p50 speedup: **{base['p50_ms']/warm['p50_ms']:.1f}×**; median latency reduction **{100*(1-warm['p50_ms']/base['p50_ms']):.1f}%**. "
        f"All **{len(trials)}** timed responses matched exactly. Peak retained estimate: **{max(r['estimated_bytes'] for r in trials):,} bytes**.", '',
        '## Interpretation','',
        'Warm results avoid corpus filtering, ranking and body transfer; PostgreSQL is still queried for the committed generation. '
        'The first two uses still execute the complete search. New and unchanged-score matches can be maintained from changed documents; '
        'removing a winner requires a full refill. This workload deliberately repeats queries, so it is not a production hit-rate or capacity claim. '
        'Maintenance tail estimates have only '+str(repeats)+' samples per case/arm. Idle expiry, eviction churn, multi-process duplication, '
        'parallel contention and heavy write rates are not benchmarked here. Full individual timings and source hashes are in results.json.']
    (output/'report.md').write_text('\n'.join(rows)+'\n')
    print(json.dumps(summaries,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--database-url')
    args=parser.parse_args()
    if args.repetitions<1:parser.error('repetitions must be positive')
    args.output.mkdir(parents=True,exist_ok=False)
    run(args.output,args.repetitions,args.database_url)

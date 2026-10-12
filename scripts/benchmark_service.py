"""Real SQLite admission/commit/index measurements; isolated temporary databases."""
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from redlining.storage import Store
from redlining.models import CreateEditReservationRequest, DocumentAnchor, SearchRequest
from redlining.services import ReplaceCommand

ROOT=Path(__file__).resolve().parents[1]


def timed(function):
    start=time.perf_counter()
    result=function()
    return result, (time.perf_counter()-start)*1000


def main():
    results=[]
    for relative, count in [('data/stress/cuad-composite.txt',3), ('data/legal/uscode-title42.txt',3)]:
        path=ROOT/relative
        if not path.exists():
            continue
        text=path.read_bytes().decode('utf-8')
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary)/'benchmark.sqlite')
            _,load_ms=timed(lambda:store.create_document('large',text))
            _,initial_index_ms=timed(lambda:store.index_revision('large',1))
            samples=[]
            for index in range(count):
                version=index+1
                start=len(text)//2
                request=CreateEditReservationRequest(target=DocumentAnchor(version=version,start=start,end=start+10))
                reservation,reserve_ms=timed(lambda:store.reserve('bench','large',request))
                oid,admit_ms=timed(lambda:store.admit(ReplaceCommand(reservation.reservation_id,'TEST-%05d'%index)))
                _,commit_ms=timed(store.process_next)
                assert store.operation(oid).document.text[start:start+10]=='TEST-%05d'%index
                _,index_ms=timed(lambda:store.index_revision('large',version+1))
                response,search_ms=timed(lambda:store.search(SearchRequest(text='TEST-%05d'%index,mode='literal',limit=1)))
                assert response.hits[0].target==DocumentAnchor(version=version+1,start=start,end=start+10)
                samples.append(dict(reserve_ms=reserve_ms,admit_ms=admit_ms,commit_ms=commit_ms,reindex_ms=index_ms,literal_search_ms=search_ms))
            results.append(dict(source=relative,utf8_bytes=path.stat().st_size,characters=len(text),load_ms=load_ms,
                initial_index_ms=initial_index_ms,samples=samples,
                median_ms={k:statistics.median(s[k] for s in samples) for k in samples[0]}))
    report={'scope':'Local SQLite service, three sequential replacements per document; no HTTP/Redis/network time or load percentiles.',
            'results':results}
    (ROOT/'data/service-benchmark.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()

"""Isolated end-to-end benchmark. Run from repository root; see benchmarks/README.md."""
import argparse
import concurrent.futures as futures
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from uuid import uuid4

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from src.postgres import PostgresStore

ROOT = Path(__file__).resolve().parents[1]
SHA = 'ed0b77d85bdf4014d7495800e8e4a70565b48ee6f8a2e5dca9cf8655dbf10eae'
SOURCE = 'https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/CUAD_v1.json'
SMALL = '\n'.join(f'Clause {i}: TOKEN{i}.' for i in range(10))


def corpus():
    path = ROOT / 'data/raw/cuad.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(SOURCE, timeout=120) as response:
            raw = response.read()
        if hashlib.sha256(raw).hexdigest() != SHA:
            raise ValueError('CUAD checksum mismatch')
        path.write_bytes(raw)
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA, 'Cached CUAD checksum mismatch'
    docs, qrels = [], {}
    for i, item in enumerate(json.loads(raw)['data'], 1):
        p = item['paragraphs'][0]
        did = f'cuad-{i:04d}'
        docs.append((did, p['context'], item['title']))
        for qa in p['qas']:
            category = qa['id'].rsplit('__', 1)[1]
            relevant = qrels.setdefault(category, set())
            if qa['answers']:
                relevant.add(did)
    return docs, qrels


def metrics(ids, relevant, k=10):
    ids = list(dict.fromkeys(ids))[:k]
    gains = [int(d in relevant) for d in ids]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return {'ndcg10': sum(g / math.log2(i + 2) for i, g in enumerate(gains)) / ideal if ideal else 0,
            'recall10': sum(gains) / len(relevant) if relevant else 0,
            'mrr10': next((1 / (i + 1) for i, g in enumerate(gains) if g), 0)}


def latency(values):
    ordered = sorted(values)
    return {'n': len(values), 'p50_ms': statistics.median(values),
            'p95_ms': ordered[math.ceil(.95 * len(ordered)) - 1]}


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, OSError, psycopg.Error):
            pass
        time.sleep(.1)
    raise TimeoutError('Readiness/assertion deadline exceeded')


class Stack:
    def __init__(self, output, dsn):
        self.output, self.external_dsn = output, dsn
        self.processes, self.logs = [], []
        self.temp = tempfile.TemporaryDirectory(prefix='sandstone-benchmark-')
        self.directory = Path(self.temp.name)
        self.schema = 'benchmark_' + uuid4().hex
        self.admin_dsn = None
        self.pg_ctl = None

    def start(self, args, env=None, name='process'):
        log = open(self.output / (name + '.log'), 'w')
        self.logs.append(log)
        p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(p)
        return p

    def __enter__(self):
        try:
            return self.setup()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def setup(self):
        self.admin_dsn = self.external_dsn
        if not self.admin_dsn:
            bindir = Path(os.environ.get('PGBIN', '/opt/homebrew/opt/postgresql@16/bin'))
            if not (bindir / 'initdb').exists():
                found = shutil.which('initdb')
                if not found:
                    raise RuntimeError('Set PGBIN or --database-url; PostgreSQL binaries required')
                bindir = Path(found).parent
            self.pg_ctl = str(bindir / 'pg_ctl')
            subprocess.run([str(bindir / 'initdb'), '-D', str(self.directory/'pg'), '-U', 'benchmark', '-A', 'trust', '--encoding=UTF8', '--no-locale'], check=True, stdout=subprocess.DEVNULL)
            pgport = port()
            subprocess.run([self.pg_ctl, '-D', str(self.directory/'pg'), '-l', str(self.output/'postgres.log'), '-o', f'-p {pgport} -h 127.0.0.1 -k {self.directory}', 'start'], check=True, stdout=subprocess.DEVNULL)
            self.admin_dsn = f'postgresql://benchmark@127.0.0.1:{pgport}/postgres'
        with psycopg.connect(self.admin_dsn, autocommit=True) as db:
            db.execute(f'CREATE SCHEMA {self.schema}')
        self.dsn = make_conninfo(self.admin_dsn, options=f'-c search_path={self.schema},public')
        self.store = PostgresStore(self.dsn, initialize=True)
        rp, ap, vp = port(), port(), port()
        env = {**os.environ, 'DATABASE_URL': self.dsn, 'REDIS_URL': f'redis://127.0.0.1:{rp}/0', 'API_URL': f'http://127.0.0.1:{ap}'}
        self.start(['redis-server', '--bind', '127.0.0.1', '--port', str(rp), '--dir', str(self.directory), '--appendonly', 'yes'], name='redis')
        self.start([sys.executable, '-m', 'uvicorn', 'src.main:build_app', '--factory', '--host', '127.0.0.1', '--port', str(ap)], env, 'api')
        self.env = env
        self.url, self.ui = env['API_URL'], f'http://127.0.0.1:{vp}'
        self.start(['npm', '--prefix', 'frontend', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', str(vp), '--strictPort'], env, 'vite')
        wait_for(lambda: httpx.get(self.url+'/documents').status_code == 200)
        wait_for(lambda: httpx.get(self.ui).status_code == 200)
        return self

    def workers(self):
        self.start([sys.executable, '-m', 'src.runtime'], self.env, 'worker')

    def __exit__(self, *exc):
        for p in reversed(self.processes):
            p.terminate()
        for p in self.processes:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
        for log in self.logs:
            log.close()
        if self.admin_dsn:
            with psycopg.connect(self.admin_dsn, autocommit=True) as db:
                db.execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')
        if self.pg_ctl:
            subprocess.run([self.pg_ctl, '-D', str(self.directory/'pg'), 'stop', '-m', 'fast'], stdout=subprocess.DEVNULL)
        self.temp.cleanup()


def search_suite(stack, docs, qrels, repetitions):
    baseline = sqlite3.connect(':memory:')
    baseline.execute('CREATE VIRTUAL TABLE fts USING fts5(document_id UNINDEXED,body)')
    began = time.perf_counter()
    baseline.executemany('INSERT INTO fts VALUES (?,?)', [(d,t) for d,t,_ in docs])
    build = time.perf_counter()-began
    rows, timings = [], {'http': [], 'fts5': []}
    texts = {d:t for d,t,_ in docs}
    with httpx.Client(base_url=stack.url, timeout=60) as client:
        for category, relevant in sorted(qrels.items()):
            query = ' '.join(re.findall(r'[^\W_]+', category)).lower()
            fts_query = ' AND '.join('"'+w+'"' for w in query.split())
            for repeat in range(repetitions + 1):
                started = time.perf_counter()
                response = client.post('/search', json={'text':query, 'mode':'terms', 'limit':100})
                elapsed = (time.perf_counter()-started)*1000
                response.raise_for_status()
                hits = response.json()['hits']
                ids = [h['document_id'] for h in hits]
                scores = [h['score'] for h in hits]
                assert all(score is not None and 0 <= score < 1 for score in scores)
                assert scores == sorted(scores, reverse=True)
                for hit in hits:
                    a = hit['target']; source = texts[hit['document_id']]
                    assert a['version']==1 and 0 <= a['start'] < a['end'] <= len(source)
                    assert source[a['start']:a['end']] in hit['context']
                began = time.perf_counter()
                b_ids = [r[0] for r in baseline.execute('SELECT document_id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts),document_id LIMIT 10', (fts_query,))]
                b_elapsed = (time.perf_counter()-began)*1000
                if repeat:
                    timings['http'].append(elapsed); timings['fts5'].append(b_elapsed)
            rows.append({'query':query, 'relevant_count':len(relevant), 'http_ids':list(dict.fromkeys(ids))[:10],
                         'fts5_ids':b_ids, 'http':metrics(ids,relevant), 'fts5':metrics(b_ids,relevant)})
        # Deterministic literal source-location cases, one from each of 30 contracts.
        for did, source, _ in docs[:30]:
            needle = source[len(source)//2:len(source)//2+40]
            r = client.post('/search', json={'text':needle,'mode':'literal','document_ids':[did]})
            r.raise_for_status(); hits = r.json()['hits']; assert hits
            assert all(source[h['target']['start']:h['target']['end']]==needle for h in hits)
        assert client.post('/search', json={'text':'ZZZ_NONEXISTENT_87daef'}).json()['hits']==[]
    baseline.close()
    return {'ranking':'PostgreSQL ts_rank_cd, maximum bounded-section score, normalization 32', 'queries':rows, 'latency':{k:latency(v) for k,v in timings.items()}, 'fts5_build_seconds':build,
            'mean':{engine:{metric:statistics.mean(r[engine][metric] for r in rows) for metric in ('ndcg10','recall10','mrr10')} for engine in timings},
            'literal_cases':30}


def edit_suite(stack, docs):
    store = stack.store
    store.create_document('trace-api', SMALL, 'API trace')
    checks = []
    with httpx.Client(base_url=stack.url, timeout=60) as c:
        reservations=[]
        for i in range(10):
            start=SMALL.index(f'TOKEN{i}')
            body={'targets':[{'version':1,'start':start,'end':start+6}]}
            r=c.post('/documents/trace-api/edit-reservations',json=body,headers={'X-Client-ID':f'api-{i}'})
            r.raise_for_status(); reservations.append(r.json()['reservations'][0]['reservation_id'])
            if i==0:
                duplicate=c.post('/documents/trace-api/edit-reservations',json=body,headers={'X-Client-ID':'api-0'})
                assert duplicate.json()['reservations'][0]['reservation_id']==reservations[0]
                assert c.post('/documents/trace-api/edit-reservations',json=body,headers={'X-Client-ID':'intruder'}).status_code==409
                checks.extend(['same-client selection returns existing reservation','overlapping client gets 409'])
        barrier=threading.Barrier(10)
        def replace(i):
            with httpx.Client(base_url=stack.url,timeout=60) as client:
                barrier.wait(timeout=30); started=time.perf_counter_ns()
                r=client.post('/documents/trace-api/replacements',json={'replacements':[{'reservation_id':reservations[i],'replacement':f'approved-{i}-long'}]})
                r.raise_for_status(); result=r.json()
                assert result['text']==store.get_document('trace-api',result['version']).text
                return {'client':i,'submitted_ns':started,'ms':(time.perf_counter_ns()-started)/1e6,'version':result['version'],'reservation_id':reservations[i]}
        with futures.ThreadPoolExecutor(10) as pool:
            trace=list(pool.map(replace,range(10)))
        final=c.get('/documents/trace-api').json()
        expected=SMALL
        for i in range(10): expected=expected.replace(f'TOKEN{i}',f'approved-{i}-long')
        assert final['version']==11 and final['text']==expected
        assert sorted(r['version'] for r in trace)==list(range(2,12))
        replay=c.post('/documents/trace-api/replacements',json={'replacements':[{'reservation_id':reservations[0],'replacement':'approved-0-long'}]}).json()
        assert replay['version']==trace[0]['version']
        assert c.post('/documents/trace-api/replacements',json={'replacements':[{'reservation_id':reservations[0],'replacement':'different'}]}).status_code==409
        assert c.post('/documents/trace-api/edit-reservations',json={'targets':[{'version':1,'start':0,'end':2}]},headers={'X-Client-ID':'stale'}).status_code==409
        checks.extend(['10 disjoint replacements exactly once, eagerly shifted anchors','replay returns original committed revision','changed payload rejected 409','stale reservation rejected 409'])
        large='\n'.join(t for _,t,_ in docs)
        store.create_document('stress-composite',large,'Synthetic CUAD concatenation')
        large_rows=[]
        for i in range(3):
            doc=c.get('/documents/stress-composite').json(); start=len(doc['text'])//2
            began=time.perf_counter()
            r=c.post('/documents/stress-composite/edit-reservations',headers={'X-Client-ID':'large'},json={'targets':[{'version':doc['version'],'start':start,'end':start+10}]});r.raise_for_status()
            result=c.post('/documents/stress-composite/replacements',json={'replacements':[{'reservation_id':r.json()['reservations'][0]['reservation_id'],'replacement':'BENCHMARK_REPLACEMENT'}]});result.raise_for_status()
            assert result.json()['text']==doc['text'][:start]+'BENCHMARK_REPLACEMENT'+doc['text'][start+10:]
            large_rows.append((time.perf_counter()-began)*1000)
        begun=time.perf_counter()
        def indexed():
            r=c.post('/search',json={'text':'BENCHMARK_REPLACEMENT','mode':'literal','document_ids':['stress-composite']})
            return r.status_code==200 and any(h['target']['version']==4 for h in r.json()['hits'])
        wait_for(indexed,120)
    return {'checks':checks,'trace':sorted(trace,key=lambda x:x['submitted_ns']),'initial':SMALL,'final':final,
            'latency':latency([r['ms'] for r in trace]),'large_bytes':len(large.encode()),'large_reserve_replace':latency(large_rows),
            'index_observed_after_response_ms':(time.perf_counter()-begun)*1000}


def ui_suite(stack, output):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    stack.store.create_document('trace-ui',SMALL,'UI concurrent trace')
    drivers=[]
    try:
        # Warm Selenium Manager once before simultaneous starts/downloads.
        options=webdriver.ChromeOptions(); options.add_argument('--headless=new'); options.add_argument('--window-size=1280,900')
        first=webdriver.Chrome(options=options); drivers.append(first)
        driver_path = first.service.path
        def launch(_):
            driver = webdriver.Chrome(service=Service(driver_path), options=options)
            drivers.append(driver)
            return driver
        with futures.ThreadPoolExecutor(3) as pool:
            list(pool.map(launch,range(9)))
        browser_version=first.capabilities['browserVersion']
        barrier=threading.Barrier(10)
        def user(i):
            d=drivers[i]; wait=WebDriverWait(d,25)
            d.get(stack.ui)
            picker=wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,'select[aria-label="Open document"]')))
            Select(picker).select_by_value('trace-ui')
            wait.until(lambda b: b.find_element(By.CSS_SELECTOR,'.revision').text=='v1' and not b.find_elements(By.CSS_SELECTOR,'.connection'))
            # DOM Range represents an actual user selection; mouseup invokes the UI handler.
            d.execute_script('''const token=arguments[0];const spans=[...document.querySelectorAll('.source [data-start]')];
            const el=spans.find(e=>e.textContent.includes(token));const pos=el.textContent.indexOf(token);
            const range=document.createRange();range.setStart(el.firstChild,pos);range.setEnd(el.firstChild,pos+token.length);
            const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
            el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));''',f'TOKEN{i}')
            editor=wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,'[data-suggestion][contenteditable="plaintext-only"]')))
            rid=editor.get_attribute('id').removeprefix('edit-')
            editor.send_keys(f'approved-{i}-long')
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,'button.submit')))
            barrier.wait(timeout=25)
            started=time.perf_counter_ns()
            d.find_element(By.CSS_SELECTOR,'button.submit').click()
            wait.until(lambda b: 'Edits applied.' in b.find_element(By.TAG_NAME,'body').text)
            wait.until(lambda b: b.find_element(By.CSS_SELECTOR,'.revision').text=='v11')
            source=d.execute_script("return [...document.querySelectorAll('.source [data-start]')].map(e=>e.textContent).join('')")
            expected=SMALL
            for j in range(10): expected=expected.replace(f'TOKEN{j}',f'approved-{j}-long')
            assert source==expected
            with stack.store.connection() as db:
                op=db.execute('SELECT p.result_version FROM replacements p JOIN replacement_items i ON i.operation_id=p.id WHERE i.reservation_id=%s',(rid,)).fetchone()
            return {'client':i,'submitted_ns':started,'version':op['result_version'],'reservation_id':rid,'ms':(time.perf_counter_ns()-started)/1e6}
        with futures.ThreadPoolExecutor(10) as pool:
            trace=list(pool.map(user,range(10)))
        assert sorted(r['version'] for r in trace)==list(range(2,12))
        first.save_screenshot(str(output/'ui-final.png'))
        return {'clients':10,'browser_version':browser_version,'trace':sorted(trace,key=lambda r:r['submitted_ns']),
                'latency':latency([r['ms'] for r in trace]),'initial':SMALL,'final':stack.store.get_document('trace-ui').model_dump(),
                'all_clients_converged':True}
    except BaseException:
        for i,d in enumerate(drivers):
            try: d.save_screenshot(str(output/f'ui-failure-{i}.png'))
            except Exception: pass
        raise
    finally:
        for d in drivers: d.quit()


def report(data, output):
    search=data['search']; edits=data['editing']; ui=data['ui']
    ranking = search.get('ranking', 'Document-ID order; no relevance scoring')
    lines=['# Contract search and redlining benchmark', '', f"Run: {data['started_utc']}. Environment: {data['environment']}.", '',
           f"Corpus: {data['documents']} CUAD contracts, {data['corpus_bytes']:,} UTF-8 bytes. SHA-256 `{SHA}`. No LLMs.", '',
           '## Method and interpretation', '',
           '[CUAD](https://www.atticusprojectai.org/cuad/) is a lawyer-annotated clause extraction dataset, not an official document-ranking benchmark. We derive 41 category queries from its labels; a document is relevant if that category has a nonempty annotated answer. Labels such as “Parties” are deliberately not rewritten to favor an engine. Missing annotations are treated as nonrelevant. These are diagnostic proxy judgments, not official CUAD or BEIR scores.', '',
           f'Both engines receive the same category words with AND semantics and the same unmodified 510 contracts. PostgreSQL ranking: {ranking}. Terms mode returns one anchor per matching document; we take the top 10 documents from up to 100 hits. FTS5 returns 10 documents ordered by BM25. The comparison measures these product-level retrieval choices.', '',
           f"Latency: one warm-up then {data['repetitions']} measured passes per query; sequential requests, nearest-rank p95. HTTP includes transport, source resolution and snippets; FTS5 is in-process, memory-resident SQL without HTTP/snippets. Their latency is NOT a controlled database speed comparison. Seed/index construction is excluded. PostgreSQL seed plus initial indexing: {data['seed_seconds']:.2f}s; FTS5 build: {search['fts5_build_seconds']:.2f}s.", '',
           '[BEIR](https://github.com/beir-cellar/beir) uses relevance metrics including nDCG and Recall; we use those metric definitions but do not compare scores across different datasets. [FTS5 BM25](https://www.sqlite.org/fts5.html#the_bm25_function) is the local classical retrieval baseline. This run is not evidence of production capacity or semantic search quality.', '',
           'Cache policy: this harness inherits QUERY_CACHE_* settings. With defaults, one warmup precedes admission on the first measured use and possible cache hits on later uses. Set QUERY_CACHE_MAX_QUERIES=0 for uncached search, or use benchmarks.query_cache to measure each arm separately.', '',
           '## Search results', '', '| Engine | nDCG@10 | Recall@10 | MRR@10 | p50 ms | p95 ms | Samples |','|---|---:|---:|---:|---:|---:|---:|']
    for engine,label in [('http','Current PostgreSQL API'),('fts5','SQLite FTS5 BM25 baseline')]:
        m=search['mean'][engine]; l=search['latency'][engine]
        lines.append(f"| {label} | {m['ndcg10']:.4f} | {m['recall10']:.4f} | {m['mrr10']:.4f} | {l['p50_ms']:.2f} | {l['p95_ms']:.2f} | {l['n']} |")
    lines += ['', 'Search assertions passed: 30 literal queries resolve to exact source slices, returned category-hit offsets/context are valid, and an absent query returns no hits. Full per-query rankings and scores are in `results.json`.', '',
              '## API editing behavior', '', *['- '+c for c in edits['checks']], '',
              f"10 concurrent replacements: p50 {edits['latency']['p50_ms']:.2f}ms, p95 {edits['latency']['p95_ms']:.2f}ms, measured from HTTP submission through full response and revision validation. Three sequential reserve+replace requests on a {edits['large_bytes']:,}-byte synthetic concatenation: p50 {edits['large_reserve_replace']['p50_ms']:.2f}ms, p95 {edits['large_reserve_replace']['p95_ms']:.2f}ms. Three samples are a smoke measurement, not a reliable tail estimate. Exact full text was verified each time.", '',
              f"Final large-document index observed current {edits['index_observed_after_response_ms']:.2f}ms after the response (HTTP observation upper bound, not exact indexing latency).", '',
              '## Browser concurrency', '', f"Selenium drove {ui['clients']} independent Chrome {ui['browser_version']} sessions. Each selected text, typed a replacement locally, then clicked Submit at a shared barrier. All ten observed the exact final text at v11 through their UI subscriptions. Click-to-all-updates-observed p50 {ui['latency']['p50_ms']:.2f}ms, p95 {ui['latency']['p95_ms']:.2f}ms; this includes Selenium scheduling and wait overhead, not pure server latency.", '',
              '![Final UI](ui-final.png)', '', '## Small-document traces', '', 'Submission order below is the test-client timestamp immediately before POST/click, not server arrival order. Processing order is the committed revision number: v2 is first. No FIFO is promised or required for disjoint reserved ranges. Length-changing edits verify eager anchor movement.']
    for label,suite in [('HTTP',edits),('Selenium',ui)]:
        lines += ['',f'### {label}', '', '| Submission rank | Client | Processing rank | Revision | Elapsed ms |','|---:|---:|---:|---:|---:|']
        for rank,row in enumerate(suite['trace'],1):
            lines.append(f"| {rank} | {row['client']} | {row['version']-1} | {row['version']} | {row['ms']:.2f} |")
    lines += ['', 'Initial v1 (both suites):', '', '```text',SMALL,'```', '', 'Final v11 (both suites):', '', '```text',edits['final']['text'],'```', '',
              '## Limits and next decisions', '', 'Ranking is evaluated against a BM25 baseline; neither keyword system understands clause meaning. Native section scoring is not BM25 and can give zero to matches whose terms never share a section. This run tests one host, 10 clients, one worker, warm reads and 510 contracts. Workers start after search measurement, with corpus creation events still in the outbox; edit timings therefore include startup/backlog effects and may include the server’s two-second outcome-reconciliation fallback. The browser suite runs later after that backlog has had time to drain. These are different operating conditions, not evidence that browser writes are faster than HTTP writes. It does not measure crash recovery, lease expiry timing, multi-host capacity, sustained load, or cold-cache behavior; existing unit/integration tests cover additional failure paths. A single large synthetic document validates large payload handling, not near-linear scaling across sizes.', '',
              'Reproduce with `python -m benchmarks.run`; see `benchmarks/README.md`. The harness uses an isolated PostgreSQL schema and temporary Redis, records failures as `failure.json`, and drops its own schema/services on exit. Corpus attribution: The Atticus Project; Hendrycks, Burns, Chen and Ball (2021), CC BY 4.0.']
    if data.get('previous_search'):
        previous = data['previous_search']
        lines += ['', '## Comparison with the earlier unranked run', '',
                  'Same corpus and query definitions; separate local runs, not a statistically controlled latency experiment.', '',
                  '| Measure | Document-ID ordering | Native relevance ordering |',
                  '|---|---:|---:|']
        for metric in ('ndcg10', 'recall10', 'mrr10'):
            lines.append(f"| {metric} | {previous['mean']['http'][metric]:.4f} | {search['mean']['http'][metric]:.4f} |")
        for metric in ('p50_ms', 'p95_ms'):
            lines.append(f"| {metric} | {previous['latency']['http'][metric]:.2f} | {search['latency']['http'][metric]:.2f} |")
    (output/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--database-url',default=None);parser.add_argument('--output',type=Path,default=ROOT/'reports/latest');parser.add_argument('--repetitions',type=int,default=3);parser.add_argument('--compare',type=Path,help='Previous results.json for the before/after table')
    args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=True)
    if any((args.output/name).exists() for name in ('report.md','results.json','failure.json')):
        parser.error('Use a fresh output directory; previous evidence is never overwritten')
    if args.repetitions<1: parser.error('repetitions must be positive')
    data={'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'environment':platform.platform()+'; Python '+platform.python_version(),'repetitions':args.repetitions}
    if args.compare:
        data['previous_search'] = json.loads(args.compare.read_text())['search']
    try:
        docs,qrels=corpus();data.update(documents=len(docs),corpus_bytes=sum(len(t.encode()) for _,t,_ in docs))
        with Stack(args.output,args.database_url) as stack:
            print('Seeding 510 contracts and indexing',flush=True);began=time.perf_counter()
            for did,text,title in docs:
                stack.store.create_document(did,text,title);stack.store.index_revision(did,1)
            data['seed_seconds']=time.perf_counter()-began
            print('Search suite',flush=True);data['search']=search_suite(stack,docs,qrels,args.repetitions)
            stack.workers()
            print('HTTP edit suite',flush=True);data['editing']=edit_suite(stack,docs)
            print('Selenium: ten clients',flush=True);data['ui']=ui_suite(stack,args.output)
        (args.output/'results.json').write_text(json.dumps(data,indent=2))
        report(data,args.output);print(args.output/'report.md',flush=True)
    except BaseException as exc:
        data['failure']=repr(exc);(args.output/'failure.json').write_text(json.dumps(data,indent=2));raise


if __name__=='__main__': main()

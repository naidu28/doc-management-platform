"""Prepare CUAD fixtures and an isolated SQLite FTS5 storage experiment.

Input: data/raw/cuad.json (official CUAD SQuAD-format release).
Not a production importer or retrieval-service implementation.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
SOURCE = 'https://huggingface.co/datasets/theatticusproject/cuad/resolve/main/CUAD_v1/CUAD_v1.json'


def main():
    raw = (DATA / 'raw/cuad.json').read_bytes()
    corpus = json.loads(raw)
    documents = []
    texts = DATA / 'contracts'
    texts.mkdir(exist_ok=True)
    annotations = []
    checked = 0
    for number, item in enumerate(corpus['data'], 1):
        if len(item['paragraphs']) != 1:
            raise ValueError('Expected a single complete context per contract')
        paragraph = item['paragraphs'][0]
        text = paragraph['context']
        doc_id = f'cuad-{number:04d}'
        encoded = text.encode('utf-8')
        (texts / f'{doc_id}.txt').write_bytes(encoded)
        for question in paragraph['qas']:
            for answer in question['answers']:
                start = answer['answer_start']
                assert text[start:start + len(answer['text'])] == answer['text']
                checked += 1
            annotations.append({'document_id': doc_id, **question})
        documents.append({'id': doc_id, 'title': item['title'], 'bytes': len(encoded),
                          'characters': len(text), 'sha256': hashlib.sha256(encoded).hexdigest(),
                          'path': f'contracts/{doc_id}.txt', 'kind': 'real_contract'})
    (DATA / 'annotations.json').write_text(json.dumps(annotations, ensure_ascii=False), encoding='utf-8')

    # Concatenation is intentionally not represented as a real single contract.
    pieces = []
    boundaries = []
    position = 0
    for doc in documents:
        marker = f'\n\n=== COMPOSITE COMPONENT {doc["id"]} ===\n\n'
        text = (DATA / doc['path']).read_bytes().decode('utf-8')
        pieces.extend([marker, text])
        position += len(marker)
        boundaries.append({'document_id': doc['id'], 'start': position, 'end': position + len(text)})
        position += len(text)
    combined = ''.join(pieces)
    fixtures = DATA / 'stress'
    fixtures.mkdir(exist_ok=True)
    (fixtures / 'cuad-composite.txt').write_bytes(combined.encode('utf-8'))
    (fixtures / 'components.json').write_text(json.dumps(boundaries, indent=2))
    manifest = {'source': SOURCE, 'source_sha256': hashlib.sha256(raw).hexdigest(),
                'license': 'CC-BY-4.0', 'attribution': 'The Atticus Project; Hendrycks, Burns, Chen, Ball (2021)',
                'documents': documents, 'validated_answer_spans': checked,
                'composite': {'path': 'stress/cuad-composite.txt', 'kind': 'synthetic_concatenation',
                              'bytes': len(combined.encode('utf-8')), 'characters': len(combined)}}
    (DATA / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')

    # Rebuild only the purpose-specific experiment database, never application data.
    db = DATA / 'retrieval-experiment.sqlite3'
    con = sqlite3.connect(db)
    con.executescript('''
        DROP TABLE IF EXISTS search_fts;
        DROP TABLE IF EXISTS search_documents;
        CREATE TABLE search_documents(id INTEGER PRIMARY KEY, document_id TEXT UNIQUE,
            version INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
        CREATE VIRTUAL TABLE search_fts USING fts5(title, body,
            content='search_documents', content_rowid='id', tokenize='unicode61');
    ''')
    started = time.perf_counter()
    for doc in documents:
        con.execute('INSERT INTO search_documents(document_id,version,title,body) VALUES (?,1,?,?)',
                    (doc['id'], doc['title'], (DATA / doc['path']).read_bytes().decode('utf-8')))
    con.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")
    con.commit()
    build_seconds = time.perf_counter() - started
    samples = []
    for query in ['"termination"', '"assignment"', '"governing law"', '"confidentiality"']:
        started = time.perf_counter()
        hits = con.execute('''SELECT d.document_id, bm25(search_fts),
            snippet(search_fts,1,'[',']','...',24)
            FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid
            WHERE search_fts MATCH ? ORDER BY bm25(search_fts) LIMIT 5''', (query,)).fetchall()
        samples.append({'query': query, 'seconds': time.perf_counter()-started,
                        'hits': [{'document_id': h[0], 'score': h[1], 'snippet': h[2]} for h in hits]})
    con.close()
    # Timing is illustrative, not a repeated/load benchmark.
    measurements = []
    for threshold in [10, 25]:
        if len(combined.encode('utf-8')) < threshold * 1024 * 1024:
            continue
        text = combined[:threshold * 1024 * 1024]
        start = len(text)//2
        began = time.perf_counter()
        updated = text[:start] + 'REPLACEMENT' + text[start+10:]
        elapsed = time.perf_counter()-began
        assert updated[start:start+11] == 'REPLACEMENT'
        measurements.append({'input_utf8_bytes': len(text.encode('utf-8')), 'input_characters':len(text),
                             'replace_seconds_single_run':elapsed})
    report = {'sqlite_version': sqlite3.sqlite_version, 'contract_count': len(documents),
              'corpus_bytes': sum(d['bytes'] for d in documents),
              'largest_contract': max(documents, key=lambda d:d['bytes']),
              'composite_bytes': manifest['composite']['bytes'], 'validated_answer_spans':checked,
              'index_build_seconds':build_seconds, 'database_bytes':db.stat().st_size,
              'search_samples':samples, 'replacement_samples':measurements,
              'caveat':'Single-run local smoke experiment; no concurrency or latency percentile claims.'}
    (DATA / 'experiment-results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='search_samples'}, indent=2))


if __name__ == '__main__':
    main()

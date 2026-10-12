"""Convert official Title 42 XML into a frozen plain-text benchmark fixture."""
import hashlib
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / 'data'


def main():
    with zipfile.ZipFile(DATA / 'raw/uscode42.zip') as archive:
        names = [n for n in archive.namelist() if n.endswith('.xml')]
        if len(names) != 1:
            raise ValueError(f'Expected one title XML, found {names}')
        xml = archive.read(names[0])
    (DATA / 'raw/uscode42.xml').write_bytes(xml)
    root = ET.fromstring(xml)
    # A benchmark extraction, not a typography-preserving legal publication.
    # Separate XML text nodes to avoid joining words across structural boundaries.
    text = '\n'.join(part.strip() for part in root.itertext() if part.strip())
    encoded = text.encode('utf-8')
    destination = DATA / 'legal'
    destination.mkdir(exist_ok=True)
    (destination / 'uscode-title42.txt').write_bytes(encoded)
    started = time.perf_counter()
    middle = len(text)//2
    updated = text[:middle] + 'BENCHMARK' + text[middle+9:]
    elapsed = time.perf_counter()-started
    assert updated[middle:middle+9] == 'BENCHMARK'
    db = DATA / 'large-document-experiment.sqlite3'
    con = sqlite3.connect(db)
    con.executescript('DROP TABLE IF EXISTS legal_fts; CREATE VIRTUAL TABLE legal_fts USING fts5(body);')
    started = time.perf_counter()
    con.execute('INSERT INTO legal_fts(body) VALUES (?)', (text,))
    con.commit()
    index_seconds = time.perf_counter()-started
    started = time.perf_counter()
    assert con.execute('SELECT count(*) FROM legal_fts WHERE legal_fts MATCH ?', ('"public health"',)).fetchone()[0] == 1
    query_seconds = time.perf_counter()-started
    con.close()
    report = {'source':(DATA/'raw/uscode42.source.txt').read_text(),
              'kind':'real_legal_compilation_not_single_contract', 'xml_bytes':len(xml),
              'xml_sha256':hashlib.sha256(xml).hexdigest(),
              'text_bytes':len(encoded), 'characters':len(text),
              'text_sha256':hashlib.sha256(encoded).hexdigest(),
              'extraction':'XML itertext, stripped nonempty text nodes joined with newline; frozen as canonical fixture',
              'replace_seconds_single_run':elapsed, 'index_seconds_single_run':index_seconds,
              'phrase_query_seconds_single_run':query_seconds,
              'caveat':'Local smoke experiment, not an end-to-end or concurrent benchmark.'}
    (destination/'manifest.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()

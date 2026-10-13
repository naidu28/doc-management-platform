"""Local CUAD demo using DATABASE_URL (not a disposable database); real PostgreSQL API and WebSockets, in-process write/index loop.

Run from the repository root: .venv/bin/python -m scripts.review_demo
Production continues to use src.main plus src.runtime workers.
"""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from src.main import build_app
from src.postgres import PostgresStore

store = PostgresStore(initialize=True)
# CUAD v1, The Atticus Project et al. (2021), CC BY 4.0; see data/README.md.
# Validate the complete local corpus before importing anything. Never overwrite
# an existing document: it may contain edits made in the live example.
data = Path(__file__).resolve().parents[1] / 'data'
manifest = json.loads((data / 'manifest.json').read_text())
fixtures = []
for entry in manifest['documents']:
    path = data / entry['path']
    if not path.exists():
        raise RuntimeError('Prepare CUAD first: run scripts/download_corpus.py and scripts/prepare_corpus.py')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise RuntimeError(f"Corpus checksum mismatch: {entry['id']}")
    fixtures.append((entry['id'], raw.decode('utf-8'), entry['title']))
with store.connection() as db:
    existing = {row['id'] for row in db.execute('SELECT id FROM documents')}
for id, text, title in fixtures:
    if id not in existing:
        store.create_document(id, text, title)


def unindexed_heads():
    with store.connection() as db:
        return [(r['id'], r['version']) for r in db.execute(
            'SELECT d.id,d.version FROM documents d LEFT JOIN search_documents s '
            'ON s.document_id=d.id WHERE s.version IS NULL OR s.version<d.version')]


for id, version in unindexed_heads():
    store.index_revision(id, version)
print(f"Demo ready: {len(fixtures)} CUAD contracts loaded and indexed; existing documents preserved.", flush=True)
app=build_app(database_url=store.dsn)
# Submission waits for committed completion, or returns 202 when still pending.

original_lifespan=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    async def process():
        while True:
            await asyncio.to_thread(store.expire)
            await asyncio.to_thread(store.process_next)
            heads = await asyncio.to_thread(unindexed_heads)
            for id,version in heads:await asyncio.to_thread(store.index_revision,id,version)
            await asyncio.sleep(.1)
    task=asyncio.create_task(process())
    async with original_lifespan(app):
        try:yield
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
app.router.lifespan_context=lifespan
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8010)

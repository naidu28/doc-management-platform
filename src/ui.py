"""Document discovery for the browser workbench."""
import asyncio


def list_documents(store):
    with store.connection() as db:
        return [dict(r) for r in db.execute('SELECT id AS document_id,title,version FROM documents ORDER BY title,id')]


def attach_ui(app, store):
    @app.get('/documents')
    async def documents():
        return await asyncio.to_thread(list_documents, store)

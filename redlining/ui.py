"""Small workbench extension: persistent draft text and document discovery."""
import asyncio
import json
from fastapi import Header
from pydantic import BaseModel, ConfigDict
from .services import ServiceError
from .storage import overlap


class DraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    replacement: str


def save_draft(store, rid, client_id, replacement):
    with store.connection(True) as db:
        row = store.row(db, rid)
        if row['client_id'] != client_id:
            raise ServiceError(409, 'OTHER_SESSION', 'This edit belongs to another session')
        if row['status'] not in ('active', 'expired'):
            raise ServiceError(409, 'DRAFT_CLOSED', 'This edit is already submitted or discarded')
        # A visual selection outlives its lease. Rebase only across edits that
        # provably did not touch it; overlapping committed edits invalidate it.
        anchor = dict(row)
        for revision in db.execute('SELECT version,patch FROM revisions WHERE document_id=? AND version>? ORDER BY version', (row['document_id'], row['version'])):
            patch = json.loads(revision['patch'])
            if overlap(anchor, patch):
                raise ServiceError(409, 'SELECTION_REPLACED', 'This selection was replaced. Select the passage again')
            if patch['end'] <= anchor['start']:
                delta = len(patch['replacement']) - (patch['end'] - patch['start'])
                anchor['start'] += delta
                anchor['end'] += delta
            anchor['version'] = revision['version']
        store.validate(db, row['document_id'], anchor)
        store.check_free(db, row['document_id'], anchor, rid)
        # Revalidate an expired selection before protecting it indefinitely.
        db.execute("UPDATE reservations SET status='active',expires=NULL,version=?,start=?,end=? WHERE id=?", (anchor['version'],anchor['start'],anchor['end'],rid))
        db.execute('INSERT INTO drafts(reservation_id,replacement) VALUES(?,?) '
                   'ON CONFLICT(reservation_id) DO UPDATE SET replacement=excluded.replacement', (rid, replacement))
        store.emit(db, row['document_id'], anchor['version'])
        return {'reservation_id': rid, 'replacement': replacement}


def list_documents(store):
    with store.connection() as db:
        return [dict(r) for r in db.execute('SELECT id AS document_id,title,version FROM documents ORDER BY title,id')]


def attach_ui(app, store):
    @app.get('/documents')
    async def documents():
        return await asyncio.to_thread(list_documents, store)

    @app.put('/edit-reservations/{rid}/draft')
    async def draft(rid: str, body: DraftInput, x_client_id: str = Header(min_length=1, max_length=200)):
        if hasattr(store, 'save_draft'):
            return await asyncio.to_thread(store.save_draft, rid, x_client_id, body.replacement)
        return await asyncio.to_thread(save_draft, store, rid, x_client_id, body.replacement)

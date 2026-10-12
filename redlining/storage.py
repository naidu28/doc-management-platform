"""SQLite source of truth. Each public mutation owns a short write transaction."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import (
    ChangeResult, OperationStatus, CompletedOperation, Document, DocumentAnchor, Error, FailedOperation,
    PendingOperation, Reservation, ReservationSnapshot, SearchHit, SearchResponse, DocumentUpdated,
)
from .services import ServiceError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, title TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(document_id TEXT NOT NULL REFERENCES documents(id),
 version INTEGER NOT NULL, text TEXT NOT NULL, patch TEXT, PRIMARY KEY(document_id,version));
CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 client_id TEXT NOT NULL, version INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
 status TEXT NOT NULL, created REAL NOT NULL, expires REAL, operation_id TEXT);
CREATE TABLE IF NOT EXISTS drafts(reservation_id TEXT PRIMARY KEY REFERENCES reservations(id), replacement TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reservation_document ON reservations(document_id,status);
CREATE TABLE IF NOT EXISTS operations(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT UNIQUE NOT NULL, reservation_id TEXT UNIQUE NOT NULL REFERENCES reservations(id),
 replacement TEXT NOT NULL, status TEXT NOT NULL, result_version INTEGER, failure TEXT);
CREATE TABLE IF NOT EXISTS outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT UNIQUE NOT NULL, document_id TEXT NOT NULL, kind TEXT NOT NULL,
 version INTEGER NOT NULL, payload TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS search_documents(id INTEGER PRIMARY KEY, document_id TEXT UNIQUE NOT NULL,
 version INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(title,body,
 content='search_documents',content_rowid='id',tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS search_insert AFTER INSERT ON search_documents BEGIN
 INSERT INTO search_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TRIGGER IF NOT EXISTS search_delete AFTER DELETE ON search_documents BEGIN
 INSERT INTO search_fts(search_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END;
CREATE TRIGGER IF NOT EXISTS search_update AFTER UPDATE ON search_documents BEGIN
 INSERT INTO search_fts(search_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body);
 INSERT INTO search_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
'''


def overlap(a, b):
    """Half-open spans; insertion points conflict at either span boundary."""
    if a['start'] == a['end']:
        return b['start'] <= a['start'] <= b['end']
    if b['start'] == b['end']:
        return a['start'] <= b['start'] <= a['end']
    return a['start'] < b['end'] and b['start'] < a['end']


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            if 'locked' in str(exc):
                raise ServiceError(503, 'STORAGE_BUSY', 'Storage is busy') from exc
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def emit(self, db, document_id, version, kind='reservations_changed', **payload):
        event_id = str(uuid4())
        db.execute('INSERT INTO outbox(event_id,document_id,kind,version,payload) VALUES(?,?,?,?,?)',
                   (event_id, document_id, kind, version, json.dumps(payload)))

    def head(self, db, document_id):
        row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise ServiceError(404, 'DOCUMENT_NOT_FOUND', 'Document not found')
        return row

    def row(self, db, reservation_id):
        row = db.execute('SELECT * FROM reservations WHERE id=?', (reservation_id,)).fetchone()
        if row is None:
            raise ServiceError(404, 'RESERVATION_NOT_FOUND', 'Reservation not found')
        return row

    def protected(self, db, document_id):
        return db.execute("SELECT * FROM reservations WHERE document_id=? AND (status='submitted' OR (status='active' AND (expires IS NULL OR expires>?)))",
                          (document_id, self.clock())).fetchall()

    def reservation_model(self, row):
        status = row['status']
        if status == 'active' and row['expires'] is not None and row['expires'] <= self.clock():
            status = 'expired'
        return Reservation(reservation_id=row['id'], document_id=row['document_id'], client_id=row['client_id'],
            target=DocumentAnchor(version=row['version'], start=row['start'], end=row['end']),
            status=status, created_at=utc(row['created']), expires_at=utc(row['expires']), operation_id=row['operation_id'])

    def validate(self, db, document_id, anchor):
        head = self.head(db, document_id)
        if anchor['version'] != head['version']:
            raise ServiceError(409, 'STALE_VERSION', 'Refresh the document before selecting text', head['version'])
        length = db.execute('SELECT text FROM revisions WHERE document_id=? AND version=?',
                            (document_id, head['version'])).fetchone()
        if not 0 <= anchor['start'] <= anchor['end'] <= len(length['text']):
            raise ServiceError(422, 'INVALID_RANGE', 'Selection is outside the document')
        return head

    def check_free(self, db, document_id, anchor, excluding=None):
        for other in self.protected(db, document_id):
            if other['id'] != excluding and overlap(anchor, other):
                raise ServiceError(409, 'RANGE_RESERVED', f"Range is reserved by client {other['client_id']}")

    def create_document(self, document_id, text, title=''):
        with self.connection(True) as db:
            if db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone():
                raise ServiceError(409, 'DOCUMENT_EXISTS', 'Document already exists')
            db.execute('INSERT INTO documents VALUES(?,?,1)', (document_id, title))
            db.execute('INSERT INTO revisions VALUES(?,1,?,NULL)', (document_id, text))
            self.emit(db, document_id, 1, 'document_created')
        return Document(document_id=document_id, version=1, text=text)

    def reserve(self, client_id, document_id, request):
        target = request.target.model_dump()
        with self.connection(True) as db:
            self.validate(db, document_id, target)
            for other in self.protected(db, document_id):
                if other['client_id'] == client_id and (other['start'], other['end']) == (target['start'], target['end']):
                    return self.reservation_model(other)
            self.check_free(db, document_id, target)
            rid, now = str(uuid4()), self.clock()
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                       (rid, document_id, client_id, target['version'], target['start'], target['end'], 'active', now, now+30))
            self.emit(db, document_id, target['version'])
            return self.reservation_model(self.row(db, rid))

    def reservation(self, rid):
        with self.connection() as db:
            return self.reservation_model(self.row(db, rid))

    def snapshot_in(self, db, document_id):
        head = self.head(db, document_id)
        return ReservationSnapshot(document_id=document_id, document_version=head['version'],
            reservations=[self.reservation_model(r) for r in self.protected(db, document_id)])

    def reservation_snapshot(self, document_id):
        with self.connection() as db:
            return self.snapshot_in(db, document_id)

    def renew(self, rid):
        with self.connection(True) as db:
            row = self.row(db, rid)
            if row['status'] not in ('active', 'expired', 'released'):
                raise ServiceError(409, 'RESERVATION_TERMINAL', 'Reservation cannot be renewed')
            if row['status'] == 'active' and row['expires'] is None:
                return self.reservation_model(row)
            self.validate(db, row['document_id'], row)
            self.check_free(db, row['document_id'], row, rid)
            db.execute("UPDATE reservations SET status='active',expires=? WHERE id=?", (self.clock()+30, rid))
            self.emit(db, row['document_id'], row['version'])
            return self.reservation_model(self.row(db, rid))

    def release(self, rid):
        with self.connection(True) as db:
            row = self.row(db, rid)
            if row['status'] == 'submitted':
                raise ServiceError(409, 'REPLACEMENT_PENDING', 'Queued replacement remains protected')
            if row['status'] in ('active', 'expired'):
                db.execute("UPDATE reservations SET status='released' WHERE id=?", (rid,))
                self.emit(db, row['document_id'], self.head(db, row['document_id'])['version'])

    def admit(self, command):
        with self.connection(True) as db:
            row = self.row(db, command.reservation_id)
            if row['operation_id']:
                op = db.execute('SELECT * FROM operations WHERE id=?', (row['operation_id'],)).fetchone()
                if op['replacement'] != command.replacement:
                    raise ServiceError(409, 'REPLACEMENT_MISMATCH', 'This reservation already has a different replacement')
                return row['operation_id']
            if row['status'] not in ('active', 'expired'):
                raise ServiceError(409, 'RESERVATION_RELEASED', 'Reacquire reservation before submission')
            self.validate(db, row['document_id'], row)
            self.check_free(db, row['document_id'], row, row['id'])
            oid = str(uuid4())
            db.execute("INSERT INTO operations(id,reservation_id,replacement,status) VALUES(?,?,?,'queued')",
                       (oid, row['id'], command.replacement))
            db.execute("UPDATE reservations SET status='submitted',operation_id=?,expires=NULL WHERE id=?", (oid, row['id']))
            self.emit(db, row['document_id'], row['version'])
            return oid

    def process_next(self):
        """FIFO selection and execution are atomic; crashes roll back to queued."""
        with self.connection(True) as db:
            op = db.execute("SELECT * FROM operations WHERE status='queued' ORDER BY sequence LIMIT 1").fetchone()
            if op is None:
                return False
            row = self.row(db, op['reservation_id'])
            head = self.head(db, row['document_id'])
            others = [r for r in self.protected(db, row['document_id']) if r['id'] != row['id']]
            if row['status'] != 'submitted' or row['version'] != head['version'] or any(overlap(row, r) for r in others):
                failure = Error(error='Queue protection invariant violated', code=500, reason='QUEUE_INVARIANT')
                db.execute("UPDATE operations SET status='failed',failure=? WHERE id=?", (failure.model_dump_json(), op['id']))
                db.execute("UPDATE reservations SET status='failed' WHERE id=?", (row['id'],))
                self.emit(db, row['document_id'], head['version'])
                return True
            text = self.document_in(db, row['document_id'], head['version']).text
            updated = text[:row['start']] + op['replacement'] + text[row['end']:]
            version = head['version'] + 1
            patch = {'start': row['start'], 'end': row['end'], 'replacement': op['replacement']}
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (row['document_id'], version, updated, json.dumps(patch)))
            db.execute('UPDATE documents SET version=? WHERE id=?', (version, row['document_id']))
            delta = len(op['replacement']) - (row['end']-row['start'])
            for other in others:
                shift = delta if other['start'] >= row['end'] else 0
                db.execute('UPDATE reservations SET version=?,start=?,end=? WHERE id=?',
                           (version, other['start']+shift, other['end']+shift, other['id']))
            db.execute("UPDATE reservations SET status='consumed' WHERE id=?", (row['id'],))
            db.execute("UPDATE operations SET status='completed',result_version=? WHERE id=?", (version, op['id']))
            self.emit(db, row['document_id'], version, 'document_updated', previous_version=head['version'])
            return True

    def document_in(self, db, document_id, version=None):
        head = self.head(db, document_id)
        version = head['version'] if version is None else version
        row = db.execute('SELECT text FROM revisions WHERE document_id=? AND version=?', (document_id, version)).fetchone()
        if row is None:
            raise ServiceError(404, 'REVISION_NOT_FOUND', 'Revision not found')
        return Document(document_id=document_id, version=version, text=row['text'])

    def get_document(self, document_id, version=None):
        with self.connection() as db:
            return self.document_in(db, document_id, version)

    def operation(self, oid):
        with self.connection() as db:
            op = db.execute('SELECT * FROM operations WHERE id=?', (oid,)).fetchone()
            if op is None:
                raise ServiceError(404, 'OPERATION_NOT_FOUND', 'Operation not found')
            common = dict(operation_id=oid, reservation_id=op['reservation_id'])
            if op['status'] == 'queued':
                return PendingOperation(**common, status='queued')
            if op['status'] == 'failed':
                return FailedOperation(**common, failure=Error.model_validate_json(op['failure']))
            r = self.row(db, op['reservation_id'])
            return CompletedOperation(**common, document=self.document_in(db, r['document_id'], op['result_version']))

    def replacement_status(self, operation_id):
        from .models import ReplacementAccepted, ReplacementCompleted, ReplacementFailed
        result = self.operation(operation_id)
        if isinstance(result, PendingOperation):
            return ReplacementAccepted(operation_id=operation_id,status_url=f'/replacement-operations/{operation_id}')
        if isinstance(result, FailedOperation):
            return ReplacementFailed(operation_id=operation_id,error=result.failure)
        return ReplacementCompleted(operation_id=operation_id,
            result=ChangeResult(**result.document.model_dump(),reservation_id=result.reservation_id))

    def replacement_result(self, operation_id):
        result = self.operation(operation_id)
        if isinstance(result, PendingOperation):
            return None
        if isinstance(result, FailedOperation):
            f = result.failure
            raise ServiceError(f.code,f.reason,f.error,f.current_version)
        return ChangeResult(**result.document.model_dump(),reservation_id=result.reservation_id)

    def expire(self):
        with self.connection(True) as db:
            docs = db.execute("SELECT DISTINCT document_id FROM reservations WHERE status='active' AND expires<=?", (self.clock(),)).fetchall()
            db.execute("UPDATE reservations SET status='expired' WHERE status='active' AND expires<=?", (self.clock(),))
            for d in docs:
                self.emit(db, d['document_id'], self.head(db, d['document_id'])['version'])

    def index_revision(self, document_id, version, force=False):
        with self.connection(True) as db:
            old = db.execute('SELECT version FROM search_documents WHERE document_id=?', (document_id,)).fetchone()
            if old and (old['version'] > version or (old['version'] == version and not force)):
                return False
            doc = self.document_in(db, document_id, version)
            title = self.head(db, document_id)['title']
            db.execute('''INSERT INTO search_documents(document_id,version,title,body) VALUES(?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET version=excluded.version,title=excluded.title,body=excluded.body''',
                       (document_id, version, title, doc.text))
            return True

    def search(self, request):
        if request.document_ids == []:
            return SearchResponse(hits=[])
        with self.connection() as db:
            clauses, params = '', []
            if request.document_ids is not None:
                clauses = ' AND d.document_id IN (' + ','.join('?' for _ in request.document_ids) + ')'
                params = request.document_ids
            hits = []
            if request.mode == 'literal':
                rows = db.execute('SELECT d.* FROM search_documents d WHERE 1=1'+clauses+' ORDER BY d.document_id', params)
                for row in rows:
                    pos = 0
                    while len(hits) < request.limit:
                        start = row['body'].find(request.text, pos)
                        if start < 0:
                            break
                        end = start + len(request.text)
                        hits.append(self.hit(row, start, end, None))
                        pos = end
                    if len(hits) >= request.limit:
                        break
            else:
                # Quote user input as FTS literals, never accept its query operators.
                quote = lambda value: '"' + value.replace('"', '""') + '"'
                query = quote(request.text) if request.mode == 'phrase' else ' AND '.join(quote(t) for t in request.text.split())
                left, right = f'\ue000{uuid4()}\ue001', f'\ue002{uuid4()}\ue003'
                rows = db.execute('''SELECT d.*,bm25(search_fts) AS score,highlight(search_fts,1,?,?) AS marked
                    FROM search_fts JOIN search_documents d ON d.id=search_fts.rowid
                    WHERE search_fts MATCH ?'''+clauses+' ORDER BY score,d.document_id LIMIT ?',
                    [left, right, 'body : ('+query+')', *params, request.limit])
                for row in rows:
                    # FTS highlight inserts markers into unmodified source, including repeated text.
                    marked = row['marked']
                    if left in row['body'] or right in row['body'] or marked.replace(left,'').replace(right,'') != row['body']:
                        raise ServiceError(500, 'ANCHOR_RESOLUTION', 'Cannot resolve indexed source offsets')
                    start = marked.find(left)
                    if start < 0:
                        continue
                    end_mark = marked.find(right, start+len(left))
                    end = start + end_mark - start - len(left)
                    hits.append(self.hit(row, start, end, row['score']))
            return SearchResponse(hits=hits)

    @staticmethod
    def hit(row, start, end, score):
        return SearchHit(document_id=row['document_id'], target=DocumentAnchor(version=row['version'], start=start, end=end),
                         context=row['body'][max(0,start-80):end+80], score=score)

    def pending_events(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE published=0 ORDER BY sequence LIMIT 100')]

    def mark_published(self, event_id):
        with self.connection(True) as db:
            db.execute('UPDATE outbox SET published=1 WHERE event_id=?', (event_id,))

    def live_state(self, document_id, after_version=None, retain_history=False):
        """One consistent read snapshot; full text on connect, patches afterward."""
        with self.connection() as db:
            snapshot = self.snapshot_in(db, document_id)
            current = snapshot.document_version
            if after_version is None or after_version > current or (current-after_version > 100 and not retain_history):
                content = {'document': self.document_in(db, document_id).model_dump()}
            else:
                rows = db.execute('''SELECT r.version,r.patch,o.event_id FROM revisions r JOIN outbox o
                    ON o.document_id=r.document_id AND o.version=r.version AND o.kind='document_updated'
                    WHERE r.document_id=? AND r.version>? ORDER BY r.version''',
                                  (document_id, after_version)).fetchall()
                content = {'updates': [DocumentUpdated(event_id=r['event_id'], document_id=document_id,
                            previous_version=r['version']-1, version=r['version'],
                            patch=json.loads(r['patch'])).model_dump() for r in rows]}
            drafts = [dict(r) for r in db.execute("SELECT d.* FROM drafts d JOIN reservations r ON r.id=d.reservation_id WHERE r.document_id=? AND r.status IN ('active','submitted')", (document_id,))]
            # Retained compact outcomes also recover failures (which do not advance
            # document version) and completion missed while disconnected.
            operations = []
            for row in db.execute(
                "SELECT o.* FROM operations o JOIN reservations r ON r.id=o.reservation_id "
                "WHERE r.document_id=? ORDER BY o.sequence", (document_id,)
            ):
                operations.append(OperationStatus(
                    operation_id=row['id'], reservation_id=row['reservation_id'],
                    status=row['status'], result_version=row['result_version'],
                    failure=json.loads(row['failure']) if row['failure'] else None,
                ).model_dump(mode='json'))
            return {'type': 'document_state', 'reservations': snapshot.model_dump(mode='json'), 'drafts': drafts, 'operations': operations, **content}

"""PostgreSQL reservations and durable, conflict-free replacement execution.

Every range mutation locks its document. Admission validates and freezes protection
atomically with queue insertion. Workers may execute replacements in any order.
"""
from bisect import bisect_right
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .models import ReplacementAccepted, ReplacementCompleted, ReplacementFailed, Error, CreateEditReservationsRequest, EditReservationsResult, ReplacementResult, ChangeResult, Document, DocumentAnchor, Reservation, ReservationSnapshot, SearchHit, SearchResponse
from .services import ReplaceCommand, ServiceError
from .storage import overlap
from .ranking import ranking_sections

DEFAULT_DSN = 'postgresql://localhost/sandstone'
SCHEMA = '''
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS documents(id text PRIMARY KEY, title text NOT NULL, version bigint NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(document_id text REFERENCES documents(id), version bigint, text text NOT NULL, patch jsonb, PRIMARY KEY(document_id,version));
CREATE TABLE IF NOT EXISTS reservations(id text PRIMARY KEY, document_id text REFERENCES documents(id), client_id text NOT NULL,
 version bigint NOT NULL, start_pos bigint NOT NULL, end_pos bigint NOT NULL, status text NOT NULL,
 created double precision NOT NULL, expires double precision, operation_id text);
ALTER TABLE reservations ALTER COLUMN expires DROP NOT NULL;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS operation_id text;
CREATE INDEX IF NOT EXISTS reservation_document ON reservations(document_id,status);
CREATE TABLE IF NOT EXISTS replacements(id text PRIMARY KEY, document_id text NOT NULL REFERENCES documents(id),
 reservation_id text UNIQUE NOT NULL REFERENCES reservations(id), replacement text NOT NULL,
 status text NOT NULL DEFAULT 'queued', result_version bigint, failure jsonb);
CREATE TABLE IF NOT EXISTS one_shot_requests(document_id text NOT NULL REFERENCES documents(id),
 client_id text NOT NULL, request_key text NOT NULL, payload jsonb NOT NULL,
 operation_id text NOT NULL REFERENCES replacements(id), PRIMARY KEY(document_id,client_id,request_key));
CREATE TABLE IF NOT EXISTS replacement_items(operation_id text NOT NULL REFERENCES replacements(id),
 reservation_id text PRIMARY KEY REFERENCES reservations(id), replacement text NOT NULL);
CREATE INDEX IF NOT EXISTS replacement_items_operation ON replacement_items(operation_id);
CREATE INDEX IF NOT EXISTS replacements_pending ON replacements(document_id) WHERE status='queued';
CREATE TABLE IF NOT EXISTS browser_connections(id text PRIMARY KEY, document_id text NOT NULL REFERENCES documents(id),
 client_id text NOT NULL, expires double precision NOT NULL);
CREATE INDEX IF NOT EXISTS browser_connections_expiry ON browser_connections(expires);
CREATE INDEX IF NOT EXISTS browser_connections_owner ON browser_connections(document_id,client_id);
CREATE TABLE IF NOT EXISTS drafts(reservation_id text PRIMARY KEY REFERENCES reservations(id), replacement text NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(sequence bigserial PRIMARY KEY, event_id text UNIQUE NOT NULL, document_id text NOT NULL,
 kind text NOT NULL, version bigint NOT NULL, payload text NOT NULL, published integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS search_documents(document_id text PRIMARY KEY REFERENCES documents(id), version bigint NOT NULL, title text NOT NULL, body text NOT NULL);
ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS ranking_sections tsvector[];
CREATE INDEX IF NOT EXISTS search_body_trigram ON search_documents USING gin(body gin_trgm_ops);
'''


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc)


class PostgresStore:
    def __init__(self, dsn=None, *, initialize=False):
        self.dsn = dsn or os.environ.get('DATABASE_URL', DEFAULT_DSN)
        if initialize:
            with self.connection(True) as db:
                # Serialize schema setup across starting processes.
                db.execute("SELECT pg_advisory_xact_lock(72581901)")
                self.migrate_legacy_queue(db)
                db.execute(SCHEMA)
                self.copy_legacy_outcomes(db)
                db.execute("""INSERT INTO replacement_items SELECT id,reservation_id,replacement
                    FROM replacements ON CONFLICT DO NOTHING""")
                self.backfill_ranking(db)

    @staticmethod
    def backfill_ranking(db):
        # One-time upgrade, serialized by the existing schema initialization lock.
        # Named cursor avoids materializing the complete corpus in Python.
        with db.cursor(name='ranking_backfill') as cursor:
            cursor.execute('SELECT document_id,body FROM search_documents WHERE ranking_sections IS NULL')
            for row in cursor:
                db.execute("""UPDATE search_documents SET ranking_sections=
                    ARRAY(SELECT to_tsvector('simple', section) FROM unnest(%s::text[]) AS section)
                    WHERE document_id=%s""", (list(ranking_sections(row['body'])),row['document_id']))

    @contextmanager
    def connection(self, write=False):
        with psycopg.connect(self.dsn, row_factory=dict_row) as db:
            if not write:
                db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            yield db

    @staticmethod
    def now(db):
        return float(db.execute('SELECT extract(epoch FROM clock_timestamp()) AS now').fetchone()['now'])

    @staticmethod
    def head(db, document_id):
        row = db.execute('SELECT * FROM documents WHERE id=%s', (document_id,)).fetchone()
        if not row:
            raise ServiceError(404, 'DOCUMENT_NOT_FOUND', 'Document not found')
        return row

    @staticmethod
    def row(db, rid):
        row = db.execute('SELECT *,start_pos AS start,end_pos AS end FROM reservations WHERE id=%s', (rid,)).fetchone()
        if not row:
            raise ServiceError(404, 'RESERVATION_NOT_FOUND', 'Reservation not found')
        return row

    @staticmethod
    def emit(db, document_id, version, kind='reservations_changed', **payload):
        db.execute('INSERT INTO outbox(event_id,document_id,kind,version,payload) VALUES(%s,%s,%s,%s,%s)',
                   (str(uuid4()), document_id, kind, version, json.dumps(payload)))

    def create_document(self, document_id, text, title=''):
        with self.connection(True) as db:
            if db.execute('SELECT id FROM documents WHERE id=%s', (document_id,)).fetchone():
                raise ServiceError(409, 'DOCUMENT_EXISTS', 'Document already exists')
            db.execute('INSERT INTO documents VALUES(%s,%s,1)', (document_id,title))
            db.execute('INSERT INTO revisions VALUES(%s,1,%s,NULL)', (document_id,text))
            self.emit(db, document_id, 1, 'document_created')
        return Document(document_id=document_id,version=1,text=text)

    @staticmethod
    def migrate_legacy_queue(db):
        # Upgrade only drained queues. Never discard already acknowledged work.
        old = db.execute("SELECT to_regclass(current_schema() || '.commands') AS name").fetchone()['name']
        if old:
            if db.execute("SELECT 1 FROM commands WHERE status='queued' LIMIT 1").fetchone():
                raise RuntimeError('Drain the old shared command queue with the previous worker before upgrading')
            db.execute('ALTER TABLE commands RENAME TO legacy_commands')
            db.execute('ALTER TABLE document_sequences RENAME TO legacy_document_sequences')

    @staticmethod
    def copy_legacy_outcomes(db):
        old = db.execute("SELECT to_regclass(current_schema() || '.legacy_commands') AS name").fetchone()['name']
        if old:
            db.execute("""INSERT INTO replacements(id,document_id,reservation_id,replacement,status,result_version,failure)
                SELECT id,document_id,reservation_id,payload->>'replacement',status,(result->>'version')::bigint,failure
                FROM legacy_commands WHERE kind='replace' ON CONFLICT DO NOTHING""")
            db.execute("""UPDATE reservations r SET operation_id=p.id FROM replacements p
                WHERE p.reservation_id=r.id AND r.operation_id IS NULL""")

    def lock_document(self, db, document_id):
        row = db.execute('SELECT * FROM documents WHERE id=%s FOR NO KEY UPDATE', (document_id,)).fetchone()
        if not row:
            raise ServiceError(404, 'DOCUMENT_NOT_FOUND', 'Document not found')
        return row

    def lock_reservation(self, db, rid):
        document_id = self.row(db,rid)['document_id']
        self.lock_document(db,document_id)
        # Another writer may have changed the anchor while we waited for the lock.
        return self.row(db,rid)

    def reserve(self, client_id, document_id, request):
        batch = CreateEditReservationsRequest(targets=[request.target])
        return self.reserve_batch(client_id, document_id, batch).reservations[0]

    def reserve_batch(self, client_id, document_id, request):
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            return self.reserve_targets(db,client_id,document_id,request)

    def reserve_targets(self, db, client_id, document_id, request):
        now = self.now(db)
        results = []
        changed = False
        context = self.validation_context(db,document_id)
        for index, anchor in enumerate(request.targets):
            target = anchor.model_dump()
            try:
                self.validate(db,document_id,target,context)
                existing = next((other for other in self.protected(db,document_id,now)
                    if other['client_id']==client_id and
                    (other['start'],other['end'])==(target['start'],target['end'])), None)
                if existing:
                    results.append(self.reservation_model(existing,now))
                    continue
                self.check_free(db,document_id,target,now)
                rid = str(uuid4())
                changed = True
                db.execute("""INSERT INTO reservations(id,document_id,client_id,version,start_pos,end_pos,status,created,expires)
                    VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s)""",
                    (rid,document_id,client_id,target['version'],target['start'],target['end'],now,now+30))
                results.append(self.reservation_model(self.row(db,rid),now))
            except ServiceError as exc:
                exc.item_index = index
                raise
        if changed:
            self.emit(db,document_id,request.targets[0].version)
        return EditReservationsResult(reservations=results)

    def eligible(self, db, row, now, context=None):
        if row['status'] not in ('active','expired'):
            raise ServiceError(409,'RESERVATION_TERMINAL','Select the passage again')
        self.validate(db,row['document_id'],row,context)
        self.check_free(db,row['document_id'],row,now,row['id'])

    def renew(self, rid):
        with self.connection(True) as db:
            row = self.lock_reservation(db,rid)
            now = self.now(db)
            self.eligible(db,row,now)
            db.execute("UPDATE reservations SET status='active',expires=%s WHERE id=%s",(now+30,rid))
            self.emit(db,row['document_id'],row['version'])
            return self.reservation_model(self.row(db,rid),now)

    def release(self, rid):
        with self.connection(True) as db:
            row = self.lock_reservation(db,rid)
            if row['status']=='submitted':
                raise ServiceError(409,'REPLACEMENT_PENDING','Queued replacement remains protected')
            if row['status'] in ('active','expired'):
                db.execute("UPDATE reservations SET status='released' WHERE id=%s",(rid,))
                self.emit(db,row['document_id'],self.head(db,row['document_id'])['version'])

    def admit(self, command):
        with self.connection(True) as db:
            row = self.lock_reservation(db,command.reservation_id)
            return self.admit_items(db,row['document_id'],[command])

    def admit_one_shot(self, client_id, document_id, request_key, request):
        # The caller can replay after losing the response without knowing any
        # server-minted reservation ID. Identity is scoped to client + document.
        payload = sorted([item.model_dump() for item in request.replacements],
                         key=lambda item:(item['target']['start'],item['target']['end']))
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            previous = db.execute('SELECT * FROM one_shot_requests WHERE document_id=%s AND client_id=%s AND request_key=%s',
                                  (document_id,client_id,request_key)).fetchone()
            if previous:
                if previous['payload']!=payload:
                    raise ServiceError(409,'IDEMPOTENCY_MISMATCH','This Idempotency-Key already identifies a different request')
                return previous['operation_id']
            reservations = self.reserve_targets(db,client_id,document_id,
                CreateEditReservationsRequest(targets=[item.target for item in request.replacements])).reservations
            commands = [ReplaceCommand(reservation.reservation_id,item.replacement)
                        for reservation,item in zip(reservations,request.replacements)]
            oid = self.admit_items(db,document_id,commands)
            db.execute('INSERT INTO one_shot_requests VALUES(%s,%s,%s,%s::jsonb,%s)',
                       (document_id,client_id,request_key,json.dumps(payload),oid))
            return oid

    def admit_batch(self, document_id, commands):
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            return self.admit_items(db,document_id,commands)

    def admit_items(self, db, document_id, commands):
        requested = {c.reservation_id:c.replacement for c in commands}
        if not commands or len(commands)>100 or len(requested)!=len(commands):
            raise ServiceError(422,'INVALID_BATCH','Use 1–100 distinct reservations')
        rows = []
        for index, command in enumerate(commands):
            try:
                row = self.row(db,command.reservation_id)
                if row['document_id']!=document_id:
                    raise ServiceError(409,'DOCUMENT_MISMATCH','All reservations must belong to the requested document')
                rows.append(row)
            except ServiceError as exc:
                exc.item_index = index
                raise
        previous = db.execute('SELECT * FROM replacement_items WHERE reservation_id=ANY(%s)',(list(requested),)).fetchall()
        if previous:
            ids = {item['operation_id'] for item in previous}
            if len(ids)==1:
                oid = next(iter(ids))
                operation = db.execute('SELECT * FROM replacements WHERE id=%s',(oid,)).fetchone()
                bound = {r['reservation_id']:r['replacement'] for r in self.operation_items(db,operation)}
                if bound==requested:
                    return oid
            raise ServiceError(409,'REPLACEMENT_MISMATCH','Reservations are already bound to a different replacement or batch')
        now = self.now(db)
        context = self.validation_context(db,document_id)
        for index, row in enumerate(rows):
            try:
                self.eligible(db,row,now,context)
                if any(overlap(row,other) for other in rows[:index]):
                    raise ServiceError(409,'RANGE_RESERVED','Batch reservations overlap')
            except ServiceError as exc:
                exc.item_index = index
                raise
        oid = str(uuid4())
        # Keep the original leading-item columns for durable pre-batch operations.
        # The child table is authoritative for membership of all new operations.
        first = commands[0]
        db.execute('INSERT INTO replacements(id,document_id,reservation_id,replacement) VALUES(%s,%s,%s,%s)',
                   (oid,document_id,first.reservation_id,first.replacement))
        for command in commands:
            db.execute('INSERT INTO replacement_items VALUES(%s,%s,%s)',(oid,command.reservation_id,command.replacement))
            db.execute("UPDATE reservations SET status='submitted',expires=NULL,operation_id=%s WHERE id=%s",(oid,command.reservation_id))
        self.emit(db,document_id,rows[0]['version'],'replacement_queued',operation_id=oid)
        return oid

    @staticmethod
    def operation_items(db, operation):
        return db.execute('SELECT * FROM replacement_items WHERE operation_id=%s ORDER BY reservation_id',
                          (operation['id'],)).fetchall()

    def save_draft(self, rid, client_id, replacement):
        with self.connection(True) as db:
            row = self.lock_reservation(db,rid)
            now = self.now(db)
            if row['client_id']!=client_id:
                raise ServiceError(409,'OTHER_SESSION','This edit belongs to another session')
            self.eligible(db,row,now)
            if row['expires']<=now:
                raise ServiceError(409,'LEASE_EXPIRED','Renew the reservation before typing')
            db.execute('INSERT INTO drafts VALUES(%s,%s) ON CONFLICT(reservation_id) DO UPDATE SET replacement=excluded.replacement',(rid,replacement))
            self.emit(db,row['document_id'],row['version'])
            return {'reservation_id':rid,'replacement':replacement}

    def protected(self, db, document_id, now):
        return db.execute("SELECT *,start_pos AS start,end_pos AS end FROM reservations WHERE document_id=%s AND (status='submitted' OR (status='active' AND expires>%s))", (document_id,now)).fetchall()

    def validation_context(self, db, document_id):
        version = self.head(db,document_id)['version']
        length = db.execute('SELECT char_length(text) AS length FROM revisions WHERE document_id=%s AND version=%s',
                            (document_id,version)).fetchone()['length']
        return version,length

    def validate(self, db, document_id, target, context=None):
        version,length = context if context is not None else self.validation_context(db,document_id)
        if target['version'] != version:
            raise ServiceError(409,'STALE_VERSION','Refresh the document before selecting text',version)
        if not 0 <= target['start'] <= target['end'] <= length:
            raise ServiceError(422,'INVALID_RANGE','Selection is outside the document')

    def check_free(self, db, document_id, target, now, excluding=None):
        for other in self.protected(db, document_id, now):
            if other['id'] != excluding and overlap(target,other):
                raise ServiceError(409,'RANGE_RESERVED',f"Range is reserved by client {other['client_id']}")

    @staticmethod
    def reservation_model(row, now):
        status = 'expired' if row['status']=='active' and row['expires'] is not None and row['expires'] <= now else row['status']
        return Reservation(reservation_id=row['id'],document_id=row['document_id'],client_id=row['client_id'],
                           target=DocumentAnchor(version=row['version'],start=row['start'],end=row['end']),status=status,
                           created_at=stamp(row['created']),expires_at=stamp(row['expires']) if row['expires'] is not None else None,
                           operation_id=row['operation_id'])

    def reservation(self, rid):
        with self.connection() as db:
            return self.reservation_model(self.row(db,rid),self.now(db))

    def snapshot_in(self, db, document_id):
        now = self.now(db)
        return ReservationSnapshot(document_id=document_id,document_version=self.head(db,document_id)['version'],
                                   reservations=[self.reservation_model(r,now) for r in self.protected(db,document_id,now)])

    def reservation_snapshot(self, document_id):
        with self.connection() as db:
            return self.snapshot_in(db,document_id)

    def process_next(self):
        with self.connection(True) as db:
            # Serial execution per document, with no request-arrival ordering promise.
            document = db.execute("""SELECT d.id FROM documents d WHERE EXISTS
                (SELECT 1 FROM replacements p WHERE p.document_id=d.id AND p.status='queued')
                LIMIT 1 FOR NO KEY UPDATE OF d SKIP LOCKED""").fetchone()
            if not document:
                return False
            replacement = db.execute("SELECT * FROM replacements WHERE document_id=%s AND status='queued' LIMIT 1",(document['id'],)).fetchone()
            try:
                with db.transaction():
                    version = self.execute(db,replacement,self.now(db))
            except ServiceError as exc:
                failure = dict(error=exc.message,code=exc.status,reason=exc.reason,current_version=exc.current_version)
                db.execute("UPDATE replacements SET status='failed',failure=%s::jsonb WHERE id=%s",(json.dumps(failure),replacement['id']))
                db.execute("UPDATE reservations SET status='failed' WHERE operation_id=%s",(replacement['id'],))
            else:
                db.execute("UPDATE replacements SET status='completed',result_version=%s WHERE id=%s",(version,replacement['id']))
            self.emit(db,document['id'],self.head(db,document['id'])['version'],'replacement_finished',operation_id=replacement['id'])
            return True

    def execute(self, db, operation, now):
        document_id = operation['document_id']
        head = self.head(db,document_id)
        items = self.operation_items(db,operation)
        edits = sorted([{**self.row(db,item['reservation_id']), 'replacement':item['replacement']}
                        for item in items], key=lambda row:(row['start'],row['end']))
        ids = {row['id'] for row in edits}
        others = [row for row in self.protected(db,document_id,now) if row['id'] not in ids]
        if not edits:
            raise ServiceError(500,'QUEUE_INVARIANT','Replacement has no items')
        text = self.document_in(db,document_id).text
        for i,row in enumerate(edits):
            if (row['document_id']!=document_id or row['status']!='submitted'
                    or row['operation_id']!=operation['id'] or row['version']!=head['version']
                    or not 0 <= row['start'] <= row['end'] <= len(text)
                    or any(overlap(row,other) for other in others)
                    or (i and overlap(edits[i-1],row))):
                raise ServiceError(500,'QUEUE_INVARIANT','Replacement protection invariant violated')
        pieces, patches, ends, shifts = [], [], [], [0]
        position = 0
        for row in edits:
            pieces.extend([text[position:row['start']],row['replacement']])
            position = row['end']
            patches.append(dict(start=row['start'],end=row['end'],replacement=row['replacement']))
            ends.append(row['end'])
            shifts.append(shifts[-1]+len(row['replacement'])-(row['end']-row['start']))
        pieces.append(text[position:])
        updated = ''.join(pieces)
        version = head['version']+1
        patch = {'patches':patches}
        db.execute('INSERT INTO revisions VALUES(%s,%s,%s,%s::jsonb)',(document_id,version,updated,json.dumps(patch)))
        db.execute('UPDATE documents SET version=%s WHERE id=%s',(version,document_id))
        for other in others:
            shift = shifts[bisect_right(ends,other['start'])]
            db.execute('UPDATE reservations SET version=%s,start_pos=start_pos+%s,end_pos=end_pos+%s WHERE id=%s',
                       (version,shift,shift,other['id']))
        db.execute("UPDATE reservations SET status='consumed' WHERE operation_id=%s",(operation['id'],))
        self.emit(db,document_id,version,'document_updated',previous_version=head['version'])
        return version

    def open_browser_connection(self, document_id, client_id):
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            now = self.now(db)
            self.cleanup_browser_connections(db,document_id,now)
            connection_id = str(uuid4())
            db.execute('INSERT INTO browser_connections VALUES(%s,%s,%s,%s)',
                       (connection_id,document_id,client_id,now+20))
            return connection_id

    def heartbeat_browser_connection(self, document_id, connection_id):
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            now = self.now(db)
            self.cleanup_browser_connections(db,document_id,now)
            return bool(db.execute('UPDATE browser_connections SET expires=%s WHERE id=%s AND document_id=%s RETURNING id',
                                   (now+20,connection_id,document_id)).fetchone())

    def close_browser_connection(self, document_id, connection_id, leaving=False):
        with self.connection(True) as db:
            self.lock_document(db,document_id)
            now = self.now(db)
            # Unexpected disconnects get five seconds to reconnect on any API process.
            db.execute('UPDATE browser_connections SET expires=LEAST(expires,%s) WHERE id=%s AND document_id=%s',
                       (now if leaving else now+5,connection_id,document_id))
            self.cleanup_browser_connections(db,document_id,now)

    def cleanup_browser_connections(self, db, document_id, now):
        expired = db.execute('DELETE FROM browser_connections WHERE document_id=%s AND expires<=%s RETURNING client_id',
                             (document_id,now)).fetchall()
        changed = False
        for client_id in {row['client_id'] for row in expired}:
            if db.execute('SELECT 1 FROM browser_connections WHERE document_id=%s AND client_id=%s',
                          (document_id,client_id)).fetchone():
                continue
            released = db.execute("""UPDATE reservations SET status='released'
                WHERE document_id=%s AND client_id=%s AND status IN ('active','expired') RETURNING id""",
                (document_id,client_id)).fetchall()
            changed = changed or bool(released)
        if changed:
            self.emit(db,document_id,self.head(db,document_id)['version'])

    def expire(self):
        # Durable presence cleanup survives API-process crashes. Ordinary API-only
        # leases still expire by time predicate, without requiring a subscription.
        with self.connection(True) as db:
            rows = db.execute('SELECT DISTINCT document_id FROM browser_connections WHERE expires<=%s',(self.now(db),)).fetchall()
            # Consistent lock order avoids deadlocks between cleanup workers.
            for document_id in sorted(row['document_id'] for row in rows):
                self.lock_document(db,document_id)
                self.cleanup_browser_connections(db,document_id,self.now(db))

    def document_in(self, db, document_id, version=None):
        head = self.head(db,document_id)
        version = version or head['version']
        row = db.execute('SELECT text FROM revisions WHERE document_id=%s AND version=%s',(document_id,version)).fetchone()
        if not row:
            raise ServiceError(404,'REVISION_NOT_FOUND','Revision not found')
        return Document(document_id=document_id,version=version,text=row['text'])

    def get_document(self, document_id, version=None):
        with self.connection() as db:
            return self.document_in(db,document_id,version)

    def operation(self, operation_id):
        with self.connection() as db:
            return db.execute('SELECT * FROM replacements WHERE id=%s',(operation_id,)).fetchone()

    def replacement_status(self, operation_id):
        with self.connection() as db:
            operation = db.execute('SELECT * FROM replacements WHERE id=%s',(operation_id,)).fetchone()
            if not operation:
                raise ServiceError(404,'OPERATION_NOT_FOUND','Replacement not found')
            if operation['status']=='queued':
                return ReplacementAccepted(operation_id=operation_id,status_url=f'/replacement-operations/{operation_id}')
            if operation['status']=='failed':
                return ReplacementFailed(operation_id=operation_id,error=Error(**operation['failure']))
            document = self.document_in(db,operation['document_id'],operation['result_version'])
            result = ReplacementResult(**document.model_dump(),
                reservation_ids=[item['reservation_id'] for item in self.operation_items(db,operation)])
            return ReplacementCompleted(operation_id=operation_id,result=result)

    def replacement_result(self, operation_id):
        outcome = self.replacement_status(operation_id)
        if isinstance(outcome, ReplacementAccepted):
            return None
        if isinstance(outcome, ReplacementFailed):
            failure = outcome.error
            raise ServiceError(failure.code,failure.reason,failure.error,failure.current_version)
        return outcome.result

    def live_state(self, document_id, after_version=None, retain_history=False):
        with self.connection() as db:
            snapshot = self.snapshot_in(db,document_id)
            version = snapshot.document_version
            if after_version is None or after_version>version or (version-after_version>100 and not retain_history):
                content = {'document':self.document_in(db,document_id).model_dump()}
            else:
                updates = db.execute("SELECT r.version,r.patch,o.event_id FROM revisions r JOIN outbox o ON o.document_id=r.document_id AND o.version=r.version AND o.kind='document_updated' WHERE r.document_id=%s AND r.version>%s ORDER BY r.version",(document_id,after_version)).fetchall()
                content = {'updates':[dict(event_id=r['event_id'],document_id=document_id,previous_version=r['version']-1,version=r['version'],patches=(r['patch']['patches'] if 'patches' in r['patch'] else [r['patch']])) for r in updates]}
            operations = db.execute('SELECT p.id AS operation_id,i.reservation_id,p.status,p.result_version,p.failure FROM replacements p JOIN replacement_items i ON i.operation_id=p.id WHERE p.document_id=%s',(document_id,)).fetchall()
            drafts = db.execute("SELECT d.* FROM drafts d JOIN reservations r ON r.id=d.reservation_id WHERE r.document_id=%s AND r.status IN ('active','submitted')",(document_id,)).fetchall()
            return dict(type='document_state',reservations=snapshot.model_dump(mode='json'),operations=operations,drafts=drafts,**content)

    def index_revision(self, document_id, version, force=False):
        with self.connection(True) as db:
            doc = self.document_in(db,document_id,version)
            result = db.execute('''INSERT INTO search_documents(document_id,version,title,body,ranking_sections)
                VALUES(%s,%s,%s,%s,ARRAY(SELECT to_tsvector('simple', section) FROM unnest(%s::text[]) AS section)) ON CONFLICT(document_id)
                DO UPDATE SET version=excluded.version,title=excluded.title,body=excluded.body,ranking_sections=excluded.ranking_sections
                WHERE search_documents.version<excluded.version OR (%s AND search_documents.version=excluded.version)
                RETURNING version''',(document_id,version,self.head(db,document_id)['title'],doc.text,list(ranking_sections(doc.text)),force)).fetchone()
            return result is not None

    def search(self, request):
        """Trigram candidates, native cover-density ranking, exact source anchors."""
        if request.document_ids == []:
            return SearchResponse(hits=[])
        params, filters = [], []
        if request.document_ids is not None:
            filters.append('document_id=ANY(%s)'); params.append(request.document_ids)
        if request.mode=='literal':
            needles = [request.text]
        else:
            needles = re.findall(r'[^\W_]+',request.text)
            if not needles:
                return SearchResponse(hits=[])
        # Escape LIKE syntax; query text never becomes executable SQL/regex.
        for needle in needles:
            filters.append('body '+('LIKE' if request.mode=='literal' else 'ILIKE')+' %s')
            params.append('%'+needle.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        if request.mode == 'literal':
            score_sql, order = 'NULL::double precision', 'document_id'
        else:
            query_function = 'phraseto_tsquery' if request.mode == 'phrase' else 'plainto_tsquery'
            score_sql = ("COALESCE((SELECT max(ts_rank_cd(section, " + query_function +
                         "('simple', %s), 32)) FROM unnest(ranking_sections) AS section), 0)::double precision")
            params.insert(0, ' '.join(needles))
            order = 'score DESC, document_id'
        hits = []
        with self.connection() as db:
            # Named cursor bounds memory to one candidate document at a time.
            with db.cursor(name='search_candidates') as cursor:
                cursor.execute('SELECT document_id,version,body,'+score_sql+' AS score FROM search_documents WHERE '+' AND '.join(filters)+' ORDER BY '+order,params)
                for row in cursor:
                    body = row['body']
                    if request.mode=='literal':
                        pos = 0
                        while len(hits)<request.limit:
                            start = body.find(request.text,pos)
                            if start<0: break
                            pos = start+len(request.text)
                            hits.append(self.hit(row,start,pos))
                    else:
                        words = [re.escape(w) for w in needles]
                        if request.mode=='phrase':
                            match = re.search(r'(?<![^\W_])'+r'[\W_]+'.join(words)+r'(?![^\W_])',body,re.I)
                        else:
                            matches = [re.search(r'(?<![^\W_])'+word+r'(?![^\W_])',body,re.I) for word in words]
                            match = min(matches,key=lambda m:m.start()) if all(matches) else None
                        if match:
                            hits.append(self.hit(row,match.start(),match.end()))
                    if len(hits)>=request.limit: break
        return SearchResponse(hits=hits)

    @staticmethod
    def hit(row,start,end):
        return SearchHit(document_id=row['document_id'],target=DocumentAnchor(version=row['version'],start=start,end=end),context=row['body'][max(0,start-80):end+80],score=row.get('score'))

    def pending_events(self):
        with self.connection() as db:
            return db.execute('SELECT * FROM outbox WHERE published=0 ORDER BY sequence LIMIT 100').fetchall()

    def mark_published(self,event_id):
        with self.connection(True) as db:
            db.execute('UPDATE outbox SET published=1 WHERE event_id=%s',(event_id,))

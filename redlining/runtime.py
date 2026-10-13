"""Async adapters and recoverable workers. Commands serialize per document."""
import asyncio
import json
import logging
import os
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .services import ServiceError
from .models import ReplacementAccepted, ReplacementResult, ChangeResult, CreateEditReservationRequest, EditReservationsResult
from .services import ReplaceCommand
from .postgres import PostgresStore

log = logging.getLogger(__name__)
STREAM = 'document-events'


class DocumentService:
    def __init__(self, store, hub=None, wait_seconds=30, reconcile_seconds=2):
        self.store = store
        self.hub = hub
        self.wait_seconds = wait_seconds
        self.reconcile_seconds = reconcile_seconds

    async def reserve(self, client_id, document_id, request):
        return await asyncio.to_thread(self.store.reserve, client_id, document_id, request)

    async def reserve_batch(self, client_id, document_id, request):
        if not hasattr(self.store, 'reserve_batch'):
            if len(request.targets) != 1:
                raise ServiceError(501,'LEGACY_ADAPTER','Multiple edits require PostgreSQL')
            reservation = await self.reserve(client_id,document_id,CreateEditReservationRequest(target=request.targets[0]))
            return EditReservationsResult(reservations=[reservation])
        return await asyncio.to_thread(self.store.reserve_batch, client_id, document_id, request)

    async def reserve_and_replace(self, client_id, document_id, request_key, request):
        return await self.wait_for_change(document_id, self.store.admit_one_shot,
                                          client_id, document_id, request_key, request)

    async def change_batch(self, document_id, request):
        if not hasattr(self.store, 'admit_batch'):
            if len(request.replacements) != 1:
                raise ServiceError(501,'LEGACY_ADAPTER','Multiple edits require PostgreSQL')
            item = request.replacements[0]
            reservation = await self.reservation(item.reservation_id)
            if reservation.document_id != document_id:
                raise ServiceError(409,'DOCUMENT_MISMATCH','Reservation belongs to another document')
            return await self.change(ReplaceCommand(item.reservation_id,item.replacement))
        return await self.wait_for_change(document_id, self.store.admit_batch, document_id, request.replacements)

    async def reservation(self, rid):
        return await asyncio.to_thread(self.store.reservation, rid)

    async def reservation_snapshot(self, document_id):
        return await asyncio.to_thread(self.store.reservation_snapshot, document_id)

    async def renew(self, rid):
        return await asyncio.to_thread(self.store.renew, rid)

    async def release(self, rid):
        return await asyncio.to_thread(self.store.release, rid)

    async def get_document(self, document_id, version=None):
        return await asyncio.to_thread(self.store.get_document, document_id, version)

    async def search(self, request):
        return await asyncio.to_thread(self.store.search, request)

    async def change(self, command):
        reservation = await self.reservation(command.reservation_id)
        return await self.wait_for_change(reservation.document_id, self.store.admit, command)

    async def replacement_operation(self, operation_id):
        return await asyncio.to_thread(self.store.replacement_status, operation_id)

    async def wait_for_change(self, document_id, admit, *arguments):
        # Register before admission. Subscription acknowledgment also wakes this
        # waiter, closing the Redis subscribe/read race.
        signal = await self.hub.attach(document_id) if self.hub else asyncio.Event()
        try:
            oid = await asyncio.to_thread(admit, *arguments)
            deadline = asyncio.get_running_loop().time() + self.wait_seconds
            while True:
                signal.clear()
                result = await asyncio.to_thread(self.store.replacement_result, oid)
                if result is not None:
                    if isinstance(result, ChangeResult):
                        return ReplacementResult(document_id=result.document_id,version=result.version,
                                                 text=result.text,reservation_ids=[result.reservation_id])
                    return result
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return ReplacementAccepted(operation_id=oid, status_url=f'/replacement-operations/{oid}')
                try:
                    await asyncio.wait_for(signal.wait(), min(remaining, self.reconcile_seconds))
                except asyncio.TimeoutError:
                    # Same server-side recovery as sockets: Pub/Sub can lose the
                    # final notification. The database outcome stays authoritative.
                    pass
        finally:
            if self.hub:
                await self.hub.detach(document_id, signal)


SQLiteService = DocumentService  # Legacy name retained for SQLite fixture tests.


class Workers:
    def __init__(self, store, redis):
        self.store = store
        self.redis = redis
        self.consumer = str(uuid4())
        self.claim_cursors = {}

    async def write_once(self):
        await asyncio.to_thread(self.store.expire)
        return await asyncio.to_thread(self.store.process_next)

    async def relay_once(self):
        for row in await asyncio.to_thread(self.store.pending_events):
            event = {k: row[k] for k in ('event_id', 'document_id', 'kind', 'version', 'payload')}
            await self.redis.xadd(STREAM, {'event': json.dumps(event)})
            await asyncio.to_thread(self.store.mark_published, row['event_id'])

    async def groups(self):
        for group in ('search-index-v1', 'live-delivery-v1'):
            try:
                await self.redis.xgroup_create(STREAM, group, id='0', mkstream=True)
            except ResponseError as exc:
                if 'BUSYGROUP' not in str(exc):
                    raise

    async def consume_once(self, group):
        # Claim stale pending entries before reading new ones. Side effects are idempotent.
        claimed = await self.redis.xautoclaim(STREAM, group, self.consumer, 60000, self.claim_cursors.get(group, '0-0'), count=20)
        self.claim_cursors[group] = claimed[0]
        entries = claimed[1]
        if not entries:
            batches = await self.redis.xreadgroup(group, self.consumer, {STREAM: '>'}, count=20, block=100)
            entries = [entry for _, batch in batches for entry in batch]
        for stream_id, fields in entries:
            try:
                event = json.loads(fields['event'])
                if group == 'search-index-v1':
                    if event['kind'] in ('document_created', 'document_updated'):
                        await asyncio.to_thread(self.store.index_revision, event['document_id'], event['version'])
                else:
                    # Invalidation only: gateways fetch CURRENT snapshots, never old range lists.
                    await self.redis.publish('document:'+event['document_id'], event['event_id'])
                await self.redis.xack(STREAM, group, stream_id)
            except Exception:
                log.exception('Event processing failed; left pending: group=%s entry=%s', group, stream_id)

    async def repeat(self, action, delay=.1):
        while True:
            try:
                await action()
            except Exception:
                log.exception('Worker iteration failed; durable work retained')
                await asyncio.sleep(1)
            await asyncio.sleep(delay)

    async def run(self):
        # Database writes remain available while Redis is down.
        async def redis_work():
            while True:
                try:
                    await self.groups()
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(self.repeat(self.relay_once))
                        for group in ('search-index-v1', 'live-delivery-v1'):
                            tasks.create_task(self.repeat(lambda g=group: self.consume_once(g)))
                except Exception:
                    log.exception('Redis workers unavailable')
                    await asyncio.sleep(1)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.repeat(self.write_once, .02))
            tasks.create_task(redis_work())


async def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get('REDLINING_DB'):
        from .storage import Store
        store = Store(os.environ['REDLINING_DB'])  # Explicit legacy fixture mode.
    else:
        store = PostgresStore(initialize=True)
    redis = Redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
    try:
        await Workers(store, redis).run()
    finally:
        await redis.aclose()


if __name__ == '__main__':
    asyncio.run(main())

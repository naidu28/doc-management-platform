"""HTTP completion waiting: notification races, reconciliation, cancellation, replay."""
import asyncio

import pytest

from src.models import CreateEditReservationRequest, DocumentAnchor
from src.runtime import DocumentService
from src.services import ReplaceCommand, ServiceError


class Hub:
    def __init__(self):
        self.signal = None
        self.detached = False

    async def attach(self, document_id):
        self.signal = asyncio.Event()
        return self.signal

    async def detach(self, document_id, signal):
        assert signal is self.signal
        self.detached = True


@pytest.fixture
def storage(pg_store):
    s = pg_store
    s.create_document('X','30 days')
    r = s.reserve('a','X',CreateEditReservationRequest(target=DocumentAnchor(version=1,start=0,end=2)))
    return s,ReplaceCommand(r.reservation_id,'60')


def observed_admission(s, hub):
    loop = asyncio.get_running_loop()
    admitted = asyncio.Event()
    original = s.admit
    def admit(command):
        assert hub.signal is not None  # Listener registered before durable admission.
        result = original(command)
        loop.call_soon_threadsafe(admitted.set)
        return result
    s.admit = admit
    return admitted


def test_notification_wakes_request_and_detaches(storage):
    s,command=storage
    async def run():
        hub=Hub();admitted=observed_admission(s,hub)
        service=DocumentService(s,hub,wait_seconds=5,reconcile_seconds=30)
        task=asyncio.create_task(service.change(command))
        await admitted.wait()
        await asyncio.to_thread(s.process_next)
        hub.signal.set()
        result=await asyncio.wait_for(task,1)
        assert result.text=='60 days' and result.version==2
        assert result.reservation_ids==[command.reservation_id]
        assert hub.detached
    asyncio.run(run())


def test_completion_before_wait_and_replay_return_original_revision(storage):
    s,command=storage
    original=s.admit
    def admit_and_finish(command):
        oid=original(command)
        s.process_next()
        return oid
    s.admit=admit_and_finish
    async def run():
        first=await DocumentService(s,Hub()).change(command)
        # Another replacement commits before replaying the first request.
        r=s.reserve('b','X',CreateEditReservationRequest(target=DocumentAnchor(version=2,start=0,end=2)))
        original(ReplaceCommand(r.reservation_id,'90'));s.process_next()
        replay=await DocumentService(s,Hub()).change(command)
        assert replay==first
        assert replay.text=='60 days' and replay.version==2
        assert s.get_document('X').text=='90 days'
    asyncio.run(run())


def test_missed_notification_reconciles(storage):
    s,command=storage
    async def run():
        hub=Hub();admitted=observed_admission(s,hub)
        task=asyncio.create_task(DocumentService(s,hub,reconcile_seconds=.02).change(command))
        await admitted.wait()
        await asyncio.to_thread(s.process_next)
        # Deliberately no signal: a lost Pub/Sub message must not strand the response.
        assert (await asyncio.wait_for(task,1)).text=='60 days'
        assert hub.detached
    asyncio.run(run())


def test_timeout_preserves_accepted_work_and_can_replay(storage):
    s,command=storage
    async def run():
        hub=Hub()
        pending = await DocumentService(s,hub,wait_seconds=.01).change(command)
        assert pending.status=='pending'
        assert pending.status_url==f'/replacement-operations/{pending.operation_id}' 
        assert s.reservation(command.reservation_id).status=='submitted'
        oid=s.reservation(command.reservation_id).operation_id
        assert s.operation(oid)['status']=='queued'
        assert hub.detached
        s.process_next()
        result=await DocumentService(s,Hub()).change(command)
        assert result.text=='60 days'
        assert s.get_document('X').version==2
    asyncio.run(run())


def test_cancelled_wait_does_not_cancel_durable_replacement(storage):
    s,command=storage
    async def run():
        hub=Hub();admitted=observed_admission(s,hub)
        task=asyncio.create_task(DocumentService(s,hub).change(command))
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert hub.detached
        assert s.reservation(command.reservation_id).status=='submitted'
        s.process_next()
        assert s.get_document('X').text=='60 days'
    asyncio.run(run())


def test_failure_returns_stored_error_and_detaches(storage):
    s,command=storage
    async def run():
        hub=Hub();admitted=observed_admission(s,hub)
        task=asyncio.create_task(DocumentService(s,hub).change(command))
        await admitted.wait()
        with s.connection(True) as db:
            db.execute('UPDATE reservations SET version=99 WHERE id=%s',(command.reservation_id,))
        s.process_next();hub.signal.set()
        with pytest.raises(ServiceError) as error: await task
        assert error.value.reason=='QUEUE_INVARIANT'
        assert s.get_document('X').version==1
        assert hub.detached
    asyncio.run(run())

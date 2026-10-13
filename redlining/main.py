"""Runnable API with PostgreSQL and shared per-document live subscriptions."""
import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from .api import create_app
from .runtime import DocumentService
from .services import ServiceError
from .postgres import PostgresStore


class Subscription:
    def __init__(self):
        self.listeners = set()
        self.task = None


class LiveHub:
    """One Redis subscription per open document per webserver process.

    Each socket serializes fresh database reads and sends. Notifications are only
    wakeups; duplicates and out-of-order Redis events cannot install stale lists.
    """
    def __init__(self, redis):
        self.redis = redis
        self.documents = {}

    async def attach(self, document_id):
        sub = self.documents.get(document_id)
        if sub is None:
            sub = self.documents[document_id] = Subscription()
            sub.task = asyncio.create_task(self.listen(document_id, sub))
        signal = asyncio.Event()
        sub.listeners.add(signal)
        return signal

    async def detach(self, document_id, signal):
        sub = self.documents[document_id]
        sub.listeners.discard(signal)
        if not sub.listeners:
            del self.documents[document_id]
            sub.task.cancel()
            with suppress(asyncio.CancelledError):
                await sub.task

    async def listen(self, document_id, sub):
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.subscribe('document:'+document_id)
                    # Wake listeners after subscribe to close the initial fetch/subscribe gap.
                    for signal in sub.listeners:
                        signal.set()
                    while True:
                        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                        if msg:
                            for signal in sub.listeners:
                                signal.set()
                        await asyncio.sleep(.01)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Periodic database reconciliation continues during Redis outages.
                await asyncio.sleep(1)


def build_app(redis_url=None, database_url=None):
    store = PostgresStore(database_url, initialize=True)
    wait_seconds = float(os.environ.get('REPLACEMENT_WAIT_SECONDS', '30'))
    if not 0 <= wait_seconds <= 30:
        raise ValueError('REPLACEMENT_WAIT_SECONDS must be between 0 and 30')
    service = DocumentService(store, wait_seconds=wait_seconds)
    app = create_app(service, service)
    from .ui import attach_ui
    attach_ui(app, store)
    redis = Redis.from_url(redis_url or os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
    hub = LiveHub(redis)
    service.hub = hub

    @asynccontextmanager
    async def lifespan(app):
        yield
        for sub in list(hub.documents.values()):
            sub.task.cancel()
        await asyncio.gather(*(s.task for s in hub.documents.values()), return_exceptions=True)
        await redis.aclose()

    app.router.lifespan_context = lifespan

    @app.websocket('/documents/{document_id}/updates')
    async def updates(socket: WebSocket, document_id: str):
        await socket.accept()
        signal = await hub.attach(document_id)
        receive_task = asyncio.create_task(socket.receive())
        connection_id = None
        leaving = False
        raw_version = socket.query_params.get('after_version', '')
        try:
            if raw_version and (not raw_version.isascii() or not raw_version.isdecimal() or len(raw_version) > 19):
                raise ServiceError(422, 'INVALID_VERSION', 'Invalid subscription revision')
            version = int(raw_version) if raw_version else None
            if version is not None and not 1 <= version <= 9223372036854775807:
                raise ServiceError(422, 'INVALID_VERSION', 'Invalid subscription revision')
            client_id = socket.query_params.get('client_id')
            if client_id is not None and isinstance(store, PostgresStore):
                if not 1 <= len(client_id) <= 200:
                    raise ServiceError(422,'INVALID_CLIENT','Invalid browser client ID')
                connection_id = await asyncio.to_thread(store.open_browser_connection,document_id,client_id)
            while True:
                signal.clear()
                state = await asyncio.to_thread(store.live_state, document_id, version, socket.query_params.get('retain_history') == '1')
                await asyncio.wait_for(socket.send_json(state), 10)
                version = state['reservations']['document_version']
                wake = asyncio.create_task(signal.wait())
                try:
                    done, _ = await asyncio.wait([wake, receive_task], timeout=2, return_when=asyncio.FIRST_COMPLETED)
                    if receive_task in done:
                        message = receive_task.result()
                        if message['type'] == 'websocket.disconnect':
                            leaving = message.get('reason') == 'page-left'
                            break
                        if connection_id and message.get('text'):
                            try:
                                heartbeat = json.loads(message['text']).get('type') == 'heartbeat'
                            except (ValueError, AttributeError):
                                heartbeat = False
                            if heartbeat and not await asyncio.to_thread(store.heartbeat_browser_connection,document_id,connection_id):
                                await socket.close(code=1008,reason='Session expired; reconnect')
                                break
                        receive_task = asyncio.create_task(socket.receive())
                finally:
                    wake.cancel()
                    with suppress(asyncio.CancelledError):
                        await wake
        except ServiceError as exc:
            await socket.send_json({'type': 'error', 'code': exc.status, 'reason': exc.reason})
            await socket.close(code=1008)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await receive_task
            try:
                if connection_id:
                    await asyncio.to_thread(store.close_browser_connection,document_id,connection_id,leaving)
            finally:
                await hub.detach(document_id, signal)

    return app




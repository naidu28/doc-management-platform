"""Real SQLite/Redis tests, isolated from any running user Redis instance."""
import asyncio
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis
from redis.asyncio import Redis

from redlining.api import create_app
from redlining.models import CreateEditReservationRequest, DocumentAnchor, SearchRequest
from redlining.runtime import SQLiteService, Workers, STREAM
from redlining.services import ReplaceCommand
from redlining.storage import Store




def test_http_reserve_submit_acknowledge_search(tmp_path):
    store=Store(tmp_path/'http.sqlite')
    store.create_document('X','30 days contract')
    service=SQLiteService(store)
    with TestClient(create_app(service,service)) as client:
        r=client.post('/documents/X/edit-reservations',headers={'X-Client-ID':'a'},json={'target':{'version':1,'start':0,'end':2}})
        assert r.status_code==200
        body={'reservation_id':r.json()['reservation_id'],'replacement':'60'}
        with ThreadPoolExecutor() as pool:
            pending=pool.submit(client.post,'/replacements',json=body)
            for _ in range(100):
                if store.process_next(): break
                time.sleep(.01)
            response=pending.result(timeout=5)
        assert response.status_code==200
        assert response.json()['text']=='60 days contract'
        assert store.live_state('X')['operations'][0]['status']=='completed'
        assert client.post('/replacements',json=body).json()==response.json()
        assert client.post('/replacements',json={**body,'replacement':'90'}).status_code==409
        store.index_revision('X',2)
        hit=client.post('/search',json={'text':'60','mode':'literal'}).json()['hits'][0]
        assert hit['target']=={'version':2,'start':0,'end':2}


def test_stream_outbox_index_pubsub_and_redelivery(tmp_path,redis_url):
    async def scenario():
        store=Store(tmp_path/'redis.sqlite')
        store.create_document('X','30 days')
        redis=Redis.from_url(redis_url,decode_responses=True)
        worker=Workers(store,redis)
        try:
            await worker.groups()
            async with redis.pubsub() as subscriber:
                await subscriber.subscribe('document:X')
                await subscriber.get_message(timeout=1)  # subscription acknowledgement
                await worker.relay_once()
                assert not store.pending_events()
                await worker.consume_once('search-index-v1')
                assert store.search(SearchRequest(text='30')).hits
                await worker.consume_once('live-delivery-v1')
                assert (await subscriber.get_message(ignore_subscribe_messages=True,timeout=1))['type']=='message'
                r=store.reserve('a','X',CreateEditReservationRequest(target=DocumentAnchor(version=1,start=0,end=2)))
                store.admit(ReplaceCommand(r.reservation_id,'60'))
                store.process_next()
                await worker.relay_once()
                await worker.consume_once('search-index-v1')
                assert not store.search(SearchRequest(text='30')).hits
                assert store.search(SearchRequest(text='60')).hits[0].target.version==2
                # Simulate publish succeeded / outbox mark failed: republish all retained rows.
                with store.connection(True) as db:
                    db.execute('UPDATE outbox SET published=0')
                await worker.relay_once()
                await worker.consume_once('search-index-v1')
                assert store.get_document('X').version==2
                assert store.search(SearchRequest(text='60')).hits[0].target.version==2
                assert (await redis.xpending(STREAM,'search-index-v1'))['pending']==0
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_websocket_initial_snapshot_and_commit_catchup(tmp_path,redis_url):
    from redlining.main import build_app
    store=Store(tmp_path/'socket.sqlite')
    store.create_document('X','30 days')
    with TestClient(build_app(store.path,redis_url)) as client:
        with client.websocket_connect('/documents/X/updates') as ws:
            initial=ws.receive_json()
            assert initial['document']['text']=='30 days'
            assert initial['reservations']['reservations']==[]
            r=store.reserve('a','X',CreateEditReservationRequest(target=DocumentAnchor(version=1,start=0,end=2)))
            # Real Pub/Sub notification wakes a fresh SQLite snapshot, not a historical list.
            redis=SyncRedis.from_url(redis_url)
            redis.publish('document:X','wake')
            while True:
                state=ws.receive_json()
                if state['reservations']['reservations']:
                    break
            assert state['reservations']['reservations'][0]['reservation_id']==r.reservation_id
            store.admit(ReplaceCommand(r.reservation_id,'60'))
            store.process_next()
            redis.publish('document:X','wake')
            while True:
                state=ws.receive_json()
                if state['reservations']['document_version']==2:
                    break
            assert state['updates'][0]['patch']['replacement']=='60'
            assert state['reservations']['reservations']==[]
            redis.close()


def test_within_document_search_route(tmp_path):
    store=Store(tmp_path/'scoped.sqlite')
    store.create_document('X','contract and contract')
    store.create_document('Y','contract elsewhere')
    store.index_revision('X',1)
    store.index_revision('Y',1)
    service=SQLiteService(store)
    with TestClient(create_app(service,service)) as client:
        response=client.get('/documents/X/search?q=contract')
        assert response.status_code==200
        assert [(h['document_id'],h['target']['start']) for h in response.json()['hits']]==[('X',0),('X',13)]


def test_real_http_worker_and_websocket(tmp_path, redis_url):
    import json
    import os
    import sys
    import httpx
    from websockets.sync.client import connect

    path=tmp_path/'network.sqlite'
    Store(path).create_document('X','30 days')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    env={**os.environ,'REDLINING_DB':str(path),'REDIS_URL':redis_url}
    server=subprocess.Popen([sys.executable,'-m','uvicorn','redlining.main:build_app','--factory','--host','127.0.0.1','--port',str(port)],
                            env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    worker=subprocess.Popen([sys.executable,'-m','redlining.runtime'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get('/documents/X').status_code==200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.03)
            else:
                pytest.fail('API failed to start')
            with connect(f'ws://127.0.0.1:{port}/documents/X/updates',proxy=None) as ws:
                assert json.loads(ws.recv(timeout=5))['document']['text']=='30 days'
                reservation=client.post('/documents/X/edit-reservations',headers={'X-Client-ID':'browser'},
                    json={'target':{'version':1,'start':0,'end':2}}).json()
                result=client.post('/replacements',json={'reservation_id':reservation['reservation_id'],'replacement':'60'})
                assert result.status_code==200
                assert result.json()['reservation_id']==reservation['reservation_id']
                for _ in range(10):
                    state=json.loads(ws.recv(timeout=5))
                    if state['reservations']['document_version']==2:
                        assert state['updates'][0]['patch']['replacement']=='60'
                        assert state['operations'][0]['status']=='completed'
                        assert state['operations'][0]['reservation_id']==result.json()['reservation_id']
                        assert result.json()['text']=='60 days'
                        break
                else:
                    pytest.fail('No committed update delivered')
                for _ in range(100):
                    hits=client.get('/documents/X/search?q=60').json()['hits']
                    if hits:
                        break
                    time.sleep(.03)
                assert hits[0]['target']['version']==2
    finally:
        for process in (worker,server):
            process.terminate()
        for process in (worker,server):
            process.wait(timeout=5)

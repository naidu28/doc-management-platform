import shutil
import socket
import subprocess
import time
import pytest
from redis import Redis as SyncRedis

@pytest.fixture
def pg_store():
    """Real application adapter in a unique schema, with no preloaded documents."""
    import os
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from src.postgres import PostgresStore
    dsn = os.environ.get('REDLINING_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set REDLINING_TEST_DATABASE_URL for PostgreSQL integration tests')
    name = 'app_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
    try:
        yield PostgresStore(make_conninfo(dsn, options=f'-c search_path={name},public'),initialize=True)
    finally:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))

@pytest.fixture
def redis_url(tmp_path):
    executable=shutil.which('redis-server')
    if not executable:
        pytest.skip('redis-server required for real transport integration')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    process=subprocess.Popen([executable,'--bind','127.0.0.1','--port',str(port),
        '--save','','--appendonly','no','--dir',str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url=f'redis://127.0.0.1:{port}/0'
    client=SyncRedis.from_url(url)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except Exception:
                time.sleep(.02)
        else:
            pytest.fail('Redis failed to start')
        yield url
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)

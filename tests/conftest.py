import shutil
import socket
import subprocess
import time
import pytest
from redis import Redis as SyncRedis

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


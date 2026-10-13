"""Bounded corpus terms/phrase results, reconciled against committed index changes."""
from collections import OrderedDict
from dataclasses import dataclass
import sys
from threading import Lock
import time

from .models import SearchResponse


@dataclass
class Entry:
    payload: bytes
    generation: int
    touched: float
    size: int


class QueryResultCache:
    def __init__(self, max_queries=64, max_bytes=8*1024*1024, idle_seconds=300, clock=time.monotonic):
        if min(max_queries, max_bytes, idle_seconds) < 0:
            raise ValueError('Query cache limits must be nonnegative')
        self.max_queries, self.max_bytes, self.idle_seconds = max_queries, max_bytes, idle_seconds
        self.clock = clock
        self.entries, self.probation = OrderedDict(), OrderedDict()
        self.bytes = 0
        self.lock = Lock()
        self.hits = self.refreshes = self.refills = 0

    @staticmethod
    def key_size(key):
        return sys.getsizeof(key) + sum(map(sys.getsizeof,key)) + 512

    def remove(self, collection, key):
        value = collection.pop(key)
        self.bytes -= value.size if isinstance(value, Entry) else value[1]

    def expire(self, now):
        for collection in (self.entries, self.probation):
            for key,value in list(collection.items()):
                touched = value.touched if isinstance(value,Entry) else value[0]
                if now-touched >= self.idle_seconds:
                    self.remove(collection,key)

    def room(self, size):
        if size > self.max_bytes:
            return False
        while self.bytes+size > self.max_bytes:
            collection = self.probation if self.probation else self.entries
            self.remove(collection,next(iter(collection)))
        return True

    def search(self, request, generation, changes, execute):
        """Callbacks share one PostgreSQL repeatable-read snapshot.

        changes(old_generation) returns changed IDs, or None when reconciliation
        exceeds retained history/work limits. execute(ids) resolves exact hits;
        ids=None means a complete corpus query.
        """
        if not self.max_queries or not self.max_bytes or not self.idle_seconds:
            return execute(None)
        key = (request.text, request.mode, request.limit)
        with self.lock:
            now = self.clock()
            self.expire(now)
            current = generation()
            entry = self.entries.get(key)
            if entry and entry.generation > current:
                return execute(None)  # Older reader cannot regress this entry.
            if entry:
                result = SearchResponse.model_validate_json(entry.payload)
                if current != entry.generation:
                    changed = changes(entry.generation)
                    if changed is None:
                        result = execute(None)
                        self.refills += 1
                    else:
                        fresh = execute(changed).hits if changed else []
                        updated = {hit.document_id:hit for hit in fresh}
                        # A departing or demoted winner needs a replacement from
                        # outside the stored top K. Refill rather than guess.
                        refill = any(hit.document_id in changed and
                            (hit.document_id not in updated or updated[hit.document_id].score < hit.score)
                            for hit in result.hits)
                        if refill:
                            result = execute(None)
                            self.refills += 1
                        else:
                            merged = [hit for hit in result.hits if hit.document_id not in changed]+fresh
                            merged.sort(key=lambda hit:(-hit.score,hit.document_id))
                            result = SearchResponse(hits=merged[:request.limit])
                        self.refreshes += 1
                else:
                    self.hits += 1
                self.remove(self.entries,key)
                self.save(key,result,current,now)
                return result
            result = execute(None)
            if key in self.probation:
                self.remove(self.probation,key)
                self.save(key,result,current,now)
            else:
                size = self.key_size(key)
                if self.room(size):
                    while len(self.probation) >= self.max_queries:
                        self.remove(self.probation,next(iter(self.probation)))
                    self.probation[key] = (now,size)
                    self.bytes += size
            return result

    def save(self,key,result,generation,now):
        payload = result.model_dump_json().encode()
        size = self.key_size(key)+sys.getsizeof(payload)
        if self.room(size):
            while len(self.entries) >= self.max_queries:
                self.remove(self.entries,next(iter(self.entries)))
            self.entries[key] = Entry(payload,generation,now,size)
            self.bytes += size

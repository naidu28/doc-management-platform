import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.sessionStorage = { getItem: () => 'client', setItem() {} };
globalThis.location = { protocol: 'http:', host: 'test' };
let socket;
globalThis.WebSocket = class {
  constructor() { socket = this; }
  close() { this.onclose?.(); }
};
const { createDocumentStore } = await import('./store.js');
function send(version = 1, text = 'abc') {
  socket.onmessage({ data: JSON.stringify({ type: 'document_state',
    document: { document_id: 'X', version, text },
    reservations: { document_version: version, reservations: [] } }) });
}

test('replacement returns HTTP document without rolling back newer subscribed state', async () => {
  const store = createDocumentStore('X');
  const stop = store.start();
  send();
  let calls = 0;
  const result = { document_id: 'X', reservation_ids: ['r'], version: 2, text: 'first edit' };
  globalThis.fetch = async () => {
    calls++;
    send(3, 'later edit');
    return { ok: true, status: 200, json: async () => result };
  };
  assert.deepEqual(await store.perform('/documents/X/replacements', { method: 'POST' }), result);
  assert.equal(store.getSnapshot().document.version, 3);
  assert.equal(calls, 1);
  stop();
});

test('proxy timeout surfaces uncertainty without automatically resubmitting', async () => {
  const store = createDocumentStore('X');
  const stop = store.start();
  send();
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: false, status: 504, json: async () => ({ error: 'Gateway timeout; outcome unknown' }) };
  };
  await assert.rejects(store.perform('/documents/X/replacements', { method: 'POST' }), /outcome unknown/);
  assert.equal(calls, 1);
  stop();
});

test('reservation responses remain immediate', async () => {
  const store = createDocumentStore('X');
  const stop = store.start();
  send();
  const reservation = { reservation_id: 'r', operation_id: 'pending', status: 'submitted', target: { version: 1, start: 0, end: 2 } };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => reservation });
  assert.deepEqual(await store.perform('/documents/X/edit-reservations', { method: 'POST' }), reservation);
  stop();
});

test('document teardown signals explicit departure and identifies its browser client', () => {
  const original = globalThis.WebSocket;
  let url, closed;
  globalThis.WebSocket = class {
    constructor(value) { url = value; }
    close(code, reason) { closed = { code, reason }; this.onclose?.(); }
  };
  try {
    const store = createDocumentStore('X');
    const stop = store.start();
    assert.match(url, /client_id=client/);
    stop();
    assert.deepEqual(closed, { code: 1000, reason: 'page-left' });
  } finally { globalThis.WebSocket = original; }
});

test('202 remains pending until the subscription reports completion', async () => {
  const store = createDocumentStore('X');
  const stop = store.start();
  send();
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: true, status: 202, json: async () => ({status:'pending', operation_id:'op', status_url:'/replacement-operations/op'}) };
  };
  const result = await store.perform('/documents/X/replacements', { method:'POST', body:JSON.stringify({replacements:[{reservation_id:'r',replacement:'new'}]}) });
  assert.equal(result.status, 'pending');
  assert.match(store.getSnapshot().operationNotice, /Waiting/);
  assert.equal(store.getSnapshot().document.version, 1);
  socket.onmessage({data:JSON.stringify({type:'document_state',document:{document_id:'X',version:2,text:'new'},
    reservations:{document_version:2,reservations:[]},operations:[{operation_id:'op',reservation_id:'r',status:'completed'}]})});
  assert.equal(store.getSnapshot().operationNotice, 'Edits applied.');
  assert.equal(calls, 1);
  stop();
});

test('batch 202 surfaces a later failure through the subscription', async () => {
  const store = createDocumentStore('X');
  const stop = store.start();
  send();
  globalThis.fetch = async () => ({ok:true,status:202,json:async()=>({status:'pending',operation_id:'op',status_url:'/replacement-operations/op'})});
  await store.perform('/documents/X/replacements',{method:'POST',body:JSON.stringify({replacements:[
    {reservation_id:'a',replacement:'A'}, {reservation_id:'b',replacement:'B'}]})});
  socket.onmessage({data:JSON.stringify({type:'document_state',document:{document_id:'X',version:1,text:'abc'},
    reservations:{document_version:1,reservations:[]},operations:['a','b'].map(reservation_id=>({
      operation_id:'op',reservation_id,status:'failed',failure:{error:'Batch could not be applied'}}))})});
  assert.equal(store.getSnapshot().operationNotice,'Batch could not be applied');
  assert.equal(store.getSnapshot().document.version,1);
  stop();
});

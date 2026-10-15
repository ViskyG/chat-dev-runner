"""Offline design model, not production Runner code and not a provider integration.

SQLite proves this model's record transitions; FakeMailbox simulates conditional writes.
No project program, agent, GitHub or Google API is invoked here.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

NAMESPACE = uuid.UUID('305b72e3-cba7-4e69-9e5a-c38bc5c10e12')

def digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()

@dataclass
class FakeMailbox:
    name: str
    runner: str = 'runner-1'
    revision: int = 0
    dispatch: str = 'DRAFT'
    token: str = 'uninitialized'
    session: int = 0
    payload: dict[str, Any] = field(default_factory=lambda: {'action': 'test', 'branch': 'main'})
    receipt: dict[str, Any] | None = None

    def change(self, payload: dict[str, Any] | None = None, *, ready: bool = True) -> None:
        """Author uses DRAFT, then READY last. Token is not changed by the author."""
        self.dispatch = 'DRAFT'
        self.revision += 1
        if payload is not None:
            self.payload = json.loads(json.dumps(payload))
        if ready:
            self.dispatch = 'READY'
            self.revision += 1

    def corrupt_ready(self) -> None:
        self.payload['changed_without_draft'] = True
        self.revision += 1

    def baseline(self, session: int) -> None:
        old_ready = self.dispatch == 'READY'
        self.session = session
        self.token = str(uuid.uuid5(NAMESPACE, f'{self.name}:session:{session}:rev:{self.revision}'))
        self.dispatch = 'NEEDS_RESUBMIT' if old_ready else 'DRAFT'
        self.revision += 1

    def ack(self, revision: int, consumed_token: str, receipt: dict[str, Any]) -> bool:
        if self.revision != revision or self.token != consumed_token or self.dispatch != 'READY':
            return False
        self.receipt = dict(receipt)
        self.dispatch = 'ACCEPTED'
        self.token = str(uuid.uuid5(NAMESPACE, consumed_token + ':next'))
        self.revision += 1
        return True

class Model:
    """One runner; session-scoped FIFO with durable non-runnable ACK candidates."""
    def __init__(self, database: str = ':memory:', *, runner: str = 'runner-1', capacity: int = 100):
        self.db = sqlite3.connect(database, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS operations(
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
            session INTEGER NOT NULL, state TEXT NOT NULL, sequence INTEGER UNIQUE,
            mailbox TEXT, token TEXT, revision INTEGER, ack_proof INTEGER NOT NULL DEFAULT 0,
            source_version TEXT, invocations INTEGER NOT NULL DEFAULT 0,
            outcome TEXT, delivery TEXT, local_saved INTEGER NOT NULL DEFAULT 0);
          CREATE UNIQUE INDEX IF NOT EXISTS one_lane ON operations((1))
            WHERE state IN ('STARTING','RUNNING');
        ''')
        self.session = 0
        self.live = False
        self.runner = runner
        self.capacity = capacity
        self.blocked_cleanup = False
        self.events: list[dict[str, Any]] = []

    def event(self, kind: str, **kw: Any) -> None:
        self.events.append({'event':kind, **kw})

    def startup(self, mailboxes: list[FakeMailbox]) -> None:
        self.session += 1
        if self.db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('STARTING','RUNNING')").fetchone()[0]:
            self.blocked_cleanup = True
        self.db.execute("UPDATE operations SET state='SUSPENDED_RESTART' WHERE state='QUEUED'")
        self.db.execute("UPDATE operations SET state='INTERRUPTED' WHERE state IN ('STARTING','RUNNING')")
        self.db.execute("UPDATE operations SET state='ACK_UNKNOWN' WHERE state='ACK_PENDING'")
        self.live = True
        for m in mailboxes:
            if m.runner == self.runner:
                m.baseline(self.session)
        self.event('STARTUP_BASELINE', session=self.session)

    def observation_gap(self, mailbox: FakeMailbox) -> None:
        # Separate mailbox epoch in production; model changes only this slot's issued token.
        mailbox.baseline(self.session)
        self.event('OBSERVATION_GAP', mailbox=mailbox.name)

    def row(self, rid: str) -> dict[str, Any]:
        r = self.db.execute('SELECT * FROM operations WHERE id=?',(rid,)).fetchone()
        if r is None:
            raise KeyError(rid)
        return dict(r)

    def reserve(self, m: FakeMailbox) -> tuple[str, str | None]:
        if not self.live or m.dispatch != 'READY':
            return 'IGNORED', None
        if m.runner != self.runner:
            return 'WRONG_RUNNER', None
        if m.session != self.session:
            return 'NEEDS_RESUBMIT', None
        if m.payload.get('action') not in {'test','build','shell'} or not m.payload.get('branch'):
            return 'INVALID_SCHEMA', None
        dg = digest(m.payload)
        rid = m.payload.get('request_id') or str(uuid.uuid5(NAMESPACE, self.runner+':'+m.name+':'+m.token))
        old = self.db.execute('SELECT * FROM operations WHERE id=?',(rid,)).fetchone()
        if old is not None:
            return ('DUPLICATE' if old['digest']==dg else 'REQUEST_ID_REUSED'), rid
        n = self.db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('QUEUED','ACK_PENDING','ACK_UNKNOWN')").fetchone()[0]
        if n >= self.capacity:
            return 'QUEUE_FULL', None
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO operations(id,digest,payload,session,state,mailbox,token,revision) VALUES(?,?,?,?,?,?,?,?)',
                (rid,dg,json.dumps(m.payload,sort_keys=True),self.session,'ACK_PENDING',m.name,m.token,m.revision))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise
        self.event('CANDIDATE_RESERVED', request_id=rid)
        return 'ACK_PENDING', rid

    def acknowledge(self, rid: str, m: FakeMailbox, *, lost_response: bool = False) -> str:
        r=self.row(rid)
        if r['state'] != 'ACK_PENDING':
            return r['state']
        if (r['mailbox'] != m.name or r['session'] != self.session):
            return 'WRONG_MAILBOX'
        receipt={'request_id':rid,'accepted_slot_token':r['token'],'payload_sha256':r['digest']}
        if not m.ack(r['revision'],r['token'],receipt):
            self.db.execute("UPDATE operations SET state='INPUT_CHANGED' WHERE id=?",(rid,))
            self.event('ACK_CONFLICT',request_id=rid)
            return 'INPUT_CHANGED'
        if lost_response:
            self.db.execute("UPDATE operations SET state='ACK_UNKNOWN' WHERE id=?",(rid,))
            self.event('ACK_RESPONSE_LOST',request_id=rid)
            return 'ACK_UNKNOWN'
        return self._finalize(rid)

    def _finalize(self, rid: str) -> str:
        r=self.row(rid)
        if not self.live or r['session']!=self.session:
            return 'SUSPENDED_RESTART'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            seq=self.db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM operations').fetchone()[0]
            changed=self.db.execute("UPDATE operations SET state='QUEUED',ack_proof=1,sequence=? WHERE id=? AND state IN ('ACK_PENDING','ACK_UNKNOWN')",(seq,rid)).rowcount
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        if changed:
            self.event('QUEUED',request_id=rid,sequence=seq)
        return self.row(rid)['state']

    def reconcile_ack(self, rid: str, m: FakeMailbox) -> str:
        r=self.row(rid)
        receipt=m.receipt
        if r['state']!='ACK_UNKNOWN':return r['state']
        if (receipt and receipt['request_id']==rid and receipt['accepted_slot_token']==r['token'] and receipt['payload_sha256']==r['digest']):
            return self._finalize(rid)
        return 'ACK_UNKNOWN'

    def submit(self,m:FakeMailbox,*,lost_response:bool=False) -> tuple[str,str|None]:
        state,rid=self.reserve(m)
        if state=='DUPLICATE' and rid:
            r=self.row(rid)
            if r['state'] in {'ACK_PENDING','ACK_UNKNOWN'}:
                return 'WAITING_ACK',rid
            receipt={'request_id':rid,'accepted_slot_token':m.token,'payload_sha256':r['digest'],'duplicate':True}
            if not m.ack(m.revision,m.token,receipt):
                return 'INPUT_CHANGED',rid
            if not r['ack_proof']:
                m.dispatch='REJECTED'
            return state,rid
        if state=='ACK_PENDING' and rid:
            return self.acknowledge(rid,m,lost_response=lost_response),rid
        return state,rid

    def claim(self) -> str | None:
        if not self.live or self.blocked_cleanup:
            return None
        self.db.execute('BEGIN IMMEDIATE')
        try:
            active=self.db.execute("SELECT id FROM operations WHERE state IN ('STARTING','RUNNING')").fetchone()
            r=self.db.execute("SELECT id FROM operations WHERE state='QUEUED' AND session=? ORDER BY sequence LIMIT 1",(self.session,)).fetchone()
            if active or r is None:
                self.db.execute('COMMIT');return None
            rid=r[0]
            self.db.execute("UPDATE operations SET state='STARTING' WHERE id=?",(rid,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        self.event('CLAIMED',request_id=rid)
        return rid

    def start(self,current_source:dict[str,str],*,source_available:bool=True) -> str | None:
        rid=self.claim()
        if rid is None:return None
        r=self.row(rid);payload=json.loads(r['payload'])
        if not source_available or payload['branch'] not in current_source:
            self.db.execute("UPDATE operations SET state='BLOCKED',outcome='SOURCE_UNAVAILABLE' WHERE id=?",(rid,))
            self.event('SOURCE_BLOCKED',request_id=rid);return rid
        self.db.execute("UPDATE operations SET source_version=?,invocations=invocations+1,state='RUNNING' WHERE id=?",(current_source[payload['branch']],rid))
        self.event('STARTED',request_id=rid,source=current_source[payload['branch']])
        return rid

    def finish(self,rid:str,*,passed:bool=True,publish:bool=True,cleanup:bool=True,local_write:bool=True) -> None:
        r=self.row(rid)
        if r['state']!='RUNNING':raise ValueError('Not running')
        outcome='PASSED' if passed else 'FAILED'
        state='COMPLETED' if local_write else 'FAULT'
        delivery=('DELIVERED' if publish else 'LOCAL_ONLY') if local_write else 'LOCAL_PERSISTENCE_FAILED'
        self.db.execute('UPDATE operations SET state=?,outcome=?,delivery=?,local_saved=? WHERE id=?',(state,outcome,delivery,int(local_write),rid))
        self.blocked_cleanup=not cleanup
        self.event('FINISHED',request_id=rid,outcome=outcome,delivery=delivery,cleanup=cleanup)

    def cancel(self,rid:str) -> str:
        r=self.row(rid)
        if r['state'] in {'ACK_PENDING','ACK_UNKNOWN','QUEUED','STARTING','RUNNING'}:
            self.db.execute("UPDATE operations SET state='CANCELLED',outcome='CANCELLED' WHERE id=?",(rid,))
            self.event('CANCELLED',request_id=rid)
            return 'CANCELLED'
        return 'UNCHANGED'

    def redeliver(self,rid:str,*,available:bool=True) -> str:
        r=self.row(rid)
        if not r['local_saved']:return 'EVIDENCE_MISSING'
        state='DELIVERED' if available else 'LOCAL_ONLY'
        self.db.execute('UPDATE operations SET delivery=? WHERE id=?',(state,rid))
        self.event('DELIVERY_ONLY',request_id=rid,status=state)
        return state

    def invariants(self) -> None:
        rows=[dict(r) for r in self.db.execute('SELECT * FROM operations')]
        assert sum(r['state'] in {'STARTING','RUNNING'} for r in rows)<=1
        for r in rows:
            assert r['invocations']<=1
            if r['state'] in {'QUEUED','STARTING','RUNNING'}:
                assert r['ack_proof']==1 and r['sequence'] is not None
            if r['invocations']:
                assert r['source_version'] is not None
            if r['delivery']=='DELIVERED':assert r['local_saved']==1
        sequences=[r['sequence'] for r in rows if r['sequence'] is not None]
        assert len(sequences)==len(set(sequences))

    def close(self) -> None:
        self.db.close()

def quality_gate(*,exit_code:int,executed:int,failed:int,report_valid:bool=True,required_output:bool=True,test_required:bool=True) -> str:
    if not required_output or exit_code!=0:return 'FAILED'
    if test_required and (not report_valid or executed<=0):return 'INSUFFICIENT'
    if failed:return 'FAILED'
    return 'PASSED'

def artifact_availability(size:int,*,budget:int=10*1024*1024,provider_limit:int=100*1024*1024,publication_ok:bool=True) -> tuple[str,str|None]:
    if size<0:raise ValueError('Negative size')
    if size>provider_limit:return 'LOCAL_ONLY','PROVIDER_SIZE_LIMIT'
    if size>budget:return 'LOCAL_ONLY','POLICY_SIZE_LIMIT'
    if not publication_ok:return 'LOCAL_ONLY','PUBLICATION_FAILED'
    return 'PUBLISHED',None

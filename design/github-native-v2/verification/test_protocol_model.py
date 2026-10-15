"""Executable examples of the design, not tests of installed Runner code."""
import itertools
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from protocol_model import FakeMailbox,Model,quality_gate,artifact_availability,digest

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.m=Model();self.slot=FakeMailbox('github');self.doc=FakeMailbox('docs');self.m.startup([self.slot,self.doc])
    def tearDown(self):self.m.invariants();self.m.close()
    def send(self,n='a',slot=None):
        s=slot or self.slot;s.change({'action':'test','branch':'main','request_id':n});return self.m.submit(s)[1]
    def test_draft_does_not_run(self):self.assertEqual(self.m.submit(self.slot),('IGNORED',None))
    def test_ack_precedes_queue(self):
        self.slot.change();state,rid=self.m.reserve(self.slot);self.assertEqual(state,'ACK_PENDING');self.assertIsNone(self.m.start({'main':'A'}));self.assertEqual(self.m.acknowledge(rid,self.slot),'QUEUED')
    def test_one_lane_fifo(self):
        a=self.send('a');b=self.send('b');self.assertEqual(self.m.start({'main':'A'}),a);self.assertIsNone(self.m.start({'main':'B'}));self.m.finish(a);self.assertEqual(self.m.start({'main':'B'}),b)
    def test_queue_resolves_current_at_start(self):
        a=self.send('a');self.m.start({'main':'A'});b=self.send('b');self.m.finish(a);self.m.start({'main':'C'});self.assertEqual(self.m.row(b)['source_version'],'C')
    def test_running_source_is_frozen(self):
        a=self.send();src={'main':'A'};self.m.start(src);src['main']='B';self.assertEqual(self.m.row(a)['source_version'],'A')
    def test_current_unavailable_not_cached(self):
        a=self.send();self.m.start({'main':'old'},source_available=False);self.assertEqual(self.m.row(a)['invocations'],0);self.assertEqual(self.m.row(a)['outcome'],'SOURCE_UNAVAILABLE')
    def test_missing_branch(self):
        a=self.send();self.m.start({});self.assertEqual(self.m.row(a)['state'],'BLOCKED')
    def test_duplicate_id_same_payload(self):
        a=self.send();self.slot.change({'action':'test','branch':'main','request_id':'a'});self.assertEqual(self.m.submit(self.slot)[0],'DUPLICATE');self.assertEqual(self.m.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],1)
    def test_duplicate_cross_transport(self):
        a=self.send();self.doc.change({'action':'test','branch':'main','request_id':'a'});self.assertEqual(self.m.submit(self.doc),('DUPLICATE',a))
    def test_changed_id_rejected(self):
        self.send();self.slot.change({'action':'build','branch':'main','request_id':'a'});self.assertEqual(self.m.submit(self.slot)[0],'REQUEST_ID_REUSED')
    def test_human_id_generated_and_rotates(self):
        self.slot.change();_,a=self.m.submit(self.slot);self.slot.change();_,b=self.m.submit(self.slot);self.assertNotEqual(a,b)
    def test_repeat_poll_accepted_no_job(self):
        self.send();self.assertEqual(self.m.submit(self.slot),('IGNORED',None))
    def test_ready_changed_before_ack(self):
        self.slot.change();_,a=self.m.reserve(self.slot);self.slot.corrupt_ready();self.assertEqual(self.m.acknowledge(a,self.slot),'INPUT_CHANGED');self.assertIsNone(self.m.start({'main':'A'}))
    def test_ack_lost_no_execute(self):
        self.slot.change();_,a=self.m.submit(self.slot,lost_response=True);self.assertEqual(self.m.row(a)['state'],'ACK_UNKNOWN');self.assertIsNone(self.m.start({'main':'A'}))
    def test_ack_lost_exact_readback(self):
        self.slot.change();_,a=self.m.submit(self.slot,lost_response=True);self.assertEqual(self.m.reconcile_ack(a,self.slot),'QUEUED')
    def test_ack_lost_no_proof_no_guess(self):
        self.slot.change();_,a=self.m.submit(self.slot,lost_response=True);self.slot.receipt=None;self.assertEqual(self.m.reconcile_ack(a,self.slot),'ACK_UNKNOWN')
    def test_ack_cannot_target_other_mailbox(self):
        self.slot.change();_,a=self.m.reserve(self.slot);self.assertEqual(self.m.acknowledge(a,self.doc),'WRONG_MAILBOX')
    def test_startup_ready_no_backlog(self):
        self.slot.change();self.m.startup([self.slot]);self.assertEqual(self.slot.dispatch,'NEEDS_RESUBMIT');self.assertEqual(self.m.submit(self.slot)[0],'IGNORED')
    def test_resubmit_after_startup_via_same_slot(self):
        self.slot.change();self.m.startup([self.slot]);self.slot.change();self.assertEqual(self.m.submit(self.slot)[0],'QUEUED')
    def test_queued_suspended_after_restart(self):
        a=self.send();self.m.startup([self.slot]);self.assertEqual(self.m.row(a)['state'],'SUSPENDED_RESTART');self.assertIsNone(self.m.start({'main':'B'}))
    def test_running_interrupted_never_rerun(self):
        a=self.send();self.m.start({'main':'A'});self.m.startup([self.slot]);self.assertEqual(self.m.row(a)['state'],'INTERRUPTED');self.assertEqual(self.m.row(a)['invocations'],1)
    def test_starting_crash_never_guesses_spawn(self):
        a=self.send();self.m.claim();self.m.startup([self.slot]);self.assertEqual(self.m.row(a)['state'],'INTERRUPTED');self.assertEqual(self.m.row(a)['invocations'],0)
    def test_offline_submit_ignored(self):
        self.m.live=False;self.slot.change();self.assertEqual(self.m.submit(self.slot)[0],'IGNORED')
    def test_gap_ready_needs_resubmit(self):
        self.slot.change();self.m.observation_gap(self.slot);self.assertEqual(self.slot.dispatch,'NEEDS_RESUBMIT')
    def test_accepted_queue_survives_provider_gap_same_session(self):
        a=self.send();self.m.observation_gap(self.doc);self.assertEqual(self.m.start({'main':'A'}),a)
    def test_wrong_runner(self):
        self.slot.runner='someone-else';self.slot.change();self.assertEqual(self.m.submit(self.slot)[0],'WRONG_RUNNER')
    def test_old_session_rejected(self):
        self.slot.change();self.slot.session=0;self.assertEqual(self.m.submit(self.slot)[0],'NEEDS_RESUBMIT')
    def test_queue_capacity_explicit(self):
        self.m.capacity=1;self.send();self.slot.change({'action':'test','branch':'main','request_id':'b'});self.assertEqual(self.m.submit(self.slot)[0],'QUEUE_FULL')
    def test_cancel_queued_out_of_band(self):
        a=self.send('a');b=self.send('b');self.m.start({'main':'A'});self.assertEqual(self.m.cancel(b),'CANCELLED');self.assertEqual(self.m.row(b)['invocations'],0)
    def test_cancel_running_out_of_band(self):
        a=self.send();self.m.start({'main':'A'});self.assertEqual(self.m.cancel(a),'CANCELLED')
    def test_cancel_terminal_no_rewrite(self):
        a=self.send();self.m.start({'main':'A'});self.m.finish(a);self.assertEqual(self.m.cancel(a),'UNCHANGED')
    def test_completion_never_overwrites_new_command(self):
        a=self.send();self.m.start({'main':'A'});self.slot.change({'action':'build','branch':'main'});rev=self.slot.revision;self.m.finish(a);self.assertEqual(self.slot.revision,rev);self.assertEqual(self.slot.dispatch,'READY')
    def test_test_failure_is_completed_operation(self):
        a=self.send();self.m.start({'main':'A'});self.m.finish(a,passed=False);self.assertEqual(self.m.row(a)['state'],'COMPLETED');self.assertEqual(self.m.row(a)['outcome'],'FAILED')
    def test_publish_failure_stays_local(self):
        a=self.send();self.m.start({'main':'A'});self.m.finish(a,publish=False);self.assertEqual(self.m.row(a)['delivery'],'LOCAL_ONLY');self.assertEqual(self.m.row(a)['local_saved'],1)
    def test_redeliver_no_new_invocation(self):
        a=self.send();self.m.start({'main':'A'});self.m.finish(a,publish=False);self.m.redeliver(a);self.assertEqual(self.m.row(a)['invocations'],1)
    def test_missing_evidence_cannot_redeliver(self):
        a=self.send();self.assertEqual(self.m.redeliver(a),'EVIDENCE_MISSING')
    def test_local_persistence_not_fabricated(self):
        a=self.send();self.m.start({'main':'A'});self.m.finish(a,local_write=False);self.assertEqual(self.m.row(a)['delivery'],'LOCAL_PERSISTENCE_FAILED')
    def test_uncertain_cleanup_blocks_lane(self):
        a=self.send('a');b=self.send('b');self.m.start({'main':'A'});self.m.finish(a,cleanup=False);self.assertIsNone(self.m.start({'main':'B'}))
    def test_sql_constraint_prevents_second_active_lane(self):
        a=self.send('a');b=self.send('b');self.m.start({'main':'A'});
        with self.assertRaises(sqlite3.IntegrityError):self.m.db.execute("UPDATE operations SET state='RUNNING' WHERE id=?",(b,))
    def test_queue_sequence_follows_acceptance_not_name(self):
        z=self.send('z');a=self.send('a');self.assertLess(self.m.row(z)['sequence'],self.m.row(a)['sequence'])
    def test_restart_blocks_new_lane_until_cleanup_proven(self):
        a=self.send();self.m.start({'main':'A'});self.m.startup([self.slot]);self.send('b');self.assertIsNone(self.m.start({'main':'B'}));self.assertTrue(self.m.blocked_cleanup)
    def test_cancel_unspawned_ack_unknown(self):
        self.slot.change();_,a=self.m.submit(self.slot,lost_response=True);self.assertEqual(self.m.cancel(a),'CANCELLED');self.assertEqual(self.m.row(a)['invocations'],0)
    def test_duplicate_clears_ready_without_second_job(self):
        self.send();self.slot.change({'action':'test','branch':'main','request_id':'a'});self.m.submit(self.slot);self.assertEqual(self.slot.dispatch,'ACCEPTED');self.assertEqual(self.m.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],1)
    def test_duplicate_pending_ack_never_false_accepted(self):
        self.slot.change({'action':'test','branch':'main','request_id':'a'});self.m.reserve(self.slot);self.doc.change({'action':'test','branch':'main','request_id':'a'});self.assertEqual(self.m.submit(self.doc)[0],'WAITING_ACK');self.assertEqual(self.doc.dispatch,'READY')
    def test_snapshot_not_bound_at_admission(self):
        a=self.send();self.assertIsNone(self.m.row(a)['source_version']);self.m.start({'main':'NEWEST'});self.assertEqual(self.m.row(a)['source_version'],'NEWEST')
    def test_invalid_schema_no_candidate(self):
        self.slot.change({'action':'nonsense'});self.assertEqual(self.m.submit(self.slot)[0],'INVALID_SCHEMA')
    def test_json_format_digest_stable(self):
        self.assertEqual(digest({'b':2,'a':1}),digest({'a':1,'b':2}))
    def test_script_bytes_not_semantically_normalized(self):
        self.assertNotEqual(digest({'script':'echo a\n'}),digest({'script':'echo a'}))

class ResultTests(unittest.TestCase):
    def test_zero_tests_not_pass(self):self.assertEqual(quality_gate(exit_code=0,executed=0,failed=0),'INSUFFICIENT')
    def test_all_skipped_not_pass(self):self.assertEqual(quality_gate(exit_code=0,executed=0,failed=0),'INSUFFICIENT')
    def test_bad_report_not_pass(self):self.assertEqual(quality_gate(exit_code=0,executed=3,failed=0,report_valid=False),'INSUFFICIENT')
    def test_missing_required_output(self):self.assertEqual(quality_gate(exit_code=0,executed=3,failed=0,required_output=False),'FAILED')
    def test_nonzero_exit(self):self.assertEqual(quality_gate(exit_code=1,executed=3,failed=0),'FAILED')
    def test_build_without_tests_valid(self):self.assertEqual(quality_gate(exit_code=0,executed=0,failed=0,test_required=False),'PASSED')
    def test_failed_test_exit_zero_not_pass(self):self.assertEqual(quality_gate(exit_code=0,executed=3,failed=1),'FAILED')
    def test_good_tests_pass(self):self.assertEqual(quality_gate(exit_code=0,executed=3,failed=0),'PASSED')
    def test_large_file_local(self):self.assertEqual(artifact_availability(101*1024*1024)[1],'PROVIDER_SIZE_LIMIT')
    def test_policy_limit_distinct(self):self.assertEqual(artifact_availability(20*1024*1024)[1],'POLICY_SIZE_LIMIT')
    def test_publication_unavailable(self):self.assertEqual(artifact_availability(10,publication_ok=False)[1],'PUBLICATION_FAILED')
    def test_fits_published(self):self.assertEqual(artifact_availability(10),('PUBLISHED',None))

class PersistenceAndOrderingTests(unittest.TestCase):
    def test_sqlite_records_survive_reopen_but_queue_does_not_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'model.sqlite');m=Model(path);s=FakeMailbox('github');m.startup([s]);s.change();_,r=m.submit(s);m.close();n=Model(path);n.startup([s]);self.assertEqual(n.row(r)['state'],'SUSPENDED_RESTART');self.assertIsNone(n.start({'main':'A'}));n.invariants();n.close()
    def test_120_event_permutations(self):
        for order in itertools.permutations(('send-a','send-b','start','finish','restart')):
            m=Model();s=FakeMailbox('github');m.startup([s]);active=None
            try:
                for e in order:
                    if e.startswith('send'):
                        s.change({'action':'test','branch':'main','request_id':e});m.submit(s)
                    elif e=='start':active=m.start({'main':'LATEST'})
                    elif e=='finish' and active and m.row(active)['state']=='RUNNING':m.finish(active)
                    elif e=='restart':m.startup([s])
                    m.invariants()
            finally:m.close()

if __name__=='__main__':unittest.main(verbosity=2)

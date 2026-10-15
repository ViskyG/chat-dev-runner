"""Additive v2 state in the existing database; v1 records remain unchanged."""

V2_MIGRATION = (
    "CREATE TABLE v2_installation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), installation_id TEXT NOT NULL UNIQUE)",
    "CREATE TABLE v2_sessions (session_id TEXT PRIMARY KEY, runner_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT)",
    "CREATE UNIQUE INDEX v2_one_live_session ON v2_sessions(runner_id) WHERE ended_at IS NULL",
    "CREATE TABLE v2_mailboxes (binding_id TEXT PRIMARY KEY, runner_id TEXT NOT NULL, project_id TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('github','drive')), coordinates_json TEXT NOT NULL, session_id TEXT REFERENCES v2_sessions(session_id), slot_token TEXT, observed_revision TEXT, continuous INTEGER NOT NULL DEFAULT 0 CHECK(continuous IN (0,1)), intake_code TEXT, diagnostic_sha256 TEXT, updated_at TEXT NOT NULL)",
    "CREATE TABLE v2_requests (request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload BLOB NOT NULL, runner_id TEXT NOT NULL, project_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('RUN_TASK','SHELL','CANCEL','STATUS','REDELIVER')), session_id TEXT NOT NULL REFERENCES v2_sessions(session_id), status TEXT NOT NULL CHECK(status IN ('ACK_PENDING','QUEUED','STARTING','RUNNING','CONTROL','CONTROL_RUNNING','COMPLETED','FAILED','CANCELLED','REJECTED','SUSPENDED_RESTART','INTERRUPTED','NEEDS_RECONCILIATION')), code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)), result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE v2_candidates (candidate_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES v2_mailboxes(binding_id), session_id TEXT NOT NULL REFERENCES v2_sessions(session_id), slot_token TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES v2_requests(request_id), observed_revision TEXT NOT NULL, observed_sha256 TEXT NOT NULL, ack_payload BLOB NOT NULL, ack_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ACK_PENDING','ACK_UNKNOWN','ACKED','ACK_FAILED','INPUT_CHANGED')), proof_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(binding_id,slot_token))",
    "CREATE TABLE v2_queue (sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE REFERENCES v2_requests(request_id), session_id TEXT NOT NULL REFERENCES v2_sessions(session_id), finalized_at TEXT NOT NULL)",
    "CREATE TABLE execution_lane (singleton INTEGER PRIMARY KEY CHECK(singleton=1), protocol TEXT NOT NULL CHECK(protocol IN ('v1','v2')), request_id TEXT NOT NULL UNIQUE, claimed_at TEXT NOT NULL)",
    "CREATE TABLE v2_journal (event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT REFERENCES v2_requests(request_id), event TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX v2_candidates_by_request ON v2_candidates(request_id,state)",
    "CREATE INDEX v2_requests_by_state ON v2_requests(session_id,status)",
    "CREATE TRIGGER v2_request_intent_frozen BEFORE UPDATE OF request_id,digest,payload,runner_id,project_id,action,session_id,created_at ON v2_requests BEGIN SELECT RAISE(ABORT,'v2 request intent is immutable'); END",
    "CREATE TRIGGER v2_request_no_delete BEFORE DELETE ON v2_requests BEGIN SELECT RAISE(ABORT,'v2 request history is immutable'); END",
    "CREATE TRIGGER v2_candidate_intent_frozen BEFORE UPDATE OF candidate_id,binding_id,session_id,slot_token,request_id,observed_revision,observed_sha256,ack_payload,ack_sha256,created_at ON v2_candidates BEGIN SELECT RAISE(ABORT,'v2 candidate intent is immutable'); END",
    "CREATE TRIGGER v2_candidate_no_delete BEFORE DELETE ON v2_candidates BEGIN SELECT RAISE(ABORT,'v2 candidate history is immutable'); END",
    "CREATE TRIGGER v2_queue_no_update BEFORE UPDATE ON v2_queue BEGIN SELECT RAISE(ABORT,'v2 sequence is immutable'); END",
    "CREATE TRIGGER v2_queue_no_delete BEFORE DELETE ON v2_queue BEGIN SELECT RAISE(ABORT,'v2 sequence history is immutable'); END",
    "CREATE TRIGGER v2_journal_no_update BEFORE UPDATE ON v2_journal BEGIN SELECT RAISE(ABORT,'v2 journal is append-only'); END",
    "CREATE TRIGGER v2_journal_no_delete BEFORE DELETE ON v2_journal BEGIN SELECT RAISE(ABORT,'v2 journal is append-only'); END",
    "INSERT INTO schema_migrations(version,name) VALUES(10,'live-session-intake-and-single-lane')",
)

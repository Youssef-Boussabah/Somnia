-- Somnia migration 0001: foundation learning store.
-- Learner-side store only. The sealed final evaluation store is NOT part of this
-- schema and must live in a separately permissioned database/location.
--
-- Custom SQLSTATEs raised here (mapped in somnia/errors.py):
--   SM001 immutable record mutation       SB001 budget exceeded
--   SB002 invalid reservation transition  SG001 gate/candidate hash mismatch
--   SP001 partition/scope violation

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS somnia;
SET search_path = somnia, public;

-- ---------------------------------------------------------------- helpers
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable record: % on %.% is not permitted', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'SM001';
END $$;

-- Attach row-level UPDATE/DELETE and statement-level TRUNCATE guards.
CREATE FUNCTION make_immutable(tbl regclass) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION somnia.forbid_mutation()',
                 'immutable_row', tbl);
  EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION somnia.forbid_mutation()',
                 'immutable_truncate', tbl);
END $$;

-- ---------------------------------------------------------------- projects, artifacts, sessions, events
CREATE TABLE projects (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE artifacts (
  sha256       text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  media_type   text NOT NULL,
  byte_size    bigint NOT NULL CHECK (byte_size >= 0),
  storage_uri  text NOT NULL,                       -- relative to the artifact root
  sensitivity  text NOT NULL CHECK (sensitivity IN ('public', 'internal', 'sensitive')),
  origin       text NOT NULL CHECK (origin IN ('observed', 'synthetic', 'mock', 'derived')),
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('artifacts');

-- 'final' is deliberately not representable: sealed final material never enters the learner store.
CREATE TABLE sessions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id         uuid NOT NULL REFERENCES projects(id),
  partition          text NOT NULL CHECK (partition IN ('development', 'validation', 'pilot')),
  origin             text NOT NULL CHECK (origin IN ('observed', 'synthetic', 'mock')),
  purpose            text NOT NULL CHECK (purpose IN ('experience', 'retrieval')),
  agent_config_hash  text NOT NULL,
  label              text,
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE events (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id              uuid NOT NULL REFERENCES projects(id),
  session_id              uuid NOT NULL REFERENCES sessions(id),
  seq                     integer NOT NULL CHECK (seq >= 1),
  event_type              text NOT NULL,
  payload_schema_version  smallint NOT NULL DEFAULT 1,
  payload                 jsonb NOT NULL,
  artifact_sha256         text REFERENCES artifacts(sha256),
  origin                  text NOT NULL CHECK (origin IN ('observed', 'synthetic', 'mock')),
  redaction_status        text NOT NULL DEFAULT 'none' CHECK (redaction_status IN ('none', 'redacted')),
  idempotency_key         text NOT NULL,
  payload_hash            text NOT NULL,
  prev_hash               text,
  event_hash              text NOT NULL UNIQUE,
  created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (session_id, seq),
  UNIQUE (session_id, idempotency_key),
  CHECK ((seq = 1) = (prev_hash IS NULL))
);
SELECT make_immutable('events');

-- ---------------------------------------------------------------- skills and skill sets
CREATE TABLE embedding_specs (
  id          text PRIMARY KEY,
  provider    text NOT NULL,
  model       text NOT NULL,
  revision    text NOT NULL,
  dims        integer NOT NULL CHECK (dims > 0),
  normalized  boolean NOT NULL,
  is_mock     boolean NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('embedding_specs');

CREATE TABLE skills (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES projects(id),
  slug        text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9_.-]{0,94}$'),
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (project_id, slug)
);
SELECT make_immutable('skills');

CREATE TABLE skill_versions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id           uuid NOT NULL REFERENCES skills(id),
  version            integer NOT NULL CHECK (version >= 1),
  parent_version_id  uuid REFERENCES skill_versions(id),
  title              text NOT NULL,
  procedure          text NOT NULL,
  applicability      jsonb NOT NULL,
  exclusions         jsonb NOT NULL,
  prerequisites      jsonb NOT NULL,
  known_limits       text NOT NULL,
  content_hash       text NOT NULL,
  token_length       integer NOT NULL CHECK (token_length > 0),
  origin             text NOT NULL CHECK (origin IN ('mock', 'model', 'user')),
  created_by_run_id  uuid,                         -- FK added after sleep_runs
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (skill_id, version),
  CHECK (parent_version_id IS DISTINCT FROM id)
);
SELECT make_immutable('skill_versions');

CREATE TABLE skill_sets (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id                uuid NOT NULL REFERENCES projects(id),
  version                   integer NOT NULL CHECK (version >= 0),
  predecessor_id            uuid REFERENCES skill_sets(id),
  content_hash              text NOT NULL,
  contains_mock_validation  boolean NOT NULL,
  created_by                text NOT NULL,
  reason                    text NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (project_id, version),
  CHECK ((version = 0) = (predecessor_id IS NULL))
);
SELECT make_immutable('skill_sets');

CREATE TABLE gate_decisions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_version_id      uuid NOT NULL REFERENCES skill_versions(id),
  candidate_hash        text NOT NULL,
  baseline_skill_set_id uuid NOT NULL REFERENCES skill_sets(id),
  validator             text NOT NULL,
  validator_version     text NOT NULL,
  suite_id              text NOT NULL,
  suite_hash            text NOT NULL,
  independent           boolean NOT NULL,
  is_mock               boolean NOT NULL,
  decision              text NOT NULL CHECK (decision IN ('pass', 'fail', 'inconclusive')),
  reason                text NOT NULL,
  paired_scores         jsonb NOT NULL,
  evidence_refs         jsonb NOT NULL,
  run_id                uuid,                      -- FK added after sleep_runs
  created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (NOT (is_mock AND independent))           -- a mock gate can never claim independence
);
SELECT make_immutable('gate_decisions');

CREATE FUNCTION gate_check_candidate_hash() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE h text;
BEGIN
  SELECT content_hash INTO h FROM somnia.skill_versions WHERE id = NEW.skill_version_id;
  IF h IS DISTINCT FROM NEW.candidate_hash THEN
    RAISE EXCEPTION 'gate decision candidate_hash % does not match skill version hash %', NEW.candidate_hash, h
      USING ERRCODE = 'SG001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER gate_candidate_hash BEFORE INSERT ON gate_decisions
  FOR EACH ROW EXECUTE FUNCTION gate_check_candidate_hash();

CREATE TABLE skill_set_members (
  skill_set_id      uuid NOT NULL REFERENCES skill_sets(id),
  skill_id          uuid NOT NULL REFERENCES skills(id),
  skill_version_id  uuid NOT NULL REFERENCES skill_versions(id),
  gate_decision_id  uuid NOT NULL REFERENCES gate_decisions(id),
  PRIMARY KEY (skill_set_id, skill_id)            -- at most one version of a skill per set
);
SELECT make_immutable('skill_set_members');
CREATE INDEX skill_set_members_version ON skill_set_members (skill_version_id);

CREATE TABLE skill_embeddings (
  skill_version_id  uuid NOT NULL REFERENCES skill_versions(id),
  spec_id           text NOT NULL REFERENCES embedding_specs(id),
  dims              integer NOT NULL,
  embedding         vector NOT NULL,
  source_hash       text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (skill_version_id, spec_id),
  CHECK (vector_dims(embedding) = dims)
);
SELECT make_immutable('skill_embeddings');

-- ---------------------------------------------------------------- snapshots
CREATE TABLE snapshots (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id         uuid NOT NULL REFERENCES projects(id),
  content_hash       text NOT NULL,
  base_skill_set_id  uuid NOT NULL REFERENCES skill_sets(id),
  event_count        integer NOT NULL CHECK (event_count > 0),
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (project_id, content_hash)
);
SELECT make_immutable('snapshots');

CREATE TABLE snapshot_events (
  snapshot_id  uuid NOT NULL REFERENCES snapshots(id),
  event_id     uuid NOT NULL REFERENCES events(id),
  event_hash   text NOT NULL,
  PRIMARY KEY (snapshot_id, event_id)
);
SELECT make_immutable('snapshot_events');

-- Learning snapshots may only contain development-partition experience.
CREATE FUNCTION snapshot_event_partition_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p text;
BEGIN
  SELECT s.partition INTO p FROM somnia.events e JOIN somnia.sessions s ON s.id = e.session_id
   WHERE e.id = NEW.event_id;
  IF p IS DISTINCT FROM 'development' THEN
    RAISE EXCEPTION 'snapshot may only include development-partition events (got %)', p USING ERRCODE = 'SP001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER snapshot_event_partition BEFORE INSERT ON snapshot_events
  FOR EACH ROW EXECUTE FUNCTION snapshot_event_partition_check();

-- ---------------------------------------------------------------- budget ledger
CREATE TABLE budget_envelopes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id       uuid NOT NULL REFERENCES projects(id),
  parent_id        uuid REFERENCES budget_envelopes(id),
  name             text NOT NULL,
  unit             text NOT NULL CHECK (unit IN ('CAD_MICRO', 'MOCK_MICRO')),
  limit_micro      bigint NOT NULL CHECK (limit_micro >= 0),
  reserved_micro   bigint NOT NULL DEFAULT 0 CHECK (reserved_micro >= 0),   -- reserved + uncertain
  confirmed_micro  bigint NOT NULL DEFAULT 0 CHECK (confirmed_micro >= 0),
  created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (project_id, name)
);

CREATE FUNCTION envelope_parent_unit_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pu text; pp uuid;
BEGIN
  IF NEW.parent_id IS NOT NULL THEN
    SELECT unit, project_id INTO pu, pp FROM somnia.budget_envelopes WHERE id = NEW.parent_id;
    IF pu IS DISTINCT FROM NEW.unit OR pp IS DISTINCT FROM NEW.project_id THEN
      RAISE EXCEPTION 'child envelope must share unit and project with parent' USING ERRCODE = 'SB002';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.parent_id IS DISTINCT FROM OLD.parent_id OR NEW.unit <> OLD.unit
                           OR NEW.limit_micro <> OLD.limit_micro OR NEW.project_id <> OLD.project_id) THEN
    RAISE EXCEPTION 'envelope identity, unit and limit are immutable' USING ERRCODE = 'SB002';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER envelope_parent_unit BEFORE INSERT OR UPDATE ON budget_envelopes
  FOR EACH ROW EXECUTE FUNCTION envelope_parent_unit_check();
CREATE TRIGGER envelope_no_delete BEFORE DELETE ON budget_envelopes
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- sleep runs and stage attempts
CREATE TABLE sleep_runs (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id             uuid NOT NULL REFERENCES projects(id),
  snapshot_id            uuid NOT NULL REFERENCES snapshots(id),
  envelope_id            uuid NOT NULL REFERENCES budget_envelopes(id),
  config_schema_version  smallint NOT NULL,
  config                 jsonb NOT NULL,
  config_hash            text NOT NULL,
  idempotency_key        text NOT NULL UNIQUE,
  status                 text NOT NULL CHECK (status IN ('pending', 'running', 'blocked', 'completed', 'failed')),
  current_stage          text NOT NULL,
  fence                  bigint NOT NULL DEFAULT 0,  -- monotonically increasing lease token
  lease_owner            text,
  lease_expires_at       timestamptz,
  blocked_reason         text,
  created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at            timestamptz
);

CREATE FUNCTION sleep_run_fence_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fence < OLD.fence THEN
    RAISE EXCEPTION 'lease fence may not decrease (% -> %)', OLD.fence, NEW.fence USING ERRCODE = 'SM001';
  END IF;
  IF NEW.snapshot_id <> OLD.snapshot_id OR NEW.config_hash <> OLD.config_hash OR NEW.envelope_id <> OLD.envelope_id THEN
    RAISE EXCEPTION 'run inputs are immutable' USING ERRCODE = 'SM001';
  END IF;
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END $$;
CREATE TRIGGER sleep_run_guard BEFORE UPDATE ON sleep_runs
  FOR EACH ROW EXECUTE FUNCTION sleep_run_fence_monotonic();

ALTER TABLE skill_versions ADD FOREIGN KEY (created_by_run_id) REFERENCES sleep_runs(id);
ALTER TABLE gate_decisions ADD FOREIGN KEY (run_id) REFERENCES sleep_runs(id);

CREATE TABLE stage_attempts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid NOT NULL REFERENCES sleep_runs(id),
  stage        text NOT NULL,
  fence        bigint NOT NULL,
  worker_id    text NOT NULL,
  status       text NOT NULL CHECK (status IN ('running', 'succeeded', 'abandoned', 'failed')),
  input_hash   text NOT NULL,
  output       jsonb,
  output_hash  text,
  error        text,
  started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at  timestamptz,
  UNIQUE (run_id, stage, fence)
);
CREATE UNIQUE INDEX stage_attempts_one_success ON stage_attempts (run_id, stage) WHERE status = 'succeeded';

-- ---------------------------------------------------------------- reservations
CREATE TABLE reservations (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  envelope_id       uuid NOT NULL REFERENCES budget_envelopes(id),
  idempotency_key   text NOT NULL UNIQUE,
  run_id            uuid REFERENCES sleep_runs(id),
  stage_attempt_id  uuid REFERENCES stage_attempts(id),
  purpose           text NOT NULL,
  provider          text NOT NULL,
  model             text NOT NULL,
  amount_micro      bigint NOT NULL CHECK (amount_micro > 0),
  pricing_snapshot  jsonb NOT NULL,
  state             text NOT NULL CHECK (state IN ('reserved', 'uncertain', 'settled', 'released')),
  actual_micro      bigint CHECK (actual_micro >= 0),
  request_id        text,
  resolution_note   text,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX reservations_run_state ON reservations (run_id, state);
CREATE TRIGGER reservation_no_delete BEFORE DELETE OR TRUNCATE ON reservations
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TABLE reservation_transitions (
  id              bigserial PRIMARY KEY,
  reservation_id  uuid NOT NULL REFERENCES reservations(id),
  from_state      text,
  to_state        text NOT NULL,
  amount_micro    bigint NOT NULL,
  actual_micro    bigint,
  note            text,
  at              timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('reservation_transitions');

-- Reserve against the leaf envelope and every ancestor atomically. The guarded UPDATE
-- re-evaluates its WHERE clause after acquiring the row lock, so concurrent reservations
-- serialize per envelope and can never exceed the limit.
CREATE FUNCTION reservation_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cur uuid := NEW.envelope_id; nxt uuid; n int;
BEGIN
  IF NEW.state <> 'reserved' OR NEW.actual_micro IS NOT NULL THEN
    RAISE EXCEPTION 'a new reservation must start in state reserved without actual cost' USING ERRCODE = 'SB002';
  END IF;
  WHILE cur IS NOT NULL LOOP
    UPDATE somnia.budget_envelopes
       SET reserved_micro = reserved_micro + NEW.amount_micro
     WHERE id = cur AND reserved_micro + confirmed_micro + NEW.amount_micro <= limit_micro
     RETURNING parent_id INTO nxt;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n = 0 THEN
      RAISE EXCEPTION 'budget exceeded: reservation of % on envelope % does not fit', NEW.amount_micro, cur
        USING ERRCODE = 'SB001';
    END IF;
    cur := nxt;
  END LOOP;
  INSERT INTO somnia.reservation_transitions (reservation_id, from_state, to_state, amount_micro)
       VALUES (NEW.id, NULL, 'reserved', NEW.amount_micro);
  RETURN NULL;
END $$;
CREATE TRIGGER reservation_insert AFTER INSERT ON reservations
  FOR EACH ROW EXECUTE FUNCTION reservation_after_insert();

-- Allowed: reserved -> settled | released | uncertain ; uncertain -> settled.
-- An uncertain reservation can never be released: it must be reconciled with an actual cost (possibly 0).
CREATE FUNCTION reservation_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.envelope_id <> OLD.envelope_id OR NEW.amount_micro <> OLD.amount_micro
     OR NEW.idempotency_key <> OLD.idempotency_key OR NEW.created_at <> OLD.created_at
     OR NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.pricing_snapshot <> OLD.pricing_snapshot THEN
    RAISE EXCEPTION 'reservation identity and amount are immutable' USING ERRCODE = 'SB002';
  END IF;
  IF NOT ((OLD.state = 'reserved' AND NEW.state IN ('settled', 'released', 'uncertain'))
          OR (OLD.state = 'uncertain' AND NEW.state = 'settled')) THEN
    RAISE EXCEPTION 'invalid reservation transition % -> %', OLD.state, NEW.state USING ERRCODE = 'SB002';
  END IF;
  IF (NEW.state = 'settled') <> (NEW.actual_micro IS NOT NULL) THEN
    RAISE EXCEPTION 'actual_micro must be set exactly when settling' USING ERRCODE = 'SB002';
  END IF;
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END $$;
CREATE TRIGGER reservation_update_guard BEFORE UPDATE ON reservations
  FOR EACH ROW EXECUTE FUNCTION reservation_before_update();

-- Settled/released reservations free their hold; settled cost is always recorded, even if it
-- overruns the limit (money already spent is a fact). An overrun blocks all later reservations.
CREATE FUNCTION reservation_after_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cur uuid := NEW.envelope_id; nxt uuid;
BEGIN
  IF NEW.state IN ('settled', 'released') THEN
    WHILE cur IS NOT NULL LOOP
      UPDATE somnia.budget_envelopes
         SET reserved_micro = reserved_micro - OLD.amount_micro,
             confirmed_micro = confirmed_micro + COALESCE(NEW.actual_micro, 0)
       WHERE id = cur
       RETURNING parent_id INTO nxt;
      cur := nxt;
    END LOOP;
  END IF;
  INSERT INTO somnia.reservation_transitions (reservation_id, from_state, to_state, amount_micro, actual_micro, note)
       VALUES (NEW.id, OLD.state, NEW.state, NEW.amount_micro, NEW.actual_micro, NEW.resolution_note);
  RETURN NULL;
END $$;
CREATE TRIGGER reservation_update_apply AFTER UPDATE ON reservations
  FOR EACH ROW EXECUTE FUNCTION reservation_after_update();

CREATE TABLE provider_calls (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id      uuid NOT NULL UNIQUE REFERENCES reservations(id),
  run_id              uuid REFERENCES sleep_runs(id),
  stage_attempt_id    uuid REFERENCES stage_attempts(id),
  request_key         text NOT NULL,     -- deterministic hash of (run, stage, template, model, prompt)
  provider            text NOT NULL,
  model               text NOT NULL,
  is_mock             boolean NOT NULL,
  prompt_artifact     text NOT NULL REFERENCES artifacts(sha256),
  response_artifact   text NOT NULL REFERENCES artifacts(sha256),
  input_tokens        integer NOT NULL,
  output_tokens       integer NOT NULL,
  request_id          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('provider_calls');
CREATE INDEX provider_calls_request_key ON provider_calls (run_id, request_key);

-- ---------------------------------------------------------------- learning outputs (immutable)
CREATE TABLE hypotheses (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id            uuid NOT NULL REFERENCES sleep_runs(id),
  stage_attempt_id  uuid NOT NULL REFERENCES stage_attempts(id),
  ordinal           integer NOT NULL,
  claim             text NOT NULL,
  mechanism         text NOT NULL,
  falsifier         text NOT NULL,
  provider_call_id  uuid REFERENCES provider_calls(id),
  origin            text NOT NULL CHECK (origin IN ('mock', 'model')),
  content_hash      text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (run_id, ordinal)
);
SELECT make_immutable('hypotheses');

CREATE TABLE hypothesis_sources (
  hypothesis_id  uuid NOT NULL REFERENCES hypotheses(id),
  event_id       uuid NOT NULL REFERENCES events(id),
  PRIMARY KEY (hypothesis_id, event_id)
);
SELECT make_immutable('hypothesis_sources');

CREATE TABLE scenarios (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id           uuid NOT NULL REFERENCES sleep_runs(id),
  hypothesis_id    uuid NOT NULL REFERENCES hypotheses(id),
  ordinal          integer NOT NULL,
  spec             jsonb NOT NULL,
  spec_hash        text NOT NULL,
  validity         text NOT NULL CHECK (validity IN ('valid', 'invalid', 'inconclusive')),
  validity_reason  text NOT NULL,
  origin           text NOT NULL CHECK (origin IN ('mock', 'model')),
  created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (run_id, ordinal)
);
SELECT make_immutable('scenarios');

CREATE TABLE experiment_runs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid NOT NULL REFERENCES sleep_runs(id),
  scenario_id  uuid NOT NULL REFERENCES scenarios(id),
  attempt_no   integer NOT NULL CHECK (attempt_no >= 1),
  executor     text NOT NULL,
  is_mock      boolean NOT NULL,
  outcome      text NOT NULL CHECK (outcome IN ('fault_detected', 'no_fault_detected', 'task_failure', 'invalid_scenario',
                                                 'agent_timeout', 'infrastructure_failure', 'safety_violation')),
  solution_hash text,
  diagnostics  jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (scenario_id, attempt_no)
);
SELECT make_immutable('experiment_runs');

CREATE TABLE skill_version_evidence (
  id                 bigserial PRIMARY KEY,
  skill_version_id   uuid NOT NULL REFERENCES skill_versions(id),
  role               text NOT NULL CHECK (role IN ('supporting', 'contradicting')),
  event_id           uuid REFERENCES events(id),
  hypothesis_id      uuid REFERENCES hypotheses(id),
  experiment_run_id  uuid REFERENCES experiment_runs(id),
  provider_call_id   uuid REFERENCES provider_calls(id),
  CHECK (num_nonnulls(event_id, hypothesis_id, experiment_run_id, provider_call_id) = 1)
);
SELECT make_immutable('skill_version_evidence');

-- ---------------------------------------------------------------- activation (pointer + audit)
CREATE TABLE activations (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id                uuid NOT NULL REFERENCES projects(id),
  idempotency_key           text NOT NULL UNIQUE,
  kind                      text NOT NULL CHECK (kind IN ('genesis', 'activate', 'rollback')),
  gate_decision_id          uuid REFERENCES gate_decisions(id),
  skill_version_id          uuid REFERENCES skill_versions(id),
  expected_previous_set_id  uuid REFERENCES skill_sets(id),
  previous_set_id           uuid REFERENCES skill_sets(id),
  new_set_id                uuid NOT NULL REFERENCES skill_sets(id),
  actor                     text NOT NULL,
  reason                    text NOT NULL,
  run_id                    uuid REFERENCES sleep_runs(id),
  created_at                timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (kind <> 'activate' OR (gate_decision_id IS NOT NULL AND skill_version_id IS NOT NULL))
);
SELECT make_immutable('activations');

CREATE TABLE active_skill_sets (
  project_id     uuid PRIMARY KEY REFERENCES projects(id),
  skill_set_id   uuid NOT NULL REFERENCES skill_sets(id),
  activation_id  uuid NOT NULL REFERENCES activations(id),
  updated_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE audit_events (
  id              bigserial PRIMARY KEY,
  project_id      uuid NOT NULL REFERENCES projects(id),
  actor           text NOT NULL,
  action          text NOT NULL,
  target          jsonb NOT NULL,
  reason          text NOT NULL,
  correlation_id  text,
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('audit_events');

-- ---------------------------------------------------------------- retrieval log
CREATE TABLE retrieval_logs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id    uuid NOT NULL REFERENCES projects(id),
  session_id    uuid NOT NULL REFERENCES sessions(id),
  skill_set_id  uuid NOT NULL REFERENCES skill_sets(id),
  request       jsonb NOT NULL,
  config_hash   text NOT NULL,
  eligible      jsonb NOT NULL,
  selected      jsonb NOT NULL,
  excluded      jsonb NOT NULL,
  token_count   integer NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('retrieval_logs');

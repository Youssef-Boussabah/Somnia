-- Somnia migration 0003: provider request lifecycle, send permits, residual holds and billing discrepancies.
-- (Repair of review finding R1 and the Codex follow-up on D044.) Forward-only. New SQLSTATEs (somnia/errors.py):
--   SB003 budget frozen by an open billing discrepancy in the envelope tree
--   SR001 invalid request resolution / evidence record, or unresolved legacy holds at upgrade
--
-- Principles (DECISIONS.md D044, D046; BUDGET.md):
--  * Billing reconciliation (what a hold cost) and request completion (whether a response exists) are separate
--    facts. An operator-entered amount, including zero, never by itself authorizes another send.
--  * Every decision about one LOGICAL request (run, request key) -- reuse / block / reserve, recording a
--    response, granting a send permit, abandoning -- runs under the same transaction-scoped lock
--    (lock_logical_request), so they cannot interleave.
--  * A sender must hold a fenced send permit before contacting the provider. Without a permit nothing was sent,
--    and a sender that was fenced out can never obtain one.
--  * While a reconciled request is still outstanding, its remaining possible charge (bound - operator amount)
--    stays held on the leaf and every ancestor (residual hold) until the response is recorded or it is abandoned.
--  * (D048) A legacy attempt is resolved only by its own evidence, never by a sibling attempt's receipt. A late
--    response can be recorded by an operator even when the prompt was never stored (the prompt stays unrecorded;
--    such responses are never reused as model output). Abandonment and reconciliation serialize on the hold's row
--    lock. A provider error after a takeover ends the send permit with its outcome and leaves billing untouched.

SET search_path = somnia, public;

-- ---------------------------------------------------------------- upgrade precondition (checked before anything is installed)
DO $$
DECLARE ids text; n int;
BEGIN
  SELECT count(*), string_agg(r.id::text || ' (' || r.state || ', run ' || r.run_id::text || ')', ', ')
    INTO n, ids
    FROM (SELECT r.* FROM somnia.reservations r
           WHERE r.run_id IS NOT NULL AND r.stage_attempt_id IS NOT NULL AND r.idempotency_key ~ '^[0-9a-f]{64}:'
             AND r.state IN ('reserved', 'uncertain')
             AND NOT EXISTS (SELECT 1 FROM somnia.provider_calls c WHERE c.reservation_id = r.id)
           ORDER BY r.created_at LIMIT 20) r;
  IF n > 0 THEN
    RAISE EXCEPTION 'cannot apply 0003: % unresolved pre-0003 gateway hold(s) without a recorded response: %. '
      'Their sender may still be running pre-0003 code, which records responses differently. To upgrade: '
      '(1) stop every `somnia run work` process and anything else running the pre-0003 code; '
      '(2) for a hold still ''reserved'': wait until its run lease has expired, then run `somnia run work <run>` once '
      'with the pre-0003 code (the claim marks the hold uncertain and blocks the run); '
      '(3) for each uncertain hold: reconcile billing from provider billing evidence with '
      '`somnia budget reconcile <reservation> --actual-micro N --note ...`; '
      '(4) re-run `somnia db migrate`. After the upgrade each such request stays outstanding (no resend) with its '
      'remaining possible charge held, until its response is recorded or it is abandoned with provider-side '
      'evidence and a sender-stopped attestation (`somnia budget abandon-request`).', n, ids
      USING ERRCODE = 'SR001';
  END IF;
END $$;

-- ---------------------------------------------------------------- helpers
CREATE FUNCTION envelope_root(e uuid) RETURNS uuid LANGUAGE sql STABLE AS $$
  WITH RECURSIVE up AS (
    SELECT id, parent_id FROM somnia.budget_envelopes WHERE id = e
    UNION ALL
    SELECT b.id, b.parent_id FROM somnia.budget_envelopes b JOIN up ON b.id = up.parent_id
  )
  SELECT id FROM up WHERE parent_id IS NULL
$$;

-- One lock per logical request, held to the end of the calling transaction.
CREATE FUNCTION lock_logical_request(run uuid, key text) RETURNS void LANGUAGE sql AS $$
  SELECT pg_advisory_xact_lock(hashtextextended(coalesce(run::text, '-') || '/' || key, 3))
$$;

-- ---------------------------------------------------------------- request identity (one per gateway reservation)
CREATE TABLE provider_requests (
  reservation_id    uuid PRIMARY KEY REFERENCES reservations(id),
  run_id            uuid REFERENCES sleep_runs(id),
  stage_attempt_id  uuid REFERENCES stage_attempts(id),
  request_key       text NOT NULL,
  provider          text NOT NULL,
  model             text NOT NULL,
  is_mock           boolean NOT NULL,
  prompt_artifact   text REFERENCES artifacts(sha256),   -- NULL only for rows backfilled from pre-0003 data
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX provider_requests_run_key ON provider_requests (run_id, request_key);

-- Send permits: written (fenced, under the logical-request lock) immediately before contacting the provider.
-- outcome (set once, with ended_at): 'response' (a response was recorded), 'not_sent' (the provider definitively
-- rejected it before acceptance), 'outcome_unknown' (timeout / dropped connection: it may have been billed).
-- Neither error outcome resolves the request or its billing by itself once the hold has left 'reserved' (D048).
CREATE TABLE provider_request_sends (
  reservation_id  uuid PRIMARY KEY REFERENCES provider_requests(reservation_id),
  fence           bigint,
  worker_id       text,
  started_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  ended_at        timestamptz,
  outcome         text CHECK (outcome IN ('response', 'not_sent', 'outcome_unknown')),
  end_note        text,
  CHECK ((ended_at IS NULL) = (end_note IS NULL) AND (ended_at IS NULL) = (outcome IS NULL))
);

CREATE TABLE provider_request_abandonments (
  reservation_id           uuid PRIMARY KEY REFERENCES provider_requests(reservation_id),
  evidence                 text NOT NULL CHECK (length(btrim(evidence)) >= 10),
  sender_stopped_evidence  text,
  actor                    text NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Residual holds: the remaining possible charge of a reconciled-but-outstanding request.
CREATE TABLE request_residual_holds (
  reservation_id  uuid PRIMARY KEY REFERENCES provider_requests(reservation_id),
  envelope_id     uuid NOT NULL REFERENCES budget_envelopes(id),
  amount_micro    bigint NOT NULL CHECK (amount_micro > 0),
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  released_at     timestamptz,
  release_reason  text,
  CHECK ((released_at IS NULL) = (release_reason IS NULL))
);

CREATE FUNCTION residual_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cur uuid := NEW.envelope_id; nxt uuid;
BEGIN
  IF NEW.released_at IS NOT NULL THEN
    RAISE EXCEPTION 'a residual hold must start unreleased' USING ERRCODE = 'SB002';
  END IF;
  WHILE cur IS NOT NULL LOOP  -- keeps an existing commitment: no limit check
    UPDATE somnia.budget_envelopes SET reserved_micro = reserved_micro + NEW.amount_micro
     WHERE id = cur RETURNING parent_id INTO nxt;
    cur := nxt;
  END LOOP;
  RETURN NULL;
END $$;
CREATE TRIGGER residual_apply AFTER INSERT ON request_residual_holds
  FOR EACH ROW EXECUTE FUNCTION residual_after_insert();

CREATE FUNCTION residual_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.released_at IS NOT NULL OR NEW.released_at IS NULL
     OR (NEW.reservation_id, NEW.envelope_id, NEW.amount_micro, NEW.created_at)
        IS DISTINCT FROM (OLD.reservation_id, OLD.envelope_id, OLD.amount_micro, OLD.created_at) THEN
    RAISE EXCEPTION 'a residual hold can only be released, once' USING ERRCODE = 'SB002';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER residual_update_guard BEFORE UPDATE ON request_residual_holds
  FOR EACH ROW EXECUTE FUNCTION residual_before_update();

CREATE FUNCTION residual_after_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cur uuid := NEW.envelope_id; nxt uuid;
BEGIN
  WHILE cur IS NOT NULL LOOP
    UPDATE somnia.budget_envelopes SET reserved_micro = reserved_micro - NEW.amount_micro
     WHERE id = cur RETURNING parent_id INTO nxt;
    cur := nxt;
  END LOOP;
  RETURN NULL;
END $$;
CREATE TRIGGER residual_release AFTER UPDATE ON request_residual_holds
  FOR EACH ROW EXECUTE FUNCTION residual_after_update();
CREATE TRIGGER residual_no_delete BEFORE DELETE OR TRUNCATE ON request_residual_holds
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- backfill pre-0003 gateway reservations
-- (Precondition above: none of them is 'reserved'/'uncertain' without a response.)
INSERT INTO provider_requests (reservation_id, run_id, stage_attempt_id, request_key, provider, model, is_mock,
                               prompt_artifact, created_at)
SELECT r.id, r.run_id, r.stage_attempt_id, split_part(r.idempotency_key, ':', 1), r.provider, r.model,
       e.unit = 'MOCK_MICRO', c.prompt_artifact, r.created_at
  FROM reservations r
  JOIN budget_envelopes e ON e.id = r.envelope_id
  LEFT JOIN provider_calls c ON c.reservation_id = r.id
 WHERE r.run_id IS NOT NULL AND r.stage_attempt_id IS NOT NULL AND r.idempotency_key ~ '^[0-9a-f]{64}:';
DO $$
DECLARE orphan int;
BEGIN
  SELECT count(*) INTO orphan FROM somnia.provider_calls c
   WHERE NOT EXISTS (SELECT 1 FROM somnia.provider_requests q WHERE q.reservation_id = c.reservation_id);
  IF orphan > 0 THEN
    RAISE EXCEPTION 'cannot apply 0003: % provider call(s) have no identifiable request', orphan USING ERRCODE = 'SR001';
  END IF;
END $$;

-- Pre-0003 sends had no permits. Completed ones are recorded as ended; for the others the sender's state is
-- unknown, so the send stays open and an abandonment needs a sender-stopped attestation.
INSERT INTO provider_request_sends (reservation_id, fence, worker_id, started_at, ended_at, outcome, end_note)
SELECT q.reservation_id, NULL, 'pre-0003', q.created_at, c.created_at,
       CASE WHEN c.id IS NOT NULL THEN 'response' END,
       CASE WHEN c.id IS NOT NULL THEN 'pre-0003: response recorded' END
  FROM provider_requests q JOIN reservations r ON r.id = q.reservation_id
  LEFT JOIN provider_calls c ON c.reservation_id = q.reservation_id
 WHERE r.state <> 'released';

-- Every pre-0003 reconciled hold without its own response stays outstanding, and its remaining possible charge
-- stays held. This includes a hold whose LOGICAL request later completed under another hold (a pre-0003 retry):
-- that retry's receipt is reusable, but it says nothing about what the first attempt billed (D048). The hold is
-- resolved only by its own evidence: its own late response (`budget record-response`) or an abandonment.
INSERT INTO request_residual_holds (reservation_id, envelope_id, amount_micro)
SELECT q.reservation_id, r.envelope_id, r.amount_micro - r.actual_micro
  FROM provider_requests q JOIN reservations r ON r.id = q.reservation_id
 WHERE r.state = 'settled' AND r.amount_micro > r.actual_micro
   AND NOT EXISTS (SELECT 1 FROM provider_calls c WHERE c.reservation_id = q.reservation_id);

SELECT make_immutable('provider_requests');
SELECT make_immutable('provider_request_abandonments');

-- ---------------------------------------------------------------- operator-recorded responses (D048)
-- A response that reaches the operator rather than the gateway (e.g. retrieved from the provider for a request
-- whose worker is gone) is recorded with the operator's identity and evidence. For a request created before
-- 0003 the prompt was never stored: such a call keeps prompt_artifact NULL ("prompt not recorded") rather than
-- inventing one. Operator-recorded responses settle billing and complete the request; the gateway never reuses
-- them as model output (their provenance rests on an operator's matching, not on the gateway's own receipt).
ALTER TABLE provider_calls ALTER COLUMN prompt_artifact DROP NOT NULL;

-- Reported usage: each token count is nonnegative on its own (D049). A negative count could otherwise hide inside a
-- zero or positive weighted total. Checked before the constraint is added, with the offending calls listed.
DO $$
DECLARE ids text;
BEGIN
  SELECT string_agg(id::text, ', ') INTO ids FROM somnia.provider_calls WHERE input_tokens < 0 OR output_tokens < 0;
  IF ids IS NOT NULL THEN
    RAISE EXCEPTION 'cannot apply 0003: provider call(s) with negative reported token counts: %', ids
      USING ERRCODE = 'SR001';
  END IF;
END $$;
ALTER TABLE provider_calls ADD CONSTRAINT provider_calls_tokens_nonnegative
  CHECK (input_tokens >= 0 AND output_tokens >= 0);

CREATE TABLE operator_recorded_responses (
  provider_call_id  uuid PRIMARY KEY REFERENCES provider_calls(id),
  reservation_id    uuid NOT NULL UNIQUE REFERENCES provider_requests(reservation_id),
  actor             text NOT NULL CHECK (length(btrim(actor)) > 0),
  evidence          text NOT NULL CHECK (length(btrim(evidence)) >= 10),
  prompt_recorded   boolean NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT make_immutable('operator_recorded_responses');

CREATE FUNCTION operator_response_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM somnia.provider_calls c WHERE c.id = NEW.provider_call_id
                    AND c.reservation_id = NEW.reservation_id
                    AND (c.prompt_artifact IS NOT NULL) = NEW.prompt_recorded) THEN
    RAISE EXCEPTION 'operator-recorded response does not match its provider call' USING ERRCODE = 'SR001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER operator_response_guard BEFORE INSERT ON operator_recorded_responses
  FOR EACH ROW EXECUTE FUNCTION operator_response_check();

-- ---------------------------------------------------------------- guards installed after the backfill
CREATE FUNCTION send_permit_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE q record;
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT run_id, request_key INTO q FROM somnia.provider_requests WHERE reservation_id = NEW.reservation_id;
    PERFORM somnia.lock_logical_request(q.run_id, q.request_key);
    IF EXISTS (SELECT 1 FROM somnia.provider_request_abandonments WHERE reservation_id = NEW.reservation_id) THEN
      RAISE EXCEPTION 'request % was abandoned; no send permit', NEW.reservation_id USING ERRCODE = 'SR001';
    END IF;
    IF NEW.ended_at IS NOT NULL THEN
      RAISE EXCEPTION 'a send permit must start open' USING ERRCODE = 'SR001';
    END IF;
  ELSIF OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL OR NEW.outcome IS NULL
        OR (NEW.reservation_id, NEW.fence, NEW.worker_id, NEW.started_at)
           IS DISTINCT FROM (OLD.reservation_id, OLD.fence, OLD.worker_id, OLD.started_at) THEN
    RAISE EXCEPTION 'a send permit can only be ended, once' USING ERRCODE = 'SR001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER send_permit_guard BEFORE INSERT OR UPDATE ON provider_request_sends
  FOR EACH ROW EXECUTE FUNCTION send_permit_check();
CREATE TRIGGER send_permit_no_delete BEFORE DELETE OR TRUNCATE ON provider_request_sends
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- A completion must belong to a recorded request and carry exactly that request's prompt (NULL only for a
-- request whose prompt was never stored). It is decided under the logical-request lock, then the hold's row lock
-- (the same order as reconcile and abandonment, so they serialize without deadlock).
CREATE FUNCTION provider_call_request_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM somnia.provider_requests q
                  WHERE q.reservation_id = NEW.reservation_id AND q.request_key = NEW.request_key
                    AND q.run_id IS NOT DISTINCT FROM NEW.run_id
                    AND q.prompt_artifact IS NOT DISTINCT FROM NEW.prompt_artifact) THEN
    RAISE EXCEPTION 'provider call for reservation % has no matching provider request (identity and prompt)',
      NEW.reservation_id USING ERRCODE = 'SR001';
  END IF;
  PERFORM somnia.lock_logical_request(NEW.run_id, NEW.request_key);
  PERFORM 1 FROM somnia.reservations WHERE id = NEW.reservation_id FOR UPDATE;
  RETURN NEW;
END $$;
CREATE TRIGGER provider_call_request BEFORE INSERT ON provider_calls
  FOR EACH ROW EXECUTE FUNCTION provider_call_request_check();

-- A call without a recorded prompt exists only as an operator-recorded response (checked at commit).
CREATE FUNCTION provider_call_prompt_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.prompt_artifact IS NULL AND NOT EXISTS (
       SELECT 1 FROM somnia.operator_recorded_responses o WHERE o.provider_call_id = NEW.id) THEN
    RAISE EXCEPTION 'provider call % has no recorded prompt and no operator record', NEW.id USING ERRCODE = 'SR001';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER provider_call_prompt_provenance AFTER INSERT ON provider_calls
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION provider_call_prompt_provenance();

-- A recorded response ends the send and releases any residual hold (its charge is now known).
CREATE FUNCTION provider_call_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE somnia.provider_request_sends
     SET ended_at = clock_timestamp(), outcome = 'response', end_note = 'response recorded'
   WHERE reservation_id = NEW.reservation_id AND ended_at IS NULL;
  UPDATE somnia.request_residual_holds SET released_at = clock_timestamp(), release_reason = 'response recorded'
   WHERE reservation_id = NEW.reservation_id AND released_at IS NULL;
  RETURN NULL;
END $$;
CREATE TRIGGER provider_call_completes AFTER INSERT ON provider_calls
  FOR EACH ROW EXECUTE FUNCTION provider_call_after_insert();

CREATE FUNCTION abandonment_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text; q record;
BEGIN
  SELECT run_id, request_key INTO q FROM somnia.provider_requests WHERE reservation_id = NEW.reservation_id;
  PERFORM somnia.lock_logical_request(q.run_id, q.request_key);
  -- Row lock on the hold: a concurrent reconcile (which may create a residual hold) either commits first and
  -- is seen here, or waits for this abandonment and then sees it. Without it, either ordering could strand a
  -- residual hold that nothing ever releases (D048).
  SELECT state INTO st FROM somnia.reservations WHERE id = NEW.reservation_id FOR UPDATE;
  IF st IN ('reserved', 'released') THEN
    RAISE EXCEPTION 'request % cannot be abandoned while its hold is %', NEW.reservation_id, st
      USING ERRCODE = 'SR001';
  END IF;
  IF EXISTS (SELECT 1 FROM somnia.provider_calls WHERE reservation_id = NEW.reservation_id) THEN
    RAISE EXCEPTION 'request % has a recorded response and cannot be abandoned', NEW.reservation_id
      USING ERRCODE = 'SR001';
  END IF;
  IF EXISTS (SELECT 1 FROM somnia.provider_request_sends WHERE reservation_id = NEW.reservation_id AND ended_at IS NULL)
     AND length(btrim(coalesce(NEW.sender_stopped_evidence, ''))) < 10 THEN
    RAISE EXCEPTION 'request % has an open send permit: its sender may still be sending, so abandonment also needs '
      'sender_stopped_evidence (for example: the worker process is confirmed terminated)', NEW.reservation_id
      USING ERRCODE = 'SR001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER abandonment_guard BEFORE INSERT ON provider_request_abandonments
  FOR EACH ROW EXECUTE FUNCTION abandonment_check();

CREATE FUNCTION abandonment_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE somnia.request_residual_holds SET released_at = clock_timestamp(), release_reason = 'abandoned'
   WHERE reservation_id = NEW.reservation_id AND released_at IS NULL;
  RETURN NULL;
END $$;
CREATE TRIGGER abandonment_releases AFTER INSERT ON provider_request_abandonments
  FOR EACH ROW EXECUTE FUNCTION abandonment_after_insert();

-- ---------------------------------------------------------------- billing discrepancies
CREATE TABLE billing_discrepancies (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id    uuid NOT NULL UNIQUE REFERENCES reservations(id),
  provider_call_id  uuid NOT NULL UNIQUE REFERENCES provider_calls(id),
  envelope_id       uuid NOT NULL REFERENCES budget_envelopes(id),
  root_envelope_id  uuid NOT NULL REFERENCES budget_envelopes(id),
  operator_micro    bigint NOT NULL CHECK (operator_micro >= 0),
  reported_micro    bigint NOT NULL CHECK (reported_micro >= 0),
  delta_micro       bigint NOT NULL,
  status            text NOT NULL CHECK (status IN ('open', 'resolved')),
  resolution_note   text,
  resolved_by       text,
  resolved_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (delta_micro = reported_micro - operator_micro AND delta_micro <> 0),
  CHECK ((status = 'resolved') = (resolution_note IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL))
);
CREATE INDEX billing_discrepancies_open ON billing_discrepancies (root_envelope_id) WHERE status = 'open';

CREATE FUNCTION discrepancy_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  SELECT envelope_id, state, actual_micro INTO r FROM somnia.reservations WHERE id = NEW.reservation_id;
  IF NEW.status <> 'open' OR r.envelope_id IS DISTINCT FROM NEW.envelope_id
     OR NEW.root_envelope_id IS DISTINCT FROM somnia.envelope_root(NEW.envelope_id)
     OR r.state NOT IN ('settled', 'released')
     OR NEW.operator_micro IS DISTINCT FROM COALESCE(r.actual_micro, 0)
     OR NOT EXISTS (SELECT 1 FROM somnia.provider_calls c WHERE c.id = NEW.provider_call_id
                                                         AND c.reservation_id = NEW.reservation_id) THEN
    RAISE EXCEPTION 'billing discrepancy does not match its reservation and recorded response' USING ERRCODE = 'SR001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER discrepancy_insert_guard BEFORE INSERT ON billing_discrepancies
  FOR EACH ROW EXECUTE FUNCTION discrepancy_before_insert();

-- Apply the difference once, to the leaf and every ancestor (no limit check: spent money is a fact).
CREATE FUNCTION discrepancy_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cur uuid := NEW.envelope_id; nxt uuid;
BEGIN
  WHILE cur IS NOT NULL LOOP
    UPDATE somnia.budget_envelopes SET confirmed_micro = confirmed_micro + NEW.delta_micro
     WHERE id = cur RETURNING parent_id INTO nxt;
    cur := nxt;
  END LOOP;
  RETURN NULL;
END $$;
CREATE TRIGGER discrepancy_apply AFTER INSERT ON billing_discrepancies
  FOR EACH ROW EXECUTE FUNCTION discrepancy_after_insert();

CREATE FUNCTION discrepancy_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'open' OR NEW.status <> 'resolved'
     OR (NEW.id, NEW.reservation_id, NEW.provider_call_id, NEW.envelope_id, NEW.root_envelope_id, NEW.operator_micro,
         NEW.reported_micro, NEW.delta_micro, NEW.created_at)
        IS DISTINCT FROM
        (OLD.id, OLD.reservation_id, OLD.provider_call_id, OLD.envelope_id, OLD.root_envelope_id, OLD.operator_micro,
         OLD.reported_micro, OLD.delta_micro, OLD.created_at) THEN
    RAISE EXCEPTION 'a billing discrepancy can only move open -> resolved' USING ERRCODE = 'SB002';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER discrepancy_update_guard BEFORE UPDATE ON billing_discrepancies
  FOR EACH ROW EXECUTE FUNCTION discrepancy_before_update();
CREATE TRIGGER discrepancy_no_delete BEFORE DELETE OR TRUNCATE ON billing_discrepancies
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- reservations
-- Insert: as 0001 plus the SB003 freeze, checked after the chain's row locks are held.
CREATE OR REPLACE FUNCTION reservation_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
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
      IF EXISTS (SELECT 1 FROM somnia.billing_discrepancies d WHERE d.status = 'open'
                    AND d.root_envelope_id = somnia.envelope_root(NEW.envelope_id)) THEN
        RAISE EXCEPTION 'budget frozen: open billing discrepancy in the envelope tree of %', NEW.envelope_id
          USING ERRCODE = 'SB003';
      END IF;
      RAISE EXCEPTION 'budget exceeded: reservation of % on envelope % does not fit', NEW.amount_micro, cur
        USING ERRCODE = 'SB001';
    END IF;
    cur := nxt;
  END LOOP;
  IF EXISTS (SELECT 1 FROM somnia.billing_discrepancies d WHERE d.status = 'open'
                AND d.root_envelope_id = somnia.envelope_root(NEW.envelope_id)) THEN
    RAISE EXCEPTION 'budget frozen: open billing discrepancy in the envelope tree of %', NEW.envelope_id
      USING ERRCODE = 'SB003';
  END IF;
  INSERT INTO somnia.reservation_transitions (reservation_id, from_state, to_state, amount_micro)
       VALUES (NEW.id, NULL, 'reserved', NEW.amount_micro);
  RETURN NULL;
END $$;

-- Update: as 0001, plus: reconciling an uncertain hold whose request is still outstanding keeps the remaining
-- possible charge (bound - operator amount) held as a residual hold on the leaf and every ancestor.
CREATE OR REPLACE FUNCTION reservation_after_update() RETURNS trigger LANGUAGE plpgsql AS $$
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
  IF OLD.state = 'uncertain' AND NEW.state = 'settled' AND NEW.amount_micro > NEW.actual_micro
     AND EXISTS (SELECT 1 FROM somnia.provider_requests q WHERE q.reservation_id = NEW.id)
     AND NOT EXISTS (SELECT 1 FROM somnia.provider_calls c WHERE c.reservation_id = NEW.id)
     AND NOT EXISTS (SELECT 1 FROM somnia.provider_request_abandonments a WHERE a.reservation_id = NEW.id) THEN
    INSERT INTO somnia.request_residual_holds (reservation_id, envelope_id, amount_micro)
         VALUES (NEW.id, NEW.envelope_id, NEW.amount_micro - NEW.actual_micro);
  END IF;
  RETURN NULL;
END $$;

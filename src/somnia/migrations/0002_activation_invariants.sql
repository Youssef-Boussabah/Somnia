-- Somnia migration 0002: activation and membership invariants (repairs after review of 4841892).
-- Forward-only. Adds BEFORE INSERT/UPDATE triggers; no table or column changes, so code written
-- for 0001 keeps working. New SQLSTATE (mapped in somnia/errors.py):
--   SA001 skill-set membership / activation / active-pointer invariant violation
--
-- Defence in depth for somnia.activation: application code checks these first and raises typed
-- errors; the triggers make a buggy or hand-written write fail instead of corrupting history.

SET search_path = somnia, public;

-- Upgrade check: refuse to install over data that already violates the project-boundary rules.
-- (Activations recorded under 0001's chained auto-activation, where a later candidate was
-- activated against a set other than its gate baseline, are legitimate history: they are counted
-- and reported, not rejected. The new rule applies to new activations only.)
DO $$
DECLARE bad_members int; bad_acts int; bad_ptrs int; chained int;
BEGIN
  SELECT count(*) INTO bad_members FROM somnia.skill_set_members m
    JOIN somnia.skill_sets st ON st.id = m.skill_set_id JOIN somnia.skills sk ON sk.id = m.skill_id
    JOIN somnia.skill_versions v ON v.id = m.skill_version_id JOIN somnia.gate_decisions g ON g.id = m.gate_decision_id
   WHERE sk.project_id <> st.project_id OR v.skill_id <> m.skill_id OR g.skill_version_id <> m.skill_version_id;
  SELECT count(*) INTO bad_acts FROM somnia.activations a JOIN somnia.skill_sets n ON n.id = a.new_set_id
   WHERE n.project_id <> a.project_id;
  SELECT count(*) INTO bad_ptrs FROM somnia.active_skill_sets p JOIN somnia.skill_sets st ON st.id = p.skill_set_id
   WHERE st.project_id <> p.project_id;
  IF bad_members + bad_acts + bad_ptrs > 0 THEN
    RAISE EXCEPTION 'cannot apply 0002: % member(s), % activation(s), % pointer(s) cross a project boundary',
      bad_members, bad_acts, bad_ptrs USING ERRCODE = 'SA001';
  END IF;
  SELECT count(*) INTO chained FROM somnia.activations a JOIN somnia.gate_decisions g ON g.id = a.gate_decision_id
   WHERE a.kind = 'activate' AND g.baseline_skill_set_id IS DISTINCT FROM a.previous_set_id;
  RAISE NOTICE '0002: % existing activation(s) replaced a set other than their gate baseline (pre-0002 history)',
    chained;
END $$;

-- A member must be a version of its skill, of the set's project, with a passing gate decision for
-- that exact version; a mock-gated member requires the set to be flagged contains_mock_validation.
CREATE FUNCTION skill_set_member_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE set_project uuid; set_mock boolean; skill_project uuid; v_skill uuid;
        g_version uuid; g_mock boolean; g_decision text;
BEGIN
  SELECT project_id, contains_mock_validation INTO set_project, set_mock FROM somnia.skill_sets WHERE id = NEW.skill_set_id;
  SELECT project_id INTO skill_project FROM somnia.skills WHERE id = NEW.skill_id;
  SELECT skill_id INTO v_skill FROM somnia.skill_versions WHERE id = NEW.skill_version_id;
  SELECT skill_version_id, is_mock, decision INTO g_version, g_mock, g_decision
    FROM somnia.gate_decisions WHERE id = NEW.gate_decision_id;
  IF skill_project IS DISTINCT FROM set_project THEN
    RAISE EXCEPTION 'skill set member skill % belongs to another project', NEW.skill_id USING ERRCODE = 'SA001';
  END IF;
  IF v_skill IS DISTINCT FROM NEW.skill_id THEN
    RAISE EXCEPTION 'member version % is not a version of skill %', NEW.skill_version_id, NEW.skill_id
      USING ERRCODE = 'SA001';
  END IF;
  IF g_version IS DISTINCT FROM NEW.skill_version_id OR g_decision IS DISTINCT FROM 'pass' THEN
    RAISE EXCEPTION 'member gate decision % is not a passing decision for version %', NEW.gate_decision_id,
      NEW.skill_version_id USING ERRCODE = 'SA001';
  END IF;
  IF g_mock AND NOT set_mock THEN
    RAISE EXCEPTION 'mock-gated member in skill set % not flagged contains_mock_validation', NEW.skill_set_id
      USING ERRCODE = 'SA001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER skill_set_member_invariants BEFORE INSERT ON skill_set_members
  FOR EACH ROW EXECUTE FUNCTION skill_set_member_check();

-- Every set an activation names belongs to its project. An 'activate' must replace exactly the
-- set its gate decision validated against, for the gated version, with a passing decision.
CREATE FUNCTION activation_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE g_baseline uuid; g_version uuid; g_decision text; v_project uuid;
BEGIN
  IF EXISTS (SELECT 1 FROM somnia.skill_sets
              WHERE id IN (NEW.new_set_id, NEW.previous_set_id, NEW.expected_previous_set_id)
                AND project_id <> NEW.project_id) THEN
    RAISE EXCEPTION 'activation % names a skill set of another project', NEW.idempotency_key USING ERRCODE = 'SA001';
  END IF;
  IF NEW.kind = 'activate' THEN
    SELECT baseline_skill_set_id, skill_version_id, decision INTO g_baseline, g_version, g_decision
      FROM somnia.gate_decisions WHERE id = NEW.gate_decision_id;
    SELECT s.project_id INTO v_project FROM somnia.skill_versions v JOIN somnia.skills s ON s.id = v.skill_id
     WHERE v.id = NEW.skill_version_id;
    IF g_baseline IS DISTINCT FROM NEW.previous_set_id THEN
      RAISE EXCEPTION 'activation must replace the set its gate decision validated against (% != %)',
        NEW.previous_set_id, g_baseline USING ERRCODE = 'SA001';
    END IF;
    IF g_version IS DISTINCT FROM NEW.skill_version_id OR g_decision IS DISTINCT FROM 'pass'
       OR v_project IS DISTINCT FROM NEW.project_id THEN
      RAISE EXCEPTION 'activation gate decision, version and project do not agree' USING ERRCODE = 'SA001';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER activation_invariants BEFORE INSERT ON activations
  FOR EACH ROW EXECUTE FUNCTION activation_check();

-- The active pointer names a set of its own project, through an activation of that project that
-- produced or targeted exactly that set.
CREATE FUNCTION active_pointer_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s_project uuid; a_project uuid; a_set uuid;
BEGIN
  SELECT project_id INTO s_project FROM somnia.skill_sets WHERE id = NEW.skill_set_id;
  SELECT project_id, new_set_id INTO a_project, a_set FROM somnia.activations WHERE id = NEW.activation_id;
  IF s_project IS DISTINCT FROM NEW.project_id OR a_project IS DISTINCT FROM NEW.project_id
     OR a_set IS DISTINCT FROM NEW.skill_set_id THEN
    RAISE EXCEPTION 'active pointer of project % must name its own set via the activation that set it', NEW.project_id
      USING ERRCODE = 'SA001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER active_pointer_invariants BEFORE INSERT OR UPDATE ON active_skill_sets
  FOR EACH ROW EXECUTE FUNCTION active_pointer_check();

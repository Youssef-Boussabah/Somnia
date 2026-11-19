"""Budget ledger: concurrency, hierarchy, uncertain billing and DB-level transition rules."""

from __future__ import annotations

import threading
import uuid

import psycopg
import pytest

from conftest import load_config, make_run
from somnia import budget
from somnia.engine import SleepWorker
from somnia.errors import BudgetExceeded, InvalidReservationTransition, translating
from somnia.mock import MockProvider, load_mock_script


def _reserve(conn, env, amount, key=None):
    return budget.reserve(conn, envelope_id=env, amount_micro=amount, idempotency_key=key or uuid.uuid4().hex,
                          purpose="test", provider="mock", model="mock", pricing_snapshot={"test": True})


def _race(connect_db, targets: list[tuple[uuid.UUID, int]]) -> tuple[int, int, list[BaseException]]:
    """Fire one reservation per target from its own connection, released by a barrier."""
    conns = [connect_db() for _ in targets]
    barrier = threading.Barrier(len(targets))
    ok, rejected, errors = [], [], []

    def worker(c, env, amount):
        try:
            barrier.wait()
            _reserve(c, env, amount)
            ok.append(env)
        except BudgetExceeded:
            rejected.append(env)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c, e, a)) for c, (e, a) in zip(conns, targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return len(ok), len(rejected), errors


def test_concurrent_reservations_cannot_exceed_limit(connect_db, conn, project):
    env = budget.create_envelope(conn, project["id"], "race", unit="MOCK_MICRO", limit_micro=100_000)
    n_ok, n_rej, errors = _race(connect_db, [(env, 7_000)] * 40)
    assert errors == []
    assert (n_ok, n_rej) == (14, 26)  # floor(100000 / 7000) = 14
    st = budget.envelope_status(conn, env)
    assert st["reserved_micro"] == 98_000 and st["reserved_micro"] <= st["limit_micro"]
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_concurrent_reservations_respect_parent_envelope(connect_db, conn, project):
    root = budget.create_envelope(conn, project["id"], "root", unit="MOCK_MICRO", limit_micro=70_000)
    a = budget.create_envelope(conn, project["id"], "a", unit="MOCK_MICRO", limit_micro=60_000, parent_id=root)
    b = budget.create_envelope(conn, project["id"], "b", unit="MOCK_MICRO", limit_micro=60_000, parent_id=root)
    n_ok, _, errors = _race(connect_db, [(a, 5_000)] * 20 + [(b, 5_000)] * 20)
    assert errors == []
    assert n_ok == 14  # parent allows 70000 / 5000
    assert budget.envelope_status(conn, root)["reserved_micro"] == 70_000
    for child in (a, b):
        assert budget.envelope_status(conn, child)["reserved_micro"] <= 60_000
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_uncertain_billing_retains_reservation(connect_db, artifacts, project):
    conn = connect_db()
    # One call's upper bound is 4000*2 + 1000*8 = 16000; the envelope fits one hold but not two.
    run = make_run(conn, project["id"], envelope_limit=24_000)
    flaky = MockProvider(load_mock_script("indexer_checkpoint_v1"), fail_mode="unknown_outcome")
    out = SleepWorker(conn, artifacts, worker_id="A", provider=flaky).work(run["run_id"])
    assert out.status == "blocked"
    res = conn.execute("SELECT id, state, amount_micro FROM reservations WHERE run_id = %s",
                       (run["run_id"],)).fetchone()
    assert res["state"] == "uncertain" and res["amount_micro"] == 16_000
    assert budget.envelope_status(conn, run["envelope_id"])["reserved_micro"] == 16_000

    # The hold is real: a new reservation that only fits if the uncertain one were freed is rejected.
    with pytest.raises(BudgetExceeded):
        _reserve(conn, run["envelope_id"], 16_000)
    with pytest.raises(InvalidReservationTransition):
        budget.release_unsent(conn, res["id"], note="no response, assume free")

    budget.reconcile(conn, res["id"], actual_micro=1_234, note="dev test: provider usage export shows 1234")
    st = budget.envelope_status(conn, run["envelope_id"])
    assert (st["reserved_micro"], st["confirmed_micro"]) == (0, 1_234)
    _reserve(conn, run["envelope_id"], 16_000)  # now fits
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_not_sent_releases_reservation(conn, artifacts, project):
    run = make_run(conn, project["id"])
    refused = MockProvider(load_mock_script("indexer_checkpoint_v1"), fail_mode="not_sent")
    with pytest.raises(Exception):
        SleepWorker(conn, artifacts, worker_id="A", provider=refused).work(run["run_id"])
    states = [r["state"] for r in conn.execute("SELECT state FROM reservations WHERE run_id = %s",
                                               (run["run_id"],)).fetchall()]
    assert states == ["released"]
    assert budget.envelope_status(conn, run["envelope_id"])["reserved_micro"] == 0


def test_db_rejects_invalid_transitions_even_from_raw_sql(conn, project):
    env = budget.create_envelope(conn, project["id"], "raw", unit="MOCK_MICRO", limit_micro=50_000)
    r = _reserve(conn, env, 10_000)
    budget.mark_uncertain(conn, r.id, note="test")
    for sql in ("UPDATE reservations SET state = 'released' WHERE id = %s",
                "UPDATE reservations SET state = 'reserved' WHERE id = %s",
                "UPDATE reservations SET amount_micro = 1 WHERE id = %s",
                "UPDATE reservations SET state = 'settled' WHERE id = %s"):  # settled requires actual_micro
        with pytest.raises(InvalidReservationTransition), translating():
            conn.execute(sql, (r.id,))
    with pytest.raises(psycopg.Error):
        conn.execute("DELETE FROM reservations WHERE id = %s", (r.id,))
    with pytest.raises(InvalidReservationTransition), translating():
        conn.execute("INSERT INTO reservations (envelope_id, idempotency_key, purpose, provider, model, amount_micro,"
                     " pricing_snapshot, state) VALUES (%s, 'raw-x', 'p', 'm', 'm', 5, '{}', 'settled')", (env,))
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_overrun_settlement_is_recorded_and_blocks_further_reservations(conn, project):
    env = budget.create_envelope(conn, project["id"], "overrun", unit="MOCK_MICRO", limit_micro=10_000)
    r = _reserve(conn, env, 8_000)
    budget.settle(conn, r.id, actual_micro=11_000, request_id="mock-overrun")  # provider charged more than bound
    st = budget.envelope_status(conn, env)
    assert st["confirmed_micro"] == 11_000 and st["available_micro"] < 0
    with pytest.raises(BudgetExceeded):
        _reserve(conn, env, 1)


def test_reservation_idempotency(conn, project):
    from somnia.errors import IdempotencyConflict
    env = budget.create_envelope(conn, project["id"], "idem", unit="MOCK_MICRO", limit_micro=50_000)
    r1 = _reserve(conn, env, 10_000, key="k1")
    r2 = _reserve(conn, env, 10_000, key="k1")
    assert r1.id == r2.id and r2.created is False
    assert budget.envelope_status(conn, env)["reserved_micro"] == 10_000
    with pytest.raises(IdempotencyConflict):
        _reserve(conn, env, 9_999, key="k1")


def test_mock_and_real_units_never_mix(conn, artifacts, project):
    cad = budget.create_envelope(conn, project["id"], "cad", unit="CAD_MICRO", limit_micro=1_000_000)
    with pytest.raises(InvalidReservationTransition):  # child must share unit with parent
        budget.create_envelope(conn, project["id"], "bad-child", unit="MOCK_MICRO", limit_micro=1, parent_id=cad)
    run = make_run(conn, project["id"])
    from somnia.engine import create_run
    with pytest.raises(ValueError):  # mock gateway config against a CAD envelope
        create_run(conn, project_id=project["id"], snapshot_id=run["snapshot_id"], envelope_id=cad,
                   config=load_config(), idempotency_key="cad-run")

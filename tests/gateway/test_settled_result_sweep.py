"""Settled admission results are turn-scoped idempotency snapshots, not history.

``sweep_settled_admission_results`` removes ``gateway.admission.result.v1.*`` state_meta rows
once their exact-retry horizon has passed, on the auto-maintenance cadence. The ``messages``
table rows for the same turns are never touched.
"""
import json
import time

import pytest

from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input

RESULT_PREFIX = 'gateway.admission.result.v1.'


def _run_turn(owner, request_id, *, payload=None, age_settled_days=None):
    admit_session_input(owner.db, epoch=owner.epoch, principal_id='api', session_id='s',
                        request_id=request_id, payload=payload or {'text': f'ask {request_id}'})
    row = claim_session_input(owner.db, epoch=owner.epoch, session_id='s')
    result = {'result': {'final_response': 'done', 'messages': [
        {'role': 'assistant', 'content': 'done', 'timestamp': time.time()}], 'completed': True},
        'usage': {'input_tokens': 1}}
    settled = settle_session_input(owner.db, epoch=owner.epoch, admission_id=row['admission_id'],
                                   generation=row['generation'], outcome='completed', result=result)
    if age_settled_days:
        with owner.db._read_ctx() as conn:
            pass
        owner.db._execute_write(lambda conn: conn.execute(
            'UPDATE session_admissions SET settled_at=? WHERE admission_id=?',
            (time.time() - age_settled_days * 86400.0, row['admission_id'])))
    return settled, row['admission_id']


def _results(owner):
    with owner.db._read_ctx() as conn:
        return {row[0]: row[1] for row in conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?", (RESULT_PREFIX + '%',))}


def test_settle_stamps_settled_at(owner):
    owner.db.create_session('s', source='api_server')
    settled, admission_id = _run_turn(owner, '0')
    assert settled['status'] == 'terminal'
    with owner.db._read_ctx() as conn:
        stamp = conn.execute('SELECT settled_at FROM session_admissions WHERE admission_id=?',
                             (admission_id,)).fetchone()[0]
    assert isinstance(stamp, float) and abs(stamp - time.time()) < 60


def test_sweep_removes_only_expired_results(owner):
    owner.db.create_session('s', source='api_server')
    _run_turn(owner, 'fresh')
    _run_turn(owner, 'old', age_settled_days=30)
    assert owner.db.sweep_settled_admission_results(retention_days=7) == 1
    remaining = _results(owner)
    assert len(remaining) == 1 and 'old' not in ' '.join(remaining)  # only the fresh one stays


def test_sweep_zero_retention_disables(owner):
    owner.db.create_session('s', source='api_server')
    _run_turn(owner, 'old', age_settled_days=30)
    assert owner.db.sweep_settled_admission_results(retention_days=0) == 0
    assert len(_results(owner)) == 1


def test_sweep_legacy_rows_without_settled_at(owner):
    """Rows written before the column existed fall back to the result's own timestamps."""
    owner.db.create_session('s', source='api_server')
    for request_id, age in (('ancient', 30), ('fresh', 0)):
        _run_turn(owner, request_id)
        owner.db._execute_write(lambda conn, rid=request_id: conn.execute(
            'UPDATE session_admissions SET settled_at=NULL WHERE admission_id IN '
            "(SELECT admission_id FROM session_admissions WHERE request_id=?)", (rid,)))
        if age:
            owner.db._execute_write(lambda conn, rid=request_id: conn.execute(
                f"UPDATE state_meta SET value=json_set(value, '$.result.messages[0].timestamp', ?) "
                f"WHERE key=(SELECT '{RESULT_PREFIX}' || admission_id FROM session_admissions WHERE request_id=?)",
                (time.time() - age * 86400.0, rid)))
    assert owner.db.sweep_settled_admission_results(retention_days=7) == 1
    with owner.db._read_ctx() as conn:
        kept = [row[0] for row in conn.execute(
            'SELECT request_id FROM session_admissions ORDER BY request_id')]
    assert kept == ['ancient', 'fresh']  # admissions survive; only their snapshots go
    assert len(_results(owner)) == 1


def test_sweep_never_touches_messages(owner):
    """The sweep deletes state_meta snapshots only; transcript rows in ``messages`` never go."""
    owner.db.create_session('s', source='api_server')
    owner.db._execute_write(lambda conn: conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s','user','keep',?)",
        (time.time(),)))
    _run_turn(owner, 'old', age_settled_days=30)
    owner.db.sweep_settled_admission_results(retention_days=7)
    with owner.db._read_ctx() as conn:
        n = conn.execute("SELECT count(*) FROM messages WHERE session_id='s'").fetchone()[0]
    assert n >= 1  # sweep deleted no transcript rows

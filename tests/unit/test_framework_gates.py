"""A-02/A-03、C-01..09：真实命令来源、确定性快路径及反证失败关闭。"""
import json
from pathlib import Path
from unittest.mock import patch

from tests.framework_support import FLAG, Reviewer, fixture, proof
from ctf_ext.harness.flag_gate import FlagGate


def test_C01_missing_command_evidence_is_L4():
    with fixture() as store:
        store.declare('c1', FLAG)
        eid = store.evidence.put_bytes(FLAG.encode())
        decision = FlagGate(store).evaluate('c1', FLAG, [eid])
        assert (decision.status, decision.level) == ('rejected', 'L4')


def test_C02_exact_hit_uses_no_llm_and_records_trace():
    with fixture() as store:
        reviewer = Reviewer(error=AssertionError('must not call model'))
        decision = proof(store, reviewer=reviewer)
        assert decision.status == 'confirmed' and decision.level == 'L1'
        assert reviewer.calls == 0
        assert all(decision.gates[k] for k in ('grounding', 'negation', 'interrogation'))
        with store.board._write() as db:
            FlagGate(store).record(db, decision)
        data = json.loads(store.db.execute('SELECT payload FROM gate_decisions').fetchone()[0])
        command = store.db.execute('SELECT * FROM command_log WHERE id=?', (data['command_id'],)).fetchone()
        assert json.loads(command['argv']) and store.restore_evidence(command['evidence_id']) == FLAG.encode()


def test_C03_case_and_wrapper_hits_require_all_reviews():
    for output in (FLAG.upper(), 'FLAG[' + FLAG[5:-1].upper() + ']'):
        with fixture() as store:
            reviewer = Reviewer()
            decision = proof(store, output=output, reviewer=reviewer)
            assert (decision.status, decision.level) == ('confirmed', 'L2')
            assert reviewer.calls == 4


def test_C04_decoy_low_entropy_and_ambiguity_rejected():
    for output, candidate in [('session-id: ' + FLAG, FLAG), ('诱饵 ' + FLAG, FLAG),
                              (FLAG + '\nflag{another_67b9}', FLAG), ('flag{aaaaaaa}', 'flag{aaaaaaa}')]:
        with fixture() as store:
            decision = proof(store, output=output, declared=candidate)
            assert (decision.status, decision.level) == ('rejected', 'L3')


def test_C05_partial_or_embedded_match_is_not_grounding():
    for output in (FLAG[:-3], 'not' + FLAG, FLAG + 'suffix', 'flag{trusted_7b2e91_extra}'):
        with fixture() as store:
            decision = proof(store, output=output)
            assert decision.status == 'rejected' and decision.level == 'L4'


def test_C06_majority_negation_vetoes_candidate():
    with fixture() as store:
        reviewer = Reviewer(votes=(True, False, True))
        decision = proof(store, output=FLAG.upper(), reviewer=reviewer)
        assert decision.status == 'rejected' and reviewer.calls == 3
        assert not decision.gates.get('interrogation')


def test_C07_every_interrogation_item_is_required():
    for key in ('source', 'unique', 'format'):
        with fixture() as store:
            answers = dict(source=True, unique=True, format=True)
            answers[key] = False
            decision = proof(store, output=FLAG.upper(), reviewer=Reviewer(answers=answers))
            assert decision.status == 'rejected'


def test_C08_missing_timeout_or_malformed_reviewer_stays_pending():
    for reviewer in (None, Reviewer(error=TimeoutError()), Reviewer(error=RuntimeError()),
                     Reviewer(votes=(False, 'false', False)), Reviewer(answers={'source': True})):
        with fixture() as store:
            decision = proof(store, output=FLAG.upper(), reviewer=reviewer)
            assert (decision.status, decision.level) == ('pending', 'L2')


def test_C08_late_review_result_cannot_confirm():
    with fixture() as store:
        class Late(Reviewer):
            def interrogate(self, focus, timeout):
                tick[0] = 40
                return super().interrogate(focus, timeout)
        tick = [0]
        # 只模拟校验时钟，不等待真实超时。
        decision = proof(store, output=FLAG.upper())
        with patch('ctf_ext.harness.flag_gate.time.monotonic', side_effect=lambda: tick[0]):
            result = FlagGate(store, Late()).evaluate('c1', FLAG, decision.evidence_ids, timeout=30)
        assert result.status == 'pending'


def test_C09_requires_exact_regular_FLAG_declaration():
    with fixture() as store:
        decision = proof(store)
        path = store.workspace('c1') / 'FLAG'
        path.unlink()
        assert FlagGate(store).evaluate('c1', FLAG, decision.evidence_ids).status == 'rejected'
        source = store.root / 'external.txt'
        source.write_text(FLAG)
        path.symlink_to(source)
        assert FlagGate(store).evaluate('c1', FLAG, decision.evidence_ids).status == 'rejected'
        path.unlink()
        store.declare('c1', 'request-id-123')
        assert FlagGate(store).evaluate('c1', FLAG, decision.evidence_ids).status == 'rejected'


def test_A03_cross_challenge_and_incomplete_evidence_rejected():
    with fixture() as store:
        decision = proof(store)
        store.declare('c2', FLAG)
        assert FlagGate(store).evaluate('c2', FLAG, decision.evidence_ids).status == 'rejected'
        store.db.execute('UPDATE command_log SET complete=0')
        assert FlagGate(store).evaluate('c1', FLAG, decision.evidence_ids).status == 'rejected'


def test_A02_tampered_archive_cannot_ground():
    with fixture() as store:
        decision = proof(store)
        path = store.db.execute('SELECT path FROM evidence WHERE sha256=?', (decision.evidence_ids[0],)).fetchone()[0]
        Path(path).write_bytes(b'tampered')
        assert FlagGate(store).evaluate('c1', FLAG, decision.evidence_ids).status == 'rejected'


def test_C07_reviewer_receives_only_focused_window_and_no_other_commands():
    with fixture() as store:
        reviewer = Reviewer()
        decision = proof(store, output='UNRELATED SECRET' + 'x' * 1000 + '\n' + FLAG.upper() + '\n' + 'y' * 1000, reviewer=reviewer)
        assert decision.status == 'confirmed'
        assert len(reviewer.focus['output_window']) < 1000
        assert 'UNRELATED SECRET' not in reviewer.focus['output_window']


def test_declaration_change_during_review_fails_closed():
    with fixture() as store:
        class Changes(Reviewer):
            def interrogate(self, focus, timeout):
                store.declare('c1', 'flag{changed_7b2e91}')
                return super().interrogate(focus, timeout)
        decision = proof(store, output=FLAG.upper(), reviewer=Changes())
        assert decision.status == 'pending'

"""优化基线测试夹具：实际本地固定命令，无模型、网络或平台提交。"""
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ctf_ext.domain.migrate import migrate
from ctf_ext.domain.models import Challenge
from ctf_ext.store.blackboard import Blackboard
from ctf_ext.store.evidence import EvidenceStore
from ctf_ext.store.session import StateStore
from ctf_ext.runtime.control import SessionControl
from ctf_ext.gateway.execution import CommandExecutor
from ctf_ext.harness.flag_gate import FlagGate
from ctf_ext.harness.schema import SubmitReceipt

FLAG = 'flag{trusted_7b2e91}'


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='framework-baseline-') as root:
        root = Path(root)
        db = sqlite3.connect(str(root / 'test.db'), isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        migrate(db)
        evidence = EvidenceStore(root / 'evidence', db)
        board = Blackboard(db, evidence_check=evidence.verify)
        store = StateStore(board, evidence, root / 'workspaces')
        board.add_challenge(Challenge(id='c1', name='离线测试', category='misc'))
        try:
            yield store
        finally:
            db.close()


def proof(store, output=FLAG, declared=FLAG, cid='c1', reviewer=None):
    control = SessionControl(store, cid)
    executor = CommandExecutor(store, control, 'test-environment', allow_local_mock=True)
    _, eid, _, _ = executor.execute_mock_decoder(output.encode(), decode=False)
    store.declare(cid, declared)
    return FlagGate(store, reviewer).evaluate(cid, declared, [eid])


class Reviewer:
    def __init__(self, votes=(False, False, False), answers=None, error=None):
        self.votes = votes
        self.answers = answers if answers is not None else dict(source=True, unique=True, format=True)
        self.error, self.calls, self.focus = error, 0, None

    def negate(self, focus, index, timeout):
        self.calls += 1
        self.focus = focus
        if self.error:
            raise self.error
        return self.votes[index]

    def interrogate(self, focus, timeout):
        self.calls += 1
        return self.answers


class Adapter:
    def __init__(self, receipt=None):
        self.receipt = receipt or SubmitReceipt('ACCEPTED', 'offline-receipt')
        self.calls, self.queries = [], []

    def submit_answer(self, sub):
        self.calls.append(sub)
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt

    def get_receipt(self, key):
        self.queries.append(key)
        return SubmitReceipt('ACCEPTED', 'queried-receipt')


def enqueue(store, gateway, decision):
    with store.board._write() as db:
        FlagGate(store).record(db, decision)
        return gateway.enqueue(db, decision)


def expect(error, callback):
    try:
        callback()
    except error:
        return
    raise AssertionError('expected ' + error.__name__)

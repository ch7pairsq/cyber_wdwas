"""文件数据库租约心跳；独立连接避免与主线程事务交错。"""
import sqlite3
import threading
import time


class LeaseHeartbeat:
    def __init__(self, board, claim, owner, seconds):
        self.board, self.claim, self.owner, self.seconds = board, claim, owner, seconds
        self.path = board._db.execute('PRAGMA database_list').fetchone()[2]
        self.stop = threading.Event()
        self.thread = None
        self.error = None

    def __enter__(self):
        if self.path:
            self.thread = threading.Thread(target=self._run, name='lease-heartbeat', daemon=True)
            self.thread.start()
        return self

    def _run(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            while not self.stop.wait(max(.01, self.seconds / 3)):
                now = time.time()
                try:
                    cursor = db.execute("UPDATE intents_ext SET lease_until=MAX(lease_until,?),updated_at=? WHERE id=? AND owner=? AND attempt_no=? AND status='RUNNING' AND lease_until>?",
                        (now + self.seconds, now, self.claim.intent_id, self.owner, self.claim.attempt_no, now))
                    db.commit()
                    if cursor.rowcount != 1:
                        self.error = 'lease lost'
                        return
                except sqlite3.Error as exc:
                    db.rollback()
                    self.error = type(exc).__name__
                    return
        finally:
            db.close()

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

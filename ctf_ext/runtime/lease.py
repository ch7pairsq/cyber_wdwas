"""文件数据库租约心跳；独立连接避免与主线程事务交错。"""
import sqlite3
import threading
import time


class LeaseHeartbeat:
    """租约自动续约守护：with 块内后台线程周期性延长租约，防执行中被回收。"""
    def __init__(self, board, claim, owner, seconds):
        """seconds 为租约时长；心跳间隔为 seconds/3，续到 now+seconds。"""
        self.board, self.claim, self.owner, self.seconds = board, claim, owner, seconds
        self.path = board._db.execute('PRAGMA database_list').fetchone()[2]
        self.stop = threading.Event()
        self.thread = None
        self.error = None

    def __enter__(self):
        """启动后台续约线程（daemon）；无数据库路径时为 no-op。"""
        if self.path:
            self.thread = threading.Thread(target=self._run, name='lease-heartbeat', daemon=True)
            self.thread.start()
        return self

    def _run(self):
        """续约循环：独立 sqlite 连接（避免与主线程事务交错）；租约丢失即退出。"""
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
        """停止续约线程并等待退出（最多 2 秒）。"""
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

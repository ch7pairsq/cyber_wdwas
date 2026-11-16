#!/usr/bin/env python3
"""并发压测（§18.2、§23.3）：4/6/8 Worker 同时领题与写结果。

测的是黑板/SQLite 的串行化与租约互斥：无重复领取、无 database-locked、
无结果丢失。它不是平台压测，也不代表真实模型吞吐。
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ctf_ext.domain.migrate import migrate
from ctf_ext.domain.models import OUTCOME_NO_SIGNAL, Challenge
from ctf_ext.store.blackboard import Blackboard, IntentDraft, WorkerResult


def run_once(workers: int, intents: int, work_ms: float) -> dict:
    root = tempfile.mkdtemp(prefix="bench-")
    db = os.path.join(root, "bench.db")
    setup = sqlite3.connect(db, timeout=30)
    migrate(setup)
    board = Blackboard(setup)
    for i in range(intents):
        cid = "c{:03d}".format(i)
        board.add_challenge(Challenge(id=cid, name=cid))
        board.add_intent(IntentDraft(challenge_id=cid, strategy_key="misc.triage.auto",
                                     params={"i": i}))
    setup.close()

    claims, errors, busy = [], [], [0.0] * workers
    lock = threading.Lock()

    def worker(idx: int) -> None:
        conn = sqlite3.connect(db, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        board = Blackboard(conn)
        name = "w{}".format(idx)
        try:
            while True:
                claim = board.claim_intent(name, lease_seconds=60)
                if claim is None:
                    break
                with lock:
                    claims.append(claim.intent_id)
                if work_ms:
                    time.sleep(work_ms / 1000.0)
                busy[idx] += work_ms / 1000.0
                result = WorkerResult(
                    intent_id=claim.intent_id, owner=name, attempt_no=claim.attempt_no,
                    challenge_id=claim.challenge_id, strategy_key=claim.strategy_key,
                    status="completed", outcome=OUTCOME_NO_SIGNAL, summary=["bench"],
                )
                board.write_result(result, "{}-{}".format(claim.intent_id, claim.attempt_no))
        except Exception as exc:  # 压测必须暴露错误，不吞掉
            with lock:
                errors.append("{}: {}".format(type(exc).__name__, exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    started = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    elapsed = max(time.time() - started, 1e-6)

    check = sqlite3.connect(db)
    completed = check.execute("SELECT COUNT(*) FROM intents_ext WHERE status='COMPLETED'").fetchone()[0]
    results = check.execute("SELECT COUNT(*) FROM results_ext").fetchone()[0]
    check.close()
    unique = len(set(claims))
    return {
        "workers": workers, "intents": intents, "seconds": round(elapsed, 3),
        "claims": len(claims), "unique_claims": unique,
        "duplicates": len(claims) - unique, "completed": completed, "results": results,
        "claims_per_sec": round(len(claims) / elapsed, 1),
        "utilization": round(sum(busy) / (workers * elapsed), 3),
        "errors": len(errors), "error_samples": errors[:3],
        "passed": not errors and len(claims) == intents and unique == intents
                  and completed == intents and results == intents,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", default="4,6,8")
    parser.add_argument("--intents", type=int, default=48)
    parser.add_argument("--work-ms", type=float, default=5.0)
    parser.add_argument("--out", default="build/bench.json")
    args = parser.parse_args()
    results = [run_once(int(w), args.intents, args.work_ms) for w in args.workers.split(",")]
    payload = {"generated_at": time.time(), "work_ms": args.work_ms,
               "note": "single-host SQLite simulation; not platform throughput",
               "results": results}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    for r in results:
        print("workers={workers} intents={intents} claims={claims} dup={duplicates} "
              "completed={completed} errors={errors} {seconds}s {claims_per_sec}/s "
              "util={utilization:.0%} passed={passed}".format(**r))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Durable job queue on Postgres (task 12).

The webhook writes a row and returns. A worker reads it, thinks, and replies.

WHY A QUEUE AT ALL
------------------
An LLM turn takes 3-8 seconds. WhatsApp wants an immediate 200. The app is one
gunicorn worker with four threads, so four slow turns and the webhook stops
answering entirely -- at which point Wati reads the integration as broken and
retries, which makes it worse. Rev 2 raises the stakes: with a knock engine firing
scheduled sends, the queue is also the only thing keeping scheduled work and live
conversation out of the same thread pool.

WHY POSTGRES AND NOT REDIS
--------------------------
Volume here is tens of messages a minute at the absolute peak. Redis would add a
service to deploy, secure, back up and keep in sync with the leads it serves, to buy
throughput we will never use. `FOR UPDATE SKIP LOCKED` has been the correct answer
for this size of problem since Postgres 9.5, and the jobs end up in the same backup
as the data they refer to.

WHAT DOES **NOT** GO IN THE QUEUE
---------------------------------
Opt-out. If someone types STOP and the worker is backed up, that must still register
immediately -- it is a regex and an insert, and deferring it means a person who asked
us to stop stays contactable for as long as the backlog lasts. Safety-critical work
stays on the fast path; only the thinking is deferred.

ORDERING
--------
One in-flight job per phone number, enforced in the claim query. Two messages from
the same person must never be processed concurrently: the agent would answer both
from the same stale state and reply twice, or race on the checklist. Different
people are fully parallel.
"""
import json
import re

import db

# The provider is busy, not broken. Worth retrying in seconds rather than minutes.
_TRANSIENT = re.compile(
    r"overloaded|529|rate.?limit|429|timeout|timed out|temporarily unavailable|"
    r"503|502|connection reset|connection aborted", re.I)

KIND_INBOUND = "inbound_message"
# Fetch Meta's click id for an ad arrival. Enqueued with phone=None ON PURPOSE: the
# claim query serialises one job per phone, so giving this a phone would put an HTTP
# round-trip in front of the buyer's reply. Nobody is waiting on a click id.
KIND_CTWA_CAPTURE = "ctwa_capture"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# How long a claimed job may stay 'running' before another worker may take it. Must
# exceed the slowest realistic turn (LLM + retrieval + send) by a wide margin: a lease
# that expires mid-thought produces a duplicate reply, which is worse than a delay.
LEASE_SECONDS = 300


def enqueue(kind, payload, phone=None, dedup_key=None, delay_seconds=0):
    """Add a job. Returns True if it was new, False if `dedup_key` already existed.

    Dedup is at the database level rather than in code, so two webhook deliveries
    racing each other cannot both win.
    """
    n = db.x("""INSERT INTO job_queue (kind, phone, payload, dedup_key, run_after)
                VALUES (%s,%s,%s,%s, now() + (%s * interval '1 second'))
                ON CONFLICT (dedup_key) DO NOTHING""",
             (kind, phone, json.dumps(payload), dedup_key, delay_seconds))
    return n == 1


def claim():
    """Take the next runnable job, or None.

    `FOR UPDATE SKIP LOCKED` lets several workers pull from the same table without
    ever handing the same row to two of them.

    The NOT EXISTS clause is the per-phone serialisation: a job is only claimable if
    no other job for that phone is already running. Without it, two quick messages
    from one person would be answered concurrently from the same stale state.
    """
    rows = db.q("""
        UPDATE job_queue SET status=%s, claimed_at=now(), attempts=attempts+1,
                             updated_at=now()
        WHERE id = (
            SELECT j.id FROM job_queue j
            WHERE j.status = %s
              AND j.run_after <= now()
              AND (j.phone IS NULL OR NOT EXISTS (
                    SELECT 1 FROM job_queue r
                    WHERE r.phone = j.phone AND r.status = %s))
            ORDER BY j.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, phone, payload, attempts, max_attempts
    """, (RUNNING, QUEUED, RUNNING))
    return rows[0] if rows else None


def complete(job_id):
    db.x("UPDATE job_queue SET status=%s, updated_at=now() WHERE id=%s",
         (DONE, job_id))


def fail(job, error):
    """Retry with exponential backoff, or give up.

    A job that has exhausted its attempts is marked failed and LEFT IN THE TABLE.
    Deleting it would hide the fact that a customer message was never answered --
    the one failure in this system that a person is waiting on.
    """
    attempts = job["attempts"]
    err = str(error)[:1000]
    if attempts >= job["max_attempts"]:
        db.x("""UPDATE job_queue SET status=%s, last_error=%s, updated_at=now()
                WHERE id=%s""", (FAILED, err, job["id"]))
        return False
    # 30s, 60s, 120s, 240s...
    backoff = 30 * (2 ** (attempts - 1))
    # ...EXCEPT for a provider that is merely busy. On 2026-08-02 Anthropic
    # returned 529 Overloaded mid-conversation; the buyer got nothing for two
    # minutes and typed "You there ?". Thirty seconds is an eternity to somebody
    # watching a chat, and an overloaded API is usually fine seconds later. Retry
    # those almost immediately and keep the long backoff for real faults.
    if _TRANSIENT.search(err):
        backoff = min(5 * attempts, 20)
    db.x("""UPDATE job_queue SET status=%s, last_error=%s, claimed_at=NULL,
                                 run_after = now() + (%s * interval '1 second'),
                                 updated_at=now()
            WHERE id=%s""", (QUEUED, err, backoff, job["id"]))
    return True


def reclaim_stale():
    """Return jobs whose worker died mid-flight to the queue.

    A worker can be killed by a deploy, an OOM or a Railway restart at any moment.
    Without this the job sits in 'running' for ever and the customer is never
    answered -- silently, because nothing errored.
    """
    return db.x("""UPDATE job_queue
                   SET status=%s, claimed_at=NULL, updated_at=now()
                   WHERE status=%s
                     AND claimed_at < now() - (%s * interval '1 second')""",
                (QUEUED, RUNNING, LEASE_SECONDS))


def stats():
    rows = db.q("""SELECT status, count(*) AS n FROM job_queue GROUP BY status""") or []
    out = {r["status"]: r["n"] for r in rows}
    oldest = db.q("""SELECT min(created_at) AS t FROM job_queue
                     WHERE status=%s""", (QUEUED,), one=True)
    failed = db.q("""SELECT id, kind, phone, attempts, last_error, updated_at
                     FROM job_queue WHERE status=%s
                     ORDER BY id DESC LIMIT 20""", (FAILED,))
    return {"counts": out,
            "oldest_queued_at": (oldest or {}).get("t"),
            "recent_failures": failed}

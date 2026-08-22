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

import config
import db

# The provider is busy, not broken. Worth retrying in seconds rather than minutes.
_TRANSIENT = re.compile(
    r"overloaded|529|rate.?limit|429|timeout|timed out|temporarily unavailable|"
    # Our OWN hourly allowance, not the provider's. Added 2026-08-22 with
    # sequencer.RateCapped: an hour of saturation outlasts the default ladder
    # (30s..240s, five attempts), whereas the transient one stretches to ~33
    # minutes over eight -- which is what a buyer waiting on an answer needs.
    r"rate.?capped|hourly cap|"
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


def enqueue_inbound(payload, phone, dedup_key=None):
    """Add an inbound turn, MERGING it into one already waiting for the same person.

    Returns "merged" if it was folded into a pending job, "queued" if a new job was
    created, or "dup" if `dedup_key` had already been used.

    WHY. People text in fragments. Measured 2026-08-11: 151 of 323 model calls -- 47%
    -- fired within 90 seconds of the previous one for the same person. Lead 1016 sent
    "Ok", "Call me", "Fast" inside thirty seconds and got three separate paragraphs
    back, each with its own retrieval and its own model call, each answering a third
    of a thought.

    Merging is strictly better on every axis. One model call instead of three, and the
    model sees the WHOLE message before answering, so "Call me / Fast / Only 2
    minutes" reads as one urgent request rather than three unrelated ones. It also
    reads more like a person: nobody fires three replies at three fragments.

    NOT A DEDUP. Every fragment is kept, joined by a newline, because the fragments
    together are the message -- dropping any of them would lose what they said.

    ONLY MERGES INTO A `queued` JOB. A running job has already read its payload, so
    appending to it would silently discard the new text.

    A race between two webhooks can still produce two jobs -- both may find nothing to
    merge into and both insert. That is exactly today's behaviour, so the worst case is
    no worse than before; it is not worth a lock to close.
    """
    text = (payload or {}).get("text") or ""

    # Idempotency has to survive merging: a merge inserts no row, so the
    # ON CONFLICT (dedup_key) that used to guarantee this cannot fire. Claimed
    # against the existing processed_webhooks table rather than a new one -- it is
    # the same question ("have I already handled this message id?") and the caller
    # already uses it, so a redelivery cannot append the same sentence twice.
    if dedup_key and not db.mark_webhook_new(dedup_key):
        return "dup"

    if text and phone:
        merged = db.q("""
            UPDATE job_queue
               SET payload = jsonb_set(
                       jsonb_set(payload, '{text}',
                                 to_jsonb(COALESCE(payload->>'text', '')
                                          || E'\\n' || %s::text)),
                       '{merged}',
                       to_jsonb(COALESCE((payload->>'merged')::int, 1) + 1)),
                   updated_at = now()
             WHERE id = (SELECT j.id FROM job_queue j
                          WHERE j.phone = %s AND j.kind = %s AND j.status = %s
                          ORDER BY j.id DESC
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1)
            RETURNING id""", (text, phone, KIND_INBOUND, QUEUED)) or []
        if merged:
            return "merged"

    n = db.x("""INSERT INTO job_queue (kind, phone, payload, dedup_key, run_after)
                VALUES (%s,%s,%s,%s, now())
                ON CONFLICT (dedup_key) DO NOTHING""",
             (KIND_INBOUND, phone, json.dumps(payload), None))
    return "queued" if n == 1 else "dup"


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


def retry_plan(attempts, err, max_attempts):
    """(give_up, backoff_seconds) for a job that just failed. Pure, so it is testable.

    `attempts` is the count INCLUDING the one that just failed.

    TWO LADDERS, because "the provider is busy" and "this code throws" want opposite
    treatment. A real fault should be retried a few times, slowly, and then left
    alone. A busy provider should be retried quickly at first -- a buyer is watching
    a chat window -- and then patiently, because the outage outlives the hiccup.

    2026-08-18 is why the patient tail exists. Anthropic returned 529 Overloaded for
    two buyers; the transient rule at the time SHORTENED the wait (5s, 10s, 15s, 20s)
    so all five attempts burned inside one minute -- the same minute the provider was
    saturated -- and the bot gave up for good. Nothing re-runs a dead job, so both
    people simply never heard back. The old rule was written for OUR rate limits,
    where retrying sooner is right because the block is per-second. Applied to a
    provider-wide outage it is exactly backwards.
    """
    if _TRANSIENT.search(err or ""):
        ladder = config.JOB_BACKOFF_TRANSIENT
        # Never LOWER a ceiling the row asked for; only raise it for this case.
        ceiling = max(max_attempts, config.JOB_MAX_ATTEMPTS_TRANSIENT)
        if attempts >= ceiling:
            return True, 0
        return False, ladder[min(attempts - 1, len(ladder) - 1)]

    if attempts >= max_attempts:
        return True, 0
    return False, 30 * (2 ** (attempts - 1))          # 30s, 60s, 120s, 240s...


def fail(job, error):
    """Retry per retry_plan, or give up.

    A job that has exhausted its attempts is marked failed and LEFT IN THE TABLE.
    Deleting it would hide the fact that a customer message was never answered --
    the one failure in this system that a person is waiting on.
    """
    err = str(error)[:1000]
    give_up, backoff = retry_plan(job["attempts"], err, job["max_attempts"])
    if give_up:
        db.x("""UPDATE job_queue SET status=%s, last_error=%s, updated_at=now()
                WHERE id=%s""", (FAILED, err, job["id"]))
        return False
    db.x("""UPDATE job_queue SET status=%s, last_error=%s, claimed_at=NULL,
                                 run_after = now() + (%s * interval '1 second'),
                                 updated_at=now()
            WHERE id=%s""", (QUEUED, err, backoff, job["id"]))
    return True


# A dead job may only be put back if a reply could still legally reach the buyer.
# WhatsApp allows a free-text message for 24h after THEIR last message; past that a
# replay would burn a turn producing something that cannot be delivered, and answer
# a question the buyer asked yesterday as though no time had passed.
REPLAY_MAX_AGE_HOURS = 20


def replay(job_id=None):
    """Put failed job(s) back in the queue. Returns the ids actually revived.

    THE MISSING SECOND CHANCE (2026-08-19). `fail()` was terminal and nothing
    anywhere re-ran a dead job -- `/api/queue` only DISPLAYED them. So on
    2026-08-18, when two jobs gave up against a 529, both buyers were silent for
    good unless a human opened the Wati inbox and typed a reply by hand.

    Deliberately manual, and deliberately not part of the worker loop. An automatic
    resurrection would re-run whatever killed the job -- for a real bug that is an
    infinite loop dressed as a feature -- and it would put a message in front of a
    buyer with nobody having looked at why the first attempt died.

    `attempts` resets to 0, so a replayed job gets a full ladder rather than landing
    on the last rung and dying again immediately.
    """
    rows = db.q(f"""UPDATE job_queue
                    SET status=%s, attempts=0, claimed_at=NULL, run_after=now(),
                        updated_at=now()
                    WHERE status=%s
                      AND created_at > now() - interval '{REPLAY_MAX_AGE_HOURS} hours'
                      AND (%s IS NULL OR id = %s)
                    RETURNING id, kind, phone""",
                (QUEUED, FAILED, job_id, job_id)) or []
    return rows


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

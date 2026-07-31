"""The worker process (task 13).

Run as its own Railway service, same repo, same database:

    python worker.py

Or, while volume is small, inside the web process by setting WORKER_IN_PROCESS=true
-- see `start_in_thread()`. Both paths run the identical loop, so moving from one to
the other is an environment change, not a code change.

WHY IT IS A SEPARATE ENTRYPOINT
-------------------------------
The web process must answer WhatsApp in milliseconds. This one may take as long as it
needs. Keeping them separate means a slow turn can never make the webhook look broken
to Wati, and a deploy that restarts the web process does not abandon a half-finished
conversation -- the job is still in the table with its lease expired, and gets picked
up again.

WHAT IT DOES NOT DO
-------------------
It does not decide whether a message is an opt-out. That runs synchronously in the
webhook, because a person who types STOP must be recorded immediately regardless of
how deep the backlog is.
"""
import logging
import os
import signal
import threading
import time

import conversation
import db
import handoff
import jobs
import qualifier
import sequencer

log = logging.getLogger("worker")

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
RECLAIM_EVERY = 60  # seconds between stale-job sweeps

_stop = threading.Event()


def handle(job):
    """Dispatch one job. Raising means retry-with-backoff; returning means done."""
    if job["kind"] == jobs.KIND_INBOUND:
        return _handle_inbound(job)
    raise ValueError(f"unknown job kind: {job['kind']}")


HISTORY_TURNS = 20


def _handle_inbound(job):
    """One customer message: retrieve, think, reply, route.

    Order matters and is not arbitrary. The reply is sent BEFORE the exit is
    routed, because a qualified lead should read "a colleague will call you"
    before the salesperson's phone buzzes -- and because a handoff that fails
    must not swallow the buyer's answer.
    """
    payload = job["payload"] or {}
    phone = job.get("phone")
    text = payload.get("text")

    lead = db.q("""SELECT * FROM leads WHERE phone=%s
                   ORDER BY updated_at DESC LIMIT 1""", (phone,), one=True)
    if not lead:
        db.log_msg(None, "in", "unattributed", text,
                   detail=f"phone={phone} worker: no lead")
        return

    if not qualifier.configured():
        db.log_msg(lead["id"], "in", "queued_turn", text,
                   detail="ANTHROPIC_API_KEY not set; message recorded, no reply")
        return

    conv = conversation.get_or_create(lead)

    # Already finished. A lead that was marked dead or handed to a human must not
    # be re-engaged by the bot -- that decision belongs to a person now.
    if conv.get("outcome"):
        db.log_msg(lead["id"], "in", "post_outcome", text,
                   detail=f"conversation already {conv['outcome']}; needs a human")
        return

    turns = db.q("""SELECT direction, body FROM message_log
                    WHERE lead_id=%s AND msg_type IN ('inbound','qualifier_turn')
                    ORDER BY id DESC LIMIT %s""", (lead["id"], HISTORY_TURNS)) or []
    history = list(reversed(turns))

    decision = qualifier.run_turn(lead, text, history=history, conv=conv)

    sent = sequencer._send(lead, "qualifier_turn", body=decision["reply"],
                           sources=decision.get("sources"))
    if not sent:
        # The gate refused, or the send failed. Do NOT advance the checklist or
        # route an exit off a message the buyer never received.
        log.warning("lead %s: reply not delivered; state unchanged", lead["id"])
        return

    conv = conversation.record_turn(conv, decision,
                                    decision.get("gate_asked"),
                                    decision.get("framing_used"))

    # Three asks, no answers. A person should look -- the bot keeps answering.
    if conv.get("_newly_flagged"):
        handoff.notify_human_flagged(lead, conv)

    handoff.route(lead, conv, decision)


def run_once():
    """Claim and run at most one job. Returns True if one was processed."""
    job = jobs.claim()
    if not job:
        return False
    try:
        handle(job)
        jobs.complete(job["id"])
        return True
    except Exception as e:
        # Broad by design: a worker that dies on an unexpected exception stops
        # answering every customer, not just this one. The job carries its own
        # retry budget and lands in `failed` when that runs out.
        log.exception("job %s failed", job["id"])
        retrying = jobs.fail(job, e)
        log.warning("job %s: %s", job["id"],
                    "will retry" if retrying else "GAVE UP -- left in table as failed")
        return True


def loop():
    log.info("worker started (poll=%ss, lease=%ss)", POLL_SECONDS, jobs.LEASE_SECONDS)
    last_reclaim = 0.0
    while not _stop.is_set():
        try:
            now = time.monotonic()
            if now - last_reclaim > RECLAIM_EVERY:
                n = jobs.reclaim_stale()
                if n:
                    log.warning("reclaimed %s stale job(s) from a dead worker", n)
                last_reclaim = now

            # Drain rather than sleeping between every job: a backlog should clear
            # at full speed, not at one job per poll interval.
            worked = False
            for _ in range(50):
                if _stop.is_set() or not run_once():
                    break
                worked = True
            if not worked:
                _stop.wait(POLL_SECONDS)
        except Exception:
            # Never let the loop itself die -- a crashed worker is a silently
            # unanswered inbox.
            log.exception("worker loop error")
            _stop.wait(POLL_SECONDS)
    log.info("worker stopped")


def start_in_thread():
    """Run the loop inside the web process. For low volume only.

    Identical loop, so the eventual move to a separate service is an env change.
    The tradeoff to be honest about: a deploy restarts both at once, and both share
    one container's CPU. Fine at tens of messages a minute; not fine under load.
    """
    t = threading.Thread(target=loop, name="worker", daemon=True)
    t.start()
    return t


def _shutdown(signum, _frame):
    # Finish the job in hand rather than abandoning it mid-conversation.
    log.info("signal %s -- finishing current job then exiting", signum)
    _stop.set()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    db.init_db()
    loop()

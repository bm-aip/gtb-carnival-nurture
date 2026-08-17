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

import answering
import config
import conversation
import db
import handoff
import jobs
import qualifier
import sequencer
import wati

log = logging.getLogger("worker")

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
RECLAIM_EVERY = 60  # seconds between stale-job sweeps

_stop = threading.Event()


def handle(job):
    """Dispatch one job. Raising means retry-with-backoff; returning means done."""
    if job["kind"] == jobs.KIND_INBOUND:
        return _handle_inbound(job)
    if job["kind"] == jobs.KIND_CTWA_CAPTURE:
        return _handle_ctwa_capture(job)
    raise ValueError(f"unknown job kind: {job['kind']}")


def _handle_ctwa_capture(job):
    """Fetch and store Meta's click id for one ad arrival.

    Sends nothing. Touches only the attribution columns, so it can never interfere
    with a conversation that is running at the same time.

    Raising here is correct on a transport failure: the queue retries with backoff,
    and Wati keeps the message history, so a click id is recoverable for as long as
    Meta's own event window lasts. Writing down "no referral" because Wati was
    briefly rate-limiting would throw away something still gettable.
    """
    payload = job["payload"] or {}
    lead_id = payload.get("lead_id")
    phone = payload.get("phone")
    if not (lead_id and phone):
        return

    ref = wati.fetch_referral(phone)          # raises -> retry with backoff
    if not ref or not (ref.get("ctwa_clid") or ref.get("source_id")):
        # A genuine negative: this conversation did not start from a Meta ad click.
        # Recorded so the backfill does not keep asking about the same walk-in.
        db.x("""UPDATE leads SET ctwa_looked_at=now(), updated_at=now()
                WHERE id=%s""", (lead_id,))
        return

    db.x("""UPDATE leads
               SET ctwa_clid       = COALESCE(%s, ctwa_clid),
                   ctwa_source_id  = COALESCE(%s, ctwa_source_id),
                   ctwa_source_url = COALESCE(%s, ctwa_source_url),
                   ctwa_headline   = COALESCE(%s, ctwa_headline),
                   ctwa_captured_at = now(),
                   ctwa_looked_at  = now(),
                   inflow          = 'ctwa',
                   updated_at      = now()
             WHERE id = %s""",
         (ref.get("ctwa_clid"), ref.get("source_id"), ref.get("source_url"),
          ref.get("headline"), lead_id))

    # COALESCE above, not plain assignment: a second lookup for the same person must
    # never blank a click id we already hold. The first click is the one Meta will
    # match, and re-clicking a different ad later must not overwrite it.
    db.log_msg(lead_id, "in", "ctwa_captured", None,
               detail=f"ad={ref.get('source_id')} url={ref.get('source_url')} "
                      f"clid_len={len(ref.get('ctwa_clid') or '')}")


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

    # THE ALLOW-LIST GATE. Owner 2026-07-31: "we use this bot only for the leads
    # from one campaign - not all". Checked HERE, on the reply path, not only at
    # intake -- the 563 carnival-era leads already in the table predate the
    # allow-list, and any of them could message the number tomorrow.
    #
    # Fails closed: a lead with no campaign is not messaged.
    if not config.campaign_allowed(lead.get("project"), lead.get("campaign")):
        db.log_msg(lead["id"], "in", "out_of_scope", text,
                   detail=f"campaign={lead.get('campaign')!r} not in allow-list; "
                          f"recorded, no reply")
        return

    # STALE JOB. A turn that failed and came back later must not answer a question
    # the conversation has already moved past.
    #
    # 2026-08-02: Anthropic returned 529 for "ECR mostly but considering omr too".
    # The buyer waited, typed "You there ?", and the NEXT turn answered them with
    # the OMR message already visible in history -- location was captured
    # correctly. The failed job then sat queued, due to retry. Left alone it would
    # have replied to a ninety-minute-old message, out of context, as a second
    # voice in a conversation that had moved on.
    #
    # If we have already spoken since this job was created, it is redundant: the
    # message it carries was in history when we spoke.
    answered_since = db.q("""SELECT 1 FROM message_log
                             WHERE lead_id=%s AND direction='out'
                               AND msg_type='qualifier_turn' AND ok
                               AND ts > %s LIMIT 1""",
                          (lead["id"], job.get("created_at")), one=True)
    if answered_since and job.get("attempts", 1) > 1:
        db.log_msg(lead["id"], "in", "stale_turn", text,
                   detail=f"job {job.get('id')} retried after we had already "
                          f"replied; message was in history, not answering twice")
        return

    if not qualifier.configured():
        db.log_msg(lead["id"], "in", "queued_turn", text,
                   detail="ANTHROPIC_API_KEY not set; message recorded, no reply")
        return

    conv = conversation.get_or_create(lead)

    # NO OUTCOME STOPS THE BOT. An outcome records what we learned about somebody;
    # it is not a door closing on a person who is still typing.
    #
    # This check has been removed three times, once per outcome, and each removal was
    # prompted by a real buyer getting silence:
    #
    #   QUALIFIED  2026-08-01. The bot asked "would you like me to pencil in a site
    #              visit?", the buyer answered "Sunday would be perfect", and got
    #              nothing -- qualifying had already closed the conversation. Asking
    #              a question and then refusing to hear the answer is worse than
    #              never asking. Owner: "it cant become cold - it has to wean off in
    #              a gentle way ... we shouldnt drop the ball untill we know sales is
    #              really ON it."
    #   ESCALATED  2026-08-02. A price question escalated, the buyer then asked what
    #              amenities the place has -- ordinary and answerable -- and got
    #              silence. Escalating means a human picks up the hard part; it does
    #              not mean the buyer stops being a person mid-sentence.
    #   DEAD       2026-08-03. The last one. A budget below the entry price no longer
    #              reaches `dead` at all (it is `nurture` now), and what remains --
    #              wrong city, a product we do not sell -- still does not justify
    #              refusing to answer someone who writes back. There is nothing to
    #              sell them; that is not a reason to be rude.
    #
    # Three instances of one wrong idea: that an outcome is an ending. It is a label.
    # Silence now has exactly two legitimate sources, and both are explicit acts by a
    # person: the buyer sends STOP (optout.py, enforced in sendgate), or an operator
    # pauses the bot in Wati. Do not add a third.

    # A BARE ACKNOWLEDGEMENT AFTER HANDOFF NEEDS NO MODEL.
    #
    # This is still a reply -- it does not add a third source of silence, and the rule
    # above stands. It just refuses to spend a model call and a paragraph of amenities
    # on the word "Ok". Lead 1016 answered "Ok", "Hm" and "By" and each got a fresh
    # fact about the clubhouse plus another push to book a visit.
    #
    # GATED ON THE CONVERSATION ALREADY BEING HANDED OVER, deliberately. Before
    # handoff, "ok" and "sure" are often real ANSWERS -- to "shall I pencil in
    # Sunday?", to "would you like someone to call?" -- and short-circuiting those
    # would drop a booking on the floor. Once a human owns the conversation there is
    # no outstanding question a one-word reply could be answering.
    if answering.is_bare_acknowledgement(text) and conv \
            and conv.get("outcome") in config.HANDED_OFF_OUTCOMES:
        sequencer._send(lead, "qualifier_ack", body=config.ACK_REPLY)
        db.log_msg(lead["id"], "in", "ack_shortcircuit", text,
                   detail=f"outcome={conv.get('outcome')}; fixed reply, no model call")
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

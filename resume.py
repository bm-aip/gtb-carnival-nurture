"""When the model is down, tell the buyer we heard them -- without the model.

WHAT HAPPENED ON 2026-09-09. The bot's Anthropic key hit a zero credit balance:

    Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error',
    'message': 'Your credit balance is too low to access the Anthropic API. ...'}}

Two people had messaged us. Meshak tapped "Need More Details" on one of our own
templates at 08:58 -- the strongest buying signal this system gets -- and heard
nothing at all until a human noticed six hours later. The alert card fired within
the hour and named him, which is why he was recovered; nothing else would have.

THE PART THAT IS NOT A BUG. `jobs.replay()` is manual on purpose, and its
docstring is right about why: an automatic resurrection re-runs whatever killed
the job, which for a real fault is an infinite loop dressed as a feature.

THE PART THAT IS. "The provider is down" is not a fault in our code, and it ends
by itself. Owner, 2026-09-09: *"I don't need an alarm but a resume step -- every
hour from the outage first surfaced it should poll the api key, if it serves we
should resume the service"*. That threads the needle exactly: do not blindly
re-run, ASK THE PROVIDER FIRST, and resume only once it actually answers.

So this module holds two things, and the split matters:

    hold()    the moment we give up on a buyer's message, send them one line of
              PLAIN CODE -- no model call, because the model is the thing that is
              down. A buyer who is told we heard them is waiting; a buyer who
              hears nothing has been dropped.

    (pass 2)  an hourly probe while anyone is waiting, and the replay that
              answers them all when it succeeds.

WHY THE NOTE FIRES AT GIVE-UP AND NOT AT THE FIRST ERROR. The job ladder retries
for about eight minutes before it gives up. A thirty-second provider hiccup
therefore never produces one of these -- the buyer just gets their answer, a
little late, and never learns anything was wrong. They only hear from us when the
thing is genuinely stuck.
"""
import logging
import os
import re
from datetime import datetime

import config
import db
import jobs
import picker
import sendgate
import sequencer

log = logging.getLogger("resume")

MSG_TYPE = "holding_note"

# WHAT THE BUYER READS. Owner's own words, 2026-09-09, and he cut the reason on
# purpose: *"dont need to say technical issue or something"*. A buyer does not
# care whose API is down and telling them invites a question nobody is there to
# answer -- the model being down is exactly why this line exists.
#
# HARDCODED, NOT A TEMPLATE AND NOT A MODEL CALL. Everything clever in this system
# needs the thing that has just failed. This is the one message that must survive
# the model, the knowledge base and the embedding service all being unreachable,
# so it is a constant in a file.
HOLDING_TEXT = "Got your message. We'll be back to you shortly."

# IS THIS THEIR PROBLEM OR OURS?
#
# Only a PROVIDER-LEVEL failure may trigger a holding note and, in pass 2, an
# automatic replay: it ends on its own, so waiting is the right thing to do. A
# fault in our own code does not end on its own, and replaying it is the infinite
# loop `jobs.replay()` warns about. Anything unmatched here behaves exactly as it
# did before this module existed.
#
# `credit balance` arrives as a 400 -- an "invalid_request_error" -- so it does
# NOT match jobs._TRANSIENT and never will: as far as the API is concerned the
# request really was invalid. That is why the 2026-09-09 jobs took the fast ladder
# (five tries in about eight minutes) and died. A wider net here does not change
# the ladder; it changes what we tell the buyer and what we do afterwards.
_OUTAGE = re.compile(
    r"credit balance|billing|quota|insufficient.?(funds|credit)|"
    r"overloaded|529|"
    r"rate.?limit|429|"
    # Auth: a revoked or expired key is an outage in every way that matters here
    # -- our messages stop, and only a human can end it.
    r"401|403|authentication.?error|permission.?error|invalid.?x-api-key|"
    r"timeout|timed out|temporarily unavailable|502|503|"
    r"connection reset|connection aborted|service unavailable", re.I)


def is_outage(error):
    """True if this failure is the provider being unavailable, not our bug."""
    return bool(_OUTAGE.search(str(error or "")))


# How long a holding note stands for. Matches jobs.REPLAY_MAX_AGE_HOURS: past that
# window WhatsApp will not carry a free-text reply anyway, so a second note could
# not be followed by an answer even if we sent one.
NOTE_HOLDS_HOURS = 20


def _already_told(phone):
    """Have we already told this person we will be back?

    ONCE PER PERSON, not once per message. Somebody who sends three messages
    during an outage has one conversation with us, and three identical "we'll be
    back" lines is the bot looking broken in a new way.

    Keyed on PHONE, like every other guard in this system: `leads` is UNIQUE
    (project, selldo_lead_id), so one human is routinely several rows.
    """
    r = db.q("""SELECT 1 FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                 WHERE l.phone = %s AND ml.direction = 'out'
                   AND ml.msg_type = %s AND ml.ok
                   AND ml.ts > now() - (%s || ' hours')::interval
                 LIMIT 1""", (phone, MSG_TYPE, NOTE_HOLDS_HOURS), one=True)
    return bool(r)


def hold(job, error):
    """One buyer's message just died on a provider outage. Tell them we heard it.

    Called from worker.run_once() at GIVE-UP, i.e. only once the retry ladder is
    spent. Returns True if a note actually went out.

    Everything here is best-effort and swallowed: this runs inside the handler for
    a job that has already failed, and an exception raised here would replace a
    buyer's missing answer with a worker crash.
    """
    try:
        if not is_outage(error):
            return False
        # Inbound messages only. Nobody is sitting in a chat window waiting on a
        # click-id fetch, and a holding note about one would be nonsense.
        if job.get("kind") != "inbound_message":
            return False
        phone = job.get("phone")
        if not phone:
            return False

        # THE OUTAGE CLOCK STARTS AT THE FIRST BUYER WE FAILED, not at the first
        # error. Pass 2 probes from this moment and gives up 12 hours after it.
        # Written before the send, so an outage is on record even if the note
        # itself cannot go out.
        if not db.get_setting("outage_since"):
            db.set_setting("outage_since", sequencer.now_ist().isoformat())
            log.warning("provider outage opened: %s", str(error)[:120])

        if _already_told(phone):
            return False

        lead = db.q("""SELECT * FROM leads WHERE phone=%s
                       ORDER BY updated_at DESC LIMIT 1""", (phone,), one=True)
        if not lead:
            return False

        # A REPLY, NOT A BROADCAST. This goes out as free session text inside the
        # 24h window the buyer opened by messaging us seconds ago, so it costs
        # nothing with Meta and needs no template. `msg_type` is deliberately not
        # `knock*` and not one of wati.COLD_FIRST_TOUCH: it must not consume the
        # daily tier, must not be held until morning by quiet hours -- somebody
        # who writes at 11pm and gets silence has been dropped at 11pm -- and must
        # not eat this person's fatigue allowance for a message they did not want.
        #
        # It still leaves through sequencer._send like everything else, so the
        # send gate can still refuse it: an opted-out person is not owed an
        # apology for silence they asked for.
        ok = sequencer._send(lead, MSG_TYPE, body=HOLDING_TEXT)
        if ok:
            log.info("lead %s told we are down and will be back", lead["id"])
        return bool(ok)
    except Exception:                                  # noqa: BLE001
        log.exception("holding note failed for job %s", (job or {}).get("id"))
        return False


# --------------------------------------------------------------------------
# PASS 2: probe the provider, and resume the service when it answers
# --------------------------------------------------------------------------
#
# Owner, 2026-09-09: *"every hour from the outage first surfaced it should poll
# the api key, if it serves we should resume the service"*, and *"if it is an api
# token exhaust case - max 12 hours - by then we should fix it"*.

# This lane messages BUYERS -- a replayed job ends in a reply going out -- so its
# picker ends on the send gate like every other one. See tests/one_gate.py.
SENDS_TO_BUYER = True

# How often to ask the provider whether it is back. Asked ONLY while somebody is
# actually waiting, so a healthy day costs nothing but one settings read a minute.
PROBE_EVERY_MIN = int(os.environ.get("RESUME_PROBE_EVERY_MIN", "60"))

# When to stop asking and hand it to a person. Owner's number: past this it is not
# a blip, it is something only a human can clear -- a card to buy, a key to rotate.
#
# WHAT THIS COSTS, said plainly: between this deadline and the 20-hour WhatsApp
# window there are eight hours in which a recovery would still have been legal and
# nobody will be looking. That is the owner's call and a reasonable one -- an
# outage still running at hour twelve does not fix itself at hour thirteen.
GIVE_UP_AFTER_HOURS = int(os.environ.get("RESUME_GIVE_UP_AFTER_HOURS", "12"))

# What the buyer reads first when their answer finally arrives. Owner's words.
# Plain code again: the model writes the answer, it does not write the manners.
APOLOGY = "Sorry for the delay!"

_SINCE = "outage_since"                # set by hold(), cleared when we recover
_LAST_PROBE = "outage_last_probe_at"
_GAVE_UP = "outage_gave_up"            # so the 12-hour card is sent once, not hourly

# How many buyers one resumed pass will put back. Far above any real outage cohort
# (the 2026-09-09 outage produced two), and bounded so a pathological queue cannot
# dump a thousand model calls into one minute.
BATCH = int(os.environ.get("RESUME_BATCH", "50"))


def probe():
    """Ask the provider, in the cheapest way there is, whether it is serving.

    Returns (ok, detail). About thirteen tokens.

    THE WHOLE DESIGN RESTS ON THIS CALL. `jobs.replay()` is manual because an
    automatic resurrection re-runs whatever killed the job. Asking first answers
    that objection: nothing is replayed until the provider has, this second,
    answered a real request. Nothing is inferred from a clock.

    `anthropic` is imported HERE rather than at the top of this module, and the
    holding-note path above must never need it: if the SDK itself were the broken
    thing, a module-scope import would take down the one message that is supposed
    to survive an outage.
    """
    try:
        import anthropic
        import qualifier                                # for the model name only
        client = anthropic.Anthropic()
        client.messages.create(model=qualifier.MODEL, max_tokens=5,
                               messages=[{"role": "user", "content": "hi"}])
        return True, "ok"
    except Exception as e:                              # noqa: BLE001
        return False, str(e)[:200]


def _outage_age_hours():
    raw = db.get_setting(_SINCE)
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (datetime.now(started.tzinfo) - started).total_seconds() / 3600.0


def due(limit=None):
    """The buyers waiting on a resumed service. Longest-waiting first.

    A job is here only if every one of these holds:

      * it gave up (`status='failed'`) and it was an inbound message -- something a
        person is waiting on, not a click-id fetch nobody is watching
      * it died on a PROVIDER outage, not on a fault of ours. Our own bugs are
        never replayed: that is the infinite loop jobs.replay() warns about
      * it is still inside the WhatsApp window, so an answer can legally arrive
      * THE SEND GATE WOULD ALLOW IT. Somebody who opted out during the outage must
        not be answered because a queue remembered them, and a number that cannot
        receive WhatsApp is not worth a model call.

    Side-effect free, so a monitor may call it.
    """
    def fetch(page, offset):
        return db.q("""SELECT id, kind, phone, last_error, created_at
                         FROM job_queue
                        WHERE status = %s AND kind = %s
                          AND created_at > now() - (%s || ' hours')::interval
                        ORDER BY created_at ASC
                        LIMIT %s OFFSET %s""",
                    (jobs.FAILED, jobs.KIND_INBOUND, jobs.REPLAY_MAX_AGE_HOURS,
                     page, offset))

    claimed = set()

    def select(row):
        if not is_outage(row.get("last_error")):
            return None
        phone = row.get("phone")
        if not phone or phone in claimed:
            return None
        # ONE HUMAN, ONE ANSWER. Somebody who wrote three times during the outage
        # has one conversation; replaying all three would answer them three times
        # over. The oldest is kept because it is the question they asked first.
        allowed, _reason = sendgate.would_allow(phone, "qualifier_turn")
        if not allowed:
            return None
        claimed.add(phone)
        return row

    return picker.scan(fetch, select, limit or BATCH)


def run():
    """One pass: probe if it is time, and resume the service if it answers.

    Returns how many buyers were put back in the queue.
    """
    if not db.get_setting(_SINCE):
        return 0                     # nothing is broken; a healthy tick costs one read

    waiting = due()
    if not waiting:
        # Everybody has been answered, or has aged out of the window while we were
        # down. Either way this outage is over as far as this lane can act on it.
        _close("no buyer is still waiting")
        return 0

    age = _outage_age_hours()
    if age is not None and age >= GIVE_UP_AFTER_HOURS:
        # ONCE, NOT HOURLY. An alert that repeats is an alert people mute, and a
        # muted alert is worse than none because it still looks like coverage.
        if db.get_setting(_GAVE_UP) != "true":
            db.set_setting(_GAVE_UP, "true")
            _alert("STILL DOWN after %dh - %d buyer(s) waiting" % (int(age), len(waiting)),
                   ", ".join(str(r["phone"]) for r in waiting[:5]),
                   "Fix the provider (credit or key), then POST /api/queue/replay. "
                   "After %dh from their message WhatsApp will not carry a reply and "
                   "it has to be the Wati inbox by hand."
                   % jobs.REPLAY_MAX_AGE_HOURS)
        return 0

    if not _probe_due():
        return 0
    db.set_setting(_LAST_PROBE, sequencer.now_ist().isoformat())

    ok, detail = probe()
    if not ok:
        log.info("provider still down after %.1fh: %s", age or 0, detail)
        return 0

    # IT ANSWERED. Put every waiting buyer back in the queue, each marked so their
    # reply opens with an apology rather than pretending no time has passed.
    revived = []
    for row in waiting:
        _mark_delayed(row["id"])
        revived += jobs.replay(row["id"])
    _close("provider answered")
    log.warning("service resumed after %.1fh, %d buyer(s) requeued",
                age or 0, len(revived))
    _alert("SERVICE RESUMED - %d buyer(s) being answered now" % len(revived),
           ", ".join(str(r["phone"]) for r in waiting[:5]),
           "They each get an apology for the delay and then their answer. "
           "No action needed.")
    return len(revived)


def _probe_due():
    """Has PROBE_EVERY_MIN passed since the last probe? No stamp means yes.

    A stamp we cannot parse is treated as due rather than as never: failing the
    other way would leave a broken timestamp holding the recovery shut forever.
    """
    raw = db.get_setting(_LAST_PROBE)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return (datetime.now(last.tzinfo) - last).total_seconds() >= PROBE_EVERY_MIN * 60


def _mark_delayed(job_id):
    """Flag the job so the reply it produces opens with an apology.

    On the PAYLOAD rather than in a side table: the flag must survive the job being
    claimed by another worker process, and it is a fact about this one message, not
    about the person.
    """
    db.x("""UPDATE job_queue
               SET payload = jsonb_set(payload::jsonb, '{delayed}', 'true'::jsonb, true)
             WHERE id = %s""", (job_id,))


def with_apology(reply, payload):
    """The answer, opened with an apology if this message waited out an outage."""
    if not (payload or {}).get("delayed"):
        return reply
    return ("%s %s" % (APOLOGY, reply)).strip()


def _close(why):
    """This outage is over. Clear the clock so the next one starts its own."""
    for key in (_SINCE, _LAST_PROBE, _GAVE_UP):
        db.set_setting(key, "")
    log.info("outage closed: %s", why)


def _alert(headline, detail, action):
    """One card to the team. Best-effort: a failed card must not abort a recovery.

    `handoff` is imported inside the function because it imports the send path,
    which imports this module's neighbours -- and because a card is never allowed
    to be the reason a buyer's answer does not go out.
    """
    try:
        import handoff
        handoff._notify(config.ALERT_PHONES,
                        [handoff._slot(headline),
                         handoff._slot("RON bot"),
                         handoff._slot("system"),
                         handoff._slot(detail),
                         handoff._slot(action)], "resume")
    except Exception:                                   # noqa: BLE001
        log.exception("resume alert failed")

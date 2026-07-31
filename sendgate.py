"""The send gate -- one bouncer at one door.

Every outbound message in the system passes `check()` before it reaches Wati.
There is exactly one caller: `sequencer._send()`. Nothing else may send.

Why a module rather than inline conditions in `_send()`: four safety rules
(opt-out, fatigue, retry ceiling, system state) will land here across Phase 0
tasks 2-4, and the knock engine, the qualifier and the ack path all send. Four
rules x three callers is twelve places to be correct in; one gate is one.

Every check keys on PHONE NUMBER, not lead id. `leads` is
UNIQUE (project, selldo_lead_id), so the schema guarantees one human can be
several rows -- an old-lead reactivation, a website form fill and a CTWA click
are three rows and one person. Lead-keyed safety would be silently broken in
exactly the reactivation case this system is being built for.

Phase 0 task 1a implements the SYSTEM STATE check only. Tasks 2, 3 and 4 add
opt-out, fatigue and the retry ceiling as further checks in `check()`; the call
site in `_send()` does not change again.
"""
import config
import db
import failures
import fatigue
import optout

# Verdict reasons. Stable strings -- they are written to message_log.detail and
# surfaced on the dashboard, so treat them as an interface, not as prose.
OK = "ok"
BLOCKED_DISABLED = "send_disabled"
BLOCKED_PAUSED = "global_pause"
BLOCKED_OPTOUT_GLOBAL = "opted_out_global"
BLOCKED_OPTOUT_PROJECT = "opted_out_project"


def sends_enabled():
    """Master switch. Default OFF.

    This replaces an accident. Until Phase 0 the system was inert only because
    every send loop compared today against the last carnival day
    (`sequencer.py:185`, `:337`, `:363`) -- deleting that dead carnival wiring
    would therefore have RE-ARMED the sender as a side effect. The kill switch
    must exist before the thing that is accidentally acting as one is removed.

    Env var, not a DB setting: a DB row can be changed by anything with a
    connection, and an env var change is a deliberate, logged redeploy.
    """
    return config.SEND_ENABLED


def paused():
    """Operator pause. Either the dashboard toggle or the env override.

    Distinct from `sends_enabled()` on purpose: this one is a runtime brake an
    operator can pull mid-incident without a redeploy, and it is expected to be
    toggled. The master switch is a build-state assertion and is expected to be
    flipped exactly once, when the new engine is ready to send.
    """
    return (db.get_setting("global_pause", "false") == "true") or config.GLOBAL_PAUSE_ENV


def check(phone, msg_type, project=None):
    """Return (allowed: bool, reason: str) for one outbound message.

    Order is cheapest-and-most-absolute first: the master switch needs no
    database at all, so in the state this system will sit in for weeks a blocked
    send costs one boolean. The opt-out ledger is queried only once we would
    otherwise really be sending.

    `project` was added in task 2, so this signature is NOT the frozen thing task
    1a claimed it would be. It is required rather than optional in spirit: opt-out
    has a per-project scope, and a caller that cannot say which project it is
    sending for is treated as blocked by ANY project-scoped opt-out rather than
    being waved through. Passing None is safe; it is never permissive.
    """
    if not sends_enabled():
        return False, BLOCKED_DISABLED
    if paused():
        return False, BLOCKED_PAUSED

    blocked, scope = optout.is_blocked(phone, project)
    if blocked:
        return False, (BLOCKED_OPTOUT_GLOBAL if scope == optout.GLOBAL
                       else BLOCKED_OPTOUT_PROJECT)

    # Fatigue is checked AFTER opt-out on purpose. Both block, but the reason
    # written to the log matters: "they told us to stop" and "we have said enough
    # this week" are different facts, and the permanent one should win the label.
    # Non-proactive sends pass straight through -- a reply inside a conversation
    # the customer opened is not fatigue.
    ok, cap = fatigue.check(phone, msg_type, project)
    if not ok:
        return False, cap

    # Retry ceiling last. It is the only check that applies to EVERY message type
    # rather than proactive ones alone: a number that cannot receive WhatsApp
    # cannot receive a reply either, so continuing to try wastes sends and buries
    # real failures in noise.
    ok, cap = failures.check(phone, msg_type, project)
    if not ok:
        return False, cap

    return True, OK

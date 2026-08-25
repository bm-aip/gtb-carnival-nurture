"""The 2026-08-25 runaway: one lead, 1,178 sends. No database, ~1 second.

    python tests/reopener_runaway.py

Lead 801 received `t7_reopener_newac` 1,178 times in 32 hours. Nothing stopped it
because three brakes shared one cause and all three were released at once:

  * `tries` in reopener.due() counted `AND r.ok` -- deliveries, not attempts -- so
    it stayed 0 through every refusal and REOPEN_MAX never bit.
  * `last_try` did the same, so the spacing anchor fell back to `last_turn_at`
    (days old) and every tick looked due.
  * the came-back guard COALESCEd to the buyer's own timestamp, and `x <= x` is
    true, so it passed on a technicality.

And the retry ceiling could not have saved it either: the refusal was "Meta has
restricted marketing messages to US recipients", which contains "restrict",
which is SYSTEM, which is uncapped BY DESIGN so our own misconfiguration can
never discard a buyer. That design is right. What was missing is the distinction
between not giving up on a PERSON and not repeating one SEND.

These tests pin the fix, not the bug.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import config                          # noqa: E402
import failures                        # noqa: E402
import reopener                        # noqa: E402

r = Results()

# --- the refusal that started it ---------------------------------------------
US_REFUSAL = ("Meta has restricted marketing messages to US recipients, other "
              "templates can still be sent")

r.eq("the live refusal still classifies as SYSTEM",
     failures.classify(US_REFUSAL), failures.SYSTEM)
r.check("and SYSTEM is still uncapped by the CLASS ceilings",
        failures.SYSTEM not in (failures.RECIPIENT, failures.TRANSIENT),
        detail="a buyer must never be discarded for our own misconfiguration")
r.check("it is NOT a hard recipient failure",
        not failures.is_hard_recipient_failure(US_REFUSAL),
        detail="the number is fine; the message type is not allowed to it")

# --- the backstop ------------------------------------------------------------
r.check("RETRY_MAX_BURST exists and is small",
        1 <= config.RETRY_MAX_BURST <= 10, detail=str(config.RETRY_MAX_BURST))

_real_counts, _real_burst = failures.counts, failures.burst_count
try:
    failures.counts = lambda phone, days=None: {}          # no class ceiling hit
    failures.burst_count = lambda phone, msg_type: config.RETRY_MAX_BURST
    allowed, reason = failures.check("919000000000", "reopener_t7")
    r.check("the burst cap blocks a send that keeps being refused", not allowed)
    r.eq("and says so with its own reason", reason, failures.CEILING_BURST)

    failures.burst_count = lambda phone, msg_type: config.RETRY_MAX_BURST - 1
    allowed, _ = failures.check("919000000000", "reopener_t7")
    r.check("one below the cap still goes out", allowed)

    # The cap is per message type, so a jammed lane must not silence the others.
    failures.burst_count = lambda phone, msg_type: (
        config.RETRY_MAX_BURST if msg_type == "reopener_t7" else 0)
    allowed_reopen, _ = failures.check("919000000000", "reopener_t7")
    allowed_reply, _ = failures.check("919000000000", "qualifier_turn")
    r.check("a jammed lane does not block a live reply to the same person",
            (not allowed_reopen) and allowed_reply,
            detail="per (phone, msg_type), never per phone")

    # No msg_type -> nothing to scope the cap to. Must not block.
    failures.burst_count = lambda phone, msg_type: 99
    allowed, _ = failures.check("919000000000", None)
    r.check("an unscoped send is never blocked by the burst cap", allowed)
finally:
    failures.counts, failures.burst_count = _real_counts, _real_burst

# --- the counters now count attempts -----------------------------------------
SQL = reopener.due.__doc__ or ""
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "reopener.py"), encoding="utf-8").read()
# TWO counters, each answering its own question.
delivered_block = src.split("AS delivered")[0].split("SELECT count(*) FROM message_log")[-1]
r.check("`delivered` DOES filter on r.ok", "AND r.ok" in delivered_block,
        detail="the ladder counts what reached them -- a refusal must not spend a chance (#71)")

attempts_block = src.split("AS attempts")[0].split("SELECT count(*) FROM message_log")[-1]
r.check("`attempts` does NOT filter on r.ok", "AND r.ok" not in attempts_block,
        detail="spacing counts everything we tried -- that is what bounds a loop (#72)")

r.check("the ladder cap reads `delivered`, not `attempts`",
        "delivered >= REOPEN_MAX" in src and "attempts >= REOPEN_MAX" not in src,
        detail="16 people were dormant at 6 attempts and 1 delivery")

last_try_block = src.split("AS last_try")[0].split("SELECT max(r.ts)")[-1]
r.check("`last_try` no longer filters on r.ok", "AND r.ok" not in last_try_block,
        detail="anchored on success it stayed NULL and every tick looked due")

came_back = src.split("l.last_inbound_at <= COALESCE(")[1].split(")")[0]
r.check("the came-back guard no longer filters on r.ok",
        "AND r.ok" not in came_back,
        detail="COALESCE fell back to the buyer's own timestamp; x <= x passed")

r.eq("three tries then dormant, unchanged", reopener.REOPEN_MAX,
     len(reopener.REOPEN_AFTER_DAYS))

# The loop bound with the ladder back on deliveries: spacing is anchored on the
# last ATTEMPT, so a number that refuses forever costs one send per interval
# rather than one per tick. 1,178 sends in 32 hours becomes at most 11.
r.check("spacing is measured in days, so a refusing number cannot loop per-tick",
        min(reopener.REOPEN_AFTER_DAYS) >= 1,
        detail=str(reopener.REOPEN_AFTER_DAYS))

# --- three states, not two ----------------------------------------------------
# delivered / refused-on-the-wire / never-sent. Collapsing the last two is what
# made the burst cap poison itself and the hourly cap unenforceable.
fsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "failures.py"), encoding="utf-8").read()
wsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "wati.py"), encoding="utf-8").read()
burst_sql = fsrc.split("def burst_count")[1].split("def check")[0]
r.check("burst_count ignores gate blocks", "NOT LIKE 'blocked:" in burst_sql,
        detail="a block never touched WhatsApp, so it is not evidence about the send")

hour_sql = wsrc.split("def sends_last_hour")[1].split("def rate_ok")[0]
r.check("the hourly cap counts refused sends, not just delivered ones",
        "ok OR COALESCE" in hour_sql,
        detail="with AND ok alone a refused lane never exhausts its hour")
r.check("the hourly cap still ignores gate blocks",
        "NOT LIKE 'blocked:" in hour_sql)

if __name__ == "__main__":
    sys.exit(0 if r.report("REOPENER RUNAWAY") else 1)

"""Capturing WHY a send failed. No database, no API, ~1 second.

    python tests/failure_reason.py

96 consecutive template failures were recorded with reason=NULL, because the parser
read four guessed key names and Wati used none of them. The reason was only ever
visible in Wati's dashboard, and it cannot be recovered afterwards -- checked
2026-08-17, a refused template barely appears in Wati's own message history.

So two things have to hold: the reason is found wherever it hides, and the raw
payload is kept on every failure so a future rename cannot cost us another 96.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bootstrap import Results        # noqa: E402

import wati                            # noqa: E402

r = Results()

# --- the shape that has been failing silently --------------------------------
# Modelled on the real templateMessageFailed callbacks: none of failureReason,
# errorMessage, reason or error is present.
observed = {"eventType": "templateMessageFailed", "waId": "919884739289",
            "whatsappMessageId": "wamid.HBgMOTE5", "statusString": "Failed",
            "timestamp": "1786430447"}
ev = wati.parse_status(observed)
r.eq("observed failure is classified as failed", ev["status"], "failed")
r.check("raw payload is now KEPT on a failure", bool(ev["raw"]),
        detail="this is the whole point -- without it the cause is unrecoverable")
r.check("the raw payload is the actual payload",
        "templateMessageFailed" in (ev["raw"] or ""))

# --- reasons found wherever the provider puts them ----------------------------
CASES = [
    ("top-level failureReason",
     {"eventType": "templateMessageFailed", "waId": "91", "failureReason":
      "Message undeliverable as Meta has restricted it for higher quality messaging"},
     "restricted"),
    ("nested error object",
     {"eventType": "templateMessageFailed", "waId": "91",
      "error": {"code": 131049, "title": "Message undeliverable"}}, "131049"),
    ("errors array, as Meta sends it",
     {"eventType": "templateMessageFailed", "waId": "91",
      "errors": [{"code": 131049, "message": "restricted for higher quality"}]},
     "131049"),
    ("a key nobody guessed",
     {"eventType": "templateMessageFailed", "waId": "91",
      "deliveryFailureCause": "quality throttle"}, "quality throttle"),
    ("wrapped in data, as Wati wraps it",
     {"data": {"eventType": "templateMessageFailed", "waId": "91",
               "failedDetail": "Template paused"}}, "Template paused"),
]
for name, payload, expect in CASES:
    ev = wati.parse_status(payload)
    got = ev.get("reason") or ""
    r.check(f"reason found: {name}", expect.lower() in got.lower(),
            detail=f"got {got!r}, wanted something containing {expect!r}")

r.check("the reason names WHICH field carried it",
        "=" in (wati.parse_status(CASES[0][1]).get("reason") or ""),
        detail="key=value, so the first real capture teaches us the schema")

# --- what must NOT be mistaken for a reason ----------------------------------
# A timestamp or a boolean dressed as a reason is worse than no reason: it looks
# like an answer and is not one.
noise = {"eventType": "templateMessageFailed", "waId": "91",
         "failed": True, "isFailed": "true", "failedAt": "1786430447"}
got = wati.parse_status(noise).get("reason")
r.check("a bare 'failed: true' is not treated as a reason",
        got is None or "true" not in got.lower(), detail=repr(got))
r.check("an epoch timestamp is not treated as a reason",
        got is None or "1786430447" not in got, detail=repr(got))

# A six-digit Meta code IS worth keeping even though it is only digits.
code_only = {"eventType": "templateMessageFailed", "waId": "91",
             "errorCode": 131049}
r.check("a Meta error code survives the numeric filter",
        "131049" in (wati.parse_status(code_only).get("reason") or ""),
        detail=repr(wati.parse_status(code_only).get("reason")))

# --- successes stay cheap -----------------------------------------------------
ok = {"eventType": "sentMessageREAD", "waId": "91", "statusString": "Read"}
ev = wati.parse_status(ok)
r.eq("a read receipt is still classified", ev["status"], "read")
r.check("successes store no raw payload", ev["raw"] is None,
        detail="keeping every payload would balloon the table for no gain")

# --- nothing raises -----------------------------------------------------------
for junk in ({}, {"data": None}, {"eventType": "templateMessageFailed"},
             {"data": {"eventType": "templateMessageFailed", "error": []}}):
    try:
        wati.parse_status(junk)
        r.check(f"survives {json.dumps(junk)[:40]}", True)
    except Exception as e:                                   # noqa: BLE001
        r.check(f"survives {json.dumps(junk)[:40]}", False, detail=str(e))

sys.exit(0 if r.report("FAILURE REASON CAPTURE") else 1)

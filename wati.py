"""Wati (official WhatsApp Cloud API) send/receive layer.

Drop-in replacement for wasender.py. Public surface mirrors it so the sequencer
swaps import with minimal change:
    send_template(phone, template_name, params)  -> (ok, detail)   [first-touch / outside 24h]
    send_text(phone, body)                        -> (ok, detail)   [reply inside 24h window]
    parse_inbound(payload)                        -> (phone, text)
    sends_last_hour(), rate_ok()

Key difference from wasender: WhatsApp official API forbids cold free-text. Any
first-touch (M1/M2/M3) MUST go as an approved template -> send_template. Free
text (send_text) only works inside the 24h window opened by a customer reply.
The day-picker buttons ride WITH the template (quick-reply buttons defined at
approval time), so there is no separate poll send -- sending the template
renders its buttons.
"""
import json
import os
import re
from datetime import datetime, timezone
import requests
import config
import db
# For is_proactive() only. fatigue imports nothing but config and db, so this
# cannot close a cycle -- and the proactive/reactive split must have ONE
# definition, or the reserve below would protect a different set of messages
# than the fatigue cap exempts.
import fatigue


def _auth_headers():
    # config.WATI_TOKEN is stored WITHOUT a leading "Bearer " (stripped in
    # config) so we add exactly one here -> no double-Bearer if the user pasted
    # the token with the prefix.
    return {"Authorization": f"Bearer {config.WATI_TOKEN}"}


def _result_ok(resp):
    """Wati returns 200 even for some logical failures; the body carries the
    real verdict in `result` (bool) or `ok`. Treat missing verdict as success
    (some endpoints return a bare object) but an explicit false as failure."""
    try:
        j = resp.json()
    except Exception:
        return True  # non-JSON 200 -> assume ok, detail still carries text
    if isinstance(j, dict):
        if j.get("result") is False or j.get("ok") is False:
            return False
        # Wati validation errors surface as {"result":"error", ...} too
        if str(j.get("result")).lower() in ("error", "false"):
            return False
    return True


def send_template(phone, template_name, params=None, broadcast=None):
    """Send an approved WhatsApp template.

    `params` may be:
      - a list  -> fills numbered {{1}},{{2}},...  (name = "1","2",...)
      - a dict  -> fills named {{brand}},{{name}}  (name = the key)
    Returns (ok, detail). Use for every first-touch / outside-24h-window send.
    """
    if isinstance(params, dict):
        parameters = [{"name": str(k), "value": str(v)} for k, v in params.items()]
    else:
        parameters = [{"name": str(i + 1), "value": str(v)}
                      for i, v in enumerate(params or [])]
    payload = {
        "template_name": template_name,
        "broadcast_name": broadcast or template_name,
        "parameters": parameters,
    }
    try:
        r = requests.post(
            f"{config.WATI_BASE}/api/v1/sendTemplateMessage",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            params={"whatsappNumber": phone},
            # 60, not 30: a staff card was lost on 2026-08-19 to a read timeout
            # that was only Wati being slow. Nothing here is urgent to the second.
            json=payload, timeout=60)
        ok = r.status_code in (200, 201) and _result_ok(r)
        # 2000, raised from 1000 on 2026-08-22. The id no longer DEPENDS on this
        # -- extract_msg_id() lifts it out of a partial body with a regex -- but a
        # response we can still read as JSON is worth having when diagnosing a
        # failure, and Wati echoes the contact record after the message.
        return ok, r.text[:2000]
    except Exception as e:
        return False, str(e)


def send_text(phone, body):
    """Free-text session message. Only delivers if the customer messaged within
    the last 24h (WhatsApp rule) -- used for acks/replies, never cold. Wati's
    sendSessionMessage takes the text as a query param, not JSON."""
    try:
        r = requests.post(
            f"{config.WATI_BASE}/api/v1/sendSessionMessage/{phone}",
            headers=_auth_headers(),
            params={"messageText": body}, timeout=60)
        ok = r.status_code in (200, 201) and _result_ok(r)
        # 2000, raised from 1000 on 2026-08-22. The id no longer DEPENDS on this
        # -- extract_msg_id() lifts it out of a partial body with a regex -- but a
        # response we can still read as JSON is worth having when diagnosing a
        # failure, and Wati echoes the contact record after the message.
        return ok, r.text[:2000]
    except Exception as e:
        return False, str(e)


def send_file(phone, path, caption=None):
    """Send one image/file into an open session. Returns (ok, detail).

    Uploads the FILE ITSELF, multipart. Deliberately not sendSessionFileViaUrl:
    hosting the images somewhere public would mean a bucket to keep alive, links
    that can rot, and a second place for a media set to drift out of step with the
    code that references it. These ship in the repo, so the file a release sends is
    the file that release was tested with.

    Like send_text, this only delivers inside the 24h window -- which is fine,
    because every caller is answering a message the buyer just sent.

    THE CAPTION IS A QUERY PARAMETER, not JSON. Same trap as sendSessionMessage:
    2026-08-05 a rupee sign went out mangled because free text on this path is URL
    encoded. Captions stay plain and money is written "Rs".
    """
    if not os.path.exists(path):
        return False, f"missing file: {path}"
    try:
        with open(path, "rb") as fh:
            r = requests.post(
                f"{config.WATI_BASE}/api/v1/sendSessionFile/{phone}",
                headers=_auth_headers(),
                params={"caption": caption} if caption else None,
                files={"file": (os.path.basename(path), fh, "image/jpeg")},
                timeout=60)
        ok = r.status_code in (200, 201) and _result_ok(r)
        return ok, r.text[:1000]
    except Exception as e:
        return False, str(e)


def check_connection():
    """Connectivity probe: ask Wati for the template list. Validates WATI_BASE +
    WATI_TOKEN without sending anything or needing an open 24h window. Returns a
    dict {ok, status, base_set, token_set, templates, detail}."""
    if not config.WATI_BASE or not config.WATI_TOKEN:
        return {"ok": False, "base_set": bool(config.WATI_BASE),
                "token_set": bool(config.WATI_TOKEN),
                "detail": "WATI_BASE and/or WATI_TOKEN not set in env"}
    try:
        r = requests.get(
            f"{config.WATI_BASE}/api/v1/getMessageTemplates",
            headers=_auth_headers(), params={"pageSize": 100}, timeout=30)
        ok = r.status_code == 200
        templates = []
        try:
            j = r.json()
            items = j.get("messageTemplates") or j.get("data") or []
            for t in items:
                if not isinstance(t, dict):
                    continue
                name = t.get("elementName") or t.get("name")
                if not name:
                    continue
                nvars = len(t.get("customParams") or [])
                templates.append({"name": name,
                                  "status": t.get("status"),
                                  "category": t.get("category"),
                                  "vars": nvars})
        except Exception:
            pass
        # Approved-only shortlist for quick scanning
        approved = [t["name"] for t in templates if str(t.get("status")).upper() == "APPROVED"]
        return {"ok": ok, "status": r.status_code,
                "base_set": True, "token_set": True,
                "approved": approved,
                "templates": templates}
    except Exception as e:
        return {"ok": False, "base_set": True, "token_set": True,
                "detail": str(e)}


def fetch_referral(phone, max_pages=6):
    """Ask Wati for the ad referral behind a conversation. Returns a dict or None.

    THIS IS THE ONLY WAY TO GET `ctwa_clid`, and it is a pull, not a push.

    Wati's webhook flattens an ad click to `sourceType=7 sourceId=... sourceUrl=...`
    and drops the click id. Verified on 2026-08-10 across 90 real arrivals: not one
    webhook carried it. The REST API keeps the whole object:

        messageReferral: {headline, body, url, sourceId, ctwaClid}

    ...hanging off the FIRST inbound message of the conversation, and only that one.
    Later messages carry `messageReferral: null`, which is why this walks to the
    oldest inbound rather than reading the newest.

    Do NOT reach for the MCP `wati_get_messages` tool instead -- it projects the
    message down to a summary shape and discards messageReferral entirely.

    Returns {ctwa_clid, source_id, source_url, headline} on an ad click, or None for
    a landing-page walk-in (which genuinely has no referral -- no Meta click ever
    happened, so None is an answer, not a failure).
    """
    if not config.WATI_BASE or not config.WATI_TOKEN or not phone:
        return None

    referral = None
    for page in range(1, max_pages + 1):
        r = requests.get(f"{config.WATI_BASE}/api/v1/getMessages/{phone}",
                         headers=_auth_headers(),
                         params={"pageSize": 100, "pageNumber": page}, timeout=30)
        # Raise, so the job queue retries with backoff instead of writing down
        # "no referral" because Wati happened to be rate-limiting us.
        r.raise_for_status()
        data = r.json() if r.content else {}

        node = data.get("messages") if isinstance(data, dict) else None
        if isinstance(node, dict):
            items = node.get("items") or []
        elif isinstance(node, list):
            items = node
        else:
            items = (data.get("items") or []) if isinstance(data, dict) else []
        if not items:
            break

        # Newest-first, so the last referral seen while paging deeper is the oldest.
        for m in items:
            if not isinstance(m, dict):
                continue
            if m.get("owner") is True or m.get("isOwner") is True:
                continue
            ref = m.get("messageReferral")
            if isinstance(ref, dict) and (ref.get("ctwaClid") or ref.get("sourceId")):
                referral = ref

        if len(items) < 100:
            break

    if not referral:
        return None
    return {
        "ctwa_clid": (referral.get("ctwaClid") or None),
        "source_id": str(referral.get("sourceId") or "") or None,
        "source_url": (referral.get("url") or None),
        # The ad's own headline. Kept because it is the only human-readable label we
        # get -- an ad id tells marketing nothing when they read the report.
        "headline": (referral.get("headline") or None),
    }


def sends_last_hour():
    """Messages that actually went on the wire in the last hour.

    `matched` is EXCLUDED. It is a bookkeeping row written when a lead is paired
    with a phone number -- nothing is sent -- but it is stored as direction='out'
    with ok=TRUE, so it was consuming the hourly allowance. Four of the hundred
    slots on 2026-08-22 went to rows that never touched WhatsApp.
    """
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE direction='out' AND ok AND msg_type <> 'matched'
                  AND ts > now() - interval '1 hour'""",
             one=True)
    return r["n"] if r else 0


def rate_ok(msg_type=None):
    """Is there hourly capacity for this message?

    A PROACTIVE send may only use the first part of the hour's allowance; the rest
    is held back for people who are actually talking to us.

    2026-08-22, and this is why: the knock backlog sent 172 templates and used
    exactly 100 of 100 slots in one hour. Sanjay Agarwalla read his message, pressed
    "Need More Details", and got nothing -- his reply arrived at slot 101. Vivek
    Chordia hit the same wall thirteen minutes earlier. A marketing blast outranked
    two live buyers because it got there first and the budget was shared.

    Someone who read our message and asked a question is worth more than the 173rd
    cold template. The reserve encodes that, cheaply: knocks stop early, replies
    keep the remainder.
    """
    used = sends_last_hour()
    if msg_type is not None and is_business_initiated(msg_type):
        return used < max(1, config.MAX_SENDS_PER_HOUR - config.REPLY_RESERVE_PER_HOUR)
    return used < config.MAX_SENDS_PER_HOUR


# The carnival first-touch types. Cold business-initiated templates, exactly like a
# knock -- but fatigue.is_proactive() matches only "knock*", so they read as replies.
#
# NOT folded into fatigue.is_proactive(), deliberately. Widening that would also
# subject m1/m2/m3 to the 4-per-journey and 2-per-7-day fatigue caps for the first
# time, which is a different decision with its own consequences and nobody has asked
# for it. Volume is ~3 a day, so the honest fix is to name them here rather than
# quietly change what fatigue means.
# `reopener_t7` joins them: the 24h window is shut, so a re-open is an approved
# template and a business-initiated conversation like any other knock.
_COLD_FIRST_TOUCH = ("m1", "m2", "m3", "reopener_t7")


def is_business_initiated(msg_type):
    """True for business-initiated sends, which must leave the reserve alone."""
    return fatigue.is_proactive(msg_type) or msg_type in _COLD_FIRST_TOUCH


# --- Phase 0 task 5: delivery status callbacks ---------------------------------
#
# Wati posts a callback for every state change of an outbound message, and the
# event names are not stable across Wati's own versions -- observed and documented
# forms include sentMessageDELIVERED, sentMessageREAD, templateMessageSent,
# sessionMessageSent, message_status and a bare `status` field. Matching an exact
# list would silently drop anything renamed.
#
# So we match on KEYWORDS and, critically, keep what we cannot classify as
# status='unknown' with its full payload attached. The entire reason this task
# exists is that the old code threw unrecognised events away; a parser that
# quietly drops the events it does not recognise repeats that mistake in a more
# sophisticated way.
#
# Order matters: failure is checked before delivery because a failure event can
# also carry the word "sent".
_STATUS_KEYWORDS = (
    ("failed",    ("fail", "undeliver", "reject", "error", "invalid", "block")),
    ("read",      ("read", "seen")),
    ("delivered", ("deliver",)),
    ("sent",      ("sent", "accept", "submit")),
)

_INBOUND_EVENTS = ("message", "text", "interactive", "button")


# Keys that plausibly carry WHY a send failed. Matched on the key NAME, at any depth,
# because Wati's callback schema is not stable and the four names we guessed were all
# empty on 96 consecutive failures.
_REASON_KEY = re.compile(r"fail|error|reject|reason|undeliver|denial|cause", re.I)
# Keys that merely say a failure happened, which we already know from `status`.
_REASON_SKIP = re.compile(r"^(failed|isfailed|hasfailed)$", re.I)
# Meta's numeric error codes, e.g. 131049 for the quality restriction.
_META_CODE = re.compile(r"\b1\d{5}\b")


def _find_reason(payload):
    """The failure reason from anywhere in the payload, or None.

    Walks the whole structure rather than reading four fixed keys, so a provider
    rename cannot silently return us to storing NULL again. Returns `key=value`
    pairs, so the first real capture also tells us WHICH field carried the answer.

    CONTEXT IS INHERITED. Meta sends `errors: [{code: 131049, message: "..."}]`,
    where the useful keys are `code` and `message` -- neither of which looks like a
    failure on its own. So once a CONTAINER key matches (`error`, `errors`,
    `failureDetail`), everything inside it counts, labelled with its parent. Without
    this the array shape returns nothing, which is exactly the silence being fixed.

    Numeric-only values are kept when they look like a Meta error code (131049 and
    friends) and dropped otherwise: a timestamp or a retry count dressed up as a
    reason is worse than no reason, because it looks like an answer.
    """
    if payload is None:
        return None
    found = []
    seen = set()

    def add(label, value):
        s = str(value).strip()
        if not s or s.lower() in ("true", "false", "0", "none", "null"):
            return
        if s.isdigit() and not _META_CODE.search(s):
            return
        item = f"{label}={s[:200]}"
        if item not in seen:
            seen.add(item)
            found.append(item)

    def walk(node, depth, prefix, inherited):
        if depth > 6 or len(found) >= 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                matched = bool(_REASON_KEY.search(key)) and not _REASON_SKIP.match(key)
                label = f"{prefix}.{key}" if prefix else key
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1, label if (matched or inherited) else "",
                         inherited or matched)
                elif v not in (None, "", [], {}):
                    if matched or inherited:
                        add(label, v)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1, prefix, inherited)

    walk(payload, 0, "", False)
    return " | ".join(found[:6]) or None


def _canonical_status(*fields):
    """First keyword hit across the supplied strings, or None."""
    blob = " ".join(str(f).lower() for f in fields if f)
    if not blob:
        return None
    for status, words in _STATUS_KEYWORDS:
        if any(w in blob for w in words):
            return status
    return None


def is_status_event(payload):
    """True when this callback is about one of OUR outbound messages.

    A real customer message is identified the same way parse_inbound does it --
    eventType in the inbound set and owner not set -- and is never treated as a
    status, so the two paths cannot both claim the same payload.
    """
    m = (payload.get("data") or payload) if isinstance(payload, dict) else {}
    if not isinstance(m, dict):
        return False
    etype = str(m.get("eventType") or m.get("type") or "")
    if etype in _INBOUND_EVENTS and not (m.get("owner") or m.get("fromMe")):
        return False
    return bool(_canonical_status(etype, m.get("status"), m.get("statusString"))
                or m.get("owner") is True or m.get("fromMe") is True)


def parse_status(payload):
    """Extract one delivery event, or None if this is not about an outbound send.

    Never raises and never returns None merely because the shape was unfamiliar:
    an unrecognised-but-outbound event comes back as status='unknown' carrying its
    raw payload, so the first real callbacks tell us the true schema instead of
    vanishing.
    """
    try:
        if not is_status_event(payload):
            return None
        m = (payload.get("data") or payload) if isinstance(payload, dict) else {}
        if not isinstance(m, dict):
            return None

        etype = m.get("eventType") or m.get("type")
        status = _canonical_status(etype, m.get("status"), m.get("statusString"))

        phone = (m.get("waId") or m.get("whatsappNumber") or m.get("to")
                 or m.get("phone") or m.get("from") or "")
        phone = re.sub(r"\D", "", str(phone)) or None

        mid = (m.get("whatsappMessageId") or m.get("messageId")
               or m.get("id") or m.get("localMessageId"))

        # THE DEEP SEARCH RUNS FIRST, and the four named keys are the fallback.
        #
        # It used to be the other way round, and all four names were empty on every
        # one of the 96 template failures recorded up to 2026-08-17 -- so `reason`
        # was NULL throughout and "Meta has restricted it for higher quality
        # messaging" was only ever readable in Wati's dashboard.
        #
        # Order matters beyond that: `error` as a nested object used to resolve to
        # its `title` alone, which reads fine and silently drops the 131049 code
        # underneath it. The deep search returns both.
        reason = _find_reason(payload)
        if not reason:
            reason = (m.get("failureReason") or m.get("errorMessage")
                      or m.get("reason") or m.get("error"))
            if isinstance(reason, dict):
                reason = reason.get("message") or reason.get("title") or str(reason)

        ts = (m.get("timestamp") or m.get("eventTime") or m.get("created")
              or m.get("createdAt"))

        return {
            "phone": phone,
            "provider_msg_id": str(mid) if mid else None,
            "status": status or "unknown",
            "reason": str(reason)[:500] if reason else None,
            "event_type": str(etype)[:80] if etype else None,
            "event_ts": _parse_ts(ts),
            # Raw kept for anything we could not classify AND for every FAILURE.
            #
            # It used to be unclassified-only, on the argument that storing every
            # payload would balloon the table for no diagnostic gain. That was right
            # about volume and wrong about failures: a failed send is the one event
            # whose cause we always end up needing and can never recover afterwards.
            # Checked 2026-08-17 -- a refused template barely appears in Wati's own
            # message history, so there is nothing to go back and read. The payload
            # is the only copy. Successes still store nothing.
            "raw": (json.dumps(payload)[:4000]
                    if (not status or status == "failed") else None),
        }
    except Exception:
        # A callback we cannot even parse is still evidence that something
        # happened. Losing it is worse than storing it shapeless.
        try:
            return {"phone": None, "provider_msg_id": None, "status": "unknown",
                    "reason": "parse_error", "event_type": None, "event_ts": None,
                    "raw": json.dumps(payload)[:4000]}
        except Exception:
            return None


def _parse_ts(v):
    """Wati sends timestamps as epoch seconds, epoch millis or an ISO string
    depending on the event. Unparseable -> None; the row's created_at still
    records when we heard about it."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            n = float(v)
            if n > 1e11:      # milliseconds
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def extract_msg_id(detail):
    """Pull the provider message id out of a send response body.

    Stored against the send in message_log so a later delivery callback can be
    joined back to the message that caused it. Best-effort by design: without an
    id the callback still lands, matched on phone instead.
    """
    if not detail:
        return None

    # REGEX FIRST, BECAUSE THE JSON IS USUALLY BROKEN BY THE TIME WE SEE IT.
    #
    # 2026-08-22: every one of 204 sends in a day stored provider_msg_id NULL. The
    # id was sitting in plain sight inside `detail` -- but `detail` is r.text[:1000]
    # (see send_template / send_text), and a JSON document cut off mid-string will
    # not parse, so json.loads raised and this returned None every single time.
    #
    # The truncation limit had already been raised once, 300 -> 1000, by someone who
    # spotted this exact risk and wrote "extract_msg_id() needs it intact". It was
    # still short: Wati echoes the whole contact record after the message. Raising
    # the number again would be the same guess with a bigger number, so instead this
    # no longer depends on the body being complete.
    #
    # A WhatsApp message id is a `wamid.` token with a fixed alphabet, which makes it
    # safe to lift out of a partial document. Tried before json.loads because a
    # truncated body is the common case, not the exception.
    m = re.search(r'"whatsappMessageId"\s*:\s*"(wamid\.[A-Za-z0-9+/=_-]+)"', detail)
    if m:
        return m.group(1)

    try:
        j = json.loads(detail)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    for key in ("whatsappMessageId", "messageId", "id", "localMessageId"):
        v = j.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    # Wati nests the echo of the sent message one level down on some endpoints.
    for outer in ("message", "data", "result"):
        inner = j.get(outer)
        if isinstance(inner, dict):
            for key in ("whatsappMessageId", "messageId", "id"):
                v = inner.get(key)
                if isinstance(v, (str, int)) and str(v).strip():
                    return str(v)
    return None


def parse_source(payload):
    """Where did this inbound come from? DIAGNOSTIC ONLY -- nothing branches on it.

    A click-to-WhatsApp message should carry a reference back to the ad that
    produced it. If it does, we can eventually adopt only strangers who arrived
    through OUR ads rather than every unknown number (owner question, 2026-08-02:
    GT Bharathi's leads should not be engaged, but we have no way to recognise
    them). On the one sample we have -- a typed message -- these are all empty,
    and we have no CTWA sample yet. So this records the evidence instead of
    assuming the answer.

    Returns a short string for the log, or None when there is nothing to say.
    """
    try:
        m = payload.get("data") or payload
        if not isinstance(m, dict):
            return None
        bits = []
        for key in ("sourceType", "sourceId", "sourceUrl", "referral",
                    "messageReferral", "adReferral", "ctwaClid", "bsuid"):
            v = m.get(key)
            if v not in (None, "", {}, [], 0):
                bits.append(f"{key}={str(v)[:120]}")
        return " ".join(bits) or None
    except Exception:
        return None


def parse_inbound(payload):
    """Extract (phone, text) from a Wati inbound webhook.

    Wati posts many event types (message, sessionMessageSent, templateMessageSent,
    delivery/read status). We only want a real customer message:
      - eventType == 'message'  (skip send/status callbacks)
      - owner is false          (owner=true means WE sent it -> skip)
    Text lives in `text`; a button/list tap arrives as an interactive reply whose
    title we normalize back to the plain button label (so 'Fri 10 July' round-
    trips into the reply parser exactly like a typed reply)."""
    try:
        m = payload.get("data", payload) or payload
        if not isinstance(m, dict):
            return None, None

        # skip our own outbound + non-message events (status/sent callbacks)
        if m.get("owner") is True or m.get("fromMe") is True:
            return None, None
        etype = m.get("eventType") or m.get("type")
        if etype and etype not in ("message", "text", "interactive", "button"):
            return None, None

        phone = (m.get("waId") or m.get("whatsappNumber")
                 or m.get("from") or m.get("phone") or "")
        phone = re.sub(r"\D", "", str(phone))

        text = (m.get("text")
                or (m.get("interactiveButtonReply") or {}).get("title")
                or (m.get("buttonReply") or {}).get("text")
                or (m.get("listReply") or {}).get("title")
                or m.get("buttonText"))
        if isinstance(text, dict):
            text = text.get("title") or text.get("text")
        text = str(text).strip() if text else None

        return (phone or None), (text or None)
    except Exception:
        return None, None


def parse_sender_name(payload):
    """WhatsApp profile name of the sender, when Wati supplies one.

    Only used for walk-ins: a lead created from an inbound message has no
    Sell.do or Meta-form record to take a name from. Never trusted for anything
    but display -- the sender controls this string, so it goes into `name`
    (which only ever renders inside a message body), never into a lookup key.
    """
    try:
        m = payload.get("data") or payload
        if not isinstance(m, dict):
            return None
        n = (m.get("senderName") or m.get("name") or "").strip()
        # Wati falls back to the raw phone number when no profile name is set;
        # "Hi 919003044700!" reads worse than "Hi there!".
        if not n or n.isdigit():
            return None
        return n[:80]
    except Exception:
        return None

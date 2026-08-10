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
import re
from datetime import datetime, timezone
import requests
import config
import db


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
            json=payload, timeout=30)
        ok = r.status_code in (200, 201) and _result_ok(r)
        # 1000, not 300: the provider message id lives in this body and
        # extract_msg_id() needs it intact to join delivery callbacks back to the
        # send. At 300 chars Wati's echo of the message could truncate first.
        return ok, r.text[:1000]
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
            params={"messageText": body}, timeout=30)
        ok = r.status_code in (200, 201) and _result_ok(r)
        # 1000, not 300: the provider message id lives in this body and
        # extract_msg_id() needs it intact to join delivery callbacks back to the
        # send. At 300 chars Wati's echo of the message could truncate first.
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
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE direction='out' AND ok AND ts > now() - interval '1 hour'""",
             one=True)
    return r["n"] if r else 0


def rate_ok():
    return sends_last_hour() < config.MAX_SENDS_PER_HOUR


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
            # Raw kept ONLY for events we could not classify -- storing every
            # payload would balloon the table for no diagnostic gain.
            "raw": None if status else json.dumps(payload)[:4000],
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

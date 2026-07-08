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
import re
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
        return ok, r.text[:300]
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
        return ok, r.text[:300]
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


def sends_last_hour():
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE direction='out' AND ok AND ts > now() - interval '1 hour'""",
             one=True)
    return r["n"] if r else 0


def rate_ok():
    return sends_last_hour() < config.MAX_SENDS_PER_HOUR


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

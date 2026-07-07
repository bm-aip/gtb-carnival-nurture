import requests
import config
import db


def _headers():
    return {"Authorization": f"Bearer {config.WASENDER_API_KEY}",
            "Content-Type": "application/json"}


def send_text(phone, body):
    """Returns (ok, detail)."""
    try:
        r = requests.post(f"{config.WASENDER_BASE}/send-message",
                          headers=_headers(),
                          json={"to": phone, "text": body}, timeout=30)
        ok = r.status_code in (200, 201)
        return ok, r.text[:300]
    except Exception as e:
        return False, str(e)


def send_poll(phone, question, options):
    """Try native poll; fall back to plain text if the endpoint rejects it."""
    try:
        r = requests.post(f"{config.WASENDER_BASE}/send-poll",
                          headers=_headers(),
                          json={"to": phone, "poll": {"name": question,
                                                      "options": options,
                                                      "multiple_answers": False}},
                          timeout=30)
        if r.status_code in (200, 201):
            return True, "poll"
    except Exception:
        pass
    return send_text(phone, question)[0], "text_fallback"


def sends_last_hour():
    r = db.q("""SELECT count(*) AS n FROM message_log
                WHERE direction='out' AND ok AND ts > now() - interval '1 hour'""",
             one=True)
    return r["n"] if r else 0


def rate_ok():
    return sends_last_hour() < config.MAX_SENDS_PER_HOUR


def parse_inbound(payload):
    """Extract (phone, text) from Wasender webhook payload.
    Handles plain messages and poll votes; shapes vary by Wasender version.

    IMPORTANT: current Wasender uses LID addressing -- key.remoteJid is a LID
    (e.g. '24172126318761@lid'), NOT the phone. The real number lives in
    key.cleanedSenderPn / key.senderPn. Read those first; fall back to
    remoteJid only when it is not a @lid. Text is in message.conversation or
    the unified top-level 'messageBody' field."""
    try:
        data = payload.get("data", payload) or {}
        msgs = data.get("messages", data)
        if isinstance(msgs, list):
            msgs = msgs[0] if msgs else {}
        key = msgs.get("key", {}) or {}
        if key.get("fromMe") or msgs.get("fromMe"):
            return None, None

        remote = key.get("remoteJid") or msgs.get("remoteJid") or ""
        # phone: prefer real-number fields; use remoteJid only if it's a phone jid
        jid = (key.get("cleanedSenderPn")
               or key.get("senderPn")
               or msgs.get("senderPn")
               or (remote if "@lid" not in remote else "")
               or msgs.get("from") or data.get("from") or remote or "")
        phone = jid.split("@")[0] if "@" in jid else jid

        m = msgs.get("message", {}) or {}
        text = (m.get("conversation")
                or (m.get("extendedTextMessage") or {}).get("text")
                or (m.get("pollUpdateMessage") or {}).get("vote")
                or msgs.get("messageBody")          # Wasender unified text field
                or msgs.get("text") or msgs.get("body"))
        if isinstance(text, dict):
            opts = text.get("selectedOptions") or []
            text = opts[0].get("name") if opts and isinstance(opts[0], dict) else str(opts)
        import re
        phone = re.sub(r"\D", "", phone or "")
        return (phone or None), (str(text).strip() if text else None)
    except Exception:
        return None, None

import os
import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import threading
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response
from flask.json.provider import DefaultJSONProvider
from apscheduler.schedulers.background import BackgroundScheduler
import config

# Bump on every deploy. /admin/config-check echoes it, so you can prove which
# source is serving before flipping a switch that messages real people.
# Bump this on EVERY deploy. It is the only way to prove which source is actually
# serving before flipping a switch that messages real people -- and it silently
# lied through the whole Phase 0 rollout, still reporting the carnival build while
# the new code was live. A stale value here is worse than no value.
CODE_VERSION = "2026-08-02-phone-keyed-knocks"
import db
import selldo
import meta
import sequencer
import sendgate
import optout
import fatigue
import failures
import kb
import embed
import jobs
import worker
import wasender
import wati
import match


class _ISOJSONProvider(DefaultJSONProvider):
    """Serialize date/datetime as ISO 8601 (2026-07-10) instead of Flask's
    default HTTP-date ("Fri, 10 Jul 2026 00:00:00 GMT"). The dashboard's
    new Date() + day-card matching both expect ISO, so without this a lead's
    selected_date renders as "Invalid Date" and the per-day counters never
    match. Fixes every JSON endpoint at once; no data or send-path change."""
    @staticmethod
    def default(o):
        if isinstance(o, (_dt.date, _dt.datetime)):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = _ISOJSONProvider(app)


# ---------- auth ----------
def _authed(a):
    return a and a.username == config.DASH_USER and a.password == config.DASH_PASS

def auth(f):
    @wraps(f)
    def w(*args, **kwargs):
        if not _authed(request.authorization):
            return Response("Auth required", 401,
                            {"WWW-Authenticate": 'Basic realm="carnival"'})
        return f(*args, **kwargs)
    return w


# ---------- pages ----------
@app.route("/")
@auth
def dashboard():
    # No event name to display -- the carnival is over and EVENT_NAME is gone
    # (Phase 0 task 1b). The dashboard header falls back to a system name until a
    # per-project operations view replaces it.
    return render_template("dashboard.html", event="Lead Engine")


# ---------- webhook ----------
@app.route("/webhook/wasender", methods=["POST"])
def wasender_webhook():
    # Diagnostic: stash the raw payload so we can see what Wasender actually
    # delivers (and confirm it delivers at all). Non-fatal if it fails.
    try:
        raw = request.get_data(as_text=True) or ""
        db.set_setting("last_webhook_raw", (sequencer.now_ist().isoformat() + " " + raw)[:2000])
        n = int(db.get_setting("webhook_hits", "0") or "0") + 1
        db.set_setting("webhook_hits", str(n))
    except Exception:
        pass
    if config.WASENDER_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != config.WASENDER_WEBHOOK_SECRET:
            return "", 403
    payload = request.get_json(silent=True) or {}
    # Dedup: Wasender delivers one message via several events -> claim the msg id
    # once so we don't ack twice.
    _m = payload.get("data", payload)
    _m = _m.get("messages", _m)
    if isinstance(_m, list):
        _m = _m[0] if _m else {}
    _mid = ((_m.get("key") or {}).get("id") if isinstance(_m, dict) else None) or \
           (_m.get("id") if isinstance(_m, dict) else None)
    if not db.mark_webhook_new(_mid):
        return jsonify({"ok": True, "dup": True})
    phone, text = wasender.parse_inbound(payload)
    if phone and text:
        sequencer.handle_inbound(phone, text)
    return jsonify({"ok": True})


def _wati_inbound(payload, allow_create):
    """Shared body for both Wati webhook routes.

    `allow_create` is the whole reason there are two routes. Creating a lead
    from a webhook means an unauthenticated POST can inject a record and make us
    send WhatsApp messages on a number that is still on a probationary tier. Only
    the secret-path route may do that.
    """
    # Dedup on Wati's message id so a retried delivery is processed once.
    # Wati posts "data": null as a REAL key, so payload.get("data", payload)
    # returns None -- the default never fires and _mid was always None, which
    # made mark_webhook_new() wave every message through. `or payload` is the
    # same fallback parse_inbound already uses. Without it, a Wati retry would
    # create a second lead and send a second welcome to the same person.
    _d = payload.get("data") or payload
    # conversationId is deliberately NOT in this chain: it is stable per CONTACT,
    # not per message, so it would swallow every reply after a person's first.
    _mid = (_d.get("id") or _d.get("whatsappMessageId")) if isinstance(_d, dict) else None

    # --- delivery callbacks (Phase 0 task 5) ---
    # Checked BEFORE the inbound dedup, because the two need different dedup keys.
    # sent, delivered and read callbacks all carry the SAME message id, so claiming
    # the id once would keep only the first and throw the rest away -- which is the
    # exact failure this task exists to fix. Status events dedup on id+status.
    ev = wati.parse_status(payload)
    if ev:
        # An async failure is the COMMON case for a blocked recipient -- WhatsApp
        # accepts the message, then fails it. Classifying it here is what lets it
        # reach the retry ceiling (task 4); without this the most frequent
        # recipient failure would never be counted.
        if ev["status"] == "failed":
            ev["fail_class"] = failures.classify(ev.get("reason") or ev.get("event_type"))
        key = f"{ev['provider_msg_id']}:{ev['status']}" if ev["provider_msg_id"] else None
        if key and not db.mark_webhook_new(key):
            return jsonify({"ok": True, "dup": True})
        db.record_delivery(ev)
        return jsonify({"ok": True, "status": ev["status"]})

    if not db.mark_webhook_new(_mid):
        return jsonify({"ok": True, "dup": True})
    phone, text = wati.parse_inbound(payload)
    if not (phone and text):
        # NOT nothing. Wati called us 183 times on 2026-08-02 and only ONE row
        # reached message_delivery, because anything that is neither a recognised
        # status nor a parseable inbound returned 200 and vanished. That is why a
        # 62% template failure rate had to be noticed by a human instead of by us.
        #
        # Keep it, shapeless, so the real schema becomes visible. Capped per hour
        # so a chatty provider cannot fill the table.
        try:
            recent = db.q("""SELECT count(*) n FROM message_delivery
                             WHERE status='unrecognised'
                               AND created_at > now() - interval '1 hour'""",
                          one=True)
            if (recent or {}).get("n", 0) < 200:
                db.record_delivery({
                    "phone": re.sub(r"\D", "", str(
                        (payload.get("data") or payload or {}).get("waId") or "")) or None,
                    "provider_msg_id": _mid,
                    "status": "unrecognised",
                    "reason": None,
                    "event_type": str((payload.get("data") or payload or {})
                                      .get("eventType") or "")[:80] or None,
                    "event_ts": None,
                    "raw": json.dumps(payload)[:4000],
                })
        except Exception:
            pass
        return jsonify({"ok": True})

    # SYNCHRONOUS, and deliberately so (task 12). Two things must happen before this
    # request returns, because deferring either is unsafe:
    #
    #   1. opt-out detection -- a person who types STOP must be uncontactable from
    #      this instant, not from whenever the queue drains.
    #   2. recording the inbound -- last_inbound_at is what stop-on-reply and the
    #      window-state logic read, and an inbound we failed to write down is
    #      unrecoverable.
    #
    # Both are a regex and an insert. Neither needs a language model.
    sequencer.handle_inbound(phone, text,
                             sender_name=wati.parse_sender_name(payload),
                             allow_create=allow_create,
                             source=wati.parse_source(payload))

    # ASYNCHRONOUS: the thinking. An LLM turn takes seconds and WhatsApp wants an
    # immediate 200 -- four slow turns in-process and the webhook stops answering,
    # which Wati reads as a broken integration and retries into.
    jobs.enqueue(jobs.KIND_INBOUND,
                 {"text": text, "sender_name": wati.parse_sender_name(payload),
                  "allow_create": allow_create},
                 phone=phone,
                 # Same id the dedup above used, so a Wati retry that slips past
                 # processed_webhooks still cannot produce two replies.
                 dedup_key=f"inbound:{_mid}" if _mid else None)
    return jsonify({"ok": True, "queued": True})


def _stash_wati(raw):
    try:
        db.set_setting("last_wati_webhook_raw",
                       (sequencer.now_ist().isoformat() + " " + raw)[:2000])
        n = int(db.get_setting("wati_webhook_hits", "0") or "0") + 1
        db.set_setting("wati_webhook_hits", str(n))
    except Exception:
        pass


@app.route("/webhook/meta", methods=["GET"])
def meta_webhook_verify():
    """Meta's subscription handshake. It calls this once when you save the URL."""
    if not config.META_VERIFY_TOKEN:
        return "verify token not configured", 503
    args = request.args
    if (args.get("hub.mode") == "subscribe"
            and args.get("hub.verify_token") == config.META_VERIFY_TOKEN):
        return args.get("hub.challenge", ""), 200
    return "forbidden", 403


def _meta_signature_ok(raw):
    """Meta signs every delivery with the app secret.

    Without this anyone who guessed the URL could inject a lead and make us send
    a WhatsApp template to a number of their choosing. An unset secret therefore
    REFUSES everything -- failing closed, like the campaign gate.
    """
    if not config.META_APP_SECRET:
        return False
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(config.META_APP_SECRET.encode(), raw,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.split("=", 1)[1])


@app.route("/webhook/meta", methods=["POST"])
def meta_webhook():
    """A lead was just submitted. Create it and knock immediately.

    Answers 200 in every case Meta should not retry. A 500 makes Meta redeliver,
    which for us means a second WhatsApp template to a real person, so anything
    we have already recorded is acknowledged rather than retried.
    """
    raw = request.get_data() or b""
    if not config.LEADGEN_WEBHOOK_ENABLED:
        return jsonify({"ok": True, "disabled": True})
    if not _meta_signature_ok(raw):
        db.log_msg(None, "in", "leadgen_rejected", None, ok=False,
                   detail="bad or missing X-Hub-Signature-256")
        return "forbidden", 403

    payload = request.get_json(silent=True) or {}
    handled = 0
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            if change.get("field") != "leadgen":
                continue
            v = change.get("value") or {}
            leadgen_id = str(v.get("leadgen_id") or "")
            page_id = str(v.get("page_id") or "")
            if not leadgen_id:
                continue

            # Only our own page. A second page on the same app must never be able
            # to push leads into RON.
            allowed_pages = config.META_PAGE_IDS.get("RON") or []
            if allowed_pages and page_id and page_id not in allowed_pages:
                db.log_msg(None, "in", "leadgen_rejected", None, ok=False,
                           detail=f"page {page_id} not in META_PAGE_IDS_RON")
                continue

            # Meta retries. Two knocks to one buyer is the failure that matters.
            if not db.mark_webhook_new(f"leadgen:{leadgen_id}"):
                handled += 1
                continue

            threading.Thread(target=_handle_leadgen, args=(leadgen_id,),
                             daemon=True).start()
            handled += 1
    return jsonify({"ok": True, "handled": handled})


def _handle_leadgen(leadgen_id):
    """Fetch, store, create the lead, knock. Runs off the request thread so Meta
    gets its 200 immediately -- their delivery timeout is short and a slow reply
    counts as a failure."""
    try:
        lead_data = meta.fetch_lead("RON", leadgen_id)
        if not lead_data or not lead_data.get("phone"):
            db.log_msg(None, "in", "leadgen_rejected", None, ok=False,
                       detail=f"{leadgen_id}: no phone in field_data")
            return

        form_name = lead_data.get("form_name")
        if form_name not in config.RON_FORMS:
            db.log_msg(None, "in", "leadgen_rejected", None, ok=False,
                       detail=f"{leadgen_id}: form {form_name!r} not in RON_FORMS")
            return

        db.x("""INSERT INTO meta_leads (meta_lead_id, project, page_id, form_id,
                                        form_name, name, phone, created_time)
                VALUES (%s,'RON',%s,%s,%s,%s,%s,%s)
                ON CONFLICT (meta_lead_id) DO NOTHING""",
             (lead_data["meta_lead_id"], lead_data.get("page_id"),
              lead_data.get("form_id"), form_name, lead_data.get("name"),
              lead_data["phone"], lead_data.get("created_time")))

        # Already known by phone? Then they are an existing lead and the knock
        # engine's own rules decide -- do not create a second row for one person.
        existing = db.q("SELECT * FROM leads WHERE phone=%s ORDER BY updated_at DESC "
                        "LIMIT 1", (lead_data["phone"],), one=True)
        if existing:
            db.log_msg(existing["id"], "in", "leadgen_dup", None,
                       detail=f"{leadgen_id}: phone already lead #{existing['id']}")
            return

        db.x("""INSERT INTO leads (project, selldo_lead_id, meta_lead_id, name, phone,
                                   campaign, selldo_status, selldo_response_at,
                                   wa_state)
                VALUES ('RON',%s,%s,%s,%s,%s,'meta_direct',%s,'queued')
                ON CONFLICT (project, selldo_lead_id) DO NOTHING""",
             (f"meta:{lead_data['meta_lead_id']}", lead_data["meta_lead_id"],
              lead_data.get("name"), lead_data["phone"], form_name,
              lead_data.get("created_time")))
        lead = db.q("SELECT * FROM leads WHERE project='RON' AND selldo_lead_id=%s",
                    (f"meta:{lead_data['meta_lead_id']}",), one=True)
        if not lead:
            return
        db.log_msg(lead["id"], "in", "leadgen", None,
                   detail=f"{leadgen_id} via {form_name}")

        import knocks
        knocks.knock_now(lead)
    except Exception as e:
        db.set_setting("leadgen_error", f"{leadgen_id}: {str(e)[:400]}")


@app.route("/webhook/wati", methods=["POST"])
def wati_webhook():
    # Legacy unauthenticated route. Kept alive so a Wati dashboard still pointing
    # here keeps working, but it can only UPDATE leads that already exist -- it
    # may never create one. Point Wati at the secret path to enable walk-ins.
    _stash_wati(request.get_data(as_text=True) or "")
    if config.WATI_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != config.WATI_WEBHOOK_SECRET:
            return "", 403
    return _wati_inbound(request.get_json(silent=True) or {}, allow_create=False)


@app.route("/webhook/wati/<token>", methods=["POST"])
def wati_webhook_secret(token):
    # Authenticated route. Wati lets you set any callback URL, so the shared
    # secret lives in the path -- no custom header needed (Wati sends none, which
    # is why WATI_WEBHOOK_SECRET must stay blank or it 403s every real post).
    # compare_digest so a wrong token cannot be found one character at a time.
    _stash_wati(request.get_data(as_text=True) or "")
    if not config.WATI_PATH_TOKEN or not secrets.compare_digest(token, config.WATI_PATH_TOKEN):
        return "", 403
    return _wati_inbound(request.get_json(silent=True) or {}, allow_create=True)


# ---------- JSON APIs ----------
@app.route("/api/summary")
@auth
def api_summary():
    # `replied` = the lead answered us on WhatsApp (tapped the day-picker or
    # typed a date). Everyone else got their day from the Meta form's own
    # preferred_date field, copied in at promotion -- they have never responded
    # to a message. Two very different levels of intent; the dashboard shows
    # them apart so nobody reads a form-fill as a confirmation.
    counts = db.q("""SELECT project, selected_date, count(*) n,
                            count(*) FILTER (WHERE last_inbound_at IS NOT NULL) replied
                     FROM leads
                     WHERE selected_date IS NOT NULL AND NOT suppressed
                     GROUP BY project, selected_date ORDER BY selected_date""")
    funnel = db.q("""SELECT project, wa_state, count(*) n FROM leads
                     GROUP BY project, wa_state""")
    errors = {k: db.get_setting(k, "") for k in
              ["selldo_error_RON", "selldo_error_ELEMENTS",
               "meta_error_RON", "meta_error_ELEMENTS",
               "meta_leads_error_RON", "meta_leads_error_ELEMENTS", "rate_capped_at"]}
    return jsonify({"day_counts": counts, "funnel": funnel, "errors": errors,
                    "paused": sequencer.paused(),
                    # Master switch state, so "why is nothing sending?" is
                    # answerable from the dashboard instead of the Railway env.
                    "sends_enabled": sendgate.sends_enabled(),
                    "sends_last_hour": wati.sends_last_hour()})


@app.route("/api/fatigue")
@auth
def api_fatigue():
    """Per-person message load. `?phone=` for one person, otherwise the heaviest.

    `lifetime` is reported but never enforced -- the owner chose a resettable
    counter over a hard lifetime ceiling, so this column exists to make a person
    who has accumulated twenty messages across four resets visible rather than
    invisible.
    """
    phone = request.args.get("phone")
    if phone:
        return jsonify(fatigue.snapshot(meta.normalize_phone(phone),
                                        request.args.get("project")))
    heaviest = db.q("""SELECT l.phone, l.project, count(*) AS proactive_sends,
                              max(ml.ts) AS last_send
                       FROM message_log ml JOIN leads l ON l.id = ml.lead_id
                       WHERE ml.direction='out' AND ml.ok
                         AND ml.msg_type LIKE 'knock%'
                       GROUP BY l.phone, l.project
                       ORDER BY count(*) DESC LIMIT 50""")
    resets = db.q("""SELECT phone, project, reason, knocks_before, created_at
                     FROM journey_resets ORDER BY id DESC LIMIT 100""")
    return jsonify({"limits": {"journey_max": config.KNOCK_MAX_PER_JOURNEY,
                               "window_max": config.FATIGUE_MAX_PER_WINDOW,
                               "window_days": config.FATIGUE_WINDOW_DAYS},
                    "heaviest": heaviest, "recent_resets": resets})


@app.route("/api/optouts")
@auth
def api_optouts():
    rows = db.q("""SELECT phone, scope, project, matched, source, note, created_at
                   FROM optouts ORDER BY id DESC LIMIT 500""")
    counts = db.q("SELECT scope, count(*) AS n FROM optouts GROUP BY scope") or []
    return jsonify({"counts": {r["scope"]: r["n"] for r in counts},
                    "recent": rows})


@app.route("/admin/optout", methods=["POST"])
@auth
def admin_optout():
    """Record an opt-out by hand, or import one from a previous campaign.

    Add-only. There is deliberately NO route that removes an opt-out: lifting one
    is a manual database action by a human who has thought about it. An endpoint
    that un-blocks people is the one mistake in this system that cannot be
    apologised for after the fact.
    """
    j = request.get_json() or {}
    phone = meta.normalize_phone(j.get("phone", ""))
    if not phone:
        return jsonify({"ok": False, "detail": "phone required"}), 400
    scope = (j.get("scope") or "global").lower()
    if scope not in (optout.GLOBAL, optout.PROJECT):
        return jsonify({"ok": False, "detail": "scope must be global or project"}), 400
    project = j.get("project")
    if scope == optout.PROJECT and not project:
        return jsonify({"ok": False, "detail": "project required for project scope"}), 400
    created = optout.record(phone, scope, project=project,
                            matched=j.get("matched"),
                            source=j.get("source") or "human",
                            note=j.get("note"))
    affected = optout.apply_to_leads(phone, scope, project=project)
    return jsonify({"ok": True, "new": created, "leads_suppressed": affected})


@app.route("/api/delivery")
@auth
def api_delivery():
    """What actually happened to our messages (Phase 0 task 5).

    The carnival's "44% blocked" figure came off Wati's dashboard because this
    system kept no record of its own. Tasks 3 and 4 set a fatigue cap and a retry
    ceiling, and both thresholds should come from this endpoint's numbers rather
    than from a guess.
    """
    hours = min(int(request.args.get("hours", 24)), 24 * 90)
    recent = db.q("""SELECT phone, status, reason, event_type, event_ts, created_at
                     FROM message_delivery
                     ORDER BY id DESC LIMIT 200""")
    unknown = db.q("""SELECT count(*) AS n FROM message_delivery
                      WHERE status='unknown'""", one=True)
    return jsonify({"rollup": db.delivery_rollup(hours),
                    # Whose fault the failures were. A spike in `system` is an
                    # alarm about US -- expired token, unapproved template -- and it
                    # is the one class that never shows up as a blocked lead.
                    "failures": failures.rollup(days=max(1, hours // 24)),
                    # Unclassified events are a to-do list, not noise: each one is
                    # a Wati event shape the parser does not know yet, kept with
                    # its raw payload so the mapping can be corrected.
                    "unknown_events": (unknown or {}).get("n", 0),
                    "recent": recent})


@app.route("/api/leads")
@auth
def api_leads():
    proj = request.args.get("project")
    where = "WHERE project=%s" if proj else ""
    rows = db.q(f"""SELECT id, project, name, phone, selldo_status, wa_state,
                           selected_date, m1_sent_at, m2_sent_at, m3_sent_at,
                           last_inbound_text, suppressed, created_at
                    FROM leads {where} ORDER BY created_at DESC LIMIT 500""",
                (proj,) if proj else None)
    return jsonify(rows)


@app.route("/api/unmatched")
@auth
def api_unmatched():
    return jsonify(db.q("""SELECT id, project, selldo_lead_id, meta_lead_id, name,
                                  selldo_status, created_at
                           FROM leads WHERE wa_state IN ('unmatched','pending_match') ORDER BY created_at DESC"""))


@app.route("/api/unmatched/<int:lead_id>/phone", methods=["POST"])
@auth
def api_fix_phone(lead_id):
    phone = meta.normalize_phone((request.get_json() or {}).get("phone", ""))
    if not phone or len(phone) < 12:
        return jsonify({"ok": False, "error": "invalid phone"}), 400
    db.x("UPDATE leads SET phone=%s, wa_state='queued', updated_at=now() WHERE id=%s",
         (phone, lead_id))
    return jsonify({"ok": True})


@app.route("/api/campaigns")
@auth
def api_campaigns():
    rows = db.q("""SELECT m.campaign_id, m.campaign_name, m.account_id, m.project,
                          COALESCE(sum(s.spend),0) spend, COALESCE(sum(s.leads),0) leads,
                          COALESCE(sum(s.impressions),0) impressions,
                          COALESCE(sum(s.clicks),0) clicks
                   FROM campaign_mapping m
                   LEFT JOIN campaign_stats s ON s.campaign_id = m.campaign_id
                   WHERE m.objective IN ('OUTCOME_LEADS','LEAD_GENERATION')
                      OR m.project IS NOT NULL
                   GROUP BY m.campaign_id, m.campaign_name, m.account_id, m.project
                   ORDER BY spend DESC""")
    for r in rows:
        r["cpl"] = round(float(r["spend"]) / r["leads"], 2) if r["leads"] else None
    return jsonify(rows)


@app.route("/api/campaigns/<cid>/map", methods=["POST"])
@auth
def api_map_campaign(cid):
    project = (request.get_json() or {}).get("project")
    if project not in ("RON", "ELEMENTS", None, ""):
        return jsonify({"ok": False}), 400
    db.x("UPDATE campaign_mapping SET project=%s WHERE campaign_id=%s",
         (project or None, cid))
    return jsonify({"ok": True})


# ---------- admin ----------
@app.route("/admin/pause", methods=["POST"])
@auth
def admin_pause():
    val = (request.get_json() or {}).get("paused", True)
    db.set_setting("global_pause", "true" if val else "false")
    return jsonify({"paused": sequencer.paused()})


@app.route("/admin/poll-now", methods=["POST"])
@auth
def admin_poll_now():
    # tick() can now block for minutes (send jitter), so run the whole pass in a
    # background thread and return immediately -- the dashboard button must not
    # hang. _seq_lock serializes this against the scheduled tick so the two never
    # send concurrently (which would double the per-tick batch budget).
    def _worker():
        with _seq_lock:
            selldo.poll_all()
            meta.poll_meta_leads()
            meta.promote_meta_leads()
            meta.poll_campaign_stats()
            match.run_matching()
            sequencer.tick()
    threading.Thread(target=_worker, name="poll-now", daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/admin/webhook-status")
@auth
def admin_webhook_status():
    # Read-only: did Wati's inbound webhook actually reach us? Shows the hit
    # counter + the last raw payload stashed by /webhook/wati. Confirms the
    # round-trip (e.g. a test reply/tap) even when the sender is NOT a known
    # lead -- handle_inbound only acks matching leads, but every POST still
    # bumps these counters. Sends nothing.
    return jsonify({
        "wati_webhook_hits": db.get_setting("wati_webhook_hits", "0"),
        "last_wati_webhook_raw": db.get_setting("last_wati_webhook_raw", ""),
        "wasender_webhook_hits": db.get_setting("webhook_hits", "0"),
    })


@app.route("/admin/delivery")
@auth
def admin_delivery():
    """Did the knocks arrive? `ok=TRUE` in our log only means Wati ACCEPTED them.

    Exists because on 2026-08-02 we sent 26 templates, logged all 26 as fine, and
    16 had failed -- which a human noticed before the system did.
    """
    hours = int(request.args.get("hours", "72"))
    rows = db.knock_delivery(hours)
    return jsonify({
        "window_hours": hours,
        "summary": db.knock_delivery_summary(hours),
        "note": ("outcome=null means no callback ever arrived for that send; "
                 "our log's ok=TRUE is acceptance by Wati, not delivery"),
        "sends": [{"lead_id": r["lead_id"], "name": r["name"], "phone": r["phone"],
                   "template": r["msg_type"], "at": str(r["ts"])[:19],
                   "outcome": r["outcome"], "fail_reason": r["fail_reason"]}
                  for r in rows[:200]],
        "unrecognised_webhooks_1h": (db.q(
            """SELECT count(*) n FROM message_delivery WHERE status='unrecognised'
               AND created_at > now() - interval '1 hour'""", one=True) or {}).get("n"),
    })


@app.route("/admin/config-check")
@auth
def admin_config_check():
    # Read-only: which build is actually serving, and what are the send gates
    # set to? Env changes on Railway trigger a redeploy, so a container can come
    # up with new variables but source that is not what you last pushed. Without
    # a probe like this that mismatch is invisible until it sends the wrong
    # messages to real people. Sends nothing, reads nothing but config.
    return jsonify({
        "code_version": CODE_VERSION,
        # bool only -- never echo the token itself, this route is behind basic
        # auth but the token is what gates lead creation from the internet.
        "wati_path_token_set": bool(config.WATI_PATH_TOKEN),
        "walkin_enabled": config.WALKIN_ENABLED,
        "m2_enabled": config.M2_ENABLED,
        "promote_enabled": config.PROMOTE_ENABLED,
        "promote_forms": config.PROMOTE_FORMS,
        "promote_window_hours": config.PROMOTE_WINDOW_HOURS,
        "max_sends_per_hour": config.MAX_SENDS_PER_HOUR,
        "send_batch_per_tick": config.SEND_BATCH_PER_TICK,
        "daily_send_cap": config.DAILY_SEND_CAP,
        "send_enabled": sendgate.sends_enabled(),
        "retry_max_recipient": config.RETRY_MAX_RECIPIENT,
        "retry_max_transient": config.RETRY_MAX_TRANSIENT,
        "knock_max_per_journey": config.KNOCK_MAX_PER_JOURNEY,
        "fatigue_max_per_window": config.FATIGUE_MAX_PER_WINDOW,
        "fatigue_window_days": config.FATIGUE_WINDOW_DAYS,
        "embed_model": config.EMBED_MODEL,
        "embed_dim": config.EMBED_DIM,
    })


@app.route("/api/queue")
@auth
def api_queue():
    """Queue depth, and any job that gave up.

    `recent_failures` is the important number: each one is a customer message that
    was never answered, and nothing else in the system will surface that."""
    return jsonify(jobs.stats())


@app.route("/api/kb")
@auth
def api_kb():
    """Is the knowledge base actually available, and what is in it?

    Answers the question "why is the bot escalating everything" without anyone
    reading deploy logs -- the commonest cause will be a database user that cannot
    CREATE EXTENSION, which is a vendor action rather than a code fix.
    """
    return jsonify(kb.stats())


@app.route("/admin/embed-check")
@auth
def admin_embed_check():
    """Confirms the embedding key works, the model name is real, and its dimension
    matches config -- without touching the corpus. A wrong model name is otherwise
    discovered halfway through an ingest."""
    return jsonify(embed.probe())


@app.route("/admin/wati-check")
@auth
def admin_wati_check():
    # Connectivity probe: confirms WATI_BASE + WATI_TOKEN reach Wati and lists
    # the templates Wati has for this number. No message sent.
    return jsonify(wati.check_connection())


@app.route("/admin/test-send", methods=["POST"])
@auth
def admin_test_send():
    j = request.get_json() or {}
    phone = meta.normalize_phone(j.get("phone", ""))
    # This route was the one way to reach a real person without passing the send
    # gate. It now goes through the same door as everything else -- an admin
    # convenience is not a reason for a message to skip opt-out and fatigue
    # checks, and "it was only a test send" is not a defence to the recipient.
    allowed, reason = sendgate.check(phone, "test", project=j.get("project"))
    if not allowed:
        return jsonify({"ok": False, "detail": f"blocked:{reason}"}), 409
    # Free-text test send: only DELIVERS if `phone` messaged this number within
    # the last 24h (WhatsApp session rule). Outside that window WhatsApp rejects
    # it, but the API response in `detail` still confirms token/URL wiring.
    ok, detail = wati.send_text(phone, j.get("body", "Test from GTB Carnival system."))
    return jsonify({"ok": ok, "detail": detail})


# Serializes the scheduled tick against a manual Poll-now pass so bulk sends
# from the two paths never overlap and blow past the per-tick batch budget.
_seq_lock = threading.Lock()


def _tick_with_matching():
    if not _seq_lock.acquire(blocking=False):
        return  # a Poll-now pass is already running; skip this scheduled tick
    try:
        meta.promote_meta_leads()
        match.run_matching()
        sequencer.tick()
    finally:
        _seq_lock.release()


# ---------- scheduler ----------
def start_scheduler():
    from datetime import datetime, timedelta
    soon = lambda s: datetime.now() + timedelta(seconds=s)
    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    # next_run_time=soon(...) -> first run right after boot, staggered so the
    # Graph API isn't hit by everything at once; then normal intervals.
    sched.add_job(selldo.poll_all, "interval", minutes=config.SELLDO_POLL_MIN,
                  id="selldo", max_instances=1, coalesce=True,
                  next_run_time=soon(5))
    # Every minute: this is the fast path to a new lead now, not Sell.do.
    sched.add_job(meta.poll_meta_leads, "interval", minutes=config.META_LEADS_POLL_MIN,
                  id="meta_leads", max_instances=1, coalesce=True,
                  next_run_time=soon(20))
    sched.add_job(meta.poll_campaign_stats, "interval", minutes=config.META_ADS_POLL_MIN,
                  id="meta", max_instances=1, coalesce=True,
                  next_run_time=soon(60))
    sched.add_job(_tick_with_matching, "interval", minutes=config.SEQUENCER_TICK_MIN,
                  id="seq", max_instances=1, coalesce=True,
                  next_run_time=soon(90))
    sched.start()


db.init_db()

# Knowledge-base schema runs SEPARATELY and never raises. `CREATE EXTENSION vector`
# needs a privilege the database user may not have, and the `vector` column type
# does not exist until the extension does -- so folding this into db.init_db() would
# mean a Railway instance without pgvector fails to boot the WEB APP, and the
# webhook stops answering because of a knowledge-base problem. Outcome is recorded
# in settings and readable at /api/kb.
kb.init_kb()

if os.environ.get("DISABLE_SCHEDULER") != "1":
    start_scheduler()

# The worker normally runs as its own Railway service (`python worker.py`). While
# volume is small it can run inside this process instead -- identical loop, so the
# split later is an environment change rather than a code change. Off by default:
# running it here means a deploy restarts both at once and both share one container.
if os.environ.get("WORKER_IN_PROCESS", "false").lower() in ("1", "true", "yes"):
    worker.start_in_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

import os
import datetime as _dt
import threading
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response
from flask.json.provider import DefaultJSONProvider
from apscheduler.schedulers.background import BackgroundScheduler
import config
import db
import selldo
import meta
import sequencer
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
    return render_template("dashboard.html", event=config.EVENT_NAME)


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


@app.route("/webhook/wati", methods=["POST"])
def wati_webhook():
    # Same diagnostic stash + dedup as the wasender route so we can see exactly
    # what Wati delivers and never process one message twice.
    try:
        raw = request.get_data(as_text=True) or ""
        db.set_setting("last_wati_webhook_raw",
                       (sequencer.now_ist().isoformat() + " " + raw)[:2000])
        n = int(db.get_setting("wati_webhook_hits", "0") or "0") + 1
        db.set_setting("wati_webhook_hits", str(n))
    except Exception:
        pass
    if config.WATI_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != config.WATI_WEBHOOK_SECRET:
            return "", 403
    payload = request.get_json(silent=True) or {}
    # Dedup on Wati's message id so retried deliveries ack once.
    _d = payload.get("data", payload)
    _mid = (_d.get("id") or _d.get("whatsappMessageId")
            or _d.get("conversationId")) if isinstance(_d, dict) else None
    if not db.mark_webhook_new(_mid):
        return jsonify({"ok": True, "dup": True})
    phone, text = wati.parse_inbound(payload)
    if phone and text:
        sequencer.handle_inbound(phone, text)
    return jsonify({"ok": True})


# ---------- JSON APIs ----------
@app.route("/api/summary")
@auth
def api_summary():
    counts = db.q("""SELECT project, selected_date, count(*) n FROM leads
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
                    "sends_last_hour": wati.sends_last_hour(),
                    "event_dates": [d.isoformat() for d in config.EVENT_DATES]})


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
    sched.add_job(meta.poll_meta_leads, "interval", minutes=config.SELLDO_POLL_MIN,
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
if os.environ.get("DISABLE_SCHEDULER") != "1":
    start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

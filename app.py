import os
import threading
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response
from apscheduler.schedulers.background import BackgroundScheduler
import config
import db
import selldo
import meta
import sequencer
import wasender
import match

app = Flask(__name__)


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
    if config.WASENDER_WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != config.WASENDER_WEBHOOK_SECRET:
            return "", 403
    phone, text = wasender.parse_inbound(request.get_json(silent=True) or {})
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
                    "sends_last_hour": wasender.sends_last_hour(),
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


@app.route("/admin/test-send", methods=["POST"])
@auth
def admin_test_send():
    j = request.get_json() or {}
    phone = meta.normalize_phone(j.get("phone", ""))
    ok, detail = wasender.send_text(phone, j.get("body", "Test from GTB Carnival system."))
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

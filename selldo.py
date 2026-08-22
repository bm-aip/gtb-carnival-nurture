"""
Polls the two Sell.do reporting databases for qualified leads.

IMPORTANT: The SQL in sql/selldo_leads.sql is a BEST-GUESS against Sell.do's
reporting schema. Run scripts/discover_selldo.py FIRST and adjust the query.
Placeholders: %(project)s and %(campaign)s are bound at runtime.
Expected output columns: selldo_lead_id, meta_lead_id, name, status
"""
import os
import psycopg2
import psycopg2.extras
import config
import db
import meta

SQL_PATH = os.path.join(os.path.dirname(__file__), "sql", "selldo_leads.sql")


def _load_sql():
    with open(SQL_PATH) as f:
        return f.read()


def _record_stage_move(lead_id, project_key, selldo_lead_id, from_stage, to_stage):
    """Write one row of Sell.do stage movement, with the knock count frozen in.

    Called at the two places a stage is learned: the first sighting of a lead
    (from_stage NULL, the baseline) and every later change.

    The knock figures are counted NOW and stored, not left to a join at read time.
    A join would re-derive them from message_log, which is mutable -- pruning it,
    or wiping one handset via /admin/reset-test, would quietly rewrite history that
    is supposed to be evidence.
    """
    k = db.q(r"""SELECT count(*) AS n, max(ml.ts) AS last_at
                FROM message_log ml
                WHERE ml.lead_id = %s AND ml.direction = 'out' AND ml.ok = TRUE
                  AND ml.msg_type LIKE 'knock\_%%'""",
             (lead_id,), one=True) or {}
    db.x("""INSERT INTO selldo_stage_history
                (lead_id, project, selldo_lead_id, from_stage, to_stage,
                 knocks_before, last_knock_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
         (lead_id, project_key, selldo_lead_id, from_stage, to_stage,
          k.get("n") or 0, k.get("last_at")))


def poll_project(project_key):
    cfg = config.SELLDO[project_key]

    # No allow-listed campaign for this project -> poll nothing at all. Checked
    # before we connect, so an empty list costs no round trip. An empty list must
    # mean "touch no lead", never "match everything".
    campaigns = [name.lower() for name in cfg.get("campaigns") or []]
    if not campaigns:
        return

    sql = _load_sql()
    rows = []
    c = psycopg2.connect(cfg["db_url"])
    try:
        c.set_session(readonly=True)
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"project": cfg["project"], "campaigns": campaigns})
            rows = cur.fetchall()
    finally:
        c.close()

    seen_ids = set()
    for r in rows:
        sid = str(r["selldo_lead_id"])
        seen_ids.add(sid)

        existing = db.q(
            "SELECT * FROM leads WHERE project=%s AND selldo_lead_id=%s",
            (project_key, sid), one=True)

        if existing:
            if (existing["selldo_status"] != r.get("status")
                    or existing.get("campaign") != r.get("campaign")):
                # Stage history BEFORE the overwrite -- this is the only moment both
                # the old and the new stage exist at once.
                if existing["selldo_status"] != r.get("status"):
                    _record_stage_move(existing["id"], project_key, sid,
                                       existing["selldo_status"], r.get("status"))
                db.x("""UPDATE leads SET selldo_status=%s, campaign=%s, updated_at=now()
                        WHERE id=%s""",
                     (r.get("status"), r.get("campaign"), existing["id"]))
            # The carnival build suppressed any lead presales had not already marked
            # "Interested". That is presales logic, and the bot REPLACES presales --
            # gating on it would suppress exactly the new enquiries the bot exists to
            # qualify. Suppression is now the opt-out ledger's job (Phase 0 task 2).
            continue

        # Already promoted straight from Meta (meta.promote_meta_leads). Sell.do
        # hands us a different lead id for the same human, so without this guard
        # we would insert a second row and message them twice.
        if r.get("meta_lead_id") and db.q(
                "SELECT 1 FROM leads WHERE project=%s AND meta_lead_id=%s",
                (project_key, str(r["meta_lead_id"])), one=True):
            continue

        # New qualified lead → phone comes from the meta_leads matcher
        db.x("""INSERT INTO leads (project, selldo_lead_id, meta_lead_id, name,
                                   selldo_status, selldo_response_at, campaign,
                                   wa_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending_match')
                ON CONFLICT (project, selldo_lead_id) DO NOTHING""",
             (project_key, sid, r.get("meta_lead_id"), r.get("name"),
              r.get("status"), r.get("response_at"), r.get("campaign")))

        # Baseline row: the stage this lead was already in when we first saw it.
        # Without it every lead's first real move would have nothing to move FROM,
        # and "was this lead already qualified before we spoke?" would be a guess.
        fresh = db.q("SELECT id FROM leads WHERE project=%s AND selldo_lead_id=%s",
                     (project_key, sid), one=True)
        if fresh:
            _record_stage_move(fresh["id"], project_key, sid, None, r.get("status"))


def poll_all():
    for pk in config.SELLDO:
        try:
            poll_project(pk)
        except Exception as e:
            db.set_setting(f"selldo_error_{pk}", str(e)[:500])
        else:
            db.set_setting(f"selldo_error_{pk}", "")

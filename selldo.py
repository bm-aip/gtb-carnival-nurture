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


def poll_project(project_key):
    cfg = config.SELLDO[project_key]
    sql = _load_sql()
    rows = []
    c = psycopg2.connect(cfg["db_url"])
    try:
        c.set_session(readonly=True)
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"project": cfg["project"], "campaign": cfg["campaign"]})
            rows = cur.fetchall()
    finally:
        c.close()

    seen_ids = set()
    for r in rows:
        sid = str(r["selldo_lead_id"])
        seen_ids.add(sid)
        qualified = config.status_qualifies(r.get("status"))

        existing = db.q(
            "SELECT * FROM leads WHERE project=%s AND selldo_lead_id=%s",
            (project_key, sid), one=True)

        if existing:
            if existing["selldo_status"] != r.get("status"):
                db.x("UPDATE leads SET selldo_status=%s, updated_at=now() WHERE id=%s",
                     (r.get("status"), existing["id"]))
            if not qualified and not existing["suppressed"]:
                db.x("UPDATE leads SET suppressed=TRUE, wa_state='suppressed', updated_at=now() WHERE id=%s",
                     (existing["id"],))
            elif qualified and existing["suppressed"]:
                db.x("UPDATE leads SET suppressed=FALSE, updated_at=now() WHERE id=%s",
                     (existing["id"],))
            continue

        if not qualified:
            continue

        # New qualified lead → phone comes from the meta_leads matcher
        db.x("""INSERT INTO leads (project, selldo_lead_id, meta_lead_id, name,
                                   selldo_status, selldo_response_at, wa_state)
                VALUES (%s,%s,%s,%s,%s,%s,'pending_match')
                ON CONFLICT (project, selldo_lead_id) DO NOTHING""",
             (project_key, sid, r.get("meta_lead_id"), r.get("name"),
              r.get("status"), r.get("response_at")))


def poll_all():
    for pk in config.SELLDO:
        try:
            poll_project(pk)
        except Exception as e:
            db.set_setting(f"selldo_error_{pk}", str(e)[:500])
        else:
            db.set_setting(f"selldo_error_{pk}", "")

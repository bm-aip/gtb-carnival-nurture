"""
Targeted Sell.do discovery, pass 2. Dumps ONLY what's needed to finalize
sql/selldo_leads.sql, and writes everything to discover2_<client>.txt
so console truncation can't eat it.

  $env:SELLDO_DB_URL = "postgresql://..."
  python scripts/discover2.py
"""
import os
import sys
import re
import psycopg2
import psycopg2.extras

url = os.environ.get("SELLDO_DB_URL")
if not url:
    sys.exit("Set SELLDO_DB_URL env var first.")

client_id = re.search(r"reporting_client(\d+)", url).group(1)
out_path = f"discover2_{client_id}.txt"
out = open(out_path, "w", encoding="utf-8")

def p(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    out.write(line + "\n")

c = psycopg2.connect(url)
c.set_session(readonly=True)
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def columns(table):
    cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s
                   ORDER BY ordinal_position""", (table,))
    return cur.fetchall()

def dump_table(table, n=5, order_desc=True):
    p(f"\n===== {table} =====")
    cols = columns(table)
    if not cols:
        p("   (table not found)")
        return
    for col in cols:
        p(f"   {col['column_name']:<36} {col['data_type']}")
    try:
        order = "ORDER BY id DESC" if order_desc else ""
        cur.execute(f'SELECT * FROM public."{table}" {order} LIMIT {n}')
        for r in cur.fetchall():
            slim = {k: (str(v)[:60] if v is not None else None) for k, v in r.items()
                    if v is not None}
            p(f"   sample: {slim}")
    except Exception as e:
        p(f"   (sample failed: {e})")
        c.rollback()

# 1. The main leads table — full columns + samples
dump_table("reporting_leads", n=3)

# 2. Stage / status side tables
for t in ("reporting_lead_stages", "reporting_lead_statuses",
          "reporting_lead_stage_change_logs"):
    dump_table(t, n=5)

# 3. Any table that could carry project / campaign / source / integration ids
p("\n===== Other candidate tables (project/campaign/source/activity) =====")
cur.execute("""SELECT table_name FROM information_schema.tables
               WHERE table_schema='public'
                 AND (table_name ILIKE '%project%' OR table_name ILIKE '%campaign%'
                      OR table_name ILIKE '%source%' OR table_name ILIKE '%sub_source%'
                      OR table_name ILIKE '%integration%' OR table_name ILIKE '%form%')
               ORDER BY table_name""")
others = [r["table_name"] for r in cur.fetchall()]
p("Found:", others)
for t in others[:10]:
    dump_table(t, n=3)

# 4. Columns on reporting_leads that smell like Meta linkage
p("\n===== reporting_leads columns matching fb/meta/lead_id/reference =====")
for col in columns("reporting_leads"):
    if any(k in col["column_name"].lower() for k in
           ("fb", "meta", "facebook", "form", "reference", "external", "src", "utm")):
        p("   ", col["column_name"], col["data_type"])

c.close()
out.close()
print(f"\nWritten to {out_path} — send that file to Claude.")

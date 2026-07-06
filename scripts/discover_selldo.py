"""
Step 1 of the runbook. Run against EACH Sell.do reporting DB:

  SELLDO_DB_URL="postgresql://..." python scripts/discover_selldo.py

Prints candidate lead tables, their columns, and 3 sample rows so the
query in sql/selldo_leads.sql can be calibrated. Read-only.
"""
import os
import sys
import psycopg2
import psycopg2.extras

url = os.environ.get("SELLDO_DB_URL")
if not url:
    sys.exit("Set SELLDO_DB_URL env var first.")

c = psycopg2.connect(url)
c.set_session(readonly=True)
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=== Tables/views that look lead-related ===")
cur.execute("""
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog','information_schema')
      AND (table_name ILIKE '%lead%' OR table_name ILIKE '%prospect%'
           OR table_name ILIKE '%enquir%')
    ORDER BY table_schema, table_name""")
tables = cur.fetchall()
for t in tables:
    print(f"  {t['table_schema']}.{t['table_name']} ({t['table_type']})")

print("\n=== Columns of interest per table ===")
for t in tables:
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position""", (t["table_schema"], t["table_name"]))
    cols = cur.fetchall()
    interesting = [c_ for c_ in cols if any(k in c_["column_name"].lower() for k in
                   ("id", "name", "stage", "status", "project", "campaign",
                    "source", "fb", "meta", "utm", "created"))]
    print(f"\n-- {t['table_schema']}.{t['table_name']} "
          f"({len(cols)} cols, showing {len(interesting)} relevant)")
    for c_ in interesting:
        print(f"     {c_['column_name']:<32} {c_['data_type']}")

    try:
        cur.execute(
            f'SELECT * FROM "{t["table_schema"]}"."{t["table_name"]}" '
            f"ORDER BY 1 DESC LIMIT 3")
        rows = cur.fetchall()
        for r in rows:
            slim = {k: v for k, v in r.items()
                    if v is not None and any(x in k.lower() for x in
                    ("id", "name", "stage", "status", "project", "campaign", "source"))}
            print(f"     sample: {slim}")
    except Exception as e:
        print(f"     (sample failed: {e})")
        c.rollback()

c.close()
print("\nPaste this output back to Claude to finalize sql/selldo_leads.sql.")

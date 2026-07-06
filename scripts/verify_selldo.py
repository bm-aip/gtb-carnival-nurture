"""
FINAL verification before deploy. Run once per client:

  $env:SELLDO_DB_URL = "postgresql://reporting_client1436:..."
  python scripts/verify_selldo.py

  $env:SELLDO_DB_URL = "postgresql://reporting_client1898:..."
  python scripts/verify_selldo.py

Writes verify_<client>.txt. Upload both to Claude.
Checks: exact stage names, campaign existence, the real query's row count,
and — critically — whether utm_lead_id is actually populated.
"""
import os
import re
import sys
import psycopg2
import psycopg2.extras

url = os.environ.get("SELLDO_DB_URL")
if not url:
    sys.exit("Set SELLDO_DB_URL env var first.")

client_id = re.search(r"reporting_client(\d+)", url).group(1)
PARAMS = {
    "1436": {"project": "Republic Of Nature", "campaign": "RON_Carnival"},
    "1898": {"project": "Elements Common", "campaign": "Meta"},
}[client_id]

out = open(f"verify_{client_id}.txt", "w", encoding="utf-8")
def p(*a):
    line = " ".join(str(x) for x in a)
    print(line); out.write(line + "\n")

c = psycopg2.connect(url)
c.set_session(readonly=True)
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

p(f"### client {client_id} | params {PARAMS}\n")

p("--- 1. ALL lead stages (exact names matter) ---")
cur.execute("SELECT id, name, pipeline FROM reporting_lead_stages ORDER BY name")
for r in cur.fetchall():
    p(f"   {r['id']:>7}  [{r['pipeline']}]  {r['name']}")

p("\n--- 2. Campaigns matching target / carnival / meta ---")
cur.execute("""SELECT id, name, active, created_at FROM reporting_campaigns
               WHERE name ILIKE '%%carnival%%' OR name ILIKE '%%meta%%'
                  OR name = %(campaign)s ORDER BY created_at DESC""", PARAMS)
rows = cur.fetchall()
for r in rows:
    p(f"   {r['id']:>7}  active={r['active']}  {r['name']}  ({r['created_at']})")
if not any(r["name"] == PARAMS["campaign"] for r in rows):
    p(f"   !! Campaign named {PARAMS['campaign']!r} NOT FOUND — filter will match nothing.")

p("\n--- 3. Distinct sub_sources on responses since June 25 (top 15) ---")
cur.execute("""SELECT sub_source, count(*) n FROM reporting_campaign_responses
               WHERE created_at >= '2026-06-25' GROUP BY sub_source
               ORDER BY n DESC LIMIT 15""")
for r in cur.fetchall():
    p(f"   {r['n']:>5}  {r['sub_source']}")

p("\n--- 4. utm_lead_id population on 'fb lead ad' responses since June 25 ---")
cur.execute("""SELECT count(*) total,
                      count(utm_lead_id) with_lead_id,
                      count(utm_form_id) with_form_id
               FROM reporting_campaign_responses
               WHERE created_at >= '2026-06-25' AND medium_value = 'fb lead ad'""")
r = cur.fetchone()
p(f"   fb-lead-ad responses: {r['total']} | utm_lead_id set: {r['with_lead_id']} "
  f"| utm_form_id set: {r['with_form_id']}")
if r["total"] and not r["with_lead_id"]:
    p("   !! utm_lead_id is NEVER populated — phone resolution design must change.")

p("\n--- 5. THE ACTUAL QUERY ---")
with open(os.path.join(os.path.dirname(__file__), "..", "sql", "selldo_leads.sql")) as f:
    sql = f.read()
cur.execute(sql, PARAMS)
rows = cur.fetchall()
p(f"   rows returned: {len(rows)}")
from collections import Counter
p("   by status:", dict(Counter((r["status"] or "?") for r in rows)))
p(f"   meta_lead_id present: {sum(1 for r in rows if r['meta_lead_id'])} / {len(rows)}")
for r in rows[:5]:
    p("   sample:", dict(r))

p("\n--- 6. NULL-project responses on target campaign (leakage check) ---")
cur.execute("""SELECT count(*) n FROM reporting_campaign_responses cr
               JOIN reporting_campaigns c ON c.id = cr.reporting_campaign_id
               WHERE c.name = %(campaign)s AND cr.created_at >= '2026-06-25'
                 AND cr.reporting_project_id IS NULL""", PARAMS)
p("   responses with NULL project on target campaign:", cur.fetchone()["n"])

c.close(); out.close()
print(f"\nWritten to verify_{client_id}.txt — upload both files to Claude.")

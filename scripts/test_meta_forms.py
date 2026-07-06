"""
Proves the NEW phone-resolution path end to end with your real tokens:
token -> page access tokens -> lead forms -> recent leads with name+phone.

  $env:META_TOKEN = "EAAS..."   (run once per token)
  python scripts/test_meta_forms.py

Writes meta_forms_<userid>.txt. If this shows leads with phone numbers,
the matcher has everything it needs.
"""
import os
import sys
import requests

GRAPH = "https://graph.facebook.com/v19.0"
token = os.environ.get("META_TOKEN")
if not token:
    sys.exit("Set META_TOKEN env var first.")

me = requests.get(f"{GRAPH}/me", params={"access_token": token}, timeout=20).json()
out = open(f"meta_forms_{me.get('id','x')}.txt", "w", encoding="utf-8")
def p(*a):
    line = " ".join(str(x) for x in a)
    print(line); out.write(line + "\n")

p("Identity:", me)

pages = requests.get(f"{GRAPH}/me/accounts",
                     params={"access_token": token,
                             "fields": "id,name,access_token", "limit": 100},
                     timeout=20).json().get("data", [])
p(f"\nPages with tokens: {len(pages)}")
for pg in pages:
    p(f"\n=== PAGE {pg['id']}  {pg['name']} ===")
    ptoken = pg.get("access_token")
    if not ptoken:
        p("   !! no page token — this page unusable for lead pulls")
        continue
    forms = requests.get(f"{GRAPH}/{pg['id']}/leadgen_forms",
                         params={"access_token": ptoken,
                                 "fields": "id,name,status", "limit": 50},
                         timeout=20).json()
    if "error" in forms:
        p("   forms error:", forms["error"].get("message"))
        continue
    for form in forms.get("data", []):
        leads = requests.get(f"{GRAPH}/{form['id']}/leads",
                             params={"access_token": ptoken,
                                     "fields": "id,created_time,field_data",
                                     "limit": 3}, timeout=20).json()
        if "error" in leads:
            p(f"   form {form['name']} [{form.get('status')}]: LEADS ERROR "
              f"{leads['error'].get('message')}")
            continue
        n = len(leads.get("data", []))
        p(f"   form {form['id']}  {form['name']}  [{form.get('status')}]  "
          f"recent leads fetched: {n}")
        for lead in leads.get("data", []):
            fields = {f["name"]: (f.get("values") or [""])[0]
                      for f in lead.get("field_data", [])}
            # mask phone middle digits in the report
            for k, v in list(fields.items()):
                if "phone" in k.lower() and len(str(v)) > 6:
                    fields[k] = str(v)[:4] + "****" + str(v)[-3:]
            p(f"      {lead['created_time']}  {fields}")

out.close()
print(f"\nWritten to meta_forms_{me.get('id','x')}.txt — upload to Claude.")

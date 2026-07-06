"""
Verifies both Meta tokens BEFORE go-live:
  1. Token validity + granted scopes (needs leads_retrieval or page admin)
  2. Visible ad accounts
  3. Optional: resolve one real lead ID -> phone

  META_TOKEN="EAAX..." python scripts/test_meta.py [lead_id]
"""
import os
import sys
import requests

GRAPH = "https://graph.facebook.com/v19.0"
token = os.environ.get("META_TOKEN")
if not token:
    sys.exit("Set META_TOKEN env var first.")

r = requests.get(f"{GRAPH}/me", params={"access_token": token,
                                        "fields": "id,name"}, timeout=20).json()
print("Identity:", r)

r = requests.get(f"{GRAPH}/me/permissions", params={"access_token": token},
                 timeout=20).json()
granted = [p["permission"] for p in r.get("data", []) if p.get("status") == "granted"]
print("Granted scopes:", granted)
if "leads_retrieval" not in granted:
    print("!! leads_retrieval NOT granted — lead phone lookup will fail. "
          "Fix the token before go-live.")

r = requests.get(f"{GRAPH}/me/adaccounts",
                 params={"access_token": token, "fields": "id,name"}, timeout=20).json()
print("Ad accounts:", [(a["id"], a.get("name")) for a in r.get("data", [])])

if len(sys.argv) > 1:
    lead_id = sys.argv[1]
    r = requests.get(f"{GRAPH}/{lead_id}",
                     params={"access_token": token, "fields": "field_data,created_time"},
                     timeout=20).json()
    print("Lead lookup:", r)

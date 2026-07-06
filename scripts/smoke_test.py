"""Smoke test against local Postgres. Mocks Wasender + Meta HTTP calls."""
import os
os.environ.update({
    "DATABASE_URL": "postgresql://carnival:carnival@localhost:5432/carnival",
    "SELLDO_DB_URL_RON": "postgresql://x", "SELLDO_DB_URL_ELEMENTS": "postgresql://x",
    "META_TOKEN_RON": "t1", "META_TOKEN_ELEMENTS": "t2",
    "WASENDER_API_KEY": "k", "DISABLE_SCHEDULER": "1",
    "EVENT_MAPS_LINK": "https://maps.app.goo.gl/MU1rPtQ6qLFC74jy8",
})
from datetime import datetime, timedelta, date
from unittest.mock import patch
import base64

sent = []
def fake_send(phone, body):
    sent.append((phone, body)); return True, "mock"

import wasender
wasender.send_text = fake_send

import app as appmod
import db, sequencer, parser

client = appmod.app.test_client()
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:change-me").decode()}

# --- parser cases ---
d10, d11, d12 = date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)
cases = {"1": d10, "2": d11, "3": d12, " 2 ": d11, "10": d10, "11th": d11,
         "on 12 july": d12, "Friday 10 July": d10, "saturday": d11,
         "I will come on the 12th": d12, "ok": None, "yes": None, "5": None}
for t, want in cases.items():
    got = parser.parse_date_reply(t)
    assert got == want, f"parser({t!r}) = {got}, want {want}"
print("PARSER_OK")

# --- lead lifecycle ---
db.x("TRUNCATE leads, message_log, campaign_mapping, campaign_stats, settings, meta_leads")
db.x("""INSERT INTO leads (project, selldo_lead_id, name, selldo_status, wa_state)
        VALUES ('RON','sd1','Karthik Raja (#101)','(Pre Sales) Interested','pending_match'),
               ('ELEMENTS','sd2','Lakshmi Lakshmi (#102)','(Pre Sales) Site Visit Scheduled','pending_match'),
               ('RON','sd3','No Phone Guy (#103)','Interested','pending_match')""")
db.x("""INSERT INTO meta_leads (meta_lead_id, project, name, phone, created_time)
        VALUES ('m1','RON','Karthik Raja','919876543210', now()),
               ('m2','ELEMENTS','Lakshmi','919812345678', now())""")

# status normalization
import config
assert config.status_qualifies('(Pre Sales) Interested')
assert config.status_qualifies('Site Visit Scheduled')
assert config.status_qualifies('(Pre Sales) Site Visit Scheduled')
assert not config.status_qualifies('(Pre Sales) Not Connected')
assert not config.status_qualifies('Site Visit Completed')
print("STATUS_NORM_OK")

# matcher: sd1 exact-ish, sd2 doubled-name subset, sd3 no candidate
import match
match.run_matching()
r = db.q("SELECT phone, wa_state FROM leads WHERE selldo_lead_id='sd1'", one=True)
assert r["phone"]=='919876543210' and r["wa_state"]=='queued', r
r = db.q("SELECT phone, wa_state FROM leads WHERE selldo_lead_id='sd2'", one=True)
assert r["phone"]=='919812345678' and r["wa_state"]=='queued', r
r = db.q("SELECT wa_state FROM leads WHERE selldo_lead_id='sd3'", one=True)
assert r["wa_state"]=='pending_match', r  # stays pending (<24h old)
print("MATCHER_OK")

with patch.object(sequencer, "now_ist") as mock_now:
    # July 6, 10:00 IST — normal M1, not combined
    mock_now.return_value = datetime(2026, 7, 6, 10, 0, tzinfo=sequencer.IST)
    sequencer.tick()
assert len(sent) == 2, sent
assert "87L" in sent[0][1] and "Elements" in sent[1][1]
assert "maps.app" not in sent[0][1], "M1 on July 6 must NOT be combined"
r = db.q("SELECT wa_state FROM leads WHERE selldo_lead_id='sd1'", one=True)
assert r["wa_state"] == "m1_sent"
print("M1_OK")

# --- inbound date selection + ack ---
sent.clear()
sequencer.handle_inbound("919876543210", "2")
r = db.q("SELECT selected_date, wa_state FROM leads WHERE selldo_lead_id='sd1'", one=True)
assert str(r["selected_date"]) == "2026-07-11" and r["wa_state"] == "date_selected"
assert len(sent) == 1 and "Saturday 11 July" in sent[0][1]
print("INBOUND_OK")

# --- M2 after 24h for non-responder ---
sent.clear()
db.x("UPDATE leads SET m1_sent_at = now() - interval '25 hours' WHERE selldo_lead_id='sd2'")
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 7, 11, 0, tzinfo=sequencer.IST)
    sequencer.tick()
assert any("checking in" in b for _, b in sent), sent
print("M2_OK")

# --- M3 evening before selected date ---
sent.clear()
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 10, 19, 0, tzinfo=sequencer.IST)  # eve of 11th
    sequencer.tick()
m3s = [b for _, b in sent if "tomorrow" in b]
assert len(m3s) == 1 and "maps.app" in m3s[0], sent
print("M3_OK")

# --- generic M3 July 9 evening to non-responder ---
sent.clear()
db.x("UPDATE leads SET m3_sent_at=NULL, m2_sent_at=now(), wa_state='m2_sent' WHERE selldo_lead_id='sd2'")
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 9, 18, 30, tzinfo=sequencer.IST)
    sequencer.tick()
assert any("Walk in on any day" in b for _, b in sent), sent
print("M3_GENERIC_OK")

# --- collapse: lead queued on July 9 gets combined M1 ---
sent.clear()
db.x("""INSERT INTO leads (project, selldo_lead_id, name, phone, selldo_status, wa_state)
        VALUES ('RON','sd4','Late Guy','919000000001','Interested','queued')""")
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 9, 12, 0, tzinfo=sequencer.IST)
    sequencer.tick()
combined = [b for p, b in sent if p == "919000000001"]
assert combined and "maps.app" in combined[0], "late M1 must carry venue link"
print("COLLAPSE_OK")

# --- suppression cancels pending ---
db.x("UPDATE leads SET suppressed=TRUE, wa_state='suppressed' WHERE selldo_lead_id='sd4'")
sent.clear()
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 9, 18, 30, tzinfo=sequencer.IST)
    sequencer.tick()
assert not any(p == "919000000001" for p, _ in sent), "suppressed lead was messaged"
print("SUPPRESS_OK")

# --- pause switch ---
db.set_setting("global_pause", "true")
db.x("""INSERT INTO leads (project, selldo_lead_id, name, phone, selldo_status, wa_state)
        VALUES ('RON','sd5','Pause Guy','919000000002','Interested','queued')""")
sent.clear()
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 7, 12, 0, tzinfo=sequencer.IST)
    sequencer.tick()
assert not sent, "pause switch ignored"
db.set_setting("global_pause", "false")
print("PAUSE_OK")

# --- form-date preference: confirm-style M1, no M2, straight to date_selected ---
db.x("""INSERT INTO leads (project, selldo_lead_id, name, selldo_status, wa_state)
        VALUES ('RON','sd6','Baptista (#200)','(Pre Sales) Interested','pending_match')""")
db.x("""INSERT INTO meta_leads (meta_lead_id, project, name, phone, created_time, preferred_date)
        VALUES ('m6','RON','Baptista','919555000111', now(), '2026-07-10')""")
match.run_matching()
r = db.q("SELECT phone, selected_date, wa_state FROM leads WHERE selldo_lead_id='sd6'", one=True)
assert r["phone"]=='919555000111' and str(r["selected_date"])=='2026-07-10' and r["wa_state"]=='queued', r
sent.clear()
with patch.object(sequencer, "now_ist") as mock_now:
    mock_now.return_value = datetime(2026, 7, 7, 10, 0, tzinfo=sequencer.IST)
    sequencer.tick()
m1s = [b for p_, b in sent if p_=='919555000111']
assert m1s and "You've chosen Friday 10 July" in m1s[0] and "Reply 1" not in m1s[0].split("chosen")[0], m1s
r = db.q("SELECT wa_state FROM leads WHERE selldo_lead_id='sd6'", one=True)
assert r["wa_state"]=='date_selected', r
print("FORM_DATE_CONFIRM_OK")

# --- API routes ---
assert client.get("/api/summary").status_code == 401, "auth not enforced"
for ep in ["/api/summary", "/api/leads", "/api/unmatched", "/api/campaigns", "/"]:
    rr = client.get(ep, headers=AUTH)
    assert rr.status_code == 200, (ep, rr.status_code)
rr = client.post("/api/unmatched/%d/phone" %
                 db.q("SELECT id FROM leads WHERE selldo_lead_id='sd3'", one=True)["id"],
                 json={"phone": "9876500000"}, headers=AUTH)
assert rr.get_json()["ok"]
r = db.q("SELECT phone, wa_state FROM leads WHERE selldo_lead_id='sd3'", one=True)
assert r["phone"] == "919876500000" and r["wa_state"] == "queued"
print("API_OK")

# --- webhook parse shapes ---
p1 = {"data": {"messages": {"key": {"remoteJid": "919876543210@s.whatsapp.net"},
      "message": {"conversation": "3"}}}}
ph, tx = wasender.parse_inbound(p1)
assert ph == "919876543210" and tx == "3", (ph, tx)
print("WEBHOOK_PARSE_OK")

print("\nALL_SMOKE_TESTS_PASSED")

"""
Matches Sell.do leads (no phone) to cached Meta form leads (name+phone) by
normalized-name similarity + created-time proximity.

Sell.do name quirks handled: trailing '(#51246)' ids, doubled tokens
('paul paul', 'Sabir Sabir' — Sell.do concatenates first+last when the form
put the same string in both), punctuation, case.

A lead only auto-matches when the best candidate is unambiguous; otherwise it
stays 'pending_match' and escalates to 'unmatched' (manual queue) after 24h.
"""
import re
from datetime import timedelta
import db

TIME_WINDOW_H = 6          # Sell.do reporting lag + tz slack
MIN_SCORE = 0.5            # token-set Jaccard
MIN_GAP = 0.2              # best must beat runner-up by this (unless same phone)


def norm_tokens(name):
    if not name:
        return frozenset()
    s = re.sub(r"\(#\d+\)", " ", name.lower())
    toks = re.findall(r"[a-z0-9]+", s)
    dedup = []
    for t in toks:
        if not dedup or dedup[-1] != t:
            dedup.append(t)
    return frozenset(dedup)


def score(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    # subset bonus: 'paul' vs 'paul kumar' should still score high
    if a <= b or b <= a:
        return max(inter / len(a | b), 0.8)
    return inter / len(a | b)


def run_matching():
    pending = db.q("""SELECT id, project, name,
                             COALESCE(selldo_response_at, created_at) AS anchor
                      FROM leads
                      WHERE wa_state='pending_match' AND phone IS NULL""")
    if not pending:
        return
    for lead in pending:
        cands = db.q("""SELECT meta_lead_id, name, phone, created_time, preferred_date FROM meta_leads
                        WHERE project=%s AND phone IS NOT NULL
                          AND created_time BETWEEN %s AND %s""",
                     (lead["project"],
                      lead["anchor"] - timedelta(hours=TIME_WINDOW_H),
                      lead["anchor"] + timedelta(hours=TIME_WINDOW_H)))
        lt = norm_tokens(lead["name"])
        scored = sorted(((score(lt, norm_tokens(c["name"])), c) for c in cands),
                        key=lambda x: -x[0])
        if not scored or scored[0][0] < MIN_SCORE:
            _escalate_if_stale(lead)
            continue
        best_s, best = scored[0]
        if len(scored) > 1:
            second_s, second = scored[1]
            ambiguous = (best_s - second_s < MIN_GAP
                         and second["phone"] != best["phone"])
            if ambiguous:
                _escalate_if_stale(lead)
                continue
        db.x("""UPDATE leads SET phone=%s, meta_lead_id=%s, wa_state='queued',
                                 selected_date=COALESCE(selected_date, %s),
                                 updated_at=now() WHERE id=%s""",
             (best["phone"], best["meta_lead_id"], best.get("preferred_date"),
              lead["id"]))
        db.log_msg(lead["id"], "out", "matched", None, ok=True,
                   detail=f"score={best_s:.2f} meta={best['meta_lead_id']}")


def _escalate_if_stale(lead):
    db.x("""UPDATE leads SET wa_state='unmatched', updated_at=now()
            WHERE id=%s AND created_at < now() - interval '24 hours'""",
         (lead["id"],))

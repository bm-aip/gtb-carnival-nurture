-- Finalized against Sell.do reporting schema (clients 1436 / 1898), 2026-07-06.
-- One row per lead: latest campaign response on the target campaign.
-- Output: selldo_lead_id, meta_lead_id, name, status

SELECT DISTINCT ON (l.id)
    l.id::text          AS selldo_lead_id,
    cr.utm_lead_id      AS meta_lead_id,
    l.name              AS name,
    st.name             AS status
FROM reporting_leads l
JOIN reporting_campaign_responses cr ON cr.reporting_lead_id = l.id
JOIN reporting_campaigns c           ON c.id = cr.reporting_campaign_id
LEFT JOIN reporting_projects p       ON p.id = cr.reporting_project_id
LEFT JOIN reporting_lead_stages st   ON st.id = l.reporting_lead_stage_id
WHERE c.name = %(campaign)s
  AND (p.name = %(project)s OR cr.reporting_project_id IS NULL)
  AND cr.created_at >= '2026-06-25'
ORDER BY l.id, cr.created_at DESC

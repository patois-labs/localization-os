/**
 * Localization OS — Airtable Automation script (paste into a "Run a script" action).
 *
 * Fires when your chosen trigger runs (a button field click, or a condition like
 * "Status = Ready for translation"). It calls Localization OS, which auto-detects the
 * target-language columns for your source field (e.g. "Notes (es-MX)", "Notes
 * (de-DE)") and fills them in. The end user never sees where it goes.
 *
 * Configure these INPUT VARIABLES in the automation step (right panel):
 *   locStudioUrl   text   e.g. https://your-tunnel.trycloudflare.com   (no trailing slash needed)
 *   apiToken       text   a dedicated, customer-confined linguist account's per-user key
 *   connectionId   text   the Localization OS Airtable connection id (a number)
 *   sourceField    text   the column to translate, e.g. "Notes"
 *   recordId       text   map this to the triggering record's "Airtable record ID"
 *   tableName      text   map this to the trigger's table name (or hard-code it)
 */
// Automation inputs are visible to collaborators with automation access.
// Only share this account's authority with trusted collaborators. Legacy shared keys are refused.
const cfg = input.config();
const url = String(cfg.locStudioUrl || '').replace(/\/$/, '');

if (!url || !cfg.apiToken || !cfg.connectionId || !cfg.recordId) {
    throw new Error('Missing config: need locStudioUrl, apiToken, connectionId, recordId.');
}

const res = await fetch(`${url}/api/connectors/${cfg.connectionId}/airtable/webhook`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Loc-Token': cfg.apiToken },
    body: JSON.stringify({
        base_id: base.id,                 // current base — no need to type it anywhere
        table: cfg.tableName,
        record_ids: [cfg.recordId],
        source_field: cfg.sourceField,
        // jobs omitted on purpose → Localization OS auto-detects target-language columns
    }),
});

const body = await res.json().catch(() => ({}));
console.log(body.message || `HTTP ${res.status}`);
output.set('ok', Boolean(body.ok));
output.set('message', body.message || `HTTP ${res.status}`);

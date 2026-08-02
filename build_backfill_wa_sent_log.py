#!/usr/bin/env python3
"""One-off: backfill the Google wa_sent_log sheet into OMS Postgres via /wms/wa-log.

Why: the Postgres wa_sent_log only started collecting when PR #86 deployed
(2026-08-02 02:43 SGT-2, i.e. UTC). Repointing any marketing workflow's cooldown
read at the API before importing history would read a near-empty table, conclude
every customer is eligible, and re-send to everyone - the 2026-05-03 failure
mode with a new cause. So: import the trailing 90 days FIRST.

Architecture (one-off, manual webhook only, NO schedule):

  Webhook → Read wa_sent_log (sheet) → Filter 90d window (native, OOM-safe)
          → Build Payloads (Code) → POST /wms/wa-log (disabled on first deploy)
          → Summary (Code)

Safety:
- POST node deploys DISABLED (POST_DISABLED=True). First run is a dry run: the
  Summary node reports row count / date range / per-workflow counts and nothing
  is written. Flip POST_DISABLED=False + re-run this script to arm, then trigger
  ONCE (never poll-retrigger - see automation-safeguards).
- Rows at/after CHOKEPOINT_CUTOFF are excluded: from that moment the whatsapp
  service logs every send itself, so importing them would double-count.
- idempotency_key = backfill:<workflow>:<digits>:<sent_at> - deterministic, so
  re-running the import is a no-op (recorded=false), and rows are identifiable
  via meta.source for a rollback.
- Native Filter node, not a Code filter: a Code filter copies all input items to
  the task runner, which OOM-killed the pod at 83k rows on 2026-06-10.
"""
import json
import os
import urllib.request
import uuid

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
WA_SENT_LOG_GID = 700800
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
OMS_CRED = {"id": "4pUEOr1SF2Fu4RNl", "name": "TBP OMS WMS PAT"}

WEBHOOK_PATH = "backfill-wa-sent-log-90d"
BACKFILL_DAYS = 90
# First auto-logged row in Postgres (PR #86 chokepoint). Sheet rows at/after this
# are already captured by the service; importing them would double-count.
CHOKEPOINT_CUTOFF = "2026-08-02T02:43:13Z"
POST_DISABLED = False  # ARMED 2026-08-02 after clean dry run (431 payloads)

BUILD_JS = r"""
// Build POST /wms/wa-log payloads from sheet rows that survived the 90d filter.
const CUTOFF = Date.parse('%CUTOFF%');
const out = [];
for (const it of $input.all()) {
  const s = it.json;
  const digits = String(s.phone || '').replace(/\D/g, '');
  if (!digits || digits.length < 8) continue;
  const e164 = '+' + (digits.length === 8 ? '65' + digits : digits);
  const t = Date.parse(s.sent_at || '');
  if (isNaN(t) || t >= CUTOFF) continue;
  const workflow = String(s.workflow || 'unknown').trim() || 'unknown';
  out.push({ json: {
    phone_number: e164,
    workflow: workflow,
    template: s.template ? String(s.template) : null,
    ref_id: s.order_id ? String(s.order_id) : null,
    idempotency_key: `backfill:${workflow}:${digits}:${s.sent_at}`,
    sent_at: new Date(t).toISOString(),
    meta: { source: 'sheet_backfill_2026_08_02', notes: s.notes ? String(s.notes) : undefined },
  }});
}
return out;
""".replace("%CUTOFF%", CHOKEPOINT_CUTOFF)

SUMMARY_JS = r"""
// Works for both runs: dry (payload items pass through the disabled POST node)
// and live (items are {recorded, idempotency_key} responses).
const items = $input.all().map(i => i.json);
const stats = { total: items.length, inserted: 0, duplicates: 0, dry_payloads: 0,
                by_workflow: {}, min_sent_at: null, max_sent_at: null };
for (const j of items) {
  if (typeof j.recorded === 'boolean') { j.recorded ? stats.inserted++ : stats.duplicates++; }
  else if (j.idempotency_key) {
    stats.dry_payloads++;
    const w = j.workflow || 'unknown';
    stats.by_workflow[w] = (stats.by_workflow[w] || 0) + 1;
    if (!stats.min_sent_at || j.sent_at < stats.min_sent_at) stats.min_sent_at = j.sent_at;
    if (!stats.max_sent_at || j.sent_at > stats.max_sent_at) stats.max_sent_at = j.sent_at;
  }
}
return [{ json: stats }];
"""


def node(name, type_, position, parameters, **extra):
    return {"parameters": parameters, "id": str(uuid.uuid4()), "name": name,
            "type": type_, "typeVersion": extra.pop("typeVersion", 2),
            "position": position, **extra}


nodes = [
    node("Manual Trigger", "n8n-nodes-base.webhook", [0, 0],
         {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}},
         typeVersion=2, webhookId=str(uuid.uuid4())),
    node("Read wa_sent_log Sheet", "n8n-nodes-base.googleSheets", [220, 0],
         {"documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                         "cachedResultName": "Bon Pet — Customer Orders DB"},
          "sheetName": {"__rl": True, "value": WA_SENT_LOG_GID, "mode": "list",
                        "cachedResultName": "wa_sent_log"},
          "options": {}},
         typeVersion=4.5, credentials={"googleSheetsOAuth2Api": GS_CRED},
         executeOnce=True, alwaysOutputData=True),
    node("Filter 90d Window", "n8n-nodes-base.filter", [440, 0],
         {"conditions": {
             "options": {"caseSensitive": True, "leftValue": "",
                         "typeValidation": "loose", "version": 2},
             "conditions": [{
                 "id": str(uuid.uuid4()),
                 "leftValue": "={{ Date.parse($json.sent_at || '') > (Date.now() - "
                              + str(BACKFILL_DAYS) + " * 24 * 60 * 60 * 1000) }}",
                 "rightValue": "",
                 "operator": {"type": "boolean", "operation": "true", "singleValue": True},
             }],
             "combinator": "and"},
          "options": {}},
         alwaysOutputData=True),
    node("Build Payloads", "n8n-nodes-base.code", [660, 0], {"jsCode": BUILD_JS}),
    node("POST /wms/wa-log", "n8n-nodes-base.httpRequest", [880, 0],
         {"method": "POST", "url": "https://api.thebonpet.com/wms/wa-log",
          "authentication": "genericCredentialType",
          "genericAuthType": "httpHeaderAuth",
          "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify($json) }}",
          "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 100}}}},
         typeVersion=4.2, credentials={"httpHeaderAuth": OMS_CRED},
         disabled=POST_DISABLED, onError="continueRegularOutput"),
    node("Summary", "n8n-nodes-base.code", [1100, 0], {"jsCode": SUMMARY_JS}),
]

connections = {
    "Manual Trigger": {"main": [[{"node": "Read wa_sent_log Sheet", "type": "main", "index": 0}]]},
    "Read wa_sent_log Sheet": {"main": [[{"node": "Filter 90d Window", "type": "main", "index": 0}]]},
    "Filter 90d Window": {"main": [[{"node": "Build Payloads", "type": "main", "index": 0}]]},
    "Build Payloads": {"main": [[{"node": "POST /wms/wa-log", "type": "main", "index": 0}]]},
    "POST /wms/wa-log": {"main": [[{"node": "Summary", "type": "main", "index": 0}]]},
}

workflow = {
    "name": "TEMP Backfill wa_sent_log → Postgres (one-off)",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore",
                 "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"},
}


def req(method, path, body=None):
    r = urllib.request.Request(API + path,
                               data=json.dumps(body).encode() if body else None,
                               method=method)
    r.add_header("X-N8N-API-KEY", KEY)
    r.add_header("User-Agent", "Mozilla/5.0")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    import sys
    existing_id = sys.argv[1] if len(sys.argv) > 1 else None
    if existing_id:
        wf = req("PUT", f"/workflows/{existing_id}", workflow)
        print(f"updated workflow {wf['id']}")
    else:
        wf = req("POST", "/workflows", workflow)
        print(f"created workflow {wf['id']}")
    req("POST", f"/workflows/{wf['id']}/activate")
    print(f"activated. POST node disabled={POST_DISABLED}")
    print(f"trigger ONCE: curl -X POST https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

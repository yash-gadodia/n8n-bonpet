#!/usr/bin/env python3
"""One-off #2: backfill the per-workflow sent tabs into OMS Postgres.

The wa_sent_log tab import (build_backfill_wa_sent_log.py) covered the GLOBAL
log, which the monthly pruner trims to ~60d. The per-workflow tabs
(reorder_reminder_sent, post_trial_sent, sub_reactivation_sent) are NOT pruned -
they are each workflow's PERMANENT "never message twice" exclusion list, with
history back to April. The parity check found 69 phones excluded by the sheet
but not by the API purely because of this gap.

Key format matches the first importer (backfill:<workflow>:<digits>:<sent_at>),
so rows dual-logged in both a per-workflow tab and the global tab collapse onto
the row already imported - no double counting.

Tabs are addressed by NAME, not GID (two build scripts claim GID 900900 for
different tabs, so one is stale; names are unambiguous).
"""
import json
import os
import urllib.request
import uuid

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
OMS_CRED = {"id": "4pUEOr1SF2Fu4RNl", "name": "TBP OMS WMS PAT"}
WEBHOOK_PATH = "backfill-workflow-sent-tabs"
CHOKEPOINT_CUTOFF = "2026-08-02T02:43:13Z"
POST_DISABLED = False  # ARMED after clean dry run (440 payloads)

TABS = [
    ("reorder_reminder_sent", "reorder_reminder"),
    ("post_trial_sent", "post_trial_nurture"),
    ("sub_reactivation_sent", "sub_reactivation"),
]

BUILD_JS = r"""
const CUTOFF = Date.parse('%CUTOFF%');
const SOURCES = %SOURCES%;   // [node name, workflow label]
const out = [];
const seen = new Set();
for (const [nodeName, workflow] of SOURCES) {
  let rows = [];
  try { rows = $(nodeName).all(); } catch (e) { continue; }
  for (const it of rows) {
    const s = it.json;
    const digits = String(s.phone || '').replace(/\D/g, '');
    if (!digits || digits.length < 8) continue;
    const t = Date.parse(s.sent_at || '');
    if (isNaN(t) || t >= CUTOFF) continue;
    const key = `backfill:${workflow}:${digits}:${s.sent_at}`;
    if (seen.has(key)) continue;   // in-run dedupe; cross-run handled by the DB
    seen.add(key);
    out.push({ json: {
      phone_number: '+' + (digits.length === 8 ? '65' + digits : digits),
      workflow: workflow,
      template: s.template ? String(s.template) : null,
      ref_id: s.last_order_id ? String(s.last_order_id) : (s.order_id ? String(s.order_id) : null),
      idempotency_key: key,
      sent_at: new Date(t).toISOString(),
      meta: { source: 'tab_backfill_2026_08_02', tab: nodeName },
    }});
  }
}
return out;
""".replace("%CUTOFF%", CHOKEPOINT_CUTOFF).replace(
    "%SOURCES%", json.dumps([[f"Read {tab}", wf] for tab, wf in TABS]))

SUMMARY_JS = r"""
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
]
prev = "Manual Trigger"
connections = {}
x = 220
for tab, _wf in TABS:
    name = f"Read {tab}"
    nodes.append(node(name, "n8n-nodes-base.googleSheets", [x, 0],
                      {"documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                                      "cachedResultName": "Bon Pet — Customer Orders DB"},
                       "sheetName": {"__rl": True, "value": tab, "mode": "name"},
                       "options": {}},
                      typeVersion=4.5, credentials={"googleSheetsOAuth2Api": GS_CRED},
                      executeOnce=True, alwaysOutputData=True,
                      onError="continueRegularOutput"))
    connections[prev] = {"main": [[{"node": name, "type": "main", "index": 0}]]}
    prev = name
    x += 220

nodes += [
    node("Build Payloads", "n8n-nodes-base.code", [x, 0], {"jsCode": BUILD_JS}),
    node("POST /wms/wa-log", "n8n-nodes-base.httpRequest", [x + 220, 0],
         {"method": "POST", "url": "https://api.thebonpet.com/wms/wa-log",
          "authentication": "genericCredentialType",
          "genericAuthType": "httpHeaderAuth",
          "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify($json) }}",
          "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 100}}}},
         typeVersion=4.2, credentials={"httpHeaderAuth": OMS_CRED},
         disabled=POST_DISABLED, onError="continueRegularOutput"),
    node("Summary", "n8n-nodes-base.code", [x + 440, 0], {"jsCode": SUMMARY_JS}),
]
connections[prev] = {"main": [[{"node": "Build Payloads", "type": "main", "index": 0}]]}
connections["Build Payloads"] = {"main": [[{"node": "POST /wms/wa-log", "type": "main", "index": 0}]]}
connections["POST /wms/wa-log"] = {"main": [[{"node": "Summary", "type": "main", "index": 0}]]}

workflow = {
    "name": "TEMP Backfill per-workflow sent tabs → Postgres (one-off)",
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
    print(f"POST node disabled={POST_DISABLED}")
    print(f"trigger ONCE: curl -X POST https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

#!/usr/bin/env python3
"""Sent Log Pruner — monthly cleanup of the global wa_sent_log tab.

Why: the log's only cross-workflow job is the 7-day cooldown (every sender
keeps its own dedup tab), but it grows forever and every sender reads the
whole tab per run. At 83k rows it OOM-killed the pod (2026-06-10 incident;
bulk was a review_watcher dump from Apr 22). One-time cleanup brought it
83,739 -> 703 rows; this workflow keeps it that way.

Monthly (1st, 05:49 SGT — clear of the :07/:13/:17/:37/:43 cron slots and
hours away from any sender) it deletes rows older than RETENTION_DAYS plus
rows with blank/unparseable sent_at, then reports the counts to the weslee
thread. 60-day retention = 8x margin over the 7-day cooldown.

Architecture:
  Monthly 05:49 SGT ┐
                    ├→ Read sent_at Column → Compute Delete Spans
  Manual Webhook    ┘    → Anything to Delete? ─true→ BatchUpdate Delete → Send Telegram Weslee
                                               └false→ Send Telegram Weslee
"""
import json
import os
import uuid
import urllib.request
import urllib.error

from _notify import telegram_send_node
from _sent_log import SHEET_ID, WA_SENT_LOG_GID, GS_CRED

API = "https://n8n.thebonpet.com/api/v1"
WF_ID = "vivE908xnTgEDg8h"
WF_NAME = "Sent Log Pruner (monthly)"
WEBHOOK_PATH = "sent-log-pruner-manual-7d2e4a"
RETENTION_DAYS = 60

N8N_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()


def uid():
    return str(uuid.uuid4())


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


COMPUTE_JS = r"""
const RETENTION_DAYS = __RETENTION_DAYS__;
const SHEET_GID = __SHEET_GID__;
const col = ($input.first().json.values || [[]])[0] || [];
const cutoff = Date.now() - RETENTION_DAYS * 24 * 60 * 60 * 1000;
// row 0 is the header; keep it and anything within retention
const dele = [];
for (let i = 1; i < col.length; i++) {
  const t = Date.parse((col[i] || '').trim());
  if (isNaN(t) || t < cutoff) dele.push(i);
}
const total = col.length;
// contiguous 0-based spans, descending so indices stay valid as rows shift up
const spans = [];
let start = null, prev = null;
for (const i of dele) {
  if (start === null) { start = prev = i; continue; }
  if (i === prev + 1) { prev = i; continue; }
  spans.push([start, prev + 1]); start = prev = i;
}
if (start !== null) spans.push([start, prev + 1]);
spans.sort((a, b) => b[0] - a[0]);
const requests = spans.map(([s, e]) => ({
  deleteDimension: { range: { sheetId: SHEET_GID, dimension: 'ROWS', startIndex: s, endIndex: e } }
}));
const message = dele.length
  ? `🧹 wa_sent_log pruned: deleting ${dele.length} rows older than ${RETENTION_DAYS}d (${spans.length} spans). ${total - dele.length} rows remain.`
  : `🧹 wa_sent_log prune: nothing older than ${RETENTION_DAYS}d. ${total} rows, all healthy.`;
return [{ json: { has_deletions: dele.length > 0, batch: { requests }, message } }];
"""


def build():
    schedule = {
        # 1st of month 05:49 SGT — clear of :07/:13/:17/:37/:43 slots and all sender hours.
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "49 5 1 * *"}]}},
        "id": uid(), "name": "Monthly 05:49 SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 200],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 400], "webhookId": uid(),
    }
    read_col = {
        "parameters": {
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/wa_sent_log!D1:D200000?majorDimension=COLUMNS",
            "authentication": "predefinedCredentialType", "nodeCredentialType": "googleSheetsOAuth2Api",
            "options": {},
        },
        "id": uid(), "name": "Read sent_at Column",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [240, 300],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    compute = {
        "parameters": {"jsCode": COMPUTE_JS
                       .replace("__RETENTION_DAYS__", str(RETENTION_DAYS))
                       .replace("__SHEET_GID__", str(WA_SENT_LOG_GID))},
        "id": uid(), "name": "Compute Delete Spans",
        "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [480, 300],
    }
    gate = {
        "parameters": {"conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": [{"id": uid(), "leftValue": "={{ $json.has_deletions }}", "rightValue": "",
                            "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
            "combinator": "and"}, "options": {}},
        "id": uid(), "name": "Anything to Delete?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [720, 300],
    }
    batch = {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType", "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.batch) }}",
            "options": {},
        },
        "id": uid(), "name": "BatchUpdate Delete",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [960, 200],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    tg = telegram_send_node("Send Telegram Weslee", [1200, 300],
                            message_expr="={{ $('Compute Delete Spans').first().json.message }}",
                            parse_mode=None)
    return {
        "name": WF_NAME,
        "nodes": [schedule, manual, read_col, compute, gate, batch, tg],
        "connections": {
            "Monthly 05:49 SGT": {"main": [[{"node": "Read sent_at Column", "type": "main", "index": 0}]]},
            "Manual Webhook": {"main": [[{"node": "Read sent_at Column", "type": "main", "index": 0}]]},
            "Read sent_at Column": {"main": [[{"node": "Compute Delete Spans", "type": "main", "index": 0}]]},
            "Compute Delete Spans": {"main": [[{"node": "Anything to Delete?", "type": "main", "index": 0}]]},
            "Anything to Delete?": {"main": [
                [{"node": "BatchUpdate Delete", "type": "main", "index": 0}],
                [{"node": "Send Telegram Weslee", "type": "main", "index": 0}],
            ]},
            "BatchUpdate Delete": {"main": [[{"node": "Send Telegram Weslee", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"},
    }


if __name__ == "__main__":
    payload = build()
    with open("/Users/yash/n8n-bonpet/sent_log_pruner_payload.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes")
    if WF_ID:
        status, _ = api(f"/workflows/{WF_ID}", "PUT", payload)
        print(f"PUT HTTP {status}")
        wid = WF_ID
    else:
        status, body = api("/workflows", "POST", payload)
        wid = body.get("id") if isinstance(body, dict) else None
        print(f"POST HTTP {status} id={wid}")
    if wid:
        s, _ = api(f"/workflows/{wid}/activate", "POST")
        print(f"Activate HTTP {s}")
        print(f"Manual fire: curl -X POST https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

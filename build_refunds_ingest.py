#!/usr/bin/env python3
"""Build Refunds Ingest pipeline:
  1. Add 'refunds' tab to the Customer Orders DB workbook
  2. Write 11 column headers
  3. Create/update 'Refunds Ingest' workflow (webhook → parse → append to refunds tab)
Webhook URL for Shopify config: https://n8n.thebonpet.com/webhook/shopify-refunds-ingest
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REFUNDS_TAB = "refunds"
REFUNDS_SHEET_ID = 200200
WEBHOOK_PATH = "shopify-refunds-ingest"

HEADERS = [
    "received_at", "refund_id", "order_id", "refund_date", "refund_amount",
    "currency", "note", "line_items_json", "user_id", "processed_at", "raw_body_json",
]


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ----- Step 1+2: add 'refunds' tab + write headers via batchUpdate -----
def setup_refunds_tab():
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": REFUNDS_SHEET_ID, "title": REFUNDS_TAB}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in HEADERS]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": REFUNDS_SHEET_ID, "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-add-refunds-tab", "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger", "type": "n8n-nodes-base.webhook",
         "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body),
            "options": {},
         }, "id": str(uuid.uuid4()), "name": "Add Tab + Headers",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED}, "continueOnFail": True},
    ]
    conn = {"Trigger": {"main": [[{"node": "Add Tab + Headers", "type": "main", "index": 0}]]}}
    s, b = http("POST", "/workflows", {"name": "TEMP Add Refunds Tab", "nodes": nodes,
                                       "connections": conn, "settings": {"executionOrder": "v1"}})
    wf = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://n8n.thebonpet.com/webhook/tmp-add-refunds-tab",
            data=b'{}', method="POST", headers={"Content-Type": "application/json"}), timeout=30)
        print("  Tab setup fired.")
    except urllib.error.HTTPError as e:
        out = e.read().decode()[:300]
        print(f"  Tab setup: {'already exists ✓' if 'already exists' in out else f'HTTP {e.code}: {out}'}")
    time.sleep(2)
    http("DELETE", f"/workflows/{wf}")


# ----- Step 3: Refunds Ingest workflow -----
PARSE_JS = r"""// Parse Shopify refund(s) payload → row for refunds tab.
// Native webhook sends a single Refund object.
function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
const raw = $input.first().json;
const body = raw.body || raw;
const refunds = body.refunds ? body.refunds : [body];
return refunds.map(r => {
  let total = 0;
  const lines = [];
  for (const rli of (r.refund_line_items || [])) {
    total += Number(rli.subtotal || 0);
    lines.push({
      line_item_id: rli.line_item_id,
      quantity: rli.quantity,
      subtotal: rli.subtotal,
      title: rli.line_item && rli.line_item.title,
      variant_id: rli.line_item && rli.line_item.variant_id,
    });
  }
  // Also include order adjustments (shipping refunds, etc.)
  for (const adj of (r.order_adjustments || [])) {
    total += Number(adj.amount || 0);
  }
  // Fallback: use r.transactions[].amount sum if refund_line_items empty
  if (total === 0) {
    for (const tx of (r.transactions || [])) {
      if (tx.kind === 'refund') total += Number(tx.amount || 0);
    }
  }
  return { json: {
    received_at: new Date().toISOString(),
    refund_id: extractId(r.id),
    order_id: extractId(r.order_id),
    refund_date: r.created_at || new Date().toISOString(),
    refund_amount: total.toFixed(2),
    currency: (r.transactions && r.transactions[0] && r.transactions[0].currency) || 'SGD',
    note: r.note || '',
    line_items_json: JSON.stringify(lines),
    user_id: extractId(r.user_id || (r.refund_shipping_lines && r.refund_shipping_lines[0] && r.refund_shipping_lines[0].handle) || ''),
    processed_at: r.processed_at || r.created_at || '',
    raw_body_json: JSON.stringify(r).slice(0, 5000),  // truncated audit trail
  }};
});
"""


def build_refunds_ingest():
    webhook = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH,
                       "responseMode": "onReceived", "responseData": "noData", "options": {}},
        "id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }
    parse = {
        "parameters": {"jsCode": PARSE_JS},
        "id": str(uuid.uuid4()), "name": "Parse Refund", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [240, 0],
    }
    append = {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": REFUNDS_SHEET_ID, "mode": "list",
                          "cachedResultName": REFUNDS_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REFUNDS_SHEET_ID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "matchingColumns": ["refund_id"],
                "schema": [{"id": h, "displayName": h, "required": False,
                            "defaultMatch": h == "refund_id", "display": True,
                            "type": "string", "canBeUsedToMatch": True} for h in HEADERS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Append to Refunds Tab",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5, "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    nodes = [webhook, parse, append]
    conn = {
        "Webhook": {"main": [[{"node": "Parse Refund", "type": "main", "index": 0}]]},
        "Parse Refund": {"main": [[{"node": "Append to Refunds Tab", "type": "main", "index": 0}]]},
    }
    # Upsert workflow by name
    existing = json.loads(http("GET", "/workflows")[1]).get("data", [])
    wf_id = next((w["id"] for w in existing if w["name"].startswith("Refunds Ingest")), None)
    payload = {"name": "Refunds Ingest (Shopify → DB)", "nodes": nodes,
               "connections": conn, "settings": {"executionOrder": "v1"}}
    if wf_id:
        s, b = http("PUT", f"/workflows/{wf_id}", payload)
        print(f"  Refunds Ingest WF updated: {wf_id} (HTTP {s})")
    else:
        s, b = http("POST", "/workflows", payload)
        wf_id = json.loads(b)["id"]
        print(f"  Refunds Ingest WF created: {wf_id}")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    return wf_id


if __name__ == "__main__":
    print("Step 1: setup refunds tab")
    setup_refunds_tab()
    print("\nStep 2: build Refunds Ingest workflow")
    wf = build_refunds_ingest()
    print(f"\n✅ Refunds pipeline live.")
    print(f"   Workflow URL: https://n8n.thebonpet.com/workflow/{wf}")
    print(f"   Webhook URL:  https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print(f"   Sheet tab:    https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REFUNDS_SHEET_ID}")

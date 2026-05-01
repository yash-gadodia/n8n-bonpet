#!/usr/bin/env python3
"""Subscribers pipeline — makes the Reorder Reminder fully autopilot.

  1. Adds 'subscribers' tab to the workbook (gid=700700)
  2. Builds Subscribers Ingest workflow (webhook → parse → upsert)
  3. Seeds the tab from the local CSV export (all 303 rows)
  4. Provides webhook URL for user to configure in Shopify for
     subscription_contracts/create and subscription_contracts/update events
"""
import csv, json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
SUB_SHEET_ID = 700700
SUB_TAB = "subscribers"
WEBHOOK_PATH = "shopify-subscriptions-ingest"
CSV_PATH = "/Users/yash/Documents/TheBonPet/subsribers_export.csv"

HEADERS = [
    "received_at", "contract_id", "customer_id", "email", "status",
    "upcoming_billing_date", "cadence_interval", "cadence_interval_count",
    "currency_code", "line_variant_id", "line_quantity", "line_selling_plan_name",
    "last_synced_at", "raw_body_json",
]


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()


# ---- Step 1: setup tab + headers ----
def setup_tab():
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": SUB_SHEET_ID, "title": SUB_TAB}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in HEADERS]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": SUB_SHEET_ID, "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-add-sub-tab", "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger", "type": "n8n-nodes-base.webhook",
         "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body), "options": {},
         }, "id": str(uuid.uuid4()), "name": "Add Tab",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED}, "continueOnFail": True},
    ]
    conn = {"Trigger": {"main": [[{"node": "Add Tab", "type": "main", "index": 0}]]}}
    s, b = http("POST", "/workflows", {"name": "TEMP Subscribers Tab", "nodes": nodes,
                                       "connections": conn, "settings": {"executionOrder": "v1"}})
    wf = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://n8n.thebonpet.com/webhook/tmp-add-sub-tab",
            data=b'{}', method="POST", headers={"Content-Type": "application/json"}), timeout=30)
        print("  Setup fired")
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:200]
        print("  " + ("Already exists ✓" if "already exists" in msg else f"HTTP {e.code}"))
    time.sleep(1)
    http("DELETE", f"/workflows/{wf}")


# ---- Step 2: ingest workflow ----
PARSE_JS = r"""// Parse Shopify subscription_contract payload OR bulk seed payload
function extractId(v) { if (!v) return ''; const s = String(v).replace(/^'/, ''); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
const raw = $input.first().json;
const body = raw.body || raw;
const contracts = body.subscribers || body.subscription_contracts || (Array.isArray(body) ? body : [body]);
const now = new Date().toISOString();
return contracts.map(c => {
  // Extract first line item if present
  const firstLine = c.line_items && c.line_items.edges && c.line_items.edges[0] && c.line_items.edges[0].node || c.lines && c.lines[0] || {};
  const customer = c.customer || {};
  return { json: {
    received_at: now,
    contract_id: extractId(c.id || c.contract_id || c.handle),
    customer_id: extractId(c.customer_id || customer.id),
    email: (c.email || customer.email || '').toLowerCase().trim(),
    status: (c.status || '').toUpperCase(),
    upcoming_billing_date: c.upcoming_billing_date || c.next_billing_date || '',
    cadence_interval: c.cadence_interval || (c.billing_policy && c.billing_policy.interval) || '',
    cadence_interval_count: c.cadence_interval_count || (c.billing_policy && c.billing_policy.interval_count) || '',
    currency_code: c.currency_code || c.currency || '',
    line_variant_id: extractId(c.line_variant_id || firstLine.variant_id || firstLine.variantId),
    line_quantity: c.line_quantity || firstLine.quantity || '',
    line_selling_plan_name: c.line_selling_plan_name || (firstLine.selling_plan && firstLine.selling_plan.name) || '',
    last_synced_at: now,
    raw_body_json: JSON.stringify(c).slice(0, 3000),
  }};
});
"""


def build_ingest():
    webhook = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH,
                       "responseMode": "onReceived", "responseData": "noData", "options": {}},
        "id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }
    parse = {
        "parameters": {"jsCode": PARSE_JS},
        "id": str(uuid.uuid4()), "name": "Parse Subscription", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [240, 0],
    }
    append = {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": SUB_SHEET_ID, "mode": "list",
                          "cachedResultName": SUB_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={SUB_SHEET_ID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "matchingColumns": ["contract_id"],
                "schema": [{"id": h, "displayName": h, "required": False,
                            "defaultMatch": h == "contract_id", "display": True,
                            "type": "string", "canBeUsedToMatch": True} for h in HEADERS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Upsert Subscription",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7, "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    nodes = [webhook, parse, append]
    conn = {
        "Webhook": {"main": [[{"node": "Parse Subscription", "type": "main", "index": 0}]]},
        "Parse Subscription": {"main": [[{"node": "Upsert Subscription", "type": "main", "index": 0}]]},
    }
    payload = {"name": "Subscribers Ingest (Shopify → DB)", "nodes": nodes, "connections": conn,
               "settings": {"executionOrder": "v1"}}
    existing = json.loads(http("GET", "/workflows?limit=100")[1]).get("data", [])
    wf_id = next((w["id"] for w in existing if w["name"] == "Subscribers Ingest (Shopify → DB)"), None)
    if wf_id:
        s, _ = http("PUT", f"/workflows/{wf_id}", payload)
        print(f"  WF updated: {wf_id} (HTTP {s})")
    else:
        s, b = http("POST", "/workflows", payload)
        wf_id = json.loads(b)["id"]
        print(f"  WF created: {wf_id}")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    return wf_id


# ---- Step 3: seed from CSV ----
def seed_from_csv():
    with open(CSV_PATH) as f:
        rows = list(csv.DictReader(f))

    # Collapse to one row per contract_id (CSV has one row per line item)
    by_contract = {}
    for r in rows:
        cid = (r.get("handle") or r.get("contract_id") or "").lstrip("'").strip()
        if not cid: continue
        # Extract numeric from gid://shopify/SubscriptionContract/123
        m = cid.rsplit("/", 1)
        cid = m[-1]
        if cid not in by_contract:
            by_contract[cid] = r

    # Enrich with email from customers CSV
    cust_csv = "/Users/yash/n8n-bonpet/exports/customers_2026-04-19/customers_export.csv"
    cid_to_email = {}
    with open(cust_csv) as f:
        for row in csv.DictReader(f):
            c = (row.get("Customer ID") or "").strip()
            if c: cid_to_email[c] = (row.get("Email") or "").strip().lower()

    payload_rows = []
    for cid, r in by_contract.items():
        customer_id = (r.get("customer_id") or "").lstrip("'").strip()
        email = cid_to_email.get(customer_id, "")
        payload_rows.append({
            "contract_id": cid,
            "customer_id": customer_id,
            "email": email,
            "status": (r.get("status") or "").upper(),
            "upcoming_billing_date": (r.get("upcoming_billing_date") or "").lstrip("'"),
            "cadence_interval": r.get("cadence_interval") or "",
            "cadence_interval_count": r.get("cadence_interval_count") or "",
            "currency_code": r.get("currency_code") or "",
            "line_variant_id": (r.get("line_variant_id") or "").lstrip("'"),
            "line_quantity": r.get("line_quantity") or "",
            "line_selling_plan_name": r.get("line_selling_plan_name") or "",
        })

    print(f"  Seeding {len(payload_rows)} unique contracts from CSV")
    BATCH = 25
    webhook_url = f"https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}"
    sent = 0
    for i in range(0, len(payload_rows), BATCH):
        batch = payload_rows[i:i+BATCH]
        try:
            req = urllib.request.Request(webhook_url, data=json.dumps({"subscribers": batch}).encode(),
                                          method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 200: sent += len(batch)
        except Exception as e:
            print(f"    batch {i//BATCH + 1} failed: {e}")
        time.sleep(0.4)
    print(f"  Seeded {sent}/{len(payload_rows)}")


if __name__ == "__main__":
    print("Step 1: setup subscribers tab")
    setup_tab()
    print("\nStep 2: build Subscribers Ingest workflow")
    wf = build_ingest()
    print(f"\nStep 3: seed {CSV_PATH}")
    time.sleep(2)
    seed_from_csv()
    print(f"\n✅ Done.")
    print(f"   Webhook URL: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print(f"   Sheet tab:   https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={SUB_SHEET_ID}")

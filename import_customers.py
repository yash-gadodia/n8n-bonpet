#!/usr/bin/env python3
"""
End-to-end customers import:
  1. Add 'customers' tab to the existing Orders DB workbook
  2. Write 21 column headers in that tab via direct updateCells (no n8n GS node bug)
  3. Build a temp 'Customer Bulk Ingest' workflow (webhook → splitOut → appendOrUpdate)
  4. POST all 1402 customers from the local CSV
  5. Leave the ingest workflow active for future re-imports & Shopify Flow updates

The same workflow can later be used by Shopify Flow customer/create events to keep this DB live.
"""
import json, uuid, os, csv, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
NEW_TAB_NAME = "customers"
NEW_TAB_SHEETID = 100100  # arbitrary stable ID
WEBHOOK_PATH = "customers-ingest"

CSV_PATH = "/Users/yash/n8n-bonpet/exports/customers_2026-04-19/customers_export.csv"

# CSV header → snake_case sheet header
COL_MAP = [
    ("Customer ID", "customer_id"),
    ("First Name", "first_name"),
    ("Last Name", "last_name"),
    ("Email", "email"),
    ("Accepts Email Marketing", "accepts_email_marketing"),
    ("Default Address Company", "default_address_company"),
    ("Default Address Address1", "default_address_address1"),
    ("Default Address Address2", "default_address_address2"),
    ("Default Address City", "default_address_city"),
    ("Default Address Province Code", "default_address_province_code"),
    ("Default Address Country Code", "default_address_country_code"),
    ("Default Address Zip", "default_address_zip"),
    ("Default Address Phone", "default_address_phone"),
    ("Phone", "phone"),
    ("Accepts SMS Marketing", "accepts_sms_marketing"),
    ("Total Spent", "total_spent"),
    ("Total Orders", "total_orders"),
    ("Note", "note"),
    ("Tax Exempt", "tax_exempt"),
    ("Tags", "tags"),
]
HEADERS = [snake for _, snake in COL_MAP] + ["last_synced_at"]


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---------- Step 1+2: Add customers tab + write headers via batchUpdate ----------
def setup_customers_tab():
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": NEW_TAB_SHEETID, "title": NEW_TAB_NAME}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in HEADERS]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": NEW_TAB_SHEETID, "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    # build & fire a temp workflow
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-add-customers-tab", "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger", "type": "n8n-nodes-base.webhook",
         "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body),
            "options": {},
         },
         "id": str(uuid.uuid4()), "name": "Add Tab + Headers", "type": "n8n-nodes-base.httpRequest",
         "typeVersion": 4.2, "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED},
         "continueOnFail": True},
    ]
    conn = {"Trigger": {"main": [[{"node": "Add Tab + Headers", "type": "main", "index": 0}]]}}
    s, b = http("POST", "/workflows", {"name": "TEMP Add Customers Tab", "nodes": nodes,
                                       "connections": conn, "settings": {"executionOrder": "v1"}})
    wf = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf}/activate")
    time.sleep(1)
    try:
        with urllib.request.urlopen(urllib.request.Request(
            "https://thebonpet.app.n8n.cloud/webhook/tmp-add-customers-tab",
            data=b'{}', method="POST", headers={"Content-Type": "application/json"}), timeout=30) as r:
            print(f"  Setup tab → HTTP {r.status}: {r.read().decode()[:200]}")
    except urllib.error.HTTPError as e:
        out = e.read().decode()[:300]
        if "already exists" in out:
            print("  Setup tab → already exists (idempotent ✓)")
        else:
            print(f"  Setup tab → HTTP {e.code}: {out}")
    time.sleep(2)
    http("DELETE", f"/workflows/{wf}")
    print("  TEMP setup workflow deleted.")


# ---------- Step 3: Build the persistent Customer Ingest workflow ----------
def build_customer_ingest():
    """Webhook → SplitOut → appendOrUpdate. Accepts {"customers": [...]} payloads.
       Reusable for both bulk import + Shopify Flow customer/create events."""
    webhook = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH,
                       "responseMode": "onReceived", "responseData": "noData", "options": {}},
        "id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }
    enrich = {
        "parameters": {"jsCode":
            "// Normalize: handle bulk CSV import {customers:[...]}, single row, OR Shopify native\n"
            "// customer webhook payload. For webhook updates (where Shopify strips PII like first_name\n"
            "// per Basic plan PCD), we SKIP empty fields so they don't overwrite existing good data.\n"
            "function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\\d+)$/); return m ? m[1] : s; }\n"
            "function addIfPresent(out, key, val) { if (val !== null && val !== undefined && val !== '') out[key] = val; }\n"
            "const raw = $input.first().json;\n"
            "const body = raw.body || raw;\n"
            "let incoming = body.customers || (Array.isArray(body) ? body : [body]);\n"
            "const now = new Date().toISOString();\n"
            "return incoming.map(c => {\n"
            "  // If already normalized (from CSV import), pass through verbatim.\n"
            "  if (c.customer_id && c.first_name !== undefined) {\n"
            "    return { json: { ...c, last_synced_at: now } };\n"
            "  }\n"
            "  // Map from Shopify REST Customer shape; only include non-empty fields\n"
            "  // so partial webhook payloads don't clobber CSV-seeded data.\n"
            "  const addr = (c.addresses && c.addresses[0]) || c.default_address || {};\n"
            "  const email_state = c.email_marketing_consent && c.email_marketing_consent.state;\n"
            "  const sms_state = c.sms_marketing_consent && c.sms_marketing_consent.state;\n"
            "  const out = { customer_id: extractId(c.id || c.customer_id), last_synced_at: now };\n"
            "  addIfPresent(out, 'first_name', c.first_name);\n"
            "  addIfPresent(out, 'last_name', c.last_name);\n"
            "  addIfPresent(out, 'email', c.email);\n"
            "  addIfPresent(out, 'phone', c.phone || addr.phone);\n"
            "  if (email_state) out.accepts_email_marketing = email_state === 'subscribed' ? 'yes' : 'no';\n"
            "  if (sms_state)   out.accepts_sms_marketing   = sms_state   === 'subscribed' ? 'yes' : 'no';\n"
            "  addIfPresent(out, 'default_address_company', addr.company);\n"
            "  addIfPresent(out, 'default_address_address1', addr.address1);\n"
            "  addIfPresent(out, 'default_address_address2', addr.address2);\n"
            "  addIfPresent(out, 'default_address_city', addr.city);\n"
            "  addIfPresent(out, 'default_address_province_code', addr.province_code);\n"
            "  addIfPresent(out, 'default_address_country_code', addr.country_code);\n"
            "  addIfPresent(out, 'default_address_zip', addr.zip);\n"
            "  addIfPresent(out, 'default_address_phone', addr.phone);\n"
            "  addIfPresent(out, 'total_spent', c.total_spent);\n"
            "  addIfPresent(out, 'total_orders', c.orders_count !== undefined ? c.orders_count : c.total_orders);\n"
            "  addIfPresent(out, 'note', c.note);\n"
            "  if (c.tax_exempt !== undefined) out.tax_exempt = c.tax_exempt ? 'yes' : 'no';\n"
            "  const tags = Array.isArray(c.tags) ? c.tags.join(',') : c.tags;\n"
            "  addIfPresent(out, 'tags', tags);\n"
            "  return { json: out };\n"
            "});\n"
        },
        "id": str(uuid.uuid4()), "name": "Normalize + Enrich", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [240, 0],
    }
    upsert = {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": NEW_TAB_SHEETID, "mode": "list",
                          "cachedResultName": NEW_TAB_NAME,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={NEW_TAB_SHEETID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "matchingColumns": ["customer_id"],
                "schema": [{"id": h, "displayName": h, "required": False, "defaultMatch": h == "customer_id",
                            "display": True, "type": "string", "canBeUsedToMatch": True} for h in HEADERS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Upsert Customer", "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.7, "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    nodes = [webhook, enrich, upsert]
    conn = {
        "Webhook": {"main": [[{"node": "Normalize + Enrich", "type": "main", "index": 0}]]},
        "Normalize + Enrich": {"main": [[{"node": "Upsert Customer", "type": "main", "index": 0}]]},
    }
    # Re-use existing workflow if present (so we don't create duplicates)
    existing = json.loads(http("GET", "/workflows")[1]).get("data", [])
    wf = None
    for w in existing:
        if w["name"].startswith("Customer Ingest"):
            wf = w["id"]
            break
    payload = {"name": "Customer Ingest (CSV + Shopify Flow → DB)",
               "nodes": nodes, "connections": conn,
               "settings": {"executionOrder": "v1"}}
    if wf:
        s, b = http("PUT", f"/workflows/{wf}", payload)
        print(f"  Customer Ingest WF updated: {wf}  (HTTP {s})")
    else:
        s, b = http("POST", "/workflows", payload)
        wf = json.loads(b)["id"]
        print(f"  Customer Ingest WF created: {wf}")
    http("PUT", f"/workflows/{wf}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf}/activate")
    return wf


# ---------- Step 4: POST all customers in batches ----------
def import_csv():
    rows = []
    with open(CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            mapped = {snake: r.get(orig, "") for orig, snake in COL_MAP}
            rows.append(mapped)
    print(f"  Read {len(rows)} customers from CSV")

    BATCH = 25
    url = f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_PATH}"
    sent = 0
    failures = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        try:
            req = urllib.request.Request(url, data=json.dumps({"customers": batch}).encode(),
                                          method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 200:
                    sent += len(batch)
                    print(f"  ✓ batch {i//BATCH + 1}/{(len(rows)+BATCH-1)//BATCH}: +{len(batch)} (total {sent})")
                else:
                    failures += 1
                    print(f"  ✗ batch {i//BATCH + 1}: HTTP {r.status}")
        except Exception as e:
            failures += 1
            print(f"  ✗ batch {i//BATCH + 1}: {e}")
        time.sleep(0.5)  # be gentle on the webhook
    return sent, failures


# ---------- run ----------
print("Step 1: setup customers tab")
setup_customers_tab()

print("\nStep 2: build Customer Ingest workflow")
ingest_wf = build_customer_ingest()
time.sleep(2)

print("\nStep 3: bulk import 1402 customers")
sent, fails = import_csv()
print(f"\n✅ Done. Sent {sent} customer rows in {fails+1 if fails else 'all'} batches; failures: {fails}")
print(f"   Workflow: https://thebonpet.app.n8n.cloud/workflow/{ingest_wf}")
print(f"   Sheet customers tab: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={NEW_TAB_SHEETID}")

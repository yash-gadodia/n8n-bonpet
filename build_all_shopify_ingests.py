#!/usr/bin/env python3
"""Build a dedicated ingest pipeline for every remaining major Shopify entity:
  - products
  - checkouts (abandoned cart)
  - fulfillments (delivery tracking)
  - inventory_levels

Each pipeline = 1 tab in the existing workbook + 1 webhook URL + 1 n8n workflow (upsert by PK).
Designed so future automations can just read from the sheet without touching Shopify API.
Idempotent: re-run safely; existing tabs/workflows are updated, not duplicated.
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"


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


# ---- Entity specs ----

PRODUCTS_PARSE = r"""// Shopify product webhook payload → products tab row.
// Branches on X-Shopify-Topic: delete events skip upsert to avoid clobbering
// the existing row (delete payloads contain only {id}).
function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
const raw = $input.first().json;
const topic = (raw.headers && (raw.headers['x-shopify-topic'] || raw.headers['X-Shopify-Topic'])) || '';
const p = raw.body || raw;
if (topic === 'products/delete' || topic === 'products/deleted') {
  return [];  // no-op; preserve existing row data
}
const variants = p.variants || [];
return [{ json: {
  received_at: new Date().toISOString(),
  product_id: extractId(p.id),
  title: p.title || '',
  handle: p.handle || '',
  vendor: p.vendor || '',
  product_type: p.product_type || '',
  status: p.status || '',
  tags: p.tags || '',
  total_variants: variants.length,
  variant_ids_json: JSON.stringify(variants.map(v => ({ id: extractId(v.id), title: v.title, sku: v.sku, price: v.price, grams: v.grams, inventory_quantity: v.inventory_quantity }))),
  created_at: p.created_at || '',
  updated_at: p.updated_at || '',
  raw_body_json: JSON.stringify(p).slice(0, 5000),
}}];
"""

CHECKOUTS_PARSE = r"""// Shopify checkout webhook payload → checkouts tab row (for abandoned cart)
function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
function normPhone(p) { if (!p) return ''; const s = String(p).trim(); const d = s.replace(/[^\d]/g, ''); if (s.startsWith('+')) return s; if (d.length === 8 && /^[689]/.test(d)) return '+65' + d; if (d.length === 10 && d.startsWith('65')) return '+' + d; return s; }
const raw = $input.first().json;
const c = raw.body || raw;
const cust = c.customer || {};
const lis = c.line_items || [];
return [{ json: {
  received_at: new Date().toISOString(),
  checkout_token: c.token || c.checkout_token || '',
  checkout_id: extractId(c.id),
  email: c.email || cust.email || '',
  phone: normPhone(c.phone || cust.phone || (c.shipping_address && c.shipping_address.phone) || ''),
  first_name: cust.first_name || (c.shipping_address && c.shipping_address.first_name) || '',
  last_name: cust.last_name || (c.shipping_address && c.shipping_address.last_name) || '',
  customer_id: extractId(cust.id),
  total_price: c.total_price || '',
  currency: c.currency || 'SGD',
  line_items_count: lis.length,
  line_items_json: JSON.stringify(lis.map(li => ({ title: li.title, quantity: li.quantity, variant_id: extractId(li.variant_id), price: li.price }))),
  abandoned_checkout_url: c.abandoned_checkout_url || '',
  completed_at: c.completed_at || '',
  created_at: c.created_at || '',
  updated_at: c.updated_at || '',
  raw_body_json: JSON.stringify(c).slice(0, 5000),
}}];
"""

FULFILLMENTS_PARSE = r"""// Shopify fulfillment webhook payload → fulfillments tab row
function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
const raw = $input.first().json;
const f = raw.body || raw;
return [{ json: {
  received_at: new Date().toISOString(),
  fulfillment_id: extractId(f.id),
  order_id: extractId(f.order_id),
  status: f.status || '',
  shipment_status: f.shipment_status || '',
  tracking_number: f.tracking_number || (f.tracking_numbers && f.tracking_numbers[0]) || '',
  tracking_url: f.tracking_url || (f.tracking_urls && f.tracking_urls[0]) || '',
  tracking_company: f.tracking_company || '',
  service: f.service || '',
  created_at: f.created_at || '',
  updated_at: f.updated_at || '',
  raw_body_json: JSON.stringify(f).slice(0, 5000),
}}];
"""

INVENTORY_PARSE = r"""// Shopify inventory_levels webhook payload → inventory tab row
function extractId(v) { if (!v) return ''; const s = String(v); const m = s.match(/(\d+)$/); return m ? m[1] : s; }
const raw = $input.first().json;
const i = raw.body || raw;
const key = extractId(i.inventory_item_id) + '_' + extractId(i.location_id);
return [{ json: {
  received_at: new Date().toISOString(),
  inventory_key: key,
  inventory_item_id: extractId(i.inventory_item_id),
  location_id: extractId(i.location_id),
  available: i.available !== undefined ? i.available : '',
  updated_at: i.updated_at || '',
  raw_body_json: JSON.stringify(i).slice(0, 2000),
}}];
"""


ENTITIES = [
    {
        "name": "products",
        "sheet_id": 300300,
        "tab": "products",
        "webhook_path": "shopify-products-ingest",
        "wf_name": "Products Ingest (Shopify → DB)",
        "parse_js": PRODUCTS_PARSE,
        "headers": [
            "received_at", "product_id", "title", "handle", "vendor", "product_type",
            "status", "tags", "total_variants", "variant_ids_json",
            "created_at", "updated_at", "raw_body_json",
        ],
        "pk": "product_id",
    },
    {
        "name": "checkouts",
        "sheet_id": 400400,
        "tab": "checkouts",
        "webhook_path": "shopify-checkouts-ingest",
        "wf_name": "Checkouts Ingest (Shopify → DB)",
        "parse_js": CHECKOUTS_PARSE,
        "headers": [
            "received_at", "checkout_token", "checkout_id", "email", "phone",
            "first_name", "last_name", "customer_id", "total_price", "currency",
            "line_items_count", "line_items_json", "abandoned_checkout_url",
            "completed_at", "created_at", "updated_at", "raw_body_json",
        ],
        "pk": "checkout_token",
    },
    {
        "name": "fulfillments",
        "sheet_id": 500500,
        "tab": "fulfillments",
        "webhook_path": "shopify-fulfillments-ingest",
        "wf_name": "Fulfillments Ingest (Shopify → DB)",
        "parse_js": FULFILLMENTS_PARSE,
        "headers": [
            "received_at", "fulfillment_id", "order_id", "status", "shipment_status",
            "tracking_number", "tracking_url", "tracking_company", "service",
            "created_at", "updated_at", "raw_body_json",
        ],
        "pk": "fulfillment_id",
    },
    # inventory removed per user request — tab + workflow deleted
]


def setup_tab(spec):
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": spec["sheet_id"], "title": spec["tab"]}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in spec["headers"]]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": spec["sheet_id"], "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    tmp_path = f"tmp-add-{spec['name']}-tab"
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": tmp_path, "responseMode": "lastNode", "options": {}},
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
    s, b = http("POST", "/workflows", {"name": f"TEMP Add {spec['tab']} Tab",
                                       "nodes": nodes, "connections": conn,
                                       "settings": {"executionOrder": "v1"}})
    wf = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://n8n.thebonpet.com/webhook/{tmp_path}",
            data=b'{}', method="POST", headers={"Content-Type": "application/json"}), timeout=30)
        print(f"  [{spec['name']}] tab setup fired")
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode()[:200]
        tag = "already exists ✓" if "already exists" in body_resp else f"HTTP {e.code}"
        print(f"  [{spec['name']}] {tag}")
    time.sleep(1)
    http("DELETE", f"/workflows/{wf}")


def build_ingest(spec):
    webhook = {
        "parameters": {"httpMethod": "POST", "path": spec["webhook_path"],
                       "responseMode": "onReceived", "responseData": "noData", "options": {}},
        "id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }
    parse = {
        "parameters": {"jsCode": spec["parse_js"]},
        "id": str(uuid.uuid4()), "name": "Parse", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [240, 0],
    }
    append = {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": spec["sheet_id"], "mode": "list",
                          "cachedResultName": spec["tab"],
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={spec['sheet_id']}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "matchingColumns": [spec["pk"]],
                "schema": [{"id": h, "displayName": h, "required": False,
                            "defaultMatch": h == spec["pk"], "display": True,
                            "type": "string", "canBeUsedToMatch": True} for h in spec["headers"]],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": f"Append to {spec['tab']}",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5, "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }
    nodes = [webhook, parse, append]
    conn = {
        "Webhook": {"main": [[{"node": "Parse", "type": "main", "index": 0}]]},
        "Parse": {"main": [[{"node": f"Append to {spec['tab']}", "type": "main", "index": 0}]]},
    }
    payload = {"name": spec["wf_name"], "nodes": nodes, "connections": conn,
               "settings": {"executionOrder": "v1"}}
    existing = json.loads(http("GET", "/workflows")[1]).get("data", [])
    wf_id = next((w["id"] for w in existing if w["name"] == spec["wf_name"]), None)
    if wf_id:
        s, b = http("PUT", f"/workflows/{wf_id}", payload)
        print(f"  [{spec['name']}] WF updated: {wf_id} (HTTP {s})")
    else:
        s, b = http("POST", "/workflows", payload)
        wf_id = json.loads(b)["id"]
        print(f"  [{spec['name']}] WF created: {wf_id}")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    return wf_id


print("Step 1: add tabs + headers")
for spec in ENTITIES:
    setup_tab(spec)

print("\nStep 2: build ingest workflows")
results = []
for spec in ENTITIES:
    wf_id = build_ingest(spec)
    results.append((spec, wf_id))

print("\n✅ All done.\n")
print(f"{'Entity':<14}{'Webhook URL'}")
print("-" * 80)
for spec, wf_id in results:
    print(f"{spec['name']:<14}https://n8n.thebonpet.com/webhook/{spec['webhook_path']}")
print()
print(f"{'Entity':<14}{'Sheet tab':<30}{'n8n workflow'}")
print("-" * 80)
for spec, wf_id in results:
    print(f"{spec['name']:<14}gid={spec['sheet_id']:<25}https://n8n.thebonpet.com/workflow/{wf_id}")

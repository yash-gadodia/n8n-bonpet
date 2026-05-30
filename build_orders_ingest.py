#!/usr/bin/env python3
"""Build the Customer Orders Ingest workflow.

Receives POSTs from Shopify Flow ("Order paid" trigger) and appends each order
(with PII) to the 'Bon Pet — Customer Orders DB' sheet's `orders` tab.
"""
import json, uuid, os, urllib.request, urllib.error

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"

WEBHOOK_PATH = "shopify-orders-ingest"

PARSE_JS = r"""// Parse Shopify order(s) → row(s) for the Customer Orders DB sheet.
// Accepts: single order object (Shopify native webhook) OR {orders: [...]} (bulk import).
const raw = $input.first().json;
const body = raw.body || raw;
const ordersIn = body.orders ? body.orders : [body];

// Normalize SG phones: bare 8-digit → prepend +65
function normalizePhone(p) {
  if (!p) return '';
  const s = String(p).trim();
  const digits = s.replace(/[^\d]/g, '');
  if (s.startsWith('+')) return s;
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  return s;
}

const out = [];
for (const j of ordersIn) {

// Shopify Flow GIDs look like 'gid://shopify/Order/12345' — extract numeric.
function extractId(v) {
  if (!v) return '';
  const s = String(v);
  const m = s.match(/(\d+)$/);
  return m ? m[1] : s;
}

const order_id = extractId(j.order_id || j.id);
const order_name = j.order_name || j.name || '';
const order_date = j.order_date || j.created_at || j.createdAt || new Date().toISOString();

const cust = j.customer || {};
const customer_id = extractId(cust.id || j.customer_id);
const first_name = cust.first_name || cust.firstName || j.first_name
  || (j.shipping_address && j.shipping_address.first_name) || '';
const last_name  = cust.last_name  || cust.lastName  || j.last_name
  || (j.shipping_address && j.shipping_address.last_name) || '';
const email      = cust.email || j.email || j.contact_email || '';
const phone      = normalizePhone(cust.phone || j.phone
  || (j.shipping_address && j.shipping_address.phone)
  || (j.billing_address && j.billing_address.phone) || '');

const ship = j.shipping_address || j.shippingAddress || {};
const city = ship.city || '';

const tags = Array.isArray(j.tags) ? j.tags.join(',') : (j.tags || '');

const total_price = j.total_price || j.totalPrice
  || (j.totalPriceSet && j.totalPriceSet.shopMoney && j.totalPriceSet.shopMoney.amount) || '';
const currency = j.currency || j.currencyCode
  || (j.totalPriceSet && j.totalPriceSet.shopMoney && j.totalPriceSet.shopMoney.currencyCode) || 'SGD';

const lines = j.line_items || j.lineItems || [];
let total_grams = 0;
let is_subscription = false;
const cart_parts = [];
const cleaned_lines = [];
for (const li of lines) {
  const variant_id = extractId(li.variant_id || (li.variant && li.variant.id) || li.variantId);
  const quantity = Number(li.quantity || li.qty || 0);
  const grams = Number(li.grams || (li.variant && li.variant.weight) || 0);
  const title = li.title || (li.variant && li.variant.title) || '';
  const selling_plan = li.selling_plan || li.sellingPlan
    || (li.selling_plan_allocation && li.selling_plan_allocation.selling_plan && li.selling_plan_allocation.selling_plan.name)
    || null;
  if (selling_plan) is_subscription = true;
  total_grams += grams * quantity;
  cleaned_lines.push({ variant_id, quantity, grams, title, selling_plan });
  if (variant_id && quantity > 0) cart_parts.push(`${variant_id}:${quantity}`);
}

const cart_link = cart_parts.length
  ? `https://thebonpet.com/cart/${cart_parts.join(',')}`
  : 'https://thebonpet.com/collections/all';

out.push({ json: {
  received_at: new Date().toISOString(),
  order_id, order_name, order_date,
  customer_id, first_name, last_name, email, phone,
  total_price, currency,
  total_grams,
  is_subscription,
  line_items_json: JSON.stringify(cleaned_lines),
  cart_link,
  city,
  tags,
}});
}
return out;
"""


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


def webhook_node():
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_PATH,
            "responseMode": "onReceived",
            "responseData": "noData",
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Shopify Flow Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
        "webhookId": str(uuid.uuid4()),
    }


def parse_node():
    return {
        "parameters": {"jsCode": PARSE_JS},
        "id": str(uuid.uuid4()),
        "name": "Parse Order",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [240, 0],
    }


HEADERS = [
    "received_at", "order_id", "order_name", "order_date",
    "customer_id", "first_name", "last_name", "email", "phone",
    "total_price", "currency", "total_grams", "is_subscription",
    "line_items_json", "cart_link", "city", "tags",
]


def append_node():
    return {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Bon Pet — Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": 0, "mode": "list",
                "cachedResultName": "orders",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
            },
            "columns": {
                "mappingMode": "autoMapInputData",
                "matchingColumns": ["order_id"],
                "schema": [{"id": h, "displayName": h, "required": False, "defaultMatch": h == "order_id", "display": True, "type": "string", "canBeUsedToMatch": True} for h in HEADERS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Append to Orders DB",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.7,
        "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


webhook = webhook_node()
parse = parse_node()
append = append_node()
nodes = [webhook, parse, append]
connections = {
    webhook["name"]: {"main": [[{"node": parse["name"], "type": "main", "index": 0}]]},
    parse["name"]: {"main": [[{"node": append["name"], "type": "main", "index": 0}]]},
}

# Look for existing workflow to update (re-runnable)
status, body = http("GET", "/workflows?name=Customer%20Orders%20Ingest%20(Shopify%20Flow%20%E2%86%92%20DB)")
existing = json.loads(body).get("data", []) if status == 200 else []
existing_match = [w for w in existing if w["name"].startswith("Customer Orders Ingest")]
if existing_match:
    wf_id = existing_match[0]["id"]
    payload = {
        "name": "Customer Orders Ingest (Shopify Flow → DB)",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }
    status, body = http("PUT", f"/workflows/{wf_id}", payload)
    print(f"Updated existing  WF_ID = {wf_id}  → HTTP {status}")
else:
    status, body = http("POST", "/workflows", {
        "name": "Customer Orders Ingest (Shopify Flow → DB)",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    })
    print(f"Create → {status}")
    wf_id = json.loads(body)["id"]
    print(f"  WF_ID = {wf_id}")

# Transfer to team project
status, body = http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
if "same destination" in body:
    print("  Project: in team ✓")
else:
    print(f"  Transfer → {status}: {body[:120]}")

# Activate (so webhook URL is live)
status, body = http("POST", f"/workflows/{wf_id}/activate")
print(f"Activate → {status}")

print(f"\n✅ Done.")
print(f"   Workflow URL: https://n8n.thebonpet.com/workflow/{wf_id}")
print(f"   Webhook URL:  https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

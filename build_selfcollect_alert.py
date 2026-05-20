#!/usr/bin/env python3
"""Self-Collection Order Alert — pings Yash in the Team Bon Pet weslee thread
whenever a paid order has shipping_line.title containing 'self-collect'.

Trigger: Shopify orders/paid webhook (registered separately — see bottom).
Pipeline: webhook → Read Customers (enrich) → Format Alert → IF self-collect → Telegram (weslee).

Customer PII (name+phone) is pulled from the Customer Orders DB Google Sheet
(Customers tab gid 100100) because Shopify Basic blocks PII in Admin API payloads.
"""
import json, uuid, os, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Self-Collect Order Alert - Telegram"
WEBHOOK_PATH = "selfcollect-order-alert-9b3e1f7c2d"

TELEGRAM_CHAT_ID = "-1002184573790"          # Team Bon Pet supergroup
TELEGRAM_WESLEE_THREAD_ID = "34253"          # weslee thread (was "2" / ops thread until 2026-05-16)
TELEGRAM_TAG = "@yashgadodia"
TELEGRAM_TOKEN = open(os.path.expanduser("~/.telegram-weslee-bot-token")).read().strip()

GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CUSTOMERS_GID = 100100


FORMAT_JS = r"""// Parse Shopify orders/paid payload, detect Self-Collection, enrich + build Telegram message.
const p = $('Shopify Webhook (orders/paid)').first().json;
const body = p.body || p;

const shippingLines = body.shipping_lines || [];
const isSelfCollect = shippingLines.some(s =>
  String(s.title || '').toLowerCase().includes('self-collect') ||
  String(s.title || '').toLowerCase().includes('self collect') ||
  String(s.code || '').toLowerCase().includes('self-collect')
);

if (!isSelfCollect) {
  return [{ json: { is_self_collect: false, skip_reason: 'not a self-collect order' } }];
}

const orderName = body.name || `#${body.order_number || body.id}`;
const total = body.total_price || '0.00';
const currency = body.currency || 'SGD';
const customerId = String((body.customer || {}).id || '');

// Enrich from Customers tab (Shopify Basic PII workaround)
function normalizePhone(p) {
  if (!p) return '';
  let s = String(p).replace(/\s/g, '').trim();
  if (s.startsWith('+')) return s;
  const digits = s.replace(/\D/g, '');
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  if (digits) return '+' + digits;
  return '';
}

let fullName = '';
let phone = '';
let email = '';
for (const c of $('Read Customers').all()) {
  if (String(c.json.customer_id || '') === customerId) {
    fullName = `${c.json.first_name || ''} ${c.json.last_name || ''}`.trim();
    phone = normalizePhone(c.json.phone || c.json.default_address_phone || '');
    email = c.json.email || '';
    break;
  }
}
// Fall back to webhook payload (rare — present only if Shopify happens to include it)
if (!fullName) {
  const cust = body.customer || {};
  fullName = `${cust.first_name || ''} ${cust.last_name || ''}`.trim() || '(name not in Customers tab)';
}
if (!phone) phone = normalizePhone((body.customer || {}).phone || body.phone || '') || '—';
if (!email) email = (body.customer || {}).email || body.email || '';

// Delivery Date from note_attributes (Bon Pet's pickup-date field)
const deliveryDate = ((body.note_attributes || []).find(a => a.name === 'Delivery Date') || {}).value || '(not set)';

const items = (body.line_items || []).map(li => {
  const qty = li.quantity || 1;
  const title = li.title || '';
  const variant = li.variant_title && li.variant_title !== 'Default Title' ? ` (${li.variant_title})` : '';
  return `${qty}× ${title}${variant}`;
}).join('\n        ');

const notes = String(body.note || '').trim();
const notesLine = notes ? `\n📝 Notes: ${notes}` : '';

const createdSgt = new Date(body.created_at || Date.now()).toLocaleString('en-SG', {
  timeZone: 'Asia/Singapore',
  hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short',
});

const message = `🏪 *Self-collect order* ${orderName}

👤 *${fullName}* · ${phone}${email ? '\n📧 ' + email : ''}
📅 Pickup: *${deliveryDate}*
💰 ${currency} $${total}

🛒 Items:
        ${items}${notesLine}

📥 Ordered: ${createdSgt} SGT

""" + TELEGRAM_TAG + r""" heads up — queue for pickup at Siglap freezer.`;

return [{
  json: {
    is_self_collect: true,
    message: message,
    order_name: orderName,
    customer_phone: phone,
    delivery_date: deliveryDate,
  }
}];
"""


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key, "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try: return e.code, json.loads(b)
        except Exception: return e.code, b


webhook = {
    "parameters": {
        "httpMethod": "POST", "path": WEBHOOK_PATH,
        "responseMode": "onReceived", "options": {"rawBody": False},
    },
    "id": uid(), "name": "Shopify Webhook (orders/paid)",
    "type": "n8n-nodes-base.webhook", "typeVersion": 2,
    "position": [0, 300], "webhookId": WEBHOOK_PATH,
}

read_customers = {
    "parameters": {
        "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                       "cachedResultName": "Bon Pet — Customer Orders DB",
                       "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
        "sheetName": {"__rl": True, "value": CUSTOMERS_GID, "mode": "list",
                      "cachedResultName": "customers",
                      "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={CUSTOMERS_GID}"},
        "options": {},
    },
    "id": uid(), "name": "Read Customers",
    "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
    "position": [240, 300],
    "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    "executeOnce": True,
}

format_code = {
    "parameters": {"jsCode": FORMAT_JS},
    "id": uid(), "name": "Format Alert",
    "type": "n8n-nodes-base.code", "typeVersion": 2,
    "position": [480, 300],
}

is_selfcollect_if = {
    "parameters": {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
            "conditions": [{
                "id": uid(),
                "leftValue": "={{ $json.is_self_collect }}",
                "rightValue": True,
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    },
    "id": uid(), "name": "Is Self-Collect?",
    "type": "n8n-nodes-base.if", "typeVersion": 2.2,
    "position": [720, 300],
}

send_telegram = {
    "parameters": {
        "method": "POST",
        "url": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        "sendBody": True,
        "bodyParameters": {"parameters": [
            {"name": "chat_id", "value": TELEGRAM_CHAT_ID},
            {"name": "message_thread_id", "value": TELEGRAM_WESLEE_THREAD_ID},
            {"name": "text", "value": "={{ $json.message }}"},
            {"name": "parse_mode", "value": "Markdown"},
        ]},
        "options": {},
    },
    "id": uid(), "name": "Send Telegram (ops)",
    "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [960, 200],
    "onError": "continueRegularOutput",
}

nodes = [webhook, read_customers, format_code, is_selfcollect_if, send_telegram]
connections = {
    webhook["name"]:           {"main": [[{"node": read_customers["name"], "type": "main", "index": 0}]]},
    read_customers["name"]:    {"main": [[{"node": format_code["name"], "type": "main", "index": 0}]]},
    format_code["name"]:       {"main": [[{"node": is_selfcollect_if["name"], "type": "main", "index": 0}]]},
    is_selfcollect_if["name"]: {"main": [
        [{"node": send_telegram["name"], "type": "main", "index": 0}],
        [],
    ]},
}

payload = {
    "name": WF_NAME,
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
}


def find_existing():
    status, data = http("GET", "/workflows?limit=250")
    if status >= 300: return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME: return wf["id"]
    return None


if __name__ == "__main__":
    out = os.path.expanduser("~/n8n-bonpet/selfcollect_alert_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(nodes)} nodes → {out}")

    existing_id = find_existing()
    if existing_id:
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
        print(f"PUT existing {new_id} → HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {new_id} → HTTP {status}")
        if status >= 300: print("ERROR:", body)

    if new_id and status < 300:
        s, _ = http("POST", f"/workflows/{new_id}/activate")
        print(f"Activate HTTP {s}")
        print(f"\nWorkflow URL: https://n8n.thebonpet.com/workflow/{new_id}")
        print(f"Webhook URL:  https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

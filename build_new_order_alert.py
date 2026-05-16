#!/usr/bin/env python3
"""New Order Alert (all orders) → weslee thread.

Fires for every paid Shopify order. Posts to Team Bon Pet → weslee thread (34253).
Self-collect orders ALSO continue firing the existing dedicated Self-Collect
Order Alert in the ops thread (user chose 'Keep both , self-collect gets an
extra loud ping').

Payload categories included (per user selection):
  • Basics: order_name, customer name, phone
  • Items + total $ + discount code
  • Delivery method + date + address
  • Customer history tag (1st order / returning Nth / sub renewal)

Trigger: Shopify orders/paid webhook (URL must be registered in Shopify Admin
once after deploy , see end of script).
Pipeline: webhook → Read Customers (enrich) → Format → Telegram.
"""
import json, uuid, os, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "New Order Alert (all orders) → weslee thread"
WEBHOOK_PATH = "new-order-alert-7b2e4f8a1c"

TELEGRAM_CHAT_ID = "-1002184573790"          # Team Bon Pet supergroup
TELEGRAM_THREAD_ID = "34253"                 # weslee thread
TELEGRAM_TOKEN = open(os.path.expanduser("~/.telegram-weslee-bot-token")).read().strip()

GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CUSTOMERS_GID = 100100

ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"


FORMAT_JS = r"""// Parse Shopify orders/paid payload + enrich + build Telegram message.
const p = $('Shopify Webhook (orders/paid)').first().json;
const body = p.body || p;

const orderName = body.name || `#${body.order_number || body.id}`;
const total = body.total_price || '0.00';
const subtotal = body.subtotal_price || total;
const currency = body.currency || 'SGD';
const customerId = String((body.customer || {}).id || '');
const ordersCount = Number((body.customer || {}).orders_count || 1);

// ── Delivery method (from shipping_lines.title , matches OMS derivation) ──
const shippingLines = body.shipping_lines || [];
const shipTitle = String((shippingLines[0] || {}).title || '').toLowerCase();
let deliveryMethod = 'NinjaVan';
let deliveryEmoji = '📦';
if (shipTitle.includes('self-collect') || shipTitle.includes('self collect')) {
  deliveryMethod = 'Self-collection 🏪';
  deliveryEmoji = '🏪';
} else if (shipTitle.includes('cold chain') || shipTitle.includes('cold-chain')) {
  deliveryMethod = 'NinjaVan cold chain';
  deliveryEmoji = '❄️';
} else if (shipTitle.includes('next day') || shipTitle.includes('nextday')) {
  deliveryMethod = 'NinjaVan next day';
  deliveryEmoji = '📦';
}

// ── Discount + subscription detection ──
const discountCodes = (body.discount_codes || []).map(d => d.code).filter(Boolean);
const isSubscription = discountCodes.some(c => /^subscription/i.test(c));
const visibleDiscount = discountCodes.find(c => !/^subscription/i.test(c)) || null;

// ── Customer enrichment (PII workaround) ──
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

let fullName = '', phone = '', email = '';
for (const c of $('Read Customers').all()) {
  if (String(c.json.customer_id || '') === customerId) {
    fullName = `${c.json.first_name || ''} ${c.json.last_name || ''}`.trim();
    phone = normalizePhone(c.json.phone || c.json.default_address_phone || '');
    email = c.json.email || '';
    break;
  }
}
if (!fullName) {
  const cust = body.customer || {};
  fullName = `${cust.first_name || ''} ${cust.last_name || ''}`.trim() || '(name not in DB)';
}
if (!phone) phone = normalizePhone((body.customer || {}).phone || body.phone || '') || '(no phone)';
if (!email) email = (body.customer || {}).email || body.email || '';

// ── Customer history tag ──
let historyTag;
if (isSubscription && ordersCount === 1) historyTag = '🆕✨ *New subscriber!*';
else if (isSubscription) historyTag = `🔁 *Subscription renewal* (order #${ordersCount})`;
else if (ordersCount === 1) historyTag = '🆕 *First order!*';
else historyTag = `↩️ *Returning customer* (order #${ordersCount})`;

// ── Line items ──
const items = (body.line_items || []).map(li => {
  const qty = li.quantity || 1;
  const title = li.title || '';
  const variant = li.variant_title && li.variant_title !== 'Default Title' ? ` (${li.variant_title})` : '';
  return `${qty}× ${title}${variant}`;
}).join('\n        ');

// ── Delivery date + address ──
const deliveryDate = ((body.note_attributes || []).find(a => a.name === 'Delivery Date') || {}).value || '(not set)';
const shipAddr = body.shipping_address || {};
const addressLine = [shipAddr.address1, shipAddr.address2, shipAddr.zip].filter(Boolean).join(', ') || '(no address)';

// ── Notes ──
const notes = String(body.note || '').trim();
const notesLine = notes ? `\n📝 Notes: ${notes}` : '';

const createdSgt = new Date(body.created_at || Date.now()).toLocaleString('en-SG', {
  timeZone: 'Asia/Singapore',
  hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short',
});

const discountLine = visibleDiscount ? `\n🎟️ Code: *${visibleDiscount}*` : '';

const message = `${deliveryEmoji} *New order* ${orderName}

${historyTag}
👤 *${fullName}* · ${phone}${email ? '\n📧 ' + email : ''}

🛒 Items:
        ${items}${discountLine}
💰 ${currency} $${total}

📅 ${deliveryMethod}
📆 ${deliveryDate}
📍 ${addressLine}${notesLine}

📥 ${createdSgt} SGT`;

return [{
  json: {
    chat_id: """ + TELEGRAM_CHAT_ID + r""",
    message_thread_id: """ + TELEGRAM_THREAD_ID + r""",
    text: message,
    order_name: orderName,
    delivery_method: deliveryMethod,
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


def build():
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
                           "cachedResultName": "Bon Pet Customer Orders DB",
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
    telegram = {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ "
                "chat_id: $json.chat_id, "
                "message_thread_id: $json.message_thread_id, "
                "text: $json.text, "
                "parse_mode: 'Markdown', "
                "disable_web_page_preview: true "
                "}) }}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Telegram Post",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [720, 300],
        "onError": "continueRegularOutput",
    }

    nodes = [webhook, read_customers, format_code, telegram]
    connections = {
        webhook["name"]:        {"main": [[{"node": read_customers["name"], "type": "main", "index": 0}]]},
        read_customers["name"]: {"main": [[{"node": format_code["name"],   "type": "main", "index": 0}]]},
        format_code["name"]:    {"main": [[{"node": telegram["name"],      "type": "main", "index": 0}]]},
    }

    return {
        "name": WF_NAME, "nodes": nodes, "connections": connections,
        "settings": {"executionOrder": "v1", "errorWorkflow": ERROR_ALERTER_ID, "timezone": "Asia/Singapore"},
    }


def main():
    wf = build()
    out_path = os.path.join(os.path.dirname(__file__), "new_order_alert_payload.json")
    with open(out_path, "w") as f:
        json.dump(wf, f, indent=2)
    print(f"💾 wrote {out_path}")

    s, listing = http("GET", "/workflows")
    existing = None
    if s == 200 and isinstance(listing, dict):
        existing = next((w for w in listing.get("data", []) if w.get("name") == WF_NAME), None)

    if existing:
        wf_id = existing["id"]
        s2, _ = http("PUT", f"/workflows/{wf_id}", wf)
        print(f"🔁 PUT /workflows/{wf_id} → {s2}")
    else:
        s2, body = http("POST", "/workflows", wf)
        wf_id = body.get("id") if isinstance(body, dict) else None
        print(f"➕ POST /workflows → {s2}  id={wf_id}")
    if s2 >= 300:
        print(body); raise SystemExit(1)

    s3, _ = http("POST", f"/workflows/{wf_id}/activate")
    print(f"✅ activate → {s3}")

    print(f"""
🌐 Webhook URL: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}

📋 NEXT STEP , register this URL in Shopify Admin (ONE-TIME setup):
  1. Open: https://admin.shopify.com/store/d2ac44-d5/settings/notifications
  2. Scroll to 'Webhooks' section, click 'Create webhook'
  3. Event: 'Order payment'
  4. Format: 'JSON'
  5. URL: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}
  6. Webhook API version: latest
  7. Save

   (the existing Self-Collect Order Alert webhook stays as-is, so self-collect
    orders will ALSO get the loud ping in the ops thread , per your call to
    keep both)

🧪 Smoke test (without waiting for a real order):
  Send a synthetic Shopify orders/paid payload to the webhook and watch
  the weslee thread for the alert.
""")


if __name__ == "__main__":
    main()

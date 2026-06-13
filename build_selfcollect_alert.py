#!/usr/bin/env python3
"""Self-Collection Order Alert - routes each self-collect order to the right pickup
IC in Telegram, keyed on the postal code in the "Self-Collection - <postal>" shipping title.

  448908 / legacy "Self-Collection"  ->  Siglap (Yash):   main weslee thread tag + DM
  681810                              ->  CCK (Chandani):  her group tag + main thread info + DM

Trigger: Shopify orders/paid webhook (registered separately, see bottom).
Pipeline: webhook -> Read Customers (enrich) -> Format Alert (fan out send-jobs) -> IF self-collect -> Telegram.

Customer PII (name+phone) is pulled from the Customer Orders DB Google Sheet
(Customers tab gid 100100) because Shopify Basic blocks PII in Admin API payloads.
"""
import json, uuid, os, subprocess, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"

# OMS (api.thebonpet.com) is the PII fallback when a brand-new customer isn't in the
# Customers tab yet (Shopify Basic redacts customer/address PII from the webhook payload).
WMS_PAT = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wms-pat", "-w"]
).decode().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
WF_NAME = "Self-Collect Order Alert - Telegram"
WEBHOOK_PATH = "selfcollect-order-alert-9b3e1f7c2d"

TELEGRAM_CHAT_ID = "-1002184573790"          # Team Bon Pet supergroup (main thread)
TELEGRAM_WESLEE_THREAD_ID = 34253            # weslee thread (was "2" / ops thread until 2026-05-16)
TELEGRAM_TOKEN = open(os.path.expanduser("~/.telegram-weslee-bot-token")).read().strip()

# ── Pickup points ──────────────────────────────────────────────────────────
# Pickup point is encoded in the shipping title "Self-Collection - <postal>".
#   448908 (or legacy bare "Self-Collection")  → Siglap   → Yash
#   681810                                      → Choa Chu Kang (CCK) → Chandani
CCK_POSTALS = ["681810"]                      # postal codes routed to Chandani's CCK point

# Siglap (Yash)
YASH_USERNAME = "yashgadodia"
YASH_DM_ID = 166637821                        # Yash's private chat with @weslee_bot

# CCK (Chandani)
CHANDANI_CHAT_ID = "-1004221528278"          # "Chandani X The Bon Pet" supergroup (migrated from -5033434144)
CHANDANI_USERNAME = "chandkiraat"            # @-tag in her group
CHANDANI_DM_ID = 579742150                   # Chandani's private chat with @weslee_bot

# Launch Cycle (external advisory agency) - visibility copy of every self-collect order
LAUNCHCYCLE_CHAT_ID = "-5177312185"          # "Launch Cycle X The Bon Pet" group

GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CUSTOMERS_GID = 100100


FORMAT_JS = r"""// Parse orders/paid, detect self-collect + pickup point, enrich, fan out send-jobs (group ping(s) + IC DM).
const p = $('Shopify Webhook (orders/paid)').first().json;
const body = p.body || p;

const shippingLines = body.shipping_lines || [];
const isSelfCollectLine = s => {
  const t = (String(s.title || '') + ' ' + String(s.code || '')).toLowerCase();
  return t.includes('self-collect') || t.includes('self collect') || t.includes('self-collection');
};
const scLine = shippingLines.find(isSelfCollectLine);

// An order can carry MULTIPLE shipping lines. A $0 "Self-Collection" line sometimes rides
// alongside a real paid courier line (seen on $0 first-subscription orders) — that order is a
// DELIVERY, not a pickup, so it must NOT fire a self-collect alert. Treat any non-self-collect
// line that is either priced > 0 or named like a courier as the authoritative delivery method.
const hasRealDelivery = shippingLines.some(s => {
  if (isSelfCollectLine(s)) return false;
  const t = (String(s.title || '') + ' ' + String(s.code || '')).toLowerCase();
  const priced = parseFloat(s.price || '0') > 0;
  return priced || t.includes('ninja') || t.includes('cold chain') ||
         t.includes('lalamove') || t.includes('courier') || t.includes('delivery');
});

if (!scLine || hasRealDelivery) {
  return [{ json: { is_self_collect: false,
    skip_reason: !scLine ? 'not a self-collect order' : 'delivery order with phantom self-collect line' } }];
}

// Pickup point from the 6-digit postal in "Self-Collection - <postal>".
const postal = (String(scLine.title || '').match(/(\d{6})/) || [])[1] || '';
const CCK_POSTALS = __CCK_POSTALS__;
const isCCK = CCK_POSTALS.indexOf(postal) !== -1;

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
// Fallback 1: Shopify webhook payload (rare under Shopify Basic — PII usually redacted).
if (!fullName) {
  const cust = body.customer || {};
  fullName = `${cust.first_name || ''} ${cust.last_name || ''}`.trim();
}
if (!phone) phone = normalizePhone((body.customer || {}).phone || body.phone || '');
if (!email) email = (body.customer || {}).email || body.email || '';
// Fallback 2: OMS (api.thebonpet.com) — has name+phone for brand-new customers who aren't
// in the Customers tab yet. This is what fixes "(name not in Customers tab) / (no phone)".
if (!fullName || !phone) {
  let omsOrders = [];
  try { omsOrders = ($('Fetch OMS Order').first().json || {}).orders || []; } catch (e) {}
  const want = String(orderName).replace(/^#/, '').trim();
  const omsO = omsOrders.find(o => String(o.order_name || '').replace(/^#/, '').trim() === want);
  if (omsO) {
    if (!fullName) {
      fullName = String(omsO.customer || omsO.shipping_name ||
        `${omsO.customer_first_name || ''} ${omsO.customer_last_name || ''}`.trim() || '').trim();
    }
    if (!phone) phone = normalizePhone(omsO.shipping_phone || omsO.customer_contact || '');
    if (!email) email = omsO.customer_email || '';
  }
}
// Final placeholders only if every source missed.
if (!fullName) fullName = '(name not in Customers tab)';
if (!phone) phone = '(no phone)';

const phoneDigits = String(phone || '').replace(/[^0-9]/g, '');
const waLink = phoneDigits.length >= 8 ? `https://wa.me/${phoneDigits}` : '';

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

// Shared order summary block, reused across the group ping(s) + IC DM.
const summary = `👤 *${fullName}* · ${phone}${waLink ? '\n💬 message buyer: ' + waLink : ''}${email ? '\n📧 ' + email : ''}
📅 Pickup: *${deliveryDate}*
💰 ${currency} $${total}

🛒 Items:
        ${items}${notesLine}

📥 Ordered: ${createdSgt} SGT`;

// Telegram targets
const MAIN_CHAT = '__MAIN_CHAT__';
const MAIN_THREAD = __MAIN_THREAD__;
const CHANDANI_CHAT = '__CHANDANI_CHAT__';
const CHANDANI_USERNAME = '__CHANDANI_USERNAME__';
const CHANDANI_DM = __CHANDANI_DM__;
const YASH_USERNAME = '__YASH_USERNAME__';
const YASH_DM = __YASH_DM__;
const LAUNCHCYCLE_CHAT = '__LC_CHAT__';

const jobs = [];
if (isCCK) {
  const tag = CHANDANI_USERNAME ? `@${CHANDANI_USERNAME}` : 'Chandani';
  // 1) Chandani's group, actionable + tagged
  jobs.push({ chat_id: CHANDANI_CHAT,
    text: `🏪 *Self-collect order · CCK* ${orderName}\n\n${summary}\n\n${tag} heads up, please queue this for pickup at the CCK point. 📦` });
  // 2) main team thread, visibility only (no tag)
  jobs.push({ chat_id: MAIN_CHAT, message_thread_id: MAIN_THREAD,
    text: `🏪 *Self-collect order · CCK (Chandani's point)* ${orderName}\n\n${summary}` });
  // 3) DM Chandani to pack (only once she has registered with the bot)
  if (CHANDANI_DM) {
    jobs.push({ chat_id: CHANDANI_DM,
      text: `📦 *New CCK self-collect order to pack* ${orderName}\n\n${summary}` });
  }
} else {
  // Siglap (448908 / legacy bare "Self-Collection")
  // 1) main team thread, actionable + tag Yash
  jobs.push({ chat_id: MAIN_CHAT, message_thread_id: MAIN_THREAD,
    text: `🏪 *Self-collect order · Siglap* ${orderName}\n\n${summary}\n\n@${YASH_USERNAME} heads up, queue for pickup at the Siglap freezer. 📦` });
  // 2) DM Yash to pack
  if (YASH_DM) {
    jobs.push({ chat_id: YASH_DM,
      text: `📦 *New Siglap self-collect order to pack* ${orderName}\n\n${summary}` });
  }
}

// Launch Cycle (external agency) - visibility copy for every self-collect order
jobs.push({ chat_id: LAUNCHCYCLE_CHAT,
  text: `🏪 *Self-collect order · ${isCCK ? 'CCK' : 'Siglap'}* ${orderName}\n\n${summary}` });

return jobs.map(j => ({ json: Object.assign({ is_self_collect: true, order_name: orderName, pickup_point: isCCK ? 'cck' : 'siglap' }, j) }));
"""
FORMAT_JS = (FORMAT_JS
    .replace("__CCK_POSTALS__", json.dumps(CCK_POSTALS))
    .replace("__MAIN_CHAT__", TELEGRAM_CHAT_ID)
    .replace("__MAIN_THREAD__", str(TELEGRAM_WESLEE_THREAD_ID))
    .replace("__CHANDANI_CHAT__", CHANDANI_CHAT_ID)
    .replace("__CHANDANI_USERNAME__", CHANDANI_USERNAME)
    .replace("__CHANDANI_DM__", str(CHANDANI_DM_ID) if CHANDANI_DM_ID else "null")
    .replace("__YASH_USERNAME__", YASH_USERNAME)
    .replace("__YASH_DM__", str(YASH_DM_ID) if YASH_DM_ID else "null")
    .replace("__LC_CHAT__", LAUNCHCYCLE_CHAT_ID))


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
                       "cachedResultName": "Bon Pet - Customer Orders DB",
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

# PII fallback source: fetch this order from the OMS by order number. executeOnce so it fires
# exactly once even though Read Customers emits many rows. retryOnFail gives the OMS a few
# seconds to ingest a just-placed order before we give up.
fetch_oms_order = {
    "parameters": {
        "method": "GET",
        "url": "https://api.thebonpet.com/wms/orders",
        "sendQuery": True,
        "queryParameters": {"parameters": [
            {"name": "search", "value": "={{ String((($('Shopify Webhook (orders/paid)').first().json.body) || $('Shopify Webhook (orders/paid)').first().json).order_number || '') }}"},
            {"name": "limit", "value": "20"},
        ]},
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "Authorization", "value": f"Bearer {WMS_PAT}"},
            {"name": "User-Agent", "value": UA},
        ]},
        "options": {},
    },
    "id": uid(), "name": "Fetch OMS Order",
    "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [480, 300],
    "executeOnce": True,
    "onError": "continueRegularOutput",
    "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000,
}

format_code = {
    "parameters": {"jsCode": FORMAT_JS},
    "id": uid(), "name": "Format Alert",
    "type": "n8n-nodes-base.code", "typeVersion": 2,
    "position": [720, 300],
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
    "position": [960, 300],
}

send_telegram = {
    "parameters": {
        "method": "POST",
        "url": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": (
            "={{ JSON.stringify(Object.assign("
            "{ chat_id: $json.chat_id, text: $json.text, parse_mode: 'Markdown', disable_web_page_preview: true }, "
            "$json.message_thread_id ? { message_thread_id: $json.message_thread_id } : {}"
            ")) }}"
        ),
        "options": {},
    },
    "id": uid(), "name": "Send Telegram (ops)",
    "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [1200, 200],
    "onError": "continueRegularOutput",
}

nodes = [webhook, read_customers, fetch_oms_order, format_code, is_selfcollect_if, send_telegram]
connections = {
    webhook["name"]:           {"main": [[{"node": read_customers["name"], "type": "main", "index": 0}]]},
    read_customers["name"]:    {"main": [[{"node": fetch_oms_order["name"], "type": "main", "index": 0}]]},
    fetch_oms_order["name"]:   {"main": [[{"node": format_code["name"], "type": "main", "index": 0}]]},
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

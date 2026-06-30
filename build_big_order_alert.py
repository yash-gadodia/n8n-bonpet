#!/usr/bin/env python3
"""Big Order Alert — Shopify orders/paid webhook + threshold check.
When a big order comes in: broadcast to team AND send customer thank-you
(phone+name pulled from the customer orders Google Sheet).
"""
import json
import uuid
import os
import urllib.request
import urllib.error
import subprocess

from _notify import telegram_send_node, telegram_launchcycle_node
from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Big Order Alert - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_PATH = "big-order-alert-5f8c3d2a1b"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
CUSTOMER_SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CUSTOMERS_TAB_GID = 100100  # "Customers" tab — 1 row per customer, has total_orders/total_spent/phone

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",     # Yash
    "+6598531677",     # Nicolas
    "+6590108515",     # Bon Pet official
    "+6587993341",     # Rachel
    "+6282240119788",  # Bari (CS)
    "+6583513308",  # Siva (Launch Cycle agency - external)
    "+6588146498",  # Raghav (Launch Cycle agency - external)
]

BIG_ORDER_THRESHOLD_SGD = 200
SHOPIFY_TOPIC = "orders/paid"
WEBHOOK_URL = f"https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}"

LOOKUP_AND_FORMAT_JS = r"""// Build team alert + customer thank-you
// Primary lookup: Customers tab by customer_id (has phone + total_orders + total_spent)
const webhook = $('Shopify Webhook (orders/paid)').first().json;
const o = webhook.body || webhook;

const customers = $('Read Customers Tab').all();

function normPhone(p) {
  if (p == null || p === '') return '';
  let s = String(p).replace(/\s+/g, '').replace(/^\+/, '');
  if (/^\d{8}$/.test(s)) return '+65' + s;
  if (/^65\d{8}$/.test(s)) return '+' + s;
  if (/^\d{6,}$/.test(s)) return '+' + s;
  return '';
}
// Alias for the cooldown snippet which calls normalizePhone
const normalizePhone = normPhone;
""" + COOLDOWN_JS_SNIPPET + r"""
function fmtSGD(n) {
  const num = typeof n === 'number' ? n : parseFloat(n || '0');
  return num.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function shortTime(iso) {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Singapore',
      weekday: 'short', day: '2-digit', month: 'short',
      hour: '2-digit', minute: '2-digit', hour12: false
    }).format(new Date(iso)) + ' SGT';
  } catch { return iso; }
}

// ---- Match customer ----
const cidFromWebhook = String((o.customer && o.customer.id) || '');
const match = cidFromWebhook
  ? customers.find(r => String(r.json.customer_id || '') === cidFromWebhook)
  : null;

// Also tolerate a top-level customer_id on test payloads that don't nest it
const fallbackCid = String(o.customer_id || '');
const match2 = (!match && fallbackCid)
  ? customers.find(r => String(r.json.customer_id || '') === fallbackCid)
  : null;

const c = match || match2;
// Handle first_name/last_name swap cases (some sheet rows had only last_name filled)
const rawFirst = c ? String(c.json.first_name || '').trim() : '';
const rawLast  = c ? String(c.json.last_name  || '').trim() : '';
const displayFirstName = rawFirst || rawLast || '';
const customerFullName = [rawFirst, rawLast].filter(Boolean).join(' ');
const customerPhone = c ? normPhone(c.json.phone || c.json.default_address_phone) : '';
const lifetimeOrders = c ? Number(c.json.total_orders || 0) : 0;
const lifetimeSpent  = c ? Number(c.json.total_spent || 0) : 0;

// Detect subscription renewal — don't founder-thank a subscription that just auto-billed
const tagsStr = String(o.tags || '').toLowerCase();
const srcStr  = String(o.source_name || '').toLowerCase();
const discCodes = (o.discount_codes || []).map(d => String(d.code || '').toLowerCase());
const isSubscriptionOrder = discCodes.some(c => String(c).toLowerCase().startsWith('subscription'));

// ---- Team alert ----
const items = (o.line_items || []).map(li => {
  const qty = li.quantity || 0;
  const title = li.title || li.name || 'Unknown';
  const variant = li.variant_title && li.variant_title !== 'Default Title' ? ` - ${li.variant_title}` : '';
  return `• ${qty}x ${title}${variant}`;
}).slice(0, 8);
const moreCount = Math.max(0, (o.line_items || []).length - 8);
const itemsBlock = items.length ? items.join('\n') : '_(no line items)_';
const moreBlock = moreCount > 0 ? `\n... and ${moreCount} more` : '';

const orderName = o.name || `#${o.order_number || o.id}`;
const adminUrl = `https://admin.shopify.com/store/__STORE__/orders/${o.id}`;

const tags = (o.tags || '').split(',').map(t => t.trim()).filter(Boolean);
const tagsBlock = tags.length ? `\n🏷  Tags: ${tags.join(', ')}` : '';
const note = (o.note || '').trim();
const noteBlock = note ? `\n📝 Note: ${note}` : '';

let customerLine = '';
if (customerFullName) {
  customerLine = `\n👤 ${customerFullName}`;
  if (lifetimeOrders > 0) {
    customerLine += ` (order #${lifetimeOrders} lifetime, S$${fmtSGD(lifetimeSpent)} total)`;
  }
}

const teamMsg = `💰 *Big Order Alert!*
_${shortTime(o.created_at || o.processed_at || new Date().toISOString())}_

Order ${orderName}
Total: *S$${fmtSGD(o.total_price)}*${customerLine}

📦 Items
${itemsBlock}${moreBlock}${tagsBlock}${noteBlock}

🔗 ${adminUrl}`;

// ---- Customer thank-you (tiered by lifetime orders) ----
const greeting = displayFirstName ? `Hi ${displayFirstName}!` : 'Hi!';

let bodyCopy;
if (lifetimeOrders >= 5) {
  bodyCopy = `Yash here from The Bon Pet. You're one of our most loyal pet parents (this is your ${lifetimeOrders}th order with us!) and honestly, truly grateful. Your furkid is in such good hands 💛`;
} else if (lifetimeOrders >= 2) {
  bodyCopy = `Yash here from The Bon Pet. Thanks so much for ordering with us again, it really means a lot that you keep coming back 💛`;
} else {
  bodyCopy = `Yash here from The Bon Pet. Had to say thanks personally for your order. Orders like yours really mean a lot to our small team here in SG.`;
}

const customerMsg = `${greeting} 🐾

${bodyCopy}

Hope your furkid loves the food. Any feedback or questions, just reply here. I'll see it.

❤️ Yash & the Bon Pet team`;

const inGlobalCooldown = customerPhone ? isInGlobalCooldown(customerPhone) : false;

return [{
  json: {
    team_msg: teamMsg,
    customer_msg: customerMsg,
    customer_phone: customerPhone,
    customer_name: customerFullName,
    customer_matched: !!c,
    is_subscription_order: isSubscriptionOrder,
    in_global_cooldown: inGlobalCooldown,
    lifetime_orders: lifetimeOrders,
    lifetime_spent: lifetimeSpent,
    order_id: o.id,
    order_name: orderName,
    total_price: o.total_price,
    // wa_sent_log (global) append fields (phone col aliased from customer_phone)
    phone: customerPhone,
    workflow: 'big_order_alert',
    template: lifetimeOrders >= 5 ? 'thank_you_vip' : (lifetimeOrders >= 2 ? 'thank_you_repeat' : 'thank_you_new'),
    sent_at: new Date().toISOString(),
    notes: 'total=S$' + o.total_price + ',lifetime_orders=' + lifetimeOrders,
  }
}];
""".replace("__STORE__", SHOPIFY_STORE)


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: return e.code, json.loads(body)
        except Exception: return e.code, body


def code_node(name, pos, js):
    return {
        "parameters": {"jsCode": js},
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": pos,
    }


def team_wa_node(name, pos, phone):
    return {
        "parameters": {
            "method": "POST",
            "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-API-Key", "value": WA_KEY},
            ]},
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "phone_number", "value": phone},
                {"name": "message", "value": "={{ $json.team_msg }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def customer_wa_node(name, pos):
    return {
        "parameters": {
            "method": "POST",
            "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-API-Key", "value": WA_KEY},
            ]},
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "phone_number", "value": "={{ $json.customer_phone }}"},
                {"name": "message", "value": "={{ $json.customer_msg }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def build():
    trigger = {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_PATH,
            "responseMode": "onReceived",
            "options": {"rawBody": False},
        },
        "id": uid(), "name": "Shopify Webhook (orders/paid)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_PATH,
    }

    threshold_if = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": f"={{{{ parseFloat($json.body?.total_price || $json.total_price || '0') }}}}",
                    "rightValue": BIG_ORDER_THRESHOLD_SGD,
                    "operator": {"type": "number", "operation": "gte"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": f"Total >= S${BIG_ORDER_THRESHOLD_SGD}?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [240, 300],
    }

    read_sheet = {
        "parameters": {
            "documentId": {
                "__rl": True, "value": CUSTOMER_SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{CUSTOMER_SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": CUSTOMERS_TAB_GID, "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{CUSTOMER_SHEET_ID}/edit#gid={CUSTOMERS_TAB_GID}",
            },
            "options": {},
        },
        "id": uid(), "name": "Read Customers Tab",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [480, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    read_global = read_global_sent_log_node([620, 300])

    lookup_format = code_node("Lookup + Format", [720, 300], LOOKUP_AND_FORMAT_JS)

    team_sends = [
        team_wa_node(f"Send Team #{i+1}", [960, 100 + i * 90], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [960, 100 + len(RECIPIENTS) * 90]
    )
    telegram_lc = telegram_launchcycle_node(
        "Send Telegram LaunchCycle", [960, 200 + len(RECIPIENTS) * 90], "={{ $json.team_msg }}"
    )

    # Customer send is gated by three checks:
    # 1) has phone  2) not a subscription renewal  3) 30-min cool-off delay
    customer_phone_if = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.customer_phone }}",
                    "rightValue": "",
                    "operator": {"type": "string", "operation": "notEmpty", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Has Customer Phone?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [960, 600],
    }

    not_subscription_if = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.is_subscription_order }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "notEqual", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Not a Subscription?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [1200, 600],
    }

    not_in_cooldown_if = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.in_global_cooldown }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "notEqual", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Not in Global Cooldown?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [1320, 600],
    }

    wait_30min = {
        "parameters": {"amount": 30, "unit": "minutes"},
        "id": uid(), "name": "Wait 30 min",
        "type": "n8n-nodes-base.wait", "typeVersion": 1.1,
        "position": [1440, 600], "webhookId": uid(),
    }

    customer_send = customer_wa_node("Send Customer Thank-You", [1680, 600])
    log_global = append_global_sent_log_node([1920, 600])

    nodes = [trigger, threshold_if, read_sheet, read_global, lookup_format, *team_sends, telegram_send, telegram_lc,
             customer_phone_if, not_subscription_if, not_in_cooldown_if, wait_30min, customer_send, log_global]

    connections = {
        trigger["name"]:        {"main": [[{"node": threshold_if["name"], "type": "main", "index": 0}]]},
        threshold_if["name"]:   {"main": [
            [{"node": read_sheet["name"], "type": "main", "index": 0}],  # true
            [],  # false
        ]},
        read_sheet["name"]:     {"main": [[{"node": read_global["name"], "type": "main", "index": 0}]]},
        read_global["name"]:    {"main": [[{"node": lookup_format["name"], "type": "main", "index": 0}]]},
        lookup_format["name"]:  {"main": [[
            *[{"node": n["name"], "type": "main", "index": 0} for n in [*team_sends, telegram_send, telegram_lc]],
            {"node": customer_phone_if["name"], "type": "main", "index": 0},
        ]]},
        customer_phone_if["name"]: {"main": [
            [{"node": not_subscription_if["name"], "type": "main", "index": 0}],  # true
            [],
        ]},
        not_subscription_if["name"]: {"main": [
            [{"node": not_in_cooldown_if["name"], "type": "main", "index": 0}],  # true (not subscription)
            [],  # false (is subscription — skip customer thank-you)
        ]},
        not_in_cooldown_if["name"]: {"main": [
            [{"node": wait_30min["name"], "type": "main", "index": 0}],  # true (not in cooldown)
            [],  # false (in cooldown — skip)
        ]},
        wait_30min["name"]: {"main": [[{"node": customer_send["name"], "type": "main", "index": 0}]]},
        customer_send["name"]: {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
    }

    return {
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
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/big_order_alert_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes → {out}")

    existing_id = find_existing()
    if existing_id:
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
        print(f"PUT existing {new_id} → HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {new_id} → HTTP {status}")

    if new_id and status < 300:
        http("PUT", f"/workflows/{new_id}/transfer",
             {"destinationProjectId": TEAM_PROJECT_ID})
        print("Transferred to team project")
        s, _ = http("POST", f"/workflows/{new_id}/activate")
        print(f"Activate HTTP {s}")

    print(f"\nShopify webhook already registered: {SHOPIFY_TOPIC} -> {WEBHOOK_URL}")

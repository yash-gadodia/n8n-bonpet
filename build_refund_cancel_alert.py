#!/usr/bin/env python3
"""Refund & Cancel Alert — two Shopify webhooks (refunds/create + orders/cancelled) feeding
into one shared team-broadcast workflow. Customer info enriched from the Customers tab.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Refund & Cancel Alert - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
REFUND_WEBHOOK_PATH = "refund-alert-7a2c8b4f1e"
CANCEL_WEBHOOK_PATH = "cancel-alert-3d9f6a2c8b"

SHOPIFY_STORE = "d2ac44-d5"
GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
CUSTOMER_SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CUSTOMERS_TAB_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6282240119788",  # Bari (CS agent, ID)
]

TAG_REFUND_JS = r"""
// Tag the webhook payload as a refund event
const p = $input.first().json;
const body = p.body || p;
return [{ json: { event_type: 'refund', payload: body } }];
"""

TAG_CANCEL_JS = r"""
// Tag the webhook payload as a cancel event
const p = $input.first().json;
const body = p.body || p;
return [{ json: { event_type: 'cancel', payload: body } }];
"""

FORMAT_JS = r"""// Format team alert for refund or cancel, enriched with customer info from Customers tab
// Only one of the two Tag nodes fired in this execution — find which.
function tryRead(nodeName) {
  try { return $(nodeName).all(); } catch (e) { return []; }
}
const refundTag = tryRead('Tag Refund');
const cancelTag = tryRead('Tag Cancel');
const tagged = (refundTag.length > 0 ? refundTag[0].json
             :  cancelTag.length > 0 ? cancelTag[0].json
             :  null);
if (!tagged) {
  return [{ json: { message: '(no tagged payload found — alert skipped)', event_type: 'unknown' } }];
}
const eventType = tagged.event_type;
const p = tagged.payload;

const customers = $('Read Customers Tab').all();

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

// For refunds, Shopify sends a refund object with order_id top-level.
// For cancellations, Shopify sends the full order object with cancelled_at set.
let orderId, orderName, orderTotal, createdAt, lineItems, reason, refundAmount, customerId;

if (eventType === 'refund') {
  orderId = p.order_id;
  orderName = null;  // refund payload doesn't include order name
  orderTotal = null;
  createdAt = p.created_at || p.processed_at;
  lineItems = (p.refund_line_items || []).map(rli => ({
    quantity: rli.quantity || 0,
    title: (rli.line_item && (rli.line_item.title || rli.line_item.name)) || 'Unknown',
    variant_title: rli.line_item && rli.line_item.variant_title,
  }));
  // Refund amount = sum of 'refund' kind transactions with status 'success'
  refundAmount = (p.transactions || [])
    .filter(t => t.kind === 'refund' && t.status === 'success')
    .reduce((s, t) => s + parseFloat(t.amount || '0'), 0);
  reason = (p.note || '').trim();
  customerId = null;  // not in refund payload; we'll need to look up via order_id or skip enrichment
} else {
  // cancel
  orderId = p.id;
  orderName = p.name || `#${p.order_number || p.id}`;
  orderTotal = p.total_price;
  createdAt = p.cancelled_at || p.updated_at || p.created_at;
  lineItems = (p.line_items || []).map(li => ({
    quantity: li.quantity || 0,
    title: li.title || li.name || 'Unknown',
    variant_title: li.variant_title,
  }));
  reason = p.cancel_reason || '';
  customerId = (p.customer && p.customer.id) ? String(p.customer.id) : null;
}

const adminUrl = orderId ? `https://admin.shopify.com/store/__STORE__/orders/${orderId}` : '';

// Enrich with customer (may not have customer_id for refunds — skip enrichment then)
let customerFullName = '', lifetimeOrders = 0, lifetimeSpent = 0;
if (customerId) {
  const c = customers.find(r => String(r.json.customer_id || '') === customerId);
  if (c) {
    const f = String(c.json.first_name || '').trim();
    const l = String(c.json.last_name || '').trim();
    customerFullName = [f, l].filter(Boolean).join(' ');
    lifetimeOrders = Number(c.json.total_orders || 0);
    lifetimeSpent  = Number(c.json.total_spent || 0);
  }
}

let customerLine = '';
if (customerFullName) {
  customerLine = `\n👤 ${customerFullName}`;
  if (lifetimeOrders > 0) {
    customerLine += ` (${lifetimeOrders} order${lifetimeOrders === 1 ? '' : 's'} lifetime, S$${fmtSGD(lifetimeSpent)} total)`;
  }
}

const items = lineItems.slice(0, 6).map(li => {
  const v = li.variant_title && li.variant_title !== 'Default Title' ? ` - ${li.variant_title}` : '';
  return `• ${li.quantity}x ${li.title}${v}`;
});
const moreCount = Math.max(0, lineItems.length - 6);
const itemsBlock = items.length ? items.join('\n') : '_(no items captured)_';
const moreBlock = moreCount > 0 ? `\n... and ${moreCount} more` : '';

let msg;
if (eventType === 'refund') {
  const refundLine = refundAmount > 0 ? `Refund: *-S$${fmtSGD(refundAmount)}*` : 'Refund: _(see Shopify)_';
  const reasonBlock = reason ? `\n\n📝 Reason: ${reason}` : '';
  const orderLine = orderId ? `Order id: ${orderId}` : '';
  msg = `↩️ *Refund processed*
_${shortTime(createdAt || new Date().toISOString())}_

${orderLine}
${refundLine}${customerLine}

📦 Refunded items
${itemsBlock}${moreBlock}${reasonBlock}

🔗 ${adminUrl}`;
} else {
  const totalLine = orderTotal ? `Amount: *S$${fmtSGD(orderTotal)}*` : '';
  const reasonBlock = reason ? `\n📝 Reason: ${reason}` : '';
  msg = `🚫 *Order cancelled*
_${shortTime(createdAt || new Date().toISOString())}_

Order ${orderName || ('#' + orderId)}
${totalLine}${customerLine}

📦 Items on order
${itemsBlock}${moreBlock}${reasonBlock}

🔗 ${adminUrl}`;
}

return [{ json: {
  message: msg,
  event_type: eventType,
  order_id: orderId,
  customer_name: customerFullName,
  lifetime_orders: lifetimeOrders,
  refund_amount: refundAmount || 0,
} }];
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


def webhook_node(name, pos, path):
    return {
        "parameters": {
            "httpMethod": "POST", "path": path,
            "responseMode": "onReceived", "options": {"rawBody": False},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": pos, "webhookId": path,
    }


def wa_node(name, pos, phone):
    return {
        "parameters": {
            "method": "POST", "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-API-Key", "value": WA_KEY},
            ]},
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "phone_number", "value": phone},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def build():
    refund_hook = webhook_node("Refund Webhook", [0, 200], REFUND_WEBHOOK_PATH)
    cancel_hook = webhook_node("Cancel Webhook", [0, 500], CANCEL_WEBHOOK_PATH)

    tag_refund = code_node("Tag Refund", [240, 200], TAG_REFUND_JS)
    tag_cancel = code_node("Tag Cancel", [240, 500], TAG_CANCEL_JS)

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
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [480, 350],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    format_node = code_node("Format Alert", [720, 350], FORMAT_JS)

    team_sends = [
        wa_node(f"Send Team #{i+1}", [960, 150 + i * 110], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [960, 150 + len(RECIPIENTS) * 110]
    )

    nodes = [refund_hook, cancel_hook, tag_refund, tag_cancel, read_sheet, format_node, *team_sends, telegram_send]

    connections = {
        refund_hook["name"]: {"main": [[{"node": tag_refund["name"], "type": "main", "index": 0}]]},
        cancel_hook["name"]: {"main": [[{"node": tag_cancel["name"], "type": "main", "index": 0}]]},
        tag_refund["name"]:  {"main": [[{"node": read_sheet["name"], "type": "main", "index": 0}]]},
        tag_cancel["name"]:  {"main": [[{"node": read_sheet["name"], "type": "main", "index": 0}]]},
        read_sheet["name"]:  {"main": [[{"node": format_node["name"], "type": "main", "index": 0}]]},
        format_node["name"]: {"main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*team_sends, telegram_send]]]},
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
    out = os.path.expanduser("~/n8n-bonpet/refund_cancel_alert_payload.json")
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

    print()
    print("Shopify webhook URLs to register (run register_shopify_webhooks.py next):")
    print(f"  refunds/create     -> https://n8n.thebonpet.com/webhook/{REFUND_WEBHOOK_PATH}")
    print(f"  orders/cancelled   -> https://n8n.thebonpet.com/webhook/{CANCEL_WEBHOOK_PATH}")

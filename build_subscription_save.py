#!/usr/bin/env python3
"""Subscription Pause/Cancel Save — on Shopify subscription_contracts/update,
when status flips to PAUSED or CANCELLED, WA the customer a save offer and
alert the team on Telegram weslee + WA broadcast.

Triggered by its own Shopify webhook (separate from Subscribers Ingest), so
seed/bulk flows through that other workflow don't fire save messages.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)
from _notify import telegram_send_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Subscription Save - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_PATH = "subscription-save-8c4f2e9a3b"

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
ORDERS_TAB_GID = 0
CUSTOMERS_TAB_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
TEAM_RECIPIENTS = [
    ("Yash",             "+6581394225"),
    ("Nicolas",          "+6598531677"),
    ("Bon Pet official", "+6590108515"),
    ("Rachel",           "+6587993341"),
    ("Shaun",            "+6581114800"),
    ("Bari",             "+6282240119788"),
]


LOOKUP_AND_FORMAT_JS = r"""// Parse Shopify subscription_contract webhook, filter PAUSED/CANCELLED,
// look up customer details from Customers tab, build customer + team messages.
function extractId(v) { if (!v) return ''; const s = String(v).replace(/^'/, ''); const m = s.match(/(\d+)$/); return m ? m[1] : s; }

function normalizePhone(p) {
  if (!p) return '';
  let s = String(p).replace(/\s/g, '').trim();
  if (s.startsWith('+')) {
    const d = s.slice(1).replace(/\D/g, '');
    return d.length >= 8 ? '+' + d : '';
  }
  const digits = s.replace(/\D/g, '');
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  if (digits.length >= 10 && digits.length <= 15) return '+' + digits;
  return '';
}
""" + COOLDOWN_JS_SNIPPET + r"""

const raw = $('Shopify Webhook (subscription_contracts/update)').first().json;
const body = raw.body || raw;
const sub = Array.isArray(body) ? body[0] : body;
if (!sub || typeof sub !== 'object') {
  return [{ json: { should_send: false, skip_reason: 'no payload' } }];
}

const status = String(sub.status || '').toUpperCase();
if (status !== 'PAUSED' && status !== 'CANCELLED') {
  return [{ json: { should_send: false, skip_reason: `status=${status}` } }];
}

const customer   = sub.customer || {};
const email      = String(sub.email || customer.email || '').toLowerCase().trim();
const contractId = extractId(sub.id || sub.contract_id || sub.handle);

if (!email) {
  return [{ json: { should_send: false, skip_reason: 'no email', contract_id: contractId } }];
}

// Lookup customer in Customers tab (for phone + name)
const custRows = $('Read Customers Tab').all();
const cust = custRows.find(r => String(r.json.email || '').toLowerCase().trim() === email);
if (!cust) {
  return [{ json: { should_send: false, skip_reason: 'customer not in DB', email, contract_id: contractId } }];
}
const cj = cust.json;
const firstName = String(cj.first_name || '').trim();
const lastName  = String(cj.last_name  || '').trim();
let phone = normalizePhone(cj.phone || cj.default_address_phone || '');
if (!phone) {
  return [{ json: { should_send: false, skip_reason: 'no phone', email, contract_id: contractId } }];
}
if (isInGlobalCooldown(phone)) {
  return [{ json: { should_send: false, skip_reason: 'global 7d cooldown', email, phone, contract_id: contractId } }];
}

// Aggregate orders for lifetime stats (total_orders, total_spent)
let totalOrders = 0, totalSpent = 0;
try {
  for (const r of $('Read Orders Tab').all()) {
    const o = r.json;
    if (String(o.email || '').toLowerCase().trim() !== email) continue;
    totalOrders += 1;
    totalSpent += parseFloat(String(o.total_price || '0').replace(/[^0-9.]/g, '')) || 0;
  }
} catch(e) {}

const greeting = firstName ? `Hi ${firstName}!` : 'Hi!';

const pauseMsg = `${greeting} 🐾

Yash here. Saw you paused your sub, hope all's good with your furkid 🐾

Take your time. When you're ready, a few things that might help:

✅ Swap to a different protein (new: Pork for dogs 🍖, Duck for cats 🦆)
✅ Stretch your cadence to 6 weeks
✅ Come back with 20% off: *WELCOMEBACK<3THEBONPET* 🎁

Anything on your mind? Just reply, I read every message 💛

❤️ Yash & the Bon Pet team`;

const cancelMsg = `${greeting} 🐾

Yash from The Bon Pet. Saw you cancelled your sub, sorry to see you go 🥺

Mind sharing what changed? We're a tiny SG team and feedback like yours actually shapes what we do next.

If you ever want to give it another shot, here's 20% off for you: *WELCOMEBACK<3THEBONPET* 🎁

Either way, thanks so much for trying us 🙏

❤️ Yash & the Bon Pet team`;

const customerMsg = status === 'PAUSED' ? pauseMsg : cancelMsg;

// Subscription line preview
const firstLine = (sub.line_items && sub.line_items.edges && sub.line_items.edges[0] && sub.line_items.edges[0].node)
                || (sub.lines && sub.lines[0]) || {};
const protein = String(
  sub.line_selling_plan_name
  || (firstLine.selling_plan && firstLine.selling_plan.name)
  || firstLine.title
  || '(plan)'
).trim();
const qty = sub.line_quantity || firstLine.quantity || '';
const bp = sub.billing_policy || {};
const cadence = `${sub.cadence_interval_count || bp.interval_count || ''} ${sub.cadence_interval || bp.interval || ''}`.trim().toLowerCase();
const nextBill = String(sub.upcoming_billing_date || sub.next_billing_date || '').slice(0, 10) || '(n/a)';

const weslee_message =
  `🚨 *Subscription ${status}*\n` +
  `👤 ${firstName} ${lastName} (${email})\n` +
  `📱 ${phone}\n` +
  `📦 ${protein}${qty ? ' x ' + qty : ''}${cadence ? ', every ' + cadence : ''}\n` +
  `📅 Next billing was: ${nextBill}\n` +
  `💰 Lifetime: ${totalOrders} orders, S$${totalSpent.toFixed(2)}\n` +
  `🆔 Contract: ${contractId}`;

return [{
  json: {
    should_send: true,
    customer_phone: phone,
    customer_message: customerMsg,
    weslee_message: weslee_message,
    status: status,
    // wa_sent_log (global) append fields
    phone: phone,
    workflow: 'subscription_save',
    template: status === 'PAUSED' ? 'subscription_save_paused' : 'subscription_save_cancelled',
    sent_at: new Date().toISOString(),
    order_id: contractId,
    notes: `status=${status}, email=${email}`,
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


def gs_read_node(name, pos, gid):
    return {
        "parameters": {
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName":  {"__rl": True, "value": gid, "mode": "list",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={gid}"},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": pos,
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
        "executeOnce": True,
        "alwaysOutputData": True,
    }


def team_wa_node(name, pos, phone):
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
                {"name": "message", "value": "={{ $json.weslee_message }}"},
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
            "httpMethod": "POST", "path": WEBHOOK_PATH,
            "responseMode": "onReceived", "options": {"rawBody": False},
        },
        "id": uid(), "name": "Shopify Webhook (subscription_contracts/update)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_PATH,
    }

    read_customers = gs_read_node("Read Customers Tab", [240, 200], CUSTOMERS_TAB_GID)
    read_orders    = gs_read_node("Read Orders Tab",    [240, 400], ORDERS_TAB_GID)
    read_global    = read_global_sent_log_node([240, 600])

    merge = {
        "parameters": {"numberInputs": 3},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [480, 300],
    }

    lookup_format = code_node("Lookup + Format", [720, 300], LOOKUP_AND_FORMAT_JS)

    should_send_if = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.should_send }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Should Send?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.3,
        "position": [960, 300],
    }

    send_customer = {
        "parameters": {
            "method": "POST", "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-API-Key", "value": WA_KEY},
            ]},
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "phone_number", "value": "={{ $json.customer_phone }}"},
                {"name": "message", "value": "={{ $json.customer_message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Send Customer WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1200, 200],
    }

    send_telegram = telegram_send_node("Send Telegram Weslee", [1200, 400],
                                       message_expr="={{ $json.weslee_message }}")

    team_wa_sends = [
        team_wa_node(f"Team WA {name}", [1200, 600 + i * 100], phone)
        for i, (name, phone) in enumerate(TEAM_RECIPIENTS)
    ]

    log_global = append_global_sent_log_node([1440, 200])

    nodes = [trigger, read_customers, read_orders, read_global, merge,
             lookup_format, should_send_if, send_customer, send_telegram,
             *team_wa_sends, log_global]

    connections = {
        trigger["name"]: {"main": [[
            {"node": read_customers["name"], "type": "main", "index": 0},
            {"node": read_orders["name"],    "type": "main", "index": 0},
            {"node": read_global["name"],    "type": "main", "index": 0},
        ]]},
        read_customers["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_orders["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_global["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        merge["name"]:          {"main": [[{"node": lookup_format["name"], "type": "main", "index": 0}]]},
        lookup_format["name"]:  {"main": [[{"node": should_send_if["name"], "type": "main", "index": 0}]]},
        should_send_if["name"]: {"main": [
            [
                {"node": send_customer["name"], "type": "main", "index": 0},
                {"node": send_telegram["name"], "type": "main", "index": 0},
                *[{"node": n["name"], "type": "main", "index": 0} for n in team_wa_sends],
            ],
            [],
        ]},
        send_customer["name"]: {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
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
    out = os.path.expanduser("~/n8n-bonpet/subscription_save_payload.json")
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
    print(f"Webhook URL to register in Shopify admin:")
    print(f"  Topic:   subscription_contracts/update")
    print(f"  Address: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print()
    print(f"Dedup: wa_sent_log 7-day cooldown (template=subscription_save_paused|cancelled)")

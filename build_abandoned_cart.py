#!/usr/bin/env python3
"""Abandoned Cart Recovery — Shopify checkouts/create webhook → wait 3h →
check Checkouts tab for completion → if still abandoned, WA the customer
with the cart_link (founder tone, one-shot per checkout event).
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _sent_log import (
import subprocess
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Abandoned Cart Recovery - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_PATH = "abandoned-cart-recovery-2b7f4c9e8a"

# How long to wait after checkout creation before checking for abandonment
WAIT_HOURS = 3

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CHECKOUTS_TAB_GID = 400400
CUSTOMERS_TAB_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
# Team alerts for recoveries are daily-batched via Morning Briefing — no per-event
# team WA / Telegram on this workflow. wa_sent_log entries (workflow='abandoned_cart')
# are the source of truth for the digest.

EXTRACT_TOKEN_JS = r"""// Stash the checkout token from the webhook payload so we can find the row later
const p = $input.first().json;
const body = p.body || p;
return [{
  json: {
    checkout_token: body.token || body.checkout_token || '',
    checkout_id: String(body.id || ''),
    created_at: body.created_at || '',
  }
}];
"""

# The checkouts tab write from Shopify ingest happens continuously. We fetch phone/name
# from the checkouts tab row. Fall back to Customers tab by customer_id if needed.
LOOKUP_AND_FORMAT_JS = r"""// After 3h wait, check if the checkout still looks abandoned and build the WA message
function tryRead(nodeName) {
  try { return $(nodeName).all(); } catch (e) { return []; }
}

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

const stashed = tryRead('Stash Token');
if (!stashed.length) {
  return [{ json: { should_send: false, skip_reason: 'no stashed context' } }];
}
const token = stashed[0].json.checkout_token;
const checkoutId = stashed[0].json.checkout_id;

const checkoutRows = $('Read Checkouts Tab').all();
const customers = $('Read Customers Tab').all();

// Match by token (preferred) or checkout_id
let row = checkoutRows.find(r => String(r.json.checkout_token || '') === token && token);
if (!row) row = checkoutRows.find(r => String(r.json.checkout_id || '') === checkoutId && checkoutId);

if (!row) {
  return [{ json: { should_send: false, skip_reason: 'checkout not found in sheet yet' } }];
}
const ck = row.json;

// Already converted? Skip.
if (ck.completed_at && String(ck.completed_at).trim()) {
  return [{ json: { should_send: false, skip_reason: 'checkout completed', checkout_token: token } }];
}

// Need a phone to send
let phone = String(ck.phone || '').trim();
let firstName = String(ck.first_name || '').trim();

// Fall back to Customers tab via customer_id
if ((!phone || !firstName) && ck.customer_id) {
  const c = customers.find(r => String(r.json.customer_id || '') === String(ck.customer_id));
  if (c) {
    if (!phone) phone = String(c.json.phone || '').trim();
    if (!firstName) firstName = String(c.json.first_name || '').trim();
  }
}

if (!phone) {
  return [{ json: { should_send: false, skip_reason: 'no phone found', checkout_token: token } }];
}
phone = normalizePhone(phone) || phone;
if (!phone.startsWith('+')) phone = '+' + phone.replace(/[^\d]/g, '');

// Global 7-day cooldown (spam prevention across workflows)
if (isInGlobalCooldown(phone)) {
  return [{ json: { should_send: false, skip_reason: 'global 7d cooldown', checkout_token: token } }];
}

// Cart link: prefer abandoned_checkout_url (Shopify provides this on checkout payload)
const cartUrl = String(ck.abandoned_checkout_url || '').trim();
if (!cartUrl) {
  return [{ json: { should_send: false, skip_reason: 'no cart link', checkout_token: token } }];
}

// Build items preview
let itemsLine = '';
try {
  const items = JSON.parse(ck.line_items_json || '[]');
  if (items.length) {
    const summary = items.slice(0, 3).map(li => `${li.quantity || 1}x ${li.title}`).join(', ');
    const more = items.length > 3 ? `, +${items.length - 3} more` : '';
    itemsLine = `\nYour cart: ${summary}${more}`;
  }
} catch (e) { /* ignore */ }

const greeting = firstName ? `Hi ${firstName}!` : 'Hi!';

const msg = `${greeting} 🐾

Yash here from The Bon Pet. Noticed you got partway through checking out earlier but didn't quite make it to the finish line.

Your cart's still ready to go, no hassle:
🛒 ${cartUrl}${itemsLine}

Anything holding you back? Just reply here, always happy to help.

❤️ Yash & the Bon Pet team`;

return [{
  json: {
    should_send: true,
    customer_phone: phone,
    customer_name: firstName,
    message: msg,
    checkout_token: token,
    cart_url: cartUrl,
    // wa_sent_log (global) append fields
    phone: phone,
    workflow: 'abandoned_cart',
    template: 'recovery_3h',
    sent_at: new Date().toISOString(),
    order_id: token,
    notes: 'first_name=' + (firstName || ''),
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


def build():
    trigger = {
        "parameters": {
            "httpMethod": "POST", "path": WEBHOOK_PATH,
            "responseMode": "onReceived", "options": {"rawBody": False},
        },
        "id": uid(), "name": "Shopify Webhook (checkouts/create)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_PATH,
    }

    stash = code_node("Stash Token", [240, 300], EXTRACT_TOKEN_JS)

    wait = {
        "parameters": {
            "amount": WAIT_HOURS,
            "unit": "hours",
        },
        "id": uid(), "name": f"Wait {WAIT_HOURS}h",
        "type": "n8n-nodes-base.wait", "typeVersion": 1.1,
        "position": [480, 300],
        "webhookId": str(uuid.uuid4()),
    }

    read_checkouts = {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": CHECKOUTS_TAB_GID, "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={CHECKOUTS_TAB_GID}",
            },
            "options": {},
        },
        "id": uid(), "name": "Read Checkouts Tab",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [720, 200],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    read_customers = {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": CUSTOMERS_TAB_GID, "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={CUSTOMERS_TAB_GID}",
            },
            "options": {},
        },
        "id": uid(), "name": "Read Customers Tab",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [720, 400],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    read_global = read_global_sent_log_node([720, 600])

    merge = {
        "parameters": {"numberInputs": 3},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [960, 300],
    }

    lookup_format = code_node("Lookup + Format", [1200, 300], LOOKUP_AND_FORMAT_JS)

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
        "position": [1440, 300],
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
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Send Recovery WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1680, 300],
    }

    log_global = append_global_sent_log_node([1920, 300])

    nodes = [trigger, stash, wait, read_checkouts, read_customers, read_global, merge,
             lookup_format, should_send_if, send_customer, log_global]

    connections = {
        trigger["name"]:        {"main": [[{"node": stash["name"], "type": "main", "index": 0}]]},
        stash["name"]:          {"main": [[{"node": wait["name"], "type": "main", "index": 0}]]},
        wait["name"]: {
            "main": [[
                {"node": read_checkouts["name"], "type": "main", "index": 0},
                {"node": read_customers["name"], "type": "main", "index": 0},
                {"node": read_global["name"], "type": "main", "index": 0},
            ]]
        },
        read_checkouts["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_customers["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_global["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        merge["name"]:          {"main": [[{"node": lookup_format["name"], "type": "main", "index": 0}]]},
        lookup_format["name"]:  {"main": [[{"node": should_send_if["name"], "type": "main", "index": 0}]]},
        should_send_if["name"]: {"main": [
            [{"node": send_customer["name"], "type": "main", "index": 0}],
            [],
        ]},
        send_customer["name"]:  {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
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
    out = os.path.expanduser("~/n8n-bonpet/abandoned_cart_payload.json")
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
    print("Shopify webhook to register (add to register_shopify_webhooks.py):")
    print(f"  checkouts/create -> https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print()
    print(f"Wait duration: {WAIT_HOURS}h (edit WAIT_HOURS + rerun to change)")

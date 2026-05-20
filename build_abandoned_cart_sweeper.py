#!/usr/bin/env python3
"""Abandoned Cart Sweeper — hourly cron replacement for the Wait-3h workflow.

Replaces the previous webhook+Wait+lookup flow (which OOM-killed n8n
self-hosted by holding every paused execution in process memory for 3 hours).

Architecture:
  Schedule (hourly :17) ┐
                        ├→ Read Checkouts → Read Customers (executeOnce)
  Manual Webhook       ┘    → Read Global Sent Log (executeOnce)
                              → Compute Candidates → Send WA → Skip Header
                                → Log Global Sent

Selection rule (Compute Candidates):
  - row.created_at is between 3h and 6h ago (catches anything missed since last run)
  - row.completed_at is empty (still abandoned)
  - row.checkout_token NOT already in wa_sent_log (workflow='abandoned_cart')
  - phone resolvable (fallback to Customers tab by customer_id)
  - phone not in 7-day global cooldown (cross-workflow spam prevention)

Cron fires :17 not :00 to avoid the OOM-prone hour-boundary cluster.
"""
import json, uuid, os, subprocess, urllib.request, urllib.error
from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)
from _blacklist import BLACKLIST_JS_SNIPPET

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Abandoned Cart Sweeper - WhatsApp"
TEAM = "i1GSXBntwNvNqic8"
WEBHOOK_PATH = "trigger-abandoned-cart-sweep"

GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
CHECKOUTS_GID = 400400
CUSTOMERS_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]
).decode().strip()

WINDOW_START_HOURS = 3   # don't message until 3h after checkout
WINDOW_END_HOURS = 6     # stop messaging after 6h (older = stale, sweeper missed it)
SEND_WA_DISABLED = False


COMPUTE_JS = r"""// Abandoned Cart Sweeper — find checkouts aged 3-6h that didn't complete and haven't been WA'd.
const DRY_RUN = false;
const WINDOW_START_HOURS = """ + str(WINDOW_START_HOURS) + r""";
const WINDOW_END_HOURS = """ + str(WINDOW_END_HOURS) + r""";

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
""" + COOLDOWN_JS_SNIPPET + BLACKLIST_JS_SNIPPET + r"""

const checkouts = $('Read Checkouts').all().map(it => it.json);
const customers = $('Read Customers').all().map(it => it.json);

// Customer fallback lookup by customer_id
const customerById = new Map();
for (const c of customers) {
  if (c.customer_id) customerById.set(String(c.customer_id), c);
}

// Already-sent dedup: wa_sent_log entries where workflow='abandoned_cart',
// keyed by order_id (which stores the checkout_token).
const ALREADY_SENT_TOKENS = new Set();
for (const it of $('Read Global Sent Log').all()) {
  const s = it.json;
  if (String(s.workflow || '').toLowerCase() === 'abandoned_cart' && s.order_id) {
    ALREADY_SENT_TOKENS.add(String(s.order_id));
  }
}

const now = Date.now();
const windowStartMs = WINDOW_START_HOURS * 3600 * 1000;
const windowEndMs = WINDOW_END_HOURS * 3600 * 1000;

const stats = {
  total_checkouts: checkouts.length,
  outside_window: 0,
  already_completed: 0,
  already_sent: 0,
  no_phone: 0,
  blacklisted: 0,
  global_cooldown: 0,
  no_cart_link: 0,
  candidates: 0,
};

const candidates = [];

for (const ck of checkouts) {
  const createdAt = Date.parse(ck.created_at || '');
  if (!createdAt) { stats.outside_window++; continue; }
  const age = now - createdAt;
  if (age < windowStartMs || age > windowEndMs) { stats.outside_window++; continue; }

  if (String(ck.completed_at || '').trim()) { stats.already_completed++; continue; }

  const token = String(ck.checkout_token || '');
  if (!token) { stats.outside_window++; continue; }
  if (ALREADY_SENT_TOKENS.has(token)) { stats.already_sent++; continue; }

  // Phone resolution
  let phone = String(ck.phone || '').trim();
  let firstName = String(ck.first_name || '').trim();
  if ((!phone || !firstName) && ck.customer_id) {
    const c = customerById.get(String(ck.customer_id));
    if (c) {
      if (!phone) phone = String(c.phone || '').trim();
      if (!firstName) firstName = String(c.first_name || '').trim();
    }
  }
  if (!phone) { stats.no_phone++; continue; }
  phone = normalizePhone(phone) || phone;
  if (!phone.startsWith('+')) phone = '+' + phone.replace(/[^\d]/g, '');

  if (isBlacklisted(phone)) { stats.blacklisted++; continue; }
  if (isInGlobalCooldown(phone)) { stats.global_cooldown++; continue; }

  const cartUrl = String(ck.abandoned_checkout_url || '').trim();
  if (!cartUrl) { stats.no_cart_link++; continue; }

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

  candidates.push({
    target_phone: DRY_RUN ? '+6581394225' : phone,
    message: msg,
    // wa_sent_log fields (autoMap to columns: phone, workflow, template, sent_at, order_id, notes)
    phone: phone,
    workflow: 'abandoned_cart',
    template: 'recovery_3h_sweeper',
    sent_at: new Date().toISOString(),
    order_id: token,
    notes: 'first_name=' + (firstName || '') + ';age_h=' + (age / 3600000).toFixed(2),
  });
  stats.candidates++;
}

if (!candidates.length) {
  // Emit a no-op header item so the downstream Send WA gets at least one input
  // but is_header=true → Skip Header filters it out before Log Global Sent.
  return [{ json: { is_header: true, target_phone: '', message: '', stats: stats } }];
}

// Prepend a diagnostic header item (filtered before Send WA + Log)
candidates.unshift({ is_header: true, target_phone: '', message: '', stats: stats });
return candidates.map(c => ({ json: c }));
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


def schedule_node():
    return {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "17 * * * *"}]}
        },
        "id": uid(), "name": "Hourly :17",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 100],
    }


def webhook_node():
    return {
        "parameters": {
            "httpMethod": "POST", "path": WEBHOOK_PATH,
            "responseMode": "onReceived", "options": {},
        },
        "id": uid(), "name": "Manual Trigger Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": uid(),
    }


def gs_read_node(name, tab_gid, tab_name, position):
    return {
        "parameters": {
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": tab_gid, "mode": "list",
                          "cachedResultName": tab_name,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={tab_gid}"},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


def compute_node():
    return {
        "parameters": {"jsCode": COMPUTE_JS},
        "id": uid(), "name": "Compute Candidates",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [960, 300],
    }


def send_wa_node():
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
                {"name": "phone_number", "value": "={{ $json.target_phone }}"},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {
                "batching": {"batch": {"batchSize": 1, "batchInterval": 5000}},
            },
        },
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1200, 300],
        "onError": "continueRegularOutput",
        "disabled": SEND_WA_DISABLED,
    }


def skip_header_node():
    # Reach back to Compute so phone/log fields survive HTTP response replacement.
    # See feedback_n8n_http_input_passthrough memory.
    js = "return $('Compute Candidates').all().filter(it => !it.json.is_header);"
    return {
        "parameters": {"jsCode": js},
        "id": uid(), "name": "Skip Header",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1440, 300],
    }


schedule = schedule_node()
webhook = webhook_node()
read_checkouts = gs_read_node("Read Checkouts", CHECKOUTS_GID, "checkouts", [240, 200])
read_customers = gs_read_node("Read Customers", CUSTOMERS_GID, "customers", [480, 200])
read_customers["executeOnce"] = True
read_global = read_global_sent_log_node([720, 200])
compute = compute_node()
send_wa = send_wa_node()
skip_header = skip_header_node()
log_global = append_global_sent_log_node([1680, 300])

nodes = [schedule, webhook, read_checkouts, read_customers, read_global,
         compute, send_wa, skip_header, log_global]

connections = {
    schedule["name"]:       {"main": [[{"node": read_checkouts["name"], "type": "main", "index": 0}]]},
    webhook["name"]:        {"main": [[{"node": read_checkouts["name"], "type": "main", "index": 0}]]},
    read_checkouts["name"]: {"main": [[{"node": read_customers["name"], "type": "main", "index": 0}]]},
    read_customers["name"]: {"main": [[{"node": read_global["name"], "type": "main", "index": 0}]]},
    read_global["name"]:    {"main": [[{"node": compute["name"], "type": "main", "index": 0}]]},
    compute["name"]:        {"main": [[{"node": send_wa["name"], "type": "main", "index": 0}]]},
    send_wa["name"]:        {"main": [[{"node": skip_header["name"], "type": "main", "index": 0}]]},
    skip_header["name"]:    {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
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
    out = os.path.expanduser("~/n8n-bonpet/abandoned_cart_sweeper_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built sweeper payload: {len(nodes)} nodes → {out}")

    existing_id = find_existing()
    if existing_id:
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
        print(f"PUT existing {new_id} → HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {new_id} → HTTP {status}")
        print(body if status >= 300 else "")

    if new_id and status < 300:
        s, _ = http("POST", f"/workflows/{new_id}/activate")
        print(f"Activate HTTP {s}")
        print(f"\nWorkflow URL: https://n8n.thebonpet.com/workflow/{new_id}")
        print(f"Manual webhook: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

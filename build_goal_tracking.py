#!/usr/bin/env python3
"""Build the Goal Tracking workflow and deploy to n8n (team project)."""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Goal Tracking - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "goal-tracking-manual-6b3d7f2e5c"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6581114800",  # Shaun
]

# Tiers — change here + re-run builder to adjust
TARGET_FLOOR   = 6500
TARGET_PRIMARY = 8500
TARGET_STRETCH = 11000

DATE_RANGES_JS = r"""// Compute current-month SGT boundaries + today/days info
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);

const y = sgtNow.getUTCFullYear();
const m = sgtNow.getUTCMonth();
const dToday = sgtNow.getUTCDate();

const monthStart = Date.UTC(y, m,     1) - SGT_OFFSET_MS;
const monthEnd   = Date.UTC(y, m + 1, 1) - SGT_OFFSET_MS;
const daysInMonth = Math.round((monthEnd - monthStart) / (24 * 60 * 60 * 1000));
const daysElapsed = dToday;         // today counts toward elapsed
const daysRemaining = daysInMonth - daysElapsed;

const monthLabel = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Singapore', month: 'long', year: 'numeric'
}).format(new Date(monthStart + SGT_OFFSET_MS));

return [{
  json: {
    fetch_start:     new Date(monthStart).toISOString(),
    fetch_end:       new Date(now.getTime()).toISOString(),  // up to "now"
    month_label:     monthLabel,
    days_in_month:   daysInMonth,
    days_elapsed:    daysElapsed,
    days_remaining:  daysRemaining,
  }
}];
"""

AGGREGATE_JS = r"""// Aggregate MTD revenue + build tiered progress message
const ranges = $('Set Date Ranges').first().json;
const orders = $('Fetch Orders').all()
  .flatMap(it => it.json.orders || [it.json])
  .filter(o => o && o.id);

const TARGET_FLOOR   = __FLOOR__;
const TARGET_PRIMARY = __PRIMARY__;
const TARGET_STRETCH = __STRETCH__;

const wStart = new Date(ranges.fetch_start).getTime();
const wEnd   = new Date(ranges.fetch_end).getTime();

let revenue = 0, orderCount = 0;
for (const o of orders) {
  const t = new Date(o.created_at).getTime();
  if (t < wStart || t > wEnd) continue;
  if (o.cancelled_at) continue;
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  revenue += parseFloat(o.total_price || '0');
  orderCount += 1;
}

const daysElapsed   = ranges.days_elapsed;
const daysInMonth   = ranges.days_in_month;
const daysRemaining = ranges.days_remaining;
const runRate = daysElapsed > 0 ? (revenue / daysElapsed) * daysInMonth : 0;

function progressBar(pct, width) {
  const capped = Math.max(0, Math.min(100, pct));
  const filled = Math.round((capped / 100) * width);
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}

function pctOf(rev, target) {
  return target > 0 ? Math.round((rev / target) * 100) : 0;
}

function fmtSGD(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0}); }
function fmtK(n) {
  if (n >= 1000) {
    const k = n / 1000;
    return Number.isInteger(k) ? `${k}K` : `${k.toFixed(1)}K`;
  }
  return String(n);
}

const pFloor   = pctOf(revenue, TARGET_FLOOR);
const pTarget  = pctOf(revenue, TARGET_PRIMARY);
const pStretch = pctOf(revenue, TARGET_STRETCH);

const needForTarget = Math.max(0, TARGET_PRIMARY - revenue);
const perDayToTarget = daysRemaining > 0 ? needForTarget / daysRemaining : needForTarget;

// On-track indicator: is run rate >= target?
let statusLine;
if (runRate >= TARGET_STRETCH) {
  statusLine = '💎 *On track for Stretch!*';
} else if (runRate >= TARGET_PRIMARY) {
  statusLine = '🚀 *On track for Target.*';
} else if (runRate >= TARGET_FLOOR) {
  statusLine = '🎯 *On track for Floor. Push for Target.*';
} else {
  statusLine = '⚠️ *Below Floor run rate.*';
}

const bar = (pct) => progressBar(pct, 10);

const msg = `🎯 *Bon Pet Goal Tracking*
_${ranges.month_label} — Day ${daysElapsed} of ${daysInMonth}_

📊 *Month-to-date*
Revenue: S$${fmtSGD(revenue)} (${orderCount} order${orderCount === 1 ? '' : 's'})
Run rate: S$${fmtSGD(runRate)}/mo

*Target progress*
Floor   S$${fmtK(TARGET_FLOOR)}  ${bar(pFloor)}  ${pFloor}%
Target  S$${fmtK(TARGET_PRIMARY)}  ${bar(pTarget)}  ${pTarget}% 🎯
Stretch S$${fmtK(TARGET_STRETCH)}  ${bar(pStretch)}  ${pStretch}%

${statusLine}
_${daysRemaining} day${daysRemaining === 1 ? '' : 's'} left · need S$${fmtSGD(perDayToTarget)}/day to hit Target_`;

return [{
  json: {
    message: msg,
    revenue,
    order_count: orderCount,
    run_rate: runRate,
    pct_floor: pFloor,
    pct_target: pTarget,
    pct_stretch: pStretch,
    days_remaining: daysRemaining,
  }
}];
""".replace("__FLOOR__", str(TARGET_FLOOR)) \
   .replace("__PRIMARY__", str(TARGET_PRIMARY)) \
   .replace("__STRETCH__", str(TARGET_STRETCH))


def uid():
    return str(uuid.uuid4())


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
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def shopify_node(name, pos, url_expr):
    return {
        "parameters": {
            "url": url_expr,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "credentials": {
            "shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}
        },
    }


def code_node(name, pos, js):
    return {
        "parameters": {"jsCode": js},
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
    }


def whatsapp_node(name, pos, phone):
    return {
        "parameters": {
            "method": "POST",
            "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "X-API-Key", "value": WA_KEY},
                ]
            },
            "sendBody": True,
            "bodyParameters": {
                "parameters": [
                    {"name": "phone_number", "value": phone},
                    {"name": "message", "value": "={{ $json.message }}"},
                ]
            },
            "options": {},
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
    }


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "5 9 * * *"}]}
        },
        "id": uid(),
        "name": "Daily 9:05 AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.3,
        "position": [0, 400],
    }

    manual = {
        "parameters": {
            "httpMethod": "POST",
            "path": MANUAL_WEBHOOK_ID,
            "responseMode": "onReceived",
            "options": {},
        },
        "id": uid(),
        "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 200],
        "webhookId": MANUAL_WEBHOOK_ID,
    }

    set_dates = code_node("Set Date Ranges", [240, 400], DATE_RANGES_JS)

    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}"
    fetch_orders = shopify_node(
        "Fetch Orders", [480, 400],
        "=" + base + "/orders.json?status=any&financial_status=paid"
        "&created_at_min={{ $json.fetch_start }}"
        "&created_at_max={{ $json.fetch_end }}"
        "&limit=250&fields=id,cancelled_at,financial_status,created_at,total_price"
    )

    aggregate = code_node("Aggregate & Format", [720, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [960, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [960, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, aggregate, *wa_sends, telegram_send]
    connections = {
        schedule["name"]:       {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        manual["name"]:         {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        set_dates["name"]:      {"main": [[{"node": fetch_orders["name"], "type": "main", "index": 0}]]},
        fetch_orders["name"]:   {"main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]},
        aggregate["name"]:      {"main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*wa_sends, telegram_send]]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def find_existing():
    status, data = http("GET", "/workflows?limit=250")
    if status >= 300:
        return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME:
            return wf["id"]
    return None


if __name__ == "__main__":
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/goal_tracking_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes → {out}")

    existing_id = find_existing()
    if existing_id:
        print(f"Found existing: {existing_id} — PUT")
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
    else:
        print("Creating new — POST")
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
    print(f"HTTP {status}")
    if isinstance(body, dict):
        print(json.dumps({k: body[k] for k in ("id", "name", "active") if k in body}, indent=2))

    if new_id and status < 300:
        status_t, body_t = http(
            "PUT", f"/workflows/{new_id}/transfer",
            {"destinationProjectId": TEAM_PROJECT_ID},
        )
        if status_t < 300:
            print(f"Transferred to team project {TEAM_PROJECT_ID}")
        elif status_t == 400 and "already" in str(body_t).lower():
            print("Already in team project")
        else:
            print(f"Transfer HTTP {status_t}: {body_t}")

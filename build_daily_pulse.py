#!/usr/bin/env python3
"""Build the Daily Pulse workflow JSON and POST it to n8n as a new inactive workflow.

Idempotent-ish: if a workflow named 'Daily Pulse - WhatsApp' already exists, we PUT-update it.
Otherwise, POST-create. Workflow stays inactive until you activate in UI.
"""
import json
import uuid
import os
import urllib.request

from _notify import telegram_send_node
import urllib.error
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Daily Pulse - WhatsApp"

# n8n Cloud quirk: POST /workflows always lands in the caller's personal project.
# We need it in the team project so it can reach shared credentials (Shopify etc).
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"  # "The Bon Pet" team project

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
# Team broadcast list — matches Weekly & Monthly Sales Report + Picklist Not Sent
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
]

DATE_RANGES_JS = r"""// Compute SGT date ranges and labels for Daily Pulse
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY = 24 * 60 * 60 * 1000;

// SGT midnight of today (returned as a UTC timestamp)
const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);
const sgtMidnightToday = Date.UTC(
  sgtNow.getUTCFullYear(), sgtNow.getUTCMonth(), sgtNow.getUTCDate()
) - SGT_OFFSET_MS;

const yesterdayStart  = new Date(sgtMidnightToday - DAY);
const yesterdayEnd    = new Date(sgtMidnightToday);
const prevDayStart    = new Date(sgtMidnightToday - 2 * DAY);
const prevDayEnd      = yesterdayStart;
const prevWeekStart   = new Date(sgtMidnightToday - 8 * DAY);
const prevWeekEnd     = new Date(sgtMidnightToday - 7 * DAY);
const openOrderCutoff = new Date(now.getTime() - DAY);

const dateFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
});
const wdFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Singapore', weekday: 'short'
});

return [{
  json: {
    yesterday_start:   yesterdayStart.toISOString(),
    yesterday_end:     yesterdayEnd.toISOString(),
    prev_day_start:    prevDayStart.toISOString(),
    prev_day_end:      prevDayEnd.toISOString(),
    prev_week_start:   prevWeekStart.toISOString(),
    prev_week_end:     prevWeekEnd.toISOString(),
    wide_fetch_start:  prevWeekStart.toISOString(),
    wide_fetch_end:    yesterdayEnd.toISOString(),
    open_order_cutoff: openOrderCutoff.toISOString(),
    formatted_date:    dateFmt.format(yesterdayStart),
    prev_day_label:    wdFmt.format(prevDayStart),
    prev_week_label:   wdFmt.format(yesterdayStart),  // same weekday as yesterday
  }
}];
"""

AGGREGATE_JS = r"""// Aggregate orders / open orders / refunds into a single WhatsApp message
const ranges = $('Set Date Ranges').first().json;

function extractOrders(nodeName) {
  try {
    return $(nodeName).all().flatMap(it => it.json.orders || [it.json]).filter(o => o && o.id);
  } catch (e) {
    return [];
  }
}

const orders       = extractOrders('Fetch Orders');
const openOrders   = extractOrders('Fetch Open Orders');
const refundOrders = extractOrders('Fetch Refunds');

function parseIso(s) { return new Date(s).getTime(); }
function fmtSGD(n)   { return n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function pctStr(curr, prior) {
  if (!prior || prior === 0) return { text: '—', emoji: '' };
  const p = Math.round(((curr - prior) / prior) * 100);
  const sign = p > 0 ? '+' : (p === 0 ? '' : '');
  const emoji = p > 0 ? '📈' : (p < 0 ? '📉' : '➡️');
  return { text: `${sign}${p}%`, emoji };
}

const yStart  = parseIso(ranges.yesterday_start);
const yEnd    = parseIso(ranges.yesterday_end);
const pdStart = parseIso(ranges.prev_day_start);
const pdEnd   = parseIso(ranges.prev_day_end);
const pwStart = parseIso(ranges.prev_week_start);
const pwEnd   = parseIso(ranges.prev_week_end);

let revY = 0, cntY = 0, revPD = 0, cntPD = 0, revPW = 0, cntPW = 0, newCust = 0;
for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  const t = parseIso(o.created_at);
  const total = parseFloat(o.total_price || '0');
  if (t >= yStart && t < yEnd) {
    revY += total; cntY++;
    if (o.customer && Number(o.customer.orders_count) === 1) newCust++;
  } else if (t >= pdStart && t < pdEnd) {
    revPD += total; cntPD++;
  } else if (t >= pwStart && t < pwEnd) {
    revPW += total; cntPW++;
  }
}

// Open orders > 24h
let openCount = 0;
let oldestMs = null;
const cutoff = parseIso(ranges.open_order_cutoff);
for (const o of openOrders) {
  const t = parseIso(o.created_at);
  if (t < cutoff) {
    openCount++;
    if (oldestMs === null || t < oldestMs) oldestMs = t;
  }
}
const oldestAgeDays = oldestMs === null ? null
  : Math.max(1, Math.floor((Date.now() - oldestMs) / (24 * 60 * 60 * 1000)));

// Refunds yesterday
let refundCount = 0, refundTotal = 0;
for (const o of refundOrders) {
  const fs = o.financial_status;
  if (fs !== 'refunded' && fs !== 'partially_refunded') continue;
  const upd = parseIso(o.updated_at);
  if (upd < yStart || upd >= yEnd) continue;
  const refunds = Array.isArray(o.refunds) ? o.refunds : [];
  let orderRefundAmount = 0;
  for (const r of refunds) {
    const rAt = parseIso(r.created_at || r.processed_at || '');
    if (rAt >= yStart && rAt < yEnd) {
      for (const tx of (r.transactions || [])) {
        if (tx.kind === 'refund' && tx.status === 'success') {
          orderRefundAmount += parseFloat(tx.amount || '0');
        }
      }
    }
  }
  if (orderRefundAmount > 0) {
    refundCount++;
    refundTotal += orderRefundAmount;
  }
}

const dod = pctStr(revY, revPD);
const wow = pctStr(revY, revPW);

const pad = (s, w) => s.padEnd(w, ' ');
const dodLine = `vs ${ranges.prev_day_label}:`;
const wowLine = `vs last ${ranges.prev_week_label}:`;
const width   = Math.max(dodLine.length, wowLine.length);

const msg = `🐾 *Bon Pet Daily Pulse*
_${ranges.formatted_date}_

💰 *Yesterday*
Revenue: S$${fmtSGD(revY)} (${cntY} order${cntY === 1 ? '' : 's'})
${pad(dodLine, width)} ${dod.text} ${dod.emoji}
${pad(wowLine, width)} ${wow.text} ${wow.emoji}

👥 *New customers*
${newCust} first-time buyer${newCust === 1 ? '' : 's'}

📦 *Open orders >24h*
${openCount} unfulfilled${oldestAgeDays ? ` (oldest: ${oldestAgeDays}d)` : ''}

↩️ *Refunds / cancels*
${refundCount} refund${refundCount === 1 ? '' : 's'}${refundTotal > 0 ? ` (-S$${fmtSGD(refundTotal)})` : ''}`;

return [{ json: { message: msg, revenue: revY, order_count: cntY, new_customers: newCust, open_orders: openCount, refund_count: refundCount, refund_total: refundTotal } }];
"""


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
            "shopifyAccessTokenApi": {
                "id": SHOPIFY_CRED_ID,
                "name": SHOPIFY_CRED_NAME,
            }
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


def merge_node(name, pos, n_inputs):
    return {
        "parameters": {
            "numberInputs": n_inputs,
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.merge",
        "typeVersion": 3,
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


MANUAL_WEBHOOK_ID = "daily-pulse-manual-7c3f8b2e1a"


def build():
    # --- Nodes ---
    schedule = {
        "parameters": {"rule": {"interval": [{"triggerAtHour": 9}]}},
        "id": uid(),
        "name": "Daily 9AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
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
        "Fetch Orders", [480, 200],
        "=" + base + "/orders.json?status=any&financial_status=paid"
        "&created_at_min={{ $json.wide_fetch_start }}"
        "&created_at_max={{ $json.wide_fetch_end }}"
        "&limit=250&fields=id,total_price,created_at,customer,financial_status"
    )
    fetch_open = shopify_node(
        "Fetch Open Orders", [480, 400],
        "=" + base + "/orders.json?status=open&fulfillment_status=unshipped"
        "&created_at_max={{ $json.open_order_cutoff }}"
        "&limit=250&fields=id,created_at,name,fulfillment_status"
    )
    fetch_refunds = shopify_node(
        "Fetch Refunds", [480, 600],
        "=" + base + "/orders.json?status=any"
        "&updated_at_min={{ $json.yesterday_start }}"
        "&updated_at_max={{ $json.yesterday_end }}"
        "&limit=250&fields=id,total_price,refunds,updated_at,financial_status"
    )

    merge = merge_node("Merge Fetches", [720, 400], 3)
    aggregate = code_node("Aggregate & Format", [960, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [1200, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1200, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, fetch_open, fetch_refunds, merge, aggregate, *wa_sends, telegram_send]

    # --- Connections ---
    # Merge node has 3 declared input ports — forces n8n to wait for all 3 fetches
    # to complete before running Aggregate. Fan-out-into-single-target doesn't give
    # that guarantee under executionOrder v1.
    connections = {
        schedule["name"]: {
            "main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]
        },
        manual["name"]: {
            "main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]
        },
        set_dates["name"]: {
            "main": [[
                {"node": fetch_orders["name"], "type": "main", "index": 0},
                {"node": fetch_open["name"],   "type": "main", "index": 0},
                {"node": fetch_refunds["name"], "type": "main", "index": 0},
            ]]
        },
        fetch_orders["name"]: {
            "main": [[{"node": merge["name"], "type": "main", "index": 0}]]
        },
        fetch_open["name"]: {
            "main": [[{"node": merge["name"], "type": "main", "index": 1}]]
        },
        fetch_refunds["name"]: {
            "main": [[{"node": merge["name"], "type": "main", "index": 2}]]
        },
        merge["name"]: {
            "main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]
        },
        aggregate["name"]: {
            "main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*wa_sends, telegram_send]]]
        },
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
    out = os.path.expanduser("~/n8n-bonpet/daily_pulse_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes → {out}")

    existing_id = find_existing()
    if existing_id:
        print(f"Found existing workflow {existing_id} — updating (PUT)")
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
    else:
        print("No existing workflow with this name — creating (POST)")
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None

    print(f"HTTP {status}")
    if isinstance(body, dict):
        print(json.dumps({k: body[k] for k in ("id", "name", "active") if k in body}, indent=2))
    else:
        print(str(body)[:2000])

    # Ensure workflow is in the team project (shared credentials live there)
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

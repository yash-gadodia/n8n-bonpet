#!/usr/bin/env python3
"""Build the Morning Briefing workflow = Daily Pulse + Goal Tracking merged into one 9 AM SGT send."""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
from _sent_log import read_global_sent_log_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Morning Briefing - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "morning-briefing-manual-3a8e9f4d1c"

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
    "+6282240119788",  # Bari (CS agent, ID)
]

# Goal tiers
TARGET_FLOOR   = 6500
TARGET_PRIMARY = 8500
TARGET_STRETCH = 11000

DATE_RANGES_JS = r"""// Compute SGT boundaries covering:
// - yesterday / prev-day / prev-week-same-day (for Daily Pulse section)
// - month-to-date (for Goal Tracking section)
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY = 24 * 60 * 60 * 1000;

const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);
const ySgt = sgtNow.getUTCFullYear();
const mSgt = sgtNow.getUTCMonth();
const dSgt = sgtNow.getUTCDate();

const sgtMidnightToday = Date.UTC(ySgt, mSgt, dSgt) - SGT_OFFSET_MS;
const monthStart       = Date.UTC(ySgt, mSgt, 1) - SGT_OFFSET_MS;
const monthEnd         = Date.UTC(ySgt, mSgt + 1, 1) - SGT_OFFSET_MS;

const yesterdayStart  = new Date(sgtMidnightToday - DAY);
const yesterdayEnd    = new Date(sgtMidnightToday);
const prevDayStart    = new Date(sgtMidnightToday - 2 * DAY);
const prevDayEnd      = yesterdayStart;
const prevWeekStart   = new Date(sgtMidnightToday - 8 * DAY);
const prevWeekEnd     = new Date(sgtMidnightToday - 7 * DAY);
const openOrderCutoff = new Date(now.getTime() - DAY);

// Fetch window: from the earlier of (month_start, prev_week_start) up to now
const fetchStart = Math.min(monthStart, prevWeekStart.getTime());
const fetchEnd   = now.getTime();

const daysInMonth   = Math.round((monthEnd - monthStart) / DAY);
const daysElapsed   = dSgt;
const daysRemaining = daysInMonth - daysElapsed;

const dateFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
});
const wdFmt   = new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Singapore', weekday: 'short'});
const monthFmt = new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Singapore', month: 'long', year: 'numeric'});

return [{
  json: {
    yesterday_start:   yesterdayStart.toISOString(),
    yesterday_end:     yesterdayEnd.toISOString(),
    prev_day_start:    prevDayStart.toISOString(),
    prev_day_end:      prevDayEnd.toISOString(),
    prev_week_start:   prevWeekStart.toISOString(),
    prev_week_end:     prevWeekEnd.toISOString(),
    month_start:       new Date(monthStart).toISOString(),
    month_end:         new Date(monthEnd).toISOString(),
    open_order_cutoff: openOrderCutoff.toISOString(),
    fetch_start:       new Date(fetchStart).toISOString(),
    fetch_end:         new Date(fetchEnd).toISOString(),
    formatted_date:    dateFmt.format(yesterdayStart),
    prev_day_label:    wdFmt.format(prevDayStart),
    prev_week_label:   wdFmt.format(yesterdayStart),
    month_label:       monthFmt.format(new Date(monthStart + SGT_OFFSET_MS)),
    days_in_month:     daysInMonth,
    days_elapsed:      daysElapsed,
    days_remaining:    daysRemaining,
  }
}];
"""

AGGREGATE_JS = r"""// Build combined Morning Briefing: yesterday snapshot + MTD progress
const ranges = $('Set Date Ranges').first().json;

const TARGET_FLOOR   = __FLOOR__;
const TARGET_PRIMARY = __PRIMARY__;
const TARGET_STRETCH = __STRETCH__;

function extractOrders(nodeName) {
  try {
    return $(nodeName).all().flatMap(it => it.json.orders || [it.json]).filter(o => o && o.id);
  } catch (e) { return []; }
}

const orders       = extractOrders('Fetch Orders');
const openOrders   = extractOrders('Fetch Open Orders');
const refundOrders = extractOrders('Fetch Refunds');

const yStart  = new Date(ranges.yesterday_start).getTime();
const yEnd    = new Date(ranges.yesterday_end).getTime();
const pdStart = new Date(ranges.prev_day_start).getTime();
const pdEnd   = new Date(ranges.prev_day_end).getTime();
const pwStart = new Date(ranges.prev_week_start).getTime();
const pwEnd   = new Date(ranges.prev_week_end).getTime();
const mStart  = new Date(ranges.month_start).getTime();
const now     = new Date().getTime();

function fmtSGD2(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function fmtSGD0(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0}); }
function fmtK(n) {
  if (n >= 1000) {
    const k = n / 1000;
    return Number.isInteger(k) ? `${k}K` : `${k.toFixed(1)}K`;
  }
  return String(n);
}
function pctStr(curr, prior) {
  if (!prior || prior === 0) return { text: '—', emoji: '' };
  const p = Math.round(((curr - prior) / prior) * 100);
  const sign = p > 0 ? '+' : (p === 0 ? '' : '');
  const emoji = p > 0 ? '📈' : (p < 0 ? '📉' : '➡️');
  return { text: `${sign}${p}%`, emoji };
}
function progressBar(pct, width) {
  const capped = Math.max(0, Math.min(100, pct));
  const filled = Math.round((capped / 100) * width);
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}
function pctOf(rev, target) { return target > 0 ? Math.round((rev / target) * 100) : 0; }

// --- Daily Pulse section ---
let revY = 0, cntY = 0, revPD = 0, cntPD = 0, revPW = 0, cntPW = 0, newCust = 0;
let revMTD = 0, cntMTD = 0;

for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  const total = parseFloat(o.total_price || '0');

  if (t >= yStart && t < yEnd) {
    revY += total; cntY++;
    if (o.customer && Number(o.customer.orders_count) === 1) newCust++;
  } else if (t >= pdStart && t < pdEnd) {
    revPD += total; cntPD++;
  } else if (t >= pwStart && t < pwEnd) {
    revPW += total; cntPW++;
  }

  if (t >= mStart && t <= now) {
    revMTD += total; cntMTD++;
  }
}

// Open orders > 24h
let openCount = 0, oldestMs = null;
const cutoff = new Date(ranges.open_order_cutoff).getTime();
for (const o of openOrders) {
  const t = new Date(o.created_at).getTime();
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
  const upd = new Date(o.updated_at).getTime();
  if (upd < yStart || upd >= yEnd) continue;
  const refunds = Array.isArray(o.refunds) ? o.refunds : [];
  let orderRefundAmount = 0;
  for (const r of refunds) {
    const rAt = new Date(r.created_at || r.processed_at || '').getTime();
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

// Abandoned cart recoveries sent yesterday (wa_sent_log, workflow='abandoned_cart')
let cartRecoveries = 0;
try {
  for (const it of $('Read Global Sent Log').all()) {
    const j = it.json;
    if (String(j.workflow || '') !== 'abandoned_cart') continue;
    const t = new Date(j.sent_at || 0).getTime();
    if (t >= yStart && t < yEnd) cartRecoveries++;
  }
} catch (e) { /* log empty on first run */ }

const dod = pctStr(revY, revPD);
const wow = pctStr(revY, revPW);

// --- Goal Tracking section ---
const daysElapsed   = ranges.days_elapsed;
const daysInMonth   = ranges.days_in_month;
const daysRemaining = ranges.days_remaining;
const runRate       = daysElapsed > 0 ? (revMTD / daysElapsed) * daysInMonth : 0;

const pFloor   = pctOf(revMTD, TARGET_FLOOR);
const pTarget  = pctOf(revMTD, TARGET_PRIMARY);
const pStretch = pctOf(revMTD, TARGET_STRETCH);

const needForTarget = Math.max(0, TARGET_PRIMARY - revMTD);
const perDayToTarget = daysRemaining > 0 ? needForTarget / daysRemaining : needForTarget;

let statusLine;
if (runRate >= TARGET_STRETCH)       statusLine = '💎 On track for Stretch!';
else if (runRate >= TARGET_PRIMARY)  statusLine = '🚀 On track for Target.';
else if (runRate >= TARGET_FLOOR)    statusLine = '🎯 On track for Floor. Push for Target.';
else                                 statusLine = '⚠️ Below Floor run rate.';

const bar = (pct) => progressBar(pct, 10);
const pad = (s, w) => s.padEnd(w, ' ');
const dodLine = `vs ${ranges.prev_day_label}:`;
const wowLine = `vs last ${ranges.prev_week_label}:`;
const cmpWidth = Math.max(dodLine.length, wowLine.length);

const msg = `🐾 *Bon Pet Morning Briefing*
_${ranges.formatted_date}_

💰 *Yesterday*
Revenue: S$${fmtSGD2(revY)} (${cntY} order${cntY === 1 ? '' : 's'})
${pad(dodLine, cmpWidth)} ${dod.text} ${dod.emoji}
${pad(wowLine, cmpWidth)} ${wow.text} ${wow.emoji}

👥 New customers: ${newCust}
📦 Open >24h: ${openCount}${oldestAgeDays ? ` (oldest ${oldestAgeDays}d)` : ''}
↩️ Refunds: ${refundCount}${refundTotal > 0 ? ` (-S$${fmtSGD2(refundTotal)})` : ''}
🛒 Cart recoveries: ${cartRecoveries}

🎯 *${ranges.month_label} — Day ${daysElapsed} of ${daysInMonth}*
MTD: S$${fmtSGD0(revMTD)} (${cntMTD} orders) · Run rate S$${fmtSGD0(runRate)}/mo
Floor   S$${fmtK(TARGET_FLOOR)}  ${bar(pFloor)}  ${pFloor}%
Target  S$${fmtK(TARGET_PRIMARY)}  ${bar(pTarget)}  ${pTarget}% 🎯
Stretch S$${fmtK(TARGET_STRETCH)}  ${bar(pStretch)}  ${pStretch}%

${statusLine}
_${daysRemaining} day${daysRemaining === 1 ? '' : 's'} left · need S$${fmtSGD0(perDayToTarget)}/day for Target_`;

return [{ json: { message: msg, revenue_yesterday: revY, revenue_mtd: revMTD, run_rate: runRate } }];
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


def merge_node(name, pos, n_inputs):
    return {
        "parameters": {"numberInputs": n_inputs},
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.merge",
        "typeVersion": 3.1,
        "position": pos,
    }


def whatsapp_node(name, pos, phone):
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
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
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
        "parameters": {"rule": {"interval": [{"triggerAtHour": 9}]}},
        "id": uid(),
        "name": "Daily 9AM SGT",
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
        "Fetch Orders", [480, 200],
        "=" + base + "/orders.json?status=any&financial_status=paid"
        "&created_at_min={{ $json.fetch_start }}"
        "&created_at_max={{ $json.fetch_end }}"
        "&limit=250&fields=id,total_price,created_at,customer,financial_status,cancelled_at"
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

    read_wa_log = read_global_sent_log_node([480, 800])
    merge = merge_node("Merge Fetches", [720, 400], 4)
    aggregate = code_node("Aggregate & Format", [960, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [1200, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1200, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, fetch_open, fetch_refunds, read_wa_log, merge, aggregate, *wa_sends, telegram_send]

    connections = {
        schedule["name"]:      {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        manual["name"]:        {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        set_dates["name"]: {
            "main": [[
                {"node": fetch_orders["name"],  "type": "main", "index": 0},
                {"node": fetch_open["name"],    "type": "main", "index": 0},
                {"node": fetch_refunds["name"], "type": "main", "index": 0},
                {"node": read_wa_log["name"],   "type": "main", "index": 0},
            ]]
        },
        fetch_orders["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        fetch_open["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        fetch_refunds["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        read_wa_log["name"]:   {"main": [[{"node": merge["name"], "type": "main", "index": 3}]]},
        merge["name"]:         {"main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]},
        aggregate["name"]:     {"main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*wa_sends, telegram_send]]]},
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
    out = os.path.expanduser("~/n8n-bonpet/morning_briefing_payload.json")
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
        print(f"Transfer → team project")

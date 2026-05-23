#!/usr/bin/env python3
"""Build the Morning Briefing workflow = Daily Pulse + Goal Tracking merged into one 9 AM SGT send."""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
from _sent_log import read_global_sent_log_node, filter_recent_sent_log_node
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
    "+6282240119788",  # Bari (CS agent, ID)
]

# Goal tiers
TARGET_FLOOR   = 6500
TARGET_PRIMARY = 8500
TARGET_STRETCH = 11000

DATE_RANGES_JS = r"""// Compute SGT boundaries covering:
// - yesterday (slim daily snapshot)
// - last 7d vs prior 7d (rolling trend)
// - month-to-date vs prior month same window (apples-to-apples MoM)
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

const priorMonthY = mSgt === 0 ? ySgt - 1 : ySgt;
const priorMonthM = mSgt === 0 ? 11 : mSgt - 1;
const priorMonthStart = Date.UTC(priorMonthY, priorMonthM, 1) - SGT_OFFSET_MS;
const priorMonthEnd   = monthStart;

// Prior-month window matches how much of current month has elapsed
const elapsedMs = now.getTime() - monthStart;
const priorMonthWindowEnd = Math.min(priorMonthStart + elapsedMs, priorMonthEnd);

const yesterdayStart  = sgtMidnightToday - DAY;
const yesterdayEnd    = sgtMidnightToday;
const last7Start      = sgtMidnightToday - 7 * DAY;
const last7End        = sgtMidnightToday;
const prior7Start     = sgtMidnightToday - 14 * DAY;
const prior7End       = sgtMidnightToday - 7 * DAY;
const openOrderCutoff = now.getTime() - DAY;

// Fetch window: from priorMonthStart up to now (covers everything we need)
const fetchStart = priorMonthStart;
const fetchEnd   = now.getTime();

const daysInMonth   = Math.round((monthEnd - monthStart) / DAY);
const daysElapsed   = dSgt;
const daysRemaining = daysInMonth - daysElapsed;

const dateFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
});
const monthFmt = new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Singapore', month: 'long', year: 'numeric'});
const priorMonthFmt = new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Singapore', month: 'short', year: 'numeric'});

return [{
  json: {
    yesterday_start:        new Date(yesterdayStart).toISOString(),
    yesterday_end:          new Date(yesterdayEnd).toISOString(),
    last7_start:            new Date(last7Start).toISOString(),
    last7_end:              new Date(last7End).toISOString(),
    prior7_start:           new Date(prior7Start).toISOString(),
    prior7_end:             new Date(prior7End).toISOString(),
    month_start:            new Date(monthStart).toISOString(),
    month_end:              new Date(monthEnd).toISOString(),
    prior_month_start:      new Date(priorMonthStart).toISOString(),
    prior_month_window_end: new Date(priorMonthWindowEnd).toISOString(),
    open_order_cutoff:      new Date(openOrderCutoff).toISOString(),
    fetch_start:            new Date(fetchStart).toISOString(),
    fetch_end:              new Date(fetchEnd).toISOString(),
    formatted_date:         dateFmt.format(new Date(yesterdayStart)),
    month_label:            monthFmt.format(new Date(monthStart + SGT_OFFSET_MS)),
    prior_month_label:      priorMonthFmt.format(new Date(priorMonthStart + SGT_OFFSET_MS)),
    days_in_month:          daysInMonth,
    days_elapsed:           daysElapsed,
    days_remaining:         daysRemaining,
  }
}];
"""

AGGREGATE_JS = r"""// Morning Briefing v2: target tracking + rolling trends + month-over-month + auto gap diagnosis.
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
const l7Start = new Date(ranges.last7_start).getTime();
const l7End   = new Date(ranges.last7_end).getTime();
const p7Start = new Date(ranges.prior7_start).getTime();
const p7End   = new Date(ranges.prior7_end).getTime();
const mStart  = new Date(ranges.month_start).getTime();
const pmStart = new Date(ranges.prior_month_start).getTime();
const pmEnd   = new Date(ranges.prior_month_window_end).getTime();
const now     = new Date().getTime();

function fmtSGD2(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function fmtSGD0(n) { return Math.round(n).toLocaleString('en-US'); }
function fmtK(n) {
  if (n >= 1000) {
    const k = n / 1000;
    return Number.isInteger(k) ? `${k}K` : `${k.toFixed(1)}K`;
  }
  return String(n);
}
function progressBar(pct, width) {
  const capped = Math.max(0, Math.min(100, pct));
  const filled = Math.round((capped / 100) * width);
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}
function pctOf(rev, target) { return target > 0 ? Math.round((rev / target) * 100) : 0; }
function isSubOrder(o) {
  // Shopify REST API exposes subscription marker via tags + source_name (not discount_codes;
  // those only carry the synthetic 'Subscription' code in webhook payloads).
  const tags = String(o.tags || '');
  if (/(^|,)\s*Subscription(\s|,|$)/i.test(tags)) return true;
  if (String(o.source_name || '').startsWith('subscription_contract')) return true;
  return false;
}

// Buckets
const newBucket = () => ({ rev: 0, count: 0, subs: 0 });
const yest   = newBucket();
const last7  = newBucket();
const prior7 = newBucket();
const mtd    = newBucket();
const priorM = newBucket();

// New-customer acquisitions (by customer.created_at falling in window, deduped)
const seenCust = new Set();
const newCustIds = { yest: new Set(), last7: new Set(), prior7: new Set(), mtd: new Set(), priorM: new Set() };

function tally(bucket, t, start, end, total, sub) {
  if (t >= start && t < end) {
    bucket.rev += total; bucket.count++; if (sub) bucket.subs++;
  }
}

for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  const total = parseFloat(o.total_price || '0');
  const sub = isSubOrder(o);

  tally(yest,   t, yStart, yEnd,   total, sub);
  tally(last7,  t, l7Start, l7End, total, sub);
  tally(prior7, t, p7Start, p7End, total, sub);
  tally(mtd,    t, mStart, now,    total, sub);
  tally(priorM, t, pmStart, pmEnd, total, sub);

  if (o.customer && o.customer.id && !seenCust.has(o.customer.id)) {
    seenCust.add(o.customer.id);
    const ca = o.customer.created_at ? new Date(o.customer.created_at).getTime() : null;
    if (ca !== null) {
      if (ca >= yStart  && ca < yEnd)   newCustIds.yest.add(o.customer.id);
      if (ca >= l7Start && ca < l7End)  newCustIds.last7.add(o.customer.id);
      if (ca >= p7Start && ca < p7End)  newCustIds.prior7.add(o.customer.id);
      if (ca >= mStart  && ca < now)    newCustIds.mtd.add(o.customer.id);
      if (ca >= pmStart && ca < pmEnd)  newCustIds.priorM.add(o.customer.id);
    }
  }
}

const aov = (b) => b.count > 0 ? b.rev / b.count : 0;

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

// Abandoned cart recoveries sent yesterday
let cartRecoveries = 0;
let _waRows = [];
try { _waRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _waRows = $('Read Global Sent Log').all(); }
  catch (e2) { _waRows = []; }
}
for (const it of _waRows) {
  const j = it.json;
  if (String(j.workflow || '') !== 'abandoned_cart') continue;
  const t = new Date(j.sent_at || 0).getTime();
  if (t >= yStart && t < yEnd) cartRecoveries++;
}

// --- Goal Tracking ---
const daysElapsed   = ranges.days_elapsed;
const daysInMonth   = ranges.days_in_month;
const daysRemaining = ranges.days_remaining;
const runRate       = daysElapsed > 0 ? (mtd.rev / daysElapsed) * daysInMonth : 0;
const dailyActual   = daysElapsed > 0 ? mtd.rev / daysElapsed : 0;

const pFloor   = pctOf(mtd.rev, TARGET_FLOOR);
const pTarget  = pctOf(mtd.rev, TARGET_PRIMARY);
const pStretch = pctOf(mtd.rev, TARGET_STRETCH);

const monthPctElapsed   = Math.round((daysElapsed / daysInMonth) * 100);
const paceGapPp         = pTarget - monthPctElapsed;
const paceLabel         = paceGapPp >= 0
  ? `+${paceGapPp}pp vs pace ✅`
  : `${paceGapPp}pp behind pace`;

const needForTarget = Math.max(0, TARGET_PRIMARY - mtd.rev);
const needForFloor  = Math.max(0, TARGET_FLOOR - mtd.rev);
const perDayToTarget = daysRemaining > 0 ? needForTarget / daysRemaining : needForTarget;
const perDayToFloor  = daysRemaining > 0 ? needForFloor / daysRemaining : needForFloor;

let statusLine;
if (runRate >= TARGET_STRETCH)       statusLine = '💎 On track for Stretch!';
else if (runRate >= TARGET_PRIMARY)  statusLine = '🚀 On track for Target.';
else if (runRate >= TARGET_FLOOR)    statusLine = '🎯 On track for Floor. Push for Target.';
else                                 statusLine = '⚠️ Below Floor run rate.';

// --- Trend rows ---
function diff(curr, prior) {
  if (prior === 0 && curr === 0) return '   =';
  if (prior === 0) return '  new';
  const p = Math.round(((curr - prior) / prior) * 100);
  const sign = p > 0 ? '+' : '';
  const emoji = Math.abs(p) >= 20 ? (p > 0 ? ' 📈' : ' 📉') : '';
  return `${sign}${p}%${emoji}`;
}
function row(label, currStr, priorStr, currNum, priorNum) {
  return `${label.padEnd(10)}${String(currStr).padStart(8)} vs ${String(priorStr).padStart(7)}  ${diff(currNum, priorNum)}`;
}

const last7Rows = [
  row('Revenue',  `S$${fmtSGD0(last7.rev)}`,  `S$${fmtSGD0(prior7.rev)}`,  last7.rev,  prior7.rev),
  row('Orders',   last7.count,                prior7.count,                last7.count, prior7.count),
  row('AOV',      `S$${fmtSGD0(aov(last7))}`, `S$${fmtSGD0(aov(prior7))}`, aov(last7), aov(prior7)),
  row('New cust', newCustIds.last7.size,      newCustIds.prior7.size,      newCustIds.last7.size, newCustIds.prior7.size),
  row('Sub ords', last7.subs,                 prior7.subs,                 last7.subs, prior7.subs),
].join('\n');

const momRows = [
  row('Revenue',  `S$${fmtSGD0(mtd.rev)}`,  `S$${fmtSGD0(priorM.rev)}`,  mtd.rev,  priorM.rev),
  row('Orders',   mtd.count,                priorM.count,                mtd.count, priorM.count),
  row('New cust', newCustIds.mtd.size,      newCustIds.priorM.size,      newCustIds.mtd.size, newCustIds.priorM.size),
].join('\n');

// --- Auto-generated gaps ---
const gaps = [];
if (paceGapPp <= -5) {
  gaps.push(`${Math.abs(paceGapPp)}pp behind monthly pace · need S$${fmtSGD0(perDayToTarget)}/day vs S$${fmtSGD0(dailyActual)}/day actual`);
}
if (newCustIds.last7.size < newCustIds.prior7.size * 0.8 && newCustIds.prior7.size > 0) {
  gaps.push(`New customer acquisition down WoW (${newCustIds.last7.size} vs ${newCustIds.prior7.size}) · top-funnel issue`);
}
if (last7.subs < prior7.subs * 0.8 && prior7.subs > 0) {
  gaps.push(`Subscription orders softening (${last7.subs} vs ${prior7.subs}) · trial conversion or winback`);
}
if (aov(last7) < aov(prior7) * 0.9 && prior7.count > 0) {
  gaps.push(`AOV down (S$${fmtSGD0(aov(last7))} vs S$${fmtSGD0(aov(prior7))}) · cart size shrinking`);
}
if (openCount >= 10) {
  gaps.push(`Open orders >24h: ${openCount}${oldestAgeDays ? ` (oldest ${oldestAgeDays}d)` : ''} · fulfilment queue debt`);
}
if (gaps.length === 0) {
  gaps.push('✅ No immediate red flags');
}
const gapBlock = gaps.map(g => `• ${g}`).join('\n');

// --- Message ---
const bar = (pct) => progressBar(pct, 10);

const msg = `🐾 *Bon Pet Morning Briefing*
_${ranges.formatted_date}_

🎯 *${ranges.month_label}* · Day ${daysElapsed} of ${daysInMonth} (${monthPctElapsed}% through)
MTD S$${fmtSGD0(mtd.rev)} · ${mtd.count} orders · Run rate S$${fmtSGD0(runRate)}/mo
Floor   S$${fmtK(TARGET_FLOOR)}  ${bar(pFloor)}  ${pFloor}%
Target  S$${fmtK(TARGET_PRIMARY)}  ${bar(pTarget)}  ${pTarget}% 🎯  ${paceLabel}
Stretch S$${fmtK(TARGET_STRETCH)}  ${bar(pStretch)}  ${pStretch}%
⏳ ${daysRemaining} day${daysRemaining === 1 ? '' : 's'} left · need S$${fmtSGD0(perDayToTarget)}/day for Target (now S$${fmtSGD0(dailyActual)}/day)

📈 *Last 7d vs prior 7d*
\`\`\`
${last7Rows}
\`\`\`

📅 *vs ${ranges.prior_month_label} (same window)*
\`\`\`
${momRows}
\`\`\`

🔎 *Gaps to watch*
${gapBlock}

📦 *Yesterday* · S$${fmtSGD0(yest.rev)} · ${yest.count} order${yest.count === 1 ? '' : 's'} · ${refundCount} refund${refundCount === 1 ? '' : 's'}${refundTotal > 0 ? ` (-S$${fmtSGD2(refundTotal)})` : ''} · ${cartRecoveries} cart recover${cartRecoveries === 1 ? 'y' : 'ies'}

${statusLine}`;

return [{ json: { message: msg, revenue_yesterday: yest.rev, revenue_mtd: mtd.rev, run_rate: runRate } }];
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
        "typeVersion": 3,
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
        "&created_at_min={{ $json.fetch_start }}"
        "&created_at_max={{ $json.fetch_end }}"
        "&limit=250&fields=id,total_price,created_at,customer,financial_status,cancelled_at,tags,source_name"
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
    filter_wa = filter_recent_sent_log_node([640, 800])
    merge = merge_node("Merge Fetches", [880, 400], 4)
    aggregate = code_node("Aggregate & Format", [960, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [1200, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1200, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, fetch_open, fetch_refunds, read_wa_log, filter_wa, merge, aggregate, *wa_sends, telegram_send]

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
        read_wa_log["name"]:   {"main": [[{"node": filter_wa["name"], "type": "main", "index": 0}]]},
        filter_wa["name"]:     {"main": [[{"node": merge["name"], "type": "main", "index": 3}]]},
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

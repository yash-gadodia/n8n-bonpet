#!/usr/bin/env python3
"""Aspire P&L — weekly/monthly cashflow report combining Aspire bank transactions
with Shopify revenue, top-5 spend categories, account balances, and MTD forecast.

Triggers: Mon 9:30 SGT (weekly), 1st of month 9:30 SGT (monthly), manual webhook.
Recipients: 4 (Yash, Nicolas, Bon Pet official, Shaun — Rachel excluded per user).
"""
import json
import uuid
import os

from _notify import telegram_send_node
import urllib.request
import urllib.error
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Aspire P&L - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "aspire-pnl-manual-7c4b9e2f8a"

# Aspire creds (currently hardcoded in node parameters — fine since workflow JSON
# stays inside n8n Cloud instance; matches existing WA_KEY pattern)
ASPIRE_BASE = "https://api.aspireapp.com/public/v1"
ASPIRE_CLIENT_ID = "SG573M-CjUe9DgRcY9kB1Fc"
ASPIRE_API_KEY = "ai7Igb4VuuSjPIwgTQHYtdmcWsfgMNwX"

# Shopify for revenue cross-reference
SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

# Goal tiers (copy of Morning Briefing constants — keep in sync manually)
TARGET_FLOOR = 6500
TARGET_PRIMARY = 8500
TARGET_STRETCH = 11000

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
# P&L recipients — SENSITIVE data, restricted to founders only
# (Rachel, Bari, and the Bon Pet official number all excluded per user)
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6581114800",  # Shaun
]

SET_RANGE_JS = r"""// Compute date range based on which trigger fired + emit shared context
function tryRead(nodeName) { try { return $(nodeName).all(); } catch (e) { return []; } }

const isWeekly = tryRead('Weekly Trigger').length > 0;
const mode = isWeekly ? 'weekly' : 'manual';

const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY = 24 * 60 * 60 * 1000;
const now = new Date();
const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);
const ySgt = sgtNow.getUTCFullYear();
const mSgt = sgtNow.getUTCMonth();
const dSgt = sgtNow.getUTCDate();
const sgtMidnightToday = Date.UTC(ySgt, mSgt, dSgt) - SGT_OFFSET_MS;

let startMs, endMs, label;
if (mode === 'weekly') {
  // Last complete Mon–Sun (same window as Top Sellers)
  const sgtDayOfWeek = sgtNow.getUTCDay();
  const daysSinceMonday = (sgtDayOfWeek + 6) % 7;
  endMs   = sgtMidnightToday - daysSinceMonday * DAY;
  startMs = endMs - 7 * DAY;
  const labelStart = new Date(startMs);
  const labelEnd   = new Date(endMs - 1);
  const dayF  = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Singapore', day:'2-digit'});
  const monF  = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Singapore', month:'short'});
  const yrF   = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Singapore', year:'numeric'});
  const d1=dayF.format(labelStart), d2=dayF.format(labelEnd);
  const m1=monF.format(labelStart), m2=monF.format(labelEnd);
  label = (m1===m2) ? `Week ${d1}–${d2} ${m2} ${yrF.format(labelEnd)}` : `Week ${d1} ${m1} – ${d2} ${m2} ${yrF.format(labelEnd)}`;
} else {
  // Manual: last 7 full days ending now
  endMs   = sgtMidnightToday;
  startMs = endMs - 7 * DAY;
  label = 'Last 7 days';
}

// Also compute current-month start for MTD/forecast (always current month)
const mtdStart = Date.UTC(ySgt, mSgt, 1) - SGT_OFFSET_MS;
const mtdEnd   = Date.UTC(ySgt, mSgt+1, 1) - SGT_OFFSET_MS;
const daysInMonth = Math.round((mtdEnd - mtdStart) / DAY);
const daysElapsed = dSgt;

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

// Shopify fetch must span BOTH the P&L window and the MTD window so the Aggregate
// code has all orders to bucket. Use min(startMs, mtdStart) as the fetch start.
const shopifyFetchStartMs = Math.min(startMs, mtdStart);

return [{ json: {
  mode,
  label,
  start_iso: iso(startMs),
  end_iso:   iso(endMs),
  shopify_fetch_start: new Date(shopifyFetchStartMs).toISOString(),
  shopify_fetch_end:   new Date(now.getTime()).toISOString(),
  mtd_start_iso: iso(mtdStart),
  mtd_end_now_iso: iso(now.getTime()),
  mtd_month_label: new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Singapore', month:'long', year:'numeric'}).format(new Date(mtdStart + SGT_OFFSET_MS)),
  days_in_month: daysInMonth,
  days_elapsed: daysElapsed,
}}];
"""

EXTRACT_TOKEN_JS = r"""// Pull the Bearer token out of the Aspire login response
const j = $input.first().json;
return [{ json: {
  access_token: j.access_token,
  expires_in: j.expires_in,
}}];
"""

AGGREGATE_JS = r"""// Build the P&L message: Aspire net cashflow + top categories + Shopify revenue + MTD forecast
const ranges = $('Set Date Range').first().json;

function tryRead(name) { try { return $(name).all(); } catch(e) { return []; } }

const txnsRaw   = tryRead('Fetch Aspire Txns');
const acctsRaw  = tryRead('Fetch Aspire Accounts');
const ordersRaw = tryRead('Fetch Shopify Orders');

// Aspire responses arrive wrapped as [{json: {data: [...]}}] or the raw .data array
function unwrapAspire(items, field) {
  if (!items.length) return [];
  const first = items[0].json || {};
  if (Array.isArray(first[field])) return first[field];
  // some executions return each data row already flattened
  return items.map(it => it.json).filter(x => x && x.id);
}

const txns = unwrapAspire(txnsRaw, 'data');
const accts = unwrapAspire(acctsRaw, 'data');

// Window for the P&L period (for categorizing transactions)
const startMs = new Date(ranges.start_iso).getTime();
const endMs   = new Date(ranges.end_iso).getTime();

function fmtSGD(cents_or_sgd, isCents) {
  const sgd = isCents ? (cents_or_sgd / 100) : cents_or_sgd;
  return sgd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtSGD0(n) {
  return Math.round(n).toLocaleString('en-US');
}

// --- Aspire aggregation ---
let cashInCents = 0, cashOutCents = 0;
let cashInCount = 0, cashOutCount = 0;
const byCategory = new Map();

for (const t of txns) {
  const tms = new Date(t.datetime).getTime();
  if (tms < startMs || tms >= endMs) continue;
  if (t.status && t.status !== 'settled') continue;  // skip pending/failed
  const amt = Number(t.amount || 0);                 // cents, negative = debit
  if (amt > 0) { cashInCents += amt; cashInCount++; }
  else if (amt < 0) {
    const absAmt = -amt;
    cashOutCents += absAmt; cashOutCount++;
    const cat = (t.additional_info && t.additional_info.spend_category) || t.channel || 'Uncategorized';
    const entry = byCategory.get(cat) || { total: 0, count: 0 };
    entry.total += absAmt;
    entry.count += 1;
    byCategory.set(cat, entry);
  }
}

const netCents = cashInCents - cashOutCents;
const topCategories = [...byCategory.entries()]
  .sort((a,b) => b[1].total - a[1].total)
  .slice(0, 5);

// --- Shopify revenue ---
const orders = ordersRaw.flatMap(it => (it.json.orders || [])).filter(o => o && o.id);
let shopifyRevenue = 0, shopifyOrderCount = 0;
for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  if (t < startMs || t >= endMs) continue;
  shopifyRevenue += parseFloat(o.total_price || '0');
  shopifyOrderCount++;
}

// --- MTD forecast (current-month run rate vs goal) ---
// Sum Shopify orders for MTD window
const mtdStart = new Date(ranges.mtd_start_iso).getTime();
const mtdEnd   = new Date(ranges.mtd_end_now_iso).getTime();
let mtdRevenue = 0, mtdCount = 0;
for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  if (t < mtdStart || t > mtdEnd) continue;
  mtdRevenue += parseFloat(o.total_price || '0');
  mtdCount++;
}
const runRate = ranges.days_elapsed > 0 ? (mtdRevenue / ranges.days_elapsed) * ranges.days_in_month : 0;

const TARGET_FLOOR = __FLOOR__;
const TARGET_PRIMARY = __PRIMARY__;
const TARGET_STRETCH = __STRETCH__;
const pctOf = (n, t) => t > 0 ? Math.round((n / t) * 100) : 0;

let statusEmoji, statusWord;
if (runRate >= TARGET_STRETCH)       { statusEmoji = '💎'; statusWord = 'Stretch'; }
else if (runRate >= TARGET_PRIMARY)  { statusEmoji = '🚀'; statusWord = 'Target'; }
else if (runRate >= TARGET_FLOOR)    { statusEmoji = '🎯'; statusWord = 'Floor'; }
else                                 { statusEmoji = '⚠️'; statusWord = 'Below Floor'; }

// --- Balances ---
const balanceLines = accts.map(a => {
  const name = (a.debit_details && a.debit_details[0] && a.debit_details[0].account_number) || a.id.slice(0, 8);
  return `• ${name}  S$${fmtSGD(a.available_balance, true)}`;
});

// --- Format message ---
const modeTitle = ranges.mode === 'weekly' ? 'Weekly P&L' : 'P&L (manual)';

const netSign = netCents >= 0 ? '+' : '-';
const netAbs = Math.abs(netCents);

const topLines = topCategories.length
  ? topCategories.map(([cat, e], i) => `${i+1}. ${cat.padEnd(18).slice(0,18)}  S$${fmtSGD(e.total, true)}  (${e.count})`).join('\n')
  : '_(no expenses)_';

const msg = `💼 *Bon Pet ${modeTitle}*
_${ranges.label}_

💰 Cash IN:   S$${fmtSGD(cashInCents, true)}  (${cashInCount} txns)
💸 Cash OUT:  S$${fmtSGD(cashOutCents, true)}  (${cashOutCount} txns)
━━━━━━━━━━━━━━━━━━━━━━
📊 Net:       ${netSign}S$${fmtSGD(netAbs, true)}

🛒 *Shopify revenue (same window)*
S$${fmtSGD(shopifyRevenue, false)}  (${shopifyOrderCount} orders)

*Top expense categories*
${topLines}

🎯 *${ranges.mtd_month_label} — Day ${ranges.days_elapsed}/${ranges.days_in_month}*
MTD: S$${fmtSGD0(mtdRevenue)} (${mtdCount} orders)
Run rate: S$${fmtSGD0(runRate)}/mo  ${statusEmoji} ${statusWord}
Floor ${pctOf(mtdRevenue, TARGET_FLOOR)}% · Target ${pctOf(mtdRevenue, TARGET_PRIMARY)}% · Stretch ${pctOf(mtdRevenue, TARGET_STRETCH)}%

💳 *Account balances*
${balanceLines.join('\n')}`;

return [{ json: {
  message: msg,
  mode: ranges.mode,
  net_cents: netCents,
  cash_in_cents: cashInCents,
  cash_out_cents: cashOutCents,
  shopify_revenue: shopifyRevenue,
  mtd_revenue: mtdRevenue,
  run_rate: runRate,
}}];
""".replace("__FLOOR__", str(TARGET_FLOOR)) \
   .replace("__PRIMARY__", str(TARGET_PRIMARY)) \
   .replace("__STRETCH__", str(TARGET_STRETCH))


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
    return {"parameters": {"jsCode": js}, "id": uid(), "name": name,
            "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos}


def build():
    weekly = {
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "30 9 * * 1"}]}},
        "id": uid(), "name": "Weekly Trigger",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.3,
        "position": [0, 250],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
                       "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 450], "webhookId": MANUAL_WEBHOOK_ID,
    }

    set_range = code_node("Set Date Range", [240, 350], SET_RANGE_JS)

    # Aspire login (POST JSON body)
    login = {
        "parameters": {
            "method": "POST",
            "url": f"{ASPIRE_BASE}/login",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({
                "grant_type": "client_credentials",
                "client_id": ASPIRE_CLIENT_ID,
                "client_secret": ASPIRE_API_KEY,
            }),
            "options": {},
        },
        "id": uid(), "name": "Aspire Login",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [480, 350],
    }

    extract_token = code_node("Extract Token", [720, 350], EXTRACT_TOKEN_JS)

    # Aspire transactions (window from Set Date Range)
    fetch_txns = {
        "parameters": {
            "method": "GET",
            "url": "=" + ASPIRE_BASE + "/transactions?start_date={{ $('Set Date Range').first().json.start_iso }}&end_date={{ $('Set Date Range').first().json.end_iso }}&per_page=1000",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $json.access_token }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Fetch Aspire Txns",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [960, 350],
    }

    fetch_accts = {
        "parameters": {
            "method": "GET",
            "url": f"{ASPIRE_BASE}/accounts",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $('Extract Token').first().json.access_token }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Fetch Aspire Accounts",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1200, 250],
    }

    # Shopify orders over the same P&L window (also covers MTD since start < MTD start)
    # Use a generous fetch from earliest(start, mtd_start) to now
    fetch_shopify = {
        "parameters": {
            "url": (
                "=https://d2ac44-d5.myshopify.com/admin/api/2024-10/orders.json"
                "?status=any&financial_status=paid"
                "&created_at_min={{ $('Set Date Range').first().json.shopify_fetch_start }}"
                "&created_at_max={{ $('Set Date Range').first().json.shopify_fetch_end }}"
                "&limit=250&fields=id,total_price,created_at,financial_status,cancelled_at"
            ),
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(), "name": "Fetch Shopify Orders",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1200, 450],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    merge = {
        "parameters": {"numberInputs": 3},
        "id": uid(), "name": "Merge Data",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [1440, 350],
    }

    aggregate = code_node("Aggregate & Format", [1680, 350], AGGREGATE_JS)

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

    wa_sends = [
        wa_node(f"Send WA #{i+1}", [1920, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1920, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [weekly, manual, set_range, login, extract_token,
             fetch_txns, fetch_accts, fetch_shopify, merge, aggregate, *wa_sends, telegram_send]

    connections = {
        weekly["name"]:  {"main": [[{"node": set_range["name"], "type": "main", "index": 0}]]},
        manual["name"]:  {"main": [[{"node": set_range["name"], "type": "main", "index": 0}]]},
        set_range["name"]: {"main": [[{"node": login["name"], "type": "main", "index": 0}]]},
        login["name"]:     {"main": [[{"node": extract_token["name"], "type": "main", "index": 0}]]},
        extract_token["name"]: {"main": [[
            {"node": fetch_txns["name"], "type": "main", "index": 0},
            {"node": fetch_accts["name"], "type": "main", "index": 0},
            {"node": fetch_shopify["name"], "type": "main", "index": 0},
        ]]},
        fetch_txns["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        fetch_accts["name"]:   {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        fetch_shopify["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
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
    if status >= 300: return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME: return wf["id"]
    return None


if __name__ == "__main__":
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/aspire_pnl_payload.json")
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

    print(f"\nManual fire: curl -X POST https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")

#!/usr/bin/env python3
"""Subscription Health Pulse — weekly Sunday 7:07 PM SGT digest of sub base.

Sends a Telegram + WA digest covering:
- Subscriber base (active / paused / cancelled lifetime)
- New subscribers this week (first sub order in last 7d)
- Sub orders revenue last 7d vs prior 7d
- Upcoming bills next 7d
- At-risk: paused >21d, active with no sub order in >35d

Data sources:
- Google Sheets `subscribers` tab (gid=700700) — live via Shopify Flow webhook
- Shopify Orders REST — last 60d, filtered by tags="Subscription" / source_name
"""
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

from _notify import telegram_send_node

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Subscription Health Pulse"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "sub-health-pulse-manual-7a4d2f8c1b"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
SUB_SHEET_GID = 700700
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()
RECIPIENTS = [
    "+6581394225",   # Yash
    "+6598531677",   # Nicolas
    "+6590108515",   # Bon Pet official
    "+6587993341",   # Rachel
    "+6282240119788",  # Bari (CS agent, ID)
]


DATE_RANGES_JS = r"""// Compute SGT boundaries for weekly sub pulse.
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY = 24 * 60 * 60 * 1000;

const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);
const ySgt = sgtNow.getUTCFullYear();
const mSgt = sgtNow.getUTCMonth();
const dSgt = sgtNow.getUTCDate();

const sgtMidnightToday = Date.UTC(ySgt, mSgt, dSgt) - SGT_OFFSET_MS;
const last7Start  = sgtMidnightToday - 7 * DAY;
const last7End    = sgtMidnightToday;
const prior7Start = sgtMidnightToday - 14 * DAY;
const prior7End   = sgtMidnightToday - 7 * DAY;
const next7End    = sgtMidnightToday + 7 * DAY;
const fetchStart  = sgtMidnightToday - 60 * DAY;

const dateFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
});

return [{
  json: {
    last7_start:  new Date(last7Start).toISOString(),
    last7_end:    new Date(last7End).toISOString(),
    prior7_start: new Date(prior7Start).toISOString(),
    prior7_end:   new Date(prior7End).toISOString(),
    next7_end:    new Date(next7End).toISOString(),
    sgt_today:    new Date(sgtMidnightToday).toISOString(),
    fetch_start:  new Date(fetchStart).toISOString(),
    fetch_end:    now.toISOString(),
    week_ending:  dateFmt.format(new Date(sgtMidnightToday - 1)),
  }
}];
"""


AGGREGATE_JS = r"""// Subscription Health Pulse aggregator.
const ranges = $('Set Date Ranges').first().json;

const subRows = $('Read Subscribers').all().map(it => it.json);

function extractOrders(nodeName) {
  try {
    return $(nodeName).all().flatMap(it => it.json.orders || [it.json]).filter(o => o && o.id);
  } catch (e) { return []; }
}
const orders = extractOrders('Fetch Orders');

const last7Start  = new Date(ranges.last7_start).getTime();
const last7End    = new Date(ranges.last7_end).getTime();
const prior7Start = new Date(ranges.prior7_start).getTime();
const prior7End   = new Date(ranges.prior7_end).getTime();
const next7End    = new Date(ranges.next7_end).getTime();
const sgtToday    = new Date(ranges.sgt_today).getTime();
const DAY = 24 * 60 * 60 * 1000;
const nowMs = Date.now();

function isSubOrder(o) {
  const tags = String(o.tags || '');
  if (/(^|,)\s*Subscription(\s|,|$)/i.test(tags)) return true;
  if (String(o.source_name || '').startsWith('subscription_contract')) return true;
  return false;
}
function fmtSGD0(n) { return Math.round(n).toLocaleString('en-US'); }
function pct(curr, prior) {
  if (prior === 0) return curr === 0 ? '   =' : ' new';
  const p = Math.round(((curr - prior) / prior) * 100);
  const sign = p > 0 ? '+' : '';
  const emoji = Math.abs(p) >= 20 ? (p > 0 ? ' 📈' : ' 📉') : '';
  return `${sign}${p}%${emoji}`;
}

// Dedupe sub rows by contract_id (sheet has 1 row per line item per contract).
const contracts = new Map();
for (const r of subRows) {
  if (!r.contract_id) continue;
  const cid = String(r.contract_id);
  if (contracts.has(cid)) continue;
  contracts.set(cid, {
    contract_id: cid,
    customer_id: String(r.customer_id || ''),
    email:       String(r.email || ''),
    status:      String(r.status || '').toUpperCase(),
    upcoming_billing_date: r.upcoming_billing_date ? new Date(r.upcoming_billing_date).getTime() : null,
    received_at:           r.received_at ? new Date(r.received_at).getTime() : null,
    cadence:               `${r.cadence_interval_count || ''}${String(r.cadence_interval || '').toLowerCase()}`,
  });
}
const all       = [...contracts.values()];
const active    = all.filter(c => c.status === 'ACTIVE');
const paused    = all.filter(c => c.status === 'PAUSED');
const cancelled = all.filter(c => c.status === 'CANCELLED');

// Filter to subscription orders only.
const subOrders = orders.filter(o => isSubOrder(o));

// Build per-customer first-sub-order timestamp + last-sub-order timestamp.
const firstSubOrderByCust = new Map();
const lastSubOrderByCust  = new Map();
for (const o of subOrders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const cid = o.customer && o.customer.id;
  if (!cid) continue;
  const t = new Date(o.created_at).getTime();
  if (!firstSubOrderByCust.has(cid) || firstSubOrderByCust.get(cid) > t) {
    firstSubOrderByCust.set(cid, t);
  }
  if (!lastSubOrderByCust.has(cid) || lastSubOrderByCust.get(cid) < t) {
    lastSubOrderByCust.set(cid, t);
  }
}

// New subscribers this week (earliest sub order in last 7d, within our 60d fetch).
let newSubsLast7 = 0;
for (const [, t0] of firstSubOrderByCust) {
  if (t0 >= last7Start && t0 < last7End) newSubsLast7++;
}

// Sub orders revenue + count, last 7d vs prior 7d.
let revLast7 = 0, cntLast7 = 0, revPrior7 = 0, cntPrior7 = 0;
for (const o of subOrders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  const total = parseFloat(o.total_price || '0');
  if (t >= last7Start && t < last7End) { revLast7 += total; cntLast7++; }
  else if (t >= prior7Start && t < prior7End) { revPrior7 += total; cntPrior7++; }
}

// Upcoming bills next 7d (active contracts only).
const upcomingByDay = new Map();
for (const c of active) {
  if (!c.upcoming_billing_date) continue;
  const t = c.upcoming_billing_date;
  if (t >= sgtToday && t < next7End) {
    const k = Math.floor((t - sgtToday) / DAY);
    upcomingByDay.set(k, (upcomingByDay.get(k) || 0) + 1);
  }
}
const totalUpcoming = [...upcomingByDay.values()].reduce((a, b) => a + b, 0);

// At-risk groups.
const pausedTooLong = paused.filter(c => c.received_at && (nowMs - c.received_at) > 21 * DAY);
const staleActive   = active.filter(c => {
  const last = lastSubOrderByCust.get(c.customer_id);
  return last && (nowMs - last) > 35 * DAY;
});
const atRiskLines = [];
if (pausedTooLong.length > 0) {
  atRiskLines.push(`• ${pausedTooLong.length} paused >21d (near 42-day cap)`);
}
if (staleActive.length > 0) {
  atRiskLines.push(`• ${staleActive.length} active but no sub order in >35d`);
}

// Day-of-week labels for upcoming bills.
const wkFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short'
});
const upcomingLines = [];
for (let i = 0; i < 7; i++) {
  const cnt = upcomingByDay.get(i) || 0;
  if (cnt > 0) {
    const dayTs = sgtToday + i * DAY;
    upcomingLines.push(`   ${wkFmt.format(new Date(dayTs))} · ${cnt}`);
  }
}

const totalActivePaused = active.length + paused.length;
const healthPct = totalActivePaused > 0 ? Math.round((active.length / totalActivePaused) * 100) : 0;

const msg = `🐾 *Bon Pet Subscription Health Pulse*
_Week ending ${ranges.week_ending}_

🔁 *Subscriber base*
   Active: ${active.length}
   Paused: ${paused.length}
   Cancelled (lifetime): ${cancelled.length}

📈 *New subscribers this week:* ${newSubsLast7}

💰 *Sub orders · last 7d vs prior 7d*
\`\`\`
Orders    ${String(cntLast7).padStart(5)} vs ${String(cntPrior7).padStart(5)}   ${pct(cntLast7, cntPrior7)}
Revenue   S$${String(fmtSGD0(revLast7)).padStart(4)} vs S$${String(fmtSGD0(revPrior7)).padStart(4)}   ${pct(revLast7, revPrior7)}
\`\`\`

⏰ *Upcoming bills next 7d:* ${totalUpcoming}
${upcomingLines.length > 0 ? upcomingLines.join('\n') : '   (none scheduled)'}

🚨 *At-risk subs*
${atRiskLines.length > 0 ? atRiskLines.join('\n') : '   ✅ None flagged'}

✅ Health: ${active.length}/${totalActivePaused} (${healthPct}% active)`;

return [{ json: {
  message: msg,
  active_count: active.length,
  paused_count: paused.length,
  cancelled_count: cancelled.length,
  new_subs_last7: newSubsLast7,
  sub_orders_last7: cntLast7,
  sub_revenue_last7: revLast7,
  upcoming_next7: totalUpcoming,
  at_risk: pausedTooLong.length + staleActive.length,
}}];
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


def code_node(name, pos, js):
    return {
        "parameters": {"jsCode": js},
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
    }


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


def gs_read_node(name, pos, tab_gid, tab_name):
    return {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Bon Pet — Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": tab_gid, "mode": "list",
                "cachedResultName": tab_name,
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={tab_gid}",
            },
            "options": {},
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.5,
        "position": pos,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "executeOnce": True,
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
    # Sunday 19:07 SGT (staggered off HH:00 per the cron-stagger memory).
    schedule = {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "7 19 * * 0"}]}
        },
        "id": uid(),
        "name": "Sun 19:07 SGT",
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
        "Fetch Orders", [480, 300],
        "=" + base + "/orders.json?status=any"
        "&created_at_min={{ $json.fetch_start }}"
        "&created_at_max={{ $json.fetch_end }}"
        "&limit=250&fields=id,total_price,created_at,customer,financial_status,cancelled_at,tags,source_name"
    )

    read_subs = gs_read_node("Read Subscribers", [480, 500], SUB_SHEET_GID, "subscribers")

    merge = merge_node("Merge", [720, 400], 2)
    aggregate = code_node("Aggregate & Format", [880, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [1140, 200 + i * 90], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1140, 200 + len(RECIPIENTS) * 90]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, read_subs, merge, aggregate, *wa_sends, telegram_send]

    connections = {
        schedule["name"]:     {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        manual["name"]:       {"main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]},
        set_dates["name"]: {
            "main": [[
                {"node": fetch_orders["name"], "type": "main", "index": 0},
                {"node": read_subs["name"],    "type": "main", "index": 0},
            ]]
        },
        fetch_orders["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_subs["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        merge["name"]:        {"main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]},
        aggregate["name"]:    {"main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*wa_sends, telegram_send]]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore"},
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
    out = os.path.expanduser("~/n8n-bonpet/sub_health_pulse_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes -> {out}")

    existing_id = find_existing()
    if existing_id:
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
        print(f"PUT existing {new_id} -> HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {new_id} -> HTTP {status}")

    if new_id and status < 300:
        http("PUT", f"/workflows/{new_id}/transfer",
             {"destinationProjectId": TEAM_PROJECT_ID})
        print("Transfer -> team project")

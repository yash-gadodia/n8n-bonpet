#!/usr/bin/env python3
"""Build the Top Seller Leaderboard workflow and deploy it to n8n (team project)."""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node, telegram_launchcycle_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Top Seller Leaderboard - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"  # "The Bon Pet"
MANUAL_WEBHOOK_ID = "top-sellers-manual-9d4a2c8f1b"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
# Team broadcast list — same 5 as Daily Pulse / Weekly Sales Report / Picklist
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6282240119788",  # Bari (CS agent, ID)
    "+6583513308",  # Siva (Launch Cycle agency - external)
    "+6588146498",  # Raghav (Launch Cycle agency - external)
]

DATE_RANGES_JS = r"""// Compute "this_week" (most recent completed Mon-Sun in SGT) + prev week
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY = 24 * 60 * 60 * 1000;

const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);
const sgtDayOfWeek = sgtNow.getUTCDay();            // 0=Sun ... 6=Sat
const daysSinceMonday = (sgtDayOfWeek + 6) % 7;     // 0=Mon ... 6=Sun
const sgtMidnightToday = Date.UTC(
  sgtNow.getUTCFullYear(), sgtNow.getUTCMonth(), sgtNow.getUTCDate()
) - SGT_OFFSET_MS;

// weekEnd = Monday 00:00 SGT of current week (i.e. Sunday 24:00 of last week)
// If triggered on a Monday, weekEnd = today 00:00 — so "last week" = the 7 days just past
const weekEnd   = sgtMidnightToday - daysSinceMonday * DAY;
const weekStart = weekEnd - 7 * DAY;
const prevWeekEnd   = weekStart;
const prevWeekStart = weekStart - 7 * DAY;

const labelStart = new Date(weekStart);
const labelEnd   = new Date(weekEnd - 1);  // Sunday 23:59:59.999 — for display

const fmtDay  = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Singapore', day: '2-digit'});
const fmtMon  = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Singapore', month: 'short'});
const fmtYear = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Singapore', year: 'numeric'});

const d1 = fmtDay.format(labelStart);
const d2 = fmtDay.format(labelEnd);
const m1 = fmtMon.format(labelStart);
const m2 = fmtMon.format(labelEnd);
const y  = fmtYear.format(labelEnd);

const weekLabel = (m1 === m2)
  ? `Week ${d1}–${d2} ${m2} ${y}`
  : `Week ${d1} ${m1} – ${d2} ${m2} ${y}`;

return [{
  json: {
    week_start:       new Date(weekStart).toISOString(),
    week_end:         new Date(weekEnd).toISOString(),
    prev_week_start:  new Date(prevWeekStart).toISOString(),
    prev_week_end:    new Date(prevWeekEnd).toISOString(),
    fetch_start:      new Date(prevWeekStart).toISOString(),  // 14d window
    fetch_end:        new Date(weekEnd).toISOString(),
    week_label:       weekLabel,
  }
}];
"""

AGGREGATE_JS = r"""// Aggregate orders into top-10 products by revenue for last week, with W-over-W movement
const ranges = $('Set Date Ranges').first().json;
const allItems = $('Fetch Orders').all();

const orders = allItems.flatMap(it => it.json.orders || [it.json]).filter(o => o && o.id);

const wStart  = new Date(ranges.week_start).getTime();
const wEnd    = new Date(ranges.week_end).getTime();
const pwStart = new Date(ranges.prev_week_start).getTime();
const pwEnd   = new Date(ranges.prev_week_end).getTime();

function bucketOrder(o, rangeStart, rangeEnd, bucket) {
  const t = new Date(o.created_at).getTime();
  if (t < rangeStart || t >= rangeEnd) return;
  if (o.cancelled_at) return;
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') return;
  for (const li of (o.line_items || [])) {
    const pid = li.product_id || `title:${li.title}`;
    const title = String(li.title || 'Unknown').trim();
    const qty = Number(li.quantity || 0);
    const price = parseFloat(li.price || '0');
    const revenue = qty * price;
    if (!bucket[pid]) bucket[pid] = { product_id: pid, title, units: 0, revenue: 0 };
    // Prefer the more descriptive title if one is clearly better
    if (title.length > bucket[pid].title.length) bucket[pid].title = title;
    bucket[pid].units += qty;
    bucket[pid].revenue += revenue;
  }
}

const weekBucket = {};
const prevBucket = {};
for (const o of orders) {
  bucketOrder(o, wStart,  wEnd,  weekBucket);
  bucketOrder(o, pwStart, pwEnd, prevBucket);
}

function rankList(bucket) {
  return Object.values(bucket).sort((a, b) => {
    if (b.revenue !== a.revenue) return b.revenue - a.revenue;
    if (b.units   !== a.units)   return b.units   - a.units;
    return a.title.localeCompare(b.title);
  });
}

const weekRanked = rankList(weekBucket);
const prevRanked = rankList(prevBucket);
const prevRankByPid = new Map();
prevRanked.forEach((p, i) => prevRankByPid.set(p.product_id, i + 1));

function movementEmoji(pid, currRank) {
  const prev = prevRankByPid.get(pid);
  if (prev === undefined) return '🆕   ';
  const delta = prev - currRank;  // positive = moved up (better rank)
  if (delta === 0) return '➡️   ';
  if (delta > 0)   return `⬆️${String(delta).padEnd(3)}`;
  return `⬇️${String(Math.abs(delta)).padEnd(3)}`;
}

function fmtSGD(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0}); }

const topN = weekRanked.slice(0, 10);
const totalWeekRevenue = weekRanked.reduce((s, p) => s + p.revenue, 0);
const top10Revenue = topN.reduce((s, p) => s + p.revenue, 0);
const top10Pct = totalWeekRevenue > 0 ? Math.round((top10Revenue / totalWeekRevenue) * 100) : 0;

let body;
if (topN.length === 0) {
  body = '_No paid orders last week._';
} else {
  const lines = topN.map((p, idx) => {
    const rank = idx + 1;
    const mv = movementEmoji(p.product_id, rank);
    const rankStr = String(rank).padStart(2, ' ');
    return `${rankStr}. ${mv} ${p.title} — S$${fmtSGD(p.revenue)} (${p.units}u)`;
  });
  body = lines.join('\n');
}

const msg = `🏆 *Bon Pet Top Sellers*
_${ranges.week_label}_

${body}

_Top-10 revenue: S$${fmtSGD(top10Revenue)} (${top10Pct}% of week)_`;

return [{ json: {
  message: msg,
  week_label: ranges.week_label,
  top_products: topN,
  total_week_revenue: totalWeekRevenue,
  top10_revenue: top10Revenue,
  top10_pct: top10Pct,
} }];
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
            "rule": {"interval": [{"field": "cronExpression", "expression": "10 9 * * 1"}]}
        },
        "id": uid(),
        "name": "Mon 9:10 AM SGT",
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
        "Fetch Orders", [480, 400],
        "=" + base + "/orders.json?status=any&financial_status=paid"
        "&created_at_min={{ $json.fetch_start }}"
        "&created_at_max={{ $json.fetch_end }}"
        "&limit=250&fields=id,cancelled_at,financial_status,created_at,line_items"
    )

    aggregate = code_node("Aggregate & Format", [720, 400], AGGREGATE_JS)

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [960, 200 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [960, 200 + len(RECIPIENTS) * 100]
    )
    telegram_lc = telegram_launchcycle_node(
        "Send Telegram LaunchCycle", [960, 300 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, set_dates, fetch_orders, aggregate, *wa_sends, telegram_send, telegram_lc]

    connections = {
        schedule["name"]: {
            "main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]
        },
        manual["name"]: {
            "main": [[{"node": set_dates["name"], "type": "main", "index": 0}]]
        },
        set_dates["name"]: {
            "main": [[{"node": fetch_orders["name"], "type": "main", "index": 0}]]
        },
        fetch_orders["name"]: {
            "main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]
        },
        aggregate["name"]: {
            "main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*wa_sends, telegram_send, telegram_lc]]]
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
    out = os.path.expanduser("~/n8n-bonpet/top_sellers_payload.json")
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

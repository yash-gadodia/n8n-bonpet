#!/usr/bin/env python3
"""Weekly Stock Report — Wed 9am SGT snapshot of all GC SKUs to team WA + Telegram weslee.

Companion to the existing Low Stock Watcher (which only fires on critical/low
thresholds). This one sends the full stock table every week so the team sees
what's healthy too, not just the alarms.

Source: same Stock Update sheet, "FOOD PRODUCTION" tab. Same filter (rows
starting with "GC ") so cat + dog SKUs are covered.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node, telegram_launchcycle_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Weekly Stock Report - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "weekly-stock-manual-2d8f4c1a9e"

DOC_ID = "1yYzRL5pkpmoPflL_vzOUeI_eimaOTMH7gfv_INlplx8"
DASHBOARD_GID = 887506772   # current balance per SKU (merged-title quirk: col keys are col_1..col_9)
IN_GID = 282256084          # cook log: PRODUCT, DATE, QTY IN, BATCH
OUT_GID = 413081182         # dispatch log: PRODUCT, DATE, QTY OUT, BATCH, bag counts
CONSUMPTION_WINDOW_DAYS = 28  # 4 weeks rolling avg for rate calc

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",     # Yash
    "+6598531677",     # Nicolas
    "+6590108515",     # Bon Pet official
    "+6587993341",     # Rachel
    "+6282240119788",  # Bari (CS)
    "+6583513308",  # Siva (Launch Cycle agency - external)
    "+6588146498",  # Raghav (Launch Cycle agency - external)
]

FORMAT_JS = r"""// Build weekly stock snapshot enriched with cook + consumption data.
//
// DASHBOARD merged-title quirk: real column names sit in row 3, so n8n maps
// PRODUCT → 'FOOD PRODUCTION' and BALANCE → 'col_4'. We skip the row where the
// value === 'PRODUCT' (that's the real header landing as data).
//
// IN tab = cook log (PRODUCT, DATE DD/MM/YYYY, QTY IN, BATCH).
// OUT tab = dispatch log (PRODUCT, DATE DD/MM/YYYY, QTY OUT, BATCH, bag counts).
// Date field name has a literal \n: "DATE\n(DD/MM/YYYY)".
const WINDOW_DAYS = __WINDOW_DAYS__;
const DAY_MS = 24 * 60 * 60 * 1000;
const nowMs = Date.now();

const dashboardRows = $('Read DASHBOARD').all();
const inRows        = $('Read IN').all();
const outRows       = $('Read OUT').all();

const today = new Date().toLocaleDateString('en-GB', {
  timeZone: 'Asia/Singapore',
  weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
});

function parseDMY(s) {
  if (!s) return NaN;
  const m = String(s).trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return NaN;
  return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1])).getTime();
}

function dateKey(row) {
  // n8n keeps the real column name including the literal \n
  return row['DATE\n(DD/MM/YYYY)'] || row.DATE || '';
}

// Last cook date per product from IN.
const lastCookByProduct = new Map();
for (const r of inRows) {
  const j = r.json;
  const product = String(j.PRODUCT || '').trim();
  if (!product) continue;
  const ts = parseDMY(dateKey(j));
  if (!Number.isFinite(ts)) continue;
  const prev = lastCookByProduct.get(product);
  if (!prev || ts > prev) lastCookByProduct.set(product, ts);
}

// Consumption per product over the last WINDOW_DAYS from OUT.
const consumptionByProduct = new Map();
const windowStart = nowMs - WINDOW_DAYS * DAY_MS;
for (const r of outRows) {
  const j = r.json;
  const product = String(j.PRODUCT || '').trim();
  if (!product) continue;
  const ts = parseDMY(dateKey(j));
  if (!Number.isFinite(ts)) continue;
  if (ts < windowStart) continue;
  const qty = Number(j['QTY OUT']);
  if (!Number.isFinite(qty)) continue;
  consumptionByProduct.set(product, (consumptionByProduct.get(product) || 0) + qty);
}

const critical = [];
const low = [];
const healthy = [];

for (const it of dashboardRows) {
  const j = it.json;
  const product = String(j['FOOD PRODUCTION'] || j.PRODUCT || '').trim();
  if (!product || product === 'PRODUCT') continue;
  if (!product.startsWith('GC ')) continue;
  const balance = Number(j.col_4 !== undefined ? j.col_4 : j.BALANCE);
  if (!Number.isFinite(balance)) continue;

  const lastCookTs = lastCookByProduct.get(product);
  const daysSinceCook = lastCookTs
    ? Math.round((nowMs - lastCookTs) / DAY_MS)
    : null;
  const lastCookLabel = lastCookTs
    ? new Date(lastCookTs).toLocaleDateString('en-GB', {
        timeZone: 'Asia/Singapore', day: '2-digit', month: 'short'
      })
    : null;

  const consumed = consumptionByProduct.get(product) || 0;
  const dailyRate = consumed / WINDOW_DAYS;
  const weeklyRate = dailyRate * 7;
  const runwayDays = dailyRate > 0 ? Math.round(balance / dailyRate) : null;

  let nextCookBy = null;
  if (runwayDays !== null) {
    // 7-day safety buffer so they cook before running out
    const byTs = nowMs + Math.max(0, runwayDays - 7) * DAY_MS;
    nextCookBy = new Date(byTs).toLocaleDateString('en-GB', {
      timeZone: 'Asia/Singapore', day: '2-digit', month: 'short'
    });
  }

  const row = {
    product, balance,
    last_cook_label: lastCookLabel, days_since_cook: daysSinceCook,
    consumed_in_window: consumed, weekly_rate: Math.round(weeklyRate),
    runway_days: runwayDays, next_cook_by: nextCookBy,
  };

  // Tier is balance-OR-runway: a high-burn SKU with 120 balance but 18 days runway
  // is as urgent as a 40-balance SKU. Catches fast-moving products before they stock out.
  const runway = row.runway_days;
  const hasRunway = runway !== null;
  if (balance < 50 || (hasRunway && runway <= 14)) critical.push(row);
  else if (balance < 100 || (hasRunway && runway <= 30)) low.push(row);
  else healthy.push(row);
}

// Sort within each tier by runway asc (most urgent first), nulls last.
const sortFn = (a, b) => {
  const ra = a.runway_days ?? 1e9;
  const rb = b.runway_days ?? 1e9;
  return ra - rb || a.balance - b.balance;
};
critical.sort(sortFn); low.sort(sortFn); healthy.sort(sortFn);

function formatLine(r) {
  const rateTxt = r.weekly_rate > 0
    ? `↓${r.weekly_rate}/wk · ~${r.runway_days}d left`
    : '(no sales in window)';
  const cookTxt = r.last_cook_label
    ? ` · cook ${r.last_cook_label} (${r.days_since_cook}d)`
    : '';
  // Runway-based warning fires regardless of tier, since high-burn healthy-balance
  // SKUs can still be <3 weeks from stockout.
  const planTxt = (r.next_cook_by && r.runway_days !== null && r.runway_days <= 21)
    ? `\n    ⚠️ Plan next cook by ${r.next_cook_by}`
    : '';
  return `  • ${r.product} — ${r.balance}\n    ${rateTxt}${cookTxt}${planTxt}`;
}

const totalSkus = critical.length + low.length + healthy.length;
const totalUnits = [...critical, ...low, ...healthy].reduce((s, r) => s + r.balance, 0);
const totalWeeklyBurn = [...critical, ...low, ...healthy].reduce((s, r) => s + r.weekly_rate, 0);

const parts = [
  `📦 *Weekly Stock Update — The Bon Pet*`,
  `📅 ${today}`,
  `_Rate based on last ${WINDOW_DAYS} days of dispatches_`,
  ''
];

if (critical.length) {
  parts.push(`🔴 *Critical* (<50 balance or ≤14d runway)`);
  parts.push(critical.map(formatLine).join('\n'));
  parts.push('');
}
if (low.length) {
  parts.push(`🟡 *Low* (<100 balance or ≤30d runway)`);
  parts.push(low.map(formatLine).join('\n'));
  parts.push('');
}
if (healthy.length) {
  parts.push(`🟢 *Healthy*`);
  parts.push(healthy.map(formatLine).join('\n'));
  parts.push('');
}
if (!totalSkus) {
  parts.push('_No GC SKUs found — check the DASHBOARD tab._');
}

parts.push(`📊 Total: ${totalSkus} SKUs · ${totalUnits.toLocaleString()} units · ${totalWeeklyBurn}/wk burn`);

const message = parts.join('\n');

return [{
  json: {
    today,
    critical_count: critical.length,
    low_count: low.length,
    healthy_count: healthy.length,
    total_skus: totalSkus,
    total_units: totalUnits,
    total_weekly_burn: totalWeeklyBurn,
    message,
  }
}];
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
        try: return e.code, json.loads(body)
        except Exception: return e.code, body


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
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
    }


def build():
    schedule = {
        "parameters": {
            # Wed 9am SGT = Wed 01:00 UTC. n8n Cloud runs UTC, but cron uses the
            # workflow's timezone (defaults to UTC unless set). Easiest: write the
            # cron as "0 9 * * 3" and set settings.timezone to Asia/Singapore.
            "rule": {"interval": [{"field": "cronExpression", "expression": "0 9 * * 3"}]}
        },
        "id": uid(), "name": "Wed 9 AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.3,
        "position": [0, 300],
    }

    manual = {
        "parameters": {
            "httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
            "responseMode": "onReceived", "options": {},
        },
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 500], "webhookId": MANUAL_WEBHOOK_ID,
    }

    def sheet_read_node(name, pos, gid):
        return {
            "parameters": {
                "documentId": {
                    "__rl": True, "value": DOC_ID, "mode": "list",
                    "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit",
                },
                "sheetName": {
                    "__rl": True, "value": gid, "mode": "list",
                    "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit#gid={gid}",
                },
                "options": {},
            },
            "id": uid(), "name": name,
            "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
            "position": pos,
            "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
        }

    read_dashboard = sheet_read_node("Read DASHBOARD", [240, 200], DASHBOARD_GID)
    read_in        = sheet_read_node("Read IN",        [240, 400], IN_GID)
    read_out       = sheet_read_node("Read OUT",       [240, 600], OUT_GID)

    merge = {
        "parameters": {"numberInputs": 3},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [480, 400],
    }

    format_node = {
        "parameters": {"jsCode": FORMAT_JS.replace("__WINDOW_DAYS__", str(CONSUMPTION_WINDOW_DAYS))},
        "id": uid(), "name": "Format Report",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [720, 400],
    }

    wa_sends = [
        whatsapp_node(f"Send WhatsApp #{i+1}", [960, 100 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [960, 100 + len(RECIPIENTS) * 100]
    )
    telegram_lc = telegram_launchcycle_node(
        "Send Telegram LaunchCycle", [960, 200 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, read_dashboard, read_in, read_out, merge, format_node, *wa_sends, telegram_send, telegram_lc]

    fanout_targets = [
        {"node": read_dashboard["name"], "type": "main", "index": 0},
        {"node": read_in["name"],        "type": "main", "index": 0},
        {"node": read_out["name"],       "type": "main", "index": 0},
    ]
    connections = {
        schedule["name"]:        {"main": [fanout_targets]},
        manual["name"]:          {"main": [fanout_targets]},
        read_dashboard["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_in["name"]:         {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_out["name"]:        {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        merge["name"]:           {"main": [[{"node": format_node["name"], "type": "main", "index": 0}]]},
        format_node["name"]:     {"main": [[
            {"node": n["name"], "type": "main", "index": 0}
            for n in [*wa_sends, telegram_send, telegram_lc]
        ]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore"},
    }


def find_existing():
    status, data = http("GET", "/workflows?limit=250")
    if status >= 300: return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME: return wf["id"]
    return None


if __name__ == "__main__":
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/weekly_stock_report_payload.json")
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
    print("Schedule: Wed 9 AM SGT (cron: 0 9 * * 3, timezone Asia/Singapore)")
    print(f"Manual fire: curl -X POST https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")

#!/usr/bin/env python3
"""Win-back — daily cron checks for customers whose last order was exactly 60 days ago
(1-day window), WAs them a come-back message. The narrow window means each dormant
customer gets at most one message per dormancy cycle without needing a send-log.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)
from _notify import telegram_send_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Win-back - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "winback-manual-4f9c3d2a7e"

# Open-ended 42+ days since last order. Sorted oldest-first, capped at DAILY_CAP
# per run so the backlog drips out instead of blasting. The winback_sent tab is
# an authoritative "already messaged" log — customers on it are skipped forever.
DORMANT_MIN_DAYS = 42   # 6 weeks — anyone past here is definitely not an active subscriber
DAILY_CAP = 50          # max sends per run

# DRY_RUN: if true, all WAs go to Yash instead of customers (plus header to Yash).
# Flip to false once you've verified candidates look right.
DRY_RUN = False
SEND_WA_DISABLED = False  # flip False to actually send WAs

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
ORDERS_TAB_GID = 0
CUSTOMERS_TAB_GID = 100100
WINBACK_SENT_TAB_GID = 1248726917  # created by setup_winback_sent_tab.py

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
# Team broadcast for the Winback header summary. Yash is excluded because he
# already receives the header via the main Send Winback WA node (target_phone
# defaults to YASH_PHONE on the header item).
TEAM_RECIPIENTS = [
    ("Nicolas",          "+6598531677"),
    ("Bon Pet official", "+6590108515"),
    ("Rachel",           "+6587993341"),
    ("Shaun",            "+6581114800"),
    ("Bari",             "+6282240119788"),
]


def team_wa_node(name, pos, phone):
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

COMPUTE_ELIGIBLE_JS = r"""// Dormant customers with daysSince >= MIN_DAYS and not already in winback_sent tab.
// Sorted oldest-first (longest lapsed = priority), capped at DAILY_CAP per run.
// Per 6-week subscription rule: daysSince >= 42 cannot be an active subscriber
// → safe to message without checking the subscribers tab.
const MIN_DAYS = __MIN__;
const DAILY_CAP = __CAP__;
const DRY_RUN = __DRY_RUN__;
const YASH_PHONE = '+6581394225';

const ordersRows   = $('Read Orders Tab').all();
const customerRows = $('Read Customers Tab').all();
const sentRows     = $('Read Winback Sent Tab').all();

function parseDate(s) {
  if (!s) return null;
  const d = new Date(String(s));
  return isNaN(d.getTime()) ? null : d.getTime();
}
function normEmail(s) { return String(s || '').trim().toLowerCase(); }

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

const sentEmails = new Set();
for (const r of sentRows) {
  const e = normEmail(r.json.email);
  if (e) sentEmails.add(e);
}

const lastByEmail = new Map();
for (const r of ordersRows) {
  const j = r.json;
  const email = normEmail(j.email);
  if (!email) continue;
  const ts = parseDate(j.order_date);
  if (!ts) continue;
  const prev = lastByEmail.get(email);
  if (!prev) {
    lastByEmail.set(email, { last_ts: ts, order_count: 1 });
  } else {
    prev.order_count += 1;
    if (ts > prev.last_ts) prev.last_ts = ts;
  }
}

const now = Date.now();
const DAY_MS = 24 * 60 * 60 * 1000;

const stats = {
  customers_scanned: customerRows.length,
  orders_scanned: ordersRows.length,
  already_sent_log_size: sentEmails.size,
  no_email: 0,
  no_orders_match: 0,
  too_recent: 0,
  already_sent: 0,
  no_phone: 0,
  invalid_phone: 0,
  skipped_global_cooldown: 0,
  eligible_before_cap: 0,
  capped_out: 0,
  sending: 0,
};

const allEligible = [];

for (const c of customerRows) {
  const j = c.json;
  const email = normEmail(j.email);
  if (!email) { stats.no_email++; continue; }
  if (sentEmails.has(email)) { stats.already_sent++; continue; }

  const last = lastByEmail.get(email);
  if (!last) { stats.no_orders_match++; continue; }

  const daysSince = (now - last.last_ts) / DAY_MS;
  if (daysSince < MIN_DAYS) { stats.too_recent++; continue; }

  const rawPhone = j.phone || j.default_address_phone || '';
  if (!rawPhone) { stats.no_phone++; continue; }
  const phone = normalizePhone(rawPhone);
  if (!phone) { stats.invalid_phone++; continue; }

  if (isInGlobalCooldown(phone)) {
    stats.skipped_global_cooldown++;
    continue;
  }

  const firstName = String(j.first_name || '').trim() ||
                    String(j.last_name || '').trim();

  allEligible.push({
    email,
    customer_id: String(j.customer_id || ''),
    phone,
    first_name: firstName,
    days_since_last_order: Math.round(daysSince),
    last_order_at: new Date(last.last_ts).toISOString(),
    total_orders: Number(j.total_orders || last.order_count || 0),
    total_spent: Number(j.total_spent || 0),
  });
}

// Newest-lapsed first: customers who just crossed 42d get the pitch while they're still warm.
allEligible.sort((a, b) => a.days_since_last_order - b.days_since_last_order);
stats.eligible_before_cap = allEligible.length;
const capped = allEligible.slice(0, DAILY_CAP);
stats.capped_out = Math.max(0, allEligible.length - DAILY_CAP);
stats.sending = capped.length;

const eligible = capped.map(x => ({ json: x }));

const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
const headerMsg = `🔁 *Win-back — ${modeTag}*\n📅 ${new Date().toISOString().slice(0,10)}\n\n` +
  `🎯 Sending today: *${stats.sending}* (cap ${DAILY_CAP})\n` +
  `Eligible total (before cap): ${stats.eligible_before_cap}\n` +
  `Capped out (waiting): ${stats.capped_out}\n` +
  `Window: ${MIN_DAYS}+ days since last order\n\n` +
  `📊 Funnel\n` +
  `• Customers scanned: ${stats.customers_scanned}\n` +
  `• Orders scanned: ${stats.orders_scanned}\n` +
  `• Already-sent log size: ${stats.already_sent_log_size}\n` +
  `• No email: ${stats.no_email}\n` +
  `• No order match: ${stats.no_orders_match}\n` +
  `• Too recent (<${MIN_DAYS}d): ${stats.too_recent}\n` +
  `• Already messaged: ${stats.already_sent}\n` +
  `• No phone: ${stats.no_phone}\n` +
  `• Invalid phone: ${stats.invalid_phone}\n` +
  `• Global 7d cooldown: ${stats.skipped_global_cooldown}`;

const header = [{ json: { is_header: true, phone: YASH_PHONE, target_phone: YASH_PHONE, message: headerMsg } }];

return header.concat(eligible);
"""

FORMAT_MESSAGE_JS = r"""// Build winback msg per eligible customer. Header passes through untouched.
const DRY_RUN = __DRY_RUN__;
const YASH_PHONE = '+6581394225';

const out = [];
for (const it of $input.all()) {
  const j = it.json;
  if (j.is_header) {
    out.push({ json: { target_phone: j.target_phone, message: j.message, is_header: true } });
    continue;
  }
  const greeting = j.first_name ? `Hey ${j.first_name}!` : 'Hey there!';
  const msg = `${greeting} 🐾\n\n` +
    `The Bon Pet here. Been a while, hope your furkid's doing well 🙂\n\n` +
    `What's new: Pork is now on the dog menu 🍖, Duck is now on the cat menu 🦆\n\n` +
    `We're still a small SG team, growing thanks to pawrents like you. Every formula and ingredient ratio we use is public 🔍 https://thebonpet.com/pages/formulas\n\n` +
    `No hidden fillers, no guesswork. PhD-formulated, AAFCO-balanced, gently cooked.\n\n` +
    `Come back with 20% off: *WELCOMEBACK<3THEBONPET* 🎁\n` +
    `Works on one-time or Subscribe & Save.\n\n` +
    `🛍 Shop: https://thebonpet.com\n` +
    `💬 Cat pawrents community: https://chat.whatsapp.com/BTh5sXiZBkKIewYLY3HDFC\n` +
    `💬 Dog pawrents community: https://chat.whatsapp.com/G3OTmBkC5os1XeJYZT2RRL\n\n` +
    `Any feedback or questions, just reply 💛\n\n` +
    `❤️ The Bon Pet team`;

  const dryPreview = `🧪 *DRY RUN — would send to ${j.first_name || 'customer'} (${j.phone})*\n` +
    `📊 days_since=${j.days_since_last_order}  orders=${j.total_orders}  spent=$${j.total_spent}\n` +
    `═══════════════════════════════════\n\n${msg}`;

  out.push({
    json: {
      target_phone: DRY_RUN ? YASH_PHONE : j.phone,
      message: DRY_RUN ? dryPreview : msg,
      email: j.email,
      customer_id: j.customer_id,
      first_name: j.first_name,
      days_since_last_order: j.days_since_last_order,
      phone: j.phone,
      workflow: 'winback',
      template: 'welcomeback_20',
      sent_at: new Date().toISOString(),
      order_id: '',
      notes: `days_since=${j.days_since_last_order}`,
      is_header: false,
    }
  });
}
return out;
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


def read_sheet_node(name, pos, gid):
    return {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": gid, "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={gid}",
            },
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": pos,
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }


def send_wa_node(name, pos):
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
            # Rate limit: 5s between sends so we don't look like a spam-blast.
            "options": {
                "batching": {
                    "batch": {
                        "batchSize": 1,
                        "batchInterval": 5000,
                    },
                },
            },
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
        "disabled": SEND_WA_DISABLED,
    }


def append_sent_node(pos):
    # Logs (email, sent_at, days_since, first_name) per messaged customer.
    # Disabled when SEND_WA_DISABLED or DRY_RUN — we don't poison the sent log
    # with customers who didn't actually get a real WA.
    return {
        "parameters": {
            "operation": "append",
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": f"gid={WINBACK_SENT_TAB_GID}", "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={WINBACK_SENT_TAB_GID}",
            },
            "columns": {
                "mappingMode": "defineBelow",
                "value": {
                    "email": "={{ $json.email }}",
                    "sent_at": "={{ $now.toISO() }}",
                    "days_since": "={{ $json.days_since_last_order }}",
                    "first_name": "={{ $json.first_name }}",
                },
                "matchingColumns": [],
                "schema": [
                    {"id": "email", "displayName": "email", "type": "string", "canBeUsedToMatch": True, "required": False, "display": True, "defaultMatch": False},
                    {"id": "sent_at", "displayName": "sent_at", "type": "string", "canBeUsedToMatch": True, "required": False, "display": True, "defaultMatch": False},
                    {"id": "days_since", "displayName": "days_since", "type": "number", "canBeUsedToMatch": True, "required": False, "display": True, "defaultMatch": False},
                    {"id": "first_name", "displayName": "first_name", "type": "string", "canBeUsedToMatch": True, "required": False, "display": True, "defaultMatch": False},
                ],
            },
            "options": {},
        },
        "id": uid(), "name": "Log Winback Sent",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": pos,
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
        "disabled": SEND_WA_DISABLED or DRY_RUN,
    }


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "0 10 * * *"}]}
        },
        "id": uid(), "name": "Daily 10 AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.3,
        "position": [0, 400],
    }

    manual = {
        "parameters": {
            "httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
            "responseMode": "onReceived", "options": {},
        },
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 200], "webhookId": MANUAL_WEBHOOK_ID,
    }

    orders       = read_sheet_node("Read Orders Tab",       [240, 300], ORDERS_TAB_GID)
    customers    = read_sheet_node("Read Customers Tab",    [240, 500], CUSTOMERS_TAB_GID)
    winback_sent = read_sheet_node("Read Winback Sent Tab", [240, 700], WINBACK_SENT_TAB_GID)
    read_global  = read_global_sent_log_node([240, 900])

    merge = {
        "parameters": {"numberInputs": 4},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [480, 500],
    }

    dry = "true" if DRY_RUN else "false"
    compute_js = (COMPUTE_ELIGIBLE_JS
                  .replace("__MIN__", str(DORMANT_MIN_DAYS))
                  .replace("__CAP__", str(DAILY_CAP))
                  .replace("__DRY_RUN__", dry))
    format_js = FORMAT_MESSAGE_JS.replace("__DRY_RUN__", dry)
    compute    = code_node("Find Eligible Customers", [720, 500], compute_js)
    format_msg = code_node("Format Message",          [960, 500], format_js)
    send       = send_wa_node("Send Winback WA",      [1200, 500])
    drop_hdr   = code_node("Drop Header",             [1440, 500],
                           "return $input.all().filter(it => !it.json.is_header);")
    log_sent   = append_sent_node([1680, 500])
    log_global = append_global_sent_log_node([1920, 500])
    log_global["disabled"] = SEND_WA_DISABLED or DRY_RUN

    pass_header = code_node("Pass Header Only", [1200, 700],
                            "return $input.all().filter(it => it.json.is_header === true);")
    send_telegram = telegram_send_node("Send Telegram Weslee", [1440, 700])

    team_wa_sends = [
        team_wa_node(f"Team WA {name}", [1440, 900 + i * 100], phone)
        for i, (name, phone) in enumerate(TEAM_RECIPIENTS)
    ]

    nodes = [schedule, manual, orders, customers, winback_sent, read_global, merge,
             compute, format_msg, send, drop_hdr, log_sent, log_global,
             pass_header, send_telegram, *team_wa_sends]

    connections = {
        schedule["name"]:  {"main": [[
            {"node": orders["name"],       "type": "main", "index": 0},
            {"node": customers["name"],    "type": "main", "index": 0},
            {"node": winback_sent["name"], "type": "main", "index": 0},
            {"node": read_global["name"],  "type": "main", "index": 0},
        ]]},
        manual["name"]:    {"main": [[
            {"node": orders["name"],       "type": "main", "index": 0},
            {"node": customers["name"],    "type": "main", "index": 0},
            {"node": winback_sent["name"], "type": "main", "index": 0},
            {"node": read_global["name"],  "type": "main", "index": 0},
        ]]},
        orders["name"]:       {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        customers["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        winback_sent["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        read_global["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 3}]]},
        merge["name"]:        {"main": [[{"node": compute["name"], "type": "main", "index": 0}]]},
        compute["name"]:      {"main": [[{"node": format_msg["name"], "type": "main", "index": 0}]]},
        format_msg["name"]:   {"main": [[
            {"node": send["name"], "type": "main", "index": 0},
            {"node": pass_header["name"], "type": "main", "index": 0},
        ]]},
        send["name"]:         {"main": [[{"node": drop_hdr["name"], "type": "main", "index": 0}]]},
        drop_hdr["name"]:     {"main": [[{"node": log_sent["name"], "type": "main", "index": 0}]]},
        log_sent["name"]:     {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
        pass_header["name"]:  {"main": [[
            {"node": send_telegram["name"], "type": "main", "index": 0},
            *[{"node": n["name"], "type": "main", "index": 0} for n in team_wa_sends],
        ]]},
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
    out = os.path.expanduser("~/n8n-bonpet/winback_payload.json")
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
    print(f"Window: last order {DORMANT_MIN_DAYS}+ days ago, cap {DAILY_CAP}/run (daily 10 AM SGT cron)")
    print(f"Sent log: winback_sent tab (gid={WINBACK_SENT_TAB_GID}) — logged customers are skipped forever")
    print(f"Global log: wa_sent_log — appended after each send; 7d cross-workflow cooldown enforced")
    print(f"Manual fire: curl -X POST https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")

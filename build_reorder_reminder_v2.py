#!/usr/bin/env python3
"""Reorder Reminder v2 — reads from Customer Orders DB sheet (workaround for Shopify Basic PII restriction).

Architecture:
  Schedule (6PM SGT) ┐
                     ├→ Read Orders → Read Customers → Compute Candidates → Send WA
  Manual Webhook    ┘

In DRY RUN, Send WA targets Yash only. Flip DRY_RUN const to false in the Code node + re-PUT to go live.
"""
import json, uuid, os, subprocess, urllib.request, urllib.error
from _notify import telegram_send_node, telegram_launchcycle_node
from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)
from _blacklist import BLACKLIST_JS_SNIPPET

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}  # self-hosted ID; old Cloud was sxbz0Cu8yhdi0RdN
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REORDER_SENT_GID = 800800
REORDER_SENT_TAB = "reorder_reminder_sent"

WF_ID = "SUuwJMm0R6gNzXnm"  # self-hosted ID; old Cloud was AMd0mktMWn73UCbZ
WEBHOOK_PATH = "trigger-reorder-now"
SEND_WA_DISABLED = False  # flip True for verification, False for live

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
YASH_PHONE = "+6581394225"

CODE_JS = r"""// Reorder Reminder v2 — reads orders + subscribers + sent-log for authoritative exclusion.
// Subscribers sheet auto-updates via Shopify webhook. Sent log updates after each send.
const DRY_RUN = false;
const YASH_PHONE = '+6581394225';

// Build the active subscriber exclusion set LIVE from the subscribers sheet.
const ACTIVE_SUBSCRIBER_EMAILS = new Set();
const ACTIVE_SUBSCRIBER_CUSTOMER_IDS = new Set();
for (const it of $('Read Subscribers').all()) {
  const s = it.json;
  if (String(s.status || '').toUpperCase() !== 'ACTIVE') continue;
  if (s.email) ACTIVE_SUBSCRIBER_EMAILS.add(String(s.email).toLowerCase().trim());
  if (s.customer_id) ACTIVE_SUBSCRIBER_CUSTOMER_IDS.add(String(s.customer_id));
}

// SG phone normalizer — the CSV backfill has mixed formats (bare 8-digit, 10-digit 65-prefix,
// spaced). WhatsApp API requires +6512345678 with no spaces.
function normalizePhone(p) {
  if (!p) return '';
  let s = String(p).replace(/\s/g, '').trim();
  if (s.startsWith('+')) {
    const d = s.slice(1).replace(/\D/g, '');
    return '+' + d;
  }
  const digits = s.replace(/\D/g, '');
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  if (digits.length >= 8 && digits.length <= 15) return '+' + digits;  // best-effort non-SG
  return '';  // unusable — empty triggers skip
}

// Rolling sent-log dedup — any phone we've ever nudged is permanently excluded.
// This is the fix for the 3x-in-a-row spam bug. Sheet is appended by Log Sent node post-send.
const ALREADY_SENT_PHONES = new Set();
for (const it of $('Read Sent Log').all()) {
  const p = normalizePhone(it.json.phone);
  if (p) ALREADY_SENT_PHONES.add(p);
}
""" + COOLDOWN_JS_SNIPPET + BLACKLIST_JS_SNIPPET + r"""

// Rival-workflow guard — don't reorder-nudge if post_trial_nurture sent recently.
// Janani case (2026-05-20): she got reorder May 1 + post_trial D21 May 11 = 2 reorder-flavoured nudges in 10d.
// 30d window matches post_trial_nurture's D7→D21 cycle: while a trial customer is being nurtured, hold reorder.
const REORDER_RIVAL_DAYS = 30;
const REORDER_RIVAL_MS = REORDER_RIVAL_DAYS * 24 * 60 * 60 * 1000;
const REORDER_RIVALS = new Set(['post_trial_nurture']);
const RECENT_RIVAL_BY_PHONE = new Map();
let _rivalRows = [];
try { _rivalRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _rivalRows = $('Read Global Sent Log').all(); }
  catch (e2) { _rivalRows = []; }
}
for (const it of _rivalRows) {
  const s = it.json;
  if (!REORDER_RIVALS.has(String(s.workflow || ''))) continue;
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  const prev = RECENT_RIVAL_BY_PHONE.get(p) || 0;
  if (t > prev) RECENT_RIVAL_BY_PHONE.set(p, t);
}
function isInRivalNudgeWindow(phone) {
  const last = RECENT_RIVAL_BY_PHONE.get(phone);
  if (!last) return false;
  return (Date.now() - last) < REORDER_RIVAL_MS;
}
const DEFAULT_CADENCE_DAYS = 14;
const GRAMS_PER_DAY = 150;
const MIN_CADENCE = 5;
const MAX_CADENCE = 30;
const REMIND_1_OFFSET_MIN = -3;
const REMIND_1_OFFSET_MAX = 0;
const REMIND_2_OFFSET_MIN = 3;
const REMIND_2_OFFSET_MAX = 5;
// Absolute safety floor — never remind someone who just ordered regardless of computed cadence
const MIN_DAYS_SINCE_LAST = 7;
// Daily fan-out ceiling — bounds a pathological run (e.g. first run over a backlog) so we
// never blast hundreds of 3-burst sends at once and OOM the instance. Narrow reminder
// windows mean this rarely binds; most-overdue customers are kept first.
const DAILY_CAP = 60;

// After Merge (combine), $input holds both orders + subscribers interleaved.
// Pull orders directly from Read Orders node for clarity.
const orders = $('Read Orders').all().map(it => it.json);

// Group orders by email (primary) or customer_id (fallback)
const ordersByKey = new Map();
for (const o of orders) {
  const email = o.email ? String(o.email).toLowerCase().trim() : '';
  const cid = o.customer_id ? String(o.customer_id) : '';
  const key = email || (cid ? `cid:${cid}` : '');
  if (!key) continue;
  if (!ordersByKey.has(key)) ordersByKey.set(key, []);
  ordersByKey.get(key).push(o);
}

const today = new Date();
const todayMs = today.getTime();

const stats = {
  total_orders: orders.length,
  unique_buyers: ordersByKey.size,
  skipped_subscription: 0,
  skipped_no_phone: 0,
  too_recent: 0,
  in_remind_1_window: 0,
  between_windows: 0,
  in_remind_2_window: 0,
  too_late: 0,
  capped_out: 0,
};

const candidates = [];
const sample_customers = [];
let messagedCount = 0;

// Most-overdue first (oldest last order) so the cap keeps the customers who need it most.
const sortedEntries = Array.from(ordersByKey.entries()).sort((a, b) => {
  const la = Math.max.apply(null, a[1].map(o => new Date(o.order_date).getTime()));
  const lb = Math.max.apply(null, b[1].map(o => new Date(o.order_date).getTime()));
  return la - lb;
});

for (const [key, custOrders] of sortedEntries) {
  custOrders.sort((a, b) => new Date(b.order_date) - new Date(a.order_date));
  const last = custOrders[0];

  // Authoritative subscriber check: skip if email OR customer_id matches Shopify's active list
  const emailLc = (last.email || '').toLowerCase().trim();
  const cid = String(last.customer_id || '');
  if (ACTIVE_SUBSCRIBER_EMAILS.has(emailLc) ||
      (cid && ACTIVE_SUBSCRIBER_CUSTOMER_IDS.has(cid))) {
    stats.skipped_subscription++;
    continue;
  }

  const phone = normalizePhone(last.phone);
  if (!phone) { stats.skipped_no_phone++; continue; }
  if (ALREADY_SENT_PHONES.has(phone)) { stats.skipped_already_sent = (stats.skipped_already_sent || 0) + 1; continue; }
  if (isInGlobalCooldown(phone)) { stats.skipped_global_cooldown = (stats.skipped_global_cooldown || 0) + 1; continue; }
  if (isInRivalNudgeWindow(phone)) { stats.skipped_rival_nudge = (stats.skipped_rival_nudge || 0) + 1; continue; }
  if (isBlacklisted(phone)) { stats.skipped_blacklist = (stats.skipped_blacklist || 0) + 1; continue; }

  const firstName = last.first_name || 'there';

  // Cadence: median gap (if 2+ orders) else compute from order weight
  let cadence;
  if (custOrders.length >= 2) {
    const gaps = [];
    for (let i = 1; i < custOrders.length; i++) {
      const g = (new Date(custOrders[i-1].order_date) - new Date(custOrders[i].order_date)) / (1000*60*60*24);
      if (g > 0) gaps.push(g);
    }
    if (gaps.length) {
      gaps.sort((a, b) => a - b);
      cadence = gaps[Math.floor(gaps.length / 2)];
    }
  }
  if (!cadence) {
    const grams = Number(last.total_grams || 0);
    cadence = grams > 0 ? grams / GRAMS_PER_DAY : DEFAULT_CADENCE_DAYS;
  }
  cadence = Math.max(MIN_CADENCE, Math.min(MAX_CADENCE, Math.round(cadence)));

  const daysSince = Math.floor((todayMs - new Date(last.order_date).getTime()) / (1000*60*60*24));
  if (daysSince < MIN_DAYS_SINCE_LAST) { stats.too_recent++; continue; }
  const offset = daysSince - cadence;

  let reminderNum = null;
  if (offset >= REMIND_1_OFFSET_MIN && offset <= REMIND_1_OFFSET_MAX) {
    reminderNum = 1; stats.in_remind_1_window++;
  } else if (offset >= REMIND_2_OFFSET_MIN && offset <= REMIND_2_OFFSET_MAX) {
    reminderNum = 2; stats.in_remind_2_window++;
  } else if (offset < REMIND_1_OFFSET_MIN) {
    stats.too_recent++;
  } else if (offset > REMIND_1_OFFSET_MAX && offset < REMIND_2_OFFSET_MIN) {
    stats.between_windows++;
  } else {
    stats.too_late++;
  }
  if (sample_customers.length < 3) sample_customers.push({firstName, phone, daysSince, cadence, ordersCount: custOrders.length});
  if (!reminderNum) continue;

  // Daily fan-out ceiling (per customer, not per burst).
  if (messagedCount >= DAILY_CAP) { stats.capped_out++; continue; }
  messagedCount++;

  // Cart link — use stored cart_link from order (was computed at ingest)
  const cartLink = last.cart_link || 'https://thebonpet.com/collections/all';

  // 3-burst pattern: short hello → founder + context → easy-reorder offer with cart link.
  // No promo codes in body copy; replies route to humans who pitch contextually.
  let msg1 = '', msg2 = '', msg3 = '';
  if (reminderNum === 1) {
    msg1 = `hihi 🐾`;
    msg2 = `yash & nic here from bon pet 🙂 saw your last order was ${daysSince} days back, your furkid prob running low soon 🥣`;
    msg3 = `easy reorder if useful 🛒 ${cartLink} - or just lmk if you need a hand 💛`;
  } else {
    msg1 = `hihi 🐾`;
    msg2 = `yash & nic here from bon pet 👋 been ${daysSince} days since your last order, wanted to check ur furkid isn't running low`;
    msg3 = `easy reorder when ready 🛒 ${cartLink} - any qs just reply 💛`;
  }

  const baseFields = {
    customer_email: last.email,
    customer_name: firstName,
    customer_phone: phone,
    phone: phone,
    first_name: firstName,
    sent_at: new Date().toISOString(),
    last_order_id: last.order_id,
    last_order_at: last.order_date,
    days_since: daysSince,
    cadence_days: cadence,
    reminder_num: reminderNum,
    past_orders_count: custOrders.length,
    cart_link: cartLink,
    workflow: 'reorder_reminder',
    template: 'reminder_' + reminderNum,
    order_id: last.order_id,
    notes: 'cadence=' + cadence + 'd,days_since=' + daysSince,
  };

  // Emit 3 items in burst order. HTTP Send WA processes sequentially at 2s/item via batching.
  // Only seq===3 propagates to Log Sent (Skip Header filter), so dedup stays per (phone, reminder).
  const _bursts = [msg1, msg2, msg3];
  for (let _i = 0; _i < 3; _i++) {
    const seq = _i + 1;
    const liveMsg = _bursts[_i];
    const dryPrefix = (seq === 1)
      ? `🧪 [DRY · R${reminderNum} → ${firstName} ${phone} · ${seq}/3]\n`
      : `[${seq}/3]\n`;
    candidates.push({
      ...baseFields,
      seq: seq,
      target_phone: DRY_RUN ? YASH_PHONE : phone,
      message: DRY_RUN ? dryPrefix + liveMsg : liveMsg,
    });
  }
}

const diag = [
  `📊 *Funnel*`,
  `• Orders read: ${stats.total_orders}`,
  `• Unique buyers: ${stats.unique_buyers}`,
  `• Skipped (subscription): ${stats.skipped_subscription}`,
  `• Skipped (no phone): ${stats.skipped_no_phone}`,
  `• Skipped (already sent previously): ${stats.skipped_already_sent || 0}`,
  `• Skipped (global 7d cooldown): ${stats.skipped_global_cooldown || 0}`,
  `• Skipped (rival 30d — post_trial_nurture): ${stats.skipped_rival_nudge || 0}`,
  `• Too recent (just ordered): ${stats.too_recent}`,
  `• In reminder #1 window: ${stats.in_remind_1_window}`,
  `• Between windows: ${stats.between_windows}`,
  `• In reminder #2 window: ${stats.in_remind_2_window}`,
  `• Too late (churned?): ${stats.too_late}`,
  `• Capped out (over daily ${DAILY_CAP}): ${stats.capped_out}`,
  ``,
  `🎯 *Candidates this run: ${candidates.length}*`,
];

const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
if (candidates.length === 0) {
  return [{ json: {
    target_phone: YASH_PHONE,
    is_header: true,
    message: `🔍 *Reorder Reminder — ${modeTag}*\n📅 ` + new Date().toISOString().slice(0,10) +
             `\n\n0 candidates today.\n\n${diag.join('\n')}\n\n*Sample customers (any window):*\n` +
             sample_customers.map(s => `• ${s.firstName} (${s.phone}) — ${s.daysSince}d since last, cadence=${s.cadence}d, ${s.ordersCount} orders`).join('\n')
  }}];
}

candidates.unshift({
  target_phone: YASH_PHONE,
  message: `🔍 *Reorder Reminder — ${modeTag}*\n📅 ` + new Date().toISOString().slice(0,10) +
           `\n\n${candidates.length} candidate(s) will be messaged ⬇️\n\n${diag.join('\n')}`,
  is_header: true,
});

return candidates.map(c => ({ json: c }));
"""


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def uid():
    return str(uuid.uuid4())


def inject_subscriber_lists(code_js):
    """Deprecated — Code node now reads from subscribers sheet at runtime."""
    return code_js


def schedule_node():
    return {
        "parameters": {"rule": {"interval": [{"triggerAtHour": 18}]}},
        "id": uid(), "name": "Daily 6PM SGT",
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


def code_node():
    return {
        "parameters": {"jsCode": inject_subscriber_lists(CODE_JS)},
        "id": uid(), "name": "Compute Reorder Candidates",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [720, 200],
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
            # 2s batching = each customer experiences a ~4s 3-burst before next customer's burst
            "options": {
                "batching": {"batch": {"batchSize": 1, "batchInterval": 2000}},
            },
        },
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [960, 200],
        "onError": "continueRegularOutput",  # one bad phone doesn't halt the rest
        "disabled": SEND_WA_DISABLED,  # flip true for verification runs that don't fire WAs
    }


def skip_header_filter_node():
    # Reaches back to upstream Code node (not $input.all()) so phone/customer fields
    # survive the HTTP Send WA response replacement. Filters seq===3 so dedup-log
    # writes only once per customer (not 3× per burst). See feedback_n8n_http_input_passthrough.
    js = "return $('Compute Reorder Candidates').all().filter(it => !it.json.is_header && it.json.seq === 3);"
    return {
        "parameters": {"jsCode": js},
        "id": uid(), "name": "Skip Header",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 300],
    }


def pass_header_only_node():
    return {
        "parameters": {"jsCode": "// Keep only the diagnostic header — that's what team broadcasts want.\nreturn $input.all().filter(it => it.json.is_header);"},
        "id": uid(), "name": "Pass Header Only",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 500],
    }


def log_sent_node():
    return {
        "parameters": {
            "operation": "append",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": REORDER_SENT_GID, "mode": "list",
                          "cachedResultName": REORDER_SENT_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REORDER_SENT_GID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "schema": [{"id": h, "displayName": h, "required": False,
                            "display": True, "type": "string"}
                           for h in ["phone", "sent_at", "last_order_id", "reminder_num", "first_name", "days_since"]],
            },
            "options": {},
        },
        "id": uid(), "name": "Log Sent",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [1440, 300],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "onError": "continueRegularOutput",
    }


schedule = schedule_node()
webhook = webhook_node()
read_orders = gs_read_node("Read Orders", 0, "orders", [240, 100])
read_subs = gs_read_node("Read Subscribers", 700700, "subscribers", [480, 200])
# CRITICAL: executeOnce=True — otherwise Read Subscribers runs once per input
# item from Read Orders (1979 times) and OOMs the workflow.
read_subs["executeOnce"] = True
read_sent_log = gs_read_node("Read Sent Log", REORDER_SENT_GID, REORDER_SENT_TAB, [480, 400])
read_sent_log["executeOnce"] = True
read_global = read_global_sent_log_node([480, 600])
code = code_node()
send_wa = send_wa_node()
skip_header = skip_header_filter_node()
log_sent = log_sent_node()
log_global = append_global_sent_log_node([1680, 300])
pass_header = pass_header_only_node()
send_telegram = telegram_send_node("Send Telegram Weslee", [1440, 500])
send_telegram_lc = telegram_launchcycle_node("Send Telegram LaunchCycle", [1440, 620])

nodes = [schedule, webhook, read_orders, read_subs, read_sent_log, read_global, code,
         send_wa, skip_header, log_sent, log_global, pass_header, send_telegram, send_telegram_lc]
connections = {
    schedule["name"]: {"main": [[{"node": read_orders["name"], "type": "main", "index": 0}]]},
    webhook["name"]:  {"main": [[{"node": read_orders["name"], "type": "main", "index": 0}]]},
    read_orders["name"]: {"main": [[{"node": read_subs["name"], "type": "main", "index": 0}]]},
    read_subs["name"]: {"main": [[{"node": read_sent_log["name"], "type": "main", "index": 0}]]},
    read_sent_log["name"]: {"main": [[{"node": read_global["name"], "type": "main", "index": 0}]]},
    read_global["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
    # Fan out from Code: one branch to Send WA (all items), one to Telegram (header only)
    code["name"]: {"main": [[
        {"node": send_wa["name"], "type": "main", "index": 0},
        {"node": pass_header["name"], "type": "main", "index": 0},
    ]]},
    send_wa["name"]: {"main": [[{"node": skip_header["name"], "type": "main", "index": 0}]]},
    # Chain per-workflow log → global log (both onError continueRegularOutput)
    # Skip Header fans out to BOTH log nodes in parallel — fixes input-passthrough bug
    # where Log Sent's autoMap stripped the phone field before passing to Log Global Sent.
    # See feedback_n8n_http_input_passthrough memory.
    skip_header["name"]: {"main": [[
        {"node": log_sent["name"],   "type": "main", "index": 0},
        {"node": log_global["name"], "type": "main", "index": 0},
    ]]},
    pass_header["name"]: {"main": [[{"node": send_telegram["name"], "type": "main", "index": 0}, {"node": send_telegram_lc["name"], "type": "main", "index": 0}]]},
}

payload = {
    "name": "Reorder Reminder - WhatsApp",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
}

if __name__ == "__main__":
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT → HTTP {status}")
    print(body[:300])
    print()
    print(f"Workflow URL: https://n8n.thebonpet.com/workflow/{WF_ID}")
    print(f"Manual webhook: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

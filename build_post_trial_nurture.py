#!/usr/bin/env python3
"""Post-Trial Nurture Sequence — 3-touch WhatsApp nurture for trial pack buyers (days 7/14/21).

Architecture:
  Schedule (10AM SGT) ┐
                      ├→ Read Orders → Compute Trial Candidates (D7/D14/D21)
                      │                → Send WA + Log to sheet
  Manual Webhook    ┘

Exclusion rules (hard-coded):
  - Active subscribers (discount code starts with "Subscription")
  - Non-trial purchases after trial (already converted)
  - Opted out (if opt-out list exists in sheet)
  - Missing/malformed phone
  - Already sent same-day step (idempotency check)

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
POST_TRIAL_SENT_GID = 900900
POST_TRIAL_SENT_TAB = "post_trial_sent"

# Workflow ID (populated on first create, then update this for future edits)
WF_NAME = "Post-Trial Nurture — WhatsApp 7/14/21"
WF_ID = "VR7jZxPaiRCwdIaP"  # Self-hosted ID (post-migration 2026-04-27); old Cloud ID was UUKCHxXItI4yEG4g

WEBHOOK_PATH = "trigger-post-trial-now"
SEND_WA_DISABLED = False  # flip True for verification, False for live

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
YASH_PHONE = "+6581394225"
TEAM_PHONES = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6282240119788",  # Bari
]

# Trial pack SKU handles (from CLAUDE.md)
TRIAL_PACK_SKUS = [
    "cat-trial-pack",
    "dog-trial-pack",
    "free-cat-trial-pack",
    "free-dog-trial-pack",
    "trial-pack",  # fallback for variants
]

CODE_JS = r"""// Post-Trial Nurture v2 — 3-burst pattern (msg1 hello, msg2 founder, msg3 question).
// Reads orders sheet for trial pack buyers at days 7/14/21.
const DRY_RUN = false;
const YASH_PHONE = '+6581394225';
const TEAM_PHONES = ['+6581394225', '+6598531677', '+6590108515', '+6587993341', '+6282240119788'];

// Trial pack SKU detection (from CLAUDE.md product catalog)
function isTrialPack(sku, title) {
  const skuLc = (sku || '').toLowerCase();
  const titleLc = (title || '').toLowerCase();
  const trialKeywords = ['trial-pack', 'trial', 'free-trial'];
  return trialKeywords.some(k => skuLc.includes(k) || titleLc.includes(k));
}

// SG phone normalizer — CSV backfill has mixed formats. WhatsApp requires +6512345678 no spaces.
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
  if (digits.length >= 8 && digits.length <= 15) return '+' + digits;
  return '';
}
""" + COOLDOWN_JS_SNIPPET.replace("__SELF_WORKFLOW__", "post_trial_nurture") + BLACKLIST_JS_SNIPPET + r"""

// Rival-workflow guard — don't trial-nurture if reorder_reminder sent recently.
// Janani case (2026-05-20): she got reorder May 1 + D21 May 11 = 2 reorder-flavoured nudges in 10d.
// 14d window catches the reorder→D21 pile-on without breaking the D7→D14→D21 own cadence.
const NURTURE_RIVAL_DAYS = 14;
const NURTURE_RIVAL_MS = NURTURE_RIVAL_DAYS * 24 * 60 * 60 * 1000;
const NURTURE_RIVALS = new Set(['reorder_reminder']);
const NURTURE_RIVAL_BY_PHONE = new Map();
let _nrRows = [];
try { _nrRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _nrRows = $('Read Global Sent Log').all(); }
  catch (e2) { _nrRows = []; }
}
for (const it of _nrRows) {
  const s = it.json;
  if (!NURTURE_RIVALS.has(String(s.workflow || ''))) continue;
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  const prev = NURTURE_RIVAL_BY_PHONE.get(p) || 0;
  if (t > prev) NURTURE_RIVAL_BY_PHONE.set(p, t);
}
function isInRivalNudgeWindow(phone) {
  const last = NURTURE_RIVAL_BY_PHONE.get(phone);
  if (!last) return false;
  return (Date.now() - last) < NURTURE_RIVAL_MS;
}

// Parse order_date (usually ISO string) to days since
function daysSinceOrder(orderDateStr) {
  const orderDate = new Date(orderDateStr);
  const today = new Date();
  const diff = today.getTime() - orderDate.getTime();
  return Math.floor(diff / (1000 * 60 * 60 * 24));
}

// line_items_json is stored as a stringified JSON array in the orders sheet.
// Parse defensively — malformed rows return an empty array rather than throwing.
function parseLineItems(o) {
  const raw = o.line_items_json;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw) || []; } catch (e) { return []; }
}

// Detect if customer is already a subscriber (ANY order with discount code starting with "Subscription")
function isActiveSubscriber(custOrders) {
  return custOrders.some(o => {
    const code = String(o.discount_code || '').trim();
    return code.toUpperCase().startsWith('SUBSCRIPTION');
  });
}

// Detect if customer converted (non-trial purchase after the trial order)
function hasConvertedSinceTrialOrder(trialOrderDate, custOrders) {
  const trialDate = new Date(trialOrderDate);
  for (const o of custOrders) {
    if (new Date(o.order_date) <= trialDate) continue; // not after trial
    const hasNonTrialItem = parseLineItems(o).some(
      li => !isTrialPack(li.sku || '', li.title || '')
    );
    if (hasNonTrialItem) return true;
  }
  return false;
}

const orders = $('Read Orders').all().map(it => it.json);

// Rolling sent-log dedup — (phone, step_num) pairs we've ever sent.
// Same pattern as Reorder Reminder v2 sent-log (post-2026-04-23 incident).
const ALREADY_SENT_KEYS = new Set();
for (const it of $('Read Post-Trial Sent').all()) {
  const s = it.json;
  const p = normalizePhone(s.phone);
  const step = String(s.step_num || '').trim();
  if (p && step) ALREADY_SENT_KEYS.add(p + '|' + step);
}

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
const todayStr = today.toISOString().split('T')[0]; // YYYY-MM-DD

const stats = {
  total_orders: orders.length,
  unique_buyers: ordersByKey.size,
  d7_sent: 0,
  d14_sent: 0,
  d21_sent: 0,
  skipped_no_trial: 0,
  skipped_subscription: 0,
  skipped_converted: 0,
  skipped_no_phone: 0,
  skipped_already_sent: 0,
};

const candidates = [];

for (const [key, custOrders] of ordersByKey) {
  custOrders.sort((a, b) => new Date(b.order_date) - new Date(a.order_date));

  // Find the EARLIEST trial pack order for this customer
  let trialOrder = null;
  for (const o of custOrders.slice().reverse()) { // iterate from oldest
    const hasTrialItem = parseLineItems(o).some(
      li => isTrialPack(li.sku || '', li.title || '')
    );
    if (hasTrialItem) {
      trialOrder = o;
      break;
    }
  }

  if (!trialOrder) { stats.skipped_no_trial++; continue; }

  // Days since the trial order
  const daysSince = daysSinceOrder(trialOrder.order_date);
  let stepNum = null;
  if (daysSince === 7) stepNum = 1;
  else if (daysSince === 14) stepNum = 2;
  else if (daysSince === 21) stepNum = 3;
  if (!stepNum) continue; // not on a trigger day

  // Exclusion: subscriber
  if (isActiveSubscriber(custOrders)) { stats.skipped_subscription++; continue; }

  // Exclusion: already converted
  if (hasConvertedSinceTrialOrder(trialOrder.order_date, custOrders)) { stats.skipped_converted++; continue; }

  // Exclusion: missing phone
  const phone = normalizePhone(trialOrder.phone);
  if (!phone) { stats.skipped_no_phone++; continue; }

  // Exclusion: already sent this step to this phone (permanent dedup)
  if (ALREADY_SENT_KEYS.has(phone + '|' + stepNum)) {
    stats.skipped_already_sent++;
    continue;
  }

  // Exclusion: global 7-day cooldown (customer messaged by ANY workflow recently)
  if (isOverFrequencyCap(phone)) {
    stats.skipped_global_cooldown = (stats.skipped_global_cooldown || 0) + 1;
    continue;
  }

  // Exclusion: reorder_reminder fired for this phone in last 14d (rival nudge guard)
  if (isInRivalNudgeWindow(phone)) {
    stats.skipped_rival_nudge = (stats.skipped_rival_nudge || 0) + 1;
    continue;
  }

  // Exclusion: repo-versioned blacklist (BLACKLIST.txt). Hard-stop opt-outs.
  if (isBlacklisted(phone)) {
    stats.skipped_blacklist = (stats.skipped_blacklist || 0) + 1;
    continue;
  }

  const firstName = trialOrder.first_name || 'pawrent';
  const petName = trialOrder.pet_name || 'your furkid';
  const cityOrArea = trialOrder.city || 'your place';

  // 3-burst pattern: short hello → founder identification → open question.
  // No promo codes, no shop links — replies route to humans who pitch contextually.
  let msg1 = '', msg2 = '', msg3 = '';
  if (stepNum === 1) {
    msg1 = `hihi 🐾`;
    msg2 = `yash & nic here from bon pet, we're the founders 🙂`;
    msg3 = `saw your trial pack went out about a week ago, just wanted to check in - how's your furkid doing? 🐾 would love to hear back from u, any feedback for us? anything we can improve?`;
  } else if (stepNum === 2) {
    msg1 = `hihi 🐾`;
    msg2 = `yash & nic here from bon pet 🙂`;
    msg3 = `been ~2 weeks since your trial pack - how's your furkid taking to it? if you're thinking about a regular pack lmk, happy to recommend something based on weight + activity 🐾`;
  } else if (stepNum === 3) {
    msg1 = `hihi 🐾`;
    msg2 = `yash & nic here from bon pet 🙂`;
    msg3 = `last nudge from us - if your furkid liked the trial and you'd like to keep them on fresh food, just say the word and i'll sort you out. if not, no worries at all, we won't keep buzzing 💛`;
  }

  const baseFields = {
    customer_email: trialOrder.email,
    customer_name: firstName,
    customer_phone: phone,
    pet_name: petName,
    phone: phone,
    first_name: firstName,
    sent_at: new Date().toISOString(),
    trial_order_id: trialOrder.order_id,
    trial_order_date: trialOrder.order_date,
    days_since: daysSince,
    step_num: stepNum,
    workflow: 'post_trial_nurture',
    template: 'D' + daysSince,
    order_id: trialOrder.order_id,
    notes: 'step=' + stepNum,
  };

  // Emit 3 items in burst order. HTTP Send WA processes sequentially at 2s/item via batching.
  // Only seq===3 propagates to Log Sent (Skip Header filters), so dedup stays per (phone, step_num).
  const msgs = [msg1, msg2, msg3];
  for (let i = 0; i < 3; i++) {
    const seq = i + 1;
    const liveMsg = msgs[i];
    const dryPrefix = (seq === 1)
      ? `🧪 [DRY · D${daysSince} → ${firstName} ${phone} · ${seq}/3]\n`
      : `[${seq}/3]\n`;
    candidates.push({
      ...baseFields,
      seq: seq,
      target_phone: DRY_RUN ? YASH_PHONE : phone,
      message: DRY_RUN ? dryPrefix + liveMsg : liveMsg,
    });
  }

  if (stepNum === 1) stats.d7_sent++;
  else if (stepNum === 2) stats.d14_sent++;
  else if (stepNum === 3) stats.d21_sent++;
}

const diag = [
  `📊 *Funnel*`,
  `• Orders read: ${stats.total_orders}`,
  `• Unique buyers: ${stats.unique_buyers}`,
  `• Skipped (no trial): ${stats.skipped_no_trial}`,
  `• Skipped (subscriber): ${stats.skipped_subscription}`,
  `• Skipped (converted): ${stats.skipped_converted}`,
  `• Skipped (no phone): ${stats.skipped_no_phone}`,
  `• Skipped (already sent this step): ${stats.skipped_already_sent}`,
  `• Skipped (global 7d cooldown): ${stats.skipped_global_cooldown || 0}`,
  `• Skipped (rival 14d — reorder_reminder): ${stats.skipped_rival_nudge || 0}`,
  `• Skipped (blacklist): ${stats.skipped_blacklist || 0}`,
  ``,
  `📬 *Sends this run*`,
  `• D7 (how's it going): ${stats.d7_sent}`,
  `• D14 (full pack): ${stats.d14_sent}`,
  `• D21 (last chance): ${stats.d21_sent}`,
  `• Total sends: ${stats.d7_sent + stats.d14_sent + stats.d21_sent}`,
];

const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
const totalSends = stats.d7_sent + stats.d14_sent + stats.d21_sent;

if (totalSends === 0) {
  return [{ json: {
    target_phone: YASH_PHONE,
    is_header: true,
    message: `🔍 *Post-Trial Nurture — ${modeTag}*\n📅 ` + new Date().toISOString().slice(0,10) +
             `\n\n0 candidates today.\n\n${diag.join('\n')}`,
  }}];
}

candidates.unshift({
  target_phone: YASH_PHONE,
  is_header: true,
  message: `🔍 *Post-Trial Nurture — ${modeTag}*\n📅 ` + new Date().toISOString().slice(0,10) +
           `\n\n${totalSends} customer(s) will be messaged ⬇️\n\n${diag.join('\n')}`,
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


def schedule_node():
    """Cron: daily at 10 AM SGT"""
    return {
        "parameters": {"rule": {"interval": [{"triggerAtHour": 10}]}},
        "id": uid(), "name": "Daily 10AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 100],
    }


def webhook_node():
    """Manual trigger webhook for testing"""
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
    """Read from Google Sheets (Customer Orders DB)"""
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
    """Compute trial candidates for days 7/14/21"""
    return {
        "parameters": {"jsCode": CODE_JS},
        "id": uid(), "name": "Compute Trial Candidates (D7/D14/D21)",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [720, 200],
    }


def send_wa_node():
    """Send WhatsApp message via thebonpet.com WA endpoint.
    Batching: 1 item / 2000ms — produces 2s gap between messages, so each customer
    experiences a tight ~4s 3-burst (msg1 → 2s → msg2 → 2s → msg3) before the next
    customer's burst starts."""
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
            "options": {
                "batching": {"batch": {"batchSize": 1, "batchInterval": 2000}},
            },
        },
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [960, 200],
        "onError": "continueRegularOutput",
        "disabled": SEND_WA_DISABLED,
    }


def skip_header_filter_node():
    # Reaches back to the upstream Code node (NOT $input.all()) so phone/customer
    # fields survive the HTTP Send WA response replacement — see memory
    # `feedback_n8n_http_input_passthrough`. Also drops seq 1 + 2 so Log Sent only
    # records one row per (phone, step_num).
    js = "return $('Compute Trial Candidates (D7/D14/D21)').all().filter(it => !it.json.is_header && it.json.seq === 3);"
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
            "sheetName": {"__rl": True, "value": POST_TRIAL_SENT_GID, "mode": "list",
                          "cachedResultName": POST_TRIAL_SENT_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={POST_TRIAL_SENT_GID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "schema": [{"id": h, "displayName": h, "required": False,
                            "display": True, "type": "string"}
                           for h in ["phone", "step_num", "sent_at", "trial_order_id", "first_name", "days_since"]],
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
read_sent_log = gs_read_node("Read Post-Trial Sent", POST_TRIAL_SENT_GID, POST_TRIAL_SENT_TAB, [480, 300])
read_sent_log["executeOnce"] = True
# CRITICAL: alwaysOutputData so an empty sent-log (first run) doesn't break the chain.
# Without this, 0 rows → 0 items → Code node never fires.
read_sent_log["alwaysOutputData"] = True
read_global = read_global_sent_log_node([480, 500])
code = code_node()
send_wa = send_wa_node()
skip_header = skip_header_filter_node()
log_sent = log_sent_node()
log_global = append_global_sent_log_node([1680, 300])
pass_header = pass_header_only_node()
send_telegram = telegram_send_node("Send Telegram Weslee", [1440, 500])
send_telegram_lc = telegram_launchcycle_node("Send Telegram LaunchCycle", [1440, 620])

nodes = [schedule, webhook, read_orders, read_sent_log, read_global, code,
         send_wa, skip_header, log_sent, log_global, pass_header, send_telegram, send_telegram_lc]

connections = {
    schedule["name"]: {"main": [[{"node": read_orders["name"], "type": "main", "index": 0}]]},
    webhook["name"]:  {"main": [[{"node": read_orders["name"], "type": "main", "index": 0}]]},
    read_orders["name"]: {"main": [[{"node": read_sent_log["name"], "type": "main", "index": 0}]]},
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
    "name": WF_NAME,
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
}

if __name__ == "__main__":
    if WF_ID and WF_ID != "None":
        # Update existing workflow
        print(f"Updating existing workflow {WF_ID}...")
        status, body = http("PUT", f"/workflows/{WF_ID}", payload)
        print(f"PUT /workflows/{WF_ID} → HTTP {status}")
        wf_id = WF_ID
    else:
        # Create new workflow
        print(f"Creating new workflow '{WF_NAME}'...")
        status, body = http("POST", "/workflows", payload)
        print(f"POST /workflows → HTTP {status}")
        resp = json.loads(body)
        wf_id = resp.get("id")
        if not wf_id:
            print("❌ Failed to create workflow")
            print(body[:500])
            exit(1)
        print(f"✅ Workflow created: {wf_id}")

        # Transfer to team project
        print(f"Transferring to team project {TEAM}...")
        status, body = http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
        print(f"PUT /transfer → HTTP {status}")

    print()
    print(f"✅ Workflow URL: https://n8n.thebonpet.com/workflow/{wf_id}")
    print(f"Manual webhook: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print()
    print("📝 FOR DRY-RUN TEST (REQUIRED before activating):")
    print("1. Open the workflow in the n8n UI")
    print("2. Click on 'Compute Trial Candidates (D7/D14/D21)' Code node")
    print("3. Find the line: const DRY_RUN = false;")
    print("4. Change to: const DRY_RUN = true;")
    print("5. Click 'Test' node")
    print("6. Click 'Execute Workflow' (top right)")
    print("7. Check messages sent to your WA (+6581394225) — they should be prefixed with '🧪 DRY RUN'")
    print("8. Verify the structure and tone look good (no AI-speak, brand voice correct)")
    print()
    print("✅ THEN GO LIVE:")
    print("1. Toggle DRY_RUN back to false")
    print("2. Click 'Test' → 'Execute Workflow' once more to verify live sends work")
    print("3. Click the toggle at top-right to 'Active' (workflow auto-runs daily at 10 AM SGT)")
    print()
    print("Schedule: Daily 10 AM SGT (Asia/Singapore)")
    print("Manual trigger: POST to https://n8n.thebonpet.com/webhook/trigger-post-trial-now")

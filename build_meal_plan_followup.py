#!/usr/bin/env python3
"""Meal Plan Follow-up — WhatsApp D3/D7.

Nic/Yash ask (LaunchCycle group, 2026-07-29): after the personalised meal plan
goes out on WhatsApp, nudge again at 3 days and 7 days if no order landed.
Goal is cutting drop-off between "plan delivered" and "order placed".

Architecture:
  Daily 11:20 SGT ┐
                  ├→ Read Meal Planner Leads → Read Orders → Read Global Sent Log
  Manual Webhook  ┘   → Filter Recent Sent Log (45d, native)
                      → Compute Follow-ups (D3/D7) → Send WA → Skip Header → Log Global Sent
                                                   └→ Pass Header Only → Telegram

Eligibility (Compute Follow-ups):
  - lead has `plan_sent_at` (written by the "Mark Plan Sent" node the sibling
    patch_meal_planner_plan_sent_marker.py adds to "TBP Meal Planner Leads").
    No marker = no follow-up, so historical leads never get retro-blasted.
  - resolvable SG phone from the `whatsapp` column
  - no order placed since the plan went out (matched on email OR phone)
  - not an active subscriber (Subscription* discount code AND ordered within 42d)
  - step not already sent (wa_sent_log, workflow='meal_plan_followup')
  - D7 requires D3 to be >= 3 days old, so a delayed D3 never stacks
  - plan older than MAX_AGE_DAYS is stale, dropped
  - 7-day global cross-workflow cooldown, OWN sends excluded (the D3→D7 gap is
    4 days by design, the cooldown would otherwise eat every D7)
  - BLACKLIST.txt hard opt-outs

Steps are day-floors (>= 3, >= 7) rather than exact-day equality, so a lead
skipped by a cooldown or a failed run gets picked up on the next run instead of
being silently lost.

DRY_RUN=true routes every send to Yash with a 🧪 prefix. Flip in CODE_JS and
re-run this script to go live.
"""
import json, uuid, os, subprocess, urllib.request, urllib.error
from _notify import telegram_send_node, telegram_launchcycle_node
from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node,
    native_filter_recent_sent_log_node,
)
from _blacklist import BLACKLIST_JS_SNIPPET

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
# The meal planner leads sheet lives on Nic's Drive and needs his credential.
GS_CRED_NIC = {"id": "7JXUrNbjnmm04LU8", "name": "NIC Google Sheets account"}

ORDERS_SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
ORDERS_GID = 0
LEADS_SHEET_ID = "1KlF4IYw5jjjCzLISFdOdhd5RYR5JxGZFttHb0BoJPtM"
LEADS_GID = 2029633008

WF_NAME = "Meal Plan Follow-up — WhatsApp D3/D7"
WF_ID = "NJ3EctLBcHhaWV3I"
WEBHOOK_PATH = "trigger-meal-plan-followup"
SENT_LOG_WINDOW_DAYS = 45  # must exceed MAX_AGE_DAYS so D3 rows are still visible at D7

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()

CODE_JS = r"""// Meal Plan Follow-up — one WA nudge at D3, one at D7, only if no order landed.
const DRY_RUN = true;
const YASH_PHONE = '+6581394225';
const WORKFLOW_TAG = 'meal_plan_followup';
const MAX_AGE_DAYS = 21;        // plan older than this is stale, stop chasing
const D3_DAYS = 3;
const D7_DAYS = 7;
const MIN_GAP_DAYS = 3;         // never two nudges closer than this
const DAILY_CAP = 40;

function normalizePhone(p) {
  if (!p) return '';
  let s = String(p).replace(/\s/g, '').trim();
  if (s.startsWith('+')) return '+' + s.slice(1).replace(/\D/g, '');
  const digits = s.replace(/\D/g, '');
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  if (digits.length >= 8 && digits.length <= 15) return '+' + digits;
  return '';
}
""" + BLACKLIST_JS_SNIPPET + r"""

const DAY_MS = 24 * 60 * 60 * 1000;
const now = Date.now();

// --- sent-log indexes -------------------------------------------------------
// One pass over the (already date-bounded) log builds three things:
//   OWN_SENT      phone|template -> permanent per-step dedup
//   OWN_LAST      phone -> last time THIS workflow messaged them (min-gap guard)
//   OTHER_LAST    phone -> last time ANY OTHER workflow messaged them (7d cooldown)
// Own sends are deliberately excluded from the cooldown map: D3 -> D7 is a
// 4-day gap, so a naive global cooldown would suppress every single D7.
const OWN_SENT = new Set();
const OWN_LAST = new Map();
const OTHER_LAST = new Map();
let _sentRows = [];
try { _sentRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _sentRows = $('Read Global Sent Log').all(); } catch (e2) { _sentRows = []; }
}
for (const it of _sentRows) {
  const s = it.json || {};
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  if (String(s.workflow || '') === WORKFLOW_TAG) {
    const tmpl = String(s.template || '').trim();
    if (tmpl) OWN_SENT.add(p + '|' + tmpl);
    if (t > (OWN_LAST.get(p) || 0)) OWN_LAST.set(p, t);
  } else if (t > (OTHER_LAST.get(p) || 0)) {
    OTHER_LAST.set(p, t);
  }
}
const GLOBAL_COOLDOWN_MS = 7 * DAY_MS;
function isInForeignCooldown(phone) {
  const last = OTHER_LAST.get(phone);
  return last ? (now - last) < GLOBAL_COOLDOWN_MS : false;
}

// --- order index ------------------------------------------------------------
// Leads are keyed on email OR phone, orders on both, so index each separately.
// A lead counts as converted if ANY order landed at or after the plan went out
// (1h of slack absorbs clock skew between the sheet write and Shopify's stamp).
const ORDERS_BY_EMAIL = new Map();
const ORDERS_BY_PHONE = new Map();
function pushOrder(map, key, order) {
  if (!key) return;
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(order);
}
for (const it of $('Read Orders').all()) {
  const o = it.json || {};
  const t = new Date(o.order_date || 0).getTime();
  if (!t) continue;
  const rec = { t: t, code: String(o.discount_code || '').trim().toUpperCase() };
  pushOrder(ORDERS_BY_EMAIL, String(o.email || '').toLowerCase().trim(), rec);
  pushOrder(ORDERS_BY_PHONE, normalizePhone(o.phone), rec);
}
function ordersFor(email, phone) {
  return (ORDERS_BY_EMAIL.get(email) || []).concat(ORDERS_BY_PHONE.get(phone) || []);
}
// Subscriber rule per CLAUDE.md: any order with a "Subscription*" code, AND a
// recent enough order to still be inside the 6-week (42d) max cadence.
function isActiveSubscriber(orders) {
  if (!orders.some(o => o.code.startsWith('SUBSCRIPTION'))) return false;
  const last = orders.reduce((m, o) => Math.max(m, o.t), 0);
  return last > 0 && (now - last) <= 42 * DAY_MS;
}

// --- copy -------------------------------------------------------------------
const qs = (o) => Object.keys(o).map(k => encodeURIComponent(k) + '=' + encodeURIComponent(o[k])).join('&');
const utm = (content) => qs({
  utm_source: 'meal-planner', utm_medium: 'whatsapp',
  utm_campaign: 'meal-plan-followup', utm_content: content,
});
// Apex domain, not www: www 301s and we don't want the UTMs riding a redirect.
const planLink = (content) => 'https://thebonpet.com/pages/get-started?' + utm(content);
// Sampler discount link mirrors the one "TBP Meal Planner Leads" already sends.
// Discount auto-applies on tap, so the code never appears in the message body.
const samplerLink = (species) => {
  const path = (species === 'cat')
    ? '/products/gently-cooked-sampler-set-for-cats'
    : '/products/gently-cooked-sampler-set-for-dogs';
  return 'https://thebonpet.com/discount/SAMPLER%253C3THEBONPET?redirect='
    + encodeURIComponent(path + '?' + utm('d7-sampler'));
};

function buildMessage(step, name, petNames, species) {
  if (step === 1) {
    return 'hi ' + name + '! 🐾 yash & nic here from The Bon Pet\n\n'
      + 'we put together ' + petNames + "'s meal plan a few days back but noticed the order hasn't come through yet. anything we can help with? happy to adjust the portions, swap proteins, or work around your delivery timing 🙂\n\n"
      + "your plan's still saved here: " + planLink('d3-plan');
  }
  return 'hi ' + name + '! 🐾 last nudge from us about ' + petNames + "'s meal plan\n\n"
    + 'if a full plan feels like a big first step, try a sampler set instead. small packs of a few proteins so you can see what ' + petNames + ' actually goes for first, first-timer pricing applies automatically at checkout:\n'
    + samplerLink(species) + '\n\n'
    + "or if the timing just isn't right, no worries at all, we won't keep buzzing 💛";
}

// --- candidate selection ----------------------------------------------------
const leads = $('Read Meal Planner Leads').all().map(it => it.json || {});
const stats = {
  leads_read: leads.length, orders_read: $('Read Orders').all().length,
  d3: 0, d7: 0,
  skipped_no_plan_sent: 0, skipped_no_phone: 0, skipped_too_early: 0,
  skipped_stale: 0, skipped_ordered: 0, skipped_subscriber: 0,
  skipped_already_sent: 0, skipped_min_gap: 0, skipped_foreign_cooldown: 0,
  skipped_blacklist: 0, skipped_cap: 0,
};
const candidates = [];
const seenPhones = new Set();

for (const r of leads) {
  const planSent = new Date(r.plan_sent_at || 0).getTime();
  if (!planSent) { stats.skipped_no_plan_sent++; continue; }

  const phone = normalizePhone(r.whatsapp);
  if (!phone) { stats.skipped_no_phone++; continue; }
  if (seenPhones.has(phone)) continue;  // one nudge per person per run

  const daysSince = Math.floor((now - planSent) / DAY_MS);
  if (daysSince < D3_DAYS) { stats.skipped_too_early++; continue; }
  if (daysSince > MAX_AGE_DAYS) { stats.skipped_stale++; continue; }

  const step = daysSince >= D7_DAYS ? 2 : 1;
  const template = step === 1 ? 'D3' : 'D7';
  if (OWN_SENT.has(phone + '|' + template)) { stats.skipped_already_sent++; continue; }

  const email = String(r.email || '').toLowerCase().trim();
  const orders = ordersFor(email, phone);
  if (orders.some(o => o.t >= planSent - 3600000)) { stats.skipped_ordered++; continue; }
  if (isActiveSubscriber(orders)) { stats.skipped_subscriber++; continue; }

  const ownLast = OWN_LAST.get(phone);
  if (ownLast && (now - ownLast) < MIN_GAP_DAYS * DAY_MS) { stats.skipped_min_gap++; continue; }
  if (isInForeignCooldown(phone)) { stats.skipped_foreign_cooldown++; continue; }
  if (isBlacklisted(phone)) { stats.skipped_blacklist++; continue; }

  if (candidates.length >= DAILY_CAP) { stats.skipped_cap++; continue; }

  let pets = [];
  try { pets = JSON.parse(r.pets_json || '[]'); } catch (e) { pets = []; }
  // Trim: pawrents type trailing spaces into the planner and "Xiaomai 's plan" reads broken.
  const petNames = pets.map(p => String(p.name || '').trim()).filter(Boolean).join(' & ') || 'your furkid';
  const speciesList = pets.map(p => String(p.species || '').toLowerCase());
  const species = (speciesList.length && speciesList.every(s => s === 'cat')) ? 'cat' : 'dog';
  const name = String(r.first_name || '').trim() || 'there';
  const message = buildMessage(step, name, petNames, species);

  seenPhones.add(phone);
  if (step === 1) stats.d3++; else stats.d7++;
  candidates.push({
    target_phone: DRY_RUN ? YASH_PHONE : phone,
    message: DRY_RUN ? '🧪 [DRY · ' + template + ' → ' + name + ' ' + phone + ']\n' + message : message,
    phone: phone,
    workflow: WORKFLOW_TAG,
    template: template,
    sent_at: new Date().toISOString(),
    order_id: '',
    notes: 'session=' + (r.session_id || '') + ' days=' + daysSince,
    first_name: name,
    days_since: daysSince,
  });
}

const diag = [
  '📊 *Funnel*',
  '• Leads read: ' + stats.leads_read,
  '• Orders read: ' + stats.orders_read,
  '• Skipped (plan never sent): ' + stats.skipped_no_plan_sent,
  '• Skipped (no phone): ' + stats.skipped_no_phone,
  '• Skipped (< 3 days): ' + stats.skipped_too_early,
  '• Skipped (> ' + MAX_AGE_DAYS + ' days, stale): ' + stats.skipped_stale,
  '• Skipped (already ordered): ' + stats.skipped_ordered,
  '• Skipped (active subscriber): ' + stats.skipped_subscriber,
  '• Skipped (step already sent): ' + stats.skipped_already_sent,
  '• Skipped (min ' + MIN_GAP_DAYS + 'd gap): ' + stats.skipped_min_gap,
  '• Skipped (7d cooldown, other workflow): ' + stats.skipped_foreign_cooldown,
  '• Skipped (blacklist): ' + stats.skipped_blacklist,
  '• Skipped (daily cap ' + DAILY_CAP + '): ' + stats.skipped_cap,
  '',
  '📬 *Sends this run*',
  '• D3 (plan still saved): ' + stats.d3,
  '• D7 (sampler / last nudge): ' + stats.d7,
  '• Total: ' + (stats.d3 + stats.d7),
];

const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
const header = {
  target_phone: YASH_PHONE,
  is_header: true,
  message: '🔍 *Meal Plan Follow-up — ' + modeTag + '*\n📅 ' + new Date().toISOString().slice(0, 10)
    + '\n\n' + (candidates.length ? candidates.length + ' pawrent(s) will be messaged ⬇️' : '0 candidates today.')
    + '\n\n' + diag.join('\n'),
};

return [header].concat(candidates).map(c => ({ json: c }));
"""


def http(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def uid():
    return str(uuid.uuid4())


def schedule_node():
    # 11:20 SGT — off the hour-boundary cluster that OOM-killed n8n on 2026-06-10,
    # and clear of the 10:00 Post-Trial Nurture run.
    return {
        "parameters": {"rule": {"interval": [{"triggerAtHour": 11, "triggerAtMinute": 20}]}},
        "id": uid(), "name": "Daily 11:20 SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 100],
    }


def webhook_node():
    return {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH,
                       "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Trigger Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": uid(),
    }


def gs_read_node(name, sheet_id, sheet_name_cached, tab_gid, tab_name, position, cred=None):
    return {
        "parameters": {
            "documentId": {"__rl": True, "value": sheet_id, "mode": "list",
                           "cachedResultName": sheet_name_cached,
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"},
            "sheetName": {"__rl": True, "value": tab_gid, "mode": "list",
                          "cachedResultName": tab_name,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={tab_gid}"},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": cred or GS_CRED},
    }


def code_node():
    return {
        "parameters": {"jsCode": CODE_JS},
        "id": uid(), "name": "Compute Follow-ups (D3/D7)",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [960, 200],
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
            # ~5s between sends — WA rate limiting, see feedback_wa_broadcast_rate_limit.
            "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 5000}}},
        },
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1200, 200],
        "onError": "continueRegularOutput",
    }


def skip_header_node():
    # Reaches back to the Code node, not $input, because the HTTP response replaces
    # the item body and would strip phone/template. See feedback_n8n_http_input_passthrough.
    js = "return $('Compute Follow-ups (D3/D7)').all().filter(it => !it.json.is_header);"
    return {
        "parameters": {"jsCode": js},
        "id": uid(), "name": "Skip Header",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1440, 200],
    }


def pass_header_node():
    return {
        "parameters": {"jsCode": "return $input.all().filter(it => it.json.is_header);"},
        "id": uid(), "name": "Pass Header Only",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 460],
    }


schedule = schedule_node()
webhook = webhook_node()
read_leads = gs_read_node("Read Meal Planner Leads", LEADS_SHEET_ID, "TBP Meal Planner Leads",
                          LEADS_GID, "leads", [240, 200], cred=GS_CRED_NIC)
read_orders = gs_read_node("Read Orders", ORDERS_SHEET_ID, "Bon Pet — Customer Orders DB",
                           ORDERS_GID, "orders", [480, 200])
read_orders["executeOnce"] = True
read_orders["alwaysOutputData"] = True
read_global = read_global_sent_log_node([720, 200])
filter_recent = native_filter_recent_sent_log_node([720, 380], days=SENT_LOG_WINDOW_DAYS)
code = code_node()
send_wa = send_wa_node()
skip_header = skip_header_node()
log_global = append_global_sent_log_node([1680, 200])
pass_header = pass_header_node()
tg_weslee = telegram_send_node("Send Telegram Weslee", [1440, 460])
tg_launchcycle = telegram_launchcycle_node("Send Telegram LaunchCycle", [1440, 580])

nodes = [schedule, webhook, read_leads, read_orders, read_global, filter_recent, code,
         send_wa, skip_header, log_global, pass_header, tg_weslee, tg_launchcycle]

connections = {
    schedule["name"]: {"main": [[{"node": read_leads["name"], "type": "main", "index": 0}]]},
    webhook["name"]: {"main": [[{"node": read_leads["name"], "type": "main", "index": 0}]]},
    read_leads["name"]: {"main": [[{"node": read_orders["name"], "type": "main", "index": 0}]]},
    read_orders["name"]: {"main": [[{"node": read_global["name"], "type": "main", "index": 0}]]},
    read_global["name"]: {"main": [[{"node": filter_recent["name"], "type": "main", "index": 0}]]},
    filter_recent["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
    code["name"]: {"main": [[
        {"node": send_wa["name"], "type": "main", "index": 0},
        {"node": pass_header["name"], "type": "main", "index": 0},
    ]]},
    send_wa["name"]: {"main": [[{"node": skip_header["name"], "type": "main", "index": 0}]]},
    skip_header["name"]: {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
    pass_header["name"]: {"main": [[
        {"node": tg_weslee["name"], "type": "main", "index": 0},
        {"node": tg_launchcycle["name"], "type": "main", "index": 0},
    ]]},
}

payload = {
    "name": WF_NAME,
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
}

if __name__ == "__main__":
    if WF_ID:
        status, body = http("PUT", f"/workflows/{WF_ID}", payload)
        print(f"PUT /workflows/{WF_ID} → HTTP {status}")
        wf_id = WF_ID
        if status != 200:
            print(str(body)[:800])
            raise SystemExit(1)
    else:
        status, body = http("POST", "/workflows", payload)
        print(f"POST /workflows → HTTP {status}")
        if status not in (200, 201):
            print(str(body)[:800])
            raise SystemExit(1)
        wf_id = body.get("id")
        print(f"✅ Created {wf_id} — set WF_ID = \"{wf_id}\" in this script for future edits")
        # No /transfer call: self-hosted n8n has no team projects (the old Cloud
        # TEAM id 404s), everything lives in the personal project post-migration.

    print(f"\n✅ https://n8n.thebonpet.com/workflow/{wf_id}")
    print(f"Manual trigger: POST https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print("\nWorkflow is left INACTIVE. DRY_RUN=true routes all sends to Yash.")
    print("Go live: flip DRY_RUN=false in CODE_JS, re-run this script, then activate.")

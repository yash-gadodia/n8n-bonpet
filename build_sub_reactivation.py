#!/usr/bin/env python3
"""Sub Reactivation — daily WA outreach to paused/cancelled subscribers in the backlog.

Cohorts (per customer, one-shot via dedup sheet):
  A) PAUSED 21-90 days, never reactivation-messaged → light check-in + 30% off
  B) CANCELLED 30-180 days, never reactivation-messaged → founder note + 30% off

Hard guardrails (post 2026-05-03 broadcast spam + 2026-05-12 winback dup incidents):
  - DRY_RUN default True. First run: Telegram Yash the counts + sample. No customer sends.
  - Per-customer dedup via sub_reactivation_sent sheet tab (gid 900900).
  - Global 7d WA cooldown across all marketing workflows (_sent_log.COOLDOWN_JS_SNIPPET).
  - BLACKLIST.txt opt-out list.
  - 5 + 5 cap per day per cohort.
  - WA HTTP node uses 5s batching (5000ms between sends).

Note: WELCOMEBACK is 1-use-per-customer in Shopify. The orders tab schema doesn't
carry the discount code column so we don't pre-filter customers who've already
redeemed. Worst case (per the screenshot, 5 customers used the code ever): a
customer sees "code already used" at checkout. Harmless. Add Shopify Admin API
lookup if this becomes a real problem.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from _blacklist import BLACKLIST_JS_SNIPPET
from _notify import telegram_send_node
from _sent_log import (
    COOLDOWN_JS_SNIPPET,
    append_global_sent_log_node,
    filter_recent_sent_log_node,
    read_global_sent_log_node,
)


API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Sub Reactivation - WhatsApp"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "sub-reactivation-manual-9b4c2e7d3a"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
SUB_SHEET_GID       = 700700  # subscribers (live, webhook-updated)
CUSTOMERS_TAB_GID   = 100100  # customers (PII workaround)
ORDERS_TAB_GID      = 0       # orders (for WELCOMEBACK redemption check)
REACT_SENT_GID      = 900900  # sub_reactivation_sent (per-customer dedup)
REACT_SENT_TAB      = "sub_reactivation_sent"
REACT_SENT_HEADERS  = ["contract_id", "customer_id", "email", "phone",
                       "first_name", "cohort", "status_at_send", "sent_at",
                       "dry_run", "message_template"]
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()
YASH_PHONE = "+6581394225"

# === KILL SWITCHES ===
DRY_RUN          = False  # 2026-05-17: live with Yash's explicit go-ahead after DRY review.
SEND_WA_DISABLED = False  # Hard-off for the WA send node (use during diagnostic runs).

# === COHORT CONFIG ===
# Windows are measured by days_since_last_paid_sub_order (behavior),
# not by Subscribers-sheet received_at (which can fire on routine webhooks).
PAUSED_MIN_DAYS    = 21
PAUSED_MAX_DAYS    = 90
CANCELLED_MIN_DAYS = 14
CANCELLED_MAX_DAYS = 365
DAILY_CAP_PER_COHORT = 5


PAUSED_MSG_TEMPLATE = """Hi {first_name}! 🐾

Yash & Nic here, just doing a check-in 🐾

A few things have changed since you paused:

✅ Switching to a dedicated cold-chain fleet for more reliable deliveries
✅ Free self-collection added if you're near Siglap
✅ Faster turnaround with our new order system
✅ Site got a refresh (much easier to subscribe + manage now)

Whenever the time feels right, we've set aside 30% off for you:
*WELCOMEBACK<3THEBONPET* 🎁

No rush, just wanted to keep you in the loop 💛

❤️ Yash & Nic"""

CANCELLED_MSG_TEMPLATE = """Hi {first_name}! 🐾

Yash & Nic from The Bon Pet. It's been a while since you cancelled and we wanted to send a proper note, not a sales pitch.

A handful of things have changed since you last tried us:

✅ Switching to a dedicated cold-chain fleet for more reliable deliveries
✅ Free self-collection if you're near Siglap
✅ Faster, more accurate fulfilment via our new order system
✅ Fully revamped site (way easier to browse + manage)

If any of that makes you curious, here's 30% off whenever you fancy another go:
*WELCOMEBACK<3THEBONPET* 🎁

Honestly though, even a one-liner on what didn't work for you would help us more than anything 🙏

❤️ Yash & Nic"""


COMPUTE_JS = (
    r"""// Sub Reactivation candidate computer.
const DRY_RUN = __DRY_RUN__;
const PAUSED_MIN_DAYS    = __PAUSED_MIN__;
const PAUSED_MAX_DAYS    = __PAUSED_MAX__;
const CANCELLED_MIN_DAYS = __CANCELLED_MIN__;
const CANCELLED_MAX_DAYS = __CANCELLED_MAX__;
const DAILY_CAP_PER_COHORT = __CAP__;
const PAUSED_TEMPLATE    = __PAUSED_TEMPLATE_JSON__;
const CANCELLED_TEMPLATE = __CANCELLED_TEMPLATE_JSON__;

const YASH_PHONE = '+6581394225';
const DAY_MS = 24 * 60 * 60 * 1000;
const nowMs = Date.now();

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
function normEmail(e) { return String(e || '').toLowerCase().trim(); }
""" + COOLDOWN_JS_SNIPPET + BLACKLIST_JS_SNIPPET + r"""

// --- Load all source nodes ---
const subRows   = $('Read Subscribers').all().map(it => it.json);
const custRows  = $('Read Customers').all().map(it => it.json);
const sentRows  = $('Read Reactivation Sent').all().map(it => it.json);
const orderRows = $('Read Orders').all().map(it => it.json);

// --- Build lookup: customer_id / email -> {phone, first_name} ---
const custByEmail   = new Map();
const custByCustId  = new Map();
for (const c of custRows) {
  const email = normEmail(c.email);
  const cid = String(c.customer_id || c.id || '').trim();
  const phone = normalizePhone(c.phone || c.default_address_phone || '');
  const firstName = String(c.first_name || '').trim();
  const record = { email, customer_id: cid, phone, first_name: firstName };
  if (email) custByEmail.set(email, record);
  if (cid) custByCustId.set(cid, record);
}

// --- Build dedup set: customers already reactivation-messaged (any cohort, any time) ---
const alreadyReactivated = new Set();
for (const r of sentRows) {
  const cid = String(r.customer_id || '').trim();
  const contractId = String(r.contract_id || '').trim();
  if (cid) alreadyReactivated.add('cust:' + cid);
  if (contractId) alreadyReactivated.add('contract:' + contractId);
}

// --- Build lookup: email + customer_id -> {last_sub_order_ts, last_any_order_ts} ---
// We prefer the last SUBSCRIPTION order (is_subscription === true/'TRUE') as the
// staleness anchor — that's the moment they were last on the subscription cadence.
// Fall back to last-any-order if they have no sub order on file.
//
// EMAIL is the primary join key: customer_id is empty on most order rows
// (~93% of orders in the sheet have no customer_id), but email coverage is
// near-100%. customer_id remains a fallback.
const orderInfoByEmail = new Map();
const orderInfoByCust  = new Map();
function parseDate(d) {
  if (!d) return null;
  const t = Date.parse(d);
  return isNaN(t) ? null : t;
}
function upsertOrderInfo(map, key, t, isSub) {
  let rec = map.get(key);
  if (!rec) {
    rec = { last_sub_ts: null, last_any_ts: null };
    map.set(key, rec);
  }
  if (t > (rec.last_any_ts || 0)) rec.last_any_ts = t;
  if (isSub && t > (rec.last_sub_ts || 0)) rec.last_sub_ts = t;
}
for (const o of orderRows) {
  const t = parseDate(o.order_date);
  if (!t) continue;
  const isSub = (o.is_subscription === true) ||
                (String(o.is_subscription || '').toUpperCase() === 'TRUE');
  const em  = normEmail(o.email);
  const cid = String(o.customer_id || '').trim();
  if (em)  upsertOrderInfo(orderInfoByEmail, em, t, isSub);
  if (cid) upsertOrderInfo(orderInfoByCust,  cid, t, isSub);
}

// --- Dedupe sub rows by contract_id; one record per contract ---
const contracts = new Map();
for (const r of subRows) {
  if (!r.contract_id) continue;
  const contractId = String(r.contract_id).trim();
  if (contracts.has(contractId)) continue;
  contracts.set(contractId, {
    contract_id: contractId,
    customer_id: String(r.customer_id || '').trim(),
    email:       normEmail(r.email),
    status:      String(r.status || '').toUpperCase(),
    received_at: r.received_at ? new Date(r.received_at).getTime() : null,
    last_synced_at: r.last_synced_at ? new Date(r.last_synced_at).getTime() : null,
    cadence:     `${r.cadence_interval_count || ''}${String(r.cadence_interval || '').toLowerCase()}`,
  });
}

// --- Candidate selection ---
const stats = {
  contracts_total:        contracts.size,
  candidates_paused:      0,
  candidates_cancelled:   0,
  skip_status:            0,
  skip_no_order_history:  0,
  skip_window:            0,
  skip_already_sent:      0,
  skip_no_customer_match: 0,
  skip_no_phone:          0,
  skip_invalid_phone:     0,
  skip_blacklist:         0,
  skip_global_cooldown:   0,
  capped_out_paused:      0,
  capped_out_cancelled:   0,
  sending_paused:         0,
  sending_cancelled:      0,
};

const candidatesPaused = [];
const candidatesCancelled = [];

for (const c of contracts.values()) {
  const status = c.status;
  let cohort = null;
  let minDays = 0, maxDays = 0;
  if (status === 'PAUSED') {
    cohort = 'paused';
    minDays = PAUSED_MIN_DAYS;
    maxDays = PAUSED_MAX_DAYS;
  } else if (status === 'CANCELLED') {
    cohort = 'cancelled';
    minDays = CANCELLED_MIN_DAYS;
    maxDays = CANCELLED_MAX_DAYS;
  } else {
    stats.skip_status++;
    continue;
  }

  // Behavior-based staleness: days since LAST PAID ORDER OF ANY TYPE (sub OR one-off).
  // A paused/cancelled customer who still buys ala carte is engaged — we don't want
  // to send them a "we miss you" message when they ordered last week. The cohort
  // (paused/cancelled) determines WHICH message, but inactivity-of-all-orders
  // determines WHETHER to send.
  //
  // Email-first join (customer_id is sparse in orders sheet).
  const orderInfo = (c.email && orderInfoByEmail.get(c.email)) ||
                    (c.customer_id && orderInfoByCust.get(c.customer_id)) ||
                    null;
  const lastOrderTs = orderInfo ? orderInfo.last_any_ts : null;
  if (!lastOrderTs) { stats.skip_no_order_history++; continue; }
  const daysSince = (nowMs - lastOrderTs) / DAY_MS;
  if (daysSince < minDays || daysSince > maxDays) { stats.skip_window++; continue; }

  if (alreadyReactivated.has('cust:' + c.customer_id) ||
      alreadyReactivated.has('contract:' + c.contract_id)) {
    stats.skip_already_sent++;
    continue;
  }

  // Customer lookup (phone + first name) — prefer customer_id, fallback to email.
  const cust = (c.customer_id && custByCustId.get(c.customer_id)) ||
               (c.email && custByEmail.get(c.email));
  if (!cust) { stats.skip_no_customer_match++; continue; }

  const phone = cust.phone;
  if (!phone) { stats.skip_no_phone++; continue; }
  if (!/^\+\d{8,15}$/.test(phone)) { stats.skip_invalid_phone++; continue; }
  if (isBlacklisted(phone)) { stats.skip_blacklist++; continue; }
  if (isInGlobalCooldown(phone)) { stats.skip_global_cooldown++; continue; }

  const candidate = {
    contract_id:        c.contract_id,
    customer_id:        c.customer_id,
    email:              c.email,
    phone:              phone,
    first_name:         cust.first_name || 'there',
    cohort:             cohort,
    status_at_send:     status,
    days_since:         Math.round(daysSince),
    last_order_iso:     new Date(lastOrderTs).toISOString(),
    last_sub_order_iso: (orderInfo && orderInfo.last_sub_ts) ? new Date(orderInfo.last_sub_ts).toISOString() : null,
  };
  if (cohort === 'paused') candidatesPaused.push(candidate);
  else                     candidatesCancelled.push(candidate);
}

// Dedupe candidates by phone — one outbound per CUSTOMER, not per contract.
// A customer can have multiple subscription contracts (e.g. one for cat one for dog).
// Without this, Chan-with-3-paused-contracts would get 3 identical WAs in 50 seconds.
// Tie-break rules:
//   - Prefer PAUSED cohort (still partially with us) over CANCELLED
//   - Within same cohort, keep the candidate with the longest staleness
const byPhone = new Map();
function dedupCandidate(c) {
  const existing = byPhone.get(c.phone);
  if (!existing) { byPhone.set(c.phone, c); return; }
  if (existing.cohort === 'cancelled' && c.cohort === 'paused') { byPhone.set(c.phone, c); return; }
  if (existing.cohort === 'paused'    && c.cohort === 'cancelled') return;
  if (c.days_since > existing.days_since) byPhone.set(c.phone, c);
}
for (const c of candidatesPaused)    dedupCandidate(c);
for (const c of candidatesCancelled) dedupCandidate(c);

// Re-split into cohorts after dedup, for capping and routing.
const dedupedPaused = [];
const dedupedCancelled = [];
for (const c of byPhone.values()) {
  if (c.cohort === 'paused') dedupedPaused.push(c);
  else dedupedCancelled.push(c);
}
stats.candidates_paused    = dedupedPaused.length;
stats.candidates_cancelled = dedupedCancelled.length;

// Oldest-first within each cohort (most-stale customers get the touch first).
dedupedPaused.sort((a, b) => b.days_since - a.days_since);
dedupedCancelled.sort((a, b) => b.days_since - a.days_since);

const sendPaused    = dedupedPaused.slice(0, DAILY_CAP_PER_COHORT);
const sendCancelled = dedupedCancelled.slice(0, DAILY_CAP_PER_COHORT);
stats.capped_out_paused    = Math.max(0, dedupedPaused.length - sendPaused.length);
stats.capped_out_cancelled = Math.max(0, dedupedCancelled.length - sendCancelled.length);
stats.sending_paused    = sendPaused.length;
stats.sending_cancelled = sendCancelled.length;

function redact(name) {
  if (!name || name === 'there') return 'there';
  return name.length <= 2 ? name + '*' : (name.slice(0, 2) + '*'.repeat(Math.min(name.length - 2, 4)));
}

// Build outgoing items.
const out = [];
function pushCandidate(c) {
  const template = c.cohort === 'paused' ? PAUSED_TEMPLATE : CANCELLED_TEMPLATE;
  const message = template.replace(/\{first_name\}/g, c.first_name);
  out.push({
    json: {
      // Routing
      target_phone: DRY_RUN ? YASH_PHONE : c.phone,
      message:      DRY_RUN ? `🧪 [DRY · ${c.cohort} → ${redact(c.first_name)} ${c.phone.slice(0,4)}*** · ${c.days_since}d in status]\n${message}` : message,
      // Dedup-log columns
      contract_id:     c.contract_id,
      customer_id:     c.customer_id,
      email:           c.email,
      phone:           c.phone,
      first_name:      c.first_name,
      cohort:          c.cohort,
      status_at_send:  c.status_at_send,
      sent_at:         new Date().toISOString(),
      dry_run:         DRY_RUN ? 'true' : 'false',
      message_template: c.cohort === 'paused' ? 'sub_reactivation_paused' : 'sub_reactivation_cancelled',
      // wa_sent_log columns
      workflow:        'sub_reactivation',
      template:        c.cohort === 'paused' ? 'sub_reactivation_paused' : 'sub_reactivation_cancelled',
      order_id:        '',
      notes:           `cohort=${c.cohort} days_since=${c.days_since}`,
    }
  });
}
for (const c of sendPaused) pushCandidate(c);
for (const c of sendCancelled) pushCandidate(c);

// --- Header summary for Yash (Telegram only, posted regardless of cohort fan-out) ---
const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
const samplePaused    = sendPaused[0];
const sampleCancelled = sendCancelled[0];
const sampleBlock = (s, label) => s
  ? `\n*Sample ${label}* → ${redact(s.first_name)} (${s.phone.slice(0,4)}***, ${s.days_since}d since last order of any type):\n${(s.cohort === 'paused' ? PAUSED_TEMPLATE : CANCELLED_TEMPLATE).replace(/\{first_name\}/g, redact(s.first_name))}`
  : `\n*No ${label} candidates today.*`;

const headerMsg = `🔁 *Sub Reactivation — ${modeTag}*\n📅 ${new Date().toISOString().slice(0,10)}\n\n` +
  `🎯 *Sending today:* ${stats.sending_paused + stats.sending_cancelled}\n` +
  `   • Paused-stale: ${stats.sending_paused} (cap ${DAILY_CAP_PER_COHORT})\n` +
  `   • Cancelled-stale: ${stats.sending_cancelled} (cap ${DAILY_CAP_PER_COHORT})\n` +
  `   • Capped: ${stats.capped_out_paused + stats.capped_out_cancelled} (waiting)\n\n` +
  `📊 *Funnel* (staleness = days since last paid order, sub OR one-off)\n` +
  `• Contracts scanned: ${stats.contracts_total}\n` +
  `• Eligible paused (pre-cap): ${stats.candidates_paused}\n` +
  `• Eligible cancelled (pre-cap): ${stats.candidates_cancelled}\n` +
  `• Skip wrong status: ${stats.skip_status}\n` +
  `• Skip no order history: ${stats.skip_no_order_history}\n` +
  `• Skip outside window: ${stats.skip_window}\n` +
  `• Skip already reactivated: ${stats.skip_already_sent}\n` +
  `• Skip no customer match: ${stats.skip_no_customer_match}\n` +
  `• Skip no phone: ${stats.skip_no_phone}\n` +
  `• Skip invalid phone: ${stats.skip_invalid_phone}\n` +
  `• Skip blacklist: ${stats.skip_blacklist}\n` +
  `• Skip global 7d cooldown: ${stats.skip_global_cooldown}` +
  sampleBlock(samplePaused, 'PAUSED-stale') +
  sampleBlock(sampleCancelled, 'CANCELLED-stale') +
  (DRY_RUN ? `\n\n_Flip DRY_RUN=False in build_sub_reactivation.py + re-push to go live._` : '');

// Header item routes to a separate Telegram-only path (is_summary=true).
out.unshift({ json: { is_summary: true, target_phone: YASH_PHONE, message: headerMsg } });

return out;
"""
)


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


# ─── Step 1: create the dedup tab (one-time, idempotent) ──────────────────
def setup_dedup_tab():
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": REACT_SENT_GID, "title": REACT_SENT_TAB}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in REACT_SENT_HEADERS]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": REACT_SENT_GID, "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-add-react-tab",
                        "responseMode": "lastNode", "options": {}},
         "id": uid(), "name": "Trigger",
         "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": json.dumps(body), "options": {},
         }, "id": uid(), "name": "Add Tab",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
         "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED},
         "continueOnFail": True},
    ]
    conn = {"Trigger": {"main": [[{"node": "Add Tab", "type": "main", "index": 0}]]}}
    s, b = http("POST", "/workflows", {
        "name": "TEMP Sub Reactivation Tab", "nodes": nodes,
        "connections": conn, "settings": {"executionOrder": "v1"},
    })
    if s >= 300:
        print(f"  setup workflow create failed: HTTP {s}")
        return
    wf_id = b.get("id") if isinstance(b, dict) else None
    if not wf_id:
        print("  no workflow id returned from setup create")
        return
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://n8n.thebonpet.com/webhook/tmp-add-react-tab",
            data=b'{}', method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }), timeout=30)
        print("  setup fired ✓")
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:200]
        print("  " + ("Tab already exists ✓" if "already exists" in msg.lower() else f"HTTP {e.code}: {msg}"))
    time.sleep(1)
    http("DELETE", f"/workflows/{wf_id}")


# ─── Step 2: main workflow ────────────────────────────────────────────────
def gs_read_node(name, pos, tab_gid, tab_name):
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
        "position": pos,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "executeOnce": True,
        "alwaysOutputData": True,
    }


def code_node(name, pos, js):
    return {
        "parameters": {"jsCode": js},
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": pos,
    }


def merge_node(name, pos, n_inputs):
    return {
        "parameters": {"numberInputs": n_inputs},
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.merge", "typeVersion": 3,
        "position": pos,
    }


def send_wa_node():
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
                {"name": "phone_number", "value": "={{ $json.target_phone }}"},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            # 5s batching = ~5s between sends (per the WA broadcast rate-limit memory).
            "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 5000}}},
        },
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1440, 600],
        "onError": "continueRegularOutput",
        "disabled": SEND_WA_DISABLED,
    }


def split_summary_from_customer_js():
    return r"""// Route summary item (is_summary=true) to Telegram-only path.
// Customer items go to WA path.
return $input.all().filter(it => !it.json.is_summary);
"""


def split_summary_only_js():
    return r"""return $input.all().filter(it => it.json.is_summary);
"""


def split_customer_filter_dry_js(dry_run: bool):
    # DRY mode: suppress customer fan-out entirely. Summary covers everything.
    # LIVE mode: pass non-summary items through to WA send.
    if dry_run:
        return "return [];\n"
    return r"""return $input.all().filter(it => !it.json.is_summary);
"""


def split_dedup_log_js():
    # Only LIVE sends get logged to dedup + global sent log.
    # DRY items are skipped from logging so we can re-run DRY freely.
    #
    # CRITICAL: reach back to 'Pick Customers' (the node that fed Send WA) — NOT $input.all().
    # Send WA is an httpRequest whose response REPLACES the item json with {success, message_id,
    # message}, stripping customer_id/contract_id/phone/sent_at. Logging $input.all() here wrote
    # blank dedup rows, so 'alreadyReactivated' was always empty and the same paused/cancelled
    # customers re-qualified every day. This is the same dedup-blanking bug class as the 2026-05
    # winback spam. See feedback_n8n_http_input_passthrough memory.
    return r"""return $('Pick Customers').all().filter(it => !it.json.is_summary && it.json.dry_run !== 'true');
"""


def append_react_sent_node():
    return {
        "parameters": {
            "operation": "append",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": REACT_SENT_GID, "mode": "list",
                          "cachedResultName": REACT_SENT_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REACT_SENT_GID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "schema": [{"id": h, "displayName": h, "required": False,
                            "display": True, "type": "string"} for h in REACT_SENT_HEADERS],
            },
            "options": {},
        },
        "id": uid(), "name": "Log Reactivation Sent",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [1920, 600],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "onError": "continueRegularOutput",
    }


def build():
    # Daily 6:07 PM SGT (Yash chose 6pm; +7min stagger off HH:00 per cron-stagger memory,
    # and off Reorder Reminder's 18:00 to avoid clustering).
    schedule = {
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "7 18 * * *"}]}},
        "id": uid(), "name": "Daily 18:07 SGT",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 200],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
                       "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 400], "webhookId": MANUAL_WEBHOOK_ID,
    }

    # Data reads
    read_subs      = gs_read_node("Read Subscribers",         [240, 100], SUB_SHEET_GID, "subscribers")
    read_customers = gs_read_node("Read Customers",           [240, 300], CUSTOMERS_TAB_GID, "customers")
    read_orders    = gs_read_node("Read Orders",              [240, 500], ORDERS_TAB_GID, "orders")
    read_react     = gs_read_node("Read Reactivation Sent",   [240, 700], REACT_SENT_GID, REACT_SENT_TAB)
    read_global    = read_global_sent_log_node([240, 900])
    filter_global  = filter_recent_sent_log_node([400, 900], days=14)

    merge = merge_node("Merge", [600, 500], 5)

    # Inject template + flags into the compute JS.
    compute_js = (COMPUTE_JS
                  .replace("__DRY_RUN__", "true" if DRY_RUN else "false")
                  .replace("__PAUSED_MIN__", str(PAUSED_MIN_DAYS))
                  .replace("__PAUSED_MAX__", str(PAUSED_MAX_DAYS))
                  .replace("__CANCELLED_MIN__", str(CANCELLED_MIN_DAYS))
                  .replace("__CANCELLED_MAX__", str(CANCELLED_MAX_DAYS))
                  .replace("__CAP__", str(DAILY_CAP_PER_COHORT))
                  .replace("__PAUSED_TEMPLATE_JSON__", json.dumps(PAUSED_MSG_TEMPLATE))
                  .replace("__CANCELLED_TEMPLATE_JSON__", json.dumps(CANCELLED_MSG_TEMPLATE)))
    compute = code_node("Find Eligible Customers", [820, 500], compute_js)

    # Route items: summary → Telegram only; customer items → WA send (LIVE only)
    route_summary  = code_node("Pick Summary", [1080, 300], split_summary_only_js())
    route_customer = code_node("Pick Customers", [1080, 600], split_customer_filter_dry_js(DRY_RUN))

    telegram_summary = telegram_send_node("Send Telegram Summary", [1320, 300])

    send_wa = send_wa_node()

    # Log only LIVE sends (filter out DRY items).
    pick_for_log = code_node("Pick Live Sends", [1680, 600], split_dedup_log_js())
    log_react    = append_react_sent_node()
    log_global   = append_global_sent_log_node([2160, 600])
    # Belt-and-braces: also disable logging when entire WA send is hard-off.
    if SEND_WA_DISABLED or DRY_RUN:
        log_react["disabled"] = True
        log_global["disabled"] = True

    nodes = [
        schedule, manual,
        read_subs, read_customers, read_orders, read_react, read_global, filter_global,
        merge, compute,
        route_summary, route_customer,
        telegram_summary, send_wa,
        pick_for_log, log_react, log_global,
    ]

    connections = {
        schedule["name"]: {"main": [[
            {"node": read_subs["name"],      "type": "main", "index": 0},
            {"node": read_customers["name"], "type": "main", "index": 0},
            {"node": read_orders["name"],    "type": "main", "index": 0},
            {"node": read_react["name"],     "type": "main", "index": 0},
            {"node": read_global["name"],    "type": "main", "index": 0},
        ]]},
        manual["name"]: {"main": [[
            {"node": read_subs["name"],      "type": "main", "index": 0},
            {"node": read_customers["name"], "type": "main", "index": 0},
            {"node": read_orders["name"],    "type": "main", "index": 0},
            {"node": read_react["name"],     "type": "main", "index": 0},
            {"node": read_global["name"],    "type": "main", "index": 0},
        ]]},
        read_subs["name"]:      {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_customers["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_orders["name"]:    {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        read_react["name"]:     {"main": [[{"node": merge["name"], "type": "main", "index": 3}]]},
        read_global["name"]:    {"main": [[{"node": filter_global["name"], "type": "main", "index": 0}]]},
        filter_global["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 4}]]},
        merge["name"]:          {"main": [[{"node": compute["name"], "type": "main", "index": 0}]]},
        compute["name"]: {"main": [[
            {"node": route_summary["name"],  "type": "main", "index": 0},
            {"node": route_customer["name"], "type": "main", "index": 0},
        ]]},
        route_summary["name"]:  {"main": [[{"node": telegram_summary["name"], "type": "main", "index": 0}]]},
        route_customer["name"]: {"main": [[{"node": send_wa["name"],          "type": "main", "index": 0}]]},
        send_wa["name"]:        {"main": [[{"node": pick_for_log["name"],     "type": "main", "index": 0}]]},
        pick_for_log["name"]:   {"main": [[
            {"node": log_react["name"],  "type": "main", "index": 0},
            {"node": log_global["name"], "type": "main", "index": 0},
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
    if status >= 300:
        return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME:
            return wf["id"]
    return None


if __name__ == "__main__":
    print("Setting up dedup tab (idempotent)...")
    setup_dedup_tab()

    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/sub_reactivation_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nBuilt workflow: {len(payload['nodes'])} nodes -> {out}")
    print(f"  DRY_RUN = {DRY_RUN}")
    print(f"  Caps: {DAILY_CAP_PER_COHORT} paused + {DAILY_CAP_PER_COHORT} cancelled per day")

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
        http("PUT", f"/workflows/{new_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
        print("Transfer -> team project")
        http("POST", f"/workflows/{new_id}/activate")
        print("Activated.")
        print(f"\nManual webhook: https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")

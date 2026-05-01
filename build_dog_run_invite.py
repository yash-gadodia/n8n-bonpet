#!/usr/bin/env python3
"""Dog Run Invite — one-off WhatsApp blast to dog owners.

Round 2 (2026-04-26): widen audience to 1+ dog orders, cap at 100, hard-exclude
the 73 already sent on Apr 20 (loaded from dog_run_apr20_sent_phones.json).

Modes (set ONE):
  TEST_ONLY = True  → sends ONE real customer-facing msg to Yash, skips everyone else
  DRY_RUN   = True  → sends per-recipient debug-wrapped preview to Yash for ALL eligible
  both False        → LIVE send to all 100

Fire:  curl -X POST https://n8n.thebonpet.com/webhook/dog-run-invite-7b3e4f9c2d
"""
import json
import uuid
import os
import glob
import urllib.request
import urllib.error

from _sent_log import (
import subprocess
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Dog Run Invite - WhatsApp (one-off)"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "dog-run-invite-7b3e4f9c2d"

MIN_DOG_ORDERS = 1
RECIPIENT_CAP  = 500

# Auto-load every prior-round phone list so each new round excludes ALL prior sends.
# After firing a round, save its phones to dog_run_<MMMDD>_sent_phones.json and they'll
# be picked up next time. Existing files: apr20 (73), apr26 (98).
PRIOR_SENT_FILES = sorted(glob.glob(os.path.expanduser("~/n8n-bonpet/dog_run_*_sent_phones.json")))
PRIOR_SENT_PHONES = sorted({p for fp in PRIOR_SENT_FILES for p in json.load(open(fp))})

# Modes — set exactly ONE to True (or both False for LIVE to all eligible)
TEST_ONLY = False
DRY_RUN   = False

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
ORDERS_TAB_GID = 0
CUSTOMERS_TAB_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
COMPUTE_ELIGIBLE_JS = r"""// Dog Run Invite Round 2 — single-tier:
//   eligible = dog_orders >= MIN_DOG_ORDERS, sorted by most-recent dog order,
//   minus PRIOR_SENT_PHONES (the 73 from Apr 20), capped at RECIPIENT_CAP.
// "dog order" = any line title matches /dog/i in line_items_json.
const MIN_DOG_ORDERS = __MIN__;
const RECIPIENT_CAP  = __RECIPIENT_CAP__;
const PRIOR_SENT     = new Set(__PRIOR_SENT_JSON__);
const DRY_RUN   = __DRY_RUN__;
const TEST_ONLY = __TEST_ONLY__;
const YASH_PHONE = '+6581394225';

const ordersRows   = $('Read Orders Tab').all();
const customerRows = $('Read Customers Tab').all();

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
// Alias kept for existing references in this file
const normPhone = normalizePhone;
""" + COOLDOWN_JS_SNIPPET + r"""

function parseDate(s) {
  if (!s) return 0;
  const d = new Date(String(s));
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

// Walk orders, count dog orders per email + track most recent order date.
const byEmail = new Map();
for (const r of ordersRows) {
  const j = r.json;
  const email = normEmail(j.email);
  if (!email) continue;

  let items = [];
  try {
    items = JSON.parse(j.line_items_json || '[]');
  } catch (_) { items = []; }

  const hasDog = items.some(li => /dog/i.test(String(li && li.title || '')));
  const ts = parseDate(j.order_date || j.created_at);

  const agg = byEmail.get(email) || { total_orders: 0, dog_orders: 0, last_ts: 0 };
  agg.total_orders += 1;
  if (hasDog) agg.dog_orders += 1;
  if (ts > agg.last_ts) agg.last_ts = ts;
  byEmail.set(email, agg);
}

// TEST_ONLY: short-circuit, send ONE real-copy msg to Yash and bail.
if (TEST_ONLY) {
  return [{ json: {
    is_header: false,
    email: 'yash@thebonpet.com',
    customer_id: 'TEST',
    phone: YASH_PHONE,
    first_name: 'Yash',
    dog_orders: 0,
    total_orders: 0,
    last_order_at: new Date().toISOString(),
    tier: 'TEST',
  }}];
}

const stats = {
  customers_scanned: customerRows.length,
  orders_scanned: ordersRows.length,
  no_email: 0,
  no_orders_match: 0,
  below_min: 0,
  no_phone: 0,
  invalid_phone: 0,
  excluded_prior_sent: 0,
  pool_after_exclusion: 0,
  capped_to: RECIPIENT_CAP,
  eligible: 0,
};

const pool = [];
for (const c of customerRows) {
  const j = c.json;
  const email = normEmail(j.email);
  if (!email) { stats.no_email++; continue; }

  const agg = byEmail.get(email);
  if (!agg) { stats.no_orders_match++; continue; }
  if (agg.dog_orders < MIN_DOG_ORDERS) { stats.below_min++; continue; }

  const rawPhone = j.phone || j.default_address_phone || '';
  if (!rawPhone) { stats.no_phone++; continue; }
  const phone = normPhone(rawPhone);
  if (!phone) { stats.invalid_phone++; continue; }
  if (PRIOR_SENT.has(phone)) { stats.excluded_prior_sent++; continue; }
  if (isInGlobalCooldown(phone)) { stats.skipped_global_cooldown = (stats.skipped_global_cooldown || 0) + 1; continue; }

  const firstName = String(j.first_name || '').trim() ||
                    String(j.last_name || '').trim();

  pool.push({
    email,
    customer_id: String(j.customer_id || ''),
    phone,
    first_name: firstName,
    dog_orders: agg.dog_orders,
    total_orders: agg.total_orders,
    last_order_at: agg.last_ts ? new Date(agg.last_ts).toISOString() : '',
    tier: 'R2',
  });
}

// Sort by most-recent dog order, cap at RECIPIENT_CAP.
pool.sort((a, b) => new Date(b.last_order_at) - new Date(a.last_order_at));
stats.pool_after_exclusion = pool.length;
const eligibleList = pool.slice(0, RECIPIENT_CAP);
stats.eligible = eligibleList.length;

const modeTag = DRY_RUN ? '🧪 DRY RUN' : '📬 LIVE';
const sampleNames = eligibleList.slice(0, 10)
  .map(e => `• ${e.first_name || '(no name)'} (${e.dog_orders} dog orders, ${e.phone})`)
  .join('\n');
const moreCount = Math.max(0, eligibleList.length - 10);

const headerMsg =
  `🐕 *Dog Run Invite Round 2 — ${modeTag}*\n` +
  `📅 ${new Date().toISOString().slice(0,10)}\n\n` +
  `🎯 Sending to: *${stats.eligible}* dog pawrents\n` +
  `Filter: ${MIN_DOG_ORDERS}+ dog orders, sorted by recency, capped at ${RECIPIENT_CAP}\n\n` +
  `📋 Top 10 by recency:\n${sampleNames || '(none)'}\n` +
  (moreCount ? `\n…and ${moreCount} more\n` : '') +
  `\n📊 Funnel\n` +
  `• Customers scanned: ${stats.customers_scanned}\n` +
  `• Orders scanned: ${stats.orders_scanned}\n` +
  `• Below ${MIN_DOG_ORDERS} dog orders: ${stats.below_min}\n` +
  `• No order match (cat-only): ${stats.no_orders_match}\n` +
  `• Excluded (Apr 20 prior send): ${stats.excluded_prior_sent}\n` +
  `• No phone: ${stats.no_phone}\n` +
  `• Invalid phone: ${stats.invalid_phone}\n` +
  `• Pool after exclusion: ${stats.pool_after_exclusion} → capped to ${stats.eligible}`;

const header = [{ json: { is_header: true, phone: YASH_PHONE, target_phone: YASH_PHONE, message: headerMsg } }];
const body = eligibleList.map(x => ({ json: x }));

return header.concat(body);
"""

FORMAT_MESSAGE_JS = r"""// Build Dog Run invite msg per eligible customer. Header passes through untouched.
const DRY_RUN = __DRY_RUN__;
const YASH_PHONE = '+6581394225';

const out = [];
for (const it of $input.all()) {
  const j = it.json;
  if (j.is_header) {
    out.push({ json: { target_phone: j.target_phone, message: j.message, is_header: true } });
    continue;
  }
  const greeting = j.first_name ? `hellooo ${j.first_name}!! 🐾` : 'hellooo pawrents!! 🐾';
  const msg = `${greeting}\n\n` +
    `something exciting to share 🥹 The Bon Pet team is organising a Sunday Paws Club doggy hangout on 3rd May (Sun) @ ECP!\n\n` +
    `it's gonna be a chill morning for the pups to play, the pawrents to meet new dog families, and we'll be preparing yummy treats for the furballs! we've also got a partner bringing sweet treats for us humans too 👀🍪 buffet vibes!!\n\n` +
    `if u + ur pup are keen, fill in this quick form to secure ur slot + goodie bag 💛\n` +
    `👉 https://docs.google.com/forms/d/e/1FAIpQLSetIjvcCzto0-Y3DVl_pHv3FEta0xi1BtfZerqgpqIFSldZWw/viewform\n\n` +
    `hope to see u there!! 🐶✨\n\n` +
    `❤️ The Bon Pet team`;

  const dryPreview = `🧪 *DRY RUN — would send to [${j.tier}] ${j.first_name || 'customer'} (${j.phone})*\n` +
    `📊 dog_orders=${j.dog_orders} total_orders=${j.total_orders} last=${(j.last_order_at || '').slice(0,10)}\n` +
    `═══════════════════════════════════\n\n${msg}`;

  out.push({
    json: {
      target_phone: DRY_RUN ? YASH_PHONE : j.phone,
      message: DRY_RUN ? dryPreview : msg,
      email: j.email,
      customer_id: j.customer_id,
      first_name: j.first_name,
      // wa_sent_log (global) append fields
      phone: j.phone,
      workflow: 'dog_run_invite',
      template: 'invite_r2_' + (j.tier || ''),
      sent_at: new Date().toISOString(),
      order_id: '',
      notes: 'dog_orders=' + (j.dog_orders || 0) + ',tier=' + (j.tier || ''),
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
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
    }


def build():
    manual = {
        "parameters": {
            "httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
            "responseMode": "onReceived", "options": {},
        },
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": MANUAL_WEBHOOK_ID,
    }

    orders    = read_sheet_node("Read Orders Tab",    [240, 200], ORDERS_TAB_GID)
    customers = read_sheet_node("Read Customers Tab", [240, 400], CUSTOMERS_TAB_GID)
    read_global = read_global_sent_log_node([240, 600])

    merge = {
        "parameters": {"numberInputs": 3},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [480, 300],
    }

    dry  = "true" if DRY_RUN else "false"
    test = "true" if TEST_ONLY else "false"
    compute_js = (COMPUTE_ELIGIBLE_JS
                  .replace("__MIN__", str(MIN_DOG_ORDERS))
                  .replace("__RECIPIENT_CAP__", str(RECIPIENT_CAP))
                  .replace("__PRIOR_SENT_JSON__", json.dumps(PRIOR_SENT_PHONES))
                  .replace("__DRY_RUN__", dry)
                  .replace("__TEST_ONLY__", test))
    format_js  = FORMAT_MESSAGE_JS.replace("__DRY_RUN__", dry)

    compute    = code_node("Find Eligible Dog Owners", [720, 300], compute_js)
    format_msg = code_node("Format Message",           [960, 300], format_js)
    send       = send_wa_node("Send Dog Run WA",       [1200, 300])
    drop_hdr   = code_node("Drop Header",              [1440, 300],
                           "return $input.all().filter(it => !it.json.is_header);")
    log_global = append_global_sent_log_node([1680, 300])

    nodes = [manual, orders, customers, read_global, merge, compute, format_msg, send, drop_hdr, log_global]

    connections = {
        manual["name"]: {"main": [[
            {"node": orders["name"],    "type": "main", "index": 0},
            {"node": customers["name"], "type": "main", "index": 0},
            {"node": read_global["name"], "type": "main", "index": 0},
        ]]},
        orders["name"]:     {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        customers["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_global["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        merge["name"]:      {"main": [[{"node": compute["name"], "type": "main", "index": 0}]]},
        compute["name"]:    {"main": [[{"node": format_msg["name"], "type": "main", "index": 0}]]},
        format_msg["name"]: {"main": [[{"node": send["name"], "type": "main", "index": 0}]]},
        send["name"]:       {"main": [[{"node": drop_hdr["name"], "type": "main", "index": 0}]]},
        drop_hdr["name"]:   {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
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
    out = os.path.expanduser("~/n8n-bonpet/dog_run_invite_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes → {out}")
    print(f"Mode: {'🧪 DRY_RUN (to Yash)' if DRY_RUN else '📬 LIVE (to customers)'}")

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
    if TEST_ONLY:
        print(f"Mode: 🧪 TEST_ONLY — fires ONE real-copy msg to Yash (+6581394225), no customers")
    elif DRY_RUN:
        print(f"Mode: 🧪 DRY_RUN — sends per-recipient debug preview to Yash for ALL eligible")
    else:
        print(f"Mode: 📬 LIVE — sends to up to {RECIPIENT_CAP} dog owners ({MIN_DOG_ORDERS}+ orders), excluding {len(PRIOR_SENT_PHONES)} from Apr 20")
    print(f"Fire:  curl -X POST https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")

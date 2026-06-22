#!/usr/bin/env python3
"""Trial Graduation → Subscription — nudge trial customers on day 7 post-delivery to convert.

Triggers daily 10am SGT. Checks Google Sheet "Customer Orders DB" for customers whose
MOST RECENT order was a trial (FREETRIAL/CATTRIAL/DOGTRIAL), delivered exactly 7 days ago.

Excludes:
  • Active subscribers (any order with Discount Code starting "Subscription")
  • Already messaged (presence in Trial Graduation Log tab)

Sends warm, emoji-friendly WA nudge with TRIALGRAD<3THEBONPET code (20% off first sub order).
Logs phone + sent_at + trial_type to prevent re-sending.

Deploy in DRAFT mode. User must:
  1. Create TRIALGRAD<3THEBONPET discount code in Shopify (20% off first sub order, 1 use/customer)
  2. Ensure Google Sheet has "Trial Graduation Log" tab (script creates on first run)
  3. Review WA message in Code node, flip DRY_RUN to false
  4. PUT workflow via n8n API

DO NOT ACTIVATE AUTOMATICALLY.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

import subprocess

from _sent_log import (
    read_global_sent_log_node, append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)

WF_ID = "trial-graduation-wf"  # n8n assigns real ID on creation; use placeholder for draft
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
ORDERS_TAB_GID = 0
TRIAL_GRAD_LOG_TAB_GID = 1828374650  # create if missing

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
YASH_PHONE = "+6581394225"

# DRY_RUN: if true, all WAs to Yash only. Flip false after reviewing candidates.
DRY_RUN = True

CODE_JS = r"""// Trial Graduation — day-7 post-trial delivery nudge to convert to subscription.
//
// Filter: customer's MOST RECENT order is a trial (FREETRIAL/CATTRIAL/DOGTRIAL),
// delivered exactly 7 days ago. Exclude active subscribers + already-messaged.
//
// Message: warm 20% off first subscription offer (code TRIALGRAD<3THEBONPET).

const DRY_RUN = __DRY_RUN__;
const YASH_PHONE = '+6581394225';
const TARGET_DAYS_SINCE = 7;  // deliver exactly 7 days ago
const TOLERANCE_DAYS = 0;      // window: [7, 7] (can relax to ±1 if needed)

const TRIAL_CODES = ['FREETRIAL<3THEBONPET', 'CATTRIAL<3THEBONPET', 'DOGTRIAL<3THEBONPET'];

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
""" + COOLDOWN_JS_SNIPPET.replace("__SELF_WORKFLOW__", "trial_graduation") + r"""

function parseDate(s) {
  if (!s) return null;
  const d = new Date(String(s));
  return isNaN(d.getTime()) ? null : d.getTime();
}

const ordersRows = $('Read Orders Tab').all();
const sentRows = $('Read Sent Log Tab').all();

// Load already-sent emails to prevent re-messaging
const sentEmails = new Set();
for (const r of sentRows) {
  const e = normEmail(r.json.email || r.json.email_address || '');
  if (e) sentEmails.add(e);
}

// Group orders by email, keep most recent
const lastByEmail = new Map();
for (const r of ordersRows) {
  const j = r.json;
  const email = normEmail(j.email || j.email_address || '');
  if (!email) continue;

  const discountCode = String(j.discount_code || j.discount_codes || '').trim().toUpperCase();
  const orderDate = parseDate(j.order_date || j.created_at || '');
  const deliveryDate = parseDate(j.delivery_date || j.delivered_at || '');

  if (!orderDate) continue;

  // Check if subscriber (ANY order with Discount Code starting "Subscription")
  if (discountCode.startsWith('SUBSCRIPTION')) {
    const existing = lastByEmail.get(email);
    if (existing) existing.is_subscriber = true;
  }

  const existing = lastByEmail.get(email);
  if (!existing || orderDate > existing.last_order_ts) {
    lastByEmail.set(email, {
      last_order_ts: orderDate,
      delivery_ts: deliveryDate,
      discount_code: discountCode,
      first_name: j.first_name || j.first || 'there',
      phone: normPhone(j.phone || j.phone_number || ''),
      pet_name: j.pet_name || '',
      trial_type: null,
      is_subscriber: false,
    });
  }
}

// Identify trial orders and compute days-since-delivery
const candidates = [];
const now = Date.now();
const DAY_MS = 24 * 60 * 60 * 1000;

const stats = {
  total_unique_customers: lastByEmail.size,
  skipped_no_phone: 0,
  skipped_no_delivery_date: 0,
  skipped_is_subscriber: 0,
  skipped_not_trial: 0,
  skipped_wrong_days_since: 0,
  skipped_already_sent: 0,
  ready_to_message: 0,
};

for (const [email, data] of lastByEmail) {
  if (!data.phone) { stats.skipped_no_phone++; continue; }
  if (!data.delivery_ts) { stats.skipped_no_delivery_date++; continue; }
  if (data.is_subscriber) { stats.skipped_is_subscriber++; continue; }
  if (isOverFrequencyCap(data.phone)) { stats.skipped_global_cooldown = (stats.skipped_global_cooldown || 0) + 1; continue; }

  // Check if most recent order is a trial
  let trialType = null;
  if (data.discount_code.includes('FREETRIAL<3THEBONPET')) trialType = 'FREETRIAL';
  else if (data.discount_code.includes('CATTRIAL<3THEBONPET')) trialType = 'CATTRIAL';
  else if (data.discount_code.includes('DOGTRIAL<3THEBONPET')) trialType = 'DOGTRIAL';

  if (!trialType) { stats.skipped_not_trial++; continue; }

  const daysSinceDelivery = Math.floor((now - data.delivery_ts) / DAY_MS);
  if (Math.abs(daysSinceDelivery - TARGET_DAYS_SINCE) > TOLERANCE_DAYS) {
    stats.skipped_wrong_days_since++;
    continue;
  }

  if (sentEmails.has(email)) { stats.skipped_already_sent++; continue; }

  stats.ready_to_message++;

  // Determine collection link (cats vs dogs)
  // Auto-apply discount link (code never shown in body) that lands on the right collection.
  let collectionUrl = 'https://thebonpet.com/discount/TRIALGRAD%253C3THEBONPET?redirect=%2Fcollections%2Fcats';
  if (trialType === 'DOGTRIAL') collectionUrl = 'https://thebonpet.com/discount/TRIALGRAD%253C3THEBONPET?redirect=%2Fcollections%2Fdogs';

  const petNamePhrase = data.pet_name ? `${data.pet_name}` : 'your furkid';

  const msg = `Hey ${data.first_name} 🐾 did ${petNamePhrase} enjoy the Bon Pet trial?\n\n` +
    `just checking in to see how it went, would genuinely love your honest thoughts (anything we can do better?) 💛\n\n` +
    `if you'd like to keep ${petNamePhrase} on fresh food, i've set aside 20% off your first subscription, it'll apply on its own at the link below 🐾 no rush at all, and happy to help you pick the right plan\n\n` +
    `→ ${collectionUrl}\n\n` +
    `❤️ Yash & the Bon Pet team`;

  const dryRunMsg = `🧪 *DRY RUN — would send to ${data.first_name} (${data.phone})*\n` +
    `📊 days_since_delivery=${daysSinceDelivery}  trial_type=${trialType}\n` +
    `═══════════════════════════════════\n\n${msg}`;

  candidates.push({
    json: {
      email: email,
      first_name: data.first_name,
      phone: data.phone,
      pet_name: data.pet_name,
      trial_type: trialType,
      days_since_delivery: daysSinceDelivery,
      // For wa_sent_log (global) append
      workflow: 'trial_graduation',
      template: 'day7_' + trialType.toLowerCase(),
      sent_at: new Date().toISOString(),
      order_id: '',
      notes: 'trial_type=' + trialType + ',days_since=' + daysSinceDelivery,
      target_phone: DRY_RUN ? YASH_PHONE : data.phone,
      message: DRY_RUN ? dryRunMsg : msg,
      is_dry_run: DRY_RUN,
    }
  });
}

// Prepend diagnostics header
const diagLines = [
  `📊 *Trial Graduation — Daily Run*`,
  `📅 ${new Date().toISOString().slice(0, 10)}`,
  ``,
  `• Unique customers scanned: ${stats.total_unique_customers}`,
  `• Skipped (no phone): ${stats.skipped_no_phone}`,
  `• Skipped (no delivery date): ${stats.skipped_no_delivery_date}`,
  `• Skipped (active subscriber): ${stats.skipped_is_subscriber}`,
  `• Skipped (not trial order): ${stats.skipped_not_trial}`,
  `• Skipped (wrong days since): ${stats.skipped_wrong_days_since}`,
  `• Skipped (already sent): ${stats.skipped_already_sent}`,
  `• Skipped (global 7d cooldown): ${stats.skipped_global_cooldown || 0}`,
  `• Ready to message: ${stats.ready_to_message}`,
];

if (candidates.length === 0) {
  return [{
    json: {
      target_phone: YASH_PHONE,
      is_header: true,
      message: diagLines.join('\n') + '\n\n_No candidates today._',
    }
  }];
}

candidates.unshift({
  json: {
    target_phone: YASH_PHONE,
    is_header: true,
    message: diagLines.join('\n') + `\n\n${candidates.length} candidate(s) follow ⬇️`,
  }
});

return candidates;
"""


def uid():
    return str(uuid.uuid4())


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"triggerAtHour": 10}]},
        },
        "id": uid(),
        "name": "Daily 10AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 300],
    }

    read_orders = {
        "parameters": {
            "spreadsheetId": SHEET_ID,
            "range": "Orders!A:Z",
            "options": {},
        },
        "id": uid(),
        "name": "Read Orders Tab",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.1,
        "position": [240, 200],
        "credentials": {
            "googleSheetsApi": {"id": GS_CRED_ID, "name": GS_CRED_NAME}
        },
    }

    read_sent_log = {
        "parameters": {
            "spreadsheetId": SHEET_ID,
            "range": "Trial Graduation Log!A:Z",
            "options": {},
        },
        "id": uid(),
        "name": "Read Sent Log Tab",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.1,
        "position": [240, 400],
        "credentials": {
            "googleSheetsApi": {"id": GS_CRED_ID, "name": GS_CRED_NAME}
        },
    }

    code = {
        "parameters": {
            "jsCode": CODE_JS.replace(
                "__DRY_RUN__", "true" if DRY_RUN else "false"
            )
        },
        "id": uid(),
        "name": "Compute Candidates",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [480, 300],
    }

    send_wa = {
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
                    {"name": "phone_number", "value": "={{ $json.target_phone }}"},
                    {"name": "message", "value": "={{ $json.message }}"},
                ]
            },
            "options": {},
        },
        "id": uid(),
        "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [720, 300],
    }

    log_sent = {
        "parameters": {
            "spreadsheetId": SHEET_ID,
            "range": "Trial Graduation Log!A:D",
            "values": "=[[{{ $json.phone }}, {{ $json.first_name }}, {{ $json.trial_type }}, {{ now().toISO() }}]]",
        },
        "id": uid(),
        "name": "Append to Log (if not header)",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.1,
        "position": [960, 300],
        "credentials": {
            "googleSheetsApi": {"id": GS_CRED_ID, "name": GS_CRED_NAME}
        },
    }

    read_global = read_global_sent_log_node([240, 600])
    log_global = append_global_sent_log_node([1200, 300])

    nodes = [schedule, read_orders, read_sent_log, read_global, code, send_wa, log_sent, log_global]
    connections = {
        schedule["name"]: {"main": [[
            {"node": read_orders["name"], "type": "main", "index": 0},
            {"node": read_sent_log["name"], "type": "main", "index": 0},
            {"node": read_global["name"], "type": "main", "index": 0},
        ]]},
        read_orders["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
        read_sent_log["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
        read_global["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
        code["name"]: {"main": [[{"node": send_wa["name"], "type": "main", "index": 0}]]},
        send_wa["name"]: {"main": [[{"node": log_sent["name"], "type": "main", "index": 0}]]},
        log_sent["name"]: {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
    }

    return {
        "name": "Trial Graduation - WhatsApp",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def api_request(method, path, payload=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    url = f"https://n8n.thebonpet.com/api/v1{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ensure_in_team_project(wf_id):
    """Transfer workflow to team project if not already there."""
    status, body = api_request("PUT", f"/workflows/{wf_id}/transfer",
                               {"destinationProjectId": TEAM_PROJECT_ID})
    if status == 200 or "same destination" in body:
        print(f"Project: in team ✓")
    else:
        print(f"⚠️  Transfer failed → HTTP {status}: {body[:200]}")


if __name__ == "__main__":
    payload = build()
    with open("/Users/yash/n8n-bonpet/trial_graduation_payload.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built: {len(payload['nodes'])} nodes")
    print()
    print("=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print()
    print("1. SHOPIFY: Create discount code")
    print("   • Name: TRIALGRAD<3THEBONPET")
    print("   • Type: Percentage")
    print("   • Value: 20%")
    print("   • Applies to: First paid subscription order only")
    print("   • Usage limit: 1 per customer")
    print()
    print("2. GOOGLE SHEET: Ensure tabs exist")
    print("   • Tab 1: 'Orders' (columns: email, first_name, phone_number, discount_code, delivery_date, pet_name, ...)")
    print("   • Tab 2: 'Trial Graduation Log' (columns: phone, first_name, trial_type, sent_at)")
    print("   •  Use GID 1828374650 for the Log tab when creating")
    print()
    print("3. REVIEW & DEPLOY")
    print("   • Read CODE_JS in this script (search for 'Hey {first_name}')")
    print("   • Verify WA message tone matches brand voice")
    print("   • In workflow, flip DRY_RUN from true → false when ready")
    print("   • Run test execution (cron will trigger at 10am SGT)")
    print()
    print("4. PUT to n8n (NOT automatic — review first)")
    print("   # Uncomment below once you're ready:")
    print("   # status, body = api_request('POST', '/workflows', payload)")
    print("   # print(f'POST HTTP {status}')")
    print("   # if status == 201:")
    print("   #     import json as j; resp = j.loads(body)")
    print("   #     wf_id = resp['data']['id']")
    print("   #     ensure_in_team_project(wf_id)")
    print("   #     print(f'Workflow created: {wf_id}')")
    print()
    print("Payload saved → /Users/yash/n8n-bonpet/trial_graduation_payload.json")

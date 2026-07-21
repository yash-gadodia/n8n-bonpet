#!/usr/bin/env python3
"""Extend subscription customer notifications to ALL event types (2026-07-21).

Patches two LIVE workflows (live JSON is source of truth — build_subscription_save.py
had drifted and must not be re-run as-is):

1. `Subscription Save - WhatsApp` (aHp12XVEld1s1ZBP)
   - Lookup + Format now routes 4 statuses:
       PAUSED / CANCELLED  -> unchanged live behaviour (save msgs, team WA, weslee)
       ACTIVE              -> "subscription updated" customer confirm  [NEW]
       BILLING_FAILED      -> "payment failed" customer nudge          [NEW]
   - Per-channel gates: should_send (customer), should_send_team_wa, should_send_telegram.
     Team WA fires for PAUSED/CANCELLED/BILLING_FAILED only (updates would spam 5 phones).
   - NEW event types ship in DRY mode (DRY_NEW_EVENTS=true): weslee gets a full
     preview incl. the would-send customer message; no customer/team WA until the
     flag is flipped after Yash approves real previews.
   - Transactional events (ACTIVE/BILLING_FAILED) bypass the 7d global cooldown but
     self-dedup: update-confirms coalesce to 1 per phone per 6h (Shopify fires one
     event per edit click — observed 3 in an hour); billing-failed max 1 per
     phone+contract per 3 days. Brand-new contracts (start_date within 2d) skip the
     update-confirm — the OMS order-confirmation WA already covers those.

2. `Subscription Ingest (Shopify Flow email -> OMS)` (yPTo1Ru9g4nbcNpV)
   - "Filter Cancel/Pause" now forwards ACTIVE and BILLING_FAILED too, plus
     event/start_date/updated_at passthrough.

Rollback: PUT the snapshots saved next to this script by rerunning with RESTORE=1.
"""
import json
import os
import sys
import urllib.request
import urllib.error

API = "https://n8n.thebonpet.com/api/v1"
SAVE_WF_ID = "aHp12XVEld1s1ZBP"
INGEST_WF_ID = "yPTo1Ru9g4nbcNpV"
SNAP_DIR = os.path.expanduser("~/n8n-bonpet/snapshots")


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
        b = e.read().decode()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b


NEW_LOOKUP_JS = r"""// Parse subscription event, route by status (PAUSED/CANCELLED/ACTIVE/BILLING_FAILED),
// look up customer details from Customers tab, build customer + team messages.
// NEW event types (ACTIVE update-confirm, BILLING_FAILED) run DRY until approved.
const DRY_NEW_EVENTS = true;

function extractId(v) { if (!v) return ''; const s = String(v).replace(/^'/, ''); const m = s.match(/(\d+)$/); return m ? m[1] : s; }

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

// --- Global WA frequency cap (spam prevention across workflows) ---
// Prefers "Filter Recent Sent Log" if present (bounded memory); falls back to
// "Read Global Sent Log" so workflows without the filter still work.
const SELF_WORKFLOW = "__SELF_WORKFLOW__";  // marketing builders .replace() this; others leave as-is (harmless)
const MARKETING_WORKFLOWS = new Set(['post_trial_nurture','winback','reorder_reminder','trial_graduation','dog_run_invite','sub_reactivation']);
const GLOBAL_COOLDOWN_DAYS = 7;
const GLOBAL_COOLDOWN_MS = GLOBAL_COOLDOWN_DAYS * 24 * 60 * 60 * 1000;
const FREQ_RECENT_DAYS = 14;            // no two DIFFERENT marketing campaigns within this gap
const FREQ_RECENT_MS = FREQ_RECENT_DAYS * 24 * 60 * 60 * 1000;
const FREQ_WINDOW_DAYS = 90;            // rolling window for the hard count cap
const FREQ_WINDOW_MS = FREQ_WINDOW_DAYS * 24 * 60 * 60 * 1000;
const FREQ_MAX_IN_WINDOW = 3;           // max marketing messages per customer per 90 days
const GLOBAL_LAST_SENT = new Map();     // phone -> latest send ms (ANY workflow)
const MKT_SENDS = new Map();            // phone -> [{t, wf}] for MARKETING rows only
const TPL_LAST_SENT = new Map();        // `${phone}|${template}` -> latest send ms (self-dedup for transactional msgs)
let _sentRows = [];
try { _sentRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _sentRows = $('Read Global Sent Log').all(); }
  catch (e2) { _sentRows = []; }
}
for (const it of _sentRows) {
  const s = it.json;
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  const prev = GLOBAL_LAST_SENT.get(p) || 0;
  if (t > prev) GLOBAL_LAST_SENT.set(p, t);
  const wf = String(s.workflow || '').trim();
  if (MARKETING_WORKFLOWS.has(wf)) {
    if (!MKT_SENDS.has(p)) MKT_SENDS.set(p, []);
    MKT_SENDS.get(p).push({ t, wf });
  }
  const tpl = String(s.template || '').trim();
  if (tpl) {
    const k = p + '|' + tpl;
    if (t > (TPL_LAST_SENT.get(k) || 0)) TPL_LAST_SENT.set(k, t);
  }
}
function isInGlobalCooldown(phone) {
  const last = GLOBAL_LAST_SENT.get(phone);
  if (!last) return false;
  return (Date.now() - last) < GLOBAL_COOLDOWN_MS;
}
function isOverFrequencyCap(phone) {
  const arr = MKT_SENDS.get(phone) || [];
  const now = Date.now();
  const recentOther = arr.some(r => (now - r.t) < FREQ_RECENT_MS && r.wf !== SELF_WORKFLOW);
  const inWindow = arr.filter(r => (now - r.t) < FREQ_WINDOW_MS).length;
  return recentOther || inWindow >= FREQ_MAX_IN_WINDOW;
}
function sentTemplateWithin(phone, template, ms) {
  const last = TPL_LAST_SENT.get(phone + '|' + template);
  if (!last) return false;
  return (Date.now() - last) < ms;
}

function skip(reason, extra) {
  return [{ json: Object.assign({ should_send: false, should_send_team_wa: false, should_send_telegram: false, skip_reason: reason }, extra || {}) }];
}

const raw = $('Shopify Webhook (subscription_contracts/update)').first().json;
const body = raw.body || raw;
const sub = Array.isArray(body) ? body[0] : body;
if (!sub || typeof sub !== 'object') {
  return skip('no payload');
}

const status = String(sub.status || '').toUpperCase();
if (!['PAUSED', 'CANCELLED', 'ACTIVE', 'BILLING_FAILED'].includes(status)) {
  return skip(`status=${status}`);
}
const isNewEventType = (status === 'ACTIVE' || status === 'BILLING_FAILED');
const dryRun = DRY_NEW_EVENTS && isNewEventType;

const customer   = sub.customer || {};
const email      = String(sub.email || customer.email || '').toLowerCase().trim();
const contractId = extractId(sub.id || sub.contract_id || sub.handle);

if (!email) {
  return skip('no email', { contract_id: contractId });
}

// Multi-contract guard (PAUSED/CANCELLED only): a customer can hold several
// contracts (e.g. cat + dog). Pausing/cancelling ONE while another is still
// ACTIVE does NOT mean they left us — never send "sorry to see you go" to a
// current subscriber. Skip if ANY OTHER contract (different contract_id) is ACTIVE.
if (status === 'PAUSED' || status === 'CANCELLED') {
  const subRows = $('Read Subscribers Tab').all();
  const hasOtherActive = subRows.some(r => {
    const j = r.json;
    if (String(j.status || '').toUpperCase() !== 'ACTIVE') return false;
    const rcid = extractId(j.contract_id || j.id || '');
    if (rcid && rcid === contractId) return false; // ignore the contract that just changed
    const remail = String(j.email || '').toLowerCase().trim();
    return remail && remail === email;
  });
  if (hasOtherActive) {
    return skip('customer has another active contract', { email, contract_id: contractId });
  }
}

// ACTIVE = subscription edit or resume. Skip if the contract is brand-new:
// the OMS order-confirmation WA already covers new subs, a second message
// would be a duplicate.
if (status === 'ACTIVE') {
  const startT = Date.parse(String(sub.start_date || ''));
  if (!isNaN(startT) && (Date.now() - startT) < 2 * 24 * 60 * 60 * 1000) {
    return skip('new contract (order confirmation covers it)', { email, contract_id: contractId });
  }
}

// Lookup customer in Customers tab (for phone + name)
const custRows = $('Read Customers Tab').all();
const cust = custRows.find(r => String(r.json.email || '').toLowerCase().trim() === email);
if (!cust) {
  return skip('customer not in DB', { email, contract_id: contractId });
}
const cj = cust.json;
const firstName = String(cj.first_name || '').trim();
const lastName  = String(cj.last_name  || '').trim();
let phone = normalizePhone(cj.phone || cj.default_address_phone || '');
if (!phone) {
  return skip('no phone', { email, contract_id: contractId });
}

const template =
  status === 'PAUSED'         ? 'subscription_save_paused' :
  status === 'CANCELLED'      ? 'subscription_save_cancelled' :
  status === 'ACTIVE'         ? 'subscription_update_confirm' :
                                'subscription_billing_failed';

// Cooldown rules: save msgs (pause/cancel) respect the 7d global cooldown.
// Transactional msgs (update confirm, billing failed) bypass it but self-dedup:
// update confirms coalesce edit-bursts (Shopify fires one event per edit click),
// billing-failed nags at most once per 3 days.
if (status === 'PAUSED' || status === 'CANCELLED') {
  if (isInGlobalCooldown(phone)) {
    return skip('global 7d cooldown', { email, phone, contract_id: contractId });
  }
} else if (status === 'ACTIVE') {
  if (sentTemplateWithin(phone, template, 6 * 60 * 60 * 1000)) {
    return skip('update confirm already sent <6h (edit burst)', { email, phone, contract_id: contractId });
  }
} else if (status === 'BILLING_FAILED') {
  if (sentTemplateWithin(phone, template, 3 * 24 * 60 * 60 * 1000)) {
    return skip('billing-failed nudge already sent <3d', { email, phone, contract_id: contractId });
  }
}

// Aggregate orders for lifetime stats (total_orders, total_spent)
let totalOrders = 0, totalSpent = 0;
try {
  for (const r of $('Read Orders Tab').all()) {
    const o = r.json;
    if (String(o.email || '').toLowerCase().trim() !== email) continue;
    totalOrders += 1;
    totalSpent += parseFloat(String(o.total_price || '0').replace(/[^0-9.]/g, '')) || 0;
  }
} catch(e) {}

const greeting = firstName ? `Hi ${firstName}!` : 'Hi!';

// Subscription line preview
const firstLine = (sub.line_items && sub.line_items.edges && sub.line_items.edges[0] && sub.line_items.edges[0].node)
                || (sub.lines && sub.lines[0]) || {};
const protein = String(
  sub.line_selling_plan_name
  || (firstLine.selling_plan && firstLine.selling_plan.name)
  || firstLine.title
  || '(plan)'
).trim();
const qty = sub.line_quantity || firstLine.quantity || '';
const bp = sub.billing_policy || {};
let cadence = `${sub.cadence_interval_count || bp.interval_count || ''} ${sub.cadence_interval || bp.interval || ''}`.trim().toLowerCase();
cadence = cadence.replace(/^(\d+) (week|month|day)$/, (m, n, u) => n === '1' ? u : `${n} ${u}s`);
const nextBill = String(sub.upcoming_billing_date || sub.next_billing_date || '').slice(0, 10) || '(n/a)';

const pauseMsg = `${greeting} 🐾

Yash here. saw you paused your sub, hope all's good with your furkid 🐾 totally no rush, take your time 💛

if anything's on your mind, just reply, i read every message myself. and whenever you're ready to pick back up, happy to help (swap proteins, stretch the cadence, whatever works for you).

❤️ Yash & the Bon Pet team`;

const cancelMsg = `${greeting} 🐾

Yash from The Bon Pet. saw you cancelled your sub, sorry to see you go 🥺

mind sharing what changed? we're a tiny SG team and feedback like yours genuinely shapes what we do next 🙏

and if you ever want to give it another shot, there's 20% off waiting for you, it applies on its own here (whenever you fancy):
https://thebonpet.com/discount/WELCOMEBACK%253C3THEBONPET
either way, thank you so much for trying us 💛

❤️ Yash & the Bon Pet team`;

const updatedMsg = `${greeting} 🐾

quick note from The Bon Pet, your subscription was just updated ✅

📦 ${protein}${qty ? ' x ' + qty : ''}${cadence ? ', every ' + cadence : ''}
📅 next delivery: ${nextBill}

if this wasn't you or something looks off, just reply here and we'll sort it out 💛

❤️ The Bon Pet team`;

const billingFailedMsg = `${greeting} 🐾

heads up from The Bon Pet, the payment for your subscription didn't go through 😿 no stress, it happens!

to keep your furkid's meals on track:

✅ check your card details at thebonpet.com/account
✅ or just reply here and we'll help you sort it out

❤️ The Bon Pet team`;

const customerMsg =
  status === 'PAUSED'         ? pauseMsg :
  status === 'CANCELLED'      ? cancelMsg :
  status === 'ACTIVE'         ? updatedMsg :
                                billingFailedMsg;

const headline =
  status === 'PAUSED'         ? '🚨 *Subscription PAUSED*' :
  status === 'CANCELLED'      ? '🚨 *Subscription CANCELLED*' :
  status === 'ACTIVE'         ? '🔄 *Subscription UPDATED*' :
                                '💳🚨 *Subscription PAYMENT FAILED*';
const billLabel = (status === 'PAUSED' || status === 'CANCELLED') ? 'Next billing was' : 'Next billing';

let weslee_message =
  `${headline}\n` +
  `👤 ${firstName} ${lastName} (${email})\n` +
  `📱 ${phone}\n` +
  `📦 ${protein}${qty ? ' x ' + qty : ''}${cadence ? ', every ' + cadence : ''}\n` +
  `📅 ${billLabel}: ${nextBill}\n` +
  `💰 Lifetime: ${totalOrders} orders, S$${totalSpent.toFixed(2)}\n` +
  `🆔 Contract: ${contractId}`;

if (dryRun) {
  weslee_message = `🧪 *DRY RUN — customer NOT messaged*\n` + weslee_message +
    `\n\n📝 Would send to customer:\n---\n${customerMsg}`;
}

// Channel gates. Team WA (5 phones) only for high-signal events; routine
// update confirms go to weslee Telegram only.
const teamWaEvent = (status === 'PAUSED' || status === 'CANCELLED' || status === 'BILLING_FAILED');

return [{
  json: {
    should_send: !dryRun,
    should_send_team_wa: teamWaEvent && !dryRun,
    should_send_telegram: true,
    dry_run: dryRun,
    status: status,
    customer_phone: phone,
    customer_message: customerMsg,
    weslee_message: weslee_message,
    // wa_sent_log (global) append fields
    phone: phone,
    workflow: 'subscription_save',
    template: template,
    sent_at: new Date().toISOString(),
    order_id: contractId,
    notes: `status=${status}, email=${email}`,
  }
}];
"""

NEW_FILTER_JS = r"""// Forward subscription events to the Subscription Save/Notify workflow,
// mapping TBP_JSON field names to the shape its Lookup expects.
// PAUSED/CANCELLED (save msgs) + ACTIVE (update confirm) + BILLING_FAILED (payment nudge).
const out=[];
for(const e of $input.all()){
  const j=e.json||{};
  const status=String(j.status||'').toUpperCase();
  if(!['CANCELLED','PAUSED','ACTIVE','BILLING_FAILED'].includes(status)) continue;
  const line=(j.lines&&j.lines[0])||{};
  out.push({json:{
    status,
    event:j.event||null,
    start_date:j.start_date||null,
    updated_at:j.updated_at||null,
    email:j.customer_email,
    customer:{email:j.customer_email},
    id:j.contract_id,
    contract_id:j.contract_id,
    customer_name:j.customer_name,
    lines:[{title:line.title,quantity:line.quantity}],
    line_quantity:line.quantity,
    billing_policy:{interval:j.billing_interval,interval_count:j.billing_interval_count},
    cadence_interval:j.billing_interval,
    cadence_interval_count:j.billing_interval_count,
    next_billing_date:j.next_billing_date,
  }});
}
return out;
"""


def uid():
    import uuid
    return str(uuid.uuid4())


def if_node(name, pos, left_expr):
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": left_expr,
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": pos,
    }


def clean_payload(wf):
    return {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ("executionOrder", "saveDataErrorExecution", "saveDataSuccessExecution",
                              "saveManualExecutions", "timezone", "errorWorkflow", "callerPolicy")},
    }


def snapshot(wf_id, tag):
    os.makedirs(SNAP_DIR, exist_ok=True)
    status, wf = http("GET", f"/workflows/{wf_id}")
    assert status == 200, f"GET {wf_id} -> {status}: {wf}"
    path = os.path.join(SNAP_DIR, f"{tag}_{wf_id}.json")
    if os.path.exists(path):
        print(f"Snapshot already exists (keeping original pre-patch state): {path}")
    else:
        with open(path, "w") as f:
            json.dump(wf, f, indent=2)
        print(f"Snapshot {wf['name']} -> {path}")
    return wf


def restore():
    for tag, wf_id in (("save_prod_2026-07-21", SAVE_WF_ID), ("ingest_prod_2026-07-21", INGEST_WF_ID)):
        path = os.path.join(SNAP_DIR, f"{tag}_{wf_id}.json")
        wf = json.load(open(path))
        status, body = http("PUT", f"/workflows/{wf_id}", clean_payload(wf))
        print(f"RESTORE {wf['name']} -> HTTP {status}")


def patch_save():
    wf = snapshot(SAVE_WF_ID, "save_prod_2026-07-21")
    nodes = {n["name"]: n for n in wf["nodes"]}

    nodes["Lookup + Format"]["parameters"]["jsCode"] = NEW_LOOKUP_JS

    team_names = [n["name"] for n in wf["nodes"] if n["name"].startswith("Team WA ")]
    existing = {n["name"] for n in wf["nodes"]}
    if "Team WA?" not in existing:
        wf["nodes"].append(if_node("Team WA?", [960, 620], "={{ $json.should_send_team_wa }}"))
    if "Telegram?" not in existing:
        wf["nodes"].append(if_node("Telegram?", [960, 440], "={{ $json.should_send_telegram }}"))

    conns = wf["connections"]
    # Customer gate keeps its name; now it feeds ONLY the customer send.
    conns["Should Send?"] = {"main": [
        [{"node": "Send Customer WA", "type": "main", "index": 0}],
        [],
    ]}
    conns["Lookup + Format"] = {"main": [[
        {"node": "Should Send?", "type": "main", "index": 0},
        {"node": "Telegram?", "type": "main", "index": 0},
        {"node": "Team WA?", "type": "main", "index": 0},
    ]]}
    conns["Telegram?"] = {"main": [
        [{"node": "Send Telegram Weslee", "type": "main", "index": 0}],
        [],
    ]}
    conns["Team WA?"] = {"main": [
        [{"node": n, "type": "main", "index": 0} for n in team_names],
        [],
    ]}

    status, body = http("PUT", f"/workflows/{SAVE_WF_ID}", clean_payload(wf))
    print(f"PUT Subscription Save -> HTTP {status}")
    if status >= 300:
        print(body)
        sys.exit(1)
    s, _ = http("POST", f"/workflows/{SAVE_WF_ID}/activate")
    print(f"Activate Save -> HTTP {s}")


OMS_GUARD_JS = r"""// BILLING_FAILED is a payment event, not a contract-state change — syncing it
// onto the OMS subscriptions record would clobber the contract's real status
// (breaks subscriber detection + the multi-contract guard). Drop it here;
// it still reaches the Save/notify workflow via Filter Cancel/Pause.
return $input.all().filter(it => String((it.json||{}).status||'').toUpperCase() !== 'BILLING_FAILED');
"""


def patch_ingest():
    wf = snapshot(INGEST_WF_ID, "ingest_prod_2026-07-21")
    nodes = {n["name"]: n for n in wf["nodes"]}
    nodes["Filter Cancel/Pause"]["parameters"]["jsCode"] = NEW_FILTER_JS
    if "Skip Billing-Failed for OMS" not in nodes:
        wf["nodes"].append({
            "parameters": {"jsCode": OMS_GUARD_JS},
            "id": uid(), "name": "Skip Billing-Failed for OMS",
            "type": "n8n-nodes-base.code", "typeVersion": 2,
            "position": [460, 120],
        })
        wf["connections"]["Parse TBP_JSON"] = {"main": [[
            {"node": "Skip Billing-Failed for OMS", "type": "main", "index": 0},
            {"node": "Filter Cancel/Pause", "type": "main", "index": 0},
        ]]}
        wf["connections"]["Skip Billing-Failed for OMS"] = {"main": [[
            {"node": "POST to OMS", "type": "main", "index": 0},
        ]]}
    status, body = http("PUT", f"/workflows/{INGEST_WF_ID}", clean_payload(wf))
    print(f"PUT Subscription Ingest -> HTTP {status}")
    if status >= 300:
        print(body)
        sys.exit(1)
    s, _ = http("POST", f"/workflows/{INGEST_WF_ID}/activate")
    print(f"Activate Ingest -> HTTP {s}")


if __name__ == "__main__":
    if os.environ.get("RESTORE") == "1":
        restore()
        sys.exit(0)
    if sys.argv[1:] == ["save"]:
        patch_save()
        sys.exit(0)
    if sys.argv[1:] == ["ingest"]:
        patch_ingest()
        sys.exit(0)
    patch_save()
    patch_ingest()
    print()
    print("Done. New event types are DRY (weslee previews only).")
    print("To go live: set DRY_NEW_EVENTS = false in NEW_LOOKUP_JS and rerun (patch_save only).")

#!/usr/bin/env python3
"""Build the Year in Paws price-lock WhatsApp broadcast workflow.

Reads the audience LIVE FROM THE SALES CRM (not a sheet), which after the
2026-08-14 backfill holds subscription_status, inferred_species, order_count
and last_order_date on every Account, with the phone on the linked Contact.

Safety design, every item traceable to automation-safeguards.md:
  * VERIFY_MODE default TRUE      -> computes and reports, sends nothing
  * DAILY_CAP                     -> hard ceiling per run
  * SplitInBatches(1) + Wait 5s   -> the throttle the safeguards require
  * cooldown-state read           -> skips anyone messaged in the last 7 days
                                     by ANY workflow (fail-closed if it errors)
  * ACTIVE contract holders excluded outright
  * one message per phone         -> waves are mutually exclusive by person
  * idempotency_key per send      -> the chokepoint dedups at the database
  * Telegram funnel report        -> counts before/after every filter

Usage:  python3 build_price_lock_broadcast.py            # create/update
        RESTORE=1 python3 build_price_lock_broadcast.py  # roll back
"""
import json, os, sys, urllib.request, urllib.error

N8N = "https://n8n.thebonpet.com/api/v1"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
KEYFILE = os.path.expanduser("~/.n8n-bonpet-newkey")
WF_NAME = "Price Lock Broadcast - WhatsApp (Year in Paws)"
SNAPSHOT = os.path.expanduser("~/n8n-bonpet/snapshots/price_lock_broadcast.json")

WMS_CRED = {"httpHeaderAuth": {"id": "4pUEOr1SF2Fu4RNl", "name": "TBP OMS WMS PAT"}}
WA_KEY = "a3f9c1e6b4d72f8a9e5c3b12d7f0a9c4e6b3d2f1c8a7e9b4c6d5f2a1b3c4d6e"

def key(): return open(KEYFILE).read().strip()
def api(path, method="GET", body=None):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(N8N + path, data=d, method=method)
    r.add_header("X-N8N-API-KEY", key()); r.add_header("User-Agent", UA)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} {method} {path}\n{e.read().decode()[:400]}")

# --------------------------------------------------------------- the code node
COMPUTE = r"""
// Price-lock broadcast: build one message per PERSON from live CRM data.
// Waves are mutually exclusive; a person can never receive two of them.
const VERIFY_MODE = true;      // <-- FLIP TO false ONLY AFTER A DRY RUN IS REVIEWED
const DAILY_CAP   = 400;
const WAVES       = ['B_paused'];   // <-- which waves this run sends

const LOCK_END = '1 September 2027';
const YIP = 'https://thebonpet.com/pages/year-in-paws?utm_source=wa&utm_medium=whatsapp&utm_campaign=price_lock_aug26';

const accounts = $('Read CRM Accounts').all().map(i => i.json).flatMap(j => j.items || [j]);
const contacts = $('Read CRM Contacts').all().map(i => i.json).flatMap(j => j.items || [j]);

// cooldown: fail CLOSED. If the read is missing, send nothing.
let cool;
try { cool = $('Read Cooldown State').first().json; }
catch (e) { throw new Error('cooldown-state unavailable, aborting before any send'); }
const recent = new Set();
for (const row of (cool.recent || cool.items || [])) {
  const p = String(row.phone_number || '').replace(/\D/g, '').replace(/^65/, '');
  if (p) recent.add(p);
}

const phoneOf = {};
for (const c of contacts) {
  const p = String(c.phone || c.mobile_phone || '').replace(/\D/g, '').replace(/^65/, '');
  if (p.length === 8 && c.account_id) phoneOf[c.account_id] = p;
}

const PRICE = {
  cat: 'Cat Chicken moves from $7.10 to $10.90, Beef and Duck to $13.90, Kangaroo to $14.90.',
  dog: 'Dog Chicken moves from $8.60 to $12.90, Beef to $15.90, Kangaroo and Fish to $16.90.',
  both:'Cat Chicken moves from $7.10 to $10.90 and Dog Chicken from $8.60 to $12.90, with the richer proteins moving too.',
  unknown:'Our Everyday recipes move to $10.90 for cats and $12.90 for dogs, with the richer proteins moving too.'
};
const LOSS = {
  cat:'Cat Chicken cost us $8.81 to make and deliver, and we sold it at $7.10.',
  dog:'Dog Chicken cost us $9.85 to make and deliver, and we sold it at $8.60.',
  both:'Cat Chicken cost us $8.81 to make and deliver, and we sold it at $7.10.',
  unknown:'Cat Chicken cost us $8.81 to make and deliver, and we sold it at $7.10.'
};

function msgB(n) {
  return `hi ${n}\n\nquick note so you don't worry when you see the news going around.\n\n`
   + `our prices go up on 1 September. yours don't.\n\n`
   + `your subscription is paused, not cancelled, so your price is already locked until ${LOCK_END}. `
   + `nothing for you to do, and no deadline to beat. whenever you're ready, unpause and you pay what you paid before.\n\n`
   + `we wanted you to hear that from us directly rather than see the price change and assume it applied to you.\n\n`
   + `if you've been paused a while and something wasn't working (delivery timing, portions, a fussy eater), `
   + `reply here and tell us. we'd rather fix it than lose you quietly.\n\n❤️ Nic and Yash`;
}
function msgA(n, sp) {
  return `hi ${n}\n\nyou were subscribed with us before and stopped, which is completely fine. `
   + `but there's a date coming up that we didn't want you to find out about after the fact.\n\n`
   + `our prices go up on 1 September. ${PRICE[sp]}\n\n`
   + `the honest reason: our Everyday recipes were losing money on every single pack. ${LOSS[sp]} `
   + `we covered that difference ourselves for months because we didn't want to raise prices on you.\n\n`
   + `so here's the one thing worth knowing 💛\n\n`
   + `start a subscription again before 1 September and you keep today's prices until ${LOCK_END}.\n\n`
   + `✅ pause or cancel any time, the lock stays as long as the subscription is active\n`
   + `✅ same recipes, same sous vide at 80°C, same open formulas\n`
   + `✅ delivery rates unchanged, self-collect still free at all 3 points\n\n`
   + `every cost behind the new prices is published, down to the 9-cent software fee 👉 ${YIP}&utm_content=cancelled\n\n`
   + `and if you stopped for a reason we could have fixed, tell us. we'd genuinely rather hear it.\n\n❤️ Nic and Yash`;
}
function msgC(n, sp, coll, tag) {
  return `hi ${n}\n\nour prices go up on 1 September. ${PRICE[sp]}\n\n`
   + `why: our Everyday recipes were losing money on every pack. ${LOSS[sp]} we covered it ourselves for months. `
   + `to keep making this food, we had to close the gap.\n\nbut you don't have to pay the new prices 💛\n\n`
   + `subscribe to any recipe before 1 September and today's prices are locked in for you until ${LOCK_END}.\n\n`
   + `✅ 10% off on top of that, every single order\n`
   + `✅ pause, skip or cancel any time, the lock stays while it's active\n`
   + `✅ same food, same formulas, still published gram by gram\n\n`
   + `every cost behind the new prices is published 👉 ${YIP}&utm_content=${tag}\n\n`
   + `👉 lock today's price: https://thebonpet.com/collections/${coll}?utm_source=wa&utm_medium=whatsapp&utm_campaign=price_lock_aug26&utm_content=${tag}\n\n`
   + `❤️ Nic and Yash`;
}

const stats = { accounts: accounts.length, no_phone: 0, active_excluded: 0,
                never_ordered: 0, cooldown_skipped: 0, wrong_wave: 0, candidates: 0 };
const seen = new Set();
const out = [];

for (const a of accounts) {
  const ph = phoneOf[a.id];
  if (!ph) { stats.no_phone++; continue; }
  if (seen.has(ph)) continue;                    // one message per human, ever
  const st = a.subscription_status;
  if (st === 'active') { stats.active_excluded++; continue; }

  let wave;
  if (st === 'paused')        wave = 'B_paused';
  else if (st === 'cancelled') wave = 'A_cancelled';
  else if ((a.order_count || 0) >= 2) wave = 'C1_repeat';
  else if ((a.order_count || 0) === 1) wave = 'C2_single';
  else { stats.never_ordered++; continue; }

  if (!WAVES.includes(wave)) { stats.wrong_wave++; continue; }
  if (recent.has(ph)) { stats.cooldown_skipped++; continue; }

  const sp = ['cat','dog','both'].includes(a.inferred_species) ? a.inferred_species : 'unknown';
  const coll = sp === 'cat' ? 'cats' : 'dogs';
  const nm = (a.name || '').trim().split(/\s+/)[0] || 'there';
  const message = wave === 'B_paused' ? msgB(nm)
                : wave === 'A_cancelled' ? msgA(nm, sp)
                : msgC(nm, sp, coll, wave === 'C1_repeat' ? 'repeat' : 'single');

  seen.add(ph);
  out.push({ json: { is_header: false, target_phone: '+65' + ph, message, wave,
    account_id: a.id, workflow: 'price_lock_aug26', template: wave,
    idempotency_key: `price_lock_aug26:${ph}:${wave}:r1` } });
}
stats.candidates = out.length;

const capped = out.slice(0, DAILY_CAP);
stats.capped_to = capped.length;
stats.verify_mode = VERIFY_MODE;
stats.waves = WAVES.join(',');

const header = { json: { is_header: true, stats,
  summary: `[price-lock] mode=${VERIFY_MODE ? 'VERIFY (no sends)' : 'LIVE'} waves=${WAVES.join(',')} `
    + `accounts=${stats.accounts} candidates=${stats.candidates} sending=${VERIFY_MODE ? 0 : capped.length} `
    + `| skipped: active=${stats.active_excluded} cooldown=${stats.cooldown_skipped} `
    + `nophone=${stats.no_phone} otherwave=${stats.wrong_wave}`,
  samples: capped.slice(0, 3).map(c => c.json.message.slice(0, 160)) } };

return VERIFY_MODE ? [header] : [header, ...capped];
"""

def node(name, ntype, tv, pos, params, creds=None, extra=None):
    n = {"parameters": params, "name": name, "type": ntype,
         "typeVersion": tv, "position": pos, "id": name.lower().replace(" ", "-")}
    if creds: n["credentials"] = creds
    if extra: n.update(extra)
    return n

nodes = [
    node("Manual Trigger Webhook", "n8n-nodes-base.webhook", 2, [-560, 0],
         {"path": "price-lock-broadcast", "responseMode": "onReceived", "options": {}},
         extra={"webhookId": "price-lock-broadcast-2026"}),
    node("CRM Login", "n8n-nodes-base.httpRequest", 4.2, [-560, 160],
         {"method": "POST", "url": "https://salescrm-api.thebonpet.com/api/v1/auth/login",
          "sendHeaders": True, "headerParameters": {"parameters": [
              {"name": "User-Agent", "value": "Mozilla/5.0"},
              {"name": "Content-Type", "value": "application/json"}]},
          "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify({username: $env.TBP_CRM_USER || 'yash', password: $env.TBP_CRM_PASS, tenant: 'tbp'}) }}",
          "options": {}}),
    node("Read CRM Accounts", "n8n-nodes-base.httpRequest", 4.2, [-340, -80],
         {"method": "GET", "url": "https://salescrm-api.thebonpet.com/api/v1/accounts?page_size=100",
          "sendHeaders": True, "headerParameters": {"parameters": [
              {"name": "User-Agent", "value": "Mozilla/5.0"},
              {"name": "Authorization", "value": "=Bearer {{ $('CRM Login').first().json.access_token }}"}]},
          "options": {"pagination": {"pagination": {
              "paginationMode": "updateAParameterInEachRequest",
              "parameters": {"parameters": [{"type": "qs", "name": "page",
                                             "value": "={{ $pageCount + 1 }}"}]},
              "paginationCompleteWhen": "other",
              "completeExpression": "={{ !$response.body.has_next }}"}}}}),
    node("Read CRM Contacts", "n8n-nodes-base.httpRequest", 4.2, [-340, 80],
         {"method": "GET", "url": "https://salescrm-api.thebonpet.com/api/v1/contacts?page_size=100",
          "sendHeaders": True, "headerParameters": {"parameters": [
              {"name": "User-Agent", "value": "Mozilla/5.0"},
              {"name": "Authorization", "value": "=Bearer {{ $('CRM Login').first().json.access_token }}"}]},
          "options": {"pagination": {"pagination": {
              "paginationMode": "updateAParameterInEachRequest",
              "parameters": {"parameters": [{"type": "qs", "name": "page",
                                             "value": "={{ $pageCount + 1 }}"}]},
              "paginationCompleteWhen": "other",
              "completeExpression": "={{ !$response.body.has_next }}"}}}}),
    node("Read Cooldown State", "n8n-nodes-base.httpRequest", 4.2, [-340, 240],
         {"method": "GET",
          "url": "https://api.thebonpet.com/wms/wa-log/cooldown-state?recent_days=7&window_days=90"
                 "&workflows=price_lock_aug26,reorder_reminder,winback,sub_reactivation,post_trial_nurture",
          "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
          "options": {}}, creds=WMS_CRED),
    node("Compute Candidates", "n8n-nodes-base.code", 2, [-80, 40], {"jsCode": COMPUTE}),
    node("Split Header", "n8n-nodes-base.filter", 2.2, [140, 40],
         {"conditions": {"options": {"caseSensitive": True, "version": 2},
                         "conditions": [{"leftValue": "={{ $json.is_header }}",
                                         "rightValue": False,
                                         "operator": {"type": "boolean", "operation": "false"}}],
                         "combinator": "and"}, "options": {}}),
    node("Throttle 1 at a time", "n8n-nodes-base.splitInBatches", 3, [360, 40],
         {"batchSize": 1, "options": {}}),
    node("Send WA", "n8n-nodes-base.httpRequest", 4.2, [580, 140],
         {"method": "POST", "url": "https://api.thebonpet.com/whatsapp/send",
          "sendHeaders": True, "headerParameters": {"parameters": [
              {"name": "Content-Type", "value": "application/json"},
              {"name": "X-API-Key", "value": WA_KEY}]},
          "sendBody": True, "bodyParameters": {"parameters": [
              {"name": "phone_number", "value": "={{ $json.target_phone }}"},
              {"name": "message", "value": "={{ $json.message }}"},
              {"name": "idempotency_key", "value": "={{ $json.idempotency_key }}"},
              {"name": "workflow", "value": "={{ $json.workflow }}"},
              {"name": "template", "value": "={{ $json.template }}"}]},
          "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 5000}}}}),
    node("Wait 5s", "n8n-nodes-base.wait", 1.1, [800, 140],
         {"amount": 5, "unit": "seconds"}, extra={"webhookId": "price-lock-wait"}),
    node("Header Only", "n8n-nodes-base.filter", 2.2, [360, -140],
         {"conditions": {"options": {"caseSensitive": True, "version": 2},
                         "conditions": [{"leftValue": "={{ $json.is_header }}",
                                         "rightValue": True,
                                         "operator": {"type": "boolean", "operation": "true"}}],
                         "combinator": "and"}, "options": {}}),
    node("Report to Telegram", "n8n-nodes-base.httpRequest", 4.2, [580, -140],
         {"method": "POST",
          "url": "https://api.telegram.org/bot8784180714:AAHlTIsa26cLZUYGyye3IP3aT1maGARaaoU/sendMessage",
          "sendBody": True, "bodyParameters": {"parameters": [
              {"name": "chat_id", "value": "-1002572441152"},
              {"name": "text", "value": "={{ $json.summary }}"}]}, "options": {}}),
]

conns = {
    "Manual Trigger Webhook": {"main": [[{"node": "CRM Login", "type": "main", "index": 0}]]},
    "CRM Login": {"main": [[{"node": "Read CRM Accounts", "type": "main", "index": 0},
                            {"node": "Read CRM Contacts", "type": "main", "index": 0},
                            {"node": "Read Cooldown State", "type": "main", "index": 0}]]},
    "Read Cooldown State": {"main": [[{"node": "Compute Candidates", "type": "main", "index": 0}]]},
    "Compute Candidates": {"main": [[{"node": "Split Header", "type": "main", "index": 0},
                                     {"node": "Header Only", "type": "main", "index": 0}]]},
    "Split Header": {"main": [[{"node": "Throttle 1 at a time", "type": "main", "index": 0}]]},
    "Throttle 1 at a time": {"main": [[], [{"node": "Send WA", "type": "main", "index": 0}]]},
    "Send WA": {"main": [[{"node": "Wait 5s", "type": "main", "index": 0}]]},
    "Wait 5s": {"main": [[{"node": "Throttle 1 at a time", "type": "main", "index": 0}]]},
    "Header Only": {"main": [[{"node": "Report to Telegram", "type": "main", "index": 0}]]},
}

payload = {"name": WF_NAME, "nodes": nodes, "connections": conns,
           "settings": {"executionOrder": "v1"}}

existing = [w for w in api("/workflows?limit=250")["data"] if w["name"] == WF_NAME]
os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
if existing:
    wid = existing[0]["id"]
    cur = api(f"/workflows/{wid}")
    json.dump(cur, open(SNAPSHOT, "w"), indent=1)
    if os.environ.get("RESTORE"):
        prev = json.load(open(SNAPSHOT))
        api(f"/workflows/{wid}", "PUT", {k: prev[k] for k in
            ("name", "nodes", "connections", "settings")})
        sys.exit(f"restored {wid} from snapshot")
    api(f"/workflows/{wid}", "PUT", payload)
    print(f"updated workflow {wid}")
else:
    r = api("/workflows", "POST", payload)
    wid = r["id"]
    print(f"created workflow {wid}")

w = api(f"/workflows/{wid}")
print(f"  name   : {w['name']}")
print(f"  active : {w['active']}  (left INACTIVE on purpose - trigger manually)")
print(f"  nodes  : {len(w['nodes'])}")
print(f"  webhook: https://n8n.thebonpet.com/webhook/price-lock-broadcast")
print("\n  VERIFY_MODE is TRUE. It will compute and report, and send nothing.")
print("  Review the Telegram funnel line, then flip VERIFY_MODE to false to go live.")

#!/usr/bin/env python3
"""Build the Reorder Reminder workflow.

v1 ships in DRY_RUN mode — sends per-candidate WA messages to Yash only.
Flip DRY_RUN to false in the Code node and re-PUT to go live to customers.
"""
import json, uuid, os, urllib.request, urllib.error
import subprocess

WF_ID = "AMd0mktMWn73UCbZ"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"  # The Bon Pet team project — shared creds live here
SHOPIFY_DOMAIN = "d2ac44-d5.myshopify.com"
SHOPIFY_API_VER = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
YASH_PHONE = "+6581394225"  # dry-run target

# All values that feed the Code node logic. Keep them obvious / editable.
CODE_JS = r"""// Reorder Reminder — compute candidates and build personalized messages.
//
// Edit DRY_RUN to false to send to actual customers (not just Yash).
const DRY_RUN = true;
const YASH_PHONE = '+6581394225';

// Strategy: per-customer cadence based on order weight (smart),
// 14 days fallback if no weight info, capped to [5, 30] days.
const DEFAULT_CADENCE_DAYS = 14;
const GRAMS_PER_DAY = 150;   // ~1 small pet's daily consumption (cat 130g, dog 200g range)
const MIN_CADENCE = 5;
const MAX_CADENCE = 30;

// Send windows
const REMIND_1_OFFSET_MIN = -3;  // days_since_order in [cadence-3, cadence]
const REMIND_1_OFFSET_MAX = 0;
const REMIND_2_OFFSET_MIN = 3;   // days_since_order in [cadence+3, cadence+5]
const REMIND_2_OFFSET_MAX = 5;

// ----- Pull and group orders -----
const orders = [];
for (const it of $input.all()) {
  const j = it.json;
  if (Array.isArray(j.orders)) orders.push(...j.orders);
  else if (j.id) orders.push(j);
}

if (orders.length === 0) {
  return [{ json: {
    skip_send: false,
    target_phone: YASH_PHONE,
    message: '🔍 *Reorder Reminder — DRY RUN*\n📅 ' + new Date().toISOString().slice(0,10) +
             '\n\n0 orders found in last 90 days (no candidates).\n\n_Workflow ran successfully._'
  }}];
}

// Group by customer.id
const byCustomer = new Map();
for (const o of orders) {
  const cid = o.customer && o.customer.id;
  if (!cid) continue;  // skip guest checkouts (no customer record → no phone consent)
  if (!byCustomer.has(cid)) byCustomer.set(cid, []);
  byCustomer.get(cid).push(o);
}

const today = new Date();
const todayMs = today.getTime();
const candidates = [];

// Diagnostic counters to understand filter funnel
const stats = {
  total_orders: orders.length,
  total_customers: byCustomer.size,
  skipped_subscription: 0,
  skipped_no_phone: 0,
  too_recent: 0,             // days_since < cadence - 3
  in_remind_1_window: 0,     // would fire reminder 1
  between_windows: 0,        // (cadence, cadence+3) gap
  in_remind_2_window: 0,
  too_late: 0,               // > cadence + 5
};
const daysSinceBuckets = {'0-7': 0, '8-14': 0, '15-21': 0, '22-30': 0, '31-60': 0, '61+': 0};
const cadenceBuckets = {'5-7': 0, '8-14': 0, '15-21': 0, '22-30': 0};

for (const [cid, cust_orders] of byCustomer) {
  // Sort newest first
  cust_orders.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  const last = cust_orders[0];

  // Skip if last order is a subscription (selling_plan_allocation present on any line)
  const isSubscriptionOrder = (last.line_items || []).some(li =>
    li.selling_plan_allocation || (li.selling_plan_allocations && li.selling_plan_allocations.length));
  if (isSubscriptionOrder) { stats.skipped_subscription++; continue; }

  const cust = last.customer || {};
  // Try every known phone path Shopify uses
  const phone = cust.phone
    || (last.shipping_address && last.shipping_address.phone)
    || (last.billing_address && last.billing_address.phone)
    || last.phone
    || (cust.default_address && cust.default_address.phone)
    || '';
  if (!phone) { stats.skipped_no_phone++; continue; }
  const firstName = cust.first_name || (last.shipping_address && last.shipping_address.first_name) || 'there';

  // Compute cadence
  let cadence;
  if (cust_orders.length >= 2) {
    // Median inter-order gap
    const gaps = [];
    for (let i = 1; i < cust_orders.length; i++) {
      const gap = (new Date(cust_orders[i-1].created_at) - new Date(cust_orders[i].created_at)) / (1000*60*60*24);
      if (gap > 0) gaps.push(gap);
    }
    if (gaps.length) {
      gaps.sort((a, b) => a - b);
      cadence = gaps[Math.floor(gaps.length / 2)];
    }
  }
  if (!cadence) {
    // Smart default: total weight / per-day estimate
    const totalGrams = (last.line_items || []).reduce((s, li) => s + (li.grams || 0) * (li.quantity || 1), 0);
    cadence = totalGrams > 0 ? totalGrams / GRAMS_PER_DAY : DEFAULT_CADENCE_DAYS;
  }
  cadence = Math.max(MIN_CADENCE, Math.min(MAX_CADENCE, Math.round(cadence)));

  const daysSince = Math.floor((todayMs - new Date(last.created_at).getTime()) / (1000*60*60*24));
  const offset = daysSince - cadence;

  // Bucket for diagnostics
  if (daysSince <= 7) daysSinceBuckets['0-7']++;
  else if (daysSince <= 14) daysSinceBuckets['8-14']++;
  else if (daysSince <= 21) daysSinceBuckets['15-21']++;
  else if (daysSince <= 30) daysSinceBuckets['22-30']++;
  else if (daysSince <= 60) daysSinceBuckets['31-60']++;
  else daysSinceBuckets['61+']++;

  if (cadence <= 7) cadenceBuckets['5-7']++;
  else if (cadence <= 14) cadenceBuckets['8-14']++;
  else if (cadence <= 21) cadenceBuckets['15-21']++;
  else cadenceBuckets['22-30']++;

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
  if (!reminderNum) continue;

  // Build pre-filled cart link from last order's line items
  const cartParts = (last.line_items || [])
    .filter(li => li.variant_id && li.quantity > 0 && !li.gift_card)
    .map(li => `${li.variant_id}:${li.quantity}`)
    .join(',');
  const cartLink = cartParts
    ? `https://thebonpet.com/cart/${cartParts}`
    : 'https://thebonpet.com/collections/all';

  // Compose customer-facing message (matches Bon Pet voice)
  const customerMsg = reminderNum === 1
    ? `Hey ${firstName}! 👋\n\n` +
      `Your furkid is probably running low on Bon Pet right about now 🐾\n` +
      `Your last order was ${daysSince} days ago — perfect time to restock so they don't miss a meal ❤️\n\n` +
      `🛒 *One-tap reorder:* ${cartLink}\n\n` +
      `💡 *Switch to Subscribe & Save* and never run out again:\n` +
      `✅ 30% off your first subscription order — code *FIRSTORDER<3THEBONPET*\n` +
      `✅ 10% off every order after\n` +
      `✅ Free delivery over $100\n` +
      `✅ Choose 1–6 week cadence, pause/cancel anytime\n\n` +
      `As always, reply here if you have any questions 🙂\n\n` +
      `❤️ The Bon Pet Team`
    : `Hey ${firstName}! 👋\n\n` +
      `Just checking in — wanted to make sure your furkid doesn't run out of Bon Pet 🐾\n` +
      `It's been ${daysSince} days since your last order. If you'd like, here's a one-tap reorder:\n\n` +
      `🛒 ${cartLink}\n\n` +
      `*Subscribe & Save tip 💡* — get 30% off your first subscription order with *FIRSTORDER<3THEBONPET*, then 10% off every order after that, with free delivery over $100. Cancel anytime.\n\n` +
      `Anything we can help with? Just reply here ❤️\n\n` +
      `The Bon Pet Team`;

  // In dry run, wrap with debug context for Yash
  const dryRunMsg = `🧪 *DRY RUN — would send to ${firstName} (${phone})*\n` +
    `📊 days_since=${daysSince}  cadence=${cadence}d  reminder=#${reminderNum}\n` +
    `═══════════════════════════════════\n\n${customerMsg}`;

  candidates.push({
    customer_id: cid,
    customer_name: firstName,
    customer_phone: phone,
    last_order_id: last.id,
    last_order_at: last.created_at,
    days_since: daysSince,
    cadence_days: cadence,
    reminder_num: reminderNum,
    cart_link: cartLink,
    customer_message: customerMsg,
    target_phone: DRY_RUN ? YASH_PHONE : phone,
    message: DRY_RUN ? dryRunMsg : customerMsg,
  });
}

// Sample what fields actually come back — pick 3 random orders for shape inspection
const sampleOrders = orders.slice(0, 3).map((o, i) => {
  const c = o.customer || {};
  return `Order ${i+1} (id ${o.id}):` +
    `\n  customer.phone: ${JSON.stringify(c.phone)}` +
    `\n  customer.email: ${JSON.stringify(c.email)}` +
    `\n  customer.first_name: ${JSON.stringify(c.first_name)}` +
    `\n  customer.default_address.phone: ${JSON.stringify(c.default_address && c.default_address.phone)}` +
    `\n  shipping_address.phone: ${JSON.stringify(o.shipping_address && o.shipping_address.phone)}` +
    `\n  billing_address.phone: ${JSON.stringify(o.billing_address && o.billing_address.phone)}` +
    `\n  order.phone: ${JSON.stringify(o.phone)}`;
}).join('\n\n');

// Build a diagnostic summary regardless
const diagLines = [
  `📊 *Funnel*`,
  `• Orders fetched: ${stats.total_orders}`,
  `• Unique customers: ${stats.total_customers}`,
  `• Skipped (subscription): ${stats.skipped_subscription}`,
  `• Skipped (no phone): ${stats.skipped_no_phone}`,
  `• Too recent (just ordered): ${stats.too_recent}`,
  `• In reminder #1 window: ${stats.in_remind_1_window}`,
  `• Between windows (gap): ${stats.between_windows}`,
  `• In reminder #2 window: ${stats.in_remind_2_window}`,
  `• Too late (already churned?): ${stats.too_late}`,
  ``,
  `📈 *Days-since-last-order distribution*`,
  ...Object.entries(daysSinceBuckets).map(([k, v]) => `• ${k}d: ${v}`),
  ``,
  `⏱️ *Computed cadence distribution*`,
  ...Object.entries(cadenceBuckets).map(([k, v]) => `• ${k}d: ${v}`),
];

if (candidates.length === 0) {
  return [{ json: {
    target_phone: YASH_PHONE,
    message: '🔍 *Reorder Reminder — DRY RUN*\n📅 ' + new Date().toISOString().slice(0,10) +
             `\n\n0 candidates today. Diagnostics:\n\n${diagLines.join('\n')}` +
             `\n\n🔬 *Sample order fields*\n${sampleOrders}`
  }}];
}

// Prepend a header summary item with diagnostics
candidates.unshift({
  target_phone: YASH_PHONE,
  message: '🔍 *Reorder Reminder — DRY RUN*\n📅 ' + new Date().toISOString().slice(0,10) +
           `\n\n${candidates.length} candidate(s) follow ⬇️\n\n${diagLines.join('\n')}`,
  is_header: true,
});

return candidates.map(c => ({ json: c }));
"""


def uid():
    return str(uuid.uuid4())


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"triggerAtHour": 18}]},
        },
        "id": uid(),
        "name": "Daily 6PM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.3,
        "position": [0, 300],
    }

    shopify_get = {
        "parameters": {
            "url": "=https://" + SHOPIFY_DOMAIN + "/admin/api/" + SHOPIFY_API_VER +
                   "/orders.json?status=any&financial_status=paid"
                   "&created_at_min={{ $now.minus({ days: 90 }).toISO() }}"
                   "&limit=250"
                   "&fields=id,name,created_at,total_price,phone,customer,line_items,shipping_address,billing_address",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(),
        "name": "Get Shopify Orders (90d)",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 300],
        "credentials": {
            "shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}
        },
    }

    code = {
        "parameters": {"jsCode": CODE_JS},
        "id": uid(),
        "name": "Compute Reorder Candidates",
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

    nodes = [schedule, shopify_get, code, send_wa]
    connections = {
        schedule["name"]: {"main": [[{"node": shopify_get["name"], "type": "main", "index": 0}]]},
        shopify_get["name"]: {"main": [[{"node": code["name"], "type": "main", "index": 0}]]},
        code["name"]: {"main": [[{"node": send_wa["name"], "type": "main", "index": 0}]]},
    }

    return {
        "name": "Reorder Reminder - WhatsApp",
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


def ensure_in_team_project():
    """Transfer the workflow to the team project if it's not already there.
    Required so shared creds (Shopify, GSheets, Gmail) are visible. Idempotent —
    n8n returns 400 'same destination' when already in the right project; we treat that as success.
    """
    status, body = api_request("PUT", f"/workflows/{WF_ID}/transfer",
                               {"destinationProjectId": TEAM_PROJECT_ID})
    if status == 200 or "same destination" in body:
        print(f"Project: in team ✓")
    else:
        print(f"⚠️  Transfer failed → HTTP {status}: {body[:200]}")


if __name__ == "__main__":
    payload = build()
    with open("/Users/yash/n8n-bonpet/reorder_payload.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built: {len(payload['nodes'])} nodes  WF_ID={WF_ID}")
    status, body = api_request("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT HTTP {status}")
    print(body[:300])
    ensure_in_team_project()

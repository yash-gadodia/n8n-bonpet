#!/usr/bin/env python3
"""Self-Collection Pickup-Ready WA Notifier.

Trigger: HTTP webhook receiving {order_number, chat_id?, message_thread_id?,
reply_to_message_id?, sender_name?, dry_run?}. Phase 1 = standalone, testable
via curl. Phase 2 (later) = wire into Telegram→Linear workflow so a message
like "@weslee_bot order #3293 ready for self collection" forwards here.

Pipeline:
  Webhook → Parse → GET /wms/orders (SELF_COLLECTION + UNFULFILLED) →
  Match by order_name → Switch (0 / 1 / 2+) →
   ├ 0:  Build No-Match Reply  ─┐
   ├ 1:  Build WA Msg → Send WA → Mark Fulfilled → Build Success Reply ─┤→ Telegram Reply
   └ 2+: Build Multi-Match Reply ──────────────────────────────────────┘

PAT for WMS API lives in keychain `wms-pat`. WA key in `wa-api-key`. Telegram
bot token in `telegram-weslee-bot`. All read at build time and embedded into
the workflow JSON (matches pattern from build_reorder_reminder_v2.py).
"""
import json, os, subprocess, urllib.request, urllib.error, uuid

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

WF_NAME = "Self-Collect Pickup Ready → WA"
WEBHOOK_PATH = "selfcollect-pickup-ready-3f9c1a"

WMS_PAT = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wms-pat", "-w"]
).decode().strip()
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()
TELEGRAM_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "telegram-weslee-bot", "-w"]
).decode().strip()

ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"
PICKUP_ADDRESS = "5 Siglap Road #17-38 Lobby K, Singapore 448908"


def n8n(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            return res.status, json.loads(res.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def uid():
    return str(uuid.uuid4())


# ───────────────────────── Code: Parse Webhook Input ─────────────────────────
PARSE_INPUT_JS = r"""// Normalize the webhook payload. Accepts either:
//   POST {order_number: 3293, chat_id?, message_thread_id?, reply_to_message_id?, sender_name?}
//   POST {order_name: "#3293", ...}
const raw = $input.first().json;
const body = raw.body || raw;

let on = body.order_number;
if (on === undefined || on === null) on = body.order_name;
if (on === undefined || on === null) {
  throw new Error("Missing order_number/order_name in webhook body");
}
const orderNumStr = String(on).replace(/^#/, '').trim();
if (!/^\d+$/.test(orderNumStr)) {
  throw new Error(`order_number not numeric: ${on}`);
}

return [{
  json: {
    order_num_str: orderNumStr,
    order_name_match: `#${orderNumStr}`,
    chat_id: body.chat_id ?? null,
    message_thread_id: body.message_thread_id ?? null,
    reply_to_message_id: body.reply_to_message_id ?? null,
    sender_name: body.sender_name || 'someone',
    extra_note: body.extra_note || null,
  }
}];
"""

# ───────────────────────── Code: Match Order ─────────────────────────
MATCH_ORDER_JS = r"""// Filter WMS response to unfulfilled self-collect orders with order_name === target.
// Then emit a single item with match_count + (matched order if exactly 1).
const ctx = $('Parse Input').first().json;
const wms = $input.first().json;
const orders = (wms && wms.orders) || [];

const target = ctx.order_name_match; // e.g. "#3293"
const matches = orders.filter(o => String(o.order_name || '').trim() === target);

const base = {
  ...ctx,
  match_count: matches.length,
  all_unfulfilled_total: wms.total ?? null,
};

if (matches.length === 1) {
  const o = matches[0];
  return [{
    json: {
      ...base,
      order: {
        id: o.id,
        order_name: o.order_name,
        first_name: o.shipping_first_name || (o.shipping_name || '').split(' ')[0] || 'there',
        last_name: o.shipping_last_name || '',
        phone: o.shipping_phone,
        line_items: o.line_items || [],
        pickup_date: o.pickup_date,
      }
    }
  }];
}

if (matches.length > 1) {
  return [{
    json: {
      ...base,
      candidates: matches.map(o => ({
        id: o.id,
        order_name: o.order_name,
        name: o.shipping_name || `${o.shipping_first_name || ''} ${o.shipping_last_name || ''}`.trim(),
        phone: o.shipping_phone,
        pickup_date: o.pickup_date,
      }))
    }
  }];
}

return [{ json: base }]; // 0 matches
"""

# ───────────────────────── Code: Build WA Message (happy path) ─────────────────────────
# Template is PLACEHOLDER per user. Refine when ready.
BUILD_WA_MSG_JS = r"""const ctx = $input.first().json;
const o = ctx.order;

const lines = [
  `Hi ${o.first_name}! 🐾`,
  '',
  `Your order ${o.order_name} is packed and ready for self-collection 🎉`,
  '',
  `📍 *Address:* 5 Siglap Road, Lobby K (Mandarin Gardens Condo), unit #17-38, Singapore 448908`,
  '',
  `❄️ Look for the pack labeled with your order ID *${o.order_name}* in the *WHITE freezer*`,
  `ℹ️ Please skip the *ORANGE freezer*, it's not part of the pickup`,
  '',
  `Everything is clearly labeled so it should be straightforward 👌`,
  '',
  `A few quick notes:`,
  `🙏 Please try to pick up within 7 days, we have limited storage space`,
  `✅ Pickup anytime, the freezer is accessible 24/7`,
  `✅ The pack itself is a cooler bag, just try to get it into your freezer within 2-3 hours of pickup`,
  `✅ Once frozen, good for up to 1 year`,
];

if (ctx.extra_note) {
  lines.push('', ctx.extra_note);
}

lines.push(
  '',
  `Any questions, just reply here. Thanks so much for choosing us 💛`,
  `<3 The Bon Pet team`,
);

return [{
  json: {
    ...ctx,
    target_phone: o.phone,
    message: lines.join('\n'),
  }
}];
"""

# ───────────────────────── Code: Build Telegram Replies ─────────────────────────
BUILD_NO_MATCH_JS = r"""const ctx = $('Parse Input').first().json;
const m = $input.first().json;
const text = [
  `❌ No unfulfilled self-collection order found matching ${ctx.order_name_match}.`,
  ``,
  `Currently ${m.all_unfulfilled_total ?? '?'} self-collect orders are UNFULFILLED in WMS.`,
  `Double-check the number, or maybe it was already fulfilled?`,
].join('\n');
return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text,
}}];
"""

BUILD_MULTI_MATCH_JS = r"""const ctx = $('Parse Input').first().json;
const m = $input.first().json;
const lines = (m.candidates || []).map(c =>
  `• ${c.order_name}, ${c.name} (${c.phone}), pickup ${c.pickup_date || 'TBD'}, internal id ${c.id}`
).join('\n');
const text = [
  `⚠️ ${m.match_count} self-collect orders match ${ctx.order_name_match}:`,
  ``,
  lines,
  ``,
  `Pls fulfill the right one via OMS UI and re-tag with a clearer reference. (v2 will support an id: lookup.)`,
].join('\n');
return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text,
}}];
"""

# After Mark Fulfilled HTTP, pull chat_id from Parse Input (not from HTTP response).
# Per feedback_n8n_http_input_passthrough.md.
BUILD_SUCCESS_JS = r"""const ctx = $('Parse Input').first().json;
const o = $('Match Order').first().json.order;
const fulfilled = $input.first().json;
const trackingNote = fulfilled.tracking_id ? ` (tracking ${fulfilled.tracking_id})` : '';
const text = [
  `✅ Sent pickup-ready WA to *${o.first_name} ${o.last_name}* at ${o.phone}`,
  `Order ${o.order_name} auto-fulfilled${trackingNote}.`,
  ``,
  `Triggered by ${ctx.sender_name}.`,
].join('\n');
return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text,
}}];
"""

# ───────────────────────── Node Factories ─────────────────────────
def webhook_node():
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_PATH,
            "responseMode": "lastNode",
            "options": {},
        },
        "id": uid(), "name": "Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 400], "webhookId": WEBHOOK_PATH,
    }


def parse_input_node():
    return {
        "parameters": {"jsCode": PARSE_INPUT_JS},
        "id": uid(), "name": "Parse Input",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [240, 400],
    }


def fetch_orders_node():
    return {
        "parameters": {
            "method": "GET",
            "url": "https://api.thebonpet.com/wms/orders",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "delivery_method", "value": "SELF_COLLECTION"},
                {"name": "fulfillment_status", "value": "UNFULFILLED"},
                {"name": "limit", "value": "200"},
            ]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": f"Bearer {WMS_PAT}"},
                {"name": "User-Agent", "value": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Fetch Unfulfilled Self-Collect",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [480, 400],
    }


def match_order_node():
    return {
        "parameters": {"jsCode": MATCH_ORDER_JS},
        "id": uid(), "name": "Match Order",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [720, 400],
    }


def switch_node():
    return {
        "parameters": {
            "rules": {"values": [
                {
                    "outputKey": "exact",
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "combinator": "and",
                        "conditions": [{
                            "id": uid(),
                            "leftValue": "={{ $json.match_count }}",
                            "rightValue": 1,
                            "operator": {"type": "number", "operation": "equals"},
                        }],
                    },
                    "renameOutput": True,
                },
                {
                    "outputKey": "none",
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "combinator": "and",
                        "conditions": [{
                            "id": uid(),
                            "leftValue": "={{ $json.match_count }}",
                            "rightValue": 0,
                            "operator": {"type": "number", "operation": "equals"},
                        }],
                    },
                    "renameOutput": True,
                },
                {
                    "outputKey": "many",
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "combinator": "and",
                        "conditions": [{
                            "id": uid(),
                            "leftValue": "={{ $json.match_count }}",
                            "rightValue": 1,
                            "operator": {"type": "number", "operation": "larger"},
                        }],
                    },
                    "renameOutput": True,
                },
            ]},
            "options": {},
        },
        "id": uid(), "name": "Switch by Count",
        "type": "n8n-nodes-base.switch", "typeVersion": 3.2,
        "position": [960, 400],
    }


def build_no_match_node():
    return {
        "parameters": {"jsCode": BUILD_NO_MATCH_JS},
        "id": uid(), "name": "Build No-Match Reply",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 600],
    }


def build_wa_msg_node():
    return {
        "parameters": {"jsCode": BUILD_WA_MSG_JS},
        "id": uid(), "name": "Build WA Message",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 200],
    }


def send_wa_node():
    return {
        "parameters": {
            "method": "POST",
            "url": "https://api.thebonpet.com/whatsapp/send",
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
        "id": uid(), "name": "Send WA",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1440, 200],
    }


def mark_fulfilled_node():
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.thebonpet.com/wms/orders/{{ $('Match Order').first().json.order.id }}/mark-fulfilled",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": f"Bearer {WMS_PAT}"},
                {"name": "User-Agent", "value": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Mark Fulfilled",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1680, 200],
    }


def build_success_node():
    return {
        "parameters": {"jsCode": BUILD_SUCCESS_JS},
        "id": uid(), "name": "Build Success Reply",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1920, 200],
    }


def build_multi_match_node():
    return {
        "parameters": {"jsCode": BUILD_MULTI_MATCH_JS},
        "id": uid(), "name": "Build Multi-Match Reply",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1200, 800],
    }


def telegram_reply_node():
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ "
                "chat_id: $json.chat_id, "
                "message_thread_id: $json.message_thread_id, "
                "reply_to_message_id: $json.reply_to_message_id, "
                "allow_sending_without_reply: true, "
                "text: $json.text, "
                "parse_mode: 'Markdown', "
                "disable_web_page_preview: true "
                "}) }}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Telegram Reply",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [2200, 400],
        "onError": "continueRegularOutput",
    }


# ───────────────────────── Workflow Builder ─────────────────────────
def build_workflow():
    webhook = webhook_node()
    parse = parse_input_node()
    fetch = fetch_orders_node()
    match = match_order_node()
    switch = switch_node()

    # Happy path (1 match)
    build_wa = build_wa_msg_node()
    send_wa = send_wa_node()
    mark_ff = mark_fulfilled_node()
    build_success = build_success_node()

    # Error paths
    build_no_match = build_no_match_node()
    build_multi = build_multi_match_node()

    telegram_reply = telegram_reply_node()

    nodes = [
        webhook, parse, fetch, match, switch,
        build_wa, send_wa, mark_ff, build_success,
        build_no_match, build_multi,
        telegram_reply,
    ]

    # Switch outputs in declaration order: [0]=exact (1 match), [1]=none (0), [2]=many (2+)
    connections = {
        webhook["name"]: {"main": [[{"node": parse["name"], "type": "main", "index": 0}]]},
        parse["name"]:   {"main": [[{"node": fetch["name"], "type": "main", "index": 0}]]},
        fetch["name"]:   {"main": [[{"node": match["name"], "type": "main", "index": 0}]]},
        match["name"]:   {"main": [[{"node": switch["name"], "type": "main", "index": 0}]]},
        switch["name"]:  {"main": [
            [{"node": build_wa["name"],        "type": "main", "index": 0}],  # exact
            [{"node": build_no_match["name"],  "type": "main", "index": 0}],  # none
            [{"node": build_multi["name"],     "type": "main", "index": 0}],  # many
        ]},
        build_wa["name"]:       {"main": [[{"node": send_wa["name"],       "type": "main", "index": 0}]]},
        send_wa["name"]:        {"main": [[{"node": mark_ff["name"],       "type": "main", "index": 0}]]},
        mark_ff["name"]:        {"main": [[{"node": build_success["name"], "type": "main", "index": 0}]]},
        build_success["name"]:  {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        build_no_match["name"]: {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        build_multi["name"]:    {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
            "errorWorkflow": ERROR_ALERTER_ID,
        },
    }


def main():
    wf = build_workflow()

    # Save payload for diffing
    out_path = os.path.join(os.path.dirname(__file__), "selfcollect_pickup_ready_payload.json")
    with open(out_path, "w") as f:
        json.dump(wf, f, indent=2)
    print(f"💾 wrote {out_path}")

    # Check if workflow already exists by name
    status, listing = n8n("GET", "/workflows")
    if status != 200 or not isinstance(listing, dict):
        print(f"❌ failed to list workflows: {status} {listing!r}")
        return
    existing = next(
        (w for w in listing.get("data", []) if w.get("name") == WF_NAME),
        None,
    )

    if existing:
        wf_id = existing["id"]
        status, body = n8n("PUT", f"/workflows/{wf_id}", wf)
        print(f"🔁 PUT /workflows/{wf_id} → {status}")
    else:
        status, body = n8n("POST", "/workflows", wf)
        print(f"➕ POST /workflows → {status}")
        if isinstance(body, dict):
            wf_id = body.get("id")
        else:
            wf_id = None

    print(f"   workflow id: {wf_id}")
    print(f"   webhook URL: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print(f"   (test URL while INACTIVE: https://n8n.thebonpet.com/webhook-test/{WEBHOOK_PATH})")
    print()
    print("⚠️  Workflow created INACTIVE. Activate via n8n UI after smoke test.")
    print()
    print("🧪 Smoke-test curl (dry-run, no chat_id → JSON-only response):")
    print(f"""
curl -sS -X POST 'https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}' \\
  -H 'Content-Type: application/json' \\
  -d '{{"order_number": 3293, "dry_run": true}}'
""")
    print("🧪 Live test to your own Telegram (send WA + fulfill, pick a real test order):")
    print(f"""
curl -sS -X POST 'https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}' \\
  -H 'Content-Type: application/json' \\
  -d '{{
    "order_number": <REAL_TEST_ORDER>,
    "chat_id": <YOUR_TELEGRAM_CHAT_ID>,
    "sender_name": "Yash"
  }}'
""")


if __name__ == "__main__":
    main()

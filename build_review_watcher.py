#!/usr/bin/env python3
"""Review Watcher — hourly poll of Google Places API for Bon Pet's reviews.
Handles both directions:
  * ≤ 2★  → team alert (all 5)
  * = 5★  + exact name match in Customers tab → founder thank-you WA + promo code
  * = 5★  no match, or 3-4★, or already in log → skip

Dedup via `review_log` tab keyed on review_id.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
import subprocess
from _sent_log import (
    read_global_sent_log_node, filter_recent_sent_log_node,
    append_global_sent_log_node, COOLDOWN_JS_SNIPPET,
)

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Review Watcher - WhatsApp"
OLD_WF_NAME = "Negative Review Watcher - WhatsApp"  # migrate from this if found

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "review-watcher-manual-8e3c7a1f4d"  # unchanged from original

GOOGLE_PLACES_API_KEY = "AIzaSyCBcrw6RXpE2Vmar5X6NzowmMfYWTE5Vlk"
BON_PET_PLACE_ID      = "ChIJqWbpqTwX2jERSJWIxogqqsg"

NEGATIVE_STAR_THRESHOLD = 2  # <= this → team alert + customer apology (if matched)
POSITIVE_STAR_TRIGGER   = 4  # >= this → customer thanks (if matched)

# Placeholder — swap in a real Shopify discount code when created.
# Suggestion: create "THANKYOU<3THEBONPET" for 15-20% off next order.
REVIEW_THANK_YOU_PROMO_CODE = "THANKYOU<3THEBONPET"

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REVIEW_LOG_TAB_GID = 709923135
CUSTOMERS_TAB_GID = 100100

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6581114800",  # Shaun
    "+6282240119788",  # Bari (CS agent, ID)
]

PARSE_REVIEWS_JS = r"""// Parse Google Places API response → per-review items
const response = $input.first().json;
const reviews = (response.result && response.result.reviews) || response.reviews || [];
const placeRating = response.result && response.result.rating;
const totalRatings = response.result && response.result.user_ratings_total;

return reviews.map(r => ({
  json: {
    review_id: `${r.time}_${(r.author_name || '').slice(0, 30)}`,
    author_name: r.author_name || '(anonymous)',
    rating: Number(r.rating || 0),
    text: r.text || '',
    relative_time: r.relative_time_description || '',
    time_epoch: r.time,
    time_iso: new Date((r.time || 0) * 1000).toISOString(),
    language: r.language || 'en',
    place_overall_rating: placeRating,
    place_total_ratings: totalRatings,
  }
}));
"""

DECIDE_ACTION_JS = (r"""// Decide what to do with each review: team_alert | customer_thanks | skip (log-only)
const NEG_THRESHOLD = __NEG__;
const POS_TRIGGER   = __POS__;
const PROMO = "__PROMO__";

function tryRead(name) { try { return $(name).all(); } catch (e) { return []; } }
const reviews   = $('Parse Reviews').all().map(it => it.json);
const logRows   = tryRead('Read Review Log');
const customers = tryRead('Read Customers Tab');

const loggedIds = new Set(logRows.map(r => String(r.json.review_id || '')));
// Per-customer dedup so we don't spam the same person across multiple reviews.
const alreadyThanked = new Set(
  logRows
    .filter(r => String(r.json.action || '') === 'customer_thanks')
    .map(r => String(r.json.matched_customer_id || ''))
    .filter(Boolean)
);
const alreadyApologized = new Set(
  logRows
    .filter(r => String(r.json.action || '') === 'customer_apology')
    .map(r => String(r.json.matched_customer_id || ''))
    .filter(Boolean)
);

function normPhone(p) {
  if (!p) return '';
  const s = String(p).trim();
  if (s.startsWith('+')) return s;
  const d = s.replace(/[^\d]/g, '');
  if (/^\d{8}$/.test(d)) return '+65' + d;
  if (/^65\d{8}$/.test(d)) return '+' + d;
  if (/^\d{6,}$/.test(d)) return '+' + d;
  return '';
}
// Alias for the cooldown snippet which calls normalizePhone
const normalizePhone = normPhone;
""" + COOLDOWN_JS_SNIPPET + r"""

function normalizeName(s) {
  return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

// Two-tier matcher: exact full-name first, then fuzzy (prefix on each token).
// Either tier still requires a UNIQUE match to avoid false positives.
function matchCustomer(authorName) {
  const target = normalizeName(authorName);
  if (!target || target.length < 3) return null;
  const targetTokens = target.split(' ').filter(Boolean);

  const exactHits = [];
  const fuzzyHits = [];

  for (const c of customers) {
    const j = c.json;
    const fn = normalizeName(j.first_name);
    const ln = normalizeName(j.last_name);
    if (!fn && !ln) continue;

    const combo1 = `${fn} ${ln}`.trim();
    const combo2 = `${ln} ${fn}`.trim();
    if (combo1 === target || combo2 === target) {
      exactHits.push(j);
      continue;
    }

    // Fuzzy: every target token must prefix-match (either direction) some customer token.
    // Requires min token length of 3 and at least 2 tokens in target (to avoid single-name collisions).
    if (targetTokens.length < 2) continue;
    const custTokens = [fn, ln].filter(t => t && t.length >= 2);
    if (custTokens.length === 0) continue;

    const allMatch = targetTokens.every(t => {
      if (t.length < 3) return false;
      return custTokens.some(ct => ct.startsWith(t) || t.startsWith(ct));
    });
    if (allMatch) fuzzyHits.push(j);
  }

  // Exact wins if unique. Otherwise fall back to fuzzy if unique.
  if (exactHits.length === 1) return exactHits[0];
  if (exactHits.length === 0 && fuzzyHits.length === 1) return fuzzyHits[0];
  return null;  // zero, multiple exact, or multiple fuzzy → skip
}

const output = [];
for (const r of reviews) {
  if (loggedIds.has(String(r.review_id))) continue;  // already processed

  if (r.rating <= NEG_THRESHOLD) {
    // Always alert team
    output.push({
      json: { ...r, action: 'team_alert', matched_customer_id: '', logged_at: new Date().toISOString() }
    });
    // Try to also reach the customer with an apology
    const matchedNeg = matchCustomer(r.author_name);
    if (matchedNeg) {
      const cidNeg = String(matchedNeg.customer_id || '');
      if (cidNeg && !alreadyApologized.has(cidNeg)) {
        const phoneNeg = normPhone(matchedNeg.phone || matchedNeg.default_address_phone);
        if (phoneNeg && !isInGlobalCooldown(phoneNeg)) {
          output.push({
            json: {
              ...r,
              action: 'customer_apology',
              matched_customer_id: cidNeg,
              matched_first_name: String(matchedNeg.first_name || '').trim(),
              matched_phone: phoneNeg,
              logged_at: new Date().toISOString(),
            }
          });
        } else if (phoneNeg && isInGlobalCooldown(phoneNeg)) {
          output.push({
            json: { ...r, action: 'log_only', matched_customer_id: cidNeg, skip_reason: 'global 7d cooldown (apology)', logged_at: new Date().toISOString() }
          });
        }
      }
    }
  } else if (r.rating >= POS_TRIGGER) {
    const matched = matchCustomer(r.author_name);
    if (matched) {
      const cid = String(matched.customer_id || '');
      if (cid && alreadyThanked.has(cid)) {
        output.push({
          json: { ...r, action: 'log_only', matched_customer_id: cid, skip_reason: 'customer already thanked previously', logged_at: new Date().toISOString() }
        });
        continue;
      }
      const phone = normPhone(matched.phone || matched.default_address_phone);
      if (phone && isInGlobalCooldown(phone)) {
        output.push({
          json: { ...r, action: 'log_only', matched_customer_id: String(matched.customer_id || ''), skip_reason: 'global 7d cooldown (thanks)', logged_at: new Date().toISOString() }
        });
      } else if (phone) {
        output.push({
          json: {
            ...r,
            action: 'customer_thanks',
            matched_customer_id: String(matched.customer_id || ''),
            matched_first_name: String(matched.first_name || '').trim(),
            matched_phone: phone,
            promo_code: PROMO,
            logged_at: new Date().toISOString(),
          }
        });
      } else {
        output.push({
          json: { ...r, action: 'log_only', matched_customer_id: String(matched.customer_id || ''), skip_reason: 'no phone on matched customer', logged_at: new Date().toISOString() }
        });
      }
    } else {
      output.push({
        json: { ...r, action: 'log_only', matched_customer_id: '', skip_reason: 'no unique name match', logged_at: new Date().toISOString() }
      });
    }
  }
  // 3-4 star: ignore entirely (don't log, don't act — let them re-enter window if rating changes)
}

return output;
""").replace("__NEG__", str(NEGATIVE_STAR_THRESHOLD)) \
   .replace("__POS__", str(POSITIVE_STAR_TRIGGER)) \
   .replace("__PROMO__", REVIEW_THANK_YOU_PROMO_CODE)


FORMAT_TEAM_ALERT_JS = r"""// Negative review → team alert message (one per input item)
const reviewUrl = `https://search.google.com/local/reviews?placeid=__PLACE_ID__`;
return $input.all().map(it => {
  const r = it.json;
  const stars = '⭐'.repeat(r.rating) + '☆'.repeat(Math.max(0, 5 - r.rating));
  const textPreview = (r.text || '').length > 400
    ? (r.text.slice(0, 400) + '...')
    : (r.text || '_(no review text)_');
  const msg = `🚨 *Negative review alert*
_${r.relative_time || r.time_iso}_

${stars}  *${r.rating}/5*
👤 ${r.author_name}

"${textPreview}"

📈 Overall: ${r.place_overall_rating}★ (${r.place_total_ratings} reviews)

🔗 Respond: ${reviewUrl}`;
  return { json: { ...r, message: msg } };
});
""".replace("__PLACE_ID__", BON_PET_PLACE_ID)


FORMAT_CUSTOMER_THANKS_JS = r"""// Positive review (4★ or 5★) + matched customer → thank-you + promo code
return $input.all().map(it => {
  const r = it.json;
  const name = r.matched_first_name || '';
  const greeting = name ? `Hi ${name}!` : 'Hi!';
  const ratingPhrase = `(${r.rating} stars! 🥹)`;
  const msg = `${greeting} 🐾

Yash here from The Bon Pet. Just saw your review pop up on Google ${ratingPhrase} and had to say thank you personally. Reviews from happy pet parents mean the world to our small team in SG.

As a small thank-you, here's 10% off your next order:
🎁 *${r.promo_code}*

Hope your furkid's been loving the food. Any feedback or new ideas, just reply here 😊

❤️ Yash & the Bon Pet team`;
  return { json: {
    message: msg,
    customer_phone: r.matched_phone,
    review_id: r.review_id,
    author_name: r.author_name,
    matched_customer_id: r.matched_customer_id,
    matched_first_name: r.matched_first_name,
    promo_code: r.promo_code,
    rating: r.rating,
    action: 'customer_thanks',
    logged_at: r.logged_at,
    // wa_sent_log (global) append fields
    phone: r.matched_phone,
    workflow: 'review_watcher',
    template: 'review_thanks_' + r.rating + 'star',
    sent_at: new Date().toISOString(),
    order_id: '',
    notes: 'review_id=' + r.review_id,
  }};
});
"""

FORMAT_CUSTOMER_APOLOGY_JS = r"""// Negative review + matched customer → founder apology (no promo code — tone-deaf)
return $input.all().map(it => {
  const r = it.json;
  const name = r.matched_first_name || '';
  const greeting = name ? `Hi ${name}!` : 'Hi!';
  const msg = `${greeting} 🐾

Yash here from The Bon Pet. Just saw your review pop up and wanted to reach out directly. I'm really sorry your experience wasn't what you hoped for.

Would love to understand what went wrong so we can do better. If you have 30 seconds to share, just reply here and I'll make sure it gets sorted.

And if there's anything we can do to make it up to you (refund, replacement, something different), just say the word.

❤️ Yash & the Bon Pet team`;
  return { json: {
    message: msg,
    customer_phone: r.matched_phone,
    review_id: r.review_id,
    author_name: r.author_name,
    matched_customer_id: r.matched_customer_id,
    matched_first_name: r.matched_first_name,
    rating: r.rating,
    action: 'customer_apology',
    logged_at: r.logged_at,
    // wa_sent_log (global) append fields
    phone: r.matched_phone,
    workflow: 'review_watcher',
    template: 'review_apology_' + r.rating + 'star',
    sent_at: new Date().toISOString(),
    order_id: '',
    notes: 'review_id=' + r.review_id,
  }};
});
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
    return {"parameters": {"jsCode": js}, "id": uid(), "name": name,
            "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos}


def sheet_ref(gid):
    return {
        "documentId": {
            "__rl": True, "value": SHEET_ID, "mode": "list",
            "cachedResultName": "Customer Orders DB",
            "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        },
        "sheetName": {
            "__rl": True, "value": gid, "mode": "list",
            "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={gid}",
        },
    }


def if_action(name, pos, action):
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.action }}",
                    "rightValue": action,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": pos,
    }


def team_wa_node(name, pos, phone):
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
                {"name": "phone_number", "value": phone},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def customer_wa_node(name, pos):
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
                {"name": "phone_number", "value": "={{ $json.customer_phone }}"},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def build():
    schedule = {
        # Stagger off HH:00 to avoid memory pressure when other crons cluster.
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "7 * * * *"}]}},
        "id": uid(), "name": "Hourly",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 400],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
                       "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 200], "webhookId": MANUAL_WEBHOOK_ID,
    }

    fetch_reviews = {
        "parameters": {
            "method": "GET",
            "url": (
                "=https://maps.googleapis.com/maps/api/place/details/json"
                f"?place_id={BON_PET_PLACE_ID}"
                "&fields=reviews,rating,user_ratings_total,name"
                f"&key={GOOGLE_PLACES_API_KEY}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Fetch Reviews",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 300],
    }
    parse = code_node("Parse Reviews", [480, 200], PARSE_REVIEWS_JS)

    read_log = {
        "parameters": {**sheet_ref(REVIEW_LOG_TAB_GID), "options": {}},
        "id": uid(), "name": "Read Review Log",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [480, 400],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
        "executeOnce": True,
    }
    read_customers = {
        "parameters": {**sheet_ref(CUSTOMERS_TAB_GID), "options": {}},
        "id": uid(), "name": "Read Customers Tab",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [480, 600],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
        "executeOnce": True,
    }

    read_global = read_global_sent_log_node([480, 800])
    filter_global = filter_recent_sent_log_node([640, 800])

    merge = {
        "parameters": {"numberInputs": 4},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3,
        "position": [880, 400],
    }

    decide = code_node("Decide Action", [960, 400], DECIDE_ACTION_JS)

    if_team        = if_action("Is Team Alert?",        [1200, 100], "team_alert")
    if_thanks      = if_action("Is Customer Thanks?",   [1200, 350], "customer_thanks")
    if_apology     = if_action("Is Customer Apology?",  [1200, 600], "customer_apology")

    format_team    = code_node("Format Team Alert",     [1440, 100], FORMAT_TEAM_ALERT_JS)
    format_thanks  = code_node("Format Customer Thanks",[1440, 350], FORMAT_CUSTOMER_THANKS_JS)
    format_apology = code_node("Format Customer Apology",[1440, 600], FORMAT_CUSTOMER_APOLOGY_JS)

    append_log = {
        "parameters": {
            "operation": "append",
            **sheet_ref(REVIEW_LOG_TAB_GID),
            "columns": {"mappingMode": "autoMapInputData", "matchingColumns": []},
            "options": {},
        },
        "id": uid(), "name": "Append to Log",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": [1680, 800],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    team_sends = [
        team_wa_node(f"Send Team #{i+1}", [1680, 50 + i * 60], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1680, 50 + len(RECIPIENTS) * 60]
    )
    send_thanks  = customer_wa_node("Send Customer Thanks",  [1680, 400])
    send_apology = customer_wa_node("Send Customer Apology", [1680, 600])
    log_global   = append_global_sent_log_node([1920, 500])

    nodes = [schedule, manual, fetch_reviews, parse, read_log, read_customers,
             read_global, filter_global, merge,
             decide, if_team, if_thanks, if_apology,
             format_team, format_thanks, format_apology,
             append_log, send_thanks, send_apology, log_global, *team_sends, telegram_send]

    connections = {
        schedule["name"]:       {"main": [[{"node": fetch_reviews["name"], "type": "main", "index": 0}]]},
        manual["name"]:         {"main": [[{"node": fetch_reviews["name"], "type": "main", "index": 0}]]},
        fetch_reviews["name"]:  {"main": [[
            {"node": parse["name"], "type": "main", "index": 0},
            {"node": read_log["name"], "type": "main", "index": 0},
            {"node": read_customers["name"], "type": "main", "index": 0},
            {"node": read_global["name"], "type": "main", "index": 0},
        ]]},
        parse["name"]:          {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_log["name"]:       {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        read_customers["name"]: {"main": [[{"node": merge["name"], "type": "main", "index": 2}]]},
        read_global["name"]:    {"main": [[{"node": filter_global["name"], "type": "main", "index": 0}]]},
        filter_global["name"]:  {"main": [[{"node": merge["name"], "type": "main", "index": 3}]]},
        merge["name"]:          {"main": [[{"node": decide["name"], "type": "main", "index": 0}]]},
        decide["name"]: {"main": [[
            {"node": if_team["name"],    "type": "main", "index": 0},
            {"node": if_thanks["name"],  "type": "main", "index": 0},
            {"node": if_apology["name"], "type": "main", "index": 0},
            {"node": append_log["name"], "type": "main", "index": 0},
        ]]},
        if_team["name"]:    {"main": [
            [{"node": format_team["name"], "type": "main", "index": 0}],
            [],
        ]},
        if_thanks["name"]:  {"main": [
            [{"node": format_thanks["name"], "type": "main", "index": 0}],
            [],
        ]},
        if_apology["name"]: {"main": [
            [{"node": format_apology["name"], "type": "main", "index": 0}],
            [],
        ]},
        format_team["name"]:    {"main": [[{"node": n["name"], "type": "main", "index": 0} for n in [*team_sends, telegram_send]]]},
        format_thanks["name"]:  {"main": [[{"node": send_thanks["name"], "type": "main", "index": 0}]]},
        format_apology["name"]: {"main": [[{"node": send_apology["name"], "type": "main", "index": 0}]]},
        send_thanks["name"]:    {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
        send_apology["name"]:   {"main": [[{"node": log_global["name"], "type": "main", "index": 0}]]},
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
    # Prefer new name, fall back to old name (migration path)
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME: return wf["id"]
    for wf in data.get("data", []):
        if wf.get("name") == OLD_WF_NAME: return wf["id"]
    return None


if __name__ == "__main__":
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/review_watcher_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes → {out}")

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

    print(f"\nManual fire: curl -X POST https://n8n.thebonpet.com/webhook/{MANUAL_WEBHOOK_ID}")
    print(f"Promo code in use: {REVIEW_THANK_YOU_PROMO_CODE}  (edit constant + rerun to change)")

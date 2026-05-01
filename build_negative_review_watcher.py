#!/usr/bin/env python3
"""DEPRECATED — superseded by build_review_watcher.py.

Review Watcher handles both directions (<=2* team alert + 5* customer thanks + 3-4* apology).
Running this script re-creates a duplicate workflow that would double-alert the team.
"""
import sys
print("DEPRECATED: use build_review_watcher.py instead. Exiting without deploy.")
sys.exit(0)

# --- Legacy code below kept for reference only, not executed ---
_LEGACY_DOCSTRING = """Negative Review Watcher — hourly poll of Google Places API for Bon Pet's reviews.
Dedup via a `review_log` tab so each review only alerts once.
Triggers team WA broadcast for reviews with rating <= 2.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Negative Review Watcher - WhatsApp"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_WEBHOOK_ID = "neg-review-manual-8e3c7a1f4d"

GOOGLE_PLACES_API_KEY = "AIzaSyCBcrw6RXpE2Vmar5X6NzowmMfYWTE5Vlk"
BON_PET_PLACE_ID      = "ChIJqWbpqTwX2jERSJWIxogqqsg"

# Rating threshold: alert on reviews at or below this
NEGATIVE_STAR_THRESHOLD = 2

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REVIEW_LOG_TAB_GID = 709923135  # `review_log` tab (created via create_review_log_tab.py)

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6581114800",  # Shaun
]

PARSE_REVIEWS_JS = r"""// Parse Google Places API response → per-review items
const response = $input.first().json;
const reviews = (response.result && response.result.reviews) || response.reviews || [];
const placeRating = response.result && response.result.rating;
const totalRatings = response.result && response.result.user_ratings_total;

return reviews.map(r => ({
  json: {
    review_id: `${r.time}_${(r.author_name || '').slice(0, 30)}`,  // stable-ish since Places doesn't give a real review_id
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

FILTER_NEW_NEGATIVE_JS = r"""// Keep only negative reviews (rating <= threshold) that aren't in the log yet
const THRESHOLD = __THRESHOLD__;
const items = $('Parse Reviews').all();
function tryRead(nodeName) {
  try { return $(nodeName).all(); } catch (e) { return []; }
}
const loggedRows = tryRead('Read Review Log');
const loggedIds = new Set(loggedRows.map(r => String(r.json.review_id || '')));

return items
  .filter(it => it.json.rating <= THRESHOLD)
  .filter(it => !loggedIds.has(String(it.json.review_id)))
  .map(it => it);
"""

FORMAT_ALERT_JS = r"""// Format negative-review team alert
const r = $input.first().json;

const stars = '⭐'.repeat(r.rating) + '☆'.repeat(Math.max(0, 5 - r.rating));
const textPreview = (r.text || '').length > 400
  ? (r.text.slice(0, 400) + '...')
  : (r.text || '_(no review text)_');

const reviewUrl = `https://search.google.com/local/reviews?placeid=__PLACE_ID__`;

const msg = `🚨 *Negative review alert*
_${r.relative_time || r.time_iso}_

${stars}  *${r.rating}/5*
👤 ${r.author_name}

"${textPreview}"

📈 Overall: ${r.place_overall_rating}★ (${r.place_total_ratings} reviews)

🔗 Respond: ${reviewUrl}`;

return [{ json: {
  message: msg,
  review_id: r.review_id,
  author_name: r.author_name,
  rating: r.rating,
  review_text: r.text,
  logged_at: new Date().toISOString(),
} }];
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


def send_wa_node(name, pos, phone):
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


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "0 * * * *"}]}
        },
        "id": uid(), "name": "Hourly",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.3,
        "position": [0, 400],
    }

    manual = {
        "parameters": {
            "httpMethod": "POST", "path": MANUAL_WEBHOOK_ID,
            "responseMode": "onReceived", "options": {},
        },
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

    parse = code_node("Parse Reviews", [480, 300], PARSE_REVIEWS_JS)

    read_log = {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": REVIEW_LOG_TAB_GID if REVIEW_LOG_TAB_GID is not None else 0,
                "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REVIEW_LOG_TAB_GID or 0}",
            },
            "options": {},
        },
        "id": uid(), "name": "Read Review Log",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [480, 500],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    merge = {
        "parameters": {"numberInputs": 2},
        "id": uid(), "name": "Merge Reads",
        "type": "n8n-nodes-base.merge", "typeVersion": 3.1,
        "position": [720, 400],
    }

    filter_new = code_node("Filter New Negative",
                           [960, 400],
                           FILTER_NEW_NEGATIVE_JS.replace("__THRESHOLD__", str(NEGATIVE_STAR_THRESHOLD)))

    format_alert = code_node("Format Alert",
                             [1200, 400],
                             FORMAT_ALERT_JS.replace("__PLACE_ID__", BON_PET_PLACE_ID))

    append_log = {
        "parameters": {
            "operation": "append",
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": REVIEW_LOG_TAB_GID if REVIEW_LOG_TAB_GID is not None else 0,
                "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={REVIEW_LOG_TAB_GID or 0}",
            },
            "columns": {"mappingMode": "autoMapInputData", "matchingColumns": []},
            "options": {},
        },
        "id": uid(), "name": "Append to Log",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [1440, 200],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    team_sends = [
        send_wa_node(f"Send Team #{i+1}", [1440, 300 + i * 100], p)
        for i, p in enumerate(RECIPIENTS)
    ]
    telegram_send = telegram_send_node(
        "Send Telegram Weslee", [1440, 300 + len(RECIPIENTS) * 100]
    )

    nodes = [schedule, manual, fetch_reviews, parse, read_log, merge,
             filter_new, format_alert, append_log, *team_sends, telegram_send]

    connections = {
        schedule["name"]:      {"main": [[{"node": fetch_reviews["name"], "type": "main", "index": 0}]]},
        manual["name"]:        {"main": [[{"node": fetch_reviews["name"], "type": "main", "index": 0}]]},
        fetch_reviews["name"]: {"main": [[
            {"node": parse["name"], "type": "main", "index": 0},
            {"node": read_log["name"], "type": "main", "index": 0},
        ]]},
        parse["name"]:         {"main": [[{"node": merge["name"], "type": "main", "index": 0}]]},
        read_log["name"]:      {"main": [[{"node": merge["name"], "type": "main", "index": 1}]]},
        merge["name"]:         {"main": [[{"node": filter_new["name"], "type": "main", "index": 0}]]},
        filter_new["name"]:    {"main": [[{"node": format_alert["name"], "type": "main", "index": 0}]]},
        format_alert["name"]:  {"main": [[
            {"node": append_log["name"], "type": "main", "index": 0},
            *[{"node": n["name"], "type": "main", "index": 0} for n in [*team_sends, telegram_send]],
        ]]},
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
    out = os.path.expanduser("~/n8n-bonpet/negative_review_watcher_payload.json")
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

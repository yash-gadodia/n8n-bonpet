#!/usr/bin/env python3
"""Build the Low Stock Watcher workflow JSON and PUT it to n8n."""
import json
import uuid
import os
import urllib.request
import urllib.error

from _notify import telegram_send_node
import subprocess

WF_ID = "lN4Bo5DOTVgGnpGl"
DOC_ID = "1yYzRL5pkpmoPflL_vzOUeI_eimaOTMH7gfv_INlplx8"
SHEET_GID = 887506772
SHEET_NAME = "FOOD PRODUCTION"

GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
RECIPIENTS = [
    "+6581394225",  # Yash
    "+6598531677",  # Nicolas
    "+6590108515",  # Bon Pet official
    "+6587993341",  # Rachel
    "+6282240119788",  # Bari (CS agent, ID)
]

CLASSIFY_JS = r"""// Filter stock rows, classify into critical (<50) and low (<100), format two WhatsApp messages
const items = $input.all();
const today = new Date().toLocaleDateString('en-GB', {
  timeZone: 'Asia/Singapore',
  day: '2-digit', month: 'short', year: 'numeric'
});

const critical = [];
const low = [];

for (const it of items) {
  const j = it.json;
  const product = String(j.PRODUCT || '').trim();
  if (!product.startsWith('GC ')) continue;

  const balance = Number(j.BALANCE);
  if (!Number.isFinite(balance)) continue;

  if (balance < 50) critical.push({ product, balance });
  else if (balance < 100) low.push({ product, balance });
}

critical.sort((a, b) => a.balance - b.balance);
low.sort((a, b) => a.balance - b.balance);

const critLines = critical.map(p => `🔴 *${p.product}* - only *${p.balance}* left`).join('\n');
const lowLines  = low.map(p => `• ${p.product} - ${p.balance}`).join('\n');

const criticalMsg = critical.length === 0 ? '' :
`🚨🚨🚨 *CRITICAL STOCK ALERT* 🚨🚨🚨

⛔ *URGENT - RESTOCK IMMEDIATELY* ⛔
📅 ${today}

The following products are below *50 units*:

${critLines}

⚠️ _Production needed ASAP to avoid stockouts._`;

const lowMsg = low.length === 0 ? '' :
`🟡 *Low Stock - The Bon Pet*
📅 ${today}

The following products are below *100 units*:

${lowLines}

_Plan production within the next few days._`;

const allOk = critical.length === 0 && low.length === 0;
const okMsg = !allOk ? '' : `✅ Stock OK, all GC SKUs ≥100 units (${today})`;

return [{
  json: {
    today,
    critical_count: critical.length,
    low_count: low.length,
    has_critical: critical.length > 0,
    has_low: low.length > 0,
    all_ok: allOk,
    critical_msg: criticalMsg,
    low_msg: lowMsg,
    ok_msg: okMsg,
    critical_items: critical,
    low_items: low
  }
}];
"""


def uid():
    return str(uuid.uuid4())


def whatsapp_node(name, pos, message_expr, phone):
    return {
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
                    {"name": "phone_number", "value": phone},
                    {"name": "message", "value": message_expr},
                ]
            },
            "options": {},
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
    }


def if_node(name, pos, left_value):
    return {
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "leftValue": left_value,
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                        "rightValue": "",
                        "id": uid(),
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(),
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": pos,
    }


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"triggerAtHour": 9}]}
        },
        "id": uid(),
        "name": "Daily 9AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 300],
    }

    read_sheet = {
        "parameters": {
            "documentId": {
                "__rl": True,
                "value": DOC_ID,
                "mode": "list",
                "cachedResultName": "Food Production / Stock",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit",
            },
            "sheetName": {
                "__rl": True,
                "value": SHEET_GID,
                "mode": "list",
                "cachedResultName": SHEET_NAME,
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit#gid={SHEET_GID}",
            },
            "options": {},
        },
        "id": uid(),
        "name": "Read Stock Sheet",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.5,
        "position": [240, 300],
        "credentials": {
            "googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}
        },
    }

    classify = {
        "parameters": {"jsCode": CLASSIFY_JS},
        "id": uid(),
        "name": "Classify & Format",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [480, 300],
    }

    if_critical = if_node("Has Critical?", [720, 100], "={{ $json.has_critical }}")
    if_low = if_node("Has Low?", [720, 500], "={{ $json.has_low }}")
    if_ok = if_node("All OK?", [720, 1300], "={{ $json.all_ok }}")

    crit_msg = "={{ $json.critical_msg }}"
    low_msg = "={{ $json.low_msg }}"
    ok_msg = "={{ $json.ok_msg }}"

    crit_sends = [whatsapp_node(f"Send Critical #{i+1}", [960, i*100], crit_msg, p)
                  for i, p in enumerate(RECIPIENTS)]
    low_sends = [whatsapp_node(f"Send Low #{i+1}", [960, 400 + i*100], low_msg, p)
                 for i, p in enumerate(RECIPIENTS)]
    ok_sends = [whatsapp_node(f"Send OK #{i+1}", [960, 1200 + i*100], ok_msg, p)
                for i, p in enumerate(RECIPIENTS)]
    telegram_crit = telegram_send_node(
        "Send Telegram Weslee (Critical)", [960, len(RECIPIENTS)*100], crit_msg
    )
    telegram_low = telegram_send_node(
        "Send Telegram Weslee (Low)", [960, 400 + len(RECIPIENTS)*100], low_msg
    )
    telegram_ok = telegram_send_node(
        "Send Telegram Weslee (OK)", [960, 1200 + len(RECIPIENTS)*100], ok_msg
    )

    nodes = [schedule, read_sheet, classify, if_critical, if_low, if_ok,
             *crit_sends, *low_sends, *ok_sends,
             telegram_crit, telegram_low, telegram_ok]

    connections = {
        schedule["name"]: {
            "main": [[{"node": read_sheet["name"], "type": "main", "index": 0}]]
        },
        read_sheet["name"]: {
            "main": [[{"node": classify["name"], "type": "main", "index": 0}]]
        },
        classify["name"]: {
            "main": [[
                {"node": if_critical["name"], "type": "main", "index": 0},
                {"node": if_low["name"], "type": "main", "index": 0},
                {"node": if_ok["name"], "type": "main", "index": 0},
            ]]
        },
        if_critical["name"]: {
            "main": [
                [{"node": n["name"], "type": "main", "index": 0} for n in [*crit_sends, telegram_crit]],
                [],
            ]
        },
        if_low["name"]: {
            "main": [
                [{"node": n["name"], "type": "main", "index": 0} for n in [*low_sends, telegram_low]],
                [],
            ]
        },
        if_ok["name"]: {
            "main": [
                [{"node": n["name"], "type": "main", "index": 0} for n in [*ok_sends, telegram_ok]],
                [],
            ]
        },
    }

    return {
        "name": "Low Stock Watcher - WhatsApp",
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
        },
    }


def put_workflow(payload):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    url = f"https://n8n.thebonpet.com/api/v1/workflows/{WF_ID}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


if __name__ == "__main__":
    payload = build()
    with open("/Users/yash/n8n-bonpet/low_stock_payload.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes")
    status, body = put_workflow(payload)
    print(f"PUT HTTP {status}")
    print(body[:2000])

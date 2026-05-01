#!/usr/bin/env python3
"""One-off: write the 17 column headers + rename Sheet1→orders into the new
'Bon Pet — Customer Orders DB' Google Sheet via a temporary n8n workflow.
The Sheets API call goes through the existing Google Sheets OAuth cred."""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"

HEADERS = [
    "received_at", "order_id", "order_name", "order_date",
    "customer_id", "first_name", "last_name", "email", "phone",
    "total_price", "currency", "total_grams", "is_subscription",
    "line_items_json", "cart_link", "city", "tags",
]


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def webhook_node():
    return {
        "parameters": {"httpMethod": "POST", "path": "setup-orders-headers", "responseMode": "lastNode", "options": {}},
        "id": str(uuid.uuid4()),
        "name": "Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
        "webhookId": str(uuid.uuid4()),
    }


def rename_node():
    body = {"requests": [{
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "title": "orders"},
            "fields": "title",
        }
    }]}
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps(body),
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Rename Sheet1 → orders",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "continueOnFail": True,
    }


def write_headers_via_node():
    """Use n8n's native Google Sheets node — appendOrUpdate with defineBelow mode
    auto-creates headers when sheet is empty."""
    cols = [{"name": h, "value": h} for h in HEADERS]
    return {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Bon Pet — Customer Orders DB",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": "gid=0", "mode": "list",
                "cachedResultName": "orders",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
            },
            "columns": {
                "mappingMode": "defineBelow",
                "value": {h: h for h in HEADERS},
                "matchingColumns": ["received_at"],
                "schema": [{"id": h, "displayName": h, "required": False, "defaultMatch": False, "display": True, "type": "string", "canBeUsedToMatch": True} for h in HEADERS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Seed Headers (via GS node)",
        "type": "n8n-nodes-base.googleSheets",
        "typeVersion": 4.7,
        "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


def write_headers_node():
    body = {
        "valueInputOption": "RAW",
        "data": [{"range": "orders!A1:Q1", "values": [HEADERS]}],
    }
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps(body),
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Write Headers",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [480, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


# --- Build & PUT workflow ---
trig = webhook_node()
rn = rename_node()
seed = write_headers_via_node()
nodes = [trig, rn, seed]
connections = {
    trig["name"]: {"main": [[{"node": rn["name"], "type": "main", "index": 0}]]},
    rn["name"]: {"main": [[{"node": seed["name"], "type": "main", "index": 0}]]},
}

# Create or reuse workflow
status, body = http("POST", "/workflows", {
    "name": "TEMP Setup Customer Orders DB",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
})
print(f"Create → {status}")
wf_id = json.loads(body)["id"]
print(f"  WF_ID = {wf_id}")

# Transfer to team project (so it can use shared cred)
status, body = http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
print(f"Transfer → {status}: {body[:80]}")

# Activate
status, body = http("POST", f"/workflows/{wf_id}/activate")
print(f"Activate → {status}")
time.sleep(1)

# Fire
url = "https://thebonpet.app.n8n.cloud/webhook/setup-orders-headers"
print(f"Firing: POST {url}")
req = urllib.request.Request(url, data=b'{}', method="POST", headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"  → HTTP {r.status}: {r.read().decode()[:200]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode()[:300]}")

# Check execution
time.sleep(2)
status, body = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
data = json.loads(body)
if data["data"]:
    ex = data["data"][0]
    print(f"\nExecution {ex['id']}: status={ex['status']}")
    runs = ex.get("data", {}).get("resultData", {}).get("runData", {})
    for n, outs in runs.items():
        out = outs[0]
        if "error" in out:
            print(f"  ❌ {n}: {out['error'].get('message', out['error'])[:200]}")
        else:
            print(f"  ✅ {n}: ok")

# Cleanup: delete the temp workflow
print(f"\nDeleting TEMP workflow {wf_id}")
status, body = http("DELETE", f"/workflows/{wf_id}")
print(f"  → HTTP {status}")

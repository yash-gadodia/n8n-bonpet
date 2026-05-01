#!/usr/bin/env python3
"""One-off: create `winback_sent` tab in Customer Orders DB sheet, seed headers,
print the GID for pasting into build_winback.py.
Mirrors setup_orders_db.py pattern (temp n8n workflow → Google Sheets OAuth cred)."""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
TAB_NAME = "winback_sent"
HEADERS = ["email", "sent_at", "days_since", "first_name"]


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
        "parameters": {"httpMethod": "POST", "path": "setup-winback-sent-tab", "responseMode": "lastNode", "options": {}},
        "id": str(uuid.uuid4()),
        "name": "Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
        "webhookId": str(uuid.uuid4()),
    }


def add_sheet_node():
    body = {"requests": [{
        "addSheet": {
            "properties": {"title": TAB_NAME},
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
        "name": "Add Sheet",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "continueOnFail": True,
    }


def write_headers_node():
    body = {
        "valueInputOption": "RAW",
        "data": [{"range": f"{TAB_NAME}!A1:D1", "values": [HEADERS]}],
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


trig = webhook_node()
add = add_sheet_node()
heads = write_headers_node()
nodes = [trig, add, heads]
connections = {
    trig["name"]: {"main": [[{"node": add["name"], "type": "main", "index": 0}]]},
    add["name"]:  {"main": [[{"node": heads["name"], "type": "main", "index": 0}]]},
}

status, body = http("POST", "/workflows", {
    "name": "TEMP Setup Winback Sent Tab",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
})
print(f"Create → {status}")
wf_id = json.loads(body)["id"]
print(f"  WF_ID = {wf_id}")

status, body = http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
print(f"Transfer → {status}: {body[:80]}")

status, body = http("POST", f"/workflows/{wf_id}/activate")
print(f"Activate → {status}")
time.sleep(1)

url = "https://thebonpet.app.n8n.cloud/webhook/setup-winback-sent-tab"
print(f"Firing: POST {url}")
req = urllib.request.Request(url, data=b'{}', method="POST", headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"  → HTTP {r.status}: {r.read().decode()[:200]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode()[:300]}")

time.sleep(2)
status, body = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
data = json.loads(body)
gid = None
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
            if n == "Add Sheet":
                try:
                    resp = out.get("data", {}).get("main", [[{}]])[0][0].get("json", {})
                    replies = resp.get("replies", [])
                    if replies and "addSheet" in replies[0]:
                        gid = replies[0]["addSheet"]["properties"]["sheetId"]
                except Exception as e:
                    print(f"  couldn't extract GID: {e}")

print(f"\nDeleting TEMP workflow {wf_id}")
status, body = http("DELETE", f"/workflows/{wf_id}")
print(f"  → HTTP {status}")

print()
if gid is not None:
    print(f"✅ winback_sent tab created. GID = {gid}")
    print(f"   Paste into build_winback.py:  WINBACK_SENT_TAB_GID = {gid}")
else:
    print(f"⚠️  Tab may already exist. Open the sheet and read GID from tab URL:")
    print(f"    https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

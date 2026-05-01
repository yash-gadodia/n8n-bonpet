#!/usr/bin/env python3
"""One-off cleanup: remove the 2 polluted leading columns + the orphan row 2
in the Customer Orders DB sheet, leaving the 17 real headers + real data."""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"


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


cleanup_body = {"requests": [
    # Delete columns A and B (the polluted spreadsheetId, replies headers)
    {"deleteDimension": {"range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2}}},
    # Delete row 2 (which had the polluted response values, now empty after col delete)
    {"deleteDimension": {"range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}}},
]}

webhook = {
    "parameters": {"httpMethod": "POST", "path": "cleanup-orders-headers", "responseMode": "lastNode", "options": {}},
    "id": str(uuid.uuid4()),
    "name": "Trigger",
    "type": "n8n-nodes-base.webhook",
    "typeVersion": 2,
    "position": [0, 0],
    "webhookId": str(uuid.uuid4()),
}

clean = {
    "parameters": {
        "method": "POST",
        "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "googleSheetsOAuth2Api",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": json.dumps(cleanup_body),
        "options": {},
    },
    "id": str(uuid.uuid4()),
    "name": "Cleanup Sheet",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [240, 0],
    "credentials": {"googleSheetsOAuth2Api": GS_CRED},
}

nodes = [webhook, clean]
connections = {webhook["name"]: {"main": [[{"node": clean["name"], "type": "main", "index": 0}]]}}

status, body = http("POST", "/workflows", {
    "name": "TEMP Cleanup Orders DB",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
})
wf_id = json.loads(body)["id"]
http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
http("POST", f"/workflows/{wf_id}/activate")
print(f"Created cleanup WF {wf_id}, activated.")
time.sleep(1)

req = urllib.request.Request("https://thebonpet.app.n8n.cloud/webhook/cleanup-orders-headers",
                              data=b'{}', method="POST",
                              headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"Fired → HTTP {r.status}: {r.read().decode()[:200]}")
except urllib.error.HTTPError as e:
    print(f"Fired → HTTP {e.code}: {e.read().decode()[:300]}")

time.sleep(2)
status, body = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
ex = json.loads(body)["data"][0]
print(f"Execution {ex['id']}: {ex['status']}")

http("DELETE", f"/workflows/{wf_id}")
print("Cleanup workflow deleted.")

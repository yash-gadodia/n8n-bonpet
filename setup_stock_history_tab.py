#!/usr/bin/env python3
"""One-off: create `stock_history` tab in the Stock Update sheet + seed headers.
Prints the GID so caller can confirm. Mirrors setup_winback_sent_tab.py pattern.
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
DOC_ID = "1yYzRL5pkpmoPflL_vzOUeI_eimaOTMH7gfv_INlplx8"
TAB_NAME = "stock_history"
HEADERS = ["date", "product", "balance", "total_in", "total_out"]
WEBHOOK_PATH = "setup-stock-history-tab-5f8a1c3d"


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
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "lastNode", "options": {}},
        "id": str(uuid.uuid4()), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }


def add_sheet_node():
    body = {"requests": [{"addSheet": {"properties": {"title": TAB_NAME}}}]}
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{DOC_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body),
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Add Sheet",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "continueOnFail": True,
    }


def write_headers_node():
    body = {"valueInputOption": "RAW",
            "data": [{"range": f"{TAB_NAME}!A1:E1", "values": [HEADERS]}]}
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{DOC_ID}/values:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body),
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Write Headers",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
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
    "name": "TEMP Setup Stock History Tab",
    "nodes": nodes, "connections": connections,
    "settings": {"executionOrder": "v1"},
})
print(f"Create → {status}")
wf_id = json.loads(body)["id"]

http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
http("POST", f"/workflows/{wf_id}/activate")
time.sleep(1)

url = f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_PATH}"
print(f"Firing: POST {url}")
try:
    req = urllib.request.Request(url, data=b'{}', method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"  → HTTP {r.status}: {r.read().decode()[:300]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode()[:300]}")

time.sleep(2)
status, body = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
data = json.loads(body)
gid = None
if data.get("data"):
    ex = data["data"][0]
    print(f"\nExecution {ex['id']}: status={ex['status']}")
    runs = ex.get("data", {}).get("resultData", {}).get("runData", {})
    for n, outs in runs.items():
        out = outs[0] if outs else {}
        if "error" in out:
            print(f"  ❌ {n}: {out['error'].get('message', out['error'])[:200]}")
        else:
            print(f"  ✅ {n}")
        if n == "Add Sheet":
            try:
                j = out["data"]["main"][0][0]["json"]
                replies = j.get("replies", [])
                if replies and "addSheet" in replies[0]:
                    gid = replies[0]["addSheet"]["properties"]["sheetId"]
            except Exception:
                pass

print(f"\nDeleting TEMP workflow {wf_id}")
http("DELETE", f"/workflows/{wf_id}")
print()
if gid is not None:
    print(f"✅ stock_history GID = {gid}")
    print(f"   Paste into build_stock_history_logger.py + build_weekly_stock_report.py:")
    print(f"   STOCK_HISTORY_TAB_GID = {gid}")
else:
    print("⚠️  Tab may already exist. Open the sheet and read the gid from its URL.")

#!/usr/bin/env python3
"""One-off: create `post_trial_sent` tab in Customer Orders DB with fixed GID 900900.
Mirrors setup_reorder_sent_tab.py pattern. Run once before (re-)deploying build_post_trial_nurture.py.

Dedup key in the workflow: (phone, step_num) combo. Ensures each trial buyer gets
each of D7/D14/D21 at most once, ever, regardless of workflow re-fires.
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
TAB_GID = 900900
TAB_NAME = "post_trial_sent"
HEADERS = ["phone", "step_num", "sent_at", "trial_order_id", "first_name", "days_since"]


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


def setup_tab():
    body = {"requests": [
        {"addSheet": {"properties": {"sheetId": TAB_GID, "title": TAB_NAME}}},
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h}} for h in HEADERS]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": TAB_GID, "rowIndex": 0, "columnIndex": 0},
        }},
    ]}
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-add-post-trial-sent-tab",
                        "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger",
         "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": json.dumps(body), "options": {},
         }, "id": str(uuid.uuid4()), "name": "Add Tab",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
         "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED}, "continueOnFail": True},
    ]
    conn = {"Trigger": {"main": [[{"node": "Add Tab", "type": "main", "index": 0}]]}}

    s, b = http("POST", "/workflows", {"name": "TEMP Setup Post Trial Sent Tab",
                                        "nodes": nodes, "connections": conn,
                                        "settings": {"executionOrder": "v1"}})
    print(f"Create → {s}")
    wf_id = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://thebonpet.app.n8n.cloud/webhook/tmp-add-post-trial-sent-tab",
            data=b'{}', method="POST",
            headers={"Content-Type": "application/json"}), timeout=30)
        print("  Setup fired")
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:200]
        print("  " + ("Already exists ✓" if "already exists" in msg.lower() else f"HTTP {e.code}: {msg}"))
    time.sleep(1)
    http("DELETE", f"/workflows/{wf_id}")
    print(f"✅ Tab `{TAB_NAME}` GID={TAB_GID}")


if __name__ == "__main__":
    setup_tab()

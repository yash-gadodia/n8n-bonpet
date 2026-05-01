#!/usr/bin/env python3
"""One-off: create the `review_log` tab in the Bon Pet customer sheet via n8n's
Google Sheets OAuth credential, then report the gid so we can activate the
Negative Review Watcher.
"""
import json, uuid, os, time, urllib.request, urllib.error

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Create review_log Tab (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "create-review-log-tab-one-off"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: return e.code, json.loads(body)
        except Exception: return e.code, body


def find_existing():
    s, d = http("GET", "/workflows?limit=250")
    for w in d.get("data", []) if s < 300 else []:
        if w.get("name") == WF_NAME: return w["id"]
    return None


def build():
    trigger = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_ID, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_ID,
    }

    # Hit Google Sheets batchUpdate API directly via HTTP node, using the Google Sheets OAuth cred
    create_sheet = {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({
                "requests": [{
                    "addSheet": {
                        "properties": {"title": "review_log"}
                    }
                }]
            }),
            "options": {},
        },
        "id": uid(), "name": "Create review_log Tab",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    return {
        "name": WF_NAME,
        "nodes": [trigger, create_sheet],
        "connections": {
            trigger["name"]: {"main": [[{"node": create_sheet["name"], "type": "main", "index": 0}]]}
        },
        "settings": {"executionOrder": "v1"},
    }


def main():
    payload = build()
    existing = find_existing()
    if existing:
        # Delete if archived to avoid PUT failures
        http("DELETE", f"/workflows/{existing}")
        existing = None

    s, body = http("POST", "/workflows", payload)
    wf_id = body.get("id") if isinstance(body, dict) else None
    print(f"Workflow: {wf_id} (HTTP {s})")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    req = urllib.request.Request(
        f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Fire: {r.status}")
    except urllib.error.HTTPError as e:
        print(f"Fire: HTTP {e.code} {e.read().decode()[:200]}")

    time.sleep(8)

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if not ex:
        print("No execution captured"); return
    print(f"Execution {ex['id']} | status={ex.get('status')}")
    rd = (ex.get("data") or {}).get("resultData", {}).get("runData", {})
    for n, runs in rd.items():
        for r in runs:
            err = r.get("error")
            if err:
                print(f"  ❌ {n}: {str(err.get('message'))[:300]}")
            else:
                items = (r.get("data") or {}).get("main", [[]])[0] or []
                print(f"  ✅ {n}: {len(items)} items")
                # Show the created sheet's gid
                if n == "Create review_log Tab":
                    for it in items:
                        replies = (it.get("json") or {}).get("replies") or []
                        for rep in replies:
                            added = rep.get("addSheet") or {}
                            props = added.get("properties") or {}
                            print(f"     → title={props.get('title')}  gid={props.get('sheetId')}")

    http("POST", f"/workflows/{wf_id}/deactivate")
    print("Helper deactivated.")


if __name__ == "__main__":
    main()

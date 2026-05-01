#!/usr/bin/env python3
"""Peek at any tab of the Bon Pet customer sheet via an n8n throwaway workflow.
Usage:  python3 peek_customer_sheet.py [GID]   (default: 0)
"""
import json, uuid, os, sys, time, urllib.request, urllib.error

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Peek Customer Sheet (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "peek-customer-sheet-4e9a1c8d2f"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
SHEET_GID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
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


def build():
    trigger = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_ID, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_ID,
    }

    read_sheet = {
        "parameters": {
            "documentId": {
                "__rl": True, "value": SHEET_ID, "mode": "list",
                "cachedResultName": "Customer Orders",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            },
            "sheetName": {
                "__rl": True, "value": SHEET_GID, "mode": "list",
                "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={SHEET_GID}",
            },
            "options": {},
        },
        "id": uid(), "name": "Read Sheet",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [240, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    return {
        "name": WF_NAME,
        "nodes": [trigger, read_sheet],
        "connections": {
            trigger["name"]: {"main": [[{"node": read_sheet["name"], "type": "main", "index": 0}]]}
        },
        "settings": {"executionOrder": "v1"},
    }


def find_existing():
    s, d = http("GET", "/workflows?limit=250")
    for w in d.get("data", []) if s < 300 else []:
        if w.get("name") == WF_NAME: return w["id"]
    return None


def main():
    payload = build()
    existing = find_existing()
    if existing:
        s, body = http("PUT", f"/workflows/{existing}", payload); wf_id = existing
    else:
        s, body = http("POST", "/workflows", payload)
        wf_id = body.get("id") if isinstance(body, dict) else None
    print(f"Workflow: {wf_id} (HTTP {s})  reading gid={SHEET_GID}")

    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    req = urllib.request.Request(
        f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        print(f"Fire: {r.status}")

    time.sleep(8)

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if not ex:
        print("No execution"); return
    print(f"Execution {ex['id']} | status={ex.get('status')}")
    rd = (ex.get("data") or {}).get("resultData", {}).get("runData", {})

    sheet_runs = rd.get("Read Sheet", [])
    if sheet_runs:
        items = (sheet_runs[0].get("data") or {}).get("main", [[]])[0] or []
        print(f"\nRow count: {len(items)}")
        if items:
            print(f"\nColumns (from row 1): {sorted(items[0].get('json', {}).keys())}")
            print(f"\nFirst 3 rows:")
            for i, it in enumerate(items[:3]):
                print(f"  row {i+1}: {json.dumps(it.get('json', {}), ensure_ascii=False)[:400]}")

    for n, runs in rd.items():
        for r in runs:
            err = r.get("error")
            if err: print(f"  ❌ {n}: {str(err.get('message'))[:200]}")

    http("POST", f"/workflows/{wf_id}/deactivate")


if __name__ == "__main__":
    main()

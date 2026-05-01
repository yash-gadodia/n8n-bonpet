#!/usr/bin/env python3
"""One-off: retry the Review Watcher for Katarina Tan and Joys Tan (who matched
but missed the thanks WA due to the iteration bug).

Strategy:
  1. Clear the review_log tab entirely.
  2. Seed it with Emily Maria's already-thanked entry, so the customer_id dedup
     in Decide Action suppresses her on the re-run.
  3. Fire the Review Watcher webhook — Katarina + Joys get thanked fresh.
"""
import json
import uuid
import os
import time
import urllib.request
import urllib.error

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Retry Review Thanks (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "retry-review-thanks-one-off"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REVIEW_LOG_TAB_GID = 709923135
GS_CRED_ID = "sxbz0Cu8yhdi0RdN"
GS_CRED_NAME = "Google Sheets account"

REVIEW_WATCHER_WEBHOOK = "https://thebonpet.app.n8n.cloud/webhook/review-watcher-manual-8e3c7a1f4d"

# Emily's identity — to seed so she isn't re-thanked
EMILY = {
    "review_id": "",  # unknown precise — we'll fetch to find it
    "author_name": "Emily Maria",
    "rating": 5,
    "action": "customer_thanks",
    "matched_customer_id": "9228598640697",
    "logged_at": "",
}


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


def build():
    trigger = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_ID, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_ID,
    }

    # 1. Read current review_log so we can preserve Emily's review_id
    read_log = {
        "parameters": {**sheet_ref(REVIEW_LOG_TAB_GID), "options": {}},
        "id": uid(), "name": "Read Current Log",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [240, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    # 2. Pick out Emily's row and prepare payload for re-seed
    prep = {
        "parameters": {
            "jsCode": r"""
const rows = $input.all().map(it => it.json);
const emily = rows.find(r => String(r.author_name || '') === 'Emily Maria');
return [{ json: {
  emily_review_id: emily ? String(emily.review_id || '') : '',
  emily_logged_at: emily ? String(emily.logged_at || new Date().toISOString()) : new Date().toISOString(),
  total_rows_before: rows.length,
} }];
"""
        },
        "id": uid(), "name": "Find Emily",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [480, 300],
    }

    # 3. Clear the sheet via batchUpdate (removes all rows except header)
    #    Actually easier: use valueInputOption with clear via spreadsheets.values:clear
    clear = {
        "parameters": {
            "method": "POST",
            "url": f"=https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/review_log!A2:Z10000:clear",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "{}",
            "options": {},
        },
        "id": uid(), "name": "Clear Log Rows",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [720, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    # 4. Build Emily's seed row as an item (Code node), then append via autoMap
    build_seed = {
        "parameters": {
            "jsCode": r"""
const emily = $('Find Emily').first().json;
return [{ json: {
  review_id: emily.emily_review_id || '',
  author_name: 'Emily Maria',
  rating: 5,
  action: 'customer_thanks',
  matched_customer_id: '9228598640697',
  logged_at: emily.emily_logged_at || new Date().toISOString(),
}}];
"""
        },
        "id": uid(), "name": "Build Seed Row",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [960, 300],
    }

    seed = {
        "parameters": {
            "operation": "append",
            **sheet_ref(REVIEW_LOG_TAB_GID),
            "columns": {"mappingMode": "autoMapInputData", "matchingColumns": []},
            "options": {},
        },
        "id": uid(), "name": "Seed Emily Row",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": [1200, 300],
        "credentials": {"googleSheetsOAuth2Api": {"id": GS_CRED_ID, "name": GS_CRED_NAME}},
    }

    # 5. Fire the Review Watcher webhook
    fire_watcher = {
        "parameters": {
            "method": "POST",
            "url": REVIEW_WATCHER_WEBHOOK,
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "{}",
            "options": {},
        },
        "id": uid(), "name": "Fire Review Watcher",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1440, 300],
    }

    nodes = [trigger, read_log, prep, clear, build_seed, seed, fire_watcher]
    connections = {
        trigger["name"]:       {"main": [[{"node": read_log["name"], "type": "main", "index": 0}]]},
        read_log["name"]:      {"main": [[{"node": prep["name"], "type": "main", "index": 0}]]},
        prep["name"]:          {"main": [[{"node": clear["name"], "type": "main", "index": 0}]]},
        clear["name"]:         {"main": [[{"node": build_seed["name"], "type": "main", "index": 0}]]},
        build_seed["name"]:    {"main": [[{"node": seed["name"], "type": "main", "index": 0}]]},
        seed["name"]:          {"main": [[{"node": fire_watcher["name"], "type": "main", "index": 0}]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
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
        # Kill stale to avoid "archived" PUT issues
        http("DELETE", f"/workflows/{existing}")
    s, body = http("POST", "/workflows", payload)
    wf_id = body.get("id") if isinstance(body, dict) else None
    print(f"Workflow: {wf_id} (HTTP {s})")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    req = urllib.request.Request(
        f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"Fire: {r.status}")

    time.sleep(18)  # clear + seed + fire watcher + watcher execution

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if ex:
        print(f"Retry helper execution {ex['id']} | {ex.get('status')}")
        rd = (ex.get("data") or {}).get("resultData", {}).get("runData", {})
        for n, runs in rd.items():
            for r in runs:
                err = r.get("error")
                if err: print(f"  ❌ {n}: {str(err.get('message'))[:200]}")
                else:
                    items = (r.get("data") or {}).get("main", [[]])[0] or []
                    print(f"  ✅ {n}: {len(items)} items")

    # Now also pull the latest Review Watcher execution to show what re-ran
    s, data = http("GET", f"/executions?workflowId=e9M54bpyzHPPRcDr&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if ex:
        print(f"\nReview Watcher retry execution {ex['id']} | {ex.get('status')}")
        rd = (ex.get("data") or {}).get("resultData", {}).get("runData", {})
        decide = rd.get("Decide Action", [])
        if decide:
            items = (decide[0].get("data") or {}).get("main", [[]])[0] or []
            for it in items:
                j = it["json"]
                print(f"  - {j.get('rating')}★ by {j.get('author_name')!r} → {j.get('action')}  reason={j.get('skip_reason', '')}")
        cust_send = rd.get("Send Customer Thanks", [])
        if cust_send:
            items = (cust_send[0].get("data") or {}).get("main", [[]])[0] or []
            print(f"\nSent customer-thanks WAs: {len(items)}")

    http("POST", f"/workflows/{wf_id}/deactivate")


if __name__ == "__main__":
    main()

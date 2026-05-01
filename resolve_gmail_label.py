#!/usr/bin/env python3
"""One-shot: create n8n workflow that lists all Gmail labels via the existing OAuth cred,
trigger it, read the result, delete the workflow.
"""
import json, os, time, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
GMAIL_CRED_ID = "FD8gO3Ky14wEtczl"
GMAIL_CRED_NAME = "Gmail account"
TARGET_LABEL_ID = "Label_4108250148954274741"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def req(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            raw = res.read().decode()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


workflow = {
    "name": "_TEMP list gmail labels",
    "nodes": [
        {
            "parameters": {},
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Manual Trigger",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": [0, 0],
        },
        {
            "parameters": {
                "resource": "label",
                "operation": "getAll",
                "returnAll": True,
            },
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "List Labels",
            "type": "n8n-nodes-base.gmail",
            "typeVersion": 2.1,
            "position": [240, 0],
            "credentials": {
                "gmailOAuth2": {"id": GMAIL_CRED_ID, "name": GMAIL_CRED_NAME}
            },
        },
        {
            "parameters": {
                "jsCode": "const all = $input.all().map(it => it.json); return [{ json: { labels: all, count: all.length } }];"
            },
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "Collect",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [480, 0],
        },
    ],
    "connections": {
        "Manual Trigger": {"main": [[{"node": "List Labels", "type": "main", "index": 0}]]},
        "List Labels":    {"main": [[{"node": "Collect",     "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1"},
}

# 1. Create workflow
s, body = req("POST", "/workflows", workflow)
print(f"create: HTTP {s}")
if s >= 300:
    print(body); raise SystemExit(1)
wf_id = body["id"]
print(f"workflow id: {wf_id}")

# 2. Trigger it via /workflows/<id>/run? Public API doesn't have manual run.
#    Instead, change to a webhook trigger so we can POST to it.
#    Cleaner: just use /executions/run is not in public API.
#    Workaround: switch trigger to a webhook, activate, POST it.
workflow["nodes"][0] = {
    "parameters": {
        "httpMethod": "POST",
        "path": "tmp-list-labels-7f3a9b",
        "responseMode": "lastNode",
        "options": {},
    },
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Trigger",
    "type": "n8n-nodes-base.webhook",
    "typeVersion": 2,
    "position": [0, 0],
    "webhookId": "tmp-list-labels-7f3a9b",
}
workflow["connections"] = {
    "Trigger":     {"main": [[{"node": "List Labels", "type": "main", "index": 0}]]},
    "List Labels": {"main": [[{"node": "Collect",     "type": "main", "index": 0}]]},
}

s, _ = req("PUT", f"/workflows/{wf_id}", workflow)
print(f"update: HTTP {s}")
s, _ = req("POST", f"/workflows/{wf_id}/activate")
print(f"activate: HTTP {s}")

# 3. POST to the webhook with responseMode=lastNode, n8n returns the result inline
print("triggering...")
trig = urllib.request.Request(
    "https://n8n.thebonpet.com/webhook/tmp-list-labels-7f3a9b",
    data=b'{}', method="POST",
    headers={"Content-Type": "application/json", "User-Agent": UA},
)
try:
    with urllib.request.urlopen(trig, timeout=30) as r:
        raw = r.read().decode()
        print(f"raw response (first 500): {raw[:500]}")
        data = json.loads(raw)
except urllib.error.HTTPError as e:
    print("trigger failed:", e.code, e.read().decode()[:500])
    raise SystemExit(1)

# 4. Find target label
labels = data.get("labels", []) if isinstance(data, dict) else []
print(f"\nFound {len(labels)} labels")
target = next((l for l in labels if l.get("id") == TARGET_LABEL_ID), None)
if target:
    print(f"\n*** {TARGET_LABEL_ID} = {target.get('name')!r} ***")
    print(f"    type: {target.get('type')}, msgsTotal: {target.get('messagesTotal')}, "
          f"msgsUnread: {target.get('messagesUnread')}")
else:
    print(f"\n!! {TARGET_LABEL_ID} NOT FOUND in account")
    print("\nAll user-defined labels in this Gmail account:")
    for l in sorted([x for x in labels if x.get("type") == "user"], key=lambda x: x.get("name","")):
        print(f"  {l.get('id'):<35} {l.get('name')!r}")

# 5. Cleanup: deactivate + delete the temp workflow
req("POST", f"/workflows/{wf_id}/deactivate")
req("DELETE", f"/workflows/{wf_id}")
print("\ncleanup: temp workflow deleted")

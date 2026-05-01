#!/usr/bin/env python3
"""Add a webhook trigger to the Reorder Reminder workflow, activate it, fire it, report.
Webhook stays in place after — safe (only fires when called)."""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_ID = "AMd0mktMWn73UCbZ"
WEBHOOK_PATH = "trigger-reorder-now"


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


# Step 1: GET current workflow
status, body = http("GET", f"/workflows/{WF_ID}")
wf = json.loads(body)
print(f"Got workflow ({status}): {len(wf['nodes'])} nodes, active={wf['active']}")

# Step 2: Add webhook trigger node if not present
if not any(n["type"] == "n8n-nodes-base.webhook" for n in wf["nodes"]):
    webhook_node = {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_PATH,
            "responseMode": "onReceived",
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Manual Trigger Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 100],
        "webhookId": str(uuid.uuid4()),
    }
    wf["nodes"].append(webhook_node)
    wf["connections"].setdefault("Manual Trigger Webhook", {"main": [[{"node": "Get Shopify Orders (90d)", "type": "main", "index": 0}]]})
    print("Added webhook trigger node")
else:
    print("Webhook trigger already present")

# Step 3: PUT workflow with webhook
payload = {
    "name": wf["name"],
    "nodes": wf["nodes"],
    "connections": wf["connections"],
    "settings": {"executionOrder": "v1"},
}
status, body = http("PUT", f"/workflows/{WF_ID}", payload)
print(f"PUT workflow → {status}")

# Step 4: Activate (required for webhooks to register)
status, body = http("POST", f"/workflows/{WF_ID}/activate")
print(f"Activate → {status}: {body[:120]}")

# Step 5: Hit the webhook
time.sleep(1)
url = f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_PATH}"
print(f"\nFiring webhook: POST {url}")
req = urllib.request.Request(url, data=b'{}', method="POST",
                              headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f"  response: HTTP {r.status} — {r.read().decode()[:200]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode()[:200]}")

# Step 6: Check executions to see if it ran
time.sleep(3)
status, body = http("GET", f"/executions?workflowId={WF_ID}&limit=2")
print(f"\nRecent executions: {body[:600]}")

#!/usr/bin/env python3
"""Register Shopify webhooks programmatically by routing an Admin API call through n8n
(the Shopify access token lives in the n8n credential, not in this script).

Each call: POST /admin/api/2024-10/webhooks.json with {webhook: {topic, address, format}}.
Idempotent-ish: lists existing webhooks first and skips if the {topic, address} pair exists.
"""
import json
import uuid
import os
import time
import urllib.request
import urllib.error
import sys

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Shopify Webhook Manager (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "shopify-webhook-manager-9f4e2c8a1d"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

# Webhooks to ensure exist. Add entries as more transactional workflows come online.
WEBHOOKS_TO_REGISTER = [
    {
        "topic":   "orders/paid",
        "address": "https://thebonpet.app.n8n.cloud/webhook/big-order-alert-5f8c3d2a1b",
        "format":  "json",
    },
    {
        "topic":   "refunds/create",
        "address": "https://thebonpet.app.n8n.cloud/webhook/refund-alert-7a2c8b4f1e",
        "format":  "json",
    },
    {
        "topic":   "orders/cancelled",
        "address": "https://thebonpet.app.n8n.cloud/webhook/cancel-alert-3d9f6a2c8b",
        "format":  "json",
    },
    {
        "topic":   "checkouts/create",
        "address": "https://thebonpet.app.n8n.cloud/webhook/abandoned-cart-recovery-2b7f4c9e8a",
        "format":  "json",
    },
    {
        "topic":   "subscription_contracts/update",
        "address": "https://thebonpet.app.n8n.cloud/webhook/subscription-save-8c4f2e9a3b",
        "format":  "json",
    },
]


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def build_workflow():
    """Workflow: webhook → Code emits N webhook configs → HTTP fires N times to Shopify → Code collates."""
    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}"

    trigger = {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_ID,
            "responseMode": "onReceived",
            "options": {},
        },
        "id": uid(),
        "name": "Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 300],
        "webhookId": WEBHOOK_ID,
    }

    list_existing = {
        "parameters": {
            "url": f"{base}/webhooks.json?limit=250",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(),
        "name": "List Existing Webhooks",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 300],
        "credentials": {
            "shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}
        },
    }

    # Code node emits one item per webhook to register (skipping those already present)
    emit_configs = {
        "parameters": {
            "jsCode": "const existing = ($input.first().json.webhooks || []).map(w => ({topic: w.topic, address: w.address}));\n"
                      f"const desired = {json.dumps(WEBHOOKS_TO_REGISTER)};\n"
                      "const needed = desired.filter(d => !existing.some(e => e.topic === d.topic && e.address === d.address));\n"
                      "const skipped = desired.filter(d =>  existing.some(e => e.topic === d.topic && e.address === d.address));\n"
                      "console.log('existing count:', existing.length, 'needed:', needed.length, 'skipped:', skipped.length);\n"
                      "return needed.map(w => ({ json: { webhook: w, _skipped: false } })).concat(skipped.map(w => ({ json: { webhook: w, _skipped: true } })));\n"
        },
        "id": uid(),
        "name": "Diff Desired vs Existing",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [480, 300],
    }

    # IF node: only register if not skipped
    filter_new = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json._skipped }}",
                    "rightValue": False,
                    "operator": {"type": "boolean", "operation": "false", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(),
        "name": "Needs Registering?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.3,
        "position": [720, 300],
    }

    register = {
        "parameters": {
            "method": "POST",
            "url": f"{base}/webhooks.json",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ webhook: $json.webhook }) }}",
            "options": {},
        },
        "id": uid(),
        "name": "Register Webhook",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [960, 200],
        "credentials": {
            "shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}
        },
    }

    nodes = [trigger, list_existing, emit_configs, filter_new, register]
    connections = {
        trigger["name"]:        {"main": [[{"node": list_existing["name"], "type": "main", "index": 0}]]},
        list_existing["name"]:  {"main": [[{"node": emit_configs["name"], "type": "main", "index": 0}]]},
        emit_configs["name"]:   {"main": [[{"node": filter_new["name"], "type": "main", "index": 0}]]},
        filter_new["name"]:     {"main": [
            [{"node": register["name"], "type": "main", "index": 0}],  # true: needs registering
            [],  # false: already registered
        ]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def find_existing_wf():
    status, data = http("GET", "/workflows?limit=250")
    for wf in data.get("data", []) if status < 300 else []:
        if wf.get("name") == WF_NAME:
            return wf["id"]
    return None


def main():
    payload = build_workflow()
    existing = find_existing_wf()
    if existing:
        status, body = http("PUT", f"/workflows/{existing}", payload)
        wf_id = existing
    else:
        status, body = http("POST", "/workflows", payload)
        wf_id = body.get("id") if isinstance(body, dict) else None
    print(f"Workflow: {wf_id} (HTTP {status})")

    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    # Fire
    req = urllib.request.Request(
        f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        print(f"Fired webhook: HTTP {r.status}")

    time.sleep(12)

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if not ex:
        print("No execution captured"); sys.exit(1)
    print(f"Execution {ex['id']} | status={ex.get('status')}")

    rd = (ex.get("data") or {}).get("resultData", {}).get("runData", {})
    for n, runs in rd.items():
        for r in runs:
            err = r.get("error")
            if err:
                print(f"  ❌ {n}: {str(err.get('message'))[:200]}")
            else:
                items = (r.get("data") or {}).get("main", [[]])[0] or []
                print(f"  ✅ {n}: {len(items)} items")

    reg_runs = rd.get("Register Webhook", [])
    for r in reg_runs:
        items = (r.get("data") or {}).get("main", [[]])[0] or []
        for it in items:
            w = it.get("json", {}).get("webhook", {})
            print(f"    registered: topic={w.get('topic')} address={w.get('address')}")

    # Deactivate helper workflow
    http("POST", f"/workflows/{wf_id}/deactivate")
    print("Helper workflow deactivated.")


if __name__ == "__main__":
    main()

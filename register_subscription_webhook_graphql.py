#!/usr/bin/env python3
"""One-off: register the subscription_contracts/update webhook via GraphQL.

REST /webhooks.json rejects SUBSCRIPTION_CONTRACTS_UPDATE (422 invalid topic),
so we use the GraphQL webhookSubscriptionCreate mutation instead. Routed
through a temporary n8n workflow so the Shopify token stays in n8n's credential.
"""
import json
import uuid
import os
import time
import urllib.request
import urllib.error
import sys

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Subscription Webhook Registrar (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "subscription-webhook-registrar-ea2f91"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "4d1xmXLJqGoPK6TX"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

CALLBACK_URL = "https://n8n.thebonpet.com/webhook/subscription-save-8c4f2e9a3b"

GRAPHQL_MUTATION = """mutation {
  webhookSubscriptionCreate(
    topic: SUBSCRIPTION_CONTRACTS_UPDATE
    webhookSubscription: {
      callbackUrl: "%s"
      format: JSON
    }
  ) {
    webhookSubscription {
      id
      callbackUrl
      topic
      format
    }
    userErrors { field message }
  }
}""" % CALLBACK_URL


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: return e.code, json.loads(body)
        except Exception: return e.code, body


def build_workflow():
    graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}/graphql.json"

    trigger = {
        "parameters": {
            "httpMethod": "POST", "path": WEBHOOK_ID,
            "responseMode": "onReceived", "options": {},
        },
        "id": uid(), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_ID,
    }

    list_existing = {
        "parameters": {
            "method": "POST",
            "url": graphql_url,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({
                "query": """{
                    webhookSubscriptions(first: 50) {
                        edges { node { id topic callbackUrl } }
                    }
                }"""
            }),
            "options": {},
        },
        "id": uid(), "name": "List Existing",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 300],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    check_existing = {
        "parameters": {
            "jsCode":
                "const edges = (($input.first().json.data || {}).webhookSubscriptions || {}).edges || [];\n"
                "const match = edges.find(e => e.node.topic === 'SUBSCRIPTION_CONTRACTS_UPDATE' "
                f"&& e.node.callbackUrl === '{CALLBACK_URL}');\n"
                "return [{ json: { already_exists: !!match, match } }];\n"
        },
        "id": uid(), "name": "Check Existing",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [480, 300],
    }

    if_new = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json.already_exists }}",
                    "rightValue": False,
                    "operator": {"type": "boolean", "operation": "false", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Needs Registering?",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [720, 300],
    }

    register = {
        "parameters": {
            "method": "POST",
            "url": graphql_url,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({"query": GRAPHQL_MUTATION}),
            "options": {},
        },
        "id": uid(), "name": "Register (GraphQL)",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [960, 200],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    nodes = [trigger, list_existing, check_existing, if_new, register]
    connections = {
        trigger["name"]:        {"main": [[{"node": list_existing["name"], "type": "main", "index": 0}]]},
        list_existing["name"]:  {"main": [[{"node": check_existing["name"], "type": "main", "index": 0}]]},
        check_existing["name"]: {"main": [[{"node": if_new["name"], "type": "main", "index": 0}]]},
        if_new["name"]: {"main": [
            [{"node": register["name"], "type": "main", "index": 0}],  # true = needs registering
            [],  # false = already registered
        ]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def find_existing_wf():
    s, data = http("GET", "/workflows?limit=250")
    for wf in (data or {}).get("data", []) if s < 300 else []:
        if wf.get("name") == WF_NAME:
            return wf["id"]
    return None


def main():
    payload = build_workflow()
    ex_id = find_existing_wf()
    if ex_id:
        s, b = http("PUT", f"/workflows/{ex_id}", payload)
        wf_id = ex_id
    else:
        s, b = http("POST", "/workflows", payload)
        wf_id = b.get("id") if isinstance(b, dict) else None
    print(f"Workflow: {wf_id} (HTTP {s})")

    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    req = urllib.request.Request(
        f"https://n8n.thebonpet.com/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        print(f"Fired: HTTP {r.status}")

    time.sleep(10)

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data and data.get("data") else None
    if not ex:
        print("No execution captured"); sys.exit(1)
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
                if n == "Register (GraphQL)":
                    for it in items:
                        body = it.get("json", {})
                        res = (body.get("data") or {}).get("webhookSubscriptionCreate") or {}
                        ws = res.get("webhookSubscription")
                        errs = res.get("userErrors") or []
                        if ws:
                            print(f"     ✅ Registered: id={ws.get('id')}  topic={ws.get('topic')}  url={ws.get('callbackUrl')}")
                        for e in errs:
                            print(f"     ⚠️ userError: {e.get('field')}: {e.get('message')}")
                        if body.get("errors"):
                            print(f"     ⚠️ GraphQL errors: {body.get('errors')}")
                elif n == "Check Existing":
                    for it in items:
                        j = it.get("json", {})
                        if j.get("already_exists"):
                            print(f"     (subscription already registered — skipping)")
                elif n == "List Existing":
                    for it in items:
                        edges = ((it.get("json", {}).get("data") or {}).get("webhookSubscriptions") or {}).get("edges") or []
                        print(f"     Store has {len(edges)} existing webhook subscriptions")

    http("POST", f"/workflows/{wf_id}/deactivate")
    print("Helper workflow deactivated.")


if __name__ == "__main__":
    main()

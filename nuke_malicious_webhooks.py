#!/usr/bin/env python3
"""URGENT: list all Shopify webhooks, identify malicious ones (address containing
'you.ve.been.p4wnd' or any non-thebonpet domain we didn't register), and DELETE them.
Routes through n8n's Shopify credential since we don't have the token locally.
"""
import json, uuid, os, time, urllib.request, urllib.error

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Nuke Malicious Webhooks (one-off)"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "nuke-malicious-webhooks-one-off"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

# Legitimate address prefix — anything NOT starting with this is suspect
LEGIT_PREFIX = "https://thebonpet.app.n8n.cloud/webhook/"


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
    url = f"{API}{path}"
    req = urllib.request.Request(url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def build():
    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}"
    trigger = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_ID, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_ID,
    }

    list_hooks = {
        "parameters": {
            "url": f"{base}/webhooks.json?limit=250",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(), "name": "List Webhooks",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 300],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    # Emit one item per suspicious webhook
    identify = {
        "parameters": {
            "jsCode": f"""
const resp = $input.first().json;
const hooks = resp.webhooks || [];
const suspicious = hooks.filter(h => !String(h.address || '').startsWith({json.dumps(LEGIT_PREFIX)}));
return suspicious.map(h => ({{ json: {{
  id: h.id,
  topic: h.topic,
  address: h.address,
  created_at: h.created_at,
}} }}));
"""
        },
        "id": uid(), "name": "Identify Suspicious",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [480, 300],
    }

    # Delete each — HTTP node fires once per input item
    delete_node = {
        "parameters": {
            "method": "DELETE",
            "url": "=" + base + "/webhooks/{{ $json.id }}.json",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(), "name": "Delete Webhook",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [720, 300],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    return {
        "name": WF_NAME,
        "nodes": [trigger, list_hooks, identify, delete_node],
        "connections": {
            trigger["name"]:    {"main": [[{"node": list_hooks["name"], "type": "main", "index": 0}]]},
            list_hooks["name"]: {"main": [[{"node": identify["name"], "type": "main", "index": 0}]]},
            identify["name"]:   {"main": [[{"node": delete_node["name"], "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"},
    }


def main():
    payload = build()
    # Delete any prior helper
    s, data = http("GET", "/workflows?limit=250")
    for w in data.get("data", []):
        if w.get("name") == WF_NAME:
            http("DELETE", f"/workflows/{w['id']}")

    s, body = http("POST", "/workflows", payload)
    wf_id = body.get("id") if isinstance(body, dict) else None
    print(f"Created helper {wf_id} (HTTP {s})")
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    http("POST", f"/workflows/{wf_id}/activate")

    req = urllib.request.Request(f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"Fire: {r.status}")

    time.sleep(10)

    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if ex:
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
                    if n in ("Identify Suspicious", "Delete Webhook"):
                        for it in items:
                            j = it.get("json", {})
                            if n == "Identify Suspicious":
                                print(f"     🚨 {j.get('topic')} -> {j.get('address')[:80]}  (id={j.get('id')}, created={j.get('created_at')})")
                            else:
                                print(f"     💀 deleted webhook id={j.get('id', '?')}")

    http("POST", f"/workflows/{wf_id}/deactivate")


if __name__ == "__main__":
    main()

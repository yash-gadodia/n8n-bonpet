#!/usr/bin/env python3
"""Search yash@ inbox for emails matching the phone+message format → identify the sender automation."""
import json, os, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
GMAIL_CRED_ID = "FD8gO3Ky14wEtczl"
GMAIL_CRED_NAME = "Gmail account"
UA = "Mozilla/5.0"

def req(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


workflow = {
    "name": "_TEMP search wa-pattern",
    "nodes": [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "tmp-find-wa-source-2k4j8h",
                "responseMode": "lastNode",
                "options": {},
            },
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Trigger",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 0],
            "webhookId": "tmp-find-wa-source-2k4j8h",
        },
        {
            "parameters": {
                "resource": "message",
                "operation": "getAll",
                "returnAll": False,
                "limit": 30,
                "simple": False,
                "filters": {"q": '"message:" "+65"'},
                "options": {"format": "metadata"},
            },
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Search",
            "type": "n8n-nodes-base.gmail",
            "typeVersion": 2.1,
            "position": [240, 0],
            "credentials": {
                "gmailOAuth2": {"id": GMAIL_CRED_ID, "name": GMAIL_CRED_NAME}
            },
        },
        {
            "parameters": {
                "jsCode": (
                    "const items = $input.all().map(it => it.json);\n"
                    "if (!items.length) return [{ json: { count: 0, messages: [], debug: 'no items' } }];\n"
                    "const sampleKeys = Object.keys(items[0] || {});\n"
                    "return [{ json: { count: items.length, sampleKeys, firstItem: items[0], allItems: items } }];"
                )
            },
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "Summarize",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [480, 0],
        },
    ],
    "connections": {
        "Trigger": {"main": [[{"node": "Search",    "type": "main", "index": 0}]]},
        "Search":  {"main": [[{"node": "Summarize", "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1"},
}

s, body = req("POST", "/workflows", workflow)
if s >= 300:
    print("create:", s, body); raise SystemExit(1)
wf_id = body["id"]
req("POST", f"/workflows/{wf_id}/activate")

trig = urllib.request.Request(
    "https://n8n.thebonpet.com/webhook/tmp-find-wa-source-2k4j8h",
    data=b'{}', method="POST",
    headers={"Content-Type": "application/json", "User-Agent": UA},
)
try:
    with urllib.request.urlopen(trig, timeout=60) as r:
        data = json.loads(r.read())
except urllib.error.HTTPError as e:
    print("trigger failed:", e.code, e.read().decode()[:500])
    req("POST", f"/workflows/{wf_id}/deactivate"); req("DELETE", f"/workflows/{wf_id}")
    raise SystemExit(1)

print(f"\nMatched messages: {data.get('count', 0)}")
print(f"Sample keys: {data.get('sampleKeys')}")
print()
print("First item (raw):")
print(json.dumps(data.get('firstItem'), indent=2)[:2500])

req("POST", f"/workflows/{wf_id}/deactivate")
req("DELETE", f"/workflows/{wf_id}")
print("cleaned up")

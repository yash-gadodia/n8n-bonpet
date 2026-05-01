#!/usr/bin/env python3
"""Quick: find out which Gmail account the n8n cred is currently signed into."""
import json, os, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
GMAIL_CRED_ID = "FD8gO3Ky14wEtczl"
GMAIL_CRED_NAME = "Gmail account"
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
    "name": "_TEMP whoami gmail",
    "nodes": [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "tmp-whoami-gmail-9c7f3b",
                "responseMode": "lastNode",
                "options": {},
            },
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Trigger",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 0],
            "webhookId": "tmp-whoami-gmail-9c7f3b",
        },
        {
            "parameters": {
                "method": "GET",
                "url": "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "gmailOAuth2",
                "options": {},
            },
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Profile",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [240, 0],
            "credentials": {
                "gmailOAuth2": {"id": GMAIL_CRED_ID, "name": GMAIL_CRED_NAME}
            },
        },
    ],
    "connections": {
        "Trigger": {"main": [[{"node": "Profile", "type": "main", "index": 0}]]}
    },
    "settings": {"executionOrder": "v1"},
}

s, body = req("POST", "/workflows", workflow)
if s >= 300:
    print("create failed:", s, body); raise SystemExit(1)
wf_id = body["id"]

s, _ = req("POST", f"/workflows/{wf_id}/activate")
print(f"activate: HTTP {s}")

trig = urllib.request.Request(
    "https://n8n.thebonpet.com/webhook/tmp-whoami-gmail-9c7f3b",
    data=b'{}', method="POST",
    headers={"Content-Type": "application/json", "User-Agent": UA},
)
try:
    with urllib.request.urlopen(trig, timeout=15) as r:
        raw = r.read().decode()
        print(f"profile: {raw}")
except urllib.error.HTTPError as e:
    print("trigger failed:", e.code, e.read().decode()[:500])

req("POST", f"/workflows/{wf_id}/deactivate")
req("DELETE", f"/workflows/{wf_id}")
print("cleaned up")

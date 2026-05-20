#!/usr/bin/env python3
"""Abandoned Cart Recovery (webhook side) — NO-OP receiver.

Pre-refactor (until 2026-05-15) this workflow had a 3-hour Wait node that
held every checkout in process memory and OOM-killed n8n self-hosted on
busy days. The recovery logic now lives in build_abandoned_cart_sweeper.py
(hourly cron, reads Checkouts tab directly).

This workflow is kept active only so the existing Shopify webhook URL
(/webhook/abandoned-cart-recovery-2b7f4c9e8a) keeps returning 200. The
Checkouts tab is populated by a separate Shopify Flow → Sheets ingest, so
this webhook does not need to do anything with the payload.
"""
import json
import uuid
import os
import urllib.request
import urllib.error

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Abandoned Cart Recovery - WhatsApp"
WEBHOOK_PATH = "abandoned-cart-recovery-2b7f4c9e8a"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"


def uid(): return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
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


def build():
    trigger = {
        "parameters": {
            "httpMethod": "POST", "path": WEBHOOK_PATH,
            "responseMode": "onReceived", "options": {"rawBody": False},
        },
        "id": uid(), "name": "Shopify Webhook (checkouts/create)",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": WEBHOOK_PATH,
    }

    return {
        "name": WF_NAME,
        "nodes": [trigger],
        "connections": {},
        "settings": {"executionOrder": "v1"},
    }


def find_existing():
    status, data = http("GET", "/workflows?limit=250")
    if status >= 300: return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME: return wf["id"]
    return None


if __name__ == "__main__":
    payload = build()
    out = os.path.expanduser("~/n8n-bonpet/abandoned_cart_payload.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes (no-op webhook) → {out}")

    existing_id = find_existing()
    if existing_id:
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        new_id = existing_id
        print(f"PUT existing {new_id} → HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        new_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {new_id} → HTTP {status}")

    if new_id and status < 300:
        s, _ = http("POST", f"/workflows/{new_id}/activate")
        print(f"Activate HTTP {s}")

    print()
    print(f"Webhook URL preserved: https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")
    print("Actual recovery logic now lives in build_abandoned_cart_sweeper.py")

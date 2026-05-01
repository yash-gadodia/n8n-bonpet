#!/usr/bin/env python3
"""Post-migration fix script. Run once to repair workflows after the n8n cloud→self-hosted migration.

Fixes:
1. Morning Briefing — Aggregate & Format `$('Set Date Ranges').item.json` → `.first().json`
2. Error Alerter — replace broken Gmail send with Telegram send to team thread
3. errorWorkflow wired up on every active workflow + Error Alerter activated
4. Shopify Admin webhooks registered for the 4 webhook-driven workflows
"""
import json
import os
import sys
import urllib.request
import urllib.error
import subprocess

API = "https://n8n.thebonpet.com/api/v1"
N8N_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()

ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"
TELEGRAM_BOT_TOKEN = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","telegram-weslee-bot","-w"]).decode().strip()
TELEGRAM_CHAT_ID = "-1002184573790"
TELEGRAM_THREAD_ID = "34253"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API_VER = "2025-01"
SELF_HOSTED_WEBHOOK_BASE = "https://n8n.thebonpet.com/webhook"

# Shopify webhook registrations: (topic, n8n webhook path)
SHOPIFY_WEBHOOKS = [
    ("orders/paid",                      "big-order-alert-5f8c3d2a1b"),
    ("refunds/create",                   "refund-alert-7a2c8b4f1e"),
    ("orders/cancelled",                 "cancel-alert-3d9f6a2c8b"),
    ("checkouts/create",                 "abandoned-cart-recovery-2b7f4c9e8a"),
    ("subscription_contracts/update",    "subscription-save-8c4f2e9a3b"),
]


def n8n(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": N8N_KEY,
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
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def workflow_put_payload(wf):
    """n8n PUT only accepts name, nodes, connections, settings, staticData."""
    out = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings") or {"executionOrder": "v1"},
    }
    if "staticData" in wf and wf["staticData"]:
        out["staticData"] = wf["staticData"]
    return out


# ─────────────────────────── Fix #1: Morning Briefing ───────────────────────
def fix_morning_briefing():
    print("\n=== Fix 1: Morning Briefing — `.item.json` → `.first().json` ===")
    s, wf = n8n("GET", "/workflows/nX75nRw6zAFN5I2h")
    if s >= 300:
        print(f"  GET failed: {s}"); return False
    patched = False
    for n in wf["nodes"]:
        if n.get("name") == "Aggregate & Format":
            old = n["parameters"]["jsCode"]
            new = old.replace("$('Set Date Ranges').item.json",
                              "$('Set Date Ranges').first().json")
            if new != old:
                n["parameters"]["jsCode"] = new
                patched = True
                print("  patched Aggregate & Format jsCode")
    if not patched:
        print("  no change needed (already patched?)"); return True
    s2, _ = n8n("PUT", "/workflows/nX75nRw6zAFN5I2h", workflow_put_payload(wf))
    print(f"  PUT → HTTP {s2}")
    return s2 < 300


# ────────────────────────── Fix #2: Error Alerter → Telegram ────────────────
def fix_error_alerter():
    print("\n=== Fix 2: Error Alerter — replace Gmail with Telegram (HTTP) ===")
    s, wf = n8n("GET", f"/workflows/{ERROR_ALERTER_ID}")
    if s >= 300:
        print(f"  GET failed: {s}"); return False

    # Replace Send Email Alert node with a Telegram HTTP request node
    new_nodes = []
    for n in wf["nodes"]:
        if n.get("name") == "Send Email Alert":
            telegram_node = {
                "parameters": {
                    "method": "POST",
                    "url": f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    "sendBody": True,
                    "bodyParameters": {"parameters": [
                        {"name": "chat_id", "value": TELEGRAM_CHAT_ID},
                        {"name": "message_thread_id", "value": TELEGRAM_THREAD_ID},
                        {"name": "text", "value": "={{ $json.message }}"},
                        {"name": "parse_mode", "value": "Markdown"},
                    ]},
                    "options": {},
                },
                "id": n["id"],
                "name": "Send Telegram Alert",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": n.get("position", [600, 300]),
            }
            new_nodes.append(telegram_node)
            print(f"  replaced 'Send Email Alert' with HTTP→Telegram node")
        else:
            new_nodes.append(n)
    wf["nodes"] = new_nodes

    # Update Format Alert code to produce a Telegram-formatted message
    for n in wf["nodes"]:
        if n.get("name") == "Format Alert":
            n["parameters"]["jsCode"] = r"""// Format error alert for Telegram
const err = $json;
const msg = `🚨 *n8n Workflow Error*
*Workflow:* ${err.workflow?.name || '(unknown)'}
*Execution:* ${err.execution?.id || '?'}
*Mode:* ${err.execution?.mode || '?'}
*When:* ${new Date().toISOString()}

*Error node:* ${err.execution?.error?.node?.name || '(top-level)'}
*Message:* ${(err.execution?.error?.message || 'no message').slice(0, 500)}

[View in n8n](https://n8n.thebonpet.com/workflow/${err.workflow?.id}/executions/${err.execution?.id})`;
return [{ json: { message: msg } }];
"""

    # n8n public API refuses to activate workflows that have ONLY an errorTrigger.
    # Add an unconnected webhook node (never called) to satisfy validation.
    wf["nodes"] = [n for n in wf["nodes"] if n.get("name") not in ("_dummy_manual", "_dummy_webhook")]
    wf["nodes"].append({
        "parameters": {
            "httpMethod": "POST",
            "path": "error-alerter-dummy-never-called",
            "responseMode": "onReceived",
            "options": {},
        },
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "_dummy_webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [-200, 600],
        "webhookId": "error-alerter-dummy-never-called",
    })

    # Rewire connections: Format Alert → Send Telegram Alert
    wf["connections"] = {
        "On Workflow Error": {"main": [[{"node": "Format Alert", "type": "main", "index": 0}]]},
        "Format Alert":      {"main": [[{"node": "Send Telegram Alert", "type": "main", "index": 0}]]},
    }

    s2, body = n8n("PUT", f"/workflows/{ERROR_ALERTER_ID}", workflow_put_payload(wf))
    print(f"  PUT → HTTP {s2}")
    if s2 >= 300:
        print(f"  body: {str(body)[:300]}")
        return False

    # Activate it
    s3, _ = n8n("POST", f"/workflows/{ERROR_ALERTER_ID}/activate")
    print(f"  activate → HTTP {s3}")
    return s3 < 300


# ───────────────────── Fix #3: errorWorkflow on every active workflow ──────
def wire_error_workflow():
    print("\n=== Fix 3: Wire errorWorkflow on every active workflow ===")
    s, data = n8n("GET", "/workflows?limit=250")
    if s >= 300:
        print(f"  GET failed: {s}"); return False
    targets = [w for w in data.get("data", []) if w.get("active") and w["id"] != ERROR_ALERTER_ID]
    print(f"  {len(targets)} active workflows to wire")
    ok, fail = 0, 0
    for w in targets:
        s2, full = n8n("GET", f"/workflows/{w['id']}")
        if s2 >= 300:
            print(f"  ✗ GET {w['name']}: {s2}"); fail += 1; continue
        settings = full.get("settings") or {"executionOrder": "v1"}
        if settings.get("errorWorkflow") == ERROR_ALERTER_ID:
            ok += 1; continue
        settings["errorWorkflow"] = ERROR_ALERTER_ID
        full["settings"] = settings
        s3, body = n8n("PUT", f"/workflows/{w['id']}", workflow_put_payload(full))
        if s3 < 300:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {w['name']} PUT {s3}: {str(body)[:200]}")
    print(f"  {ok} wired, {fail} failed")
    return fail == 0


# ───────────────────── Fix #4: Register Shopify webhooks ───────────────────
def shopify_token():
    return subprocess.check_output(
        ["security", "find-generic-password", "-s", "shopify-bonpet-admin-token", "-w"]
    ).decode().strip()


def shopify_request(method, path, body=None):
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VER}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-Shopify-Access-Token": shopify_token(),
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
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def register_shopify_webhooks():
    print("\n=== Fix 4: Register Shopify Admin webhooks ===")
    s, existing = shopify_request("GET", "/webhooks.json")
    if s >= 300:
        print(f"  GET webhooks failed: {s} {existing}"); return False
    have = {(w["topic"], w["address"]) for w in existing.get("webhooks", [])}
    print(f"  Existing webhooks: {len(have)}")

    ok, fail, skipped = 0, 0, 0
    for topic, path in SHOPIFY_WEBHOOKS:
        addr = f"{SELF_HOSTED_WEBHOOK_BASE}/{path}"
        if (topic, addr) in have:
            print(f"  ⊙ {topic} → {addr} already registered")
            skipped += 1; continue
        s2, body = shopify_request("POST", "/webhooks.json", {
            "webhook": {"topic": topic, "address": addr, "format": "json"}
        })
        if s2 < 300:
            print(f"  ✓ {topic} → {addr}")
            ok += 1
        else:
            print(f"  ✗ {topic}: HTTP {s2} {str(body)[:300]}")
            fail += 1
    print(f"  {ok} registered, {skipped} already existed, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}
    if only in ("all", "1", "morning"):
        results["morning_briefing"] = fix_morning_briefing()
    if only in ("all", "2", "alerter"):
        results["error_alerter"] = fix_error_alerter()
    if only in ("all", "3", "wire"):
        results["wire_error_workflow"] = wire_error_workflow()
    if only in ("all", "4", "shopify"):
        results["shopify_webhooks"] = register_shopify_webhooks()

    print("\n=== Summary ===")
    for k, v in results.items():
        print(f"  {k}: {'OK' if v else 'FAILED'}")

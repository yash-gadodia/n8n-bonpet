#!/usr/bin/env python3
"""Replay only the failed Checkouts + Fulfillments from the original backfill window.
Slower throttle (1.2s between each = 50/min, well under Sheets API 60/min limit)."""
import json, os, subprocess, time
from datetime import datetime, timedelta, timezone
from urllib import request, error

SHOP_TOKEN = subprocess.check_output(["security","find-generic-password","-s","shopify-bonpet-admin-token","-w"], text=True).strip()
SHOP = "thegoodpetco.myshopify.com"
N8N = "https://n8n.thebonpet.com"
SINCE = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
SLEEP = 1.2  # 50 req/min, safe under 60/min limit

def shopify(path, query=""):
    url = f"https://{SHOP}/admin/api/2026-04/{path}{('?'+query) if query else ''}"
    req = request.Request(url)
    req.add_header("X-Shopify-Access-Token", SHOP_TOKEN)
    return json.loads(request.urlopen(req, timeout=60).read())

def post_n8n(path, body):
    req = request.Request(f"{N8N}/webhook/{path}", data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except error.HTTPError as e:
        return e.code, e.read().decode()[:100]

# Re-pull source data
print("Re-pulling Shopify data...")
orders = shopify("orders.json", f"status=any&updated_at_min={SINCE}&limit=250")["orders"]
checkouts = shopify("checkouts.json", f"updated_at_min={SINCE}&limit=250").get("checkouts", [])

# Replay fulfillments (slow)
print(f"\nReplaying {sum(len(o.get('fulfillments') or []) for o in orders)} fulfillments at {SLEEP}s/req...")
ful_count = ok_count = fail_count = 0
for o in orders:
    for ful in (o.get("fulfillments") or []):
        ful_count += 1
        payload = {"order_id": o["id"], "fulfillment": ful, "order": o}
        code, body = post_n8n("shopify-fulfillments-ingest", payload)
        if code == 200: ok_count += 1
        else:
            fail_count += 1
            print(f"  FAIL ful {o.get('name')}: {code} {body[:80]}")
        time.sleep(SLEEP)
print(f"  Fulfillments: {ok_count}/{ful_count} OK, {fail_count} fail\n")

# Replay checkouts (slow)
print(f"Replaying {len(checkouts)} checkouts at {SLEEP}s/req...")
ok_count = fail_count = 0
for ck in checkouts:
    code, body = post_n8n("shopify-checkouts-ingest", ck)
    if code == 200: ok_count += 1
    else:
        fail_count += 1
        print(f"  FAIL ck {ck.get('id')}: {code} {body[:80]}")
    time.sleep(SLEEP)
print(f"  Checkouts: {ok_count}/{len(checkouts)} OK, {fail_count} fail")

print("\nReplay complete. Wait ~30s then verify executions.")

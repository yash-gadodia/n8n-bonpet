#!/usr/bin/env python3
"""Backfill Shopify ingests by replaying last N days of events to new n8n webhooks.
All ingest workflows use upsert (appendOrUpdate) on Google Sheets, so safe to replay."""
import json, os, subprocess, time
from datetime import datetime, timedelta, timezone
from urllib import request, error

SHOP_TOKEN = subprocess.check_output(["security","find-generic-password","-s","shopify-bonpet-admin-token","-w"], text=True).strip()
SHOP = "thegoodpetco.myshopify.com"
N8N = "https://n8n.thebonpet.com"
DAYS_BACK = 5
SINCE = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).isoformat()
print(f"Backfilling Shopify events since {SINCE} ({DAYS_BACK} days)\n")

def shopify(path, query=""):
    url = f"https://{SHOP}/admin/api/2026-04/{path}{('?'+query) if query else ''}"
    req = request.Request(url)
    req.add_header("X-Shopify-Access-Token", SHOP_TOKEN)
    req.add_header("accept", "application/json")
    return json.loads(request.urlopen(req, timeout=60).read())

def post_n8n(webhook_path, body):
    url = f"{N8N}/webhook/{webhook_path}"
    req = request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:100]
    except error.HTTPError as e:
        return e.code, e.read().decode()[:100]

# 1. Orders (paid + cancelled etc)
print("== Orders ==")
orders = shopify("orders.json", f"status=any&updated_at_min={SINCE}&limit=250")["orders"]
print(f"  Found {len(orders)} orders updated since {SINCE}")
for o in orders:
    code, _ = post_n8n("shopify-orders-ingest", o)
    if code != 200: print(f"  FAIL {o.get('name')}: {code}")
    time.sleep(0.3)
print(f"  Done ({len(orders)} sent)\n")

# 2. Customers
print("== Customers ==")
customers = shopify("customers.json", f"updated_at_min={SINCE}&limit=250")["customers"]
print(f"  Found {len(customers)} customers")
for c in customers:
    code, _ = post_n8n("customers-ingest", c)
    if code != 200: print(f"  FAIL {c.get('email')}: {code}")
    time.sleep(0.3)
print(f"  Done ({len(customers)} sent)\n")

# 3. Products (less time-sensitive, refresh active products)
print("== Products ==")
products = shopify("products.json", f"updated_at_min={SINCE}&limit=250")["products"]
print(f"  Found {len(products)} products")
for p in products:
    code, _ = post_n8n("shopify-products-ingest", p)
    if code != 200: print(f"  FAIL {p.get('id')}: {code}")
    time.sleep(0.3)
print(f"  Done ({len(products)} sent)\n")

# 4. Refunds — Shopify lists refunds via order_id, fetch from each order with refunds
print("== Refunds ==")
refund_count = 0
for o in orders:
    if o.get("refunds"):
        for refund in o["refunds"]:
            payload = {"order_id": o["id"], "refund": refund, "order": o}
            code, _ = post_n8n("shopify-refunds-ingest", payload)
            if code != 200: print(f"  FAIL refund on {o.get('name')}: {code}")
            refund_count += 1
            time.sleep(0.3)
print(f"  Done ({refund_count} refunds sent)\n")

# 5. Fulfillments — same pattern, embedded in orders
print("== Fulfillments ==")
ful_count = 0
for o in orders:
    for ful in (o.get("fulfillments") or []):
        payload = {"order_id": o["id"], "fulfillment": ful, "order": o}
        code, _ = post_n8n("shopify-fulfillments-ingest", payload)
        if code != 200: print(f"  FAIL fulfillment on {o.get('name')}: {code}")
        ful_count += 1
        time.sleep(0.3)
print(f"  Done ({ful_count} fulfillments sent)\n")

# 6. Checkouts — abandoned checkouts (separate endpoint)
print("== Checkouts (abandoned) ==")
checkouts = shopify("checkouts.json", f"updated_at_min={SINCE}&limit=250").get("checkouts", [])
print(f"  Found {len(checkouts)} abandoned checkouts")
for ck in checkouts:
    code, _ = post_n8n("shopify-checkouts-ingest", ck)
    if code != 200: print(f"  FAIL checkout {ck.get('id')}: {code}")
    time.sleep(0.3)
print(f"  Done ({len(checkouts)} sent)\n")

print("=" * 50)
print(f"Backfill complete: {len(orders)} orders, {len(customers)} customers, {len(products)} products, {refund_count} refunds, {ful_count} fulfillments, {len(checkouts)} checkouts")

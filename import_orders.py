#!/usr/bin/env python3
"""Backfill historical orders from Shopify CSV export → orders tab.
Shopify CSV has 1 row per LINE ITEM, so we group by order Name first."""
import csv, json, urllib.request, urllib.error, time, re
from collections import defaultdict

CSV_PATH = "/Users/yash/n8n-bonpet/exports/orders_2026-04-19/orders_export.csv"
WEBHOOK = "https://thebonpet.app.n8n.cloud/webhook/shopify-orders-ingest"
BATCH = 25
GRAMS_RE = re.compile(r"\((\d+)\s*g\)")


def parse_grams(name):
    m = GRAMS_RE.search(name or "")
    return int(m.group(1)) if m else 0


def detect_subscription(row):
    """Shopify tags subscription orders with 'Subscription' in the order Tags column.
    Also check Discount Code and Source as fallbacks."""
    tags = (row.get("Tags") or "").lower()
    if "subscription" in tags:
        return True
    source = (row.get("Source") or "").lower()
    if "subscription_contract" in source:
        return True
    dc = (row.get("Discount Code") or "").strip().strip('"').lower()
    if dc.startswith("subscription"):
        return True
    return False


# --- Read CSV, group line items by order Name ---
orders_by_name = defaultdict(lambda: {"line_items": []})
total_lines = 0
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        total_lines += 1
        name = row.get("Name") or ""
        if not name:
            continue
        o = orders_by_name[name]
        # First time we see this order, capture order-level fields
        if "order_name" not in o:
            o["order_name"] = name
            o["order_id"] = (row.get("Id") or "").strip()
            o["order_date"] = row.get("Paid at") or row.get("Created at") or ""
            o["email"] = row.get("Email") or ""
            o["is_subscription"] = detect_subscription(row)
            o["phone"] = (row.get("Phone") or row.get("Shipping Phone")
                          or row.get("Billing Phone") or "")
            full_name = (row.get("Shipping Name") or row.get("Billing Name") or "").strip()
            parts = full_name.split(" ", 1)
            o["first_name"] = parts[0] if parts else ""
            o["last_name"] = parts[1] if len(parts) > 1 else ""
            o["total_price"] = row.get("Total") or ""
            o["currency"] = row.get("Currency") or "SGD"
            o["city"] = row.get("Shipping City") or row.get("Billing City") or ""
            o["tags"] = row.get("Tags") or ""
            o["customer_id"] = ""  # not in CSV; lookup later via email if needed
        # Line item — accumulate
        title = row.get("Lineitem name") or ""
        qty = int(float(row.get("Lineitem quantity") or 0))
        sku = row.get("Lineitem sku") or ""
        grams = parse_grams(title)
        o["line_items"].append({
            "variant_id": "",  # not in CSV
            "title": title,
            "quantity": qty,
            "grams": grams,
            "sku": sku,
            "selling_plan": None,  # backfill assumes non-subscription; live Flow data will set this
        })

print(f"Read {total_lines} line items → {len(orders_by_name)} unique orders")

# --- Convert to ingest-friendly shape ---
orders_to_send = []
for name, o in orders_by_name.items():
    if not o.get("order_id"):
        continue  # skip malformed rows
    total_grams = sum(li["grams"] * li["quantity"] for li in o["line_items"])
    # If customer had ANY subscription order, poison all their line_items's selling_plan
    # so the parse node marks this order as subscription too. (Backfill-only workaround.)
    selling_plan = "Backfill-Subscription" if o.get("is_subscription") else None
    for li in o["line_items"]:
        if selling_plan:
            li["selling_plan"] = selling_plan

    orders_to_send.append({
        "order_id": o["order_id"],
        "order_name": o["order_name"],
        "order_date": o["order_date"],
        "customer": {
            "id": o["customer_id"],
            "first_name": o["first_name"],
            "last_name": o["last_name"],
            "email": o["email"],
            "phone": o["phone"],
        },
        "shipping_address": {"phone": o["phone"], "city": o["city"]},
        "total_price": o["total_price"],
        "currency": o["currency"],
        "tags": o["tags"],
        "line_items": o["line_items"],
        "_total_grams_hint": total_grams,
    })

print(f"Prepared {len(orders_to_send)} orders for ingest")

# --- POST in batches ---
sent, fails = 0, 0
for i in range(0, len(orders_to_send), BATCH):
    batch = orders_to_send[i:i+BATCH]
    try:
        req = urllib.request.Request(WEBHOOK, data=json.dumps({"orders": batch}).encode(),
                                      method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status == 200:
                sent += len(batch)
                bn = i // BATCH + 1
                tn = (len(orders_to_send) + BATCH - 1) // BATCH
                if bn % 10 == 0 or bn == tn:
                    print(f"  ✓ batch {bn}/{tn}: total {sent}")
            else:
                fails += 1
    except Exception as e:
        fails += 1
        print(f"  ✗ batch {i//BATCH + 1}: {e}")
    time.sleep(0.4)

print(f"\n✅ Done. Sent {sent} orders; failures: {fails}")
print("   Sheet writes are async, will drain in ~10-15 mins.")

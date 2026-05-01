#!/usr/bin/env python3
"""Backfill abandoned carts from CSV to the checkouts tab.
CSV uses Shopify orders format — one row per line item. We group by Name (checkout ID)."""
import csv, json, re, urllib.request, time
from collections import defaultdict

CSV = "/Users/yash/Documents/TheBonPet/abandoned_carts.csv"
WEBHOOK = "https://thebonpet.app.n8n.cloud/webhook/shopify-checkouts-ingest"
GRAMS_RE = re.compile(r"\((\d+)\s*g\)")


def grams(name):
    m = GRAMS_RE.search(name or ""); return int(m.group(1)) if m else 0


def normalize_phone(p):
    if not p: return ""
    s = str(p).replace(" ", "").strip()
    if s.startswith("+"): return "+" + "".join(c for c in s[1:] if c.isdigit())
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) == 8 and digits[0] in "689": return "+65" + digits
    if len(digits) == 10 and digits.startswith("65"): return "+" + digits
    if 8 <= len(digits) <= 15: return "+" + digits
    return ""


carts = defaultdict(lambda: {"line_items": []})
with open(CSV) as f:
    for row in csv.DictReader(f):
        name = (row.get("Name") or "").strip()
        if not name: continue
        c = carts[name]
        if "checkout_token" not in c:
            c["checkout_token"] = name.lstrip("#")
            c["checkout_id"] = (row.get("Id") or "").strip()
            c["email"] = row.get("Email") or ""
            c["phone"] = normalize_phone(row.get("Phone") or row.get("Shipping Phone") or row.get("Billing Phone") or "")
            full = (row.get("Shipping Name") or row.get("Billing Name") or "").strip()
            parts = full.split(" ", 1)
            c["first_name"] = parts[0] if parts else ""
            c["last_name"] = parts[1] if len(parts) > 1 else ""
            c["total_price"] = row.get("Total") or ""
            c["currency"] = row.get("Currency") or "SGD"
            c["created_at"] = row.get("Created at") or ""
            c["updated_at"] = row.get("Created at") or ""
            c["completed_at"] = row.get("Paid at") or ""  # abandoned if empty
            c["abandoned_checkout_url"] = ""  # not in CSV
            c["customer_id"] = ""
        c["line_items"].append({
            "title": row.get("Lineitem name") or "",
            "quantity": int(float(row.get("Lineitem quantity") or 0)),
            "variant_id": "",
            "price": row.get("Lineitem price") or "",
            "grams": grams(row.get("Lineitem name") or ""),
        })

rows = []
for k, c in carts.items():
    rows.append({
        "token": c["checkout_token"],
        "id": c["checkout_id"],
        "email": c["email"],
        "phone": c["phone"],
        "customer": {"first_name": c["first_name"], "last_name": c["last_name"]},
        "shipping_address": {"phone": c["phone"]},
        "total_price": c["total_price"],
        "currency": c["currency"],
        "created_at": c["created_at"],
        "updated_at": c["updated_at"],
        "completed_at": c["completed_at"],
        "abandoned_checkout_url": c["abandoned_checkout_url"],
        "line_items": c["line_items"],
    })

print(f"Parsed {len(rows)} unique abandoned carts")
abandoned = sum(1 for r in rows if not r["completed_at"])
print(f"  Abandoned (not completed): {abandoned}")
print(f"  Completed (recovered): {len(rows) - abandoned}")

# POST in batches to the checkouts webhook. The existing parse handles {checkout} shape
# but not batch {checkouts:[...]} — post one-at-a-time (slower but simple)
sent = 0
for i, r in enumerate(rows):
    try:
        req = urllib.request.Request(WEBHOOK, data=json.dumps(r).encode(),
                                      method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200: sent += 1
    except Exception as e:
        print(f"  ✗ row {i}: {e}")
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(rows)} sent")
    time.sleep(0.15)

print(f"\n✅ Sent {sent}/{len(rows)} to ingest webhook")

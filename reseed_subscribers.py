#!/usr/bin/env python3
"""Re-seed the "subscribers" sheet (Customer Orders DB, gid 700700) from a fresh
Shopify subscription-contracts CSV export.

WHY: the Subscription Health Pulse counts active/paused/cancelled from that sheet.
The sheet is fed by a Shopify Flow -> ingest webhook that drifts (misses status
changes), so the count goes stale. Until subscription read scope is added to the
Shopify custom app (see below), re-seed manually from a fresh export.

HOW TO GET THE CSV:
  Shopify Admin -> Apps -> Subscriptions -> Subscription contracts -> Export -> CSV.

USAGE:
  python3 reseed_subscribers.py "/path/to/export.csv"

It dedupes by contract (one row per contract), upserts every contract via the
existing ingest webhook (appendOrUpdate on contract_id), browser UA to clear
Cloudflare, paced to avoid OOM. Verify after with the pulse or a sheet read.

DURABLE FIX (removes the manual step): add `read_own_subscription_contracts` to the
"Shopify Access Token n8n" custom app, then a scheduled workflow can pull
subscriptionContracts straight from Shopify daily. Until then, this script.
"""
import csv, json, sys, time, urllib.request, urllib.error

WEBHOOK = "https://n8n.thebonpet.com/webhook/shopify-subscriptions-ingest"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
BATCH = 25
PACE_SECONDS = 3  # keep < 5 sends / 2 min to avoid n8n OOM


def main(csv_path):
    rows = list(csv.DictReader(open(csv_path)))
    by_contract = {}
    for r in rows:
        cid = (r.get("handle") or r.get("contract_id") or "").strip()
        if not cid:
            continue
        cid = cid.rsplit("/", 1)[-1]  # gid://.../SubscriptionContract/123 -> 123
        by_contract.setdefault(cid, r)

    payload = [{
        "contract_id": cid,
        "customer_id": (r.get("customer_id") or "").strip(),
        "email": "",
        "status": (r.get("status") or "").upper().strip(),
        "upcoming_billing_date": (r.get("upcoming_billing_date") or "").strip(),
        "cadence_interval": r.get("cadence_interval") or "",
        "cadence_interval_count": r.get("cadence_interval_count") or "",
        "currency_code": r.get("currency_code") or "",
        "line_variant_id": (r.get("line_variant_id") or "").strip(),
        "line_quantity": r.get("line_quantity") or "",
        "line_selling_plan_name": r.get("line_selling_plan_name") or "",
    } for cid, r in by_contract.items()]

    from collections import Counter
    breakdown = Counter(p["status"] for p in payload)
    print(f"{len(payload)} unique contracts | {dict(breakdown)}")

    sent = 0
    for i in range(0, len(payload), BATCH):
        batch = payload[i:i + BATCH]
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps({"subscribers": batch}).encode(),
            method="POST", headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                sent += len(batch)
                print(f"  batch {i // BATCH + 1}: {res.status} (total {sent})")
        except urllib.error.HTTPError as e:
            print(f"  batch {i // BATCH + 1}: HTTP {e.code} {e.read().decode()[:120]}")
        time.sleep(PACE_SECONDS)
    print(f"DONE: {sent}/{len(payload)} upserted")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 reseed_subscribers.py <export.csv>")
        sys.exit(1)
    main(sys.argv[1])

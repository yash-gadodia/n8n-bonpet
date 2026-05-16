#!/usr/bin/env python3
"""One-shot cleanup after the 11:03 bulk run:
  - 7 customers received WA but Mark Fulfilled crashed (403, missing UA header).
    Direct-curl mark-fulfilled for each so Shopify reflects fulfilled state.
  - Angelia (#3283) was missed because her delivery_method=NINJAVAN_COLD_CHAIN
    excludes her from the workflow's filter. Send her the same WA via direct
    WA endpoint. DO NOT auto-fulfill her (NinjaVan, not self-collect — flag
    for manual OMS handling).
"""
import json, subprocess, time, urllib.request, urllib.error

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
WMS_PAT = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wms-pat", "-w"]
).decode().strip()
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()

WMS_LIST = "https://api.thebonpet.com/wms/orders?limit=400"
WA_URL = "https://api.thebonpet.com/whatsapp/send"
APOLOGY_NOTE = (
    "PS: Sorry about the incorrect WhatsApp template that went out when you placed "
    "your order, that's been fixed on our end now 🙏"
)


def http(method, url, body=None, extra_headers=None):
    headers = {"User-Agent": UA}
    if extra_headers: headers.update(extra_headers)
    data = json.dumps(body).encode() if body is not None else None
    if data: headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fetch_orders():
    s, body = http("GET", WMS_LIST, extra_headers={"Authorization": f"Bearer {WMS_PAT}"})
    return json.loads(body).get("orders", [])


def build_message(first_name, order_id):
    return "\n".join([
        f"Hi {first_name}! 🐾",
        "",
        f"Your order {order_id} is packed and ready for self-collection 🎉",
        "",
        "📍 *Address:* 5 Siglap Road, Lobby K (Mandarin Gardens Condo), unit #17-38, Singapore 448908",
        "",
        f"❄️ Look for the pack labeled with your order ID *{order_id}* in the *WHITE freezer*",
        "ℹ️ Please skip the *ORANGE freezer*, it's not part of the pickup",
        "",
        "Everything is clearly labeled so it should be straightforward 👌",
        "",
        "A few quick notes:",
        "🙏 Please try to pick up within 7 days, we have limited storage space",
        "✅ Pickup anytime, the freezer is accessible 24/7",
        "✅ The pack itself is a cooler bag, just try to get it into your freezer within 2-3 hours of pickup",
        "✅ Once frozen, good for up to 1 year",
        "",
        APOLOGY_NOTE,
        "",
        "Any questions, just reply here. Thanks so much for choosing us 💛",
        "<3 The Bon Pet team",
    ])


def fulfill(internal_id, order_name):
    print(f"  fulfill internal_id={internal_id} ({order_name}) ...")
    s, body = http("POST",
                   f"https://api.thebonpet.com/wms/orders/{internal_id}/mark-fulfilled",
                   extra_headers={"Authorization": f"Bearer {WMS_PAT}"})
    print(f"  → HTTP {s}  {body[:150]}")
    return s == 200


def main():
    print("📡 fetching WMS orders…")
    orders = fetch_orders()
    print(f"   {len(orders)} orders found\n")

    # Step 1: fulfill the 7 self-collect orders that got WA but stuck unfulfilled.
    stuck = [o for o in orders
             if o.get("delivery_method") == "SELF_COLLECTION"
             and o.get("fulfillment_status") == "UNFULFILLED"]
    print(f"━━━ STEP 1: fulfilling {len(stuck)} self-collect orders ━━━")
    for o in stuck:
        fulfill(o["id"], o["order_name"])
        time.sleep(2)
    print()

    # Step 2: send Angelia (NinjaVan, not auto-fulfilled).
    print("━━━ STEP 2: sending Angelia (NinjaVan, no auto-fulfill) ━━━")
    angelia = next((o for o in orders if o.get("order_name") == "#3283"), None)
    if not angelia:
        print("  ❌ #3283 not found in WMS response, skipping")
    else:
        first = angelia.get("shipping_first_name") or "there"
        msg = build_message(first, angelia["order_name"])
        s, body = http("POST", WA_URL,
                       body={"phone_number": angelia["shipping_phone"], "message": msg},
                       extra_headers={"X-API-Key": WA_KEY})
        print(f"  → WA to {angelia['shipping_phone']} ({first}): HTTP {s}  {body[:200]}")
        print(f"  ⚠️  #{angelia['order_name']} delivery_method={angelia.get('delivery_method')}.")
        print(f"     NOT auto-fulfilled. Adjust manually in OMS if Angelia is switching to self-collect.")


if __name__ == "__main__":
    main()

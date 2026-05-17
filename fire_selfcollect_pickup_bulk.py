#!/usr/bin/env python3
"""One-shot bulk fire of pickup-ready WAs to ALL currently-unfulfilled self-collect
orders. Routes through the n8n webhook so WA send + auto-fulfill use the same
template + flow as the per-order Telegram tag path.

Run modes:
  python3 fire_selfcollect_pickup_bulk.py preview   # show all WAs that WOULD send
  python3 fire_selfcollect_pickup_bulk.py send      # actually fire (8s between calls)

Kill-switch: Ctrl+C between sends. Already-fired orders won't refire.

Per [[feedback_n8n_broadcast_safeguards]]: DRY count gate enforced via preview mode.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error

WMS_PAT = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wms-pat", "-w"]
).decode().strip()
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()

WMS_LIST = "https://api.thebonpet.com/wms/orders?delivery_method=SELF_COLLECTION&fulfillment_status=UNFULFILLED&limit=200"
PICKUP_WEBHOOK = "https://n8n.thebonpet.com/webhook/selfcollect-pickup-ready-3f9c1a"
WA_DIRECT_URL = "https://api.thebonpet.com/whatsapp/send"
SEND_INTERVAL_SECONDS = 8
YASH_MIRROR = {"first_name": "Yash", "order_name": "#YASH-TEST", "phone": "+6581394225"}

APOLOGY_NOTE = (
    "PS: Sorry about the incorrect WhatsApp template that went out when you placed "
    "your order, that's been fixed on our end now 🙏"
)


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_orders():
    req = urllib.request.Request(
        WMS_LIST,
        headers={"Authorization": f"Bearer {WMS_PAT}", "User-Agent": UA},
    )
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    return data.get("orders", [])


def build_preview_message(o):
    first = (o.get("shipping_first_name")
             or (o.get("shipping_name") or "").split(" ")[0]
             or "there")
    lines = [
        f"Hi {first}! 🐾",
        "",
        f"Your order {o['order_name']} is packed and ready for self-collection 🎉",
        "",
        "📍 *Address:* 5 Siglap Road, Lobby K (Mandarin Gardens Condo), unit #17-38, Singapore 448908",
        "",
        f"❄️ Look for the pack labeled with your order ID *{o['order_name']}* in the *WHITE freezer*",
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
        "❤️ The Bon Pet team",
    ]
    return "\n".join(lines)


def fire(order_name_int):
    body = {
        "order_number": int(order_name_int),
        "extra_note": APOLOGY_NOTE,
        "sender_name": "Bulk fire (Yash)",
    }
    req = urllib.request.Request(
        PICKUP_WEBHOOK,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return 0, f"exception: {e}"


def fire_yash_mirror():
    """Send Yash a direct WA with the same template (no fulfillment, no real order)."""
    fake_order = {
        "shipping_first_name": YASH_MIRROR["first_name"],
        "shipping_name": YASH_MIRROR["first_name"],
        "order_name": YASH_MIRROR["order_name"],
    }
    message = build_preview_message(fake_order)
    body = {"phone_number": YASH_MIRROR["phone"], "message": message}
    req = urllib.request.Request(
        WA_DIRECT_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": WA_KEY, "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return 0, f"exception: {e}"


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "preview").lower()
    if mode not in ("preview", "send"):
        print(f"unknown mode {mode!r}, use 'preview' or 'send'")
        sys.exit(1)

    orders = fetch_orders()
    print(f"📦 {len(orders)} unfulfilled self-collect orders\n")

    for i, o in enumerate(orders, 1):
        order_name = o["order_name"]
        on_int = order_name.lstrip("#")
        name = o.get("shipping_name") or "(no name)"
        phone = o.get("shipping_phone")
        pickup = o.get("pickup_date") or "?"
        print(f"━━━ {i}. {order_name}  {name}  {phone}  (pickup {pickup}) ━━━")

        if mode == "preview":
            print(build_preview_message(o))
            print()
        else:
            print(f"  → POST {PICKUP_WEBHOOK} order_number={on_int}")
            t0 = time.time()
            status, body = fire(on_int)
            dt = time.time() - t0
            print(f"  → HTTP {status} in {dt:.1f}s  body={body!r}")
            if i < len(orders):
                print(f"  ⏱  sleeping {SEND_INTERVAL_SECONDS}s before next…")
                time.sleep(SEND_INTERVAL_SECONDS)
            print()

    if mode == "send":
        print(f"━━━ {len(orders)+1}. {YASH_MIRROR['order_name']}  Yash (mirror)  {YASH_MIRROR['phone']}  (direct WA, no fulfillment) ━━━")
        time.sleep(SEND_INTERVAL_SECONDS)
        t0 = time.time()
        status, body = fire_yash_mirror()
        dt = time.time() - t0
        print(f"  → HTTP {status} in {dt:.1f}s  body={body!r}")
        print()

    if mode == "preview":
        print()
        print("☝️  DRY RUN. To actually fire:")
        print(f"    python3 {os.path.basename(__file__)} send")
        print(f"    (will send {len(orders)} customer WAs + auto-fulfill {len(orders)} orders + 1 mirror to Yash,"
              f" {SEND_INTERVAL_SECONDS}s apart)")


if __name__ == "__main__":
    main()

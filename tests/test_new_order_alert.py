#!/usr/bin/env python3
"""Unit tests for build_new_order_alert.py FORMAT_JS logic.

Runs the actual FORMAT_JS string (extracted from the build script) inside a
Node subprocess with mocked $('node-name') context, against realistic
Shopify orders/paid fixtures. Asserts on the produced Telegram message
content + structured fields.

Run: python3 tests/test_new_order_alert.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Import the build script as a module to extract FORMAT_JS
spec = importlib.util.spec_from_file_location("noa", os.path.join(ROOT, "build_new_order_alert.py"))
noa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(noa)
FORMAT_JS = noa.FORMAT_JS

# ── Fixture factory ────────────────────────────────────────────────────────────
def make_shopify_payload(*, order_number, customer_id, customer_orders_count,
                        shipping_title, discount_codes=None, line_items=None,
                        delivery_date="2026-06-01", phone="+6591234567",
                        first_name="Test", last_name="Customer",
                        email="test@example.com", total="42.50",
                        address1="1 Test Rd", address2=None, zip_code="100001"):
    return {
        "body": {
            "name": f"#{order_number}",
            "order_number": order_number,
            "id": 1000000 + order_number,
            "total_price": total,
            "subtotal_price": total,
            "currency": "SGD",
            "created_at": "2026-05-16T08:00:00Z",
            "customer": {
                "id": customer_id,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": phone,
                "orders_count": customer_orders_count,
            },
            "shipping_address": {
                "address1": address1,
                "address2": address2,
                "zip": zip_code,
            },
            "shipping_lines": [{"title": shipping_title, "code": shipping_title}],
            "discount_codes": [{"code": c} for c in (discount_codes or [])],
            "line_items": line_items or [
                {"quantity": 1, "title": "Test Product", "variant_title": "Chicken"}
            ],
            "note_attributes": [{"name": "Delivery Date", "value": delivery_date}],
            "note": "",
            "phone": phone,
            "email": email,
        }
    }


def make_customers_db_rows():
    """Mock rows from the Customers sheet (Read Customers node output)."""
    return [
        {"customer_id": "555000001", "first_name": "Alice", "last_name": "Tan",
         "phone": "+6591111111", "email": "alice@example.com"},
        {"customer_id": "555000002", "first_name": "Bob", "last_name": "Lim",
         "phone": "98765432", "email": "bob@example.com"},  # 8-digit local → should normalize
    ]


# ── Node runner ────────────────────────────────────────────────────────────────
NODE_HARNESS = r"""
const fs = require('fs');
const ctx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function $(nodeName) {
  return {
    first() { return { json: ctx[nodeName][0] }; },
    all()   { return ctx[nodeName].map(j => ({ json: j })); },
  };
}

const formatFn = new Function('$', __FORMAT_JS_BODY__);
const result = formatFn($);
console.log(JSON.stringify(result));
"""


def run_format_js(shopify_payload, customers_rows):
    """Execute FORMAT_JS in Node with mocked $() returning the given data."""
    ctx = {
        "Shopify Webhook (orders/paid)": [shopify_payload],
        "Read Customers": customers_rows,
    }
    # Wrap FORMAT_JS body in a function. The JS in build script is the body of
    # a Code node, expecting `return [...]` at the end.
    harness = NODE_HARNESS.replace("__FORMAT_JS_BODY__", json.dumps(FORMAT_JS))
    with tempfile.TemporaryDirectory() as tmp:
        ctx_path = os.path.join(tmp, "ctx.json")
        harness_path = os.path.join(tmp, "harness.js")
        with open(ctx_path, "w") as f: json.dump(ctx, f)
        with open(harness_path, "w") as f: f.write(harness)
        r = subprocess.run(["node", harness_path, ctx_path],
                           capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    output = json.loads(r.stdout.strip())
    # FORMAT_JS returns [{json: {...}}], unwrap to the inner dict
    return output[0]["json"]


# ── Assertion helpers ──────────────────────────────────────────────────────────
PASS_COUNT = 0
FAIL_COUNT = 0


def case(name, fn):
    global PASS_COUNT, FAIL_COUNT
    try:
        fn()
        print(f"  ✅ {name}")
        PASS_COUNT += 1
    except AssertionError as e:
        print(f"  ❌ {name}: {e}")
        FAIL_COUNT += 1
    except Exception as e:
        print(f"  💥 {name}: {type(e).__name__}: {e}")
        FAIL_COUNT += 1


def assert_in(needle, haystack, label=""):
    assert needle in haystack, f"{label or 'expected'} {needle!r} not found in: {haystack[:300]}"


def assert_not_in(needle, haystack, label=""):
    assert needle not in haystack, f"{label or 'expected'} {needle!r} unexpectedly found in: {haystack[:300]}"


# ── Test cases ─────────────────────────────────────────────────────────────────
def test_first_order_self_collect_no_discount():
    payload = make_shopify_payload(
        order_number=9001, customer_id=555000001, customer_orders_count=1,
        shipping_title="Self-collect",
    )
    out = run_format_js(payload, make_customers_db_rows())
    msg = out["text"]
    assert_in("🏪 *New order* #9001", msg, "header with self-collect emoji")
    assert_in("🆕 *First order!*", msg, "first-order tag")
    assert_in("Alice Tan", msg, "customer name from DB")
    assert_in("+6591111111", msg, "phone from DB")
    assert_in("Self-collection 🏪", msg, "delivery method label")
    assert_not_in("🎟️", msg, "no discount line when no code")
    assert out["delivery_method"] == "Self-collection 🏪"


def test_returning_customer_cold_chain_with_promo():
    payload = make_shopify_payload(
        order_number=9002, customer_id=555000002, customer_orders_count=3,
        shipping_title="NinjaVan Cold Chain", discount_codes=["BONPET10"],
    )
    out = run_format_js(payload, make_customers_db_rows())
    msg = out["text"]
    assert_in("❄️ *New order* #9002", msg, "cold-chain emoji header")
    assert_in("↩️ *Returning customer* (order #3)", msg, "returning Nth tag")
    assert_in("Bob Lim", msg, "Bob name")
    assert_in("+6598765432", msg, "8-digit phone normalized to +65 prefix")
    assert_in("🎟️ Code: *BONPET10*", msg, "promo code surfaced")
    assert_in("NinjaVan cold chain", msg, "cold chain method")


def test_new_subscriber():
    payload = make_shopify_payload(
        order_number=9003, customer_id=555000001, customer_orders_count=1,
        shipping_title="NinjaVan Next Day",
        discount_codes=["Subscription First Order"],
    )
    out = run_format_js(payload, make_customers_db_rows())
    msg = out["text"]
    assert_in("🆕✨ *New subscriber!*", msg, "new subscriber tag")
    assert_not_in("🎟️", msg, "Subscription code hidden from visible discount line")
    assert_in("📦", msg, "NextDay emoji")


def test_subscription_renewal():
    payload = make_shopify_payload(
        order_number=9004, customer_id=555000002, customer_orders_count=5,
        shipping_title="NinjaVan Cold Chain",
        discount_codes=["Subscription Recurring 10% off"],
    )
    out = run_format_js(payload, make_customers_db_rows())
    msg = out["text"]
    assert_in("🔁 *Subscription renewal* (order #5)", msg, "renewal tag")
    assert_not_in("🎟️", msg, "Subscription code hidden")


def test_customer_not_in_db_fallback():
    """Customer not in the sheet → falls back to webhook payload customer fields."""
    payload = make_shopify_payload(
        order_number=9005, customer_id=999999999, customer_orders_count=1,
        shipping_title="Self-collect",
        first_name="Unknown", last_name="Stranger", phone="+6582828282",
    )
    out = run_format_js(payload, make_customers_db_rows())
    msg = out["text"]
    assert_in("Unknown Stranger", msg, "fallback name from webhook")
    assert_in("+6582828282", msg, "fallback phone from webhook")


def test_missing_phone_renders_placeholder():
    payload = make_shopify_payload(
        order_number=9006, customer_id=999999999, customer_orders_count=1,
        shipping_title="Self-collect", phone="",
    )
    payload["body"]["customer"]["phone"] = ""
    payload["body"]["phone"] = ""
    out = run_format_js(payload, make_customers_db_rows())
    assert_in("(no phone)", out["text"], "phone placeholder when missing everywhere")


def test_telegram_routing_fields():
    """chat_id + message_thread_id are surfaced for the Telegram Post node."""
    payload = make_shopify_payload(
        order_number=9007, customer_id=555000001, customer_orders_count=1,
        shipping_title="Self-collect",
    )
    out = run_format_js(payload, make_customers_db_rows())
    # chat_id is negative supergroup id, thread_id is 34253 (weslee thread)
    assert out["chat_id"] == -1002184573790, f"chat_id mismatch: {out['chat_id']}"
    assert out["message_thread_id"] == 34253, f"thread_id mismatch: {out['message_thread_id']}"


# ── Workflow structure tests (pure-Python, no Node needed) ─────────────────────
def test_workflow_structure():
    wf = noa.build()
    node_names = [n["name"] for n in wf["nodes"]]
    for required in ("Shopify Webhook (orders/paid)", "Read Customers",
                     "Format Alert", "Telegram Post"):
        assert required in node_names, f"missing node {required}"
    # Telegram Post uses Markdown parse_mode
    tg = next(n for n in wf["nodes"] if n["name"] == "Telegram Post")
    assert "Markdown" in tg["parameters"]["jsonBody"], "Telegram Post should use Markdown parse_mode"
    # errorWorkflow is wired (so failures alert via global)
    assert wf["settings"]["errorWorkflow"] == "c3Vk2nt9WINzp9GH", "errorWorkflow not wired"


def main():
    print("🧪 test_new_order_alert.py")
    print()
    print("FORMAT_JS behaviour (Node subprocess):")
    case("first-order self-collect, no discount", test_first_order_self_collect_no_discount)
    case("returning customer, cold chain, promo code visible", test_returning_customer_cold_chain_with_promo)
    case("new subscriber (1st order + Subscription code)", test_new_subscriber)
    case("subscription renewal (Nth order + Subscription code)", test_subscription_renewal)
    case("customer not in DB → webhook fallback", test_customer_not_in_db_fallback)
    case("missing phone everywhere → placeholder", test_missing_phone_renders_placeholder)
    case("chat_id + message_thread_id routing fields", test_telegram_routing_fields)
    print()
    print("Workflow structure (Python):")
    case("required nodes + Markdown + errorWorkflow", test_workflow_structure)

    print()
    print(f"━━━ {PASS_COUNT} passed, {FAIL_COUNT} failed ━━━")
    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stop offline (POS/expo) sales reaching GA4 + Meta CAPI.

The orders/paid webhook fans out to two independent branches:

    Shopify Webhook ─┬─> Derive GA4/Meta Event ─> GA4 MP + Meta CAPI   (ad platforms)
                     └─> Extract Order ─> CRM + Telegram               (internal)

POS sales at expos were being sent to Google and Meta as *web* purchases. That
inflates ad-attributed conversions and ROAS, and it feeds the optimisers a
signal no ad ever produced.

Only the ad branch is gated. The CRM branch is left alone on purpose — a
customer met at an expo is a real customer and belongs in the sales CRM.

Idempotent: re-running is a no-op.
"""
import json
import os
import urllib.request
import urllib.error

from _online_sales import IS_ONLINE_JS

API = "https://n8n.thebonpet.com/api/v1"
WF_ID = "3UwBuHSH7PiWVhM2"
NODE = "Derive GA4/Meta Event"

ANCHOR = """const p = $input.first().json;
const body = p.body || p;"""

GATE = ANCHOR + """
""" + IS_ONLINE_JS + """
// Ad platforms must only ever see online sales — a POS sale at an expo is not
// an ad-attributable web purchase. The CRM branch is deliberately NOT gated.
if (!isOnlineOrder(body)) return [];"""


def http(method, path, body=None):
    key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": key,
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
        b = e.read().decode()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b


def main():
    status, wf = http("GET", f"/workflows/{WF_ID}")
    if status >= 300:
        raise SystemExit(f"GET failed: {status} {wf}")

    node = next((n for n in wf["nodes"] if n["name"] == NODE), None)
    if node is None:
        raise SystemExit(f"node {NODE!r} not found")

    js = node["parameters"]["jsCode"]
    if "isOnlineOrder" in js:
        print("Already patched — nothing to do.")
        return
    if ANCHOR not in js:
        raise SystemExit("anchor not found — node was edited; re-check by hand")

    node["parameters"]["jsCode"] = js.replace(ANCHOR, GATE, 1)

    payload = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings", {"executionOrder": "v1"}),
    }
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT {WF_ID} → HTTP {status} · GA4/Meta branch now online-only")
    if status >= 300:
        raise SystemExit(str(body)[:500])


if __name__ == "__main__":
    main()

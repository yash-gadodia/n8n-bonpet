#!/usr/bin/env python3
"""Patch "Weekly & Monthly Sales Report" to count online sales only.

This workflow was authored in the n8n UI, so there is no build_*.py to
regenerate it. This patch is idempotent — re-running it is a no-op.

Three fixes:
  1. Online-only filter (POS / HitPay POS / draft orders excluded).
  2. `fields=` was missing `cancelled_at`, so the existing cancelled-order
     guard silently never fired. Added, along with `source_name`.
  3. The monthly run fetches ~2 months of orders but read only the first
     250-order page and dropped the rest. Added Link-header pagination and
     switched the aggregator from $input.first() to $input.all().
"""
import json
import os
import urllib.request
import urllib.error

from _online_sales import IS_ONLINE_JS

API = "https://n8n.thebonpet.com/api/v1"
WF_ID = "Sv1nluGjlEhLX8CV"

SHOPIFY_PAGINATION = {
    "pagination": {
        "pagination": {
            "paginationMode": "responseContainsNextURL",
            "nextURL": "={{ ($response.headers.link || '').split(',').find(s => s.includes('rel=\"next\"'))?.match(/<([^>]+)>/)?.[1] }}",
            "paginationCompleteWhen": "other",
            "completeExpression": "={{ !($response.headers.link || '').includes('rel=\"next\"') }}",
            "limitPagesFetched": True,
            "maxRequests": 10,
        }
    }
}

OLD_FIELDS = "fields=id,total_price,subtotal_price,total_tax,currency,financial_status,created_at,line_items"
NEW_FIELDS = OLD_FIELDS + ",cancelled_at,source_name"

OLD_INPUT = """const data = $input.first().json;
const orders = data.orders || [];"""
NEW_INPUT = """const orders = $input.all().flatMap(it => it.json.orders || []).filter(o => o && o.id);"""

OLD_LOOP = """for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;"""
NEW_LOOP = """for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  if (!isOnlineOrder(o)) continue;"""


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
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

    changed = []
    for n in wf["nodes"]:
        if n["name"] == "Get Shopify Orders":
            url = n["parameters"].get("url", "")
            if OLD_FIELDS in url and NEW_FIELDS not in url:
                n["parameters"]["url"] = url.replace(OLD_FIELDS, NEW_FIELDS)
                changed.append("fields += cancelled_at,source_name")
            if n["parameters"].get("options") != SHOPIFY_PAGINATION:
                n["parameters"]["options"] = SHOPIFY_PAGINATION
                changed.append("pagination")

        if n["name"] == "Aggregate Metrics":
            js = n["parameters"]["jsCode"]
            if "isOnlineOrder" not in js:
                js = js.replace(OLD_LOOP, NEW_LOOP, 1)
                js = js.replace("function agg(list) {", IS_ONLINE_JS + "\nfunction agg(list) {", 1)
                changed.append("online-only filter")
            if OLD_INPUT in js:
                js = js.replace(OLD_INPUT, NEW_INPUT, 1)
                changed.append("read all pages")
            n["parameters"]["jsCode"] = js

    if not changed:
        print("Already patched — nothing to do.")
        return

    payload = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings", {"executionOrder": "v1"}),
    }
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT {WF_ID} → HTTP {status} · applied: {', '.join(changed)}")
    if status >= 300:
        raise SystemExit(str(body)[:500])


if __name__ == "__main__":
    main()

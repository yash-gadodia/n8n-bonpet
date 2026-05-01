#!/usr/bin/env python3
"""One-off: deploy a Shopify analysis workflow, fire it, read aggregated monthly numbers, delete it."""
import json
import uuid
import os
import time
import urllib.request
import urllib.error

API = "https://thebonpet.app.n8n.cloud/api/v1"
WF_NAME = "Growth Analysis (one-off)"

TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_ID = "growth-analysis-one-off-8f3c2d1e0b"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "heQ68zjV90EpARzU"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

EMIT_RANGES_JS = r"""// Emit 12 items — one per month — covering the last 12 full calendar months (SGT)
const now = new Date();
const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;
const sgtNow = new Date(now.getTime() + SGT_OFFSET_MS);

// End = SGT midnight of first day of CURRENT month
const endY = sgtNow.getUTCFullYear();
const endM = sgtNow.getUTCMonth();

const items = [];
for (let i = 12; i >= 1; i--) {
  const startYear  = endY;
  const startMonth = endM - i;
  const endMonth   = endM - i + 1;
  const mStart = Date.UTC(startYear, startMonth, 1) - SGT_OFFSET_MS;
  const mEnd   = Date.UTC(startYear, endMonth,   1) - SGT_OFFSET_MS;
  items.push({
    json: {
      month_index: 12 - i,
      month_key: new Date(mStart + SGT_OFFSET_MS).toISOString().slice(0, 7),  // YYYY-MM
      fetch_start: new Date(mStart).toISOString(),
      fetch_end:   new Date(mEnd).toISOString(),
    }
  });
}
return items;
"""

AGGREGATE_JS = r"""// Aggregate all orders returned across the 12 month-fetches into monthly buckets
const allOrders = $input.all()
  .flatMap(it => it.json.orders || [])
  .filter(o => o && o.id);

const SGT_OFFSET_MS = 8 * 60 * 60 * 1000;

const byMonth = new Map();
const seenIds = new Set();  // just in case of dedup across overlapping windows

for (const o of allOrders) {
  if (seenIds.has(o.id)) continue;
  seenIds.add(o.id);

  if (o.cancelled_at) continue;
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;

  // Use SGT month key
  const sgtDate = new Date(new Date(o.created_at).getTime() + SGT_OFFSET_MS);
  const key = sgtDate.toISOString().slice(0, 7);  // YYYY-MM
  const total = parseFloat(o.total_price || '0');

  const entry = byMonth.get(key) || { month: key, revenue: 0, orders: 0 };
  entry.revenue += total;
  entry.orders += 1;
  byMonth.set(key, entry);
}

const sorted = [...byMonth.values()].sort((a, b) => a.month.localeCompare(b.month));

// Also compute 6/3/1-month trailing averages
const recent12 = sorted.slice(-12);
const recent6  = sorted.slice(-6);
const recent3  = sorted.slice(-3);
const avg = arr => arr.length ? arr.reduce((s, m) => s + m.revenue, 0) / arr.length : 0;

return [{
  json: {
    total_orders_analyzed: allOrders.length,
    dedup_unique: seenIds.size,
    months: sorted.map(m => ({
      month: m.month,
      revenue: Math.round(m.revenue * 100) / 100,
      orders: m.orders,
      aov: m.orders ? Math.round((m.revenue / m.orders) * 100) / 100 : 0,
    })),
    avg_last_12: Math.round(avg(recent12) * 100) / 100,
    avg_last_6:  Math.round(avg(recent6)  * 100) / 100,
    avg_last_3:  Math.round(avg(recent3)  * 100) / 100,
  }
}];
"""


def uid():
    return str(uuid.uuid4())


def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def build():
    webhook = {
        "parameters": {
            "httpMethod": "POST",
            "path": WEBHOOK_ID,
            "responseMode": "onReceived",
            "options": {},
        },
        "id": uid(),
        "name": "Webhook Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 300],
        "webhookId": WEBHOOK_ID,
    }

    emit_ranges = {
        "parameters": {"jsCode": EMIT_RANGES_JS},
        "id": uid(),
        "name": "Emit 12 Monthly Ranges",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [240, 300],
    }

    # HTTP node fires ONCE per input item (default), so this fans out into 12 calls
    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}"
    fetch = {
        "parameters": {
            "url": (
                "=" + base + "/orders.json?status=any&financial_status=paid"
                "&created_at_min={{ $json.fetch_start }}"
                "&created_at_max={{ $json.fetch_end }}"
                "&limit=250&fields=id,cancelled_at,financial_status,created_at,total_price"
            ),
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": {},
        },
        "id": uid(),
        "name": "Fetch Month",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [480, 300],
        "credentials": {
            "shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}
        },
    }

    aggregate = {
        "parameters": {"jsCode": AGGREGATE_JS},
        "id": uid(),
        "name": "Aggregate Monthly",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [720, 300],
    }

    nodes = [webhook, emit_ranges, fetch, aggregate]
    connections = {
        webhook["name"]:      {"main": [[{"node": emit_ranges["name"], "type": "main", "index": 0}]]},
        emit_ranges["name"]:  {"main": [[{"node": fetch["name"],       "type": "main", "index": 0}]]},
        fetch["name"]:        {"main": [[{"node": aggregate["name"],   "type": "main", "index": 0}]]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def find_existing():
    status, data = http("GET", "/workflows?limit=250")
    if status >= 300:
        return None
    for wf in data.get("data", []):
        if wf.get("name") == WF_NAME:
            return wf["id"]
    return None


def main():
    existing_id = find_existing()
    payload = build()

    if existing_id:
        print(f"Updating existing: {existing_id}")
        status, body = http("PUT", f"/workflows/{existing_id}", payload)
        wf_id = existing_id
    else:
        status, body = http("POST", "/workflows", payload)
        wf_id = body.get("id") if isinstance(body, dict) else None
        print(f"Created: {wf_id}")

    # Transfer to team project (needed for shopify credential access)
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})

    # Activate
    s, _ = http("POST", f"/workflows/{wf_id}/activate")
    print(f"Activate HTTP {s}")

    # Fire webhook
    print("Firing webhook...")
    req = urllib.request.Request(
        f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_ID}",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        print("Webhook response:", r.status)

    print("Waiting 20s for 12 Shopify calls to complete...")
    time.sleep(20)

    # Pull execution
    s, data = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    ex = data["data"][0] if data.get("data") else None
    if not ex:
        print("No execution found")
        return
    print(f"Execution {ex['id']} | status={ex.get('status')}")

    run_data = (ex.get("data") or {}).get("resultData", {}).get("runData", {})
    for node, runs in run_data.items():
        for r in runs:
            err = r.get("error")
            if err:
                print(f"  ❌ {node}: {str(err.get('message'))[:200]}")
            else:
                items = (r.get("data") or {}).get("main", [[]])[0] or []
                print(f"  ✅ {node}: {len(items)} items")

    # Output aggregated result
    agg_runs = run_data.get("Aggregate Monthly", [])
    if agg_runs:
        items = (agg_runs[0].get("data") or {}).get("main", [[]])[0] or []
        if items:
            j = items[0].get("json", {})
            print("\n=== Monthly breakdown ===")
            for m in j.get("months", []):
                print(f"  {m['month']}: S${m['revenue']:>10,.2f} | {m['orders']:>4} orders | AOV S${m['aov']:.2f}")
            print(f"\nAvg last 12mo: S${j.get('avg_last_12'):,.2f}")
            print(f"Avg last 6mo:  S${j.get('avg_last_6'):,.2f}")
            print(f"Avg last 3mo:  S${j.get('avg_last_3'):,.2f}")
            print(f"Total orders analyzed (dedup): {j.get('dedup_unique')}")

            # Save raw JSON for later analysis
            out = os.path.expanduser("~/n8n-bonpet/growth_analysis_result.json")
            with open(out, "w") as f:
                json.dump(j, f, indent=2)
            print(f"\nSaved full result → {out}")


if __name__ == "__main__":
    main()

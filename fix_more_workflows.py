#!/usr/bin/env python3
"""Round 2 fixes: Weekly/Monthly Sales aggregate, Leads Funnel, Abandoned Cart OOM."""
import json, os, urllib.request, urllib.error, time

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def n8n(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            return res.status, json.loads(res.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def put_payload(wf):
    out = {"name": wf["name"], "nodes": wf["nodes"],
           "connections": wf["connections"],
           "settings": wf.get("settings") or {"executionOrder": "v1"}}
    if wf.get("staticData"):
        out["staticData"] = wf["staticData"]
    return out


# ─────────────────────────── Weekly/Monthly Sales fix ──────────────────────
def fix_sales_aggregate():
    print("\n=== Fix Weekly/Monthly Sales — replace $items() with $().first() ===")
    s, wf = n8n("GET", "/workflows/Sv1nluGjlEhLX8CV")
    if s >= 300:
        print("GET failed:", s); return False

    new_code = """// Pick whichever Set Range node ran in this execution
let ctx = null;
try { const m = $('Set Monthly Range').all(); if (m && m.length) ctx = m[0].json; } catch (e) {}
if (!ctx) { try { const w = $('Set Weekly Range').all(); if (w && w.length) ctx = w[0].json; } catch (e) {} }
if (!ctx) {
  // Last-resort default — should never hit
  ctx = { period: 'weekly', label: 'Weekly Report', period_label: 'This Week', prev_label: 'Last Week',
          start_date: new Date(Date.now() - 7*86400000).toISOString(),
          end_date: new Date().toISOString(),
          prev_start_date: new Date(Date.now() - 14*86400000).toISOString(),
          prev_end_date: new Date(Date.now() - 7*86400000).toISOString() };
}

const data = $input.first().json;
const orders = data.orders || [];

const curStart = new Date(ctx.start_date).getTime();
const curEnd = new Date(ctx.end_date).getTime();
const prevStart = new Date(ctx.prev_start_date).getTime();
const prevEnd = new Date(ctx.prev_end_date).getTime();

function agg(list) {
  let sales=0, subtotal=0, tax=0, items=0, currency='SGD';
  for (const o of list) {
    sales += parseFloat(o.total_price||0);
    subtotal += parseFloat(o.subtotal_price||0);
    tax += parseFloat(o.total_tax||0);
    currency = o.currency || currency;
    for (const li of (o.line_items||[])) items += (li.quantity||0);
  }
  const count = list.length;
  const aov = count ? (sales/count) : 0;
  return { order_count: count, total_sales: sales, total_subtotal: subtotal, total_tax: tax, total_items: items, aov: aov, currency: currency };
}

const curOrders = [];
const prevOrders = [];
for (const o of orders) {
  if (o.financial_status !== 'paid' && o.financial_status !== 'partially_refunded') continue;
  if (o.cancelled_at) continue;
  const t = new Date(o.created_at).getTime();
  if (t >= curStart && t <= curEnd) curOrders.push(o);
  else if (t >= prevStart && t <= prevEnd) prevOrders.push(o);
}

const cur = agg(curOrders);
const prev = agg(prevOrders);

function pct(curVal, prevVal) {
  if (prevVal === 0) return curVal > 0 ? 100 : 0;
  return ((curVal - prevVal) / prevVal) * 100;
}
function arrow(v) {
  if (v > 0.5) return '📈';
  if (v < -0.5) return '📉';
  return '➡️';
}
function fmtPct(v) {
  const sign = v > 0 ? '+' : '';
  return sign + v.toFixed(1) + '%';
}

const dSales  = pct(cur.total_sales, prev.total_sales);
const dOrders = pct(cur.order_count, prev.order_count);
const dItems  = pct(cur.total_items, prev.total_items);
const dAov    = pct(cur.aov, prev.aov);

return [{
  json: {
    period: ctx.period,
    label: ctx.label,
    period_label: ctx.period_label,
    prev_label: ctx.prev_label,
    currency: cur.currency,
    cur_order_count: cur.order_count,
    cur_total_sales: cur.total_sales.toFixed(2),
    cur_total_items: cur.total_items,
    cur_aov: cur.aov.toFixed(2),
    cur_total_subtotal: cur.total_subtotal.toFixed(2),
    cur_total_tax: cur.total_tax.toFixed(2),
    prev_order_count: prev.order_count,
    prev_total_sales: prev.total_sales.toFixed(2),
    prev_total_items: prev.total_items,
    prev_aov: prev.aov.toFixed(2),
    delta_sales_pct: fmtPct(dSales),
    delta_sales_arrow: arrow(dSales),
    delta_orders_pct: fmtPct(dOrders),
    delta_orders_arrow: arrow(dOrders),
    delta_items_pct: fmtPct(dItems),
    delta_items_arrow: arrow(dItems),
    delta_aov_pct: fmtPct(dAov),
    delta_aov_arrow: arrow(dAov),
  }
}];
"""

    patched = False
    for n in wf["nodes"]:
        if n.get("name") == "Aggregate Metrics":
            n["parameters"]["jsCode"] = new_code
            patched = True
            print("  patched Aggregate Metrics jsCode (replaced $items() with $().all())")
    if not patched:
        print("  Aggregate Metrics node not found"); return False

    s, _ = n8n("PUT", "/workflows/Sv1nluGjlEhLX8CV", put_payload(wf))
    print(f"  PUT → HTTP {s}")
    return s < 300


# ───────────────────── Leads Funnel — investigate then fix ──────────────────
def inspect_leads_funnel_error():
    print("\n=== Inspect Leads Funnel error (Send Free Trial Message) ===")
    s, data = n8n("GET", "/executions?workflowId=xl6MczAGNwcNEBuc&status=error&limit=2&includeData=true")
    if s >= 300:
        print("GET failed:", s); return
    if not isinstance(data, dict) or not data.get("data"):
        print("  no error executions found"); return
    for e in data["data"][:2]:
        print(f'\n--- {e.get("startedAt")} mode={e.get("mode")} ---')
        rd = e.get("data", {}).get("resultData", {})
        for n, runs in rd.get("runData", {}).items():
            for r in runs:
                if r.get("error"):
                    err = r["error"]
                    print(f'  >> {n}')
                    print(f'     name: {err.get("name")}')
                    print(f'     message: {err.get("message","")[:300]}')
                    print(f'     description: {err.get("description","")[:300]}')
                    desc = err.get('context', {}) or {}
                    print(f'     context: {json.dumps(desc)[:400]}')
                    if 'httpCode' in err:
                        print(f'     httpCode: {err.get("httpCode")}')
                    if 'request' in err:
                        print(f'     request: {json.dumps(err.get("request"))[:400]}')


# ───────────────────── Abandoned Cart OOM — investigate ─────────────────────
def inspect_abandoned_cart_oom():
    print("\n=== Inspect Abandoned Cart OOM (execution 1069) ===")
    s, data = n8n("GET", "/executions?workflowId=SxeOUWpesKXMYOxR&status=error&limit=3&includeData=true")
    if s >= 300:
        print("GET failed:", s); return
    items = data.get("data", []) if isinstance(data, dict) else []
    print(f'  {len(items)} error execs')
    for e in items[:3]:
        print(f'\n--- {e.get("startedAt")} stoppedAt={e.get("stoppedAt")} mode={e.get("mode")} ---')
        rd = e.get("data", {}).get("resultData", {})
        runs = rd.get("runData", {})
        print(f'  nodes ran ({len(runs)}): {list(runs.keys())}')
        # Look at output sizes
        for n, runlist in runs.items():
            for r in runlist:
                out = r.get("data", {}).get("main", [[]])
                if out and out[0]:
                    print(f'  {n}: {len(out[0])} items')
                if r.get("error"):
                    print(f'  >> {n} ERROR: {r["error"].get("message","")[:200]}')
        if "error" in rd:
            err = rd["error"]
            print(f'  TOP-LEVEL: {err.get("message","")[:300]}')


if __name__ == "__main__":
    import sys
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if only in ("all", "sales"):
        fix_sales_aggregate()
    if only in ("all", "leads"):
        inspect_leads_funnel_error()
    if only in ("all", "cart"):
        inspect_abandoned_cart_oom()

#!/usr/bin/env python3
"""Build the "Weekly & Monthly Sales Report" workflow and deploy it to n8n.

This workflow was originally authored in the n8n UI, so it had no builder and
every UI edit silently diverged from the repo. Captured from the live workflow
on 2026-08-02 and reproduced here.

Two triggers share one pipeline:
    Weekly Trigger (Mon 9AM)   -> Set Weekly Range  ─┐
    Monthly Trigger (1st 9AM)  -> Set Monthly Range ─┴> Get Shopify Orders
        -> Aggregate Metrics -> Format WhatsApp Message -> 5x WA + Telegram

Windows are ROLLING, not calendar: weekly = last 7 days vs the 7 before it,
monthly = last 30 days vs the 30 before. That differs from Customer Metrics and
Top Sellers, which use calendar Mon-Sun weeks. Left as-is deliberately.

Notes for anyone editing:
- Sales counted are ONLINE only (see _online_sales.py). Offline POS/expo sales
  are excluded so an expo weekend doesn't read as DTC growth.
- Shopify REST returns newest-first and caps a page at 250, so the monthly run
  (~60 days of orders) silently dropped the oldest orders without pagination.
- This report goes to the internal team only. Unlike the other reports it does
  NOT include the Launch Cycle agency numbers (Siva/Raghav) or the LaunchCycle
  Telegram group. Intentional - do not "fix" it without asking.
- The credential here is a DIFFERENT Shopify token id to the other builders,
  though both are named "Shopify Access Token n8n".

RETIRED 2026-08-02: deactivated in n8n. Every metric it reported (sales,
orders, AOV vs prior period) is already in the Morning Briefing's last-7d and
MTD-vs-prior-month blocks. Kept here so it can be revived with one PUT; note
the known bug that the Format node reads $json.start_date / $json.end_date,
which Aggregate Metrics never outputs, so the date line renders blank.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

from _online_sales import IS_ONLINE_JS

API = "https://n8n.thebonpet.com/api/v1"
WF_NAME = "Weekly & Monthly Sales Report - WhatsApp"
WF_ID = "Sv1nluGjlEhLX8CV"

ERROR_WORKFLOW_ID = "c3Vk2nt9WINzp9GH"

SHOPIFY_STORE = "d2ac44-d5"
SHOPIFY_API = "2024-10"
SHOPIFY_CRED_ID = "4d1xmXLJqGoPK6TX"
SHOPIFY_CRED_NAME = "Shopify Access Token n8n"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()

TELEGRAM_CHAT_ID = "-1002184573790"
TELEGRAM_THREAD_ID = "34253"
TELEGRAM_TOKEN = open(os.path.expanduser("~/.telegram-weslee-bot-token")).read().strip()

# Internal team only - no Launch Cycle. Node names keep the original numbering
# (there is no #5) so execution history stays comparable.
RECIPIENTS = [
    ("Send WhatsApp #1", "+6581394225",    [1504, 464]),   # Yash
    ("Send WhatsApp #2", "+6598531677",    [1680, 64]),    # Nicolas
    ("Send WhatsApp #3", "+6590108515",    [1760, 288]),   # Bon Pet official
    ("Send WhatsApp #4", "+6587993341",    [1648, 608]),   # Rachel
    ("Send WhatsApp #6", "+6282240119788", [1648, 808]),   # Bari (CS agent, ID)
]

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

AGGREGATE_JS = r"""// Pick whichever Set Range node ran in this execution
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

const orders = $input.all().flatMap(it => it.json.orders || []).filter(o => o && o.id);

const curStart = new Date(ctx.start_date).getTime();
const curEnd = new Date(ctx.end_date).getTime();
const prevStart = new Date(ctx.prev_start_date).getTime();
const prevEnd = new Date(ctx.prev_end_date).getTime();

__IS_ONLINE_JS__
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
  if (!isOnlineOrder(o)) continue;
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
""".replace("__IS_ONLINE_JS__", IS_ONLINE_JS)

# Brand rule: no em-dashes or en-dashes anywhere in customer/team copy. The live
# version carried an en-dash in the title; it is a plain hyphen here.
WA_MESSAGE = (
    "=📊 *The Bon Pet - {{ $json.label }}*\n"
    "🗓️ {{ $json.start_date.substring(0,10) }} → {{ $json.end_date.substring(0,10) }}\n\n"
    "*{{ $json.period_label }}* _(vs {{ $json.prev_label }})_\n\n"
    "💰 Sales: {{ $json.currency }} {{ $json.cur_total_sales }}\n"
    "   {{ $json.delta_sales_arrow }} {{ $json.delta_sales_pct }} _(prev: {{ $json.currency }} {{ $json.prev_total_sales }})_\n\n"
    "🧾 Orders: {{ $json.cur_order_count }}\n"
    "   {{ $json.delta_orders_arrow }} {{ $json.delta_orders_pct }} _(prev: {{ $json.prev_order_count }})_\n\n"
    "📦 Items: {{ $json.cur_total_items }}\n"
    "   {{ $json.delta_items_arrow }} {{ $json.delta_items_pct }} _(prev: {{ $json.prev_total_items }})_\n\n"
    "📈 AOV: {{ $json.currency }} {{ $json.cur_aov }}\n"
    "   {{ $json.delta_aov_arrow }} {{ $json.delta_aov_pct }} _(prev: {{ $json.currency }} {{ $json.prev_aov }})_\n\n"
    "_Online sales only. POS/event sales are excluded._"
)


def uid():
    return str(uuid.uuid4())


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


def schedule_node(name, cron, pos):
    return {
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": cron}]}},
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": pos,
    }


def range_node(name, pos, period, days, label, period_label, prev_label):
    def a(i, n, v, t):
        return {"id": str(i), "name": n, "value": v, "type": t}
    return {
        "parameters": {
            "assignments": {"assignments": [
                a(1,  "period",          period, "string"),
                a(2,  "days",            days,   "number"),
                a(3,  "start_date",      f"={{{{ $now.minus({{ days: {days} }}).toISO() }}}}", "string"),
                a(4,  "end_date",        "={{ $now.toISO() }}", "string"),
                a(5,  "prev_start_date", f"={{{{ $now.minus({{ days: {days * 2} }}).toISO() }}}}", "string"),
                a(6,  "prev_end_date",   f"={{{{ $now.minus({{ days: {days} }}).toISO() }}}}", "string"),
                a(7,  "fetch_start",     f"={{{{ $now.minus({{ days: {days * 2} }}).toISO() }}}}", "string"),
                a(8,  "fetch_end",       "={{ $now.toISO() }}", "string"),
                a(9,  "label",           label, "string"),
                a(10, "period_label",    period_label, "string"),
                a(11, "prev_label",      prev_label, "string"),
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.set", "typeVersion": 3.4,
        "position": pos,
    }


def wa_node(name, phone, pos):
    return {
        "parameters": {
            "method": "POST",
            "url": WA_URL,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-API-Key", "value": WA_KEY},
            ]},
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "phone_number", "value": phone},
                {"name": "message", "value": "={{ $json.message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def build():
    weekly_trigger  = schedule_node("Weekly Trigger (Mon 9AM)",  "0 9 * * 1", [592, 0])
    monthly_trigger = schedule_node("Monthly Trigger (1st 9AM)", "0 9 1 * *", [592, 304])

    weekly_range  = range_node("Set Weekly Range",  [816, 0],
                               "weekly", 7, "Weekly Report", "This Week", "Last Week")
    monthly_range = range_node("Set Monthly Range", [816, 304],
                               "monthly", 30, "Monthly Report", "This Month", "Last Month")

    fetch = {
        "parameters": {
            "url": (
                f"=https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API}/orders.json"
                "?status=any&financial_status=paid"
                "&created_at_min={{ $json.fetch_start }}"
                "&created_at_max={{ $json.fetch_end }}"
                "&limit=250&fields=id,total_price,subtotal_price,total_tax,currency,"
                "financial_status,created_at,line_items,cancelled_at,source_name"
            ),
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "shopifyAccessTokenApi",
            "options": SHOPIFY_PAGINATION,
        },
        "id": uid(), "name": "Get Shopify Orders",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1040, 160],
        "credentials": {"shopifyAccessTokenApi": {"id": SHOPIFY_CRED_ID, "name": SHOPIFY_CRED_NAME}},
    }

    aggregate = {
        "parameters": {"jsCode": AGGREGATE_JS},
        "id": uid(), "name": "Aggregate Metrics",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [1264, 160],
    }

    format_msg = {
        "parameters": {
            "assignments": {"assignments": [
                {"id": "1", "name": "message", "value": WA_MESSAGE, "type": "string"},
                {"id": "2", "name": "phone_number", "value": "+6581394225", "type": "string"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Format WhatsApp Message",
        "type": "n8n-nodes-base.set", "typeVersion": 3.4,
        "position": [1472, 160],
    }

    wa_sends = [wa_node(n, p, pos) for n, p, pos in RECIPIENTS]

    telegram = {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "chat_id", "value": TELEGRAM_CHAT_ID},
                {"name": "message_thread_id", "value": TELEGRAM_THREAD_ID},
                {"name": "text", "value": "={{ $json.message }}"},
                {"name": "parse_mode", "value": "Markdown"},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Send Telegram Weslee",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [1700, 800],
    }

    fanout = [{"node": n["name"], "type": "main", "index": 0} for n in (*wa_sends, telegram)]

    nodes = [weekly_trigger, monthly_trigger, weekly_range, monthly_range,
             fetch, aggregate, format_msg, *wa_sends, telegram]

    connections = {
        weekly_trigger["name"]:  {"main": [[{"node": weekly_range["name"],  "type": "main", "index": 0}]]},
        monthly_trigger["name"]: {"main": [[{"node": monthly_range["name"], "type": "main", "index": 0}]]},
        weekly_range["name"]:    {"main": [[{"node": fetch["name"], "type": "main", "index": 0}]]},
        monthly_range["name"]:   {"main": [[{"node": fetch["name"], "type": "main", "index": 0}]]},
        fetch["name"]:           {"main": [[{"node": aggregate["name"], "type": "main", "index": 0}]]},
        aggregate["name"]:       {"main": [[{"node": format_msg["name"], "type": "main", "index": 0}]]},
        format_msg["name"]:      {"main": [fanout]},
    }

    return {
        "name": WF_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "errorWorkflow": ERROR_WORKFLOW_ID},
    }


if __name__ == "__main__":
    payload = build()
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT {WF_ID} -> HTTP {status} ({len(payload['nodes'])} nodes)")
    if status >= 300:
        raise SystemExit(str(body)[:600])

#!/usr/bin/env python3
"""Meal Plan Follow-up — did the D3/D7 nudges actually sell anything?

Two independent attributions, because each misses something the other catches:

  COHORT  — everyone wa_sent_log says we messaged (workflow=meal_plan_followup),
            cross-checked against Shopify orders placed after their send. Catches
            the pawrent who ignored the link and ordered days later off their own
            bat. This is the number that matters.
  UTM     — orders whose landing_site carries utm_campaign=meal-plan-followup.
            Only catches link-clickers, but it splits D3 vs D7 cleanly and proves
            which message did the work.

Also reports the parent campaign (meal-plan-ready, the original plan message) as
a baseline, so "did the follow-ups add anything" has something to compare against.

Run bare for a terminal report, --telegram to also post to the weslee thread.
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

SHOP = "d2ac44-d5.myshopify.com"
API_VERSION = "2025-01"
N8N_API = "https://n8n.thebonpet.com/api/v1"
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
WA_SENT_LOG_GID = 700800
WORKFLOW_TAG = "meal_plan_followup"
FOLLOWUP_WF_ID = "NJ3EctLBcHhaWV3I"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def keychain(service):
    return subprocess.check_output(
        ["security", "find-generic-password", "-s", service, "-w"]).decode().strip()


def get_json(url, headers):
    req = urllib.request.Request(url, headers={**headers, "User-Agent": UA})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def norm_phone(p):
    s = re.sub(r"\s", "", str(p or "")).strip()
    if s.startswith("+"):
        return "+" + re.sub(r"\D", "", s[1:])
    d = re.sub(r"\D", "", s)
    if len(d) == 8 and d[0] in "689":
        return "+65" + d
    if len(d) == 10 and d.startswith("65"):
        return "+" + d
    return "+" + d if 8 <= len(d) <= 15 else ""


def parse_dt(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_sends():
    """Every (phone, template, sent_at) this workflow has actually delivered.

    Source is the n8n execution history rather than the wa_sent_log sheet: the
    sheet lives on a Google account this machine holds no credential for, and
    the "Skip Header" node output is the definitive post-send list anyway (it
    sits downstream of Send WA, so it only contains messages that went out).
    """
    key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    hdr = {"X-N8N-API-KEY": key}
    execs = get_json(f"{N8N_API}/executions?workflowId={FOLLOWUP_WF_ID}&limit=100", hdr)
    sends = {}
    for e in execs.get("data", []):
        d = get_json(f"{N8N_API}/executions/{e['id']}?includeData=true", hdr)
        rd = d.get("data", {}).get("resultData", {}).get("runData", {})
        node = rd.get("Skip Header")
        if not node:
            continue
        try:
            rows = [i["json"] for i in node[0]["data"]["main"][0]]
        except Exception:
            continue
        for r in rows:
            p = norm_phone(r.get("phone"))
            t = parse_dt(r.get("sent_at"))
            if not p or not t:
                continue
            k = (p, str(r.get("template") or "").strip())
            # email is absent on the 2026-07-29 backfill sends, present after.
            if k not in sends or t < sends[k][0]:
                sends[k] = (t, str(r.get("email") or "").lower().strip())
    return sends


def fetch_orders(since):
    token = keychain("shopify-bonpet-admin-token")
    fields = "id,name,created_at,email,phone,landing_site,total_price,customer"
    url = (f"https://{SHOP}/admin/api/{API_VERSION}/orders.json?status=any&limit=250"
           f"&created_at_min={since.isoformat()}&fields={fields}")
    out, seen = [], set()
    while url:
        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token, "User-Agent": UA})
        with urllib.request.urlopen(req) as r:
            out.extend(json.loads(r.read().decode()).get("orders", []))
            link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m and m.group(1) not in seen else None
        if url:
            seen.add(url)
    return out


def main():
    sends = fetch_sends()
    if not sends:
        print("No meal_plan_followup sends found in wa_sent_log yet.")
        return

    first_send = min(t for t, _ in sends.values())
    orders = fetch_orders(first_send - timedelta(days=1))

    # --- cohort attribution ---
    by_phone, by_email = {}, {}
    for o in orders:
        t = parse_dt(o.get("created_at"))
        if not t:
            continue
        cust = o.get("customer") or {}
        for p in {norm_phone(o.get("phone")), norm_phone(cust.get("phone"))}:
            if p:
                by_phone.setdefault(p, []).append((t, o))
        for e in {str(o.get("email") or "").lower().strip(),
                  str(cust.get("email") or "").lower().strip()}:
            if e:
                by_email.setdefault(e, []).append((t, o))

    per_phone = {}
    for (phone, tmpl), (sent, email) in sends.items():
        cur = per_phone.get(phone)
        if not cur or sent < cur[1]:
            per_phone[phone] = (tmpl, sent, email)

    converted, revenue = {}, 0.0
    for phone, (tmpl, sent, email) in per_phone.items():
        hits = [(t, o) for t, o in by_phone.get(phone, []) if t >= sent]
        hits += [(t, o) for t, o in by_email.get(email, []) if email and t >= sent]
        if hits:
            t, o = min(hits)
            converted[phone] = (tmpl, o)
            revenue += float(o.get("total_price") or 0)

    # --- utm attribution ---
    utm = {}
    for o in orders:
        ls = str(o.get("landing_site") or "")
        m = re.search(r"utm_campaign=([^&]+)", ls)
        if not m:
            continue
        camp = urllib.parse.unquote(m.group(1))
        c = re.search(r"utm_content=([^&]+)", ls)
        utm.setdefault(camp, []).append((o.get("name"), c.group(1) if c else "-",
                                         o.get("total_price")))

    d3 = sum(1 for v in per_phone.values() if v[0] == "D3")
    d7 = sum(1 for v in per_phone.values() if v[0] == "D7")
    cd3 = sum(1 for t, _ in converted.values() if t == "D3")
    cd7 = sum(1 for t, _ in converted.values() if t == "D7")
    rate = lambda a, b: f"{(100.0*a/b):.1f}%" if b else "n/a"

    L = [
        "🍽️ *Meal Plan Follow-up — results*",
        f"📅 first send {first_send.astimezone().strftime('%Y-%m-%d')}"
        f" → {datetime.now().astimezone().strftime('%Y-%m-%d')}",
        "",
        "*Cohort (messaged → ordered after)*",
        f"• Messaged: {len(per_phone)}  (D3 {d3} · D7 {d7})",
        f"• Ordered:  {len(converted)}  ({rate(len(converted), len(per_phone))})",
        f"   – via D3: {cd3} / {d3}  ({rate(cd3, d3)})",
        f"   – via D7: {cd7} / {d7}  ({rate(cd7, d7)})",
        f"• Revenue:  ${revenue:.2f}",
        "",
        "*UTM-attributed orders*",
    ]
    if utm:
        for camp, rows in sorted(utm.items(), key=lambda kv: -len(kv[1])):
            tag = "  ← follow-up" if camp == "meal-plan-followup" else ""
            L.append(f"• {camp}: {len(rows)}{tag}")
            for name, content, price in rows[:6]:
                L.append(f"   {name} · {content} · ${price}")
    else:
        L.append("• none")
    if converted:
        L += ["", "*Who converted*"]
        for phone, (tmpl, o) in sorted(converted.items(), key=lambda kv: kv[1][1]["created_at"]):
            L.append(f"• {tmpl} {phone} → {o.get('name')} ${o.get('total_price')}")

    msg = "\n".join(L)
    print(msg)

    if "--telegram" in sys.argv:
        token = open(os.path.expanduser("~/.telegram-weslee-bot-token")).read().strip()
        body = json.dumps({"chat_id": "-1002184573790", "message_thread_id": 34253,
                           "text": msg, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req) as r:
                print("\ntelegram:", r.status)
        except urllib.error.HTTPError as e:
            print("\ntelegram FAILED:", e.code, e.read().decode()[:200])


if __name__ == "__main__":
    main()

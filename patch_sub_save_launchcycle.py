#!/usr/bin/env python3
"""Add Launch Cycle (external agency) to Subscription Save alerts — REDACTED (2026-08-03).

Patches the LIVE `Subscription Save - WhatsApp` workflow (aHp12XVEld1s1ZBP).
build_subscription_save.py has drifted and must NOT be re-run — live JSON is the
source of truth (same reason as patch_sub_notify_all_events.py).

Launch Cycle are external, so they get a PII-light variant: first name + last
initial, no email, no phone. Everything they need for churn analysis, nothing
they have no reason to action. Internal team WA + weslee are unchanged.

Wired off the existing "Team WA?" gate, so LC inherits its rules for free:
fires for PAUSED/CANCELLED/BILLING_FAILED only (not routine ACTIVE updates),
and stays silent while DRY_NEW_EVENTS is true.
"""
import json, os, subprocess, urllib.request, urllib.error, uuid

API = "https://n8n.thebonpet.com/api/v1"
WF_ID = "aHp12XVEld1s1ZBP"

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wa-api-key", "-w"]
).decode().strip()

LAUNCHCYCLE = [("Siva", "+6583513308"), ("Raghav", "+6588146498")]

# Redacted message, built alongside weslee_message. Deliberately omits email and
# phone; keeps product/cadence, billing date, lifetime value and contract ID.
LC_MESSAGE_JS = """
const lastInitial = lastName ? ` ${String(lastName).trim().charAt(0)}.` : '';
const lc_message =
  `${headline}\\n` +
  `\\u{1F464} ${firstName}${lastInitial}\\n` +
  `\\u{1F4E6} ${protein}${qty ? ' x ' + qty : ''}${cadence ? ', every ' + cadence : ''}\\n` +
  `\\u{1F4C5} ${billLabel}: ${nextBill}\\n` +
  `\\u{1F4B0} Lifetime: ${totalOrders} orders, S$${totalSpent.toFixed(2)}\\n` +
  `\\u{1F194} Contract: ${contractId}`;
"""


def uid():
    return str(uuid.uuid4())


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


def lc_wa_node(name, pos, phone):
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
                {"name": "message", "value": "={{ $json.lc_message }}"},
            ]},
            "options": {},
        },
        "id": uid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
    }


def main():
    status, wf = http("GET", f"/workflows/{WF_ID}")
    if status >= 300:
        raise SystemExit(f"fetch failed: {status} {wf}")

    nodes = wf["nodes"]
    conns = wf["connections"]

    fmt = next(n for n in nodes if n["name"] == "Lookup + Format")
    js = fmt["parameters"]["jsCode"]

    if "lc_message" not in js:
        anchor = "// Channel gates."
        if anchor not in js:
            raise SystemExit("anchor '// Channel gates.' not found — inspect live code first")
        js = js.replace(anchor, LC_MESSAGE_JS.strip() + "\n\n" + anchor, 1)
        js = js.replace(
            "    weslee_message: weslee_message,",
            "    weslee_message: weslee_message,\n    lc_message: lc_message,",
            1,
        )
        fmt["parameters"]["jsCode"] = js
        print("✅ Lookup + Format: added redacted lc_message")
    else:
        print("↩️  lc_message already present — code untouched")

    existing = {n["name"] for n in nodes}
    gate = conns["Team WA?"]["main"][0]
    for i, (who, phone) in enumerate(LAUNCHCYCLE):
        name = f"LC WA {who}"
        if name in existing:
            print(f"↩️  {name} already exists")
            continue
        nodes.append(lc_wa_node(name, [1200, 1150 + i * 100], phone))
        gate.append({"node": name, "type": "main", "index": 0})
        print(f"✅ added {name} ({phone}) off the Team WA? gate")

    payload = {
        "name": wf["name"],
        "nodes": nodes,
        "connections": conns,
        "settings": wf.get("settings", {"executionOrder": "v1"}),
    }
    s2, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT /workflows/{WF_ID} → {s2}")
    if s2 >= 300:
        raise SystemExit(str(body)[:800])
    s3, _ = http("POST", f"/workflows/{WF_ID}/activate")
    print(f"activate → {s3}")


if __name__ == "__main__":
    main()

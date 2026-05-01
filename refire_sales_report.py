#!/usr/bin/env python3
"""Add temp manual webhook to Weekly/Monthly Sales workflow, fire it for monthly, then remove the temp trigger."""
import json, os, urllib.request, urllib.error, time

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
WF_ID = "Sv1nluGjlEhLX8CV"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TMP_PATH = "tmp-sales-monthly-refire-9k4e"
TMP_NODE_NAME = "_TempManualMonthly"


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


def remove_temp(wf):
    wf["nodes"] = [n for n in wf["nodes"] if n.get("name") != TMP_NODE_NAME]
    wf["connections"] = {k: v for k, v in wf["connections"].items() if k != TMP_NODE_NAME}


# Step 1: GET, add temp trigger feeding Set Monthly Range
print("Step 1: add temp manual webhook to Sales workflow")
s, wf = n8n("GET", f"/workflows/{WF_ID}")
if s >= 300: raise SystemExit(f"GET failed: {s} {wf}")

remove_temp(wf)  # idempotent
wf["nodes"].append({
    "parameters": {
        "httpMethod": "POST", "path": TMP_PATH,
        "responseMode": "onReceived", "options": {},
    },
    "id": "55555555-5555-5555-5555-555555555555",
    "name": TMP_NODE_NAME,
    "type": "n8n-nodes-base.webhook",
    "typeVersion": 2,
    "position": [-200, 0],
    "webhookId": TMP_PATH,
})
wf["connections"][TMP_NODE_NAME] = {
    "main": [[{"node": "Set Monthly Range", "type": "main", "index": 0}]]
}

s, body = n8n("PUT", f"/workflows/{WF_ID}", put_payload(wf))
print(f"  PUT add-temp → HTTP {s}")
if s >= 300: raise SystemExit(body)

# Step 2: Fire it
print("Step 2: trigger via webhook")
trig = urllib.request.Request(
    f"https://n8n.thebonpet.com/webhook/{TMP_PATH}",
    data=b'{}', method="POST",
    headers={"Content-Type": "application/json", "User-Agent": UA},
)
try:
    with urllib.request.urlopen(trig, timeout=60) as r:
        print(f"  HTTP {r.status}: {r.read().decode()[:200]}")
except urllib.error.HTTPError as e:
    print(f"  trigger error {e.code}: {e.read().decode()[:300]}")

print("  waiting 12s for run to complete...")
time.sleep(12)

# Step 3: Check execution result
print("Step 3: verify")
s, data = n8n("GET", f"/executions?workflowId={WF_ID}&limit=1&includeData=true")
if s < 300:
    e = data["data"][0]
    print(f'  startedAt: {e.get("startedAt")}')
    print(f'  finished: {e.get("finished")}')
    rd = e.get("data",{}).get("resultData",{})
    print(f'  lastNode: {rd.get("lastNodeExecuted")}')
    runs = rd.get("runData",{})
    if "Aggregate Metrics" in runs:
        agg = runs["Aggregate Metrics"][0]
        out = agg.get("data",{}).get("main",[[]])
        if out and out[0]:
            j = out[0][0].get("json",{})
            print(f'\\n  *** Aggregate output ***')
            print(f'  period: {j.get("period")}, label: {j.get("label")}')
            print(f'  cur_orders: {j.get("cur_order_count")}  cur_sales: {j.get("currency")} {j.get("cur_total_sales")}')
            print(f'  prev_orders: {j.get("prev_order_count")}  prev_sales: {j.get("currency")} {j.get("prev_total_sales")}')
    if "Format WhatsApp Message" in runs:
        msg_node = runs["Format WhatsApp Message"][0]
        out = msg_node.get("data",{}).get("main",[[]])
        if out and out[0]:
            print(f'\\n  message preview:')
            print('  ' + (out[0][0].get('json',{}).get('message','')[:600].replace('\\n','\\n  ')))

# Step 4: Remove temp trigger
print("\\nStep 4: remove temp trigger")
s, wf = n8n("GET", f"/workflows/{WF_ID}")
if s < 300:
    remove_temp(wf)
    s2, _ = n8n("PUT", f"/workflows/{WF_ID}", put_payload(wf))
    print(f"  PUT cleanup → HTTP {s2}")

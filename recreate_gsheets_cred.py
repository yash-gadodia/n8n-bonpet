#!/usr/bin/env python3
"""Re-create 'Google Sheets account' cred fresh + relink all workflow nodes that reference it."""
import json, os, subprocess
from urllib import request, error

NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
CID = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","google-oauth-n8n-clientid","-w"], text=True).strip()
CSECRET = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","google-oauth-n8n-clientsecret","-w"], text=True).strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def call(p, m="GET", b=None):
    r = request.Request(f"https://n8n.thebonpet.com{p}", data=json.dumps(b).encode() if b else None, method=m)
    r.add_header("X-N8N-API-KEY", NEW_KEY); r.add_header("User-Agent", UA); r.add_header("accept","application/json")
    if b: r.add_header("content-type", "application/json")
    try:
        with request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

# 1. Create fresh cred
code, resp = call("/api/v1/credentials", "POST", {
    "name": "Google Sheets account",
    "type": "googleSheetsOAuth2Api",
    "data": {"clientId": CID, "clientSecret": CSECRET},
})
new_id = resp.get("id")
print(f"Created Google Sheets account: {new_id}\n")

# 2. Walk all workflows, relink any node that references "Google Sheets account" by name
ALLOWED = {"executionOrder","errorWorkflow","timezone","saveExecutionProgress","saveManualExecutions","saveDataErrorExecution","saveDataSuccessExecution","executionTimeout"}

_, data = call("/api/v1/workflows?limit=250")
patched = 0
for w in data["data"]:
    wid = w["id"]
    was_active = w.get("active", False)
    _, full = call(f"/api/v1/workflows/{wid}")
    changed = 0
    for node in full.get("nodes", []):
        for ct, cr in (node.get("credentials") or {}).items():
            if isinstance(cr, dict) and cr.get("name") == "Google Sheets account":
                if cr.get("id") != new_id:
                    cr["id"] = new_id
                    changed += 1
    if not changed: continue
    if was_active: call(f"/api/v1/workflows/{wid}/deactivate", "POST")
    payload = {
        "name": full["name"], "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k:v for k,v in (full.get("settings") or {}).items() if k in ALLOWED} or {"executionOrder":"v1"}
    }
    if full.get("staticData"): payload["staticData"] = full["staticData"]
    pc, pr = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    if pc in (200,201):
        patched += 1
        print(f"  PATCH  {full['name']}  ({changed} refs)")
        if was_active: call(f"/api/v1/workflows/{wid}/activate", "POST")
    else:
        print(f"  FAIL   {full['name']}: {pr.get('message',pr)[:100]}")
        if was_active: call(f"/api/v1/workflows/{wid}/activate", "POST")

print(f"\nRelinked {patched} workflows. New cred id: {new_id}")
print("Now in n8n UI: open the cred and click Sign in with Google.")

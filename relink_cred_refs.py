#!/usr/bin/env python3
"""Rewrite credential references in migrated workflows to point at new instance's cred IDs."""
import json, os
from urllib import request, error

NEW_HOST = "https://n8n.thebonpet.com"
NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

CRED_NAME_TO_ID = {
    "Google Sheets account":           "BdwcUCERhVxln6O9",
    "GSheets — BonPet Leads":          "mNgHiVbk9aDPAc5R",
    "Google Sheets Trigger account 2": "wDUuHNH6o4CJlD0Y",
    "Gmail account":                   "FD8gO3Ky14wEtczl",
    "Shopify Access Token n8n":        "4d1xmXLJqGoPK6TX",
    "Shopify Access Token account TBP":"Np4lXXVpDIGyzktW",
}

def call(path, method="GET", body=None):
    req = request.Request(f"{NEW_HOST}{path}", data=json.dumps(body).encode() if body else None, method=method)
    req.add_header("X-N8N-API-KEY", NEW_KEY)
    req.add_header("User-Agent", UA)
    req.add_header("accept", "application/json")
    if body:
        req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

ALLOWED_SETTINGS = {"executionOrder", "errorWorkflow", "timezone",
                    "saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution", "executionTimeout"}

_, data = call("/api/v1/workflows?limit=250")
wfs = data.get("data", [])
print(f"Processing {len(wfs)} workflows...\n")

patched = unchanged = failed = 0

for w in wfs:
    wid = w["id"]
    was_active = w.get("active", False)
    code, full = call(f"/api/v1/workflows/{wid}")
    if code != 200:
        failed += 1
        continue

    changed = []
    for node in full.get("nodes", []):
        if not node.get("credentials"):
            continue
        for cred_type, cref in list(node["credentials"].items()):
            if not isinstance(cref, dict):
                continue
            cname = cref.get("name")
            old_id = cref.get("id")
            new_id = CRED_NAME_TO_ID.get(cname)
            if new_id and new_id != old_id:
                cref["id"] = new_id
                changed.append(f"{node.get('name')}/{cred_type}")

    if not changed:
        unchanged += 1
        continue

    if was_active:
        call(f"/api/v1/workflows/{wid}/deactivate", "POST")

    payload = {
        "name": full["name"],
        "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k: v for k, v in (full.get("settings") or {}).items() if k in ALLOWED_SETTINGS} or {"executionOrder": "v1"},
    }
    if full.get("staticData"):
        payload["staticData"] = full["staticData"]

    code, resp = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    if code in (200, 201):
        patched += 1
        print(f"  PATCH  {full['name']}  ({len(changed)} refs)")
        if was_active:
            ac, ar = call(f"/api/v1/workflows/{wid}/activate", "POST")
            if ac not in (200, 201):
                print(f"    REACT-FAIL: {ar.get('message', ar)[:120]}")
    else:
        failed += 1
        print(f"  FAIL   {full['name']}: {resp.get('message', resp)}")
        if was_active:
            call(f"/api/v1/workflows/{wid}/activate", "POST")

print(f"\nResult: {patched} patched, {unchanged} unchanged, {failed} failed")

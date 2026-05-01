#!/usr/bin/env python3
"""Patch Review Watcher's 'Decide Action' Code node — substitute __NEG__/__POS__/__PROMO__ tokens."""
import json, os
from urllib import request, error

NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
NEG = "2"
POS = "4"
PROMO = "THANKYOU<3THEBONPET"

def call(p, m="GET", b=None):
    r = request.Request(f"https://n8n.thebonpet.com{p}", data=json.dumps(b).encode() if b else None, method=m)
    r.add_header("X-N8N-API-KEY", NEW_KEY); r.add_header("User-Agent", "Mozilla/5.0"); r.add_header("accept","application/json")
    if b: r.add_header("content-type", "application/json")
    try:
        with request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

# Find Review Watcher
_, data = call("/api/v1/workflows?limit=250")
for w in data["data"]:
    if w["name"] != "Review Watcher - WhatsApp": continue
    wid = w["id"]
    was_active = w.get("active", False)
    _, full = call(f"/api/v1/workflows/{wid}")
    patched = 0
    for n in full["nodes"]:
        if n["type"] != "n8n-nodes-base.code": continue
        params = n.get("parameters", {}) or {}
        code = params.get("jsCode", "")
        if "__NEG__" not in code: continue
        new_code = code.replace("__NEG__", NEG).replace("__POS__", POS).replace("__PROMO__", PROMO)
        params["jsCode"] = new_code
        n["parameters"] = params
        patched += 1
        print(f"  Patched Code node: {n['name']}")
    if not patched:
        print("  No __NEG__ tokens found.")
        break
    # Deactivate, PUT, reactivate
    if was_active: call(f"/api/v1/workflows/{wid}/deactivate", "POST")
    payload = {
        "name": full["name"], "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k: v for k, v in (full.get("settings") or {}).items() if k in ("executionOrder","errorWorkflow","timezone")} or {"executionOrder":"v1"}
    }
    if full.get("staticData"): payload["staticData"] = full["staticData"]
    code, resp = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    print(f"  PUT result: {code}")
    if was_active:
        call(f"/api/v1/workflows/{wid}/activate", "POST")
        print("  Reactivated.")
    break

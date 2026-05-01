#!/usr/bin/env python3
"""Fix the broken sent_log pattern across all WA broadcast workflows.
Old cloud n8n preserved input fields after HTTP Request; self-hosted (older) doesn't.
Fix: make Skip Header / Drop Header pull from the compute node directly (bypass HTTP response)."""
import json, os
from urllib import request, error
NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Workflow name → (skip-node name, compute-src node name)
FIXES = {
    "Dog Run Invite - WhatsApp (one-off)": ("Drop Header", "Format Message"),
    "Post-Trial Nurture — WhatsApp 7/14/21": ("Skip Header", "Compute Trial Candidates (D7/D14/D21)"),
    "Reorder Reminder - WhatsApp": ("Skip Header", "Compute Reorder Candidates"),
    "Win-back - WhatsApp": ("Drop Header", "Format Message"),
}

ALLOWED = {"executionOrder","errorWorkflow","timezone","saveExecutionProgress","saveManualExecutions","saveDataErrorExecution","saveDataSuccessExecution","executionTimeout"}

def call(p, m="GET", b=None):
    r = request.Request(f"https://n8n.thebonpet.com{p}", data=json.dumps(b).encode() if b else None, method=m)
    r.add_header("X-N8N-API-KEY", NEW_KEY); r.add_header("User-Agent", UA); r.add_header("accept","application/json")
    if b: r.add_header("content-type","application/json")
    try:
        with request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

_, data = call("/api/v1/workflows?limit=250")
for w in data["data"]:
    if w["name"] not in FIXES: continue
    skip_name, compute_name = FIXES[w["name"]]
    wid = w["id"]
    was_active = w.get("active", False)
    _, full = call(f"/api/v1/workflows/{wid}")
    patched = False
    for n in full.get("nodes", []):
        if n.get("name") != skip_name: continue
        if n.get("type") != "n8n-nodes-base.code": continue
        new_code = (
            "// Drop the diagnostic header item — log only real customer sends.\n"
            "// Pull from compute node directly so log keeps phone/first_name/etc instead of just HTTP response.\n"
            f"return $({json.dumps(compute_name)}).all().filter(it => !it.json.is_header);\n"
        )
        n["parameters"]["jsCode"] = new_code
        patched = True
        break
    if not patched:
        print(f"  SKIP  {w['name']} — node '{skip_name}' not found")
        continue
    if was_active: call(f"/api/v1/workflows/{wid}/deactivate", "POST")
    payload = {
        "name": full["name"], "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k:v for k,v in (full.get("settings") or {}).items() if k in ALLOWED} or {"executionOrder":"v1"}
    }
    if full.get("staticData"): payload["staticData"] = full["staticData"]
    code, resp = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    print(f"  {'OK ' if code in (200,201) else 'FAIL'}  {w['name']}: {skip_name} now reads from {compute_name!r}")
    # do NOT reactivate yet — user wants to verify first
print("\nAll patched workflows are LEFT INACTIVE for verification.")

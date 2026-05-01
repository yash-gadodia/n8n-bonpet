#!/usr/bin/env python3
"""Patch typeVersions on migrated workflows down to what the new instance supports.
Runs through every workflow on new n8n, walks nodes, downgrades any typeVersion above
the registered max for that node type. PUTs the modified workflow back."""
import json, os
from urllib import request, error

NEW_HOST = "https://n8n.thebonpet.com"
NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

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

# Build node type -> max supported version map
print("Loading node type registry from new instance...")
_, nodes_meta = call("/types/nodes.json")  # this returns a list when success
# /types/nodes.json doesn't go through /api/v1, hit directly
_resp = request.Request(f"{NEW_HOST}/types/nodes.json")
_resp.add_header("X-N8N-API-KEY", NEW_KEY)
_resp.add_header("User-Agent", UA)
nodes_list = json.loads(request.urlopen(_resp).read())
new_max = {}
for n in nodes_list:
    name = n.get("name")
    if not name: continue
    versions = n.get("version")
    vs = []
    if isinstance(versions, list):
        vs = [float(v) for v in versions]
    elif versions is not None:
        vs = [float(versions)]
    if name not in new_max:
        new_max[name] = max(vs) if vs else 0
    else:
        new_max[name] = max(new_max[name], max(vs) if vs else 0)

print(f"  loaded {len(new_max)} node types")

# Walk workflows on new
_, data = call("/api/v1/workflows?limit=250")
wfs = data.get("data", [])
print(f"\nProcessing {len(wfs)} workflows on new instance...")

patched_count = 0
skipped_count = 0
fail_count = 0

for w in wfs:
    wid = w["id"]
    code, full = call(f"/api/v1/workflows/{wid}")
    if code != 200:
        print(f"  FETCH-FAIL  {w['name']}: {code}")
        fail_count += 1
        continue

    changed = []
    for node in full.get("nodes", []):
        ntype = node.get("type")
        ntv = float(node.get("typeVersion", 1))
        nmax = new_max.get(ntype)
        if nmax is None:
            # node type not registered (e.g., webdav) — leave as-is, will fail activation
            continue
        if ntv > nmax:
            # cap typeVersion to the highest integer-or-decimal step <= nmax
            old = node["typeVersion"]
            node["typeVersion"] = nmax if isinstance(nmax, float) and not nmax.is_integer() else int(nmax)
            changed.append(f"{node.get('name')}({ntype}): {old}→{node['typeVersion']}")

    if not changed:
        skipped_count += 1
        continue

    # PUT updated workflow
    payload = {
        "name": full["name"],
        "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k: v for k, v in (full.get("settings") or {}).items()
                     if k in ("executionOrder", "errorWorkflow", "timezone",
                              "saveExecutionProgress", "saveManualExecutions",
                              "saveDataErrorExecution", "saveDataSuccessExecution",
                              "executionTimeout")} or {"executionOrder": "v1"},
    }
    if full.get("staticData"):
        payload["staticData"] = full["staticData"]

    code, resp = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    if code in (200, 201):
        patched_count += 1
        print(f"  PATCH  {w['name']}  ({len(changed)} nodes)")
        for c in changed[:5]:
            print(f"           {c}")
        if len(changed) > 5:
            print(f"           ... +{len(changed)-5} more")
    else:
        fail_count += 1
        msg = resp.get("message", str(resp))[:200]
        print(f"  FAIL   {w['name']}: {msg}")

print(f"\nResult: {patched_count} patched, {skipped_count} unchanged, {fail_count} failed")

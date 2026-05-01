#!/usr/bin/env python3
"""Activate previously-active workflows on new self-hosted n8n.
Maps active-on-old workflow names to new IDs and activates each.
Does NOT touch old instance — that stays as-is until user explicitly says."""
import json
from pathlib import Path
from urllib import request, error

OLD_HOST = "https://thebonpet.app.n8n.cloud"
NEW_HOST = "https://n8n.thebonpet.com"
OLD_KEY = Path.home().joinpath(".n8n-bonpet-key").read_text().strip()
NEW_KEY = Path.home().joinpath(".n8n-bonpet-newkey").read_text().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def call(host, path, key, method="GET", body=None):
    url = f"{host}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("X-N8N-API-KEY", key)
    req.add_header("accept", "application/json")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

# 1. Active workflow names on old
_, old = call(OLD_HOST, "/api/v1/workflows?limit=250", OLD_KEY)
active_names = sorted([w["name"] for w in old.get("data", []) if w.get("active")])
print(f"Active on old: {len(active_names)}")

# 2. Map names to new IDs
_, new = call(NEW_HOST, "/api/v1/workflows?limit=250", NEW_KEY)
new_by_name = {w["name"]: w["id"] for w in new.get("data", [])}

# 3. Activate each on new
print()
results = []
for name in active_names:
    new_id = new_by_name.get(name)
    if not new_id:
        results.append((name, "MISSING", ""))
        print(f"  MISS  {name}  (not found on new)")
        continue
    code, resp = call(NEW_HOST, f"/api/v1/workflows/{new_id}/activate", NEW_KEY, "POST")
    if code in (200, 201):
        results.append((name, "ACTIVATED", new_id))
        print(f"  ON    {name}  -> {new_id}")
    else:
        msg = resp.get("message", str(resp))[:200]
        results.append((name, "FAIL", msg))
        print(f"  FAIL  {name}: {msg}")

print()
ok = sum(1 for r in results if r[1] == "ACTIVATED")
print(f"Activated {ok}/{len(active_names)} on new instance.")
print("Old instance unchanged — both still running until you say to deactivate old.")

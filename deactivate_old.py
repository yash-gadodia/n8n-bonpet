#!/usr/bin/env python3
"""Deactivate the 27 previously-active workflows on OLD n8n cloud.
Reverse cutover: new instance is now active, old is being silenced."""
import json, os
from urllib import request, error

OLD_HOST = "https://thebonpet.app.n8n.cloud"
OLD_KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()

def call(path, method="GET", body=None):
    req = request.Request(f"{OLD_HOST}{path}", data=json.dumps(body).encode() if body else None, method=method)
    req.add_header("X-N8N-API-KEY", OLD_KEY)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("accept", "application/json")
    if body: req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

_, data = call("/api/v1/workflows?limit=250")
to_deact = [w for w in data.get("data", []) if w.get("active")]
print(f"Deactivating {len(to_deact)} active workflows on OLD cloud...\n")
for w in to_deact:
    code, resp = call(f"/api/v1/workflows/{w['id']}/deactivate", "POST")
    print(f"  {'OFF ' if code in (200,201) else 'FAIL'} {w['name']}")
print("\nOld cloud silenced. New self-hosted is the sole runner.")

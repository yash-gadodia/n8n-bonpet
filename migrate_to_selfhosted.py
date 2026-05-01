#!/usr/bin/env python3
"""Migrate all workflows from n8n Cloud → self-hosted n8n.thebonpet.com.

Strategy:
- Fetch all workflows from old instance (cloud) with full nodes/connections.
- Strip API-rejected fields (id, active, tags, createdAt, updatedAt, etc.).
- POST each to new instance as inactive.
- Log credential references found in nodes (for manual recreation on new instance).
- Log webhook trigger URLs (for external-caller updates).
- DO NOT touch old instance state. Old workflows stay active until user manually deactivates.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib import request, error

OLD_HOST = "https://thebonpet.app.n8n.cloud"
NEW_HOST = "https://n8n.thebonpet.com"
OLD_KEY = Path.home().joinpath(".n8n-bonpet-key").read_text().strip()
NEW_KEY = Path.home().joinpath(".n8n-bonpet-newkey").read_text().strip()

OUT_DIR = Path.home() / "n8n-bonpet" / "migration_2026-04-27"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def call(host, path, key, method="GET", body=None):
    url = f"{host}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("X-N8N-API-KEY", key)
    req.add_header("accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# 1. Pull all workflows from old (with full JSON)
print("== Pulling workflows from old instance ==")
status, data = call(OLD_HOST, "/api/v1/workflows?limit=250", OLD_KEY)
assert status == 200, f"List failed: {status} {data}"
wf_summaries = data.get("data", [])
print(f"Found {len(wf_summaries)} workflows on old instance")

full_workflows = []
for s in wf_summaries:
    wid = s["id"]
    code, full = call(OLD_HOST, f"/api/v1/workflows/{wid}", OLD_KEY)
    if code != 200:
        print(f"  WARN: failed to fetch {wid} ({s['name']}): {code}")
        continue
    full_workflows.append(full)
    (OUT_DIR / f"old_{wid}.json").write_text(json.dumps(full, indent=2))

print(f"Saved {len(full_workflows)} full workflow JSONs to {OUT_DIR}")

# 2. Build credential + webhook reports
cred_refs = {}  # cred_id -> {name, type, used_in: [wf_name]}
webhook_paths = []  # (wf_name, node_name, path, http_method)

for wf in full_workflows:
    wf_name = wf["name"]
    for node in wf.get("nodes", []):
        # credentials
        for cred_type, cred_ref in (node.get("credentials") or {}).items():
            cid = cred_ref.get("id") if isinstance(cred_ref, dict) else None
            cname = cred_ref.get("name") if isinstance(cred_ref, dict) else cred_ref
            if cid is None:
                continue
            entry = cred_refs.setdefault(cid, {"name": cname, "type": cred_type, "used_in": set()})
            entry["used_in"].add(wf_name)
        # webhooks
        if node.get("type") in ("n8n-nodes-base.webhook", "n8n-nodes-base.formTrigger"):
            params = node.get("parameters", {}) or {}
            path = params.get("path", "")
            method = params.get("httpMethod", "GET")
            webhook_paths.append((wf_name, node.get("name", ""), path, method))

# 3. Push each to new instance
print("\n== Pushing to new instance ==")

# Skip workflows that already exist on new instance (by name) to support safe re-runs
_, existing = call(NEW_HOST, "/api/v1/workflows?limit=250", NEW_KEY)
existing_names = {w["name"] for w in (existing.get("data") or [])}
print(f"  (already on new: {len(existing_names)} — will skip those)")

# n8n self-hosted API only accepts these keys in `settings`
ALLOWED_SETTINGS = {"executionOrder", "errorWorkflow", "timezone", "saveExecutionProgress",
                    "saveManualExecutions", "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout"}

results = []
for wf in full_workflows:
    name = wf["name"]
    if name in existing_names:
        results.append((name, "SKIP", "", "already exists on new"))
        print(f"  SKIP  {name}  (already exists)")
        continue
    raw_settings = wf.get("settings") or {}
    settings = {k: v for k, v in raw_settings.items() if k in ALLOWED_SETTINGS}
    if "executionOrder" not in settings:
        settings["executionOrder"] = "v1"
    payload = {
        "name": wf["name"],
        "nodes": wf.get("nodes", []),
        "connections": wf.get("connections", {}),
        "settings": settings,
    }
    if wf.get("staticData"):
        payload["staticData"] = wf["staticData"]
    code, resp = call(NEW_HOST, "/api/v1/workflows", NEW_KEY, "POST", payload)
    if code in (200, 201):
        new_id = resp.get("id")
        results.append((name, "OK", new_id, ""))
        print(f"  OK  {name}  -> {new_id}")
    else:
        msg = resp.get("message", str(resp))[:150]
        results.append((name, "FAIL", "", msg))
        print(f"  FAIL  {name}  ({code}): {msg}")
    time.sleep(0.2)  # gentle on the API

# 4. Reports
print("\n== Writing reports ==")
report = OUT_DIR / "MIGRATION_REPORT.md"
with report.open("w") as f:
    ok = sum(1 for r in results if r[1] == "OK")
    fail = sum(1 for r in results if r[1] == "FAIL")
    f.write(f"# n8n Cloud → Self-hosted migration — 2026-04-27\n\n")
    f.write(f"**Source:** {OLD_HOST}\n")
    f.write(f"**Target:** {NEW_HOST}\n")
    f.write(f"**Result:** {ok}/{len(results)} migrated, {fail} failed\n\n")
    f.write(f"All workflows imported as INACTIVE. Old workflows untouched (still active on cloud).\n\n")

    f.write("## Migration results\n\n")
    f.write("| Status | Workflow | New ID | Note |\n|---|---|---|---|\n")
    for name, st, nid, note in results:
        f.write(f"| {st} | {name} | {nid} | {note} |\n")

    f.write("\n## Credentials to recreate on new instance\n\n")
    f.write(f"Found {len(cred_refs)} unique credentials referenced. Recreate each with the SAME NAME on new instance, then the workflows will auto-link.\n\n")
    f.write("| Credential name | Type | Used in workflows |\n|---|---|---|\n")
    for cid, info in sorted(cred_refs.items(), key=lambda x: x[1]["name"] or ""):
        used = ", ".join(sorted(info["used_in"]))
        f.write(f"| {info['name']} | {info['type']} | {used} |\n")

    f.write("\n## Webhook URLs that will change\n\n")
    f.write("These triggers had public webhook URLs on cloud. The new self-hosted URLs follow the same path but on the new host. **External callers (Shopify Flow, etc.) need updating.**\n\n")
    f.write("| Workflow | Node | Old URL | New URL | Method |\n|---|---|---|---|---|\n")
    for wf_name, node, path, method in webhook_paths:
        old_url = f"{OLD_HOST}/webhook/{path}"
        new_url = f"{NEW_HOST}/webhook/{path}"
        f.write(f"| {wf_name} | {node} | {old_url} | {new_url} | {method} |\n")

    f.write("\n## Next steps\n\n")
    f.write("1. On new instance UI: recreate each credential listed above with the SAME NAME — workflows will auto-relink.\n")
    f.write("2. Test each migrated workflow manually (Execute Workflow button) before activating.\n")
    f.write("3. Update external webhook callers (Shopify Flow, etc.) to point at new URLs.\n")
    f.write("4. Activate workflows on new instance one-by-one.\n")
    f.write("5. AFTER verifying parity, deactivate corresponding workflows on old cloud.\n")
    f.write("6. After ~1 week of stable operation, delete cloud workflows + cancel cloud subscription.\n")

print(f"Report written: {report}")
print(f"OK={sum(1 for r in results if r[1]=='OK')} FAIL={sum(1 for r in results if r[1]=='FAIL')}")

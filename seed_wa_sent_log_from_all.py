#!/usr/bin/env python3
"""One-off: scan n8n executions from past 7d across customer-WA workflows, seed wa_sent_log.

v2 (post-2026-04-23 incident): instead of scraping phones from every node output
(which incorrectly includes upstream customer-list reads), we target ONLY the
IMMEDIATE UPSTREAM of each WA-send node (HTTP request to whatsapp/send). That
upstream item is the exact payload that fed the send call.

Reorder Reminder already seeded via seed_wa_sent_log_from_reorder.py — skip here.
"""
import json, os, urllib.request, urllib.error, uuid, time, datetime

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
WA_LOG_RANGE = "wa_sent_log!A:F"
WA_ENDPOINT_HINT = "whatsapp/send"

TEAM_PHONES = {
    "+6581394225", "+6598531677", "+6590108515",
    "+6587993341", "+6581114800", "+6282240119788",
}

WORKFLOWS = {
    "zg9zKFssSJRiJAuS": ("abandoned_cart",     "recovery"),
    "b6MFnZQVTdIRw35a": ("big_order_alert",    "thank_you"),
    "e9M54bpyzHPPRcDr": ("review_watcher",     "review_response"),
    "isMdcBBwcgFitYVp": ("winback",            "winback_60d"),
    "3Lpb27gzixBCKGKe": ("dog_run_invite",     "invite"),
}


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()


def norm(p):
    if not p: return ""
    s = str(p).replace(" ", "").strip()
    if s.startswith("+"): return s
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) == 8 and digits[0] in "689": return "+65" + digits
    if len(digits) == 10 and digits.startswith("65"): return "+" + digits
    if 8 <= len(digits) <= 15: return "+" + digits
    return ""


def find_wa_send_nodes(wf):
    """Names of nodes whose URL hits the WA send endpoint."""
    out = []
    for n in wf.get("nodes", []):
        url = str(n.get("parameters", {}).get("url", ""))
        if WA_ENDPOINT_HINT in url.lower():
            out.append(n["name"])
    return out


def find_upstream(wf, node_name):
    """Names of nodes that feed `node_name`'s main input."""
    up = []
    for src, dests in wf.get("connections", {}).items():
        for dest_list in dests.get("main", []):
            for d in dest_list:
                if d.get("node") == node_name:
                    up.append(src)
    return up


def extract_phone(item_json):
    """Try known field names for phone on an item."""
    for f in ("customer_phone", "target_phone", "phone", "recipient_phone", "phone_number"):
        v = item_json.get(f)
        if v:
            p = norm(v)
            if p: return p
    return ""


def extract_order_id(item_json):
    for f in ("order_id", "last_order_id", "trial_order_id", "checkout_token", "cart_token"):
        v = item_json.get(f)
        if v: return str(v)
    return ""


def collect_for_workflow(workflow_id, slug, tmpl, cutoff_iso):
    s, b = http("GET", f"/workflows/{workflow_id}")
    wf = json.loads(b)
    send_nodes = find_wa_send_nodes(wf)
    if not send_nodes:
        print(f"  [{slug}] no WA send nodes found, skipping")
        return []

    # Map each send node to its immediate upstream(s)
    upstream_map = {sn: find_upstream(wf, sn) for sn in send_nodes}

    s, b = http("GET", f"/executions?workflowId={workflow_id}&limit=100")
    executions = json.loads(b).get("data", [])
    rows = []
    for ex_meta in executions:
        started = ex_meta.get("startedAt", "")
        if not started or started < cutoff_iso: continue
        if ex_meta.get("status") != "success": continue
        s, b = http("GET", f"/executions/{ex_meta['id']}?includeData=true")
        ex = json.loads(b)
        rd = ex.get("data", {}).get("resultData", {}).get("runData", {})

        seen_here = {}
        for send_node, upstreams in upstream_map.items():
            # If the send node didn't run, skip
            if send_node not in rd: continue
            # Look at each upstream's output items
            for up in upstreams:
                up_runs = rd.get(up, [])
                for run in up_runs:
                    if "error" in run: continue
                    items_all = run.get("data", {}).get("main", [[]])
                    for out_port in items_all:
                        for it in out_port:
                            j = it.get("json", {})
                            # IF nodes carry a `should_send` or similar — respect it if present
                            if j.get("should_send") is False: continue
                            p = extract_phone(j)
                            if not p or p in TEAM_PHONES or p in seen_here: continue
                            seen_here[p] = {
                                "order_id": extract_order_id(j),
                                "first_name": j.get("customer_name") or j.get("first_name") or "",
                            }
        for p, meta in seen_here.items():
            rows.append({
                "phone": p,
                "workflow": slug,
                "template": tmpl,
                "sent_at": started,
                "order_id": meta["order_id"],
                "notes": meta["first_name"],
            })
    return rows


def append_rows(rows):
    if not rows:
        print("  (no rows to append)")
        return
    # Chunk by 500 to avoid huge payloads
    CHUNK = 500
    total_done = 0
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i+CHUNK]
        values = [[r["phone"], r["workflow"], r["template"],
                   r["sent_at"], r["order_id"], r["notes"]] for r in chunk]
        body = {"values": values}
        nodes = [
            {"parameters": {"httpMethod": "POST", "path": f"tmp-append-wa-log-chunk-{i}",
                            "responseMode": "lastNode", "options": {}},
             "id": str(uuid.uuid4()), "name": "Trigger",
             "type": "n8n-nodes-base.webhook", "typeVersion": 2,
             "position": [0, 0], "webhookId": str(uuid.uuid4())},
            {"parameters": {
                "method": "POST",
                "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{WA_LOG_RANGE}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "googleSheetsOAuth2Api",
                "sendBody": True, "specifyBody": "json",
                "jsonBody": json.dumps(body), "options": {},
             }, "id": str(uuid.uuid4()), "name": "Append",
             "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
             "position": [240, 0],
             "credentials": {"googleSheetsOAuth2Api": GS_CRED}},
        ]
        conn = {"Trigger": {"main": [[{"node": "Append", "type": "main", "index": 0}]]}}
        s, b = http("POST", "/workflows", {"name": f"TEMP Append WA Log Chunk {i}",
                                            "nodes": nodes, "connections": conn,
                                            "settings": {"executionOrder": "v1"}})
        wf_id = json.loads(b)["id"]
        http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
        http("POST", f"/workflows/{wf_id}/activate")
        time.sleep(1)
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://thebonpet.app.n8n.cloud/webhook/tmp-append-wa-log-chunk-{i}",
                data=b'{}', method="POST",
                headers={"Content-Type": "application/json"}), timeout=60)
            total_done += len(chunk)
            print(f"  chunk {i//CHUNK + 1}: seeded {len(chunk)} rows ✓ (total {total_done})")
        except urllib.error.HTTPError as e:
            print(f"  chunk {i//CHUNK + 1} HTTP {e.code}: {e.read().decode()[:120]}")
        time.sleep(0.5)
        http("DELETE", f"/workflows/{wf_id}")


if __name__ == "__main__":
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)).isoformat().replace("+00:00", "Z")
    print(f"Cutoff: {cutoff}\n")

    all_rows = []
    for wid, (slug, tmpl) in WORKFLOWS.items():
        rows = collect_for_workflow(wid, slug, tmpl, cutoff)
        print(f"[{slug}] {len(rows)} customer phones")
        for r in rows[:5]:
            print(f"  {r['phone']}  order={r['order_id']}  sent_at={r['sent_at']}  {r['notes']}")
        if len(rows) > 5:
            print(f"  ... and {len(rows)-5} more")
        all_rows.extend(rows)

    print(f"\nTotal: {len(all_rows)} rows")
    if all_rows:
        print("Appending to wa_sent_log...")
        append_rows(all_rows)

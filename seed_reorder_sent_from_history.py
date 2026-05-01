#!/usr/bin/env python3
"""One-off: seed `reorder_reminder_sent` tab from recent n8n execution history.

Context: from 2026-04-19 through 2026-04-23, the reorder reminder workflow fired
daily without dedup and spammed ~20 customers 2-5 times each. This script grabs
every unique phone we sent to (from execution history + the hardcoded fallback list
that lived in RECENTLY_SENT_PHONES) and pre-populates the sent log so none of them
get re-messaged after we reactivate the fixed workflow.

Safe to run multiple times — Google Sheets values:append just adds rows, so rerunning
produces duplicate rows. Worth seeding once and no more.
"""
import json, os, urllib.request, urllib.error, uuid, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
TAB_NAME = "reorder_reminder_sent"

# Daily + initial manual executions that actually fired Send WA.
EXECUTIONS = [
    3394, 3249, 3157, 3017,    # 2026-04-20 through 2026-04-23 daily schedules
    2829, 2668, 2667, 2639,    # 2026-04-19 initial manual webhook runs
]

# The 11 phones that were hardcoded into RECENTLY_SENT_PHONES — sent on 2026-04-19
# in earlier manual runs that aren't in the API execution history cleanly.
HARDCODED_LEGACY = [
    ("+6581864255", "Kenneth"),
    ("+6584825110", "Mayer"),
    ("+6597120995", "Connie"),
    ("+6597856266", "Hazel"),
    ("+6583380150", "Chuan"),
    ("+6596870177", "Yee"),
    ("+6582987784", "Yi"),
    ("+6596574614", "Chandani"),
    ("+6590097354", "Kathrine"),
    ("+6587508842", "Aina"),
    ("+6593860166", "Arabel"),
]


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()


def fetch_execution_items(ex_id):
    s, body = http("GET", f"/executions/{ex_id}?includeData=true")
    if s != 200: return []
    ex = json.loads(body)
    run_data = ex.get("data", {}).get("resultData", {}).get("runData", {})
    code_key = next((k for k in run_data if "Candidate" in k or "Compute" in k), None)
    if not code_key: return []
    return run_data[code_key][0].get("data", {}).get("main", [[]])[0]


def collect_rows():
    """One row per unique phone. Later rows (if phone repeats) overwrite earlier,
    so we end up with the MOST RECENT send metadata per phone."""
    by_phone = {}
    # Process oldest to newest so newest wins
    for ex_id in reversed(EXECUTIONS):
        items = fetch_execution_items(ex_id)
        for it in items:
            j = it.get("json", {})
            if j.get("is_header"): continue
            phone = j.get("customer_phone") or j.get("target_phone") or ""
            if not phone or phone == "+6581394225":  # Yash = summary recipient, skip
                continue
            by_phone[phone] = {
                "phone": phone,
                "sent_at": "",  # filled below
                "last_order_id": str(j.get("last_order_id", "")),
                "reminder_num": str(j.get("reminder_num", "")),
                "first_name": j.get("customer_name") or j.get("first_name") or "",
                "days_since": str(j.get("days_since", "")),
            }
        # Attach execution start time as sent_at for all rows this pass
        s, body = http("GET", f"/executions/{ex_id}?includeData=false")
        started = json.loads(body).get("startedAt", "") if s == 200 else ""
        for it in items:
            j = it.get("json", {})
            if j.get("is_header"): continue
            phone = j.get("customer_phone") or j.get("target_phone") or ""
            if not phone or phone == "+6581394225": continue
            if phone in by_phone: by_phone[phone]["sent_at"] = started

    # Merge the hardcoded legacy list (do not overwrite if already present)
    for phone, name in HARDCODED_LEGACY:
        if phone not in by_phone:
            by_phone[phone] = {
                "phone": phone,
                "sent_at": "2026-04-19T10:00:00Z",
                "last_order_id": "",
                "reminder_num": "1",
                "first_name": name,
                "days_since": "",
            }
    return list(by_phone.values())


def append_via_temp_wf(rows):
    """Fire a one-shot n8n workflow that does Google Sheets values:append."""
    values = [[r["phone"], r["sent_at"], r["last_order_id"],
               r["reminder_num"], r["first_name"], r["days_since"]] for r in rows]
    body = {"values": values}

    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-seed-reorder-sent",
                        "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger",
         "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{TAB_NAME}!A:F:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
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
    s, b = http("POST", "/workflows", {"name": "TEMP Seed Reorder Sent",
                                        "nodes": nodes, "connections": conn,
                                        "settings": {"executionOrder": "v1"}})
    wf_id = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://thebonpet.app.n8n.cloud/webhook/tmp-seed-reorder-sent",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"}), timeout=60)
        print(f"  Seeded {len(rows)} rows ✓")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
    time.sleep(1)
    http("DELETE", f"/workflows/{wf_id}")


if __name__ == "__main__":
    print(f"Fetching execution items from {len(EXECUTIONS)} runs...")
    rows = collect_rows()
    print(f"Unique phones to seed: {len(rows)}")
    for r in sorted(rows, key=lambda x: x["first_name"]):
        print(f"  {r['phone']}  {r['first_name']:14s}  last_order={r['last_order_id']}  sent_at={r['sent_at']}")
    print()
    print("Appending to sheet...")
    append_via_temp_wf(rows)
    print()
    print(f"✅ https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=800800")

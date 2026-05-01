#!/usr/bin/env python3
"""One-off: copy rows from reorder_reminder_sent (GID 800800) into wa_sent_log (GID 700800).

We seed from this week's reorder spam wave (2026-04-19 through 2026-04-23) so the
global 7-day cooldown correctly excludes Yi, Sarah, Alix, etc. from imminent sends
by other workflows (Post-Trial Nurture etc.).

Older winback_sent / other logs not seeded: they predate the 7-day cooldown window
by definition (winback fires at 60d, which is already >7d ago by nature).
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
REORDER_SENT_RANGE = "reorder_reminder_sent!A:F"
WA_LOG_RANGE = "wa_sent_log!A:F"


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()


def read_reorder_rows():
    """Use a temp n8n workflow to GET the reorder_reminder_sent sheet rows."""
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-read-reorder-sent",
                        "responseMode": "lastNode", "options": {}},
         "id": str(uuid.uuid4()), "name": "Trigger",
         "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 0], "webhookId": str(uuid.uuid4())},
        {"parameters": {
            "method": "GET",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{REORDER_SENT_RANGE}",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api", "options": {},
         }, "id": str(uuid.uuid4()), "name": "Read",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
         "position": [240, 0],
         "credentials": {"googleSheetsOAuth2Api": GS_CRED}},
    ]
    conn = {"Trigger": {"main": [[{"node": "Read", "type": "main", "index": 0}]]}}

    s, b = http("POST", "/workflows", {"name": "TEMP Read Reorder Sent",
                                        "nodes": nodes, "connections": conn,
                                        "settings": {"executionOrder": "v1"}})
    wf_id = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    time.sleep(1)
    try:
        req = urllib.request.Request("https://thebonpet.app.n8n.cloud/webhook/tmp-read-reorder-sent",
            data=b'{}', method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:
        print(f"read failed: {e}")
        resp = {}
    finally:
        http("DELETE", f"/workflows/{wf_id}")

    values = resp.get("values", [])
    if not values: return []
    headers = values[0]
    rows = []
    for row in values[1:]:
        row_dict = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        rows.append(row_dict)
    return rows


def append_rows(rows):
    """Append rows (list of dicts with wa_sent_log schema) to wa_sent_log tab."""
    values = [[r.get("phone", ""), r.get("workflow", ""), r.get("template", ""),
               r.get("sent_at", ""), r.get("order_id", ""), r.get("notes", "")] for r in rows]
    body = {"values": values}
    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "tmp-append-wa-log",
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
    s, b = http("POST", "/workflows", {"name": "TEMP Append WA Log",
                                        "nodes": nodes, "connections": conn,
                                        "settings": {"executionOrder": "v1"}})
    wf_id = json.loads(b)["id"]
    http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
    http("POST", f"/workflows/{wf_id}/activate")
    time.sleep(1)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://thebonpet.app.n8n.cloud/webhook/tmp-append-wa-log",
            data=b'{}', method="POST",
            headers={"Content-Type": "application/json"}), timeout=30)
        print(f"  Seeded {len(rows)} rows ✓")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
    time.sleep(1)
    http("DELETE", f"/workflows/{wf_id}")


if __name__ == "__main__":
    print("Reading reorder_reminder_sent...")
    reorder_rows = read_reorder_rows()
    print(f"Found {len(reorder_rows)} rows")

    # Transform to wa_sent_log schema
    wa_rows = []
    for r in reorder_rows:
        step = r.get("reminder_num", "")
        wa_rows.append({
            "phone": r.get("phone", ""),
            "workflow": "reorder_reminder",
            "template": f"reminder_{step}" if step else "reminder",
            "sent_at": r.get("sent_at", ""),
            "order_id": r.get("last_order_id", ""),
            "notes": f"days_since={r.get('days_since', '')}",
        })

    print(f"\nSeeding wa_sent_log with {len(wa_rows)} rows...")
    append_rows(wa_rows)
    print(f"\n✅ https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=700800")

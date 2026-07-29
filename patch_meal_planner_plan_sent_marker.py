#!/usr/bin/env python3
"""Add a `plan_sent_at` marker to the "TBP Meal Planner Leads" workflow.

Why: the meal-plan comms fire off the "Plan Ready?" branch, but nothing records
WHEN the plan actually went out. The follow-up workflow (build_meal_plan_followup.py)
needs that timestamp to compute D3/D7 without guessing from `last_updated`.

Wiring: Send Email fans out to BOTH "Send WhatsApp" (untouched) and the new
"Mark Plan Sent" sheet write, so a Sheets hiccup can never block the WA send.
Anchoring on Send Email (not Send WhatsApp) is deliberate: "Plan Ready?" already
guarantees an email address, phone is optional.

`handlingExtraData: insertInNewColumn` creates the `plan_sent_at` header on first
write, so no manual sheet prep is needed.
"""
import json, os, urllib.request, urllib.error, uuid

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
WF_ID = "Athof1GaI7i0l441"  # TBP Meal Planner Leads
LEADS_SHEET_ID = "1KlF4IYw5jjjCzLISFdOdhd5RYR5JxGZFttHb0BoJPtM"
LEADS_GID = 2029633008
# The meal planner leads sheet is owned by Nic, so it needs HIS Sheets credential.
# The default "Google Sheets account" cred has no access to this document.
GS_CRED_NIC = {"id": "7JXUrNbjnmm04LU8", "name": "NIC Google Sheets account"}
NODE_NAME = "Mark Plan Sent"


def http(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def mark_plan_sent_node(position):
    return {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": LEADS_SHEET_ID, "mode": "list",
                           "cachedResultName": "TBP Meal Planner Leads",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{LEADS_SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": LEADS_GID, "mode": "list",
                          "cachedResultName": "leads",
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{LEADS_SHEET_ID}/edit#gid={LEADS_GID}"},
            "columns": {
                "mappingMode": "defineBelow",
                "matchingColumns": ["session_id"],
                "value": {
                    "session_id": "={{ $('Flatten').item.json.session_id }}",
                    "plan_sent_at": "={{ new Date().toISOString() }}",
                },
            },
            "options": {"handlingExtraData": "insertInNewColumn"},
        },
        "id": str(uuid.uuid4()), "name": NODE_NAME,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED_NIC},
        "onError": "continueRegularOutput",
    }


if __name__ == "__main__":
    status, wf = http("GET", f"/workflows/{WF_ID}")
    if status != 200:
        print(f"❌ GET workflow → HTTP {status}\n{wf}")
        raise SystemExit(1)

    json.dump(wf, open(os.path.expanduser("~/n8n-bonpet/snapshots/meal_planner_leads_prod.json"), "w"), indent=2)
    print("📸 snapshot → snapshots/meal_planner_leads_prod.json")

    send_email = next(n for n in wf["nodes"] if n["name"] == "Send Email")
    node = mark_plan_sent_node([send_email["position"][0] + 220, send_email["position"][1] + 200])
    # Rebuild the node every run rather than skipping when present, so credential or
    # mapping edits in this file actually reach prod on a re-run.
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] != NODE_NAME] + [node]

    conns = wf["connections"].setdefault("Send Email", {"main": [[]]})
    if not any(c["node"] == NODE_NAME for c in conns["main"][0]):
        conns["main"][0].append({"node": NODE_NAME, "type": "main", "index": 0})

    payload = {"name": wf["name"], "nodes": wf["nodes"],
               "connections": wf["connections"], "settings": wf.get("settings") or {}}
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT /workflows/{WF_ID} → HTTP {status}")
    if status != 200:
        print(str(body)[:600])
        raise SystemExit(1)
    print(f"✅ Added '{NODE_NAME}' → https://n8n.thebonpet.com/workflow/{WF_ID}")

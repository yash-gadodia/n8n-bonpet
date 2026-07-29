#!/usr/bin/env python3
"""Add a `plan_sent_at` marker to the "TBP Meal Planner Leads" workflow.

Why: the meal-plan comms fire off the "Plan Ready?" branch, but nothing records
WHEN the plan actually went out. The follow-up workflow (build_meal_plan_followup.py)
needs that timestamp to compute D3/D7 without guessing from `last_updated`.

Wiring: Send Email fans out to BOTH "Send WhatsApp" (untouched) and the new
"Mark Plan Sent" sheet write, so a Sheets hiccup can never block the WA send.
Anchoring on Send Email (not Send WhatsApp) is deliberate: "Plan Ready?" already
guarantees an email address, phone is optional.

Mapping mode matters here. `defineBelow` silently DROPS a field whose column
does not exist in the header (handlingExtraData only governs autoMap), so the
node reported success while writing nothing. `autoMapInputData` fed by a Code
node that emits exactly {session_id, plan_sent_at}, plus
`handlingExtraData: insertInNewColumn`, is the supported way to have n8n create
the column on first write.
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
ROW_NODE_NAME = "Plan Sent Row"


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


def plan_sent_row_node(position):
    """Emit exactly the two columns to write, so autoMap has a clean shape."""
    js = ("return [{ json: { session_id: $('Flatten').first().json.session_id,"
          " plan_sent_at: new Date().toISOString() } }];")
    return {
        "parameters": {"jsCode": js},
        "id": str(uuid.uuid4()), "name": ROW_NODE_NAME,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": position,
    }


def col(name, match=False):
    return {"id": name, "displayName": name, "required": False,
            "defaultMatch": match, "display": True, "type": "string",
            "canBeUsedToMatch": True}


def build_schema(wf):
    """Full sheet column list, in sheet order, with plan_sent_at appended.

    Sheets v4.5 validates a defineBelow schema positionally against the live
    header row. A two-entry schema fails with "Column names were updated after
    the node's setup: session_id -> timestamp" because it expects entry 0 to be
    the sheet's first column. So mirror the Sheet Upsert node's schema (n8n keeps
    it in sync with the real header) and append the one column we are adding.
    """
    upsert = next(n for n in wf["nodes"] if n["name"] == "Sheet Upsert")
    names = [c["id"] for c in upsert["parameters"]["columns"]["schema"]]
    if "plan_sent_at" not in names:
        names.append("plan_sent_at")
    return [col(n, match=(n == "session_id")) for n in names]


def mark_plan_sent_node(position, schema):
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
                "mappingMode": "autoMapInputData",
                "matchingColumns": ["session_id"],
                "schema": schema,
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
    x, y = send_email["position"]
    row = plan_sent_row_node([x + 220, y + 200])
    node = mark_plan_sent_node([x + 440, y + 200], build_schema(wf))
    # Rebuild both nodes every run rather than skipping when present, so credential
    # or mapping edits in this file actually reach prod on a re-run.
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] not in (NODE_NAME, ROW_NODE_NAME)] + [row, node]

    conns = wf["connections"].setdefault("Send Email", {"main": [[]]})
    conns["main"][0] = [c for c in conns["main"][0] if c["node"] != NODE_NAME]
    if not any(c["node"] == ROW_NODE_NAME for c in conns["main"][0]):
        conns["main"][0].append({"node": ROW_NODE_NAME, "type": "main", "index": 0})
    wf["connections"][ROW_NODE_NAME] = {"main": [[{"node": NODE_NAME, "type": "main", "index": 0}]]}

    payload = {"name": wf["name"], "nodes": wf["nodes"],
               "connections": wf["connections"], "settings": wf.get("settings") or {}}
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    print(f"PUT /workflows/{WF_ID} → HTTP {status}")
    if status != 200:
        print(str(body)[:600])
        raise SystemExit(1)
    print(f"✅ Added '{NODE_NAME}' → https://n8n.thebonpet.com/workflow/{WF_ID}")

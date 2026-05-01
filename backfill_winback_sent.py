#!/usr/bin/env python3
"""One-off: append 50 just-messaged customers to winback_sent tab, and extract
the tab's gid for pasting into build_winback.py.

Assumes /tmp/winback_sent_backfill.json exists with the 50 customer dicts
pulled from execution #3001's Format Message output.
"""
import json, uuid, os, urllib.request, urllib.error, time

KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
API = "https://thebonpet.app.n8n.cloud/api/v1"
TEAM = "i1GSXBntwNvNqic8"
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}
SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
TAB_NAME = "winback_sent"
WEBHOOK_PATH = "backfill-winback-sent-b7e2f1a3"


def http(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


with open("/tmp/winback_sent_backfill.json") as f:
    CUSTOMERS = json.load(f)
assert len(CUSTOMERS) == 50, f"Expected 50 customers, got {len(CUSTOMERS)}"

NOW_ISO = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())
ROWS = [[c["email"], NOW_ISO, c["days_since"], c["first_name"]] for c in CUSTOMERS]


def webhook_node():
    return {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "lastNode", "options": {}},
        "id": str(uuid.uuid4()), "name": "Trigger",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 0], "webhookId": str(uuid.uuid4()),
    }


def get_metadata_node():
    return {
        "parameters": {
            "method": "GET",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "fields", "value": "sheets.properties.sheetId,sheets.properties.title"},
            ]},
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Get Metadata",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


def extract_gid_node():
    js = f"""// Find winback_sent gid from metadata response.
const meta = $input.first().json;
const sheets = meta.sheets || [];
const match = sheets.find(s => s.properties && s.properties.title === {json.dumps(TAB_NAME)});
const gid = match ? match.properties.sheetId : null;
return [{{ json: {{ gid, tab: {json.dumps(TAB_NAME)}, rows_to_append: {len(ROWS)} }} }}];
"""
    return {
        "parameters": {"jsCode": js},
        "id": str(uuid.uuid4()), "name": "Extract GID",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [480, 0],
    }


def append_rows_node():
    body = {"values": ROWS}
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{TAB_NAME}!A2:D:append",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "valueInputOption", "value": "RAW"},
                {"name": "insertDataOption", "value": "INSERT_ROWS"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps(body),
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Append Rows",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [720, 0],
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
    }


def final_summary_node():
    js = """// Final output: surface the gid so caller can read it.
const gid = $('Extract GID').first().json.gid;
const append = $input.first().json;
return [{ json: { gid, updates: append.updates || append } }];
"""
    return {
        "parameters": {"jsCode": js},
        "id": str(uuid.uuid4()), "name": "Final Summary",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [960, 0],
    }


trig = webhook_node()
get_meta = get_metadata_node()
extract = extract_gid_node()
append = append_rows_node()
final = final_summary_node()
nodes = [trig, get_meta, extract, append, final]
connections = {
    trig["name"]:     {"main": [[{"node": get_meta["name"], "type": "main", "index": 0}]]},
    get_meta["name"]: {"main": [[{"node": extract["name"], "type": "main", "index": 0}]]},
    extract["name"]:  {"main": [[{"node": append["name"], "type": "main", "index": 0}]]},
    append["name"]:   {"main": [[{"node": final["name"], "type": "main", "index": 0}]]},
}

status, body = http("POST", "/workflows", {
    "name": "TEMP Backfill Winback Sent",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
})
print(f"Create → {status}")
wf_id = json.loads(body)["id"]
print(f"  WF_ID = {wf_id}")

status, _ = http("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM})
print(f"Transfer → {status}")

status, _ = http("POST", f"/workflows/{wf_id}/activate")
print(f"Activate → {status}")
time.sleep(1)

url = f"https://thebonpet.app.n8n.cloud/webhook/{WEBHOOK_PATH}"
print(f"Firing: POST {url}")
req = urllib.request.Request(url, data=b'{}', method="POST", headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        resp_body = r.read().decode()
        print(f"  → HTTP {r.status}: {resp_body[:400]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode()[:400]}")

time.sleep(2)

# Pull execution to confirm + extract gid (in case lastNode response truncated)
status, body = http("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
data = json.loads(body)
gid = None
append_count = 0
if data.get("data"):
    ex = data["data"][0]
    print(f"\nExecution {ex['id']}: status={ex['status']}")
    runs = ex.get("data", {}).get("resultData", {}).get("runData", {})
    for n, outs in runs.items():
        out = outs[0] if outs else {}
        if "error" in out:
            print(f"  ❌ {n}: {out['error'].get('message', out['error'])[:200]}")
        else:
            print(f"  ✅ {n}")
        if n == "Extract GID":
            try:
                j = out["data"]["main"][0][0]["json"]
                gid = j.get("gid")
            except Exception as e:
                print(f"     (couldn't pull gid: {e})")
        if n == "Append Rows":
            try:
                j = out["data"]["main"][0][0]["json"]
                updates = j.get("updates") or {}
                append_count = updates.get("updatedRows", 0)
            except Exception:
                pass

print(f"\nDeleting TEMP workflow {wf_id}")
http("DELETE", f"/workflows/{wf_id}")

print()
print(f"✅ Rows appended: {append_count}")
if gid is not None:
    print(f"✅ winback_sent GID = {gid}")
    print(f"   Paste into build_winback.py:  WINBACK_SENT_TAB_GID = {gid}")
else:
    print("⚠️  Could not extract gid; check sheet URL manually.")

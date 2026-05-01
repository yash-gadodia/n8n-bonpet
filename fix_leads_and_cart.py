#!/usr/bin/env python3
"""
1. Leads Funnel: insert IF node between "Get Name and Number" → "Send Free Trial Message"
   so we skip rows where the regex couldn't extract a phone.
2. Abandoned Cart: extend "Stash Token" to also capture phone/customer_id/email,
   then insert IF after it that short-circuits if no phone AND no customer_id
   (no way to recover that lead anyway).
"""
import json, os, urllib.request, urllib.error, uuid

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def n8n(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            return res.status, json.loads(res.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def put_payload(wf):
    out = {"name": wf["name"], "nodes": wf["nodes"],
           "connections": wf["connections"],
           "settings": wf.get("settings") or {"executionOrder": "v1"}}
    if wf.get("staticData"):
        out["staticData"] = wf["staticData"]
    return out


# ─────────────────────── Leads Funnel: IF guard ─────────────────────────
def fix_leads_funnel():
    print("\n=== Fix Leads Funnel — add 'Has Phone?' guard ===")
    s, wf = n8n("GET", "/workflows/xl6MczAGNwcNEBuc")
    if s >= 300:
        print("GET failed:", s); return False

    # Drop any prior copy
    wf["nodes"] = [n for n in wf["nodes"] if n.get("name") != "Has Phone?"]
    for k in list(wf["connections"].keys()):
        if k == "Has Phone?":
            del wf["connections"][k]

    # Find positions of source/target
    pos_src = next((n["position"] for n in wf["nodes"] if n["name"] == "Get Name and Number"), [400, 0])
    pos_tgt = next((n["position"] for n in wf["nodes"] if n["name"] == "Send Free Trial Message"), [600, 0])
    if_pos = [(pos_src[0] + pos_tgt[0]) / 2, pos_src[1]]

    if_node = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [{
                    "id": str(uuid.uuid4()),
                    "leftValue": "={{ $json.number }}",
                    "rightValue": "",
                    "operator": {"type": "string", "operation": "notEmpty", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Has Phone?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": if_pos,
    }
    wf["nodes"].append(if_node)

    # Rewire: Get Name and Number → Has Phone? → (true) Send Free Trial Message + Change Lead Stage to Intake
    # Existing fan-out from "Get Name and Number" was to BOTH "Send Free Trial Message" and "Change Customer Lead Stage to Intake"
    src_conn = wf["connections"].get("Get Name and Number", {}).get("main", [[]])
    targets = list(src_conn[0]) if src_conn else []
    print(f"  existing fan-out from 'Get Name and Number': {[t['node'] for t in targets]}")

    # Replace with single Has Phone? target
    wf["connections"]["Get Name and Number"] = {
        "main": [[{"node": "Has Phone?", "type": "main", "index": 0}]]
    }
    # Has Phone? main[0] = true branch (sends), main[1] = false (skip)
    wf["connections"]["Has Phone?"] = {
        "main": [
            targets,  # TRUE: keep original fan-out
            [],       # FALSE: do nothing
        ]
    }

    s, body = n8n("PUT", "/workflows/xl6MczAGNwcNEBuc", put_payload(wf))
    print(f"  PUT → HTTP {s}")
    return s < 300


# ─────────────────────── Abandoned Cart: stash + IF guard ────────────────
def fix_abandoned_cart():
    print("\n=== Fix Abandoned Cart — early-exit if no phone & no customer_id ===")
    s, wf = n8n("GET", "/workflows/SxeOUWpesKXMYOxR")
    if s >= 300:
        print("GET failed:", s); return False

    # 1. Update Stash Token to capture more fields
    new_stash = """// Stash checkout details from the webhook payload so we can find/recover later
const p = $input.first().json;
const body = p.body || p;
const customer = body.customer || {};
return [{
  json: {
    checkout_token: body.token || body.checkout_token || '',
    checkout_id: String(body.id || ''),
    created_at: body.created_at || '',
    phone: String(body.phone || customer.phone || '').trim(),
    customer_id: String(customer.id || body.customer_id || ''),
    email: String(body.email || customer.email || '').trim(),
  }
}];
"""
    for n in wf["nodes"]:
        if n.get("name") == "Stash Token":
            n["parameters"]["jsCode"] = new_stash
            print("  updated Stash Token to capture phone/customer_id/email")

    # 2. Drop prior copy of guard
    wf["nodes"] = [n for n in wf["nodes"] if n.get("name") != "Recoverable?"]
    for k in list(wf["connections"].keys()):
        if k == "Recoverable?":
            del wf["connections"][k]

    # 3. Insert "Recoverable?" between Stash Token and Wait 3h
    pos_stash = next((n["position"] for n in wf["nodes"] if n["name"] == "Stash Token"), [200, 300])
    if_pos = [pos_stash[0] + 220, pos_stash[1]]

    if_node = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [
                    {
                        "id": str(uuid.uuid4()),
                        "leftValue": "={{ $json.phone }}",
                        "rightValue": "",
                        "operator": {"type": "string", "operation": "notEmpty", "singleValue": True},
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "leftValue": "={{ $json.customer_id }}",
                        "rightValue": "",
                        "operator": {"type": "string", "operation": "notEmpty", "singleValue": True},
                    },
                ],
                "combinator": "or",  # need EITHER phone OR customer_id
            },
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": "Recoverable?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": if_pos,
    }
    wf["nodes"].append(if_node)

    # 4. Rewire: Stash Token → Recoverable? → (true) Wait 3h
    wait_targets = wf["connections"].get("Stash Token", {}).get("main", [[]])
    targets = list(wait_targets[0]) if wait_targets else []
    print(f"  existing 'Stash Token' targets: {[t['node'] for t in targets]}")

    wf["connections"]["Stash Token"] = {
        "main": [[{"node": "Recoverable?", "type": "main", "index": 0}]]
    }
    wf["connections"]["Recoverable?"] = {
        "main": [
            targets,  # TRUE: keep original (Wait 3h chain)
            [],       # FALSE: drop
        ]
    }

    s, body = n8n("PUT", "/workflows/SxeOUWpesKXMYOxR", put_payload(wf))
    print(f"  PUT → HTTP {s}")
    return s < 300


if __name__ == "__main__":
    import sys
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if only in ("all", "leads"):
        fix_leads_funnel()
    if only in ("all", "cart"):
        fix_abandoned_cart()

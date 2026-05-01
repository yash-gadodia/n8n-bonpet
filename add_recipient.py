#!/usr/bin/env python3
"""Generic: add a phone number as an additional broadcast recipient to all 'team broadcast' workflows.

Usage:  python3 add_recipient.py +6581114800

Idempotent: skips a workflow if the phone is already present.
"""
import json, uuid, os, sys, urllib.request, urllib.error, copy

API = "https://thebonpet.app.n8n.cloud/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()

# (workflow_id, source_node_name, source_branch_index, name_prefix)
# name_prefix is used to mint a fresh node name like {prefix} #N
TARGETS = [
    ("IF06B0WkxWB6UUfX", "Email Received?",         1, "HTTP Request"),
    ("zxoqtD2JQdEXvGff", "Format WhatsApp Message", 0, "Send WhatsApp #"),
]


def http(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_phone(node):
    bp = node.get("parameters", {}).get("bodyParameters", {}).get("parameters", [])
    val = next((p["value"] for p in bp if p.get("name") == "phone_number"), "")
    return str(val).lstrip("=")


def add(wf_id, source_node, branch_idx, name_prefix, phone):
    status, wf = http("GET", f"/workflows/{wf_id}")
    print(f"\n[{wf_id}] {wf.get('name')}  (HTTP {status})")

    wa_nodes = [n for n in wf["nodes"]
                if n.get("type") == "n8n-nodes-base.httpRequest"
                and "whatsapp/send" in n.get("parameters", {}).get("url", "")]
    if not wa_nodes:
        print("  ⚠️  no whatsapp send nodes found — skipping")
        return

    if any(get_phone(n) == phone for n in wa_nodes):
        print(f"  ⏭️  {phone} already present, skipping")
        return

    # find next free name like "{prefix}{N}" / "{prefix} #N"
    used_names = {n["name"] for n in wf["nodes"]}
    n = 1
    while True:
        candidates = [f"{name_prefix}{n}", f"{name_prefix}{n if not name_prefix.endswith('#') else n}"]
        # Picklist style: "HTTP Request", "HTTP Request1", "HTTP Request2", ...
        # Sales style:    "Send WhatsApp #1", "#2", ...
        cand = f"{name_prefix}{n}" if name_prefix.endswith("#") else f"{name_prefix}{n}"
        if cand not in used_names and (name_prefix not in used_names if n == 1 else True):
            new_name = cand
            break
        n += 1

    template = wa_nodes[-1]
    new_node = copy.deepcopy(template)
    new_node["id"] = str(uuid.uuid4())
    new_node["name"] = new_name
    new_node["position"] = [template["position"][0], template["position"][1] + 100]
    for p in new_node["parameters"]["bodyParameters"]["parameters"]:
        if p.get("name") == "phone_number":
            p["value"] = phone

    wf["nodes"].append(new_node)

    src_conn = wf["connections"].setdefault(source_node, {"main": []})
    while len(src_conn["main"]) <= branch_idx:
        src_conn["main"].append([])
    src_conn["main"][branch_idx].append({"node": new_name, "type": "main", "index": 0})

    payload = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": {"executionOrder": wf.get("settings", {}).get("executionOrder", "v1")},
    }
    status, body = http("PUT", f"/workflows/{wf_id}", payload)
    print(f"  added '{new_name}' → {phone}  PUT {status}")
    if status >= 300:
        print(f"  body: {body}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    phone = sys.argv[1].replace(" ", "")
    if not phone.startswith("+"):
        print("Phone must start with + (e.g. +6581114800)"); sys.exit(1)

    print(f"Adding {phone} to all team-broadcast workflows…")
    for args in TARGETS:
        add(*args, phone=phone)
    print("\nFor Low Stock Watcher: edit RECIPIENTS in build_low_stock.py and re-run it.")

#!/usr/bin/env python3
"""Add Rachel +6587993341 to broadcast workflows by cloning the existing 3rd send node."""
import json, uuid, os, urllib.request, urllib.error, copy

API = "https://thebonpet.app.n8n.cloud/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
RACHEL = "+6587993341"

WORKFLOWS = [
    # (workflow_id, source_node_name, source_branch_index, template_node_name, new_node_name, x_offset, y_offset)
    ("IF06B0WkxWB6UUfX", "Email Received?", 1, "HTTP Request2", "HTTP Request3", 0, 200),
    ("zxoqtD2JQdEXvGff", "Format WhatsApp Message", 0, "Send WhatsApp #3", "Send WhatsApp #4", 0, 200),
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


def patch_workflow(wf_id, source_node, branch_idx, template_name, new_name, dx, dy):
    status, wf = http("GET", f"/workflows/{wf_id}")
    print(f"\n[{wf_id}] {wf.get('name')}  (HTTP {status})")

    # already added?
    if any(n["name"] == new_name for n in wf["nodes"]):
        print(f"  ⏭️  {new_name} already exists, skipping")
        return

    template = next(n for n in wf["nodes"] if n["name"] == template_name)
    new_node = copy.deepcopy(template)
    new_node["id"] = str(uuid.uuid4())
    new_node["name"] = new_name
    new_node["position"] = [template["position"][0] + dx, template["position"][1] + dy]

    # rewrite phone — handle both string-value and bodyParameters
    bp = new_node["parameters"].get("bodyParameters", {}).get("parameters", [])
    for p in bp:
        if p.get("name") == "phone_number":
            p["value"] = RACHEL

    wf["nodes"].append(new_node)

    # add to source node's connection list
    src_conn = wf["connections"].setdefault(source_node, {"main": []})
    while len(src_conn["main"]) <= branch_idx:
        src_conn["main"].append([])
    src_conn["main"][branch_idx].append({"node": new_name, "type": "main", "index": 0})

    # strip non-PUT-able settings keys (public API only accepts executionOrder)
    settings = {"executionOrder": wf.get("settings", {}).get("executionOrder", "v1")}

    payload = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": settings,
    }
    status, body = http("PUT", f"/workflows/{wf_id}", payload)
    print(f"  PUT → HTTP {status}")
    if status >= 300:
        print(f"  body: {body}")


for args in WORKFLOWS:
    patch_workflow(*args)

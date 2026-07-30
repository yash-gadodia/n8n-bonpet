#!/usr/bin/env python3
"""Fix the Meta CAPI leg of "TBP GTM — Purchase + Subscription (server-side)".

Root cause (146 failures 18-30 Jul, every single run): Shopify Basic withholds
Protected Customer Data, so the orders/paid webhook delivers `email: null`. That
made `email_norm` empty, so the guard `email_norm ? {em:[...]} : {}` correctly
refused to send the empty-string hash and shipped `user_data: {}` instead. Meta
rejects any event with no customer identifier -> HTTP 400 "Invalid parameter".
GA4 was unaffected because it only needs client_id.

Fix: use the identifiers the webhook DOES carry, which were sitting unused in the
same payload - Shopify `customer.id` (hashed -> external_id), `browser_ip`
(client_ip_address) and `client_details.user_agent` (client_user_agent). Approved
by Yash 2026-07-30 as a deliberate data-sharing choice.

Hashing rules matter and are easy to get backwards: em and external_id MUST be
SHA-256 hex; client_ip_address and client_user_agent MUST be sent PLAIN. Hashing
the latter two silently destroys match quality rather than erroring.

The empty-string hash (e3b0c442...) must never reach Meta: it would be an
identical bogus "customer" on every order and would poison match quality, so em
is still only included when a real email exists.
"""
import json, os, urllib.request, urllib.error, uuid

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
WF = "3UwBuHSH7PiWVhM2"
HASH_EXT = "Hash External ID (SHA256)"


def http(m, p, b=None):
    r = urllib.request.Request(API + p, data=json.dumps(b).encode() if b is not None else None,
        method=m, headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(r, timeout=90) as x:
            return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# user_data built so it can never be empty: external_id is always present (falls
# back to the generated client_id), ip/ua ride along plain when the webhook has them.
USER_DATA = (
    "user_data: Object.assign("
    "{ external_id: [ $json.ext_id ] },"
    " ($json.email_norm ? { em: [ $json.em ] } : {}),"
    " ($json.client_ip_address ? { client_ip_address: $json.client_ip_address } : {}),"
    " ($json.client_user_agent ? { client_user_agent: $json.client_user_agent } : {})"
    ")"
)

if __name__ == "__main__":
    s, w = http("GET", f"/workflows/{WF}")
    if s != 200:
        raise SystemExit(f"GET failed {s}: {w}")
    json.dump(w, open(os.path.expanduser("~/n8n-bonpet/snapshots/tbp_gtm_prod.json"), "w"), indent=2)
    print("snapshot -> snapshots/tbp_gtm_prod.json")

    nodes = {n["name"]: n for n in w["nodes"]}

    # 1. carry the identifiers out of the webhook
    derive = nodes["Derive GA4/Meta Event"]
    code = derive["parameters"]["jsCode"]
    old_base = "const base = { client_id: cid, email_norm, transaction_id };"
    new_base = (
        "// Shopify Basic withholds email/phone, so Meta needs the identifiers the\n"
        "// webhook DOES carry. ext_id_raw gets SHA-256'd downstream; ip/ua stay PLAIN\n"
        "// (hashing those two silently kills match quality instead of erroring).\n"
        "const cd = body.client_details || {};\n"
        "const ext_id_raw = String((body.customer && body.customer.id) || cid);\n"
        "const client_ip_address = body.browser_ip || cd.browser_ip || '';\n"
        "const client_user_agent = cd.user_agent || '';\n"
        "const base = { client_id: cid, email_norm, transaction_id,\n"
        "               ext_id_raw, client_ip_address, client_user_agent };"
    )
    if old_base not in code:
        raise SystemExit("anchor 'const base = ...' not found - inspect the node before rerunning")
    derive["parameters"]["jsCode"] = code.replace(old_base, new_base, 1)

    # 2. hash external_id (Meta wants SHA-256 hex, same as em)
    hash_email = nodes["Hash Email (SHA256)"]
    if HASH_EXT not in nodes:
        hx, hy = hash_email["position"]
        ext = {
            "parameters": {"action": "hash", "type": "SHA256",
                           "value": "={{ $json.ext_id_raw }}",
                           "dataPropertyName": "ext_id", "encoding": "hex"},
            "id": str(uuid.uuid4()), "name": HASH_EXT,
            "type": "n8n-nodes-base.crypto", "typeVersion": 1,
            "position": [hx + 200, hy],
        }
        w["nodes"].append(ext)
        # rewire: Hash Email -> Hash External ID -> Meta CAPI
        w["connections"]["Hash Email (SHA256)"] = {
            "main": [[{"node": HASH_EXT, "type": "main", "index": 0}]]}
        w["connections"][HASH_EXT] = {
            "main": [[{"node": "Meta CAPI", "type": "main", "index": 0}]]}

    # 3. build a user_data that always carries at least one identifier
    capi = nodes["Meta CAPI"]
    jb = capi["parameters"]["jsonBody"]
    old_ud = "user_data: ($json.email_norm ? { em: [ $json.em ] } : {})"
    if old_ud not in jb:
        raise SystemExit("anchor 'user_data: ...' not found - inspect the Meta CAPI node")
    capi["parameters"]["jsonBody"] = jb.replace(old_ud, USER_DATA, 1)

    payload = {"name": w["name"], "nodes": w["nodes"],
               "connections": w["connections"], "settings": w.get("settings") or {}}
    s, b = http("PUT", f"/workflows/{WF}", payload)
    print(f"PUT /workflows/{WF} -> {s}")
    if s != 200:
        print(str(b)[:600]); raise SystemExit(1)
    print("✅ patched. Verify on the next real order, or replay a failed execution.")

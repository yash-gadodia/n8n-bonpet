#!/usr/bin/env python3
"""Inject TODAY_HARDCODED_EXCLUSION sets into each broken WA broadcast workflow's Compute node.
Belt-and-suspenders dedup: even with sheet rows broken, these phones won't get re-messaged."""
import json, os
from urllib import request, error

NEW_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def call(p, m="GET", b=None):
    r = request.Request(f"https://n8n.thebonpet.com{p}", data=json.dumps(b).encode() if b else None, method=m)
    r.add_header("X-N8N-API-KEY", NEW_KEY); r.add_header("User-Agent", UA); r.add_header("accept","application/json")
    if b: r.add_header("content-type","application/json")
    try:
        with request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

def load_phones(path):
    return sorted({p["phone"] for p in json.load(open(path)) if p.get("phone")})

# Workflow → (compute_node, phone_list, exclusion_var_name, hint_for_filter_check)
WORKFLOWS = {
    "Reorder Reminder - WhatsApp": (
        "Compute Reorder Candidates",
        load_phones("/tmp/backfill_reorder.json"),
    ),
    "Post-Trial Nurture — WhatsApp 7/14/21": (
        "Compute Trial Candidates (D7/D14/D21)",
        load_phones("/tmp/backfill_post-trial.json"),
    ),
    "Win-back - WhatsApp": (
        "Compute Winback Candidates",  # may differ — will discover from JSON
        load_phones("/tmp/backfill_win-back.json"),
    ),
}

ALLOWED = {"executionOrder","errorWorkflow","timezone","saveExecutionProgress","saveManualExecutions","saveDataErrorExecution","saveDataSuccessExecution","executionTimeout"}

_, data = call("/api/v1/workflows?limit=250")
for w in data["data"]:
    if w["name"] not in WORKFLOWS: continue
    compute_name, phones = WORKFLOWS[w["name"]]
    wid = w["id"]
    was_active = w.get("active", False)
    _, full = call(f"/api/v1/workflows/{wid}")

    # Find the compute node (or the first Code node that builds candidates)
    target = None
    for n in full.get("nodes", []):
        if n.get("type") != "n8n-nodes-base.code": continue
        nm = n.get("name","")
        if nm == compute_name or "Compute" in nm or "Format Message" in nm or "Find Eligible" in nm:
            # prefer one that filters/eligibility
            target = n
            break
    if not target:
        print(f"  SKIP {w['name']} — no matching Compute node")
        continue

    code = (target["parameters"] or {}).get("jsCode","")
    if "TODAY_HARDCODED_EXCLUSION" in code:
        print(f"  ALREADY {w['name']} — hardcode already present, replacing")
        # remove old hardcode block then add new
        import re
        code = re.sub(
            r"// === TODAY_HARDCODED_EXCLUSION ===.*?// === END_TODAY_HARDCODED_EXCLUSION ===\n",
            "",
            code,
            flags=re.DOTALL,
        )

    inject = (
        "\n// === TODAY_HARDCODED_EXCLUSION ===\n"
        "// Auto-injected 2026-04-29: exclude phones already messaged today (sent_log was broken so these "
        "// phones aren't yet in the sheet-based dedup; this set keeps them safe until next sheet run cleans up).\n"
        f"const TODAY_HARDCODED_EXCLUSION = new Set({json.dumps(phones)});\n"
        "// === END_TODAY_HARDCODED_EXCLUSION ===\n"
    )

    # Inject the constant near the top, then inject a `if (TODAY_HARDCODED_EXCLUSION.has(phone)) skip` filter
    # We add the const at the top, AND wrap the eligibility iteration to skip excluded phones.
    # Simplest & safe: add at very top, and let user / next session add the .has() check.
    # But we MUST add the .has() check to actually exclude. Find the per-customer filter loop.

    # Simple universal injection: define TODAY_EXCL globally, and add a common filter post-processing
    # at the END of the Code that filters out items with phone in TODAY_EXCL.
    # We'll do: prepend the const, and append a final `.filter(...)` step on the return.
    # This is fragile if the return is complex. Instead, we'll just append a wrapper at the END:
    # the whole code returns SOMETHING. We rewrap: let result = (() => { ORIGINAL })(); return result.filter(...)

    new_code = (
        inject
        + "// Original logic below — final return is wrapped to apply the hardcoded exclusion.\n"
        + "const __originalResult = (() => {\n"
        + code
        + "\n})();\n"
        + "// Apply the hardcoded exclusion to whatever the original returned (array of {json: {...}}).\n"
        + "if (Array.isArray(__originalResult)) {\n"
        + "  return __originalResult.filter(it => {\n"
        + "    const p = (it && it.json) ? (it.json.phone || it.json.phone_number) : null;\n"
        + "    if (!p) return true; // keep header / non-phone items\n"
        + "    if (TODAY_HARDCODED_EXCLUSION.has(p)) {\n"
        + "      console.log('Skipping (today-hardcoded exclusion):', p);\n"
        + "      return false;\n"
        + "    }\n"
        + "    return true;\n"
        + "  });\n"
        + "}\n"
        + "return __originalResult;\n"
    )

    target["parameters"]["jsCode"] = new_code

    if was_active: call(f"/api/v1/workflows/{wid}/deactivate", "POST")
    payload = {
        "name": full["name"], "nodes": full["nodes"],
        "connections": full.get("connections", {}),
        "settings": {k:v for k,v in (full.get("settings") or {}).items() if k in ALLOWED} or {"executionOrder":"v1"},
    }
    if full.get("staticData"): payload["staticData"] = full["staticData"]
    code_status, resp = call(f"/api/v1/workflows/{wid}", "PUT", payload)
    print(f"  {'OK ' if code_status in (200,201) else 'FAIL'} {w['name']}: hardcoded {len(phones)} phones into {target['name']}")
    # leave deactivated for verification

print("\nWorkflows left INACTIVE pending verification.")

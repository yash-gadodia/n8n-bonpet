#!/usr/bin/env python3
"""Drop the 50% sampler offer from the day-0 meal-plan comms (TBP-176).

Why: Launch Cycle (Siva) flagged that a discount on first contact trains customers
to wait for codes. The meal-plan-ready WhatsApp + email were offering
SAMPLER<3THEBONPET before the lead had seen full price. The sampler offer still
goes out on D7 via Meal Plan Follow-up (NJ3EctLBcHhaWV3I), so day-0 now only
pushes "Finish your order".

Edits only the "Compose Comms" Code node, by exact string replacement against the
live workflow (UI edits since are preserved). Snapshot first, re-GET to verify.

    python3 patch_meal_planner_day0_no_discount.py            # dry run
    python3 patch_meal_planner_day0_no_discount.py --apply
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
WF_ID = "Athof1GaI7i0l441"  # TBP Meal Planner Leads
NODE = "Compose Comms"
HERE = os.path.dirname(os.path.abspath(__file__))

REPLACEMENTS = [
    ("const SAMPLER_CODE = 'SAMPLER<3THEBONPET';\n"
     "// Auto-apply discount URL: tapping sets the code, then lands on the product with UTMs intact.\n"
     "// NOTE plural '-for-dogs': the singular handle store-redirects and strips the query.\n"
     "const samplerLink = (sp, content, medium) => {\n"
     "  const path = (sp === 'cat') ? '/products/gently-cooked-sampler-set-for-cats' : '/products/gently-cooked-sampler-set-for-dogs';\n"
     "  return 'https://thebonpet.com/discount/SAMPLER%253C3THEBONPET?redirect=' + encodeURIComponent(path + '?' + qs(utm(medium || 'email', content)));\n"
     "};\n", ""),
    ("const samplerTextLink = samplerLink(species, 'sampler-' + species, 'email');\n", ""),
    ("\n  + '\\n\\nNot ready for the full plan? Tap for a sampler set at 50% off, the discount auto-applies (or use code ' + SAMPLER_CODE + '): ' + samplerTextLink", ""),
    ("const waSamplerLink = samplerLink(species, 'sampler-' + species, 'whatsapp');\n", ""),
    ("\n  + '\\n\\nNot ready for the full plan? Try a sampler set at 50% off, one of each protein. Tap and the discount auto-applies at checkout: ' + waSamplerLink", ""),
    ("const packsImg = species === 'cat' ? IMG.packsCat : IMG.packsDog;\n", ""),
    (",\n      'Or start smaller: the sampler set is one pack of each protein, and new customers get it at 50% off.'])", "])"),
]
# The sampler card in the HTML body: cut from its opening table to the end of the body expression.
CARD_START = "\n  + '<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" style=\"margin:0 0 8px 0;\"><tr><td width=\"150\""
CARD_END = "+ '</td></tr></table>';\n"
PREHEADER_OLD = "Finish the order or start with a sampler at 50% off."
PREHEADER_NEW = "Finish the order whenever you are ready."


def http(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def patch(js):
    for old, new in REPLACEMENTS:
        assert js.count(old) == 1, f"anchor not found exactly once: {old[:70]!r}"
        js = js.replace(old, new)
    i = js.index(CARD_START)
    j = js.index(CARD_END, i)
    js = js[:i] + ";\n" + js[j + len(CARD_END):]
    assert js.count(PREHEADER_OLD) == 1
    js = js.replace(PREHEADER_OLD, PREHEADER_NEW)
    assert "SAMPLER" not in js and "50% off" not in js and "sampler" not in js.lower(), "sampler text still present"
    return js


def main():
    status, wf = http("GET", f"/workflows/{WF_ID}")
    assert status == 200, wf
    node = next(n for n in wf["nodes"] if n["name"] == NODE)
    new_js = patch(node["parameters"]["jsCode"])
    if "--apply" not in sys.argv:
        print(new_js)
        print("\n(dry run, pass --apply)")
        return
    os.makedirs(os.path.join(HERE, ".snapshots"), exist_ok=True)
    snap = os.path.join(HERE, ".snapshots", f"{WF_ID}-{datetime.now():%Y%m%d-%H%M%S}-pre-day0-no-discount.json")
    json.dump(wf, open(snap, "w"))
    node["parameters"]["jsCode"] = new_js
    payload = {"name": wf["name"], "nodes": wf["nodes"], "connections": wf["connections"],
               "settings": wf.get("settings") or {}}
    status, body = http("PUT", f"/workflows/{WF_ID}", payload)
    assert status == 200, f"PUT {status}: {str(body)[:600]}"
    _, live = http("GET", f"/workflows/{WF_ID}")
    live_js = next(n for n in live["nodes"] if n["name"] == NODE)["parameters"]["jsCode"]
    assert live_js == new_js and live["active"], "live workflow does not match"
    print(f"✅ patched + verified live (active={live['active']}). snapshot: {os.path.relpath(snap, HERE)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Add GA4 UTM tags to customer-facing links in the live lifecycle workflows.

Convention (matches the meal-planner flows already live):
    utm_medium=whatsapp · utm_source=<flow> · utm_campaign=<variant> · utm_content=<link role>

Patched workflows (live JSON is source of truth; snapshots taken before write):
    SUuwJMm0R6gNzXnm  Reorder Reminder       cart permalink        reminder-1 / reminder-2
    PHnGZ0zVIX5knHg5  Abandoned Cart Sweeper checkout recovery URL sweeper
    ZyQBmsJXRyjOmxrE  Checkout Recovery      recovery URL          sampler-cart / first-order / returning
    G33gVYy7VmvNHd4d  Sub Reactivation       WELCOMEBACK link      paused / cancelled (via ?redirect=)
    aHp12XVEld1s1ZBP  Subscription Save      WELCOMEBACK link      cancelled (via ?redirect=)

Verified 2026-08-04: cart permalinks and /discount/...?redirect= both carry UTMs
through Shopify's redirects (curl-tested). Bare /discount/ links land untagged.

NB: source escape styles differ per workflow. Sub Reactivation stores templates
with literal \\uXXXX + \\n escape sequences; Checkout Recovery uses \\u{...} + \\n;
the rest use real newlines. Patterns below match the live source exactly.

Usage:
    python3 patch_utm_links.py           # dry run: check every pattern matches
    python3 patch_utm_links.py apply     # snapshot + PUT + verify
    RESTORE=1 python3 patch_utm_links.py # restore all snapshots
"""
import json, os, sys, urllib.request
from pathlib import Path

BASE = "https://n8n.thebonpet.com/api/v1"
KEY = Path("~/.n8n-bonpet-newkey").expanduser().read_text().strip()
SNAP = Path(__file__).parent / "snapshots"
SNAP.mkdir(exist_ok=True)

def req(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())

WB = "https://thebonpet.com/discount/WELCOMEBACK%253C3THEBONPET"
def wb(source, campaign):
    return (WB + "?redirect=%2F%3Futm_source%3D" + source +
            "%26utm_medium%3Dwhatsapp%26utm_campaign%3D" + campaign +
            "%26utm_content%3Dwelcomeback-20")

def utm_helper(name, source, content):
    return ("const " + name + " = (u, c) => u + (u.indexOf('?') >= 0 ? '&' : '?') + "
            "'utm_source=" + source + "&utm_medium=whatsapp&utm_campaign=' + c + "
            "'&utm_content=" + content + "';")

PATCHES = [
    ("SUuwJMm0R6gNzXnm", "Compute Reorder Candidates", [
        ("  const cartLink = last.cart_link || 'https://thebonpet.com/collections/all';",
         "  const cartLink = last.cart_link || 'https://thebonpet.com/collections/all';\n  " +
         utm_helper("__utm", "reorder-reminder", "cart")),
        ("msg3 = `easy reorder if useful 🛒 ${cartLink} - or just lmk if you need a hand 💛`;",
         "msg3 = `easy reorder if useful 🛒 ${__utm(cartLink, 'reminder-1')} - or just lmk if you need a hand 💛`;"),
        ("msg3 = `easy reorder when ready 🛒 ${cartLink} - any qs just reply 💛`;",
         "msg3 = `easy reorder when ready 🛒 ${__utm(cartLink, 'reminder-2')} - any qs just reply 💛`;"),
    ]),
    ("PHnGZ0zVIX5knHg5", "Compute Candidates", [
        ("  const cartUrl = String(ck.abandoned_checkout_url || '').trim();\n"
         "  if (!cartUrl) { stats.no_cart_link++; continue; }",
         "  const cartUrl0 = String(ck.abandoned_checkout_url || '').trim();\n"
         "  if (!cartUrl0) { stats.no_cart_link++; continue; }\n"
         "  const cartUrl = cartUrl0 + (cartUrl0.indexOf('?') >= 0 ? '&' : '?') + "
         "'utm_source=cart-sweeper&utm_medium=whatsapp&utm_campaign=sweeper&utm_content=recovery';"),
    ]),
    ("ZyQBmsJXRyjOmxrE", "Parse & Gate", [
        ("  const recoveryUrl = d.recovery_url || 'https://thebonpet.com/cart';",
         "  const recoveryUrl = d.recovery_url || 'https://thebonpet.com/cart';\n  " +
         utm_helper("__ru0", "checkout-recovery", "recovery") +
         "\n  const __ru = (c) => __ru0(recoveryUrl, c);"),
        ("takes 50% off \\u{1F389}\\nFinish up here: ${recoveryUrl}",
         "takes 50% off \\u{1F389}\\nFinish up here: ${__ru('sampler-cart')}"),
        ("for 30% off total!\\nFinish up here: ${recoveryUrl}",
         "for 30% off total!\\nFinish up here: ${__ru('first-order')}"),
        ("Pick up where you left off: ${recoveryUrl}",
         "Pick up where you left off: ${__ru('returning')}"),
    ]),
    ("G33gVYy7VmvNHd4d", "Find Eligible Customers", [
        ("own here \\ud83d\\udc9b\\n" + WB,
         "own here \\ud83d\\udc9b\\n" + wb("sub-reactivation", "paused")),
        ("(whenever you fancy):\\n" + WB,
         "(whenever you fancy):\\n" + wb("sub-reactivation", "cancelled")),
    ]),
    ("aHp12XVEld1s1ZBP", "Lookup + Format", [
        ("(whenever you fancy):\n" + WB,
         "(whenever you fancy):\n" + wb("sub-save", "cancelled")),
    ]),
]

def restore():
    for f in sorted(SNAP.glob("utm_patch_*.json")):
        wf = json.loads(f.read_text())
        wid = f.stem.replace("utm_patch_", "")
        req("PUT", f"/workflows/{wid}", {k: wf[k] for k in ("name", "nodes", "connections", "settings")})
        print(f"restored {wid} ({wf['name']})")

def main(apply):
    all_ok = True
    for wid, node_name, subs in PATCHES:
        wf = req("GET", f"/workflows/{wid}")
        node = next((n for n in wf["nodes"] if n["name"] == node_name), None)
        if not node:
            print(f"❌ {wid}: node {node_name!r} not found"); all_ok = False; continue
        js = node["parameters"]["jsCode"]
        ok = True
        for old, new in subs:
            c = js.count(old)
            if c != 1:
                print(f"❌ {wf['name']} [{node_name}]: {c} matches (need 1): {old[:60]!r}")
                ok = all_ok = False
        if not ok:
            continue
        for old, new in subs:
            js = js.replace(old, new)
        print(f"✓ {wf['name']}: {len(subs)} replacement(s) ready" + ("" if apply else " (dry run)"))
        if apply:
            snap = SNAP / f"utm_patch_{wid}.json"
            if not snap.exists():
                snap.write_text(json.dumps(wf, ensure_ascii=False))
            node["parameters"]["jsCode"] = js
            req("PUT", f"/workflows/{wid}", {k: wf[k] for k in ("name", "nodes", "connections", "settings")})
            check = req("GET", f"/workflows/{wid}")
            cjs = next(n for n in check["nodes"] if n["name"] == node_name)["parameters"]["jsCode"]
            verified = all(new in cjs for _, new in subs)
            print(f"  PUT ok · active={check['active']} · patterns live: {'✓' if verified else '❌ MISMATCH'}")
            if not verified or not check["active"]:
                all_ok = False
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    if os.environ.get("RESTORE"):
        restore(); sys.exit()
    main(len(sys.argv) > 1 and sys.argv[1] == "apply")

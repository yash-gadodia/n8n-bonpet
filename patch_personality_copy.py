#!/usr/bin/env python3
"""Personality touches on live customer-facing n8n copy (Launch Cycle review, Aug 2026).

Surgical additions only; utility text, gating and links untouched. The n8n fleet
already speaks founder-casual, so this is 4 small touches, not a rewrite:

    JgH4FFgotZSbNFFL  Win-back        "(still doing zoomies? 🐾)"
    VR7jZxPaiRCwdIaP  Post-Trial D7   "any happy dances at mealtime?"
    PHnGZ0zVIX5knHg5  Cart Sweeper    "(we checked with your furkid, they're in favour 🐾)"
    xSJYBYH6DpwzQGkn  Pickup Ready    Siglap freezer line

Deliberately untouched: delivery exceptions, sub save (sincere by design),
review responses, reorder + nurture bursts (already handwritten-casual).

Usage:  python3 patch_personality_copy.py [apply]   ·   RESTORE=1 to roll back
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

PATCHES = [
    ("JgH4FFgotZSbNFFL", "Format Message", [
        ("just thinking of you and your furkid, how have they been?",
         "just thinking of you and your furkid, how have they been? (still doing zoomies? 🐾)"),
    ]),
    ("VR7jZxPaiRCwdIaP", "Compute Trial Candidates (D7/D14/D21)", [
        ("just wanted to check in - how's your furkid doing? 🐾 would love to hear back from u",
         "just wanted to check in - how's your furkid doing, any happy dances at mealtime? 🐾 would love to hear back from u"),
    ]),
    ("PHnGZ0zVIX5knHg5", "Compute Candidates", [
        ("🛒 ${cartUrl}${itemsLine}\n\nAnything holding you back?",
         "🛒 ${cartUrl}${itemsLine}\n(we checked with your furkid, they're in favour 🐾)\n\nAnything holding you back?"),
    ]),
    ("xSJYBYH6DpwzQGkn", "Build WA Message", [
        ("`ℹ️ Please skip the *ORANGE freezer*, it's not part of the pickup`,",
         "`ℹ️ Please skip the *ORANGE freezer*, it's not part of the pickup`,\n    `🧊 (your furkid's dinner is literally chilling in there, waiting for you)`,"),
    ]),
]

def restore():
    for f in sorted(SNAP.glob("personality_*.json")):
        wf = json.loads(f.read_text())
        wid = f.stem.replace("personality_", "")
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
        bad = [old for old, _ in subs if js.count(old) != 1]
        if bad:
            print(f"❌ {wf['name']}: pattern not unique/found: {bad[0][:60]!r}"); all_ok = False; continue
        for old, new in subs:
            js = js.replace(old, new)
        print(f"✓ {wf['name']}: {len(subs)} touch(es) ready" + ("" if apply else " (dry run)"))
        if apply:
            snap = SNAP / f"personality_{wid}.json"
            if not snap.exists():
                snap.write_text(json.dumps(wf, ensure_ascii=False))
            node["parameters"]["jsCode"] = js
            req("PUT", f"/workflows/{wid}", {k: wf[k] for k in ("name", "nodes", "connections", "settings")})
            check = req("GET", f"/workflows/{wid}")
            cjs = next(n for n in check["nodes"] if n["name"] == node_name)["parameters"]["jsCode"]
            ok = all(new in cjs for _, new in subs) and check["active"]
            print(f"  PUT ok · active={check['active']} · live: {'✓' if ok else '❌'}")
            all_ok = all_ok and ok
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    if os.environ.get("RESTORE"):
        restore(); sys.exit()
    main(len(sys.argv) > 1 and sys.argv[1] == "apply")

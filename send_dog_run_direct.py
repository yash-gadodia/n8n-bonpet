#!/usr/bin/env python3
"""Send Sunday Paws Club dog run invite DIRECTLY via WA API, bypassing n8n.

n8n Cloud is out of monthly executions on 2026-04-26 — this is a one-off
fallback to drain the remaining dog-owner pool today since the dog run
is on 3 May (7 days away).

Audience: customers with 1+ dog orders in the local Feb 2026 order export,
sorted by most-recent dog order, hard-excluding every phone in the existing
~/n8n-bonpet/dog_run_*_sent_phones.json files (Apr 20 + Apr 26 morning).

After the blast, saves successful sends to dog_run_apr26b_sent_phones.json
so future n8n-driven rounds auto-exclude via the glob in build_dog_run_invite.py.
"""
import csv
import subprocess
import glob
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime

WA_URL = "https://api.thebonpet.com/whatsapp/send"
WA_KEY = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","wa-api-key","-w"]).decode().strip()
YASH_PHONE = "+6581394225"

ORDER_CSV = os.path.expanduser(
    "~/Documents/TheBonPet/exports/orders/export all order feb 2026.csv"
)
PRIOR_SENT_GLOB = os.path.expanduser("~/n8n-bonpet/dog_run_*_sent_phones.json")
OUT_FILE = os.path.expanduser("~/n8n-bonpet/dog_run_apr26b_sent_phones.json")

DELAY_SEC = 1.0    # Apr 26 n8n run did 0.11s/send with 100% success; 1s is conservative
TEST_FIRST = True  # send 1 smoke test to Yash before the blast


def norm_phone(p):
    if not p:
        return ""
    s = re.sub(r"[^\d+]", "", str(p))
    if s.startswith("+65") and len(s) == 11:
        return s
    if s.startswith("65") and len(s) == 10:
        return "+" + s
    if len(s) == 8 and s[0] in "89":
        return "+65" + s
    if s.startswith("+") and len(s) >= 10:
        return s
    return ""


def build_message(first_name: str) -> str:
    g = f"hellooo {first_name}!! 🐾" if first_name else "hellooo pawrents!! 🐾"
    return (
        f"{g}\n\n"
        "something exciting to share 🥹 The Bon Pet team is organising a Sunday Paws Club doggy hangout on 3rd May (Sun) @ ECP!\n\n"
        "it's gonna be a chill morning for the pups to play, the pawrents to meet new dog families, and we'll be preparing yummy treats for the furballs! we've also got a partner bringing sweet treats for us humans too 👀🍪 buffet vibes!!\n\n"
        "if u + ur pup are keen, fill in this quick form to secure ur slot + goodie bag 💛\n"
        "👉 https://docs.google.com/forms/d/e/1FAIpQLSetIjvcCzto0-Y3DVl_pHv3FEta0xi1BtfZerqgpqIFSldZWw/viewform\n\n"
        "hope to see u there!! 🐶✨\n\n"
        "❤️ The Bon Pet team"
    )


def send_wa(phone: str, message: str):
    body = json.dumps({"phone_number": phone, "message": message}).encode()
    req = urllib.request.Request(
        WA_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": WA_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, e.reason
    except Exception as e:
        return 0, str(e)


def load_excluded():
    files = sorted(glob.glob(PRIOR_SENT_GLOB))
    excl = set()
    for fp in files:
        for p in json.load(open(fp)):
            excl.add(p)
    return excl, files


def find_eligible(excl: set):
    dog = re.compile(r"for Dog", re.IGNORECASE)
    custs = {}
    with open(ORDER_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not dog.search(row.get("Lineitem name", "")):
                continue
            email = (row.get("Email") or "").strip().lower()
            if not email:
                continue
            phone = (row.get("Billing Phone") or row.get("Shipping Phone") or "").strip()
            name = (row.get("Billing Name") or row.get("Shipping Name") or "").strip()
            first = name.split()[0] if name else ""
            created = (row.get("Created at") or "").strip()
            try:
                dt = datetime.fromisoformat(
                    created.replace(" +0800", "+08:00").replace(" ", "T")[:25]
                )
            except Exception:
                dt = None
            c = custs.setdefault(email, {"first": first, "phone": phone, "last": dt, "n": 0})
            c["n"] += 1
            if dt and (c["last"] is None or dt > c["last"]):
                c["last"] = dt
            if not c["phone"] and phone:
                c["phone"] = phone
            if not c["first"] and first:
                c["first"] = first

    out = []
    for email, c in custs.items():
        e164 = norm_phone(c["phone"])
        if not e164.startswith("+65"):
            continue
        if e164 in excl:
            continue
        if c["last"] is None:
            continue
        out.append(
            {
                "first": c["first"],
                "phone": e164,
                "email": email,
                "last_dt": c["last"],
                "n": c["n"],
            }
        )
    # Dedupe by phone (some emails share a phone), keep most recent
    by_phone = {}
    for r in out:
        prev = by_phone.get(r["phone"])
        if prev is None or r["last_dt"] > prev["last_dt"]:
            by_phone[r["phone"]] = r
    deduped = list(by_phone.values())
    deduped.sort(key=lambda r: r["last_dt"], reverse=True)
    return deduped


def main():
    excl, prior_files = load_excluded()
    print(f"Excluding {len(excl)} prior-sent phones from: {[os.path.basename(f) for f in prior_files]}")

    eligible = find_eligible(excl)
    print(f"Eligible (1+ dog orders, valid SG, not prior-sent): {len(eligible)}")
    if not eligible:
        print("Nothing to send. Bye.")
        return

    print(f"\nDate range of eligible: {eligible[-1]['last_dt'].date()} → {eligible[0]['last_dt'].date()}")
    print(f"Top 5 (most recent): {[(e['first'], e['phone']) for e in eligible[:5]]}")

    if TEST_FIRST:
        print(f"\n🧪 Smoke test → {YASH_PHONE}")
        status, body = send_wa(YASH_PHONE, build_message("Yash"))
        print(f"   HTTP {status}: {str(body)[:200]}")
        if not (status == 200 and isinstance(body, dict) and body.get("success")):
            print("❌ Smoke test FAILED — aborting blast. Fix and rerun.")
            return
        print("   ✅ OK. Pausing 5s before blast…")
        time.sleep(5)

    sent, failed = [], []
    n = len(eligible)
    print(f"\n📬 Blasting {n} dog owners (delay {DELAY_SEC}s/msg, ~{int(n * DELAY_SEC / 60)} min total)…\n")
    for i, c in enumerate(eligible, 1):
        msg = build_message(c["first"])
        status, body = send_wa(c["phone"], msg)
        ok = status == 200 and isinstance(body, dict) and body.get("success")
        tag = "✅" if ok else "❌"
        print(f"  [{i:>3}/{n}] {tag} {c['phone']:14} {(c['first'] or '(no name)')[:15]:15} HTTP {status}")
        if ok:
            sent.append(c["phone"])
        else:
            failed.append({"phone": c["phone"], "first": c["first"], "status": status, "body": str(body)[:200]})
            # If we get 5+ consecutive failures, abort to investigate
            if len(failed) >= 5 and all(f["status"] != 200 for f in failed[-5:]):
                print(f"\n⚠️  5 consecutive failures — aborting at {i}/{n} to investigate.")
                break
        if i < n:
            time.sleep(DELAY_SEC)

    # Persist successful sends so future n8n rounds auto-exclude via glob
    if sent:
        unique = sorted(set(sent))
        json.dump(unique, open(OUT_FILE, "w"), indent=2)
        print(f"\n💾 Saved {len(unique)} unique sent phones → {OUT_FILE}")

    print(f"\n📊 Done: {len(sent)} sent / {len(failed)} failed (out of {n} eligible)")
    if failed:
        print("\nFailures (first 20):")
        for f in failed[:20]:
            print(f"   {f}")


if __name__ == "__main__":
    main()

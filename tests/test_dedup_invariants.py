#!/usr/bin/env python3
"""Dedup / anti-spam invariants for every build_*.py workflow generator.

Guards against the two re-spam bug classes found on 2026-05-30:

  1. Win-back: a Google Sheets dedup-log node had sheetName.value = "gid=1248726917"
     (a STRING) instead of the numeric gid 1248726917. n8n read it literally, the node
     threw "Sheet ... not found", which HALTED the run before the sibling global-sent log
     wrote. Nothing was logged -> the same lapsed customers re-qualified every day -> 6 days
     of daily spam.

  2. Sub Reactivation: the dedup-log code node read $input.all() fed by the WA-send
     httpRequest node, whose response REPLACES the item json. The logged rows therefore
     contained {success, message_id, message} and NO phone/customer_id/contract_id, so the
     "already messaged" set was always empty -> silent daily re-spam since it went live.

These tests build every workflow from source (no network, no deploy) and assert the
invariants that make both classes impossible. Run before every deploy.

Run: python3 tests/test_dedup_invariants.py   (exit 0 = all pass, 1 = failure)
"""
import glob
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # so build scripts can import sibling helpers (_notify, _sent_log, ...)

failures = []
def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    if not ok:
        failures.append(f"{name}: {detail}")

# Import-safe sender scripts: deploy is guarded by `if __name__ == "__main__"` AND they
# expose a pure build() returning the payload. NEVER add a script that runs deploy/HTTP at
# import time (e.g. build_all_shopify_ingests, build_reorder_reminder_v2, build_post_trial_nurture)
# or importing it here would fire a live mutation. New customer-facing sender? Give it a
# guarded build() and add it here.
# NOTE: build_dog_run_invite.py and build_trial_graduation.py currently have a SyntaxError
# (a stray `import subprocess` injected inside a `from _sent_log import (...)` block) and
# cannot be imported - both are off/one-off. Excluded here; fix the source to re-add them.
SENDER_SCRIPTS = [
    "build_winback.py", "build_sub_reactivation.py", "build_review_watcher.py",
    "build_reorder_reminder.py", "build_abandoned_cart.py",
]

def build_senders():
    payloads, errors = {}, {}
    for fn in SENDER_SCRIPTS:
        path = os.path.join(ROOT, fn)
        mod = os.path.splitext(fn)[0]
        try:
            spec = importlib.util.spec_from_file_location(mod, path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            wf = m.build()
            if isinstance(wf, dict) and "nodes" in wf:
                payloads[wf.get("name", mod)] = (mod, wf)
        except BaseException as e:  # noqa - catch SystemExit too
            errors[mod] = repr(e)
    return payloads, errors

# ── Invariant 0: import-free source-text scan of EVERY build_*.py for the literal bug ──
def inv_source_no_gid_string_literal():
    """Catches the winback typo in ANY build script, even ones we can't safely import.
    The bug was: "value": f"gid={WINBACK_SENT_TAB_GID}"  (a gid=-prefixed STRING literal)."""
    bad = []
    pat = re.compile(r'["\']value["\']\s*:\s*f?["\']gid=')   # "value": "gid=...  or  f"gid=...
    for path in sorted(glob.glob(os.path.join(ROOT, "build_*.py"))):
        src = open(path, encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), 1):
            if pat.search(line):
                bad.append(f"{os.path.basename(path)}:{i}: {line.strip()[:90]}")
    check("no gid= string sheetName literal in any build_*.py source", not bad, "\n    ".join(bad))

# ── node helpers ────────────────────────────────────────────────────────────────
WRITE_OPS = {"append", "appendOrUpdate", "update"}
def gs_nodes(wf):  return [n for n in wf["nodes"] if n.get("type") == "n8n-nodes-base.googleSheets"]
def op(n):         return n.get("parameters", {}).get("operation", "read")
def sheetval(n):
    sn = n.get("parameters", {}).get("sheetName")
    return sn.get("value") if isinstance(sn, dict) else sn
def err_continues(n):
    return n.get("onError") in ("continueRegularOutput", "continueErrorOutput") or n.get("continueOnFail") is True
def upstream_types(wf, node_name):
    byname = {n["name"]: n for n in wf["nodes"]}
    out = []
    for src, conns in wf.get("connections", {}).items():
        for grp in conns.get("main", []):
            for t in (grp or []):
                if t["node"] == node_name:
                    out.append(byname.get(src, {}).get("type", ""))
    return out

def downstream_types(wf, node_name):
    byname = {n["name"]: n for n in wf["nodes"]}
    outs = wf.get("connections", {}).get(node_name, {}).get("main", [])
    return [byname.get(t["node"], {}).get("type", "") for grp in outs for t in (grp or [])]

# ── Invariant 1: no "gid=<n>" string sheetNames (the winback bug) ────────────────
def _gid_string_violations(payloads):
    bad = []
    for name, (_mod, wf) in payloads.items():
        for n in gs_nodes(wf):
            v = sheetval(n)
            if isinstance(v, str) and re.match(r"^\s*gid=\d+", v):
                bad.append(f"{name} :: {n['name']} :: sheetName={v!r}")
    return bad

def inv_no_gid_string_sheetname(payloads):
    bad = _gid_string_violations(payloads)
    check("no gid=<n> string sheetNames (winback bug)", not bad, "\n    ".join(bad))

# ── Invariant 2: dedup-log appends never halt the run on error ───────────────────
def inv_dedup_append_onerror_continue(payloads):
    bad = []
    for name, (_mod, wf) in payloads.items():
        node_names = [n["name"].lower() for n in wf["nodes"]]
        # only customer-blast dedup pattern: has a global/sent log
        if not any(("global sent" in x) or ("sent log" in x) for x in node_names):
            continue
        for n in gs_nodes(wf):
            if op(n) in WRITE_OPS and "sent" in n["name"].lower() and not n.get("disabled"):
                if not err_continues(n):
                    bad.append(f"{name} :: {n['name']} (onError={n.get('onError', 'HALT')})")
    check("dedup append nodes have onError=continue", not bad, "\n    ".join(bad))

# ── Invariant 3: no dedup-log code node reads $input straight off a send httpRequest ──
def inv_no_input_all_after_http(payloads):
    """The sub-reactivation bug shape exactly: a code node that (a) reads $input, (b) sits
    directly downstream of an httpRequest (the WA send, whose response replaces the item),
    and (c) feeds a Google Sheets write (the dedup log). A code node that consumes a data-
    FETCH http response for computation is fine - only logging-after-send is the trap, so we
    require the node to feed a sheet write."""
    bad = []
    for name, (_mod, wf) in payloads.items():
        for n in wf["nodes"]:
            if n.get("type") not in ("n8n-nodes-base.code", "n8n-nodes-base.function"):
                continue
            code = n.get("parameters", {}).get("jsCode", "") or n.get("parameters", {}).get("functionCode", "")
            if not any(p in code for p in ("$input.all()", "$input.item", "$input.first(")):
                continue
            up_http = any("httpRequest" in t for t in upstream_types(wf, n["name"]))
            down_sheet = any("googleSheets" in t for t in downstream_types(wf, n["name"]))
            if up_http and down_sheet:
                bad.append(f"{name} :: {n['name']} ($input straight off send-http, feeds dedup log)")
    check("no dedup-log $input.* directly after send httpRequest (sub-reactivation bug)", not bad, "\n    ".join(bad))

# ── Invariant 4: per-workflow sent-tab read value == write value ─────────────────
def inv_read_write_sent_tab_match(payloads):
    bad = []
    for name, (_mod, wf) in payloads.items():
        reads, writes = set(), set()
        for n in gs_nodes(wf):
            nm = n["name"].lower()
            if "sent" in nm and "global" not in nm:
                (writes if op(n) in WRITE_OPS else reads).add(str(sheetval(n)))
        if reads and writes and not (reads & writes):
            bad.append(f"{name} :: read sent-tab {reads} != write sent-tab {writes}")
    check("sent-tab read/write gid consistent within workflow", not bad, "\n    ".join(bad))

# ── Meta-test: prove the gid= detector actually catches the bug ──────────────────
def inv_detector_has_teeth():
    buggy = {"FakeWF": ("fake", {"nodes": [{
        "name": "Log X", "type": "n8n-nodes-base.googleSheets",
        "parameters": {"operation": "append", "sheetName": {"value": "gid=999"}},
    }], "connections": {}})}
    caught = len(_gid_string_violations(buggy)) == 1
    check("detector flags a synthetic gid= bug (has teeth)", caught,
          "gid= detector did NOT catch a known-bad workflow")


if __name__ == "__main__":
    payloads, errors = build_senders()
    print(f"\nBuilt {len(payloads)} sender workflows from source")
    if errors:
        print("Sender scripts that failed to build (MUST be fixed - import-safety regressed?):")
        for m, e in sorted(errors.items()):
            print(f"  - {m}: {e[:90]}")
    # Sanity: the workflows that carried the two bugs MUST be among those built.
    must = ["Win-back - WhatsApp", "Sub Reactivation - WhatsApp"]
    missing = [m for m in must if m not in payloads]
    check("critical customer-blast workflows built + asserted", not missing, f"missing: {missing}")
    check("all curated sender scripts imported cleanly", not errors, f"errors: {list(errors)}")

    print("\nSource-text scan (all build_*.py):")
    inv_source_no_gid_string_literal()

    print("\nStructural invariants (importable senders):")
    inv_no_gid_string_sheetname(payloads)
    inv_dedup_append_onerror_continue(payloads)
    inv_no_input_all_after_http(payloads)
    inv_read_write_sent_tab_match(payloads)
    inv_detector_has_teeth()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("All invariants pass.")
    raise SystemExit(0)

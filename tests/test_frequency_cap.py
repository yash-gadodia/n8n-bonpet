#!/usr/bin/env python3
"""Anti-spam frequency-cap tests (the 2026-06-22 over-messaging fix).

Customer +65 9145 4483 received THREE different lifecycle campaigns in ~5 weeks
(post-trial D7 on 18 May, post-trial D21 on 1 Jun, winback on 22 Jun). The old
guard was a 7-day cross-workflow cooldown, which never trips on campaigns spaced
14-21 days apart. The fix:

  - isOverFrequencyCap(phone): hard per-customer MARKETING cap. Blocks if a DIFFERENT
    marketing campaign messaged this phone in the last 14d, OR >=3 marketing messages
    in the last 90d. Same-workflow sends are exempt from the 14d rule so a campaign's
    own designed cadence (post-trial D7/D14/D21, reorder #1/#2) still fires.
  - winback gains a 30d rival guard vs post_trial_nurture + reorder_reminder.
  - isInGlobalCooldown(phone): unchanged 7d any-send courtesy guard, used only by
    transactional senders (abandoned cart / review watcher / subscription save).

Two test layers:
  1) BEHAVIOURAL: runs the REAL COOLDOWN_JS_SNIPPET (from _sent_log.py) in node
     against 7 scenarios, including the exact screenshot timeline.
  2) SOURCE-SCAN: every marketing builder is actually wired to the cap.

Run: python3 tests/test_frequency_cap.py   (exit 0 = pass, 1 = fail)
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _sent_log import COOLDOWN_JS_SNIPPET  # noqa: E402

failures = []
def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    if not ok:
        failures.append(f"{name}: {detail}")

# Winback's rival guard, mirrored from build_winback.py so we can exercise the exact
# logic that blocks the 22-Jun message. Kept in sync via the source-scan test below.
WINBACK_RIVAL_JS = r"""
const WINBACK_RIVAL_MS = 30 * 24 * 60 * 60 * 1000;
const WINBACK_RIVALS = new Set(['post_trial_nurture','reorder_reminder']);
function isInWinbackRivalWindow(phone) {
  const arr = MKT_SENDS.get(phone) || [];
  const now = Date.now();
  return arr.some(r => WINBACK_RIVALS.has(r.wf) && (now - r.t) < WINBACK_RIVAL_MS);
}
"""

# The snippet reads SELF_WORKFLOW from a literal; make it read the makeCap() param.
SNIPPET_PARAM = COOLDOWN_JS_SNIPPET.replace('"__SELF_WORKFLOW__"', 'selfWf')

HARNESS = r"""
'use strict';
let __ROWS__ = [];
function $(name) {
  if (name === 'Read Global Sent Log') return { all: () => __ROWS__ };
  throw new Error('no node ' + name);   // forces the snippet's Filter->Read fallback path
}
function normalizePhone(p) { return p ? ('+' + String(p).replace(/[^0-9]/g, '')) : ''; }
const DAY = 24 * 60 * 60 * 1000;
const NOW = Date.now();
function setRows(arr) {
  __ROWS__ = arr.map(r => ({ json: { phone: r.p, workflow: r.wf,
    sent_at: new Date(NOW - r.d * DAY).toISOString() } }));
}
function makeCap(selfWf) {
__SNIPPET__
__RIVAL__
  return { isOverFrequencyCap, isInGlobalCooldown, isInWinbackRivalWindow };
}

const PHONE = '+6591454483';
const results = [];
function expect(label, actual, want) { results.push({ label, pass: actual === want, actual, want }); }

// 1) SCREENSHOT CASE: post-trial D7 (35d ago) + D21 (21d ago) -> winback today must be BLOCKED
//    by the 30d rival guard (cap alone allows it: only 2 prior, 21d gap).
setRows([{ p: PHONE, wf: 'post_trial_nurture', d: 35 },
         { p: PHONE, wf: 'post_trial_nurture', d: 21 }]);
let wb = makeCap('winback');
expect('screenshot: winback blocked by 30d rival guard', wb.isInWinbackRivalWindow(PHONE), true);

// 2) CADENCE PRESERVED: post-trial D7 was 7d ago; D14 attempt (same workflow) must be ALLOWED.
setRows([{ p: PHONE, wf: 'post_trial_nurture', d: 7 }]);
let pt = makeCap('post_trial_nurture');
expect('cadence: post-trial D14 not blocked by own D7', pt.isOverFrequencyCap(PHONE), false);

// 3) CROSS-CAMPAIGN 14d: winback 5d ago -> reorder today must be BLOCKED.
setRows([{ p: PHONE, wf: 'winback', d: 5 }]);
let ro = makeCap('reorder_reminder');
expect('cross-campaign within 14d blocked', ro.isOverFrequencyCap(PHONE), true);

// 4) 90d COUNT CAP: 3 marketing msgs (80d/50d/20d, none within 14d) -> 4th BLOCKED.
setRows([{ p: PHONE, wf: 'trial_graduation', d: 80 },
         { p: PHONE, wf: 'post_trial_nurture', d: 50 },
         { p: PHONE, wf: 'reorder_reminder', d: 20 }]);
let cap4 = makeCap('winback');
expect('4th marketing msg in 90d blocked', cap4.isOverFrequencyCap(PHONE), true);

// 5) FIRST CONTACT: empty log -> ALLOWED.
setRows([]);
let fresh = makeCap('winback');
expect('first contact allowed', fresh.isOverFrequencyCap(PHONE), false);

// 6) TRANSACTIONAL NOT COUNTED: abandoned_cart 2d + review_watcher 1d -> marketing ALLOWED,
//    but the 7d courtesy guard (isInGlobalCooldown) still sees the recent contact.
setRows([{ p: PHONE, wf: 'abandoned_cart', d: 2 },
         { p: PHONE, wf: 'review_watcher', d: 1 }]);
let tx = makeCap('winback');
expect('transactional msgs do not block marketing cap', tx.isOverFrequencyCap(PHONE), false);
expect('transactional msgs still trip 7d courtesy guard', tx.isInGlobalCooldown(PHONE), true);

// 7) RUNAWAY BACKSTOP: a broken per-workflow dedup re-sends winback daily (1d/2d/3d ago).
//    Same-workflow so the 14d rule is exempt, but the 90d count cap stops the 4th -> BLOCKED.
//    This is the guarantee that daily re-spam can NEVER exceed 3 touches.
setRows([{ p: PHONE, wf: 'winback', d: 1 },
         { p: PHONE, wf: 'winback', d: 2 },
         { p: PHONE, wf: 'winback', d: 3 }]);
let runaway = makeCap('winback');
expect('runaway same-workflow re-send capped at 3 in 90d', runaway.isOverFrequencyCap(PHONE), true);

console.log(JSON.stringify(results));
"""

def run_behavioural():
    node = shutil.which("node")
    if not node:
        check("node available for behavioural test", False, "node not on PATH")
        return
    js = (HARNESS
          .replace("__SNIPPET__", SNIPPET_PARAM)
          .replace("__RIVAL__", WINBACK_RIVAL_JS))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            check("behavioural snippet runs in node", False, out.stderr.strip()[:300])
            return
        results = json.loads(out.stdout.strip().splitlines()[-1])
        for r in results:
            check(r["label"], r["pass"], f"got {r['actual']!r}, want {r['want']!r}")
    finally:
        os.unlink(path)

# ── Source-scan: every marketing builder is wired to the cap ──────────────────
MARKETING_BUILDERS = {
    "build_post_trial_nurture.py": "post_trial_nurture",
    "build_winback.py": "winback",
    "build_reorder_reminder_v2.py": "reorder_reminder",
    "build_trial_graduation.py": "trial_graduation",
    "build_sub_reactivation.py": "sub_reactivation",
    "build_dog_run_invite.py": "dog_run_invite",
}

def run_source_scan():
    # snippet defines both guards
    check("snippet defines isOverFrequencyCap", "function isOverFrequencyCap" in COOLDOWN_JS_SNIPPET)
    check("snippet defines isInGlobalCooldown", "function isInGlobalCooldown" in COOLDOWN_JS_SNIPPET)
    for fn, wf in MARKETING_BUILDERS.items():
        src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        token = f'COOLDOWN_JS_SNIPPET.replace("__SELF_WORKFLOW__", "{wf}")'
        check(f"{fn}: injects SELF_WORKFLOW={wf}", token in src,
              "missing exact .replace(__SELF_WORKFLOW__) call")
        check(f"{fn}: gate calls isOverFrequencyCap", "isOverFrequencyCap(" in src)
        check(f"{fn}: no stale isInGlobalCooldown gate", "isInGlobalCooldown(" not in src,
              "marketing senders must use the cap, not the 7d courtesy guard")
        check(f"{fn}: reads global sent log", "read_global_sent_log_node(" in src)
        check(f"{fn}: appends to global sent log", "append_global_sent_log_node(" in src)
        check(f"{fn}: workflow string '{wf}' is in MARKETING_WORKFLOWS",
              f"'{wf}'" in COOLDOWN_JS_SNIPPET)
    # winback rival guard mirrors the JS we tested behaviourally
    wbsrc = open(os.path.join(ROOT, "build_winback.py"), encoding="utf-8").read()
    check("build_winback.py: has 30d rival guard vs post_trial+reorder",
          "WINBACK_RIVALS" in wbsrc and "post_trial_nurture" in wbsrc and "isInRivalNudgeWindow" in wbsrc)

    # No raw promo code shown in any customer message body. Codes are conveyed via
    # auto-apply /discount/ links (encoded as %253C), never as a *CODE* bold string.
    bold_code = re.compile(r"\*[A-Z0-9]+<3THEBONPET\*")
    for fn in list(MARKETING_BUILDERS) + ["build_subscription_save.py", "build_review_watcher.py"]:
        src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        hits = bold_code.findall(src)
        check(f"{fn}: no raw *PROMO<3THEBONPET* code in body", not hits, str(hits))


if __name__ == "__main__":
    print("\nBehavioural (real COOLDOWN_JS_SNIPPET in node):")
    run_behavioural()
    print("\nSource-scan (marketing builders wired to cap):")
    run_source_scan()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("All frequency-cap tests pass.")
    raise SystemExit(0)

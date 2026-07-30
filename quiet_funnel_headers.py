#!/usr/bin/env python3
"""Make the five marketing funnel headers quiet and short.

Yash 2026-07-30: "the alerts are also suuuuper long, use more graphical stuff,
less 0 0 0 0 is useless data points." All five of these workflows posted a full
funnel summary to the weslee thread every single day at a 100% post rate, most
lines reading ": 0". That is ~4 messages/day of pure noise, and it trains the
team to ignore the thread.

One uniform patch to each workflow's header-gate node:
  1. Emit nothing at all when the run sent nothing (silence beats "0 candidates").
  2. Compress the 21-26 line funnel dump to ~3 lines: headline, a 10-cell progress
     bar, and the top-3 non-zero skips. No zero rows ever.

Safe because in all five the gate is fed by the compute/format node that fans out
to BOTH the sender and the gate, so the gate genuinely sees the candidate items
and `total - headers` is a true send count. Verified from the connection graph
before writing this (n8n does not persist node inputData, so execution data
cannot confirm it).
"""
import json, os, urllib.request, urllib.error

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
SNAP = os.path.expanduser("~/n8n-bonpet/snapshots")

GATES = {
    "Reorder Reminder - WhatsApp":            "Pass Header Only",
    "Win-back - WhatsApp":                    "Pass Header Only",
    "Post-Trial Nurture — WhatsApp 7/14/21":  "Pass Header Only",
    "Sub Reactivation - WhatsApp":            "Pick Summary",
    "Meal Plan Follow-up — WhatsApp D3/D7":   "Pass Header Only",
}

GATE_JS = r"""// Signal over noise (feedback_alert_design_signal_over_noise, 2026-07-30).
// Yash: "alerts are suuuuper long, use more graphical stuff, less 0 0 0 0".
//  1. post NOTHING when the run sent nothing,
//  2. compress the 21-26 line funnel dump to ~3 lines: headline + bar + top-3 skips.
// Generic on purpose - parses the bullet lines rather than knowing each workflow.
const all = $input.all();
const isHdr = (j) => j.is_header === true || j.is_summary === true;
const hdrs = all.filter(it => isHdr(it.json));
const sends = all.length - hdrs.length;
if (!hdrs.length || sends === 0) return [];

const h = hdrs[0];
const lines = String(h.json.message || '').split('\n');
const title = (lines.find(l => /\*/.test(l)) || lines[0] || '').trim();
const dateL = (lines.find(l => /^\u{1F4C5}/u.test(l)) || '').replace(/^\u{1F4C5}\s*/u, '').trim();

const bullets = [];
for (const l of lines) {
  const m = l.match(/^\s*[•\-]\s*(.+?)\s*[:：]\s*\*?(-?[\d,]+)\*?/);
  if (m) bullets.push({ label: m[1].trim(), n: parseInt(m[2].replace(/,/g, ''), 10) });
}
const isCtx  = (s) => /read|scanned|unique buyers|log size|contracts/i.test(s);
const isSkip = (s) => /skip|too |already|no |invalid|cooldown|blacklist|capp|churn|outside|wrong|window|between|eligible|exclud|subscriber/i.test(s);
const tidy = (s) => {                      // drop the 'Skipped (' wrapper
  let t = s.replace(/^skipp?e?d?\s*\(?/i, '').replace(/\)$/, '').trim();
  const opens = (t.match(/\(/g) || []).length;
  const closes = (t.match(/\)/g) || []).length;
  if (opens > closes) t += ')';           // keep inner parens balanced
  return t;
};

const skips = bullets.filter(b => isSkip(b.label) && !isCtx(b.label) && b.n > 0)
                     .sort((a, b) => b.n - a.n);
const sendBits = bullets.filter(b => !isSkip(b.label) && !isCtx(b.label) && b.n > 0
                                     && !/total/i.test(b.label));
const scanned = bullets.filter(b => isCtx(b.label)).sort((a, b) => b.n - a.n)[0];

const pool = sends + skips.reduce((t, b) => t + b.n, 0);
const filled = pool > 0 ? Math.max(1, Math.round((sends / pool) * 10)) : 0;
const bar = '▓'.repeat(filled) + '░'.repeat(10 - filled);

const out = [];
out.push(dateL ? title + '  ·  ' + dateL : title);
out.push('\u{1F4EC} *' + sends + ' sent*  ' + bar + (scanned ? '  ' + scanned.n + ' ' + scanned.label.toLowerCase() : ''));
if (sendBits.length) out.push('   ' + sendBits.map(b => tidy(b.label) + ' *' + b.n + '*').join(' · '));
if (skips.length)    out.push('\u{1F50E} skips: ' + skips.slice(0, 3).map(b => tidy(b.label) + ' ' + b.n).join(' · '));
h.json.message = out.join('\n');
return [h];
"""


def http(m, p, b=None):
    r = urllib.request.Request(API + p, data=json.dumps(b).encode() if b is not None else None,
        method=m, headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(r, timeout=90) as x:
            return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


if __name__ == "__main__":
    os.makedirs(SNAP, exist_ok=True)
    wfs = {w["name"]: w["id"] for w in http("GET", "/workflows?limit=250")[1]["data"]}
    for name, gate in GATES.items():
        wid = wfs.get(name)
        if not wid:
            print(f"  SKIP (not found): {name}"); continue
        s, w = http("GET", f"/workflows/{wid}")
        if s != 200:
            print(f"  GET failed {name}: {s}"); continue
        json.dump(w, open(f"{SNAP}/quiet_{wid}_prod.json", "w"), indent=2)
        node = next((n for n in w["nodes"] if n["name"] == gate), None)
        if node is None:
            print(f"  SKIP (gate {gate!r} missing): {name}"); continue
        node["parameters"]["jsCode"] = GATE_JS
        st, b = http("PUT", f"/workflows/{wid}", {"name": w["name"], "nodes": w["nodes"],
                     "connections": w["connections"], "settings": w.get("settings") or {}})
        print(f"  {name[:44]:46} {gate!r:20} -> HTTP {st}")
        if st != 200:
            print("     ", str(b)[:200])

#!/usr/bin/env python3
"""Parity check: Reorder Reminder's sent-log exclusion state, sheet vs API.

Before cutting Reorder Reminder over from the Google wa_sent_log sheet to
GET /wms/wa-log, prove both sources produce the SAME exclusion verdicts. This
workflow reads both, rebuilds the live workflow's exclusion structures from each
(builder code copied verbatim from `Compute Reorder Candidates`), and diffs the
verdict per phone across the union of phones. It sends NOTHING - the only output
is a summary item on the execution.

Mismatches are bucketed by expected cause:
- pre_backfill_history: phone excluded by the sheet's reorder_reminder_sent tab
  rows older than the 90d wa_sent_log backfill window (API can't know them).
  Fix = backfill the per-workflow tabs too.
- post_cutoff_labels: rows sent after the 2026-08-02T02:43 chokepoint carry the
  real workflow label in the sheet but `whatsapp_service` in the API (n8n does
  not pass workflow labels yet). Fix = re-run the backfill at cutover with the
  cutoff extended.
- transactional_only: API cooldown=true purely because of `whatsapp_service`
  rows the sheet never captured (order confirmations etc). Semantic decision:
  should a transactional send quiet marketing for 7d? Surfaced, not assumed.
- other: none of the above - a REAL divergence. Target: 0.
"""
import json
import os
import urllib.request
import uuid

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
GLOBAL_GID = 700800       # wa_sent_log tab
REORDER_SENT_GID = 800800  # reorder_reminder_sent tab
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}
OMS_CRED = {"id": "4pUEOr1SF2Fu4RNl", "name": "TBP OMS WMS PAT"}
WEBHOOK_PATH = "parity-reorder-sentlog"
CHOKEPOINT_CUTOFF = "2026-08-02T02:43:13Z"

COMPARE_JS = r"""
// ---- builders copied VERBATIM from the live `Compute Reorder Candidates` ----
function normalizePhone(p) {
  if (!p) return '';
  let s = String(p).replace(/\s/g, '').trim();
  if (s.startsWith('+')) {
    const d = s.slice(1).replace(/\D/g, '');
    return '+' + d;
  }
  const digits = s.replace(/\D/g, '');
  if (digits.length === 8 && /^[689]/.test(digits)) return '+65' + digits;
  if (digits.length === 10 && digits.startsWith('65')) return '+' + digits;
  if (digits.length >= 8 && digits.length <= 15) return '+' + digits;
  return '';
}
const SELF_WORKFLOW = 'reorder_reminder';
const MARKETING_WORKFLOWS = new Set(['post_trial_nurture','winback','reorder_reminder','trial_graduation','dog_run_invite','sub_reactivation']);
const GLOBAL_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000;
const FREQ_RECENT_MS = 14 * 24 * 60 * 60 * 1000;
const FREQ_WINDOW_MS = 90 * 24 * 60 * 60 * 1000;
const FREQ_MAX_IN_WINDOW = 3;
const REORDER_RIVAL_MS = 30 * 24 * 60 * 60 * 1000;
const REORDER_RIVALS = new Set(['post_trial_nurture']);
const CUTOFF = Date.parse('%CUTOFF%');
const now = Date.now();

// Build the three exclusion structures from a list of {phone, workflow, sent_at} rows.
function buildState(rows) {
  const st = { lastSent: new Map(), lastNonSvc: new Map(), mkt: new Map(), rival: new Map() };
  for (const s of rows) {
    const p = normalizePhone(s.phone);
    if (!p) continue;
    const t = new Date(s.sent_at || 0).getTime();
    if (!t) continue;
    const wf = String(s.workflow || '').trim();
    if (t > (st.lastSent.get(p) || 0)) st.lastSent.set(p, t);
    if (wf !== 'whatsapp_service' && t > (st.lastNonSvc.get(p) || 0)) st.lastNonSvc.set(p, t);
    if (MARKETING_WORKFLOWS.has(wf)) {
      if (!st.mkt.has(p)) st.mkt.set(p, []);
      st.mkt.get(p).push({ t, wf });
    }
    if (REORDER_RIVALS.has(wf) && t > (st.rival.get(p) || 0)) st.rival.set(p, t);
  }
  return st;
}
function verdict(st, alreadySet, p) {
  const arr = st.mkt.get(p) || [];
  return {
    already: alreadySet.has(p),
    cooldown: (now - (st.lastSent.get(p) || 0)) < GLOBAL_COOLDOWN_MS,
    freqcap: arr.some(r => (now - r.t) < FREQ_RECENT_MS && r.wf !== SELF_WORKFLOW)
             || arr.filter(r => (now - r.t) < FREQ_WINDOW_MS).length >= FREQ_MAX_IN_WINDOW,
    rival: (now - (st.rival.get(p) || 0)) < REORDER_RIVAL_MS,
  };
}

// ---- source rows ----
const sheetGlobal = $('Read Global Sent Tab').all().map(i => i.json)
  .map(s => ({ phone: s.phone, workflow: s.workflow, sent_at: s.sent_at }));
const apiAll = $('API All Rows').all().map(i => i.json)
  .map(s => ({ phone: s.phone_number, workflow: s.workflow, sent_at: s.sent_at }));

// Per-workflow permanent exclusion (ALREADY_SENT_PHONES)
const sheetAlready = new Set();
const sheetAlreadyTimes = new Map();
for (const it of $('Read Reorder Sent Tab').all()) {
  const p = normalizePhone(it.json.phone);
  if (!p) continue;
  sheetAlready.add(p);
  const t = new Date(it.json.sent_at || 0).getTime() || 0;
  if (t > (sheetAlreadyTimes.get(p) || 0)) sheetAlreadyTimes.set(p, t);
}
const apiAlready = new Set();
for (const it of $('API Reorder Rows').all()) {
  const p = normalizePhone(it.json.phone_number);
  if (p) apiAlready.add(p);
}

const S = buildState(sheetGlobal);
const A = buildState(apiAll);

// ---- diff over the union ----
const phones = new Set([...S.lastSent.keys(), ...A.lastSent.keys(), ...sheetAlready, ...apiAlready]);
const out = { phones_compared: phones.size,
              sheet_global_rows: sheetGlobal.length, api_rows: apiAll.length,
              sheet_reorder_tab: sheetAlready.size, api_reorder_phones: apiAlready.size,
              match: 0, mismatch: 0,
              buckets: { pre_backfill_history: 0, post_cutoff_labels: 0, transactional_only: 0, other: 0 },
              examples: [] };
const backfillFloor = Date.parse('2026-06-03T00:00:00Z');  // oldest imported row

for (const p of phones) {
  const vs = verdict(S, sheetAlready, p);
  const va = verdict(A, apiAlready, p);
  const excludedS = vs.already || vs.cooldown || vs.freqcap || vs.rival;
  const excludedA = va.already || va.cooldown || va.freqcap || va.rival;
  if (excludedS === excludedA) { out.match++; continue; }
  out.mismatch++;
  let bucket = 'other';
  if (vs.already && !va.already && (sheetAlreadyTimes.get(p) || 0) < backfillFloor) {
    bucket = 'pre_backfill_history';
  } else if ((S.lastSent.get(p) || 0) >= CUTOFF || (A.lastSent.get(p) || 0) >= CUTOFF) {
    // divergence involves post-chokepoint rows (label gap / dual-write timing)
    bucket = excludedA && !excludedS && !A.lastNonSvc.get(p) ? 'transactional_only' : 'post_cutoff_labels';
  } else if (excludedA && !excludedS && !A.lastNonSvc.get(p)) {
    bucket = 'transactional_only';
  }
  out.buckets[bucket]++;
  if (out.examples.length < 15) out.examples.push({
    phone: p.slice(0, 6) + '****', bucket,
    sheet: vs, api: va,
    sheet_last: S.lastSent.get(p) ? new Date(S.lastSent.get(p)).toISOString() : null,
    api_last: A.lastSent.get(p) ? new Date(A.lastSent.get(p)).toISOString() : null,
    reorder_tab_last: sheetAlreadyTimes.get(p) ? new Date(sheetAlreadyTimes.get(p)).toISOString() : null,
  });
}
return [{ json: out }];
""".replace("%CUTOFF%", CHOKEPOINT_CUTOFF)


def node(name, type_, position, parameters, **extra):
    return {"parameters": parameters, "id": str(uuid.uuid4()), "name": name,
            "type": type_, "typeVersion": extra.pop("typeVersion", 2),
            "position": position, **extra}


def sheet_read(name, gid, tab, position):
    return node(name, "n8n-nodes-base.googleSheets", position,
                {"documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                                "cachedResultName": "Bon Pet — Customer Orders DB"},
                 "sheetName": {"__rl": True, "value": gid, "mode": "list",
                               "cachedResultName": tab},
                 "options": {}},
                typeVersion=4.5, credentials={"googleSheetsOAuth2Api": GS_CRED},
                executeOnce=True, alwaysOutputData=True)


def api_read(name, url, position):
    return node(name, "n8n-nodes-base.httpRequest", position,
                {"method": "GET", "url": url,
                 "authentication": "genericCredentialType",
                 "genericAuthType": "httpHeaderAuth",
                 "options": {}},
                typeVersion=4.2, credentials={"httpHeaderAuth": OMS_CRED},
                executeOnce=True, alwaysOutputData=True)


nodes = [
    node("Manual Trigger", "n8n-nodes-base.webhook", [0, 0],
         {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}},
         typeVersion=2, webhookId=str(uuid.uuid4())),
    sheet_read("Read Reorder Sent Tab", REORDER_SENT_GID, "reorder_reminder_sent", [220, -100]),
    sheet_read("Read Global Sent Tab", GLOBAL_GID, "wa_sent_log", [440, -100]),
    api_read("API Reorder Rows",
             "https://api.thebonpet.com/wms/wa-log/recent?workflow=reorder_reminder&limit=1000",
             [660, -100]),
    api_read("API All Rows",
             "https://api.thebonpet.com/wms/wa-log/recent?limit=1000",
             [880, -100]),
    node("Compare", "n8n-nodes-base.code", [1100, 0], {"jsCode": COMPARE_JS}),
]

connections = {
    "Manual Trigger": {"main": [[{"node": "Read Reorder Sent Tab", "type": "main", "index": 0}]]},
    "Read Reorder Sent Tab": {"main": [[{"node": "Read Global Sent Tab", "type": "main", "index": 0}]]},
    "Read Global Sent Tab": {"main": [[{"node": "API Reorder Rows", "type": "main", "index": 0}]]},
    "API Reorder Rows": {"main": [[{"node": "API All Rows", "type": "main", "index": 0}]]},
    "API All Rows": {"main": [[{"node": "Compare", "type": "main", "index": 0}]]},
}

workflow = {
    "name": "TEMP Parity — Reorder sent-log sheet vs API",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore",
                 "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"},
}


def req(method, path, body=None):
    r = urllib.request.Request(API + path,
                               data=json.dumps(body).encode() if body else None,
                               method=method)
    r.add_header("X-N8N-API-KEY", KEY)
    r.add_header("User-Agent", "Mozilla/5.0")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    import sys
    existing_id = sys.argv[1] if len(sys.argv) > 1 else None
    if existing_id:
        wf = req("PUT", f"/workflows/{existing_id}", workflow)
        print(f"updated workflow {wf['id']}")
    else:
        wf = req("POST", "/workflows", workflow)
        print(f"created workflow {wf['id']}")
    req("POST", f"/workflows/{wf['id']}/activate")
    print(f"trigger ONCE: curl -X POST https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}")

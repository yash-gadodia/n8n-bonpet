"""Shared helpers for the global WA sent log.

Every customer-facing workflow writes to wa_sent_log (GID 700800) after sending.
Marketing workflows also READ it and check a 7-day cooldown to prevent cross-workflow spam.

Usage in a build_*.py:

    from _sent_log import (
        WA_SENT_LOG_GID, WA_SENT_LOG_TAB,
        read_global_sent_log_node,
        append_global_sent_log_node,
        COOLDOWN_JS_SNIPPET,
    )

    # In nodes list:
    read_global = read_global_sent_log_node([480, 500])
    log_global = append_global_sent_log_node([1680, 300])

    # In CODE_JS: include COOLDOWN_JS_SNIPPET after the normalizePhone() function,
    # then in the candidate loop, call `if (isInGlobalCooldown(phone)) { stats.skipped_global_cooldown++; continue; }`
"""
import os
import uuid

SHEET_ID = "1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI"
WA_SENT_LOG_GID = 700800
WA_SENT_LOG_TAB = "wa_sent_log"
WA_SENT_LOG_COLUMNS = ["phone", "workflow", "template", "sent_at", "order_id", "notes"]
GS_CRED = {"id": "KLjk8w62GoEMImKa", "name": "Google Sheets account"}  # self-hosted ID; old Cloud was sxbz0Cu8yhdi0RdN


def read_global_sent_log_node(position, name="Read Global Sent Log"):
    """Google Sheets read node for the global WA sent log.

    Set alwaysOutputData=True so an empty log doesn't break the chain on first run.
    executeOnce=True so we read the tab once, not once per upstream item.
    """
    return {
        "parameters": {
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": WA_SENT_LOG_GID, "mode": "list",
                          "cachedResultName": WA_SENT_LOG_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={WA_SENT_LOG_GID}"},
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "executeOnce": True,
        "alwaysOutputData": True,
    }


def filter_recent_sent_log_node(position, name="Filter Recent Sent Log", days=14):
    """Drops wa_sent_log rows older than `days`. Place between
    read_global_sent_log_node and the downstream Merge to keep merge memory
    bounded as the log grows. Cooldown is 7d; 14d default gives a buffer.
    """
    js = (
        f"const CUTOFF_MS = {days} * 24 * 60 * 60 * 1000;\n"
        "const now = Date.now();\n"
        "return $input.all().filter(it => {\n"
        "  const t = Date.parse((it.json && it.json.sent_at) || '');\n"
        "  if (isNaN(t)) return false;\n"
        "  return (now - t) <= CUTOFF_MS;\n"
        "});\n"
    )
    import uuid as _uuid
    return {
        "parameters": {"jsCode": js},
        "id": str(_uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": position,
    }


def native_filter_recent_sent_log_node(position, name="Filter Recent Sent Log", days=14):
    """Native Filter node variant of filter_recent_sent_log_node.

    Use this one for big logs: a Code-node filter still copies EVERY input item
    to the task-runner process before filtering (that copy OOM-killed the pod
    when wa_sent_log hit 83k rows on 2026-06-10). The native Filter node runs
    in the main process, so only the surviving rows ever reach a runner.

    alwaysOutputData so zero recent rows still produces an (empty) item and
    downstream Code nodes execute. The condition is a pure JS boolean (not a
    dateTime operator) because even loose typeValidation hard-errors on blank
    sent_at cells ("Conversion error: the string '' can't be converted to a
    dateTime") and the 83k-row log has blank gap rows. Date.parse of blank or
    garbage yields NaN, the comparison is false, the row is dropped.
    """
    cutoff_ms = str(days) + " * 24 * 60 * 60 * 1000"
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": str(uuid.uuid4()),
                    "leftValue": "={{ Date.parse($json.sent_at || '') > (Date.now() - " + cutoff_ms + ") }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.filter", "typeVersion": 2,
        "position": position,
        "alwaysOutputData": True,
    }


def append_global_sent_log_node(position, name="Log Global Sent"):
    """Google Sheets append node. Assumes upstream items carry the 6 column fields
    (phone, workflow, template, sent_at, order_id, notes) at the top level of json.

    onError continueRegularOutput so a sheet hiccup doesn't crash the workflow.
    """
    return {
        "parameters": {
            "operation": "append",
            "documentId": {"__rl": True, "value": SHEET_ID, "mode": "list",
                           "cachedResultName": "Bon Pet — Customer Orders DB",
                           "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
            "sheetName": {"__rl": True, "value": WA_SENT_LOG_GID, "mode": "list",
                          "cachedResultName": WA_SENT_LOG_TAB,
                          "cachedResultUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={WA_SENT_LOG_GID}"},
            "columns": {
                "mappingMode": "autoMapInputData",
                "schema": [{"id": h, "displayName": h, "required": False,
                            "display": True, "type": "string"} for h in WA_SENT_LOG_COLUMNS],
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "onError": "continueRegularOutput",
    }


# JS snippet to inject into a workflow's Code node AFTER normalizePhone() is defined.
# Reads $('Read Global Sent Log').all() (upstream node must have that exact name),
# and exposes TWO guards:
#
#   isInGlobalCooldown(phone)  - short courtesy guard: true if ANY outbound (marketing
#                                OR transactional) went to this phone in the last 7 days.
#                                Used by TRANSACTIONAL senders (abandoned cart, review
#                                watcher, subscription save) - they should still fire on a
#                                customer-triggered event, just not double-text within 7d.
#
#   isOverFrequencyCap(phone)  - hard per-customer MARKETING cap (the anti-spam fix).
#                                true if EITHER:
#                                  (a) a DIFFERENT marketing campaign messaged this phone in
#                                      the last FREQ_RECENT_DAYS (14) days, OR
#                                  (b) >= FREQ_MAX_IN_WINDOW (3) marketing messages were sent
#                                      in the last FREQ_WINDOW_DAYS (90) days.
#                                Counts MARKETING_WORKFLOWS rows only. Same-workflow sends are
#                                exempt from (a) so a campaign's own designed multi-touch
#                                cadence (post-trial D7/D14/D21, reorder #1/#2) is preserved -
#                                set SELF_WORKFLOW to the builder's own workflow string.
#                                Used by every MARKETING sender. This is the backstop that
#                                makes runaway daily re-spam impossible even if a per-workflow
#                                dedup tab breaks: at most 3 marketing touches per 90 days.
#
# Marketing builders inject this and call isOverFrequencyCap(phone); set the SELF_WORKFLOW
# token via .replace("__SELF_WORKFLOW__", "winback") so own-cadence steps aren't blocked.
COOLDOWN_JS_SNIPPET = r"""
// --- Global WA frequency cap (spam prevention across workflows) ---
// Prefers "Filter Recent Sent Log" if present (bounded memory); falls back to
// "Read Global Sent Log" so workflows without the filter still work.
const SELF_WORKFLOW = "__SELF_WORKFLOW__";  // marketing builders .replace() this; others leave as-is (harmless)
const MARKETING_WORKFLOWS = new Set(['post_trial_nurture','winback','reorder_reminder','trial_graduation','dog_run_invite','sub_reactivation']);
const GLOBAL_COOLDOWN_DAYS = 7;
const GLOBAL_COOLDOWN_MS = GLOBAL_COOLDOWN_DAYS * 24 * 60 * 60 * 1000;
const FREQ_RECENT_DAYS = 14;            // no two DIFFERENT marketing campaigns within this gap
const FREQ_RECENT_MS = FREQ_RECENT_DAYS * 24 * 60 * 60 * 1000;
const FREQ_WINDOW_DAYS = 90;            // rolling window for the hard count cap
const FREQ_WINDOW_MS = FREQ_WINDOW_DAYS * 24 * 60 * 60 * 1000;
const FREQ_MAX_IN_WINDOW = 3;           // max marketing messages per customer per 90 days
const GLOBAL_LAST_SENT = new Map();     // phone -> latest send ms (ANY workflow)
const MKT_SENDS = new Map();            // phone -> [{t, wf}] for MARKETING rows only
let _sentRows = [];
try { _sentRows = $('Filter Recent Sent Log').all(); }
catch (e) {
  try { _sentRows = $('Read Global Sent Log').all(); }
  catch (e2) { _sentRows = []; }
}
for (const it of _sentRows) {
  const s = it.json;
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  const prev = GLOBAL_LAST_SENT.get(p) || 0;
  if (t > prev) GLOBAL_LAST_SENT.set(p, t);
  const wf = String(s.workflow || '').trim();
  if (MARKETING_WORKFLOWS.has(wf)) {
    if (!MKT_SENDS.has(p)) MKT_SENDS.set(p, []);
    MKT_SENDS.get(p).push({ t, wf });
  }
}
function isInGlobalCooldown(phone) {
  const last = GLOBAL_LAST_SENT.get(phone);
  if (!last) return false;
  return (Date.now() - last) < GLOBAL_COOLDOWN_MS;
}
function isOverFrequencyCap(phone) {
  const arr = MKT_SENDS.get(phone) || [];
  const now = Date.now();
  const recentOther = arr.some(r => (now - r.t) < FREQ_RECENT_MS && r.wf !== SELF_WORKFLOW);
  const inWindow = arr.filter(r => (now - r.t) < FREQ_WINDOW_MS).length;
  return recentOther || inWindow >= FREQ_MAX_IN_WINDOW;
}
"""

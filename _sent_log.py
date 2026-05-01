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
GS_CRED = {"id": "sxbz0Cu8yhdi0RdN", "name": "Google Sheets account"}


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
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "executeOnce": True,
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
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7,
        "position": position,
        "credentials": {"googleSheetsOAuth2Api": GS_CRED},
        "onError": "continueRegularOutput",
    }


# JS snippet to inject into a workflow's Code node AFTER normalizePhone() is defined.
# Reads $('Read Global Sent Log').all() (upstream node must have that exact name),
# builds a Map<phone, latest-sent-ms>, and exposes isInGlobalCooldown(phone).
# Default 7 days = 604800000 ms.
COOLDOWN_JS_SNIPPET = r"""
// --- Global WA cooldown (spam prevention across workflows) ---
// Reads wa_sent_log via the "Read Global Sent Log" upstream node. Any customer
// messaged by ANY workflow in the last N days is excluded.
const GLOBAL_COOLDOWN_DAYS = 7;
const GLOBAL_COOLDOWN_MS = GLOBAL_COOLDOWN_DAYS * 24 * 60 * 60 * 1000;
const GLOBAL_LAST_SENT = new Map();
for (const it of $('Read Global Sent Log').all()) {
  const s = it.json;
  const p = normalizePhone(s.phone);
  if (!p) continue;
  const t = new Date(s.sent_at || 0).getTime();
  if (!t) continue;
  const prev = GLOBAL_LAST_SENT.get(p) || 0;
  if (t > prev) GLOBAL_LAST_SENT.set(p, t);
}
function isInGlobalCooldown(phone) {
  const last = GLOBAL_LAST_SENT.get(phone);
  if (!last) return false;
  return (Date.now() - last) < GLOBAL_COOLDOWN_MS;
}
"""

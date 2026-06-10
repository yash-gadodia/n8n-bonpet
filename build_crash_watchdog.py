#!/usr/bin/env python3
"""Crash Watchdog — alerts the weslee thread about failed/crashed executions.

Why this exists: when the n8n pod OOM-crashes mid-execution, the recovered
execution is marked errored but the Error Alerter (errorTrigger) never fires
for it — between 2026-05-27 and 2026-06-10 there were ~50 silent crashes and
zero alerts. This workflow polls n8n's own public API every 30 min for
executions with status=error started in the last 35 min and posts a per-
workflow summary to Telegram. It catches both crash-recovered and regular
errored executions (slight overlap with Error Alerter is acceptable).

Architecture:
  Schedule (13,43 * * * *) ┐
                           ├→ Fetch Error Executions (localhost API)
  Manual Webhook           ┘    → Filter Recent Crashes → Send Telegram Weslee

NAME_MAP is baked at build time from the live workflow list — rerun this
script after renaming/adding workflows to refresh it. Unknown ids fall back
to the raw workflow id in the alert.
"""
import json
import os
import uuid
import urllib.request
import urllib.error

from _notify import telegram_send_node

API = "https://n8n.thebonpet.com/api/v1"
WF_ID = "xPSl6E77HfPvHBUT"
WF_NAME = "Crash Watchdog → weslee"
WEBHOOK_PATH = "crash-watchdog-manual-4e7d2a"
LOOKBACK_MIN = 35

N8N_KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()


def uid():
    return str(uuid.uuid4())


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": N8N_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fetch_name_map():
    status, data = api("/workflows?limit=250")
    assert status == 200, f"workflow list failed: {status}"
    return {w["id"]: w["name"] for w in data["data"] if w.get("active")}


def filter_js(name_map):
    return (
        "const NAME_MAP = " + json.dumps(name_map, ensure_ascii=False) + ";\n"
        "const SELF_ID = " + json.dumps(WF_ID or "") + ";\n"
        "const LOOKBACK_MIN = " + str(LOOKBACK_MIN) + ";\n"
        r"""const resp = $('Fetch Error Executions').first().json;
const cutoff = Date.now() - LOOKBACK_MIN * 60 * 1000;
const recent = (resp.data || []).filter(e =>
  e.workflowId !== SELF_ID &&
  e.startedAt && new Date(e.startedAt).getTime() >= cutoff
);
if (!recent.length) return [];
const by = {};
for (const e of recent) {
  const k = NAME_MAP[e.workflowId] || e.workflowId;
  (by[k] = by[k] || []).push(e);
}
const fmt = ts => new Date(ts).toLocaleTimeString('en-GB', {
  timeZone: 'Asia/Singapore', hour: '2-digit', minute: '2-digit'
});
const lines = Object.entries(by).map(([name, es]) =>
  `• ${name} x${es.length} (last ${fmt(es.map(x => x.startedAt).sort().pop())} SGT)`
);
const message = `🚨 n8n Crash Watchdog\n` +
  `${recent.length} failed execution(s) in the last ${LOOKBACK_MIN} min:\n` +
  lines.join('\n') +
  `\nMost crashes here are OOM. Executions: https://n8n.thebonpet.com/home/executions`;
return [{ json: { message, count: recent.length } }];
"""
    )


def build(name_map):
    schedule = {
        # 13/43 stays clear of :00 cluster, :07 (Sub React), :17 (Sweeper), :37 (Review Watcher).
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "13,43 * * * *"}]}},
        "id": uid(), "name": "Every 30 min",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "position": [0, 200],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 400],
        "webhookId": uid(),
    }
    fetch = {
        # localhost: skips Cloudflare (UA blocking + 5xx during recovery windows).
        "parameters": {
            "url": "http://localhost:5678/api/v1/executions",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "status", "value": "error"},
                {"name": "limit", "value": "25"},
            ]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-N8N-API-KEY", "value": N8N_KEY}]},
            "options": {},
        },
        "id": uid(), "name": "Fetch Error Executions",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [240, 300],
    }
    filt = {
        "parameters": {"jsCode": filter_js(name_map)},
        "id": uid(), "name": "Filter Recent Crashes",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [480, 300],
    }
    tg = telegram_send_node("Send Telegram Weslee", [720, 300], parse_mode=None)
    return {
        "name": WF_NAME,
        "nodes": [schedule, manual, fetch, filt, tg],
        "connections": {
            "Every 30 min": {"main": [[{"node": "Fetch Error Executions", "type": "main", "index": 0}]]},
            "Manual Webhook": {"main": [[{"node": "Fetch Error Executions", "type": "main", "index": 0}]]},
            "Fetch Error Executions": {"main": [[{"node": "Filter Recent Crashes", "type": "main", "index": 0}]]},
            "Filter Recent Crashes": {"main": [[{"node": "Send Telegram Weslee", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"},
    }


if __name__ == "__main__":
    name_map = fetch_name_map()
    payload = build(name_map)
    with open("/Users/yash/n8n-bonpet/crash_watchdog_payload.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Built payload: {len(payload['nodes'])} nodes, {len(name_map)} names in map")
    if WF_ID:
        status, body = api(f"/workflows/{WF_ID}", "PUT", payload)
        print(f"PUT HTTP {status}")
    else:
        status, body = api("/workflows", "POST", payload)
        print(f"POST HTTP {status}")
        if status == 200:
            wid = body["id"]
            print(f"Created workflow id: {wid} — set WF_ID and rerun to bake self-exclusion")
            status2, _ = api(f"/workflows/{wid}/activate", "POST")
            print(f"Activate HTTP {status2}")

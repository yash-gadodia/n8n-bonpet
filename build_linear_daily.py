#!/usr/bin/env python3
"""Build the Linear Daily Tasks workflow.

Schedule: Daily 9:30 AM SGT (just after Morning Briefing).
Source: Linear GraphQL API → all open issues (Backlog/Todo/In Progress/In Review).
Group by primary category label. Send to team Telegram thread.
"""
import json
import os
import subprocess
import urllib.request
import urllib.error
import uuid

API = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
WF_NAME = "Linear Daily Tasks"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_PATH = "linear-daily-manual-7e3f2c"

TELEGRAM_BOT_TOKEN = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","telegram-weslee-bot","-w"]).decode().strip()
TELEGRAM_CHAT_ID = "-1002184573790"
TELEGRAM_THREAD_ID = "34253"

LINEAR_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "linear-thebonpet-api", "-w"]
).decode().strip()

ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"


def n8n(method, path, body=None):
    r = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(r) as res:
            return res.status, json.loads(res.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def uid():
    return str(uuid.uuid4())


LINEAR_QUERY = (
    "{ issues(first: 200, filter: { state: { type: { in: [\\\"backlog\\\",\\\"unstarted\\\",\\\"started\\\"] } } }) "
    "{ nodes { identifier title priority priorityLabel state { name type } "
    "assignee { name email } labels { nodes { name } } updatedAt url } } }"
)


FORMAT_JS = r"""// Group Linear open issues by primary category label, format Telegram digest
const resp = $input.first().json;
const issues = (resp.data && resp.data.issues && resp.data.issues.nodes) || [];

const CAT_PRIORITY = ['Dev', 'Marketing', 'Ops', 'BD', 'Customer Support', 'OMS', 'Whatsapp'];
const CAT_EMOJI = {
  'Dev':              '💻',
  'Marketing':        '📣',
  'Ops':              '🔧',
  'BD':               '🤝',
  'Customer Support': '💬',
  'OMS':              '📦',
  'Whatsapp':         '📲',
  'Other':            '📌',
};

function shortName(person) {
  if (!person) return '—';
  const n = (person.name || '').trim();
  if (n) return n.split(' ')[0];
  const email = (person.email || '').split('@')[0];
  return email || '—';
}

function priIcon(p) {
  // Linear priority: 0=No, 1=Urgent, 2=High, 3=Medium, 4=Low
  if (p === 1) return '🚨';
  if (p === 2) return '🔺';
  if (p === 4) return '🔻';
  return '';
}

const STALE_DAYS = 14;
const stale = (updatedAt) => {
  const ms = Date.now() - new Date(updatedAt).getTime();
  return ms > STALE_DAYS * 24 * 60 * 60 * 1000;
};

const groups = {};
for (const cat of CAT_PRIORITY) groups[cat] = [];
groups['Other'] = [];

for (const i of issues) {
  const labelNames = (i.labels.nodes || []).map(l => l.name);
  let bucket = 'Other';
  for (const cat of CAT_PRIORITY) {
    if (labelNames.includes(cat)) { bucket = cat; break; }
  }
  groups[bucket].push(i);
}

// Sort each group: Urgent → priority → stale first
for (const cat of Object.keys(groups)) {
  groups[cat].sort((a, b) => {
    // Urgent (1) = highest, then High (2), Medium (3), Low (4), No (0 last)
    const pa = a.priority === 0 ? 99 : a.priority;
    const pb = b.priority === 0 ? 99 : b.priority;
    if (pa !== pb) return pa - pb;
    return new Date(a.updatedAt).getTime() - new Date(b.updatedAt).getTime();
  });
}

const totalOpen = issues.length;
const inProgress = issues.filter(i => i.state.type === 'started').length;
const todo       = issues.filter(i => i.state.type === 'unstarted').length;
const backlog    = issues.filter(i => i.state.type === 'backlog').length;
const staleCount = issues.filter(i => stale(i.updatedAt)).length;

const lines = [];
lines.push('📋 *Bon Pet — Open Tasks*');
lines.push(`_${totalOpen} open · ${inProgress} in progress · ${todo} todo · ${backlog} backlog_`);
if (staleCount) lines.push(`_⚠️ ${staleCount} stale (>${STALE_DAYS}d no update)_`);
lines.push('');

const MAX_PER_GROUP = 5;
for (const cat of [...CAT_PRIORITY, 'Other']) {
  const list = groups[cat];
  if (!list.length) continue;
  const e = CAT_EMOJI[cat] || '•';
  lines.push(`${e} *${cat} (${list.length})*`);
  const shown = list.slice(0, MAX_PER_GROUP);
  for (const i of shown) {
    const pi = priIcon(i.priority);
    const st = stale(i.updatedAt) ? ' ⚠️' : '';
    const asn = shortName(i.assignee);
    const stateBadge = i.state.type === 'started' ? ' [WIP]' : '';
    const title = (i.title || '').slice(0, 70);
    lines.push(`  • [${i.identifier}](${i.url}) ${pi}${stateBadge} \`${asn}\` — ${title}${st}`);
  }
  if (list.length > MAX_PER_GROUP) {
    lines.push(`  _+${list.length - MAX_PER_GROUP} more in ${cat}_`);
  }
  lines.push('');
}

lines.push('_View all: https://linear.app/thebonpet/team/TBP/all_');

const message = lines.join('\n');
return [{ json: { message, total: totalOpen, in_progress: inProgress, todo, backlog, stale: staleCount } }];
"""


def schedule_node():
    return {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "30 9 * * *"}]}
        },
        "id": uid(),
        "name": "Daily 9:30AM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.3,
        "position": [0, 200],
    }


def manual_node():
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": MANUAL_PATH,
            "responseMode": "onReceived",
            "options": {},
        },
        "id": uid(),
        "name": "Manual Trigger (Webhook)",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 400],
        "webhookId": MANUAL_PATH,
    }


def linear_node():
    return {
        "parameters": {
            "method": "POST",
            "url": "https://api.linear.app/graphql",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": LINEAR_TOKEN},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({"query": LINEAR_QUERY.replace("\\\"", "\"")}),
            "options": {},
        },
        "id": uid(),
        "name": "Fetch Linear Issues",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 300],
    }


def code_node():
    return {
        "parameters": {"jsCode": FORMAT_JS},
        "id": uid(),
        "name": "Format Digest",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [480, 300],
    }


def telegram_node():
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            "sendBody": True,
            "bodyParameters": {"parameters": [
                {"name": "chat_id", "value": TELEGRAM_CHAT_ID},
                {"name": "message_thread_id", "value": TELEGRAM_THREAD_ID},
                {"name": "text", "value": "={{ $json.message }}"},
                {"name": "parse_mode", "value": "Markdown"},
                {"name": "disable_web_page_preview", "value": "true"},
            ]},
            "options": {},
        },
        "id": uid(),
        "name": "Send Telegram",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [720, 300],
    }


def build():
    sched = schedule_node()
    manual = manual_node()
    fetch = linear_node()
    fmt = code_node()
    tg = telegram_node()

    return {
        "name": WF_NAME,
        "nodes": [sched, manual, fetch, fmt, tg],
        "connections": {
            sched["name"]:  {"main": [[{"node": fetch["name"], "type": "main", "index": 0}]]},
            manual["name"]: {"main": [[{"node": fetch["name"], "type": "main", "index": 0}]]},
            fetch["name"]:  {"main": [[{"node": fmt["name"],   "type": "main", "index": 0}]]},
            fmt["name"]:    {"main": [[{"node": tg["name"],    "type": "main", "index": 0}]]},
        },
        "settings": {
            "executionOrder": "v1",
            "errorWorkflow": ERROR_ALERTER_ID,
            "timezone": "Asia/Singapore",
        },
    }


def find_existing():
    s, data = n8n("GET", "/workflows?limit=250")
    if s >= 300: return None
    for w in data.get("data", []):
        if w["name"] == WF_NAME:
            return w["id"]
    return None


if __name__ == "__main__":
    payload = build()
    existing = find_existing()
    if existing:
        s, body = n8n("PUT", f"/workflows/{existing}", payload)
        print(f"PUT existing {existing} → {s}")
        wf_id = existing
    else:
        s, body = n8n("POST", "/workflows", payload)
        wf_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {wf_id} → {s}")

    if s >= 300:
        print(body); raise SystemExit(1)

    # transfer to team project (if needed)
    n8n("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})

    # activate
    s, body = n8n("POST", f"/workflows/{wf_id}/activate")
    print(f"activate → {s}")

    # test fire via manual webhook
    print("test firing...")
    trig = urllib.request.Request(
        f"https://n8n.thebonpet.com/webhook/{MANUAL_PATH}",
        data=b'{}', method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(trig, timeout=30) as r:
            print(f"  HTTP {r.status}: {r.read().decode()[:200]}")
    except urllib.error.HTTPError as e:
        print(f"  trigger error {e.code}: {e.read().decode()[:300]}")

    # wait + verify
    import time
    time.sleep(8)
    s, data = n8n("GET", f"/executions?workflowId={wf_id}&limit=1&includeData=true")
    if s < 300:
        e = data["data"][0]
        rd = e.get("data",{}).get("resultData",{})
        print(f"\nExecution finished={e.get('finished')} lastNode={rd.get('lastNodeExecuted')}")
        for n, runs in rd.get("runData",{}).items():
            for r in runs:
                if r.get("error"):
                    print(f"  ERR in {n}: {r['error'].get('message','')[:200]}")
        if "Format Digest" in rd.get("runData",{}):
            out = rd["runData"]["Format Digest"][0].get("data",{}).get("main",[[]])
            if out and out[0]:
                msg = out[0][0]["json"].get("message","")
                print(f"\n=== MESSAGE PREVIEW ===\n{msg}")

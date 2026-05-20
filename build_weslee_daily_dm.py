#!/usr/bin/env python3
"""Build the 'Weslee Daily Per-Person DM' workflow.

Schedule: 18:00 SGT daily.
For each teammate registered in TEAM_DM_CHATS, fetch their open Linear tickets
and DM the list via @weslee_bot. Skips anyone with no open work.

Onboarding a teammate (one-time):
  1. Have them DM /start (or any message) to @weslee_bot in a private chat.
  2. The bot replies with their chat_id.
  3. Add them to TEAM_DM_CHATS below, then re-deploy this workflow.
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
WF_NAME = "Weslee Daily Per-Person DM"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
MANUAL_PATH = "weslee-daily-dm-manual-9a2e1f"

TELEGRAM_BOT_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "telegram-weslee-bot", "-w"]
).decode().strip()
LINEAR_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "linear-thebonpet-api", "-w"]
).decode().strip()
ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"

# Add a teammate AFTER they /start the bot in DM (bot reply gives chat_id).
# linear_first_name (matches Linear's "name" field) → registration record.
TEAM_DM_CHATS = {
    "yash":    {"chat_id": 166637821, "telegram": "yashgadodia", "display": "Yash"},
    # "nicolas": {"chat_id": 0, "telegram": "nicolaswee",  "display": "Nicolas"},
    # "rachel":  {"chat_id": 0, "telegram": "rachellrqq",  "display": "Rachel"},
    # "shaun":   {"chat_id": 0, "telegram": "gotchykid",   "display": "Shaun"},
}


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
    "{ issues(first: 250, filter: { state: { type: { in: [\"backlog\",\"unstarted\",\"started\"] } } }) "
    "{ nodes { identifier title priority state { name type } "
    "assignee { name email } labels { nodes { name } } updatedAt url } } }"
)


FORMAT_PER_PERSON_JS = r"""// Fan out one Telegram-send item per registered teammate.
// Each item carries chat_id + a formatted per-person message.
const resp = $input.first().json;
const issues = (resp.data && resp.data.issues && resp.data.issues.nodes) || [];
const ROSTER = __ROSTER__;

const CAT_PRIORITY = ['Dev','Marketing','Ops','BD','Customer Support','OMS','Whatsapp'];
const CAT_EMOJI = {
  'Dev':'💻','Marketing':'📣','Ops':'🔧','BD':'🤝','Customer Support':'💬','OMS':'📦','Whatsapp':'📲','Other':'📌'
};
const STALE_DAYS = 14;
const isStale = (u) => Date.now() - new Date(u).getTime() > STALE_DAYS * 86400000;
const priIcon = (p) => p === 1 ? '🚨' : p === 2 ? '🔺' : p === 4 ? '🔻' : '';

function bucket(issue) {
  const ln = (issue.labels.nodes || []).map(l => l.name);
  for (const c of CAT_PRIORITY) if (ln.includes(c)) return c;
  return 'Other';
}

function matchesPerson(issue, linearFirstName) {
  if (!issue.assignee) return false;
  const ln = (issue.assignee.name || '').toLowerCase();
  const le = (issue.assignee.email || '').toLowerCase();
  const want = linearFirstName.toLowerCase();
  return ln.startsWith(want + ' ') || ln === want || le.startsWith(want + '@') || le.includes(want);
}

const out = [];
const rosterKeys = Object.keys(ROSTER);
for (const key of rosterKeys) {
  const r = ROSTER[key];
  if (!r.chat_id) continue;

  const mine = issues.filter(i => matchesPerson(i, key));
  if (!mine.length) continue;  // skip empty inboxes — no daily nag if nothing's on you

  // group by category
  const groups = {};
  for (const c of CAT_PRIORITY) groups[c] = [];
  groups['Other'] = [];
  for (const i of mine) groups[bucket(i)].push(i);
  for (const c of Object.keys(groups)) {
    groups[c].sort((a, b) => {
      const pa = a.priority === 0 ? 99 : a.priority;
      const pb = b.priority === 0 ? 99 : b.priority;
      if (pa !== pb) return pa - pb;
      return new Date(a.updatedAt).getTime() - new Date(b.updatedAt).getTime();
    });
  }

  const total = mine.length;
  const wip   = mine.filter(i => i.state.type === 'started').length;
  const todo  = mine.filter(i => i.state.type === 'unstarted').length;
  const back  = mine.filter(i => i.state.type === 'backlog').length;
  const stale = mine.filter(i => isStale(i.updatedAt)).length;

  const lines = [];
  lines.push(`☀️ *Your open tasks — ${r.display}*`);
  lines.push(`_${total} open · ${wip} in progress · ${todo} todo · ${back} backlog_`);
  if (stale) lines.push(`_⚠️ ${stale} stale (>${STALE_DAYS}d no update)_`);
  lines.push('');

  const MAX = 8;  // show more per person since the list is just theirs
  for (const c of [...CAT_PRIORITY, 'Other']) {
    const list = groups[c];
    if (!list.length) continue;
    const e = CAT_EMOJI[c] || '•';
    lines.push(`${e} *${c} (${list.length})*`);
    for (const i of list.slice(0, MAX)) {
      const pi = priIcon(i.priority);
      const st = isStale(i.updatedAt) ? ' ⚠️' : '';
      const wipBadge = i.state.type === 'started' ? ' [WIP]' : '';
      lines.push(`  • [${i.identifier}](${i.url}) ${pi}${wipBadge} — ${(i.title||'').slice(0,70)}${st}`);
    }
    if (list.length > MAX) lines.push(`  _+${list.length - MAX} more_`);
    lines.push('');
  }
  lines.push('Reply with `mark TBP-X done` (in the team thread) to close.');

  out.push({ json: {
    chat_id: r.chat_id,
    text: lines.join('\n'),
    _person: key,
    _count: total,
  }});
}

return out;
""".replace("__ROSTER__", json.dumps(TEAM_DM_CHATS))


def build():
    schedule = {
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "0 18 * * *"}]}
        },
        "id": uid(), "name": "Daily 6PM SGT",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 200],
    }
    manual = {
        "parameters": {"httpMethod": "POST", "path": MANUAL_PATH, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Manual Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 400],
        "webhookId": MANUAL_PATH,
    }
    fetch = {
        "parameters": {
            "method": "POST",
            "url": "https://api.linear.app/graphql",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": LINEAR_TOKEN},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": json.dumps({"query": LINEAR_QUERY}),
            "options": {},
        },
        "id": uid(), "name": "Fetch Linear Issues",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [240, 300],
    }
    fanout = {
        "parameters": {"jsCode": FORMAT_PER_PERSON_JS},
        "id": uid(), "name": "Fan Out Per Person",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [480, 300],
    }
    # Send Telegram DM — n8n iterates this node once per input item.
    send = {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ "
                "chat_id: $json.chat_id, "
                "text: $json.text, "
                "parse_mode: 'Markdown', "
                "disable_web_page_preview: true "
                "}) }}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Send DM",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [720, 300],
    }

    nodes = [schedule, manual, fetch, fanout, send]
    connections = {
        schedule["name"]: {"main": [[{"node": fetch["name"],  "type": "main", "index": 0}]]},
        manual["name"]:   {"main": [[{"node": fetch["name"],  "type": "main", "index": 0}]]},
        fetch["name"]:    {"main": [[{"node": fanout["name"], "type": "main", "index": 0}]]},
        fanout["name"]:   {"main": [[{"node": send["name"],   "type": "main", "index": 0}]]},
    }
    return {
        "name": WF_NAME, "nodes": nodes, "connections": connections,
        "settings": {"executionOrder": "v1", "errorWorkflow": ERROR_ALERTER_ID, "timezone": "Asia/Singapore"},
    }


def find_existing():
    s, data = n8n("GET", "/workflows?limit=250")
    if s >= 300: return None
    for w in data.get("data", []):
        if w["name"] == WF_NAME:
            return w["id"]
    return None


def deploy():
    payload = build()
    existing = find_existing()
    if existing:
        s, body = n8n("PUT", f"/workflows/{existing}", payload)
        wf_id = existing
        print(f"PUT existing {wf_id} → {s}")
    else:
        s, body = n8n("POST", "/workflows", payload)
        wf_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST new {wf_id} → {s}")
    if s >= 300:
        print(body); raise SystemExit(1)
    n8n("PUT", f"/workflows/{wf_id}/transfer", {"destinationProjectId": TEAM_PROJECT_ID})
    s2, _ = n8n("POST", f"/workflows/{wf_id}/activate")
    print(f"activate → {s2}")
    if not TEAM_DM_CHATS:
        print("⚠️  TEAM_DM_CHATS is empty — workflow will be a no-op until teammates DM the bot.")
        print("    Onboard: have them /start @weslee_bot in private, paste chat_id into TEAM_DM_CHATS, redeploy.")
    return wf_id


def test_fire():
    print("test firing...")
    req = urllib.request.Request(
        f"https://n8n.thebonpet.com/webhook/{MANUAL_PATH}",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  HTTP {r.status}: {r.read().decode()[:200]}")
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:300]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_fire()
    else:
        deploy()

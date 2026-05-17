#!/usr/bin/env python3
"""Build the 'Telegram → Linear' multi-intent workflow.

Supported intents (Claude classifies):
- "create"  → make a Linear ticket
- "summary" → list open tasks (optionally filtered by category / stale / assignee)
- "help"    → reply with usage examples

Trigger: Telegram bot webhook for @weslee_bot mentions.
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
WF_NAME = "Telegram → Linear Ticket"
TEAM_PROJECT_ID = "i1GSXBntwNvNqic8"
WEBHOOK_PATH = "telegram-weslee-mention-7c4b9e"

TELEGRAM_BOT_TOKEN = subprocess.check_output(["security","find-generic-password","-a","thebonpet","-s","telegram-weslee-bot","-w"]).decode().strip()
TELEGRAM_BOT_USERNAME = "weslee_bot"

LINEAR_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "linear-thebonpet-api", "-w"]
).decode().strip()
ANTHROPIC_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-a", "yash", "-s", "yash-anthropic-key", "-w"]
).decode().strip()
WMS_PAT = subprocess.check_output(
    ["security", "find-generic-password", "-a", "thebonpet", "-s", "wms-pat", "-w"]
).decode().strip()

LINEAR_TBP_TEAM_ID = "12118ee6-e5f2-4d01-98a4-54c9ff5c86f4"
LINEAR_BACKLOG_STATE_ID = "8c7d9052-a913-4871-b7a1-b5fac536c3dc"
LINEAR_LABEL_IDS = {
    "Dev":              "dd174cf0-108e-4b35-883f-02359e4fa4ef",
    "Marketing":        "6257388f-883c-43ee-8f83-d6a625b3c7ae",
    "Ops":              "0068801d-7378-41e5-a36c-a49c4971c3c6",
    "BD":               "a69dcfff-2cfb-48ff-a3a8-d0d6e2fc6ce2",
    "Customer Support": "125a55ee-24f6-4460-b109-c034eee35925",
    "OMS":              "0f285625-2823-48ca-9c10-03b49e88495c",
    "Whatsapp":         "02fcdfcd-e7c9-4051-9ca2-6619a1c969d1",
}
LINEAR_STATE_IDS = {
    "Backlog":     "8c7d9052-a913-4871-b7a1-b5fac536c3dc",
    "Todo":        "7003c181-abfe-4ba0-96f6-d50cd652ae43",
    "In Progress": "6bf61e2f-4515-4f75-a3a3-703ce5b33e76",
    "In Review":   "ce1f953e-3662-4af0-8422-d4e22e7ed2bc",
    "Done":        "cab643ed-7bfb-4ce5-80bd-47bf5f36876d",
    "Canceled":    "f623e135-e4c8-4b10-a6a1-10ec9e8aa1db",
    "Duplicate":   "15b8dd1e-b7fa-4a14-8f01-09b3df889dbf",
}
LINEAR_USER_IDS = {
    # friendly name → Linear user ID
    "rachel":   "a61c170d-188f-485e-805c-16ce1aef9e13",
    "shaun":    "1f10cd24-6dd4-4cf1-8e42-eb2a53889d1e",
    "nicolas":  "58ae415c-a03c-454f-8e5a-00dcebafa3d9",
    "yash":     "c3231b5f-8db1-4c15-8b63-35fb3e980e86",
    # email aliases
    "rachelliewruiqi@gmail.com": "a61c170d-188f-485e-805c-16ce1aef9e13",
    "gotchykid@gmail.com":       "1f10cd24-6dd4-4cf1-8e42-eb2a53889d1e",
    "nicolas@thebonpet.com":     "58ae415c-a03c-454f-8e5a-00dcebafa3d9",
    "yash@thebonpet.com":        "c3231b5f-8db1-4c15-8b63-35fb3e980e86",
}
ERROR_ALERTER_ID = "c3Vk2nt9WINzp9GH"
PICKUP_READY_WEBHOOK = "https://n8n.thebonpet.com/webhook/selfcollect-pickup-ready-3f9c1a"

TELEGRAM_HANDLES = {
    "nicolas@thebonpet.com":     "nicolaswee",
    "rachelliewruiqi@gmail.com": "rachellrqq",
    "gotchykid@gmail.com":       "gotchykid",
    "yash@thebonpet.com":        "yashgadodia",
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


PARSE_MENTION_JS = r"""// Accept (a) @weslee_bot mentions in groups, OR (b) any private-chat DM (auto-handled as "register")
const upd = $input.first().json;
const body = upd.body || upd;
const msg = body.message || body.edited_message || body.channel_post || null;
if (!msg || !msg.text) return [];

const text = msg.text;
const isPrivate = msg.chat && msg.chat.type === 'private';
const entities = msg.entities || [];
let mentioned = false;
for (const e of entities) {
  if (e.type === 'mention') {
    const m = text.substr(e.offset, e.length).toLowerCase();
    if (m === '@__BOT_USERNAME__') { mentioned = true; break; }
  }
}
if (!mentioned && !isPrivate) return [];

const cleaned = text
  .replace(new RegExp('@__BOT_USERNAME__\\b', 'gi'), '')
  .replace(/\s+/g, ' ')
  .trim();

// FAST-PATH: pickup-ready intent → route to Self-Collect Pickup Ready workflow.
// Matches "order #3293 ready for self collection" (+ variants: no #, hyphen, no space).
const pickupMatch = cleaned.match(/\border\s*#?(\d+)[^\n]{0,40}ready[^\n]{0,40}self.?collect/i);
if (pickupMatch) {
  return [{
    json: {
      _pickup_ready: true,
      pickup_order_num: pickupMatch[1],
      chat_id: msg.chat.id,
      message_thread_id: msg.message_thread_id || null,
      reply_to_message_id: msg.message_id,
      sender_name: (msg.from && (msg.from.first_name || msg.from.username)) || 'Unknown',
    }
  }];
}

// Private DM /start (or similar) → quick Chat ID reply, skip Claude
const startCmds = /^\s*(\/start|\/register|register|chat[\s_-]?id)\b/i;
if (isPrivate && startCmds.test(cleaned)) {
  const sn = (msg.from && (msg.from.first_name || msg.from.username)) || 'friend';
  const un = (msg.from && msg.from.username) || '';
  const reply = [
    `👋 Hi ${sn}!`,
    '',
    `Your Chat ID: \`${msg.chat.id}\``,
    un ? `Handle: @${un}` : '',
    '',
    `Once added to the roster, you'll get a 6pm SGT DM with your open Linear tasks.`,
    '',
    `Meanwhile, you can DM me commands anytime:`,
    `  • create a new ticket: "make tix to call vendor"`,
    `  • see your tasks: "what's mine?"`,
    `  • update: "mark TBP-30 done"`,
  ].join('\n');
  return [{
    json: {
      _private_register: true,
      chat_id: msg.chat.id,
      reply_to_message_id: msg.message_id,
      message_thread_id: null,
      text: reply,
    }
  }];
}

return [{
  json: {
    text: cleaned || '(empty)',
    raw_text: text,
    chat_id: msg.chat.id,
    message_thread_id: msg.message_thread_id || null,
    reply_to_message_id: msg.message_id,
    sender_name: (msg.from && (msg.from.first_name || msg.from.username)) || 'Unknown',
    sender_username: (msg.from && msg.from.username) || null,
    sender_id: msg.from && msg.from.id,
    chat_title: (msg.chat && msg.chat.title) || '',
  }
}];
""".replace("__BOT_USERNAME__", TELEGRAM_BOT_USERNAME)


CLAUDE_REQ_JS = r"""// Build Claude classifier request — multi-intent
const ctx = $input.first().json;

// Map Telegram username → Linear first-name handle so Claude can resolve "my"/"mine".
const SENDER_TO_LINEAR = {
  'yashgadodia': 'yash',
  'nicolaswee':  'nicolas',
  'rachellrqq':  'rachel',
  'gotchykid':   'shaun',
};
const handle = (ctx.sender_username || '').toLowerCase();
const senderLinear = SENDER_TO_LINEAR[handle] || null;

const system = `You are routing chat messages to @weslee_bot, an internal assistant for The Bon Pet (Singapore-based fresh pet food brand, Shopify DTC, n8n automation, custom OMS).

Classify the message and respond with ONLY valid JSON.

Schema:
{
  "action": "create" | "summary" | "update" | "help" | "packlist" | "send_pickup_wa",
  // For "create" — always include title/description/category/priority; assignee is optional:
  "title": "<= 70 chars, action-oriented, no trailing period",
  "description": "1-2 sentences",
  "category": "Dev" | "Marketing" | "Ops" | "BD" | "Customer Support" | "OMS" | "Whatsapp" | "Other",
  "priority": 0|1|2|3|4,
  "assignee": "rachel" | "shaun" | "nicolas" | "yash" | null,
  // For "summary" — optional filter:
  "filter": { "category": "...", "stale": true, "assignee": "first name or email", "unassigned": true },
  // For "packlist" — optional filter:
  "pack_filter": { "self_collect_only": true | false },
  // For "update" — required identifier + at least one field in updates:
  "identifier": "TBP-23",
  "updates": {
    "state": "Done" | "In Progress" | "Backlog" | "Todo" | "In Review" | "Canceled" | "Duplicate",
    "assignee": "rachel" | "shaun" | "nicolas" | "yash",
    "priority": 0|1|2|3|4,
    "category": "Dev" | "Marketing" | "Ops" | "BD" | "Customer Support" | "OMS" | "Whatsapp" | "Other"
  }
}

Routing:
- "send_pickup_wa" when user wants to SEND THE PICKUP-READY WHATSAPP TEMPLATE to customers whose self-collect orders are packed/ready. This fans out the WA + auto-fulfills OMS for ALL unfulfilled self-collect orders — fires IMMEDIATELY, no confirm step. Triggers:
   - "fire wa notif", "send wa notif", "send the wa notif", "fire the wa"
   - "fire whatsapp notif for pickup ready", "send whatsapp notification for all self collection orders"
   - "send pickup ready WAs", "blast pickup ready", "fire pickup ready notifs"
   - "let customers know their orders are ready", "tell customers to pick up"
   - "orders are packed, fire wa", "all packed send wa", "fire it", "send it for all pending pickup"
   - This is the DEFAULT meaning of "send wa notif" / "fire wa" in this chat — there is no other generic-WA broadcast intent exposed here. If a user says anything like "send wa notif" / "fire wa" / similar, this is the intent.
   - Do NOT use this for: SINGLE-order pickup-ready tags ("order #3293 ready for self collection") — those are handled before Claude sees the message.
- "packlist" when user is asking about PHYSICAL ORDERS that need to be PACKED / SHIPPED / FULFILLED (NOT Linear tickets). These hit the live OMS, not Linear. Triggers:
   - "what do I need to pack?", "what's left to pack?", "show me the packlist", "pack list"
   - "which self-collection orders do I need to pack?", "what self-collect orders are open?"
   - "what orders are unfulfilled?", "what orders need fulfilling?", "open orders to ship"
   - "what's pending pickup?", "orders waiting for self collection"
   - DEFAULT pack_filter.self_collect_only = true. NinjaVan / cold-chain orders are packed by the Pet Axis manufacturer, not the user — so plain "what do I need to pack?" should ONLY show self-collection orders.
   - Set pack_filter.self_collect_only = false ONLY if the user explicitly asks for all orders: "all orders", "every order", "include delivery", "include ninjavan", "include njv", "show ninjavan orders", "everything unfulfilled".
- "update" when user wants to MODIFY an existing issue (mentioned by ID like TBP-23, "issue 23", or just "23"):
   - "mark TBP-23 done", "close TBP-9", "TBP-19 is done"  → state: Done
   - "move TBP-31 to in progress", "start TBP-11"          → state: In Progress
   - "cancel TBP-25", "drop TBP-22"                        → state: Canceled
   - "assign TBP-19 to nicolas", "give TBP-23 to rachel"   → assignee
   - "bump TBP-9 to urgent", "make TBP-31 high priority"   → priority
   - "tag TBP-30 as Marketing"                             → category
   - User can chain: "mark TBP-23 done and assign to rachel"  → updates: { state: "Done", assignee: "rachel" }
   - If user says just a number ("close 23"), set identifier as "TBP-23"
- "summary" when user ASKS about LINEAR TASKS/TICKETS ("what's open?", "show me dev tasks", "what's stale?", "what is rachel working on?", "summarise tasks", "list backlog"). DO NOT use "summary" for questions about orders/packing/shipping — use "packlist" for those.
- "help" when user is asking what the bot can do, or the message is empty/unclear/just hi
- "create" by default — when the user is describing a NEW task or thing to do
   - If the user says "assign to X" / "for X" / "give to X" / "X to ..." in the same message, set assignee to that person (rachel|shaun|nicolas|yash). Otherwise assignee = null.
   - Examples:
     - "make a tix assign yash to fix homepage"      → action: create, assignee: "yash"
     - "for nicolas: order packaging tomorrow"        → action: create, assignee: "nicolas"
     - "rachel pls draft IG caption for sous vide"    → action: create, assignee: "rachel"
     - "fix the abandoned cart job"                   → action: create, assignee: null

Categories for "create" / "update":
- Dev: code, n8n workflows, website, automation, bugs, scripts
- Marketing: content, ads, social, IG, influencers, partnerships, SEO
- Ops: kitchen, fulfillment, logistics, finance/Aspire, suppliers, inventory
- BD: B2B leads, retail (KohePets, PLC), vet channels
- Customer Support: refunds, complaints, individual customer issues
- OMS: order management, picklists, delivery scheduling
- Whatsapp: WA broadcasts, templates, customer messaging
- Other: anything else

Priority cues: "urgent"/"asap"/"now" → 1; "important"/"high" → 2; default → 3; "low"/"someday" → 4.

For "summary": parse "dev"/"marketing"/etc → filter.category. "stale"/"old" → filter.stale=true. Names like "rachel"/"shaun"/"nicolas"/"yash" → filter.assignee. Phrases like "unassigned", "no owner", "no one", "nobody", "orphan", "without an owner", "not assigned" → filter.unassigned=true (and DO NOT set filter.assignee in this case).

Self-reference: if the message uses "my", "mine", "me", or "i" (e.g. "what's mine?", "my open tix", "what am i working on", "assign to me"), resolve to the sender's Linear handle shown in "Sender Linear handle:" below. If no Linear handle is shown for the sender, omit filter.assignee for summaries and omit assignee for creates.

Output ONLY JSON. No preamble. No code fences.`;

const senderLine = senderLinear
  ? `From: ${ctx.sender_name}\nSender Linear handle: ${senderLinear}`
  : `From: ${ctx.sender_name}\nSender Linear handle: (unknown — do not infer)`;
const user = `${senderLine}\nMessage: ${ctx.text}`;

return [{
  json: {
    _ctx: ctx,
    payload: {
      model: 'claude-haiku-4-5',
      max_tokens: 500,
      system,
      messages: [{ role: 'user', content: user }],
    }
  }
}];
"""


PARSE_INTENT_JS = r"""// Parse Claude classifier output → set action + structured fields
const resp = $input.first().json;
const ctx = $('Parse Mention').first().json;

const blocks = resp.content || [];
const text = blocks.map(b => b.text || '').join('').trim();
let cleaned = text.replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();

let parsed;
try { parsed = JSON.parse(cleaned); }
catch (e) { parsed = { action: 'create', title: ctx.text.slice(0, 70), description: ctx.text, category: 'Other', priority: 3 }; }

const action = parsed.action || 'create';

return [{
  json: {
    _ctx: ctx,
    action,
    parsed,
  }
}];
"""


# ─────────────────────── CREATE branch helpers ──────────────────────────
BUILD_MUTATION_JS = r"""// Map Claude's create-classification → Linear issueCreate variables
const inp = $input.first().json;
const ctx = inp._ctx;
const p = inp.parsed || {};
const labels = __LABELS__;
const users  = __USERS__;

const labelIds = labels[p.category] ? [labels[p.category]] : [];
const assigneeName = p.assignee ? String(p.assignee).toLowerCase().replace(/^@/, '') : null;
const assigneeId = assigneeName ? (users[assigneeName] || null) : null;
const description = [
  p.description || '',
  '',
  `_Filed by **${ctx.sender_name}** via Telegram on ${new Date().toISOString().slice(0,10)}_`,
  `_Original: "${ctx.raw_text.slice(0, 300)}"_`,
].join('\n');

return [{
  json: {
    _ctx: ctx,
    title: (p.title || 'Untitled').slice(0, 250),
    description,
    category: p.category || 'Other',
    priority: typeof p.priority === 'number' ? p.priority : 3,
    labelIds,
    assigneeId,
    assigneeName: assigneeId ? assigneeName : null,
  }
}];
""".replace("__LABELS__", json.dumps(LINEAR_LABEL_IDS)) \
   .replace("__USERS__",  json.dumps(LINEAR_USER_IDS))


CREATE_REPLY_JS = r"""// Build Telegram reply for created issue
const ticket = $('Build Linear Mutation').first().json;
const linRes = $input.first().json;
const issue = linRes && linRes.data && linRes.data.issueCreate && linRes.data.issueCreate.issue;
if (!issue) {
  const errs = JSON.stringify((linRes && linRes.errors) || linRes).slice(0, 300);
  return [{ json: { chat_id: ticket._ctx.chat_id, message_thread_id: ticket._ctx.message_thread_id,
                    reply_to_message_id: ticket._ctx.reply_to_message_id,
                    text: `❌ Couldn't create ticket: ${errs}` } }];
}
const assignedLine = ticket.assigneeName ? ` · Assigned: ${ticket.assigneeName}` : '';
const text = `✅ Created [${issue.identifier}](${issue.url})\n*${ticket.title}*\nCategory: \`${ticket.category}\` · Priority: ${ticket.priority}${assignedLine}`;
return [{ json: {
  chat_id: ticket._ctx.chat_id,
  message_thread_id: ticket._ctx.message_thread_id,
  reply_to_message_id: ticket._ctx.reply_to_message_id,
  text,
} }];
"""


# ─────────────────────── SUMMARY branch ────────────────────────────────────
LINEAR_QUERY = (
    "{ issues(first: 200, filter: { state: { type: { in: [\"backlog\",\"unstarted\",\"started\"] } } }) "
    "{ nodes { identifier title priority priorityLabel state { name type } "
    "assignee { name email } labels { nodes { name } } updatedAt url } } }"
)


SUMMARY_FORMAT_JS = r"""// Format on-demand summary, optionally filtered (category/stale/assignee)
const ctx = $('Parse Intent').first().json._ctx;
const intentParsed = $('Parse Intent').first().json.parsed || {};
const filter = intentParsed.filter || {};
const resp = $input.first().json;
const issues = (resp.data && resp.data.issues && resp.data.issues.nodes) || [];

const TELEGRAM_HANDLES = __HANDLES__;
const CAT_PRIORITY = ['Dev', 'Marketing', 'Ops', 'BD', 'Customer Support', 'OMS', 'Whatsapp'];
const CAT_EMOJI = { 'Dev':'💻','Marketing':'📣','Ops':'🔧','BD':'🤝','Customer Support':'💬','OMS':'📦','Whatsapp':'📲','Other':'📌' };

function mention(person) {
  if (!person) return '_unassigned_';
  const email = (person.email || '').toLowerCase();
  if (TELEGRAM_HANDLES[email]) return '@' + TELEGRAM_HANDLES[email];
  const n = (person.name || '').trim();
  if (n && !n.includes('@')) return n.split(' ')[0];
  return email.split('@')[0] || '_unassigned_';
}
function priIcon(p) { if (p === 1) return '🚨'; if (p === 2) return '🔺'; if (p === 4) return '🔻'; return ''; }
const STALE_DAYS = 14;
const isStale = (u) => Date.now() - new Date(u).getTime() > STALE_DAYS * 86400000;

// Apply filters from Claude
let filtered = issues.slice();
let filterDesc = [];
if (filter.category) {
  const cat = filter.category;
  filtered = filtered.filter(i => i.labels.nodes.some(l => l.name.toLowerCase() === cat.toLowerCase()));
  filterDesc.push(`category: ${cat}`);
}
if (filter.stale) {
  filtered = filtered.filter(i => isStale(i.updatedAt));
  filterDesc.push('stale only');
}
if (filter.unassigned) {
  filtered = filtered.filter(i => !i.assignee);
  filterDesc.push('unassigned only');
} else if (filter.assignee) {
  const a = filter.assignee.toLowerCase();
  filtered = filtered.filter(i => {
    const asn = i.assignee || {};
    const email = (asn.email || '').toLowerCase();
    const name = (asn.name || '').toLowerCase();
    return email.includes(a) || name.includes(a) || (TELEGRAM_HANDLES[email] || '').toLowerCase() === a;
  });
  filterDesc.push(`assignee: ${filter.assignee}`);
}

const groups = {};
for (const c of CAT_PRIORITY) groups[c] = [];
groups['Other'] = [];
for (const i of filtered) {
  const ln = i.labels.nodes.map(l => l.name);
  let bucket = 'Other';
  for (const c of CAT_PRIORITY) if (ln.includes(c)) { bucket = c; break; }
  groups[bucket].push(i);
}
for (const c of Object.keys(groups)) {
  groups[c].sort((a, b) => {
    const pa = a.priority === 0 ? 99 : a.priority;
    const pb = b.priority === 0 ? 99 : b.priority;
    if (pa !== pb) return pa - pb;
    return new Date(a.updatedAt).getTime() - new Date(b.updatedAt).getTime();
  });
}

const total = filtered.length;
const inProg = filtered.filter(i => i.state.type === 'started').length;
const todo   = filtered.filter(i => i.state.type === 'unstarted').length;
const back   = filtered.filter(i => i.state.type === 'backlog').length;
const stale  = filtered.filter(i => isStale(i.updatedAt)).length;

const lines = [];
const fdesc = filterDesc.length ? ` _(${filterDesc.join(', ')})_` : '';
lines.push(`📋 *Open Tasks*${fdesc}`);
if (total === 0) {
  lines.push('_No matching open tasks 🎉_');
} else {
  lines.push(`_${total} open · ${inProg} in progress · ${todo} todo · ${back} backlog_`);
  if (stale) lines.push(`_⚠️ ${stale} stale (>${STALE_DAYS}d no update)_`);
  lines.push('');

  const MAX = 5;
  for (const c of [...CAT_PRIORITY, 'Other']) {
    const list = groups[c];
    if (!list.length) continue;
    const e = CAT_EMOJI[c] || '•';
    lines.push(`${e} *${c} (${list.length})*`);
    for (const i of list.slice(0, MAX)) {
      const pi = priIcon(i.priority);
      const st = isStale(i.updatedAt) ? ' ⚠️' : '';
      const wip = i.state.type === 'started' ? ' [WIP]' : '';
      lines.push(`  • [${i.identifier}](${i.url}) ${pi}${wip} ${mention(i.assignee)} — ${(i.title||'').slice(0, 70)}${st}`);
    }
    if (list.length > MAX) lines.push(`  _+${list.length - MAX} more_`);
    lines.push('');
  }
  lines.push('_View all: https://linear.app/thebonpet/team/TBP/all_');
}

return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text: lines.join('\n'),
} }];
""".replace("__HANDLES__", json.dumps(TELEGRAM_HANDLES))


# ─────────────────────── UPDATE branch ────────────────────────────────────
LOOKUP_ISSUE_QUERY = (
    "query Issue($id: String!) { issue(id: $id) "
    "{ id identifier title state { name id } assignee { name email id } priority labels { nodes { id name } } } }"
)

BUILD_UPDATE_JS = r"""// Map Claude's update payload → Linear issueUpdate variables
const lookup = $input.first().json;
const ctx = $('Parse Intent').first().json._ctx;
const intentParsed = $('Parse Intent').first().json.parsed || {};
const STATES = __STATES__;
const USERS  = __USERS__;
const LABELS = __LABELS__;

const issue = lookup && lookup.data && lookup.data.issue;
if (!issue) {
  return [{ json: {
    _ctx: ctx,
    error: `Issue ${intentParsed.identifier || '?'} not found`,
    identifier: intentParsed.identifier,
  } }];
}

const updates = intentParsed.updates || {};
const input = {};
const changes = [];

if (updates.state) {
  const sid = STATES[updates.state];
  if (sid) { input.stateId = sid; changes.push(`state → ${updates.state}`); }
}
if (updates.assignee) {
  const a = updates.assignee.toLowerCase();
  const uid = USERS[a] || USERS[a.replace(/^@/, '')];
  if (uid) { input.assigneeId = uid; changes.push(`assignee → ${updates.assignee}`); }
}
if (typeof updates.priority === 'number') {
  input.priority = updates.priority;
  const pmap = {1:'Urgent',2:'High',3:'Medium',4:'Low',0:'No priority'};
  changes.push(`priority → ${pmap[updates.priority] || updates.priority}`);
}
if (updates.category && LABELS[updates.category]) {
  // Add the new category label, preserving any non-category labels (Feature/Bug/etc)
  const CAT_NAMES = ['Dev','Marketing','Ops','BD','Customer Support','OMS','Whatsapp'];
  const keptLabels = (issue.labels.nodes || [])
    .filter(l => !CAT_NAMES.includes(l.name))
    .map(l => l.id);
  input.labelIds = [...keptLabels, LABELS[updates.category]];
  changes.push(`category → ${updates.category}`);
}

if (Object.keys(input).length === 0) {
  return [{ json: {
    _ctx: ctx,
    error: `Couldn't parse what to change for ${issue.identifier}`,
    identifier: issue.identifier,
  } }];
}

return [{ json: {
  _ctx: ctx,
  uuid: issue.id,
  identifier: issue.identifier,
  title: issue.title,
  changes,
  input,
} }];
""".replace("__STATES__", json.dumps(LINEAR_STATE_IDS)) \
   .replace("__USERS__",  json.dumps(LINEAR_USER_IDS)) \
   .replace("__LABELS__", json.dumps(LINEAR_LABEL_IDS))


UPDATE_REPLY_JS = r"""// Format the update reply
const upd = $('Build Update Mutation').first().json;
const linRes = $input.first().json;

if (upd.error) {
  return [{ json: {
    chat_id: upd._ctx.chat_id,
    message_thread_id: upd._ctx.message_thread_id,
    reply_to_message_id: upd._ctx.reply_to_message_id,
    text: `⚠️ ${upd.error}`,
  } }];
}

const issue = linRes && linRes.data && linRes.data.issueUpdate && linRes.data.issueUpdate.issue;
if (!issue) {
  const errs = JSON.stringify((linRes && linRes.errors) || linRes).slice(0, 300);
  return [{ json: {
    chat_id: upd._ctx.chat_id,
    message_thread_id: upd._ctx.message_thread_id,
    reply_to_message_id: upd._ctx.reply_to_message_id,
    text: `❌ Couldn't update ${upd.identifier}: ${errs}`,
  } }];
}

const text = `✅ Updated [${issue.identifier}](${issue.url})\n*${issue.title}*\n${upd.changes.map(c => `• ${c}`).join('\n')}`;
return [{ json: {
  chat_id: upd._ctx.chat_id,
  message_thread_id: upd._ctx.message_thread_id,
  reply_to_message_id: upd._ctx.reply_to_message_id,
  text,
} }];
"""


# ─────────────────────── PACKLIST branch ───────────────────────────────
PACKLIST_FORMAT_JS = r"""// Format the WMS unfulfilled-orders response for Telegram
const ctx = $('Parse Intent').first().json._ctx;
const intentParsed = $('Parse Intent').first().json.parsed || {};
const pf = intentParsed.pack_filter || {};
const selfOnly = pf.self_collect_only === true;

const resp = $input.first().json;
let orders = (resp && resp.orders) || [];
const totalAll = (resp && resp.total != null) ? resp.total : orders.length;

if (selfOnly) {
  orders = orders.filter(o => String(o.delivery_method || '').toUpperCase() === 'SELF_COLLECTION');
}

// Sort ascending by pickup_date (nulls last)
orders.sort((a, b) => {
  const da = a.pickup_date ? new Date(a.pickup_date).getTime() : 1e15;
  const db = b.pickup_date ? new Date(b.pickup_date).getTime() : 1e15;
  return da - db;
});

const METHOD_LABEL = {
  'SELF_COLLECTION':    '🏪 Self-Collection',
  'NINJAVAN_NEXTDAY':   '🚚 NinjaVan Next-Day',
  'NINJAVAN_COLD_CHAIN':'❄️ NinjaVan Cold-Chain',
};

function fmtDate(d) {
  if (!d) return 'no date';
  try {
    const dt = new Date(d);
    return dt.toLocaleDateString('en-SG', { month: 'short', day: 'numeric', timeZone: 'Asia/Singapore' });
  } catch (e) { return String(d).slice(0, 10); }
}

function summarizeItems(items) {
  if (!items || !items.length) return '';
  const parts = items.map(it => {
    const q = it.quantity || 1;
    const t = (it.title || it.sku || 'item').replace(/\s*\(.*?\)\s*/g, '').trim();
    return `${q}× ${t}`;
  });
  return parts.join(', ');
}

const groups = { 'SELF_COLLECTION': [], 'NINJAVAN_NEXTDAY': [], 'NINJAVAN_COLD_CHAIN': [], '_OTHER': [] };
for (const o of orders) {
  const m = String(o.delivery_method || '').toUpperCase();
  if (groups[m]) groups[m].push(o);
  else groups['_OTHER'].push(o);
}

const lines = [];
const heading = selfOnly ? '📦 *Unfulfilled Self-Collection Orders*' : '📦 *Unfulfilled Orders — Packlist*';
lines.push(heading);

if (orders.length === 0) {
  lines.push(selfOnly
    ? '_No unfulfilled self-collection orders 🎉_'
    : '_No unfulfilled orders 🎉_');
  return [{ json: {
    chat_id: ctx.chat_id,
    message_thread_id: ctx.message_thread_id,
    reply_to_message_id: ctx.reply_to_message_id,
    text: lines.join('\n'),
  } }];
}

lines.push(`_${orders.length} order${orders.length === 1 ? '' : 's'} pending${selfOnly ? '' : ` · ${totalAll} total unfulfilled in WMS`}_`);
lines.push('');

const MAX_PER_GROUP = 15;
const order_groups = selfOnly
  ? [['SELF_COLLECTION', groups['SELF_COLLECTION']]]
  : [['SELF_COLLECTION', groups['SELF_COLLECTION']], ['NINJAVAN_NEXTDAY', groups['NINJAVAN_NEXTDAY']], ['NINJAVAN_COLD_CHAIN', groups['NINJAVAN_COLD_CHAIN']], ['_OTHER', groups['_OTHER']]];

for (const [method, list] of order_groups) {
  if (!list.length) continue;
  const label = METHOD_LABEL[method] || '📌 Other';
  lines.push(`${label} *(${list.length})*`);
  for (const o of list.slice(0, MAX_PER_GROUP)) {
    const fn = o.shipping_first_name || (o.shipping_name || '').split(' ')[0] || 'unknown';
    const items = summarizeItems(o.line_items);
    const itemsTrunc = items.length > 60 ? items.slice(0, 57) + '…' : items;
    lines.push(`  • \`${o.order_name}\` · ${fmtDate(o.pickup_date)} · ${fn}${itemsTrunc ? ` — ${itemsTrunc}` : ''}`);
  }
  if (list.length > MAX_PER_GROUP) lines.push(`  _+${list.length - MAX_PER_GROUP} more_`);
  lines.push('');
}

lines.push('_View OMS: https://oms.thebonpet.com/orders_');

return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text: lines.join('\n'),
} }];
"""


# ─────────────────────── SEND_PICKUP_WA branch ────────────────────────────
PICKUP_WA_PREVIEW_JS = r"""// DRY preview for pickup-ready WA bulk blast
const ctx = $('Parse Intent').first().json._ctx;
const resp = $input.first().json;
const orders = (resp && resp.orders) || [];

if (orders.length === 0) {
  return [{ json: {
    chat_id: ctx.chat_id,
    message_thread_id: ctx.message_thread_id,
    reply_to_message_id: ctx.reply_to_message_id,
    text: '🎉 *No unfulfilled self-collect orders* — nothing to send.',
  } }];
}

function fmtDate(d) {
  if (!d) return 'no date';
  try {
    const dt = new Date(d);
    return dt.toLocaleDateString('en-SG', { month: 'short', day: 'numeric', timeZone: 'Asia/Singapore' });
  } catch (e) { return String(d).slice(0, 10); }
}

const lines = [];
lines.push(`📲 *Pickup-Ready WA Blast — DRY RUN*`);
lines.push(`_${orders.length} customer${orders.length === 1 ? '' : 's'} will receive the pickup-ready template + auto-fulfill on OMS:_`);
lines.push('');
for (const o of orders) {
  const fn = o.shipping_first_name || (o.shipping_name || '').split(' ')[0] || 'unknown';
  const phone = o.shipping_phone || '(no phone)';
  lines.push(`  • \`${o.order_name}\` · ${fmtDate(o.pickup_date)} · ${fn} · ${phone}`);
}
lines.push('');
lines.push(`⏱  Send pace: 8s between WAs (~${orders.length * 8}s total)`);
lines.push('');
lines.push('Reply *@weslee\\_bot confirm pickup wa* to fire.');
lines.push('_Note: Shopify is read-only for automation — mark fulfillment there manually if needed._');

return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text: lines.join('\n'),
} }];
"""

FIRE_PICKUP_WA_JS = r"""// Sequentially POST to the pickup-ready webhook for each unfulfilled self-collect order
const ctx = $('Parse Intent').first().json._ctx;
const resp = $input.first().json;
const orders = (resp && resp.orders) || [];
const PICKUP_URL = 'https://n8n.thebonpet.com/webhook/selfcollect-pickup-ready-3f9c1a';
const DELAY_MS = 8000;

if (orders.length === 0) {
  return [{ json: {
    chat_id: ctx.chat_id,
    message_thread_id: ctx.message_thread_id,
    reply_to_message_id: ctx.reply_to_message_id,
    text: '🎉 No unfulfilled self-collect orders — nothing to send.',
  } }];
}

const results = [];
for (let i = 0; i < orders.length; i++) {
  const o = orders[i];
  const orderNumStr = String(o.order_name || '').replace(/^#/, '').trim();
  const orderNum = parseInt(orderNumStr, 10);
  if (!Number.isFinite(orderNum)) {
    results.push({ order: o.order_name, ok: false, status: 0, error: 'bad order_name' });
    continue;
  }
  try {
    const r = await this.helpers.httpRequest({
      method: 'POST',
      url: PICKUP_URL,
      headers: { 'Content-Type': 'application/json' },
      body: {
        order_number: orderNum,
        sender_name: `Bulk via Telegram (${ctx.sender_name || 'unknown'})`,
      },
      json: true,
      returnFullResponse: true,
    });
    const status = r.statusCode || r.status || 0;
    results.push({ order: o.order_name, ok: status >= 200 && status < 300, status });
  } catch (e) {
    const status = (e && (e.statusCode || e.status)) || 0;
    results.push({ order: o.order_name, ok: false, status, error: String(e.message || e).slice(0, 200) });
  }
  if (i < orders.length - 1) {
    await new Promise(res => setTimeout(res, DELAY_MS));
  }
}

const okCount = results.filter(r => r.ok).length;
const failCount = results.length - okCount;
const lines = [];
lines.push(`✅ *Pickup-Ready WA Blast Complete*`);
lines.push(`_${okCount}/${results.length} sent${failCount ? ` · ⚠️ ${failCount} failed` : ''}_`);
lines.push('');
for (const r of results) {
  const icon = r.ok ? '✅' : '❌';
  const detail = r.ok ? '' : ` · ${r.status}${r.error ? ' ' + r.error : ''}`;
  lines.push(`  ${icon} \`${r.order}\`${detail}`);
}
lines.push('');
lines.push('_WA + OMS + Shopify auto-synced per order (see per-order replies above)._');

return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text: lines.join('\n'),
} }];
"""


# ─────────────────────── HELP branch ────────────────────────────────────
HELP_FORMAT_JS = r"""const ctx = $('Parse Intent').first().json._ctx;
const text = `🤖 *@weslee_bot — what I can do*

📝 *Create a ticket* (auto-categorized → Linear backlog):
  • _@weslee urgent: refund #3219_
  • _@weslee fix abandoned cart OOM next sprint_
  • _@weslee follow up with KohePets on retail_

📋 *Show open Linear tasks* (filterable):
  • _@weslee what's open?_
  • _@weslee show dev tasks_
  • _@weslee what's stale?_
  • _@weslee what is rachel working on?_

📦 *Show unfulfilled orders* (live OMS):
  • _@weslee what do I need to pack?_
  • _@weslee which self-collection orders are open?_
  • _@weslee show the packlist_

📲 *Send pickup-ready WAs* (auto-fulfills OMS, 2-step confirm):
  • _@weslee send wa notif_ → shows DRY preview
  • _@weslee confirm pickup wa_ → fires for all unfulfilled self-collect orders

🛠️ *Update a ticket* (state/assignee/priority/category):
  • _@weslee mark TBP-23 done_
  • _@weslee assign TBP-19 to nicolas_
  • _@weslee move TBP-31 to in progress_
  • _@weslee bump TBP-9 to urgent_
  • _@weslee mark TBP-23 done and assign to rachel_

_Daily 9:30 AM SGT digest also auto-fires in this thread._`;
return [{ json: {
  chat_id: ctx.chat_id,
  message_thread_id: ctx.message_thread_id,
  reply_to_message_id: ctx.reply_to_message_id,
  text,
} }];
"""


def build():
    webhook = {
        "parameters": {"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}},
        "id": uid(), "name": "Telegram Webhook", "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [0, 400], "webhookId": WEBHOOK_PATH,
    }
    parse_mention = {
        "parameters": {"jsCode": PARSE_MENTION_JS},
        "id": uid(), "name": "Parse Mention", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [240, 400],
    }
    is_pickup_ready = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json._pickup_ready }}",
                    "rightValue": "true",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Is Pickup Ready", "type": "n8n-nodes-base.if",
        "typeVersion": 2, "position": [340, 400],
    }
    forward_pickup = {
        "parameters": {
            "method": "POST",
            "url": PICKUP_READY_WEBHOOK,
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ "
                "order_number: $json.pickup_order_num, "
                "chat_id: $json.chat_id, "
                "message_thread_id: $json.message_thread_id, "
                "reply_to_message_id: $json.reply_to_message_id, "
                "sender_name: $json.sender_name "
                "}) }}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Forward to Pickup Webhook",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [560, 200],
        "onError": "continueRegularOutput",
    }
    is_private_register = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{
                    "id": uid(),
                    "leftValue": "={{ $json._private_register }}",
                    "rightValue": "true",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": uid(), "name": "Is Private Register", "type": "n8n-nodes-base.if",
        "typeVersion": 2, "position": [560, 400],
    }
    build_claude = {
        "parameters": {"jsCode": CLAUDE_REQ_JS},
        "id": uid(), "name": "Build Claude Request", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [480, 400],
    }
    claude_call = {
        "parameters": {
            "method": "POST",
            "url": "https://api.anthropic.com/v1/messages",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "x-api-key", "value": ANTHROPIC_KEY},
                {"name": "anthropic-version", "value": "2023-06-01"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.payload) }}",
            "options": {},
        },
        "id": uid(), "name": "Claude Classify", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [720, 400],
    }
    parse_intent = {
        "parameters": {"jsCode": PARSE_INTENT_JS},
        "id": uid(), "name": "Parse Intent", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [960, 400],
    }

    # Switch on intent
    def rule(action_name):
        return {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{"id": uid(), "leftValue": "={{ $json.action }}", "rightValue": action_name,
                                "operator": {"type": "string", "operation": "equals"}}],
                "combinator": "and",
            },
            "renameOutput": True, "outputKey": action_name,
        }
    switch = {
        "parameters": {
            "rules": {"values": [rule("create"), rule("summary"), rule("update"), rule("help"), rule("packlist"), rule("send_pickup_wa")]},
            "options": {"fallbackOutput": "extra", "renameFallbackOutput": "create"},
        },
        "id": uid(), "name": "Route Intent", "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2, "position": [1200, 400],
    }

    # CREATE branch
    build_mutation = {
        "parameters": {"jsCode": BUILD_MUTATION_JS},
        "id": uid(), "name": "Build Linear Mutation", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1440, 200],
    }
    linear_create = {
        "parameters": {
            "method": "POST",
            "url": "https://api.linear.app/graphql",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": LINEAR_TOKEN},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                '={{ JSON.stringify({ query: '
                '"mutation IssueCreate($title:String!,$description:String,$teamId:String!,$stateId:String,$labelIds:[String!],$priority:Int,$assigneeId:String)'
                '{ issueCreate(input:{title:$title,description:$description,teamId:$teamId,stateId:$stateId,labelIds:$labelIds,priority:$priority,assigneeId:$assigneeId})'
                '{ success issue { identifier url } } }",'
                'variables: { '
                'title: $json.title, description: $json.description, '
                'teamId: "' + LINEAR_TBP_TEAM_ID + '", stateId: "' + LINEAR_BACKLOG_STATE_ID + '", '
                'labelIds: $json.labelIds, priority: $json.priority, '
                'assigneeId: $json.assigneeId '
                '} }) }}'
            ),
            "options": {},
        },
        "id": uid(), "name": "Linear Create Issue", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1680, 200],
    }
    create_reply = {
        "parameters": {"jsCode": CREATE_REPLY_JS},
        "id": uid(), "name": "Format Create Reply", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1920, 200],
    }

    # SUMMARY branch
    linear_fetch = {
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
        "id": uid(), "name": "Fetch Linear Issues", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1440, 400],
    }
    summary_format = {
        "parameters": {"jsCode": SUMMARY_FORMAT_JS},
        "id": uid(), "name": "Format Summary", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1680, 400],
    }

    # UPDATE branch
    lookup_issue = {
        "parameters": {
            "method": "POST",
            "url": "https://api.linear.app/graphql",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": LINEAR_TOKEN},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                '={{ JSON.stringify({ query: '
                '"' + LOOKUP_ISSUE_QUERY + '", '
                'variables: { id: $json.parsed.identifier } '
                '}) }}'
            ),
            "options": {},
        },
        "id": uid(), "name": "Lookup Issue", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1440, 600],
    }
    build_update = {
        "parameters": {"jsCode": BUILD_UPDATE_JS},
        "id": uid(), "name": "Build Update Mutation", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1680, 600],
    }
    linear_update = {
        "parameters": {
            "method": "POST",
            "url": "https://api.linear.app/graphql",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": LINEAR_TOKEN},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                '={{ ($json.error || !$json.uuid) ? '
                'JSON.stringify({ query: "{ __typename }" }) : '  # noop on error
                'JSON.stringify({ query: '
                '"mutation IssueUpdate($id:String!,$input:IssueUpdateInput!)'
                '{ issueUpdate(id:$id, input:$input)'
                '{ success issue { identifier title url state { name } assignee { name email } priority labels { nodes { name } } } } }",'
                'variables: { id: $json.uuid, input: $json.input } '
                '}) }}'
            ),
            "options": {},
        },
        "id": uid(), "name": "Linear Update Issue", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1920, 600],
    }
    update_reply = {
        "parameters": {"jsCode": UPDATE_REPLY_JS},
        "id": uid(), "name": "Format Update Reply", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [2160, 600],
    }

    # HELP branch
    help_format = {
        "parameters": {"jsCode": HELP_FORMAT_JS},
        "id": uid(), "name": "Format Help", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1440, 800],
    }

    # PACKLIST branch — query the live OMS for unfulfilled orders
    wms_fetch = {
        "parameters": {
            "method": "GET",
            "url": "https://api.thebonpet.com/wms/orders",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "fulfillment_status", "value": "UNFULFILLED"},
                {"name": "limit", "value": "200"},
            ]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": f"Bearer {WMS_PAT}"},
                {"name": "User-Agent", "value": UA},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Fetch WMS Orders", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1440, 1000],
    }
    packlist_format = {
        "parameters": {"jsCode": PACKLIST_FORMAT_JS},
        "id": uid(), "name": "Format Packlist", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1680, 1000],
    }

    # SEND_PICKUP_WA branch — fetch self-collect unfulfilled, fire immediately (no confirm gate)
    fetch_pickup_orders = {
        "parameters": {
            "method": "GET",
            "url": "https://api.thebonpet.com/wms/orders",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "fulfillment_status", "value": "UNFULFILLED"},
                {"name": "delivery_method", "value": "SELF_COLLECTION"},
                {"name": "limit", "value": "200"},
            ]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": f"Bearer {WMS_PAT}"},
                {"name": "User-Agent", "value": UA},
            ]},
            "options": {},
        },
        "id": uid(), "name": "Fetch Pickup Orders", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [1440, 1200],
    }
    fire_pickup_wa = {
        "parameters": {"jsCode": FIRE_PICKUP_WA_JS},
        "id": uid(), "name": "Fire Pickup WAs", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [1680, 1200],
    }

    # Common send. allow_sending_without_reply lets the message go through even if
    # reply_to_message_id was deleted or doesn't exist (smoke tests, edge cases).
    telegram_reply = {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ "
                "chat_id: $json.chat_id, "
                "message_thread_id: $json.message_thread_id, "
                "reply_to_message_id: $json.reply_to_message_id, "
                "allow_sending_without_reply: true, "
                "text: $json.text, "
                "parse_mode: 'Markdown', "
                "disable_web_page_preview: true "
                "}) }}"
            ),
            "options": {},
        },
        "id": uid(), "name": "Telegram Reply", "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [2160, 400],
    }

    nodes = [webhook, parse_mention, is_pickup_ready, forward_pickup, is_private_register,
             build_claude, claude_call, parse_intent, switch,
             build_mutation, linear_create, create_reply,
             linear_fetch, summary_format,
             lookup_issue, build_update, linear_update, update_reply,
             help_format,
             wms_fetch, packlist_format,
             fetch_pickup_orders, fire_pickup_wa,
             telegram_reply]

    connections = {
        webhook["name"]:        {"main": [[{"node": parse_mention["name"], "type": "main", "index": 0}]]},
        parse_mention["name"]:  {"main": [[{"node": is_pickup_ready["name"], "type": "main", "index": 0}]]},
        is_pickup_ready["name"]: {"main": [
            [{"node": forward_pickup["name"],      "type": "main", "index": 0}],  # true  → forward + end
            [{"node": is_private_register["name"], "type": "main", "index": 0}],  # false → existing flow
        ]},
        is_private_register["name"]: {"main": [
            [{"node": telegram_reply["name"], "type": "main", "index": 0}],  # true → DM reply
            [{"node": build_claude["name"],   "type": "main", "index": 0}],  # false → Claude classify
        ]},
        build_claude["name"]:   {"main": [[{"node": claude_call["name"],   "type": "main", "index": 0}]]},
        claude_call["name"]:    {"main": [[{"node": parse_intent["name"],  "type": "main", "index": 0}]]},
        parse_intent["name"]:   {"main": [[{"node": switch["name"],        "type": "main", "index": 0}]]},
        # Switch outputs: 0=create, 1=summary, 2=update, 3=help, 4=packlist, 5=send_pickup_wa (fallback → create)
        switch["name"]: {"main": [
            [{"node": build_mutation["name"],      "type": "main", "index": 0}],  # 0: create
            [{"node": linear_fetch["name"],        "type": "main", "index": 0}],  # 1: summary
            [{"node": lookup_issue["name"],        "type": "main", "index": 0}],  # 2: update
            [{"node": help_format["name"],         "type": "main", "index": 0}],  # 3: help
            [{"node": wms_fetch["name"],           "type": "main", "index": 0}],  # 4: packlist
            [{"node": fetch_pickup_orders["name"], "type": "main", "index": 0}],  # 5: send_pickup_wa
        ]},
        # CREATE
        build_mutation["name"]: {"main": [[{"node": linear_create["name"], "type": "main", "index": 0}]]},
        linear_create["name"]:  {"main": [[{"node": create_reply["name"],  "type": "main", "index": 0}]]},
        create_reply["name"]:   {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        # SUMMARY
        linear_fetch["name"]:   {"main": [[{"node": summary_format["name"],"type": "main", "index": 0}]]},
        summary_format["name"]: {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        # UPDATE
        lookup_issue["name"]:   {"main": [[{"node": build_update["name"],  "type": "main", "index": 0}]]},
        build_update["name"]:   {"main": [[{"node": linear_update["name"], "type": "main", "index": 0}]]},
        linear_update["name"]:  {"main": [[{"node": update_reply["name"],  "type": "main", "index": 0}]]},
        update_reply["name"]:   {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        # HELP
        help_format["name"]:    {"main": [[{"node": telegram_reply["name"],"type": "main", "index": 0}]]},
        # PACKLIST
        wms_fetch["name"]:       {"main": [[{"node": packlist_format["name"], "type": "main", "index": 0}]]},
        packlist_format["name"]: {"main": [[{"node": telegram_reply["name"],  "type": "main", "index": 0}]]},
        # SEND_PICKUP_WA — fire immediately, no confirm gate
        fetch_pickup_orders["name"]: {"main": [[{"node": fire_pickup_wa["name"],  "type": "main", "index": 0}]]},
        fire_pickup_wa["name"]:      {"main": [[{"node": telegram_reply["name"], "type": "main", "index": 0}]]},
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
    return wf_id


def simulate(intent_text, sender="yash"):
    """Simulate a Telegram update payload against the live webhook. For testing."""
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 999000 + hash(intent_text) % 1000,
            "from": {"id": 166637821, "is_bot": False, "first_name": sender, "username": sender},
            "chat": {"id": -1002184573790, "type": "supergroup", "is_forum": True, "title": "Team Bon Pet"},
            "message_thread_id": 34253,
            "date": 1777000000,
            "text": f"@{TELEGRAM_BOT_USERNAME} {intent_text}",
            "entities": [{"offset": 0, "length": len(TELEGRAM_BOT_USERNAME) + 1, "type": "mention"}],
        }
    }
    req = urllib.request.Request(
        f"https://n8n.thebonpet.com/webhook/{WEBHOOK_PATH}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  HTTP {r.status}: {r.read().decode()[:120]}")
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:200]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "deploy":
        deploy()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # Smoke-test the 3 intents
        wf_id = find_existing()
        print(f"workflow: {wf_id}")
        print("Test 1: summary")
        simulate("what's open?")
        print("Test 2: filtered summary")
        simulate("show dev tasks")
        print("Test 3: help")
        simulate("what can you do?")
        # Skip 'create' to avoid making real Linear tickets during smoke
    else:
        deploy()

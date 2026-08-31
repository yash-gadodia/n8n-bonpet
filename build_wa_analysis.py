#!/usr/bin/env python3
"""WhatsApp Complaint Analysis + CSAT — weekly LLM classification of customer chats.

Architecture:
  Schedule (Mon 09:00 SGT) → Compute Window → Fetch Conversations (paginated)
    → Filter Classifiable → Classify (Anthropic, 1 call per conversation)
    → Build Rows → Store Analysis (POST /wms/wa-analysis) → Telegram

Only conversations with at least one INBOUND message are classified. Measured live
2026-08-09: 258 WhatsApp threads in a week, 45 with a customer reply. The other 213
are outbound-only order confirmations - classifying them would dilute the complaint
rate ~6x and burn ~70k tokens a week on threads that contain no customer signal.

Reuses two existing credentials, creates none:
  TBP OMS WMS PAT (httpHeaderAuth) -> Bearer bonpet_pat_..., valid on both
    api.thebonpet.com/whatsapp/admin/* (since PR #128) and /wms/*
  Anthropic account (anthropicApi) -> x-api-key, already used by Leads Funnel
"""
import json, os, uuid, urllib.request, urllib.error
from _notify import telegram_send_node
from _deploy_guard import preflight

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"

OMS_CRED = {"id": "4pUEOr1SF2Fu4RNl", "name": "TBP OMS WMS PAT"}
ANTHROPIC_CRED = {"id": "Dlt7E39chcNqW69o", "name": "Anthropic account"}

WF_NAME = "WhatsApp Complaint Analysis + CSAT"
WEBHOOK_PATH = "trigger-wa-analysis-now"
# Stable uuid: regenerating it on every build would orphan the registered path.
WEBHOOK_UUID = "3f1c9a52-7d84-4e6b-9c11-2a6f8b0d4e77"
WF_ID = "D6B09OLv6MJCbthd"  # re-runs PUT this workflow instead of creating a duplicate

MODEL = "claude-opus-5"
WINDOW_DAYS = 7
PAGE_SIZE = 200
MAX_PAGES = 20

# Must stay identical to the Literal in oms-backend/app/beans/wms/wa_analysis.py
# and the enum in semantics.yaml. Anything else is rejected with a 422.
CATEGORIES = [
    "delivery_late", "delivery_damaged", "delivery_scheduling", "stock_availability",
    "product_quality", "pet_refused_food", "feeding_advice", "order_change",
    "subscription_management", "billing_payment", "praise", "other",
]

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "is_complaint": {"type": "boolean"},
        "csat_score": {
            "anyOf": [{"type": "integer", "enum": [1, 2, 3, 4, 5]}, {"type": "null"}],
        },
        "summary": {"type": "string"},
    },
    "required": ["category", "sentiment", "is_complaint", "csat_score", "summary"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You classify WhatsApp conversations between The Bon Pet (a Singapore fresh pet food brand) and its customers. You will be given one conversation transcript. Return one verdict.

CUSTOMER and US label who is speaking. Judge the CUSTOMER's experience only - never score us on our own messages.

category: the customer's main reason for writing, from the fixed list. Use `other` when nothing fits rather than forcing a near-match, and `praise` only when the message is thanks or compliments with no problem attached.

is_complaint: true when the customer reports something wrong - late or damaged delivery, a quality problem, a billing error, an unanswered request. A question ("when does my order arrive?") is not a complaint. Frustration about something we got wrong is.

csat_score: 1-5, inferred from how satisfied the customer sounds by the end of the conversation. 1 is angry or threatening to leave, 3 is neutral or transactional, 5 is delighted. Return null when the conversation carries no satisfaction signal either way - a one-line logistics question with no reaction is null, not 3. Never guess a middling score to avoid returning null; a wrong 3 silently drags the average.

summary: one sentence, under 25 words, describing what the customer wanted or complained about. No names, no phone numbers."""

COMPUTE_WINDOW_JS = f"""
// The manual webhook may pass ?since=&until= to re-run a specific week (backfill,
// or re-classify after a prompt change). The schedule passes nothing and gets the
// trailing window. Both paths must produce midnight-UTC bounds or the upsert key
// won't match an earlier run of the same week.
const q = ($json && $json.query) || {{}};
if (q.since && q.until) {{
  return [{{ json: {{ since: new Date(q.since).toISOString(), until: new Date(q.until).toISOString() }} }}];
}}

// Trailing {WINDOW_DAYS} days, aligned to midnight UTC so a re-run of the same week
// hits the same (phone, window_start, window_end) key and UPDATES its verdicts
// instead of writing a second row. The upsert depends on these bounds being stable.
const now = new Date();
const until = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
const since = new Date(until.getTime() - {WINDOW_DAYS} * 24 * 60 * 60 * 1000);
return [{{ json: {{ since: since.toISOString(), until: until.toISOString() }} }}];
"""

# Team/ops numbers from CLAUDE.md's broadcast list. These threads are internal
# (freezer-temperature alerts, ops chatter) and were being scored as customer
# satisfaction - Nicolas's line landed in the data as an `other` conversation.
TEAM_NUMBERS = [
    "+6581394225",   # Yash
    "+6598531677",   # Nicolas
    "+6590108515",   # Bon Pet official
    "+6587993341",   # Rachel
    "+6581114800",   # Shaun
    "+6282240119788",  # Bari
    "+6596574614",  # Chandani (CS)
    # Launch Cycle - external agency, not customers (Yash, 2026-08-09)
    "+6588146498",   # Raghav
    "+6583513308",   # Siva
]

FILTER_JS = ("const TEAM = " + json.dumps(TEAM_NUMBERS) + ";\n"
             "const MODEL = " + json.dumps(MODEL) + ";\n"
             "const SYSTEM = " + json.dumps(SYSTEM_PROMPT) + ";\n"
             "const SCHEMA = " + json.dumps(VERDICT_SCHEMA) + ";\n" + """
// Flatten the paginated pages, then keep only conversations the customer actually
// spoke in. Outbound-only threads (order confirmations nobody replied to) carry no
// signal: including them would inflate the denominator of every rate we report.
const since = $('Compute Window').first().json.since;
const until = $('Compute Window').first().json.until;

const conversations = [];
for (const item of $input.all()) {
  for (const c of (item.json.conversations || [])) conversations.push(c);
}

const out = [];
let skipped = 0, team = 0, oneSided = 0;
for (const c of conversations) {
  const msgs = c.messages || [];
  if (!msgs.some((m) => m.is_inbound)) { skipped++; continue; }

  // Internal/ops threads are not customer satisfaction.
  if (TEAM.includes(c.phone)) { team++; continue; }

  // A one-sided transcript cannot support a satisfaction judgement: without our
  // replies the model cannot tell a resolved issue from an ignored one. Every
  // conversation recorded from 2026-05 on has both sides; the older device-history
  // archive is ~99% customer-only, so this also gates out that era.
  if (!msgs.some((m) => !m.is_inbound)) { oneSided++; continue; }

  // Media rows have a null body; keep them as placeholders so "customer sent a
  // photo of the packaging" is not silently read as an empty turn.
  const transcript = msgs.map((m) => {
    const who = m.is_inbound ? 'CUSTOMER' : 'US';
    const body = m.content || (m.media_type ? `[sent ${m.media_type}]` : '[empty]');
    return `${who}: ${body}`;
  }).join('\\n');

  // Assemble the whole Anthropic request here rather than in an n8n expression.
  // A multi-line {{ }} expression in the HTTP node fails to parse ("invalid
  // syntax") and the failure surfaces at run time, not at build time.
  out.push({ json: {
    phone: c.phone,
    since, until,
    message_count: msgs.length,
    transcript,
    body: {
      model: MODEL,
      max_tokens: 2000,
      system: SYSTEM,
      output_config: { effort: 'low', format: { type: 'json_schema', schema: SCHEMA } },
      messages: [{ role: 'user', content: 'Classify this conversation:\\n\\n' + transcript }],
    },
  }});
}

console.log(`[wa-analysis] ${conversations.length} conversations, ${out.length} classifiable, ${skipped} outbound-only, ${team} team, ${oneSided} one-sided`);
return out;
""")

BUILD_ROWS_JS = f"""
// Pair each Anthropic response back with the conversation it came from. The HTTP
// node runs once per input item and preserves order, so index i of this node's
// input is index i of Filter Classifiable's output.
const sources = $('Filter Classifiable').all();
const responses = $input.all();
const CATEGORIES = {json.dumps(CATEGORIES)};

const items = [];
const rejected = [];

// The verdict fields are schema-constrained, but `summary` is free text and
// occasionally arrives with a trailing artifact - an HTML entity, a stray angle
// bracket, a fragment of a control token. Observed roughly 1 run in 6 on long
// transcripts; the classification itself was identical every time. Scrub here
// rather than downstream, because this string is shown on the dashboard and
// written to the customer's CRM record.
function cleanSummary(s) {{
  return String(s || '')
    .replace(/&[a-z]+;|&#\\d+;/gi, ' ')          // HTML entities
    .replace(/<[^>]*>?/g, ' ')                   // tags and unterminated fragments
    .replace(/[<>{{}}]/g, ' ')                   // stray brackets with no opening tag
    .replace(/[*_`~#]/g, ' ')                    // markdown artifacts (seen: a trailing '**')
    .replace(/[\\u0000-\\u001f\\u007f]/g, ' ')     // control characters
    .replace(/\\s+/g, ' ')
    .trim()
    .slice(0, 2000);                             // API max_length on the field
}}

responses.forEach((res, i) => {{
  const src = sources[i] && sources[i].json;
  if (!src) return;

  // output_config.format guarantees the first block is text containing valid JSON
  // against our schema - but a refusal or a max_tokens stop returns something else,
  // so parse defensively rather than trusting the shape.
  let verdict = null;
  try {{
    const block = (res.json.content || []).find((b) => b.type === 'text');
    if (block) verdict = JSON.parse(block.text);
  }} catch (e) {{
    rejected.push(`${{src.phone}}: unparseable (${{e.message}})`);
    return;
  }}
  if (!verdict) {{
    rejected.push(`${{src.phone}}: stop_reason=${{res.json.stop_reason}}`);
    return;
  }}
  if (!CATEGORIES.includes(verdict.category)) {{
    rejected.push(`${{src.phone}}: bad category ${{verdict.category}}`);
    return;
  }}

  items.push({{
    phone_number: src.phone,
    window_start: src.since,
    window_end: src.until,
    category: verdict.category,
    sentiment: verdict.sentiment,
    is_complaint: !!verdict.is_complaint,
    csat_score: verdict.csat_score === null ? null : Number(verdict.csat_score),
    summary: cleanSummary(verdict.summary),
    message_count: src.message_count,
    model: '{MODEL}',
    meta: {{ source: 'n8n_weekly' }},
  }});
}});

if (items.length === 0) {{
  // Nothing to POST. Fail loudly rather than sending an empty batch, which the
  // API rejects with a 422 anyway.
  throw new Error(`No verdicts survived validation from ${{responses.length}} responses: ${{rejected.join('; ')}}`);
}}

const scored = items.filter((i) => i.csat_score !== null);
const summary = {{
  classified: items.length,
  rejected: rejected.length,
  complaints: items.filter((i) => i.is_complaint).length,
  avg_csat: scored.length ? (scored.reduce((a, i) => a + i.csat_score, 0) / scored.length).toFixed(2) : null,
  csat_sample: scored.length,
  rejected_detail: rejected.slice(0, 5),
}};

// The API caps a batch at 500; a week is ~45, so one request is always enough.
return [{{ json: {{ items: items.slice(0, 500), summary }} }}];
"""

ALERT_JS = """
const s = $('Build Rows').first().json.summary;
const stored = $input.first().json;
const top = {};
for (const i of $('Build Rows').first().json.items) {
  if (i.is_complaint) top[i.category] = (top[i.category] || 0) + 1;
}
const ranked = Object.entries(top).sort((a, b) => b[1] - a[1]).slice(0, 3)
  .map(([k, v]) => `${k}: ${v}`).join(', ') || 'none';

const lines = [
  `*WhatsApp analysis - week to ${$('Compute Window').first().json.until.slice(0, 10)}*`,
  `${s.classified} conversations classified (${s.complaints} complaints)`,
  `Derived CSAT ${s.avg_csat === null ? 'n/a' : s.avg_csat}/5 from ${s.csat_sample} scored`,
  `Top pain points: ${ranked}`,
  `Stored: ${stored.inserted} new, ${stored.updated} updated`,
];
if (s.rejected > 0) lines.push(`WARNING ${s.rejected} rejected: ${s.rejected_detail.join('; ')}`);

return [{ json: { message: lines.join('\\n') } }];
"""


def node(name, typ, pos, params, **extra):
    n = {"parameters": params, "id": str(uuid.uuid4()), "name": name,
         "type": typ, "typeVersion": extra.pop("tv", 1), "position": pos}
    n.update(extra)
    return n


def code(name, pos, js):
    return node(name, "n8n-nodes-base.code", pos, {"jsCode": js.strip()}, tv=2)


schedule = node("Weekly Monday 9am", "n8n-nodes-base.scheduleTrigger", [-200, 300], {
    "rule": {"interval": [{"field": "cronExpression", "expression": "0 1 * * 1"}]},
}, tv=1.2)

# Manual trigger for test runs and backfills. The n8n public API cannot execute a
# workflow, so a webhook is the only programmatic way in. webhookId must be set
# explicitly on the node: workflows created via the API without it activate fine
# but the path 404s forever ("not registered").
manual = node("Manual Trigger", "n8n-nodes-base.webhook", [-200, 460], {
    "path": WEBHOOK_PATH,
    "responseMode": "onReceived",
    "options": {},
}, tv=2, webhookId=WEBHOOK_UUID)

window = code("Compute Window", [20, 300], COMPUTE_WINDOW_JS)

fetch = node("Fetch Conversations", "n8n-nodes-base.httpRequest", [240, 300], {
    "url": "https://api.thebonpet.com/whatsapp/admin/conversations",
    "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth",
    "sendQuery": True,
    "queryParameters": {"parameters": [
        {"name": "since", "value": "={{ $json.since }}"},
        {"name": "until", "value": "={{ $json.until }}"},
        {"name": "limit", "value": str(PAGE_SIZE)},
        {"name": "offset", "value": "0"},
    ]},
    "options": {
        "response": {"response": {"neverError": False}},
        "pagination": {"pagination": {
            "paginationMode": "updateAParameterInEachRequest",
            "parameters": {"parameters": [
                {"type": "qs", "name": "offset", "value": f"={{{{ $pageCount * {PAGE_SIZE} }}}}"},
            ]},
            "paginationCompleteWhen": "other",
            "completeExpression": f"={{{{ $response.body.conversations.length < {PAGE_SIZE} }}}}",
            "limitPagesFetched": True,
            "maxRequestsPerPage": MAX_PAGES,
        }},
    },
}, tv=4.2, credentials={"httpHeaderAuth": OMS_CRED})

filter_node = code("Filter Classifiable", [460, 300], FILTER_JS)

classify = node("Classify", "n8n-nodes-base.httpRequest", [680, 300], {
    "method": "POST",
    "url": "https://api.anthropic.com/v1/messages",
    "authentication": "predefinedCredentialType",
    "nodeCredentialType": "anthropicApi",
    "sendHeaders": True,
    "headerParameters": {"parameters": [
        {"name": "anthropic-version", "value": "2023-06-01"},
    ]},
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify($json.body) }}",
    "options": {"batching": {"batch": {"batchSize": 4, "batchInterval": 1000}}},
}, tv=4.2, credentials={"anthropicApi": ANTHROPIC_CRED})

build_rows = code("Build Rows", [900, 300], BUILD_ROWS_JS)

store = node("Store Analysis", "n8n-nodes-base.httpRequest", [1120, 300], {
    "method": "POST",
    "url": "https://api.thebonpet.com/wms/wa-analysis",
    "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth",
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ items: $json.items }) }}",
    "options": {},
}, tv=4.2, credentials={"httpHeaderAuth": OMS_CRED})

alert = code("Build Alert", [1340, 300], ALERT_JS)
telegram = telegram_send_node("Notify Team", [1560, 300])

nodes = [schedule, manual, window, fetch, filter_node, classify, build_rows, store, alert, telegram]

connections = {}
chain = [window, fetch, filter_node, classify, build_rows, store, alert, telegram]
for a, b in zip(chain, chain[1:]):
    connections[a["name"]] = {"main": [[{"node": b["name"], "type": "main", "index": 0}]]}
for trigger in (schedule, manual):
    connections[trigger["name"]] = {"main": [[{"node": window["name"], "type": "main", "index": 0}]]}

payload = {
    "name": WF_NAME,
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore"},
}


def http(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body else None,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


if __name__ == "__main__":
    if WF_ID:
        preflight(WF_ID, payload)
        status, body = http("PUT", f"/workflows/{WF_ID}", payload)
        print(f"PUT -> HTTP {status}")
    else:
        status, body = http("POST", "/workflows", payload)
        print(f"POST -> HTTP {status}")
    print(body[:400])
    if status < 300:
        wid = json.loads(body).get("id")
        print(f"\nWorkflow: https://n8n.thebonpet.com/workflow/{wid}")
        print("Created INACTIVE by design - activate in the UI after a manual test run.")

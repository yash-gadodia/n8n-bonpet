#!/usr/bin/env python3
"""Google Review Auto-Reply — creates the workflow via n8n public API.

⚠️ CREATION SNAPSHOT ONLY (2026-09-06). The live workflow (PoSCFuP6yOu6FJVE) has
drifted: a Build Claude Request code node feeds the HTTP node (predefined-cred
auth failed with an inline jsonBody), the webhook node carries a webhookId
(required for the clean /webhook/<path> URL), DRY_RUN is false, and the
Anthropic credential is 7XGUErOlmGwrZXdW "Anthropic account (Sep 2026)".
Live JSON is source of truth — patch over the API, never re-run this.

Fetches all GBP reviews (paginated), picks unanswered ones (GBP itself is the
dedupe state: a review with reviewReply is done), drafts a custom reply with
Claude, posts it via the GBP v4 reply endpoint, and summarises to Team Bon Pet
Telegram. DRY_RUN starts True: drafts go to Telegram only, nothing posted
publicly until the constant is flipped.
"""
import json, os, sys, urllib.request

N8N = "https://n8n.thebonpet.com/api/v1"
KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()

ACCOUNT = "114536525592782210040"
LOCATION = "13860156224086962631"
GBP_CRED = {"id": "t6B7SBg1PDBOyjUd", "name": "Google Business Profile account"}
ANTHROPIC_CRED = {"id": "Dlt7E39chcNqW69o", "name": "Anthropic account"}
TG_TOKEN = "8784180714:AAHmsdP0ztB2iJjQPGqJZovfvDFiaTkABTs"
TG_CHAT = "-1002184573790"

REVIEWS_URL = f"https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}/reviews"

SYSTEM_PROMPT = """You write public replies to Google reviews for The Bon Pet, a small Singapore fresh pet food brand (sous vide cooked, human-grade meals for cats and dogs, founded by Nic and Yash).

Rules:
- Warm, casual, sincere. Sound like a small local team, not a corporation and not an AI.
- 1 to 3 short sentences, under 60 words.
- Start by addressing the reviewer by first name (e.g. "Hi Sarah,"). If the name looks like a handle or initials, just start with "Hi,".
- If the review has text, reference something specific they said. If it is star-only, keep it to a short genuine thank-you.
- Pets are "furkids" where natural. Do not force brand words in.
- At most one emoji, only if it genuinely adds warmth. Zero is fine.
- NEVER use em dashes or en dashes. Use commas or split the sentence.
- No promo codes, no discount offers, no refund promises, no health claims.
- For 3 stars and below: apologise sincerely without excuses, thank them for the honesty, and invite them to email hello@thebonpet.com so we can make it right.
- Vary sentence structure; do not open with "Thank you so much" boilerplate.
- If the review is not in English, reply in the review's language.
- Output ONLY the reply text, no quotes around it."""

def n(name, ntype, pos, params, cred=None, tv=None):
    node = {"name": name, "type": ntype, "typeVersion": tv or 1, "position": pos, "parameters": params}
    if cred:
        node["credentials"] = cred
    return node

nodes = [
    n("Hourly", "n8n-nodes-base.scheduleTrigger", [-460, 300],
      {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, tv=1.2),
    n("Manual Trigger (Webhook)", "n8n-nodes-base.webhook", [-460, 480],
      {"httpMethod": "POST", "path": "review-autoreply-manual-8c1f5e2b", "options": {}}, tv=2),
    n("Fetch Reviews", "n8n-nodes-base.httpRequest", [-220, 380],
      {"method": "GET", "url": REVIEWS_URL,
       "authentication": "predefinedCredentialType",
       "nodeCredentialType": "googleBusinessProfileOAuth2Api",
       "sendQuery": True,
       "queryParameters": {"parameters": [{"name": "pageSize", "value": "50"}]},
       "options": {"pagination": {"pagination": {
           "paginationMode": "updateAParameterInEachRequest",
           "parameters": {"parameters": [{"type": "qs", "name": "pageToken", "value": "={{ $response.body.nextPageToken }}"}]},
           "paginationCompleteWhen": "other",
           "completeExpression": "={{ !$response.body.nextPageToken }}",
           "limitPagesFetched": True,
           "maxRequests": 12}}}},
      cred={"googleBusinessProfileOAuth2Api": GBP_CRED}, tv=4.2),
    n("Select Unanswered", "n8n-nodes-base.code", [20, 380],
      {"jsCode": r"""
const DRY_RUN = true;          // flip to false to post replies publicly
const DRIP_LIMIT = 4;          // replies per hourly run (~96/day)
const STARS = { ONE: 1, TWO: 2, THREE: 3, FOUR: 4, FIVE: 5 };

const reviews = [];
for (const page of $input.all()) {
  for (const r of (page.json.reviews || [])) reviews.push(r);
}
const unanswered = reviews.filter(r => !r.reviewReply);
// newest first: recent reviews matter most (and GBP surfaces them on top)
unanswered.sort((a, b) => new Date(b.createTime) - new Date(a.createTime));

return unanswered.slice(0, DRIP_LIMIT).map(r => ({ json: {
  review_id: r.reviewId,
  review_name: r.name,
  reviewer: (r.reviewer && r.reviewer.displayName) || '',
  rating: STARS[r.starRating] || 0,
  comment: r.comment || '',
  create_time: r.createTime,
  dry_run: DRY_RUN,
  total_unanswered: unanswered.length,
}}));
"""}, tv=2),
    n("Draft Reply (Claude)", "n8n-nodes-base.httpRequest", [260, 380],
      {"method": "POST", "url": "https://api.anthropic.com/v1/messages",
       "authentication": "predefinedCredentialType",
       "nodeCredentialType": "anthropicApi",
       "sendHeaders": True,
       "headerParameters": {"parameters": [{"name": "anthropic-version", "value": "2023-06-01"}]},
       "sendBody": True, "specifyBody": "json", "contentType": "json",
       "jsonBody": "={{ JSON.stringify({ model: 'claude-sonnet-5', max_tokens: 300, system: $('Select Unanswered').first().json.system_prompt || " + json.dumps(SYSTEM_PROMPT) + ", messages: [{ role: 'user', content: 'Reviewer: ' + $json.reviewer + '\\nRating: ' + $json.rating + ' stars\\nReview text: ' + ($json.comment || '(no text, star rating only)') }] }) }}",
       "options": {}},
      cred={"anthropicApi": ANTHROPIC_CRED}, tv=4.2),
    n("Prepare Reply", "n8n-nodes-base.code", [500, 380],
      {"mode": "runOnceForEachItem", "jsCode": r"""
const src = $('Draft Reply (Claude)').item.json;
let reply = ((src.content && src.content[0] && src.content[0].text) || '').trim();
reply = reply.replace(/[—–]/g, ',').replace(/^["']|["']$/g, '').replace(/\s+\n/g, '\n').trim();
const meta = $('Select Unanswered').item.json;
return { json: { ...meta, reply } };
"""}, tv=2),
    n("Is Live?", "n8n-nodes-base.if", [740, 380],
      {"conditions": {"options": {"caseSensitive": True, "typeValidation": "strict", "version": 2},
        "conditions": [{"leftValue": "={{ $json.dry_run }}", "rightValue": "", "operator": {"type": "boolean", "operation": "false", "singleValue": True}}],
        "combinator": "and"}}, tv=2.2),
    n("Post Reply", "n8n-nodes-base.httpRequest", [980, 280],
      {"method": "PUT",
       "url": f"=https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}/reviews/{{{{ $json.review_id }}}}/reply",
       "authentication": "predefinedCredentialType",
       "nodeCredentialType": "googleBusinessProfileOAuth2Api",
       "sendBody": True, "specifyBody": "json", "contentType": "json",
       "jsonBody": "={{ JSON.stringify({ comment: $json.reply }) }}",
       "options": {}},
      cred={"googleBusinessProfileOAuth2Api": GBP_CRED}, tv=4.2),
    n("Format TG Summary", "n8n-nodes-base.code", [1220, 380],
      {"jsCode": r"""
const items = $input.all().map(it => it.json);
if (!items.length) return [];
const dry = items[0].dry_run;
const head = dry
  ? 'Google Review Auto-Reply, DRY RUN (nothing posted). Drafts:'
  : 'Google Review Auto-Reply posted ' + items.length + ' repl' + (items.length === 1 ? 'y' : 'ies') + ':';
const lines = items.map(r =>
  '\n\n' + '⭐'.repeat(r.rating) + ' ' + (r.reviewer || 'Anonymous') +
  (r.comment ? '\n"' + r.comment.slice(0, 150) + (r.comment.length > 150 ? '...' : '') + '"' : ' (star only)') +
  '\n↪ ' + r.reply);
const tail = '\n\nUnanswered remaining: ' + (items[0].total_unanswered - (dry ? 0 : items.length));
return [{ json: { text: (head + lines.join('') + tail).slice(0, 4000) } }];
"""}, tv=2),
    n("Telegram Team", "n8n-nodes-base.httpRequest", [1460, 380],
      {"method": "POST", "url": f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
       "sendBody": True, "specifyBody": "json", "contentType": "json",
       "jsonBody": "={{ JSON.stringify({ chat_id: '" + TG_CHAT + "', text: $json.text, disable_web_page_preview: true }) }}",
       "options": {}}, tv=4.2),
]

connections = {
    "Hourly": {"main": [[{"node": "Fetch Reviews", "type": "main", "index": 0}]]},
    "Manual Trigger (Webhook)": {"main": [[{"node": "Fetch Reviews", "type": "main", "index": 0}]]},
    "Fetch Reviews": {"main": [[{"node": "Select Unanswered", "type": "main", "index": 0}]]},
    "Select Unanswered": {"main": [[{"node": "Draft Reply (Claude)", "type": "main", "index": 0}]]},
    "Draft Reply (Claude)": {"main": [[{"node": "Prepare Reply", "type": "main", "index": 0}]]},
    "Prepare Reply": {"main": [[{"node": "Is Live?", "type": "main", "index": 0}]]},
    "Is Live?": {"main": [
        [{"node": "Post Reply", "type": "main", "index": 0}],
        [{"node": "Format TG Summary", "type": "main", "index": 0}],
    ]},
    "Post Reply": {"main": [[{"node": "Format TG Summary", "type": "main", "index": 0}]]},
    "Format TG Summary": {"main": [[{"node": "Telegram Team", "type": "main", "index": 0}]]},
}

wf = {"name": "Google Review Auto-Reply", "nodes": nodes, "connections": connections,
      "settings": {"executionOrder": "v1", "timezone": "Asia/Singapore"}}

req = urllib.request.Request(f"{N8N}/workflows", data=json.dumps(wf).encode(),
                             headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json",
                                      "User-Agent": "Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req) as r:
    out = json.load(r)
print("created workflow id:", out.get("id"))

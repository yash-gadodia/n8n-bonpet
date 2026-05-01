# Bon Pet Shopify Automations — Design

**Date:** 2026-04-19
**Author:** Yash (via Claude)
**Status:** Phase 1a (Daily Pulse) approved for implementation

## Goal

Build 15 automation workflows on n8n Cloud (`thebonpet.app.n8n.cloud`) that combine Shopify data with WhatsApp broadcasts to the 5-person Bon Pet team. Ship in phases so each workflow can be tested in isolation before the next.

## Phased rollout

| Phase | Tier | Workflows | Trigger shape |
|---|---|---|---|
| **1** | Scheduled digests | daily pulse, top seller leaderboard, customer metrics, goal tracking | cron + Shopify REST |
| **2** | Transactional alerts | big order alert, VIP thank you, refund/cancel alert, abandoned cart, negative review watcher | Shopify webhooks (real-time) |
| **3** | Customer lifecycle | birthday flow, win-back, product launcher | cron + Shopify customer queries |
| **4** | Data/AI heavy | competitor price, demand forecast, AI insights | varies; needs extra design |

Each phase ships one workflow at a time. User tests each in production before the next is built.

Within Phase 1, build order: **daily pulse → top seller leaderboard → customer metrics → goal tracking**. Daily pulse is the anchor morning message and informs the other three.

## Shared infrastructure (already wired)

All Phase 1 workflows reuse existing primitives — no new credentials, no new endpoints.

### WhatsApp broadcast
- Endpoint: `POST https://api.thebonpet.com/whatsapp/send`
- Auth: header `X-API-Key: a3f9c1e6…d6e`
- Body: `{ phone_number, message }`
- 5 parallel HTTP nodes, one per recipient from `~/n8n-bonpet/` `RECIPIENTS` list:
  - `+6581394225` (Yash), `+6598531677` (Nicolas), `+6590108515` (Bon Pet official), `+6587993341` (Rachel), `+6581114800` (Shaun)

### Shopify fetch
- Store: `d2ac44-d5.myshopify.com`, API version `2024-10`
- n8n credential type `shopifyAccessTokenApi` (already configured — reuse by ID)
- HTTP Request node with `authentication: predefinedCredentialType`

### Scheduling
- Morning broadcasts standardized at **9 AM SGT** (matches existing `Low Stock Watcher`, `Weekly & Monthly Sales Report`, `Picklist Not Sent`).
- n8n `scheduleTrigger` with `cronExpression: 0 9 * * *` and explicit timezone `Asia/Singapore`.

### Build tooling
- Use `~/n8n-bonpet/build_low_stock.py` as the generator template: Python script emits workflow JSON and POSTs to the n8n public API.
- For each new workflow, produce a `build_<name>.py` helper committed to `~/n8n-bonpet/`.
- Public API gotcha: `settings` must be stripped to `{ executionOrder: "v1" }` only — reject `binaryMode` / `timezone` fields. Timezone goes on the `scheduleTrigger` node instead.

## Workflow 1 — Daily Pulse (Phase 1a, ready to build)

### Trigger
Daily at 09:00 `Asia/Singapore` via `scheduleTrigger`.

### Data flow
```
scheduleTrigger (9 AM SGT)
    ↓
Set date ranges (SGT)
    - yesterday_start / yesterday_end (prior SGT day)
    - prev_day_start / prev_day_end     (two days ago, for D-over-D %)
    - prev_week_start / prev_week_end   (same weekday last week, for W-over-W %)
    - open_order_cutoff                 (now − 24h, ISO)
    ↓
3 parallel Shopify HTTP fetches:
    (a) Orders — yesterday, prev day, prev week (single window covering all 3)
         orders.json?status=any&created_at_min=<prev_week_start>&created_at_max=<yesterday_end>
         &fields=id,total_price,created_at,customer,line_items,financial_status,fulfillment_status
         &limit=250 (paginate via `page_info` if needed)
    (b) Open orders — unfulfilled, created >24h ago
         orders.json?status=open&fulfillment_status=unfulfilled&created_at_max=<open_order_cutoff>
         &fields=id,created_at,name&limit=250
    (c) Refunds — orders updated yesterday with refund status
         orders.json?financial_status=refunded,partially_refunded
         &updated_at_min=<yesterday_start>&updated_at_max=<yesterday_end>
         &fields=id,total_price,refunds,updated_at&limit=250
    ↓
Code: Aggregate Metrics
    - Bucket orders by date range → revenue + count for yesterday/prev-day/prev-week
    - Count new customers: orders where customer.orders_count == 1, date == yesterday
    - Open order count + age of oldest
    - Refund count + total refund value (sum of refunds[].transactions where kind == 'refund')
    - Compute D-over-D % and W-over-W % (handle zero-division → show "—")
    ↓
Set: Format Message (WhatsApp template, below)
    ↓
5 parallel HTTP POSTs → api.thebonpet.com/whatsapp/send (one per recipient)
```

### Message template

```
🐾 *Bon Pet Daily Pulse*
_{{ formattedDate }}_

💰 *Yesterday*
Revenue: S${{ revenue }} ({{ orderCount }} orders)
vs {{ prevDayLabel }}:      {{ dodSign }}{{ dodPct }}% {{ dodEmoji }}
vs last {{ prevWeekLabel }}: {{ wowSign }}{{ wowPct }}% {{ wowEmoji }}

👥 *New customers*
{{ newCustomerCount }} first-time buyers

📦 *Open orders >24h*
{{ openOrderCount }} unfulfilled (oldest: {{ oldestAge }})

↩️ *Refunds / cancels*
{{ refundCount }} refunds (-S${{ refundTotal }})
```

Emoji rule: `+x% 📈`, `0% ➡️`, `-x% 📉`, `—` when prior-period value is 0.

Formatting rules:
- `oldestAge` — `Nd` if open order age ≥ 24h rounded down (always true given threshold), e.g. `3d`, `1d`.
- `revenue` / `refundTotal` — `1,234.56` (comma thousands, 2 decimals, no currency symbol — `S$` is in the template).
- `dodPct` / `wowPct` — integer rounded, e.g. `12`, `4`. Zero = `0% ➡️`.
- `prevDayLabel` / `prevWeekLabel` — 3-letter weekday, e.g. `Thu`, `Fri`.
- `formattedDate` — `Fri 18 Apr 2026`.

### Definitions (locked)
- **Yesterday** = SGT 00:00–24:00 of prior day.
- **New customer** = order.customer.orders_count == 1 (first-ever order).
- **Open order >24h** = `fulfillment_status != "fulfilled"` AND `created_at < now − 24h`.
- **% comparisons** = D-over-D (vs prior SGT day) AND W-over-W (vs same weekday last week).
- **Currency** = SGD, formatted with thousand-separators and 2 decimals.

### Edge cases handled
- Zero orders yesterday → message still sends, shows `S$0.00 (0 orders)` and `—` for % changes.
- Shopify fetch failure → n8n node error; workflow surfaces in n8n execution log. No retry on first pass (follow-up: add retry + dead-letter alert to Yash's number only).
- More than 250 orders/day → paginate via `Link` header (`page_info` cursor). If total orders exceed ~2500/day this will need batching redesign.
- Customer field missing on order (guest checkout) → skip from new-customer count.

### Success criteria
- Message delivered to all 5 recipients by 09:00:30 SGT.
- Numbers reconcile with Shopify admin dashboard for the same day (±0).
- Running the workflow on a test day produces identical output on a second run.

### Test plan
1. Build workflow via `build_daily_pulse.py`. Do NOT activate yet.
2. In n8n UI: open workflow, click **Execute Workflow** manually. Inspect each node's output.
3. Compare aggregated numbers against Shopify admin `/admin/orders?created_at=<yesterday>` filter.
4. Spot-check message formatting on Yash's number only: temporarily disable `Send #2`–`#5`, run manually, verify WhatsApp receipt.
5. Re-enable all 5 sends. Activate workflow. Confirm next-day 09:00 SGT delivery.
6. Monitor for 3 consecutive days before moving to Workflow 2.

## Workflows 2–4 (Phase 1, to be designed after Daily Pulse is live)

Deferred brainstorming — will re-enter this skill when Daily Pulse is validated. Rough shape captured here to avoid scope drift:

- **Top Seller Leaderboard** — weekly, Monday 9 AM SGT. Top 10 products by revenue over the prior 7 days. Also show W-over-W movement (↑5, ↓2, —).
- **Customer Metrics** — weekly, Monday 9:15 AM SGT. New vs returning customers, repeat purchase rate, avg order value, top customer by spend.
- **Goal Tracking** — daily 9:05 AM SGT. Progress bar toward monthly revenue target. Requires a `targets` config (Google Sheet or constants in build script — decide during that workflow's brainstorm).

## Out of scope (explicit)

- Phase 2+ (transactional, lifecycle, AI) — separate specs later.
- Consolidating the 4 Phase-1 digests into one mega-message. Separate messages let users mute by workflow if needed.
- Internationalization / multi-currency. Store is SGD-only.
- Historical backfill. Workflows only produce data going forward from activation.
- Retry/dead-letter infrastructure. Add only if Phase 1 surfaces reliability issues.

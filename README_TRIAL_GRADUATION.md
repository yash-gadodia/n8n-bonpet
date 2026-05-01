# Trial Graduation → Subscription Workflow

**Status:** Draft (not yet deployed)  
**Expected impact:** +$3-4k/mo ARR (est. 50 trial redemptions/mo × 10% → 15% attach rate)

---

## What It Does

Sends a warm, WhatsApp nudge to trial customers exactly 7 days after their trial delivery, offering 20% off their first subscription order. Designed to convert the 90% of trial takers who don't immediately subscribe.

**Audience:** Customers whose MOST RECENT order was a trial (FREETRIAL, CATTRIAL, or DOGTRIAL discount code), delivered 7 days ago.

**Exclusions:**
- Active subscribers (anyone with ANY order using a "Subscription*" discount code)
- Already-messaged customers (tracked in "Trial Graduation Log" sheet tab)
- Orders without phone number or delivery date

**Message tone:** Warm, emoji-heavy Singaporean (furkids, 🐾), no em-dashes. Invitation to resubscribe, not obligation.

---

## How to Deploy

### 1. Create Shopify Discount Code

Go to **Admin > Discounts > Create discount** and set up:

```
Name:                 TRIALGRAD<3THEBONPET
Type:                 Percentage discount
Value:                20%
Applies to:           Specific product variants (filter to subscription SKUs only)
                      OR leave blank and add code restrictions below
Usage limit:          1 per customer (critical — prevents abuse)
Active:               Yes
Minimum purchase:     None
Specific collections: None (let customers choose cats or dogs)
```

**Important:** This code must apply ONLY to first subscription order (verify in Shopify UI).

---

### 2. Prepare Google Sheet Tabs

The workflow reads from **"Customer Orders DB"** (ID: `1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI`).

#### Tab 1: "Orders"
Must have columns (case-insensitive):
- `email` or `email_address`
- `first_name` or `first`
- `phone` or `phone_number`
- `discount_code` or `discount_codes`
- `order_date` or `created_at`
- `delivery_date` or `delivered_at`
- `pet_name` (optional, used in greeting if present)

#### Tab 2: "Trial Graduation Log" (create new)
Create a new tab with GID `1828374650` and columns:
- Column A: `phone` (phone number who received message)
- Column B: `first_name` (customer name for audit)
- Column C: `trial_type` (FREETRIAL, CATTRIAL, or DOGTRIAL)
- Column D: `sent_at` (ISO timestamp)

This log prevents re-sending the same customer twice.

---

### 3. Review & Activate Workflow

#### Review the message
Edit `build_trial_graduation.py`, search for `const msg = ` in `CODE_JS`. Message currently reads:

```
Hey {first_name} 🐾 did {pet_name or "your furkid"} enjoy the Bon Pet trial?

If they were a fan, we'd love to keep them eating fresh. Here's 20% off your first subscription code *TRIALGRAD<3THEBONPET*

Also free delivery over $60 cat / $100 dog + 10% off ongoing. Cancel anytime, no lock-ins.

→ https://thebonpet.com/collections/cats (or /dogs)

❤️ The Bon Pet team
```

**Verify:** Warm tone, no em-dashes, emoji-friendly, matches brand voice in `/Users/yash/Documents/TheBonPet/CLAUDE.md`.

#### Test with dry-run
1. In `build_trial_graduation.py`, line ~19: `DRY_RUN = True`
2. Run: `python3 build_trial_graduation.py`
3. This generates `trial_graduation_payload.json` with DRY_RUN enabled
4. Deploy to n8n (see step 4)
5. Trigger workflow manually in n8n UI
6. All messages go to Yash (+6581394225) with `[DRY RUN]` prefix for review
7. Check diagnostics: how many candidates found? Why were others skipped?

#### Flip to live
1. Once dry-run output looks right, edit `build_trial_graduation.py`
2. Line ~19: `DRY_RUN = False`
3. Run: `python3 build_trial_graduation.py` again
4. Deploy updated payload to n8n (step 4 below)
5. Workflow now sends to actual customers at 10am SGT daily

---

### 4. PUT Workflow to n8n Cloud API

Once payload is reviewed and DRY_RUN is set correctly:

```bash
cd /Users/yash/n8n-bonpet

# Option A: Create new workflow
# Uncomment the deployment code at bottom of build_trial_graduation.py
python3 <<'EOF'
import json, urllib.request, os
api_key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
with open('trial_graduation_payload.json') as f:
    payload = json.load(f)
url = "https://thebonpet.app.n8n.cloud/api/v1/workflows"
req = urllib.request.Request(url, json.dumps(payload).encode(), method='POST',
    headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    resp = json.loads(r.read())
    wf_id = resp['data']['id']
    print(f"Workflow created: {wf_id}")
    print(f"Link: https://thebonpet.app.n8n.cloud/workflows/{wf_id}")
    # Save WF_ID to script for future updates
EOF

# Option B: Update existing workflow (if already deployed)
# Edit build_trial_graduation.py line 27: WF_ID = "abc123..." (from n8n UI)
# Then: python3 build_trial_graduation.py && python3 -c "..."
```

After creation, n8n will assign a real workflow ID. Update `WF_ID` in `build_trial_graduation.py` for future edits.

---

## Message Walkthrough

**Day 1 → Trial delivery**
Customer receives trial pack (e.g., CATTRIAL code used at checkout).

**Day 7 at 10am SGT → Workflow triggers**
1. Workflow reads all orders from Google Sheet
2. Filters for customers whose most recent order is a trial (CATTRIAL/DOGTRIAL/FREETRIAL)
3. Calculates days since delivery (should be exactly 7)
4. Checks they haven't already been messaged (Trial Graduation Log)
5. Checks they're not a subscriber (no "Subscription*" discount codes anywhere in history)
6. Sends WA nudge with TRIALGRAD<3THEBONPET code (20% off first sub)

**Customer action**
Clicks link → https://thebonpet.com/collections/cats or /dogs → enters code TRIALGRAD<3THEBONPET → first subscription order gets 20% off → automatically enrolled in 10% ongoing + free delivery over threshold.

---

## Test Plan

### Dry-run test (before going live)
1. Create a test customer in Google Sheet with:
   - email: `test-trial-7d@example.com`
   - first_name: `TestCat`
   - phone: `+6581394225` (Yash)
   - discount_code: `CATTRIAL<3THEBONPET`
   - delivery_date: 7 days ago
   - pet_name: `Mittens`

2. Set `DRY_RUN = True` in script, deploy
3. Trigger workflow manually in n8n UI
4. Should receive message from Yash's phone with `[DRY RUN]` prefix
5. Verify message tone, collection URL is correct (`/collections/cats` for CATTRIAL)

### Live test (first week of deployment)
- Monitor n8n execution logs: 0 HTTP 400s or 500s?
- Check Trial Graduation Log: entries being appended?
- Did any customer complain about duplicate messages? (indicates dedup logic failed)

### Metrics to track
- **Candidates per day:** Should stabilize around 50/30 = ~2 candidates/day (50 trials/mo ÷ 30 days)
- **Conversion rate:** Check Shopify for TRIALGRAD<3THEBONPET usage → estimate attach rate
- **WA send success rate:** HTTP 2xx in n8n logs

---

## Blockers & Caveats

1. **Phone number source:** Workflow relies on phone being in Google Sheet. If Shopify Flow → Sheet sync is missing phone numbers, workflow finds 0 candidates. Verify "Orders" tab has phone for recent trial orders.

2. **Delivery date:** Must have `delivery_date` or `delivered_at` column. If missing, all orders skip. Cross-check with Ninja Van tracking if needed.

3. **Discount code format:** Code must exactly match `FREETRIAL<3THEBONPET`, `CATTRIAL<3THEBONPET`, or `DOGTRIAL<3THEBONPET` (case-insensitive, but these are the three variants in use).

4. **Subscriber exclusion:** Logic checks if ANY order has discount code starting with "Subscription". If subscriber table structure changes, update the check (line ~76 in CODE_JS).

5. **7-day window:** Currently exact (±0 days). If you want ±1 day window to catch delivery delays, change `TOLERANCE_DAYS = 0` to `TOLERANCE_DAYS = 1` on line ~25 of CODE_JS.

---

## Related Automations

- **Reorder Reminder** (`build_reorder_reminder.py`): 14/30-day cadence-based nudges for repeat customers
- **Win-back** (`build_winback.py`): 60-day dormancy campaign
- **Customer PII:** Always read from Google Sheet, NOT Shopify API (Shopify Basic blocks Protected Customer Data)

---

## References

- **Bon Pet CLAUDE.md:** `/Users/yash/Documents/TheBonPet/CLAUDE.md` (brand voice, promo codes, constraints)
- **Google Sheet:** https://docs.google.com/spreadsheets/d/1GP0RBDnvl-tHBDRv6DRdrungM2BXM5Z-LnQxmzEeuXI
- **n8n Cloud:** https://thebonpet.app.n8n.cloud
- **WA API endpoint:** `https://api.thebonpet.com/whatsapp/send`

---

## Rollback

If campaign underperforms or causes complaints:
1. Set workflow to **inactive** in n8n UI
2. Optionally delete from n8n (irreversible)
3. Pause flow in Shopify (if connected to Flowvio trigger)
4. No customer data is deleted — Trial Graduation Log remains as audit trail

---

**Built:** 2026-04-19  
**Script location:** `/Users/yash/n8n-bonpet/build_trial_graduation.py`  
**Payload:** `/Users/yash/n8n-bonpet/trial_graduation_payload.json`

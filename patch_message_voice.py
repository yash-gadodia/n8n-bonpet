#!/usr/bin/env python3
"""Patch live n8n customer copy to the Aug 2026 voice (automation/message-voice-revamp-2026-08.md).

Patches LIVE workflow JSON in place. Never re-run a build_*.py to do this: all seven
marketing builders were audited 2026-08-07 and every one had drifted, so a rebuild
silently reverts live behaviour.

Usage:
    python3 patch_message_voice.py                  # --check: verify anchors, no writes
    python3 patch_message_voice.py --apply winback  # patch one workflow
    python3 patch_message_voice.py --apply all
    RESTORE=snapshots/<file>.json python3 patch_message_voice.py --restore

Every --apply writes a full pre-change snapshot to snapshots/ first, then re-GETs and
verifies the new copy is live and the old copy is gone. A 200 is not proof.
"""
import json, os, re, sys, subprocess, urllib.request, urllib.error
from datetime import datetime

KEY = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
API = "https://n8n.thebonpet.com/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
SNAPDIR = os.path.join(HERE, "snapshots")
# Cloudflare fronts n8n and rejects default client user-agents with 403 code 1010.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def http(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_wf(wid):
    st, body = http("GET", f"/workflows/{wid}")
    assert st == 200, f"GET {wid} -> {st}: {body[:300]}"
    return json.loads(body)


def put_wf(wid, wf):
    payload = {"name": wf["name"], "nodes": wf["nodes"],
               "connections": wf["connections"],
               "settings": wf.get("settings") or {"executionOrder": "v1"}}
    st, body = http("PUT", f"/workflows/{wid}", payload)
    assert st == 200, f"PUT {wid} -> {st}: {body[:300]}"


def jstl(s):
    """Render a Python string as a JS backtick template literal body.

    Backslashes and backticks are escaped; real newlines become \\n so the emitted
    JS stays on one line. ${...} interpolations pass through untouched, which is the
    whole reason these stay template literals rather than becoming json.dumps output.
    """
    return (s.replace("\\", "\\\\").replace("`", "\\`").replace("\n", "\\n"))


def set_const(js, name, value, quote):
    """Replace the value of `const NAME = <string literal>;` regardless of its length."""
    if quote == '"':
        lit = json.dumps(value)
        pat = re.compile(r'(const\s+' + re.escape(name) + r'\s*=\s*)"(?:[^"\\]|\\.)*"\s*;')
    else:
        lit = "`" + jstl(value) + "`"
        pat = re.compile(r'(const\s+' + re.escape(name) + r'\s*=\s*)`(?:[^`\\]|\\.)*`\s*;', re.S)
    new, n = pat.subn(lambda m: m.group(1) + lit + ";", js)
    assert n == 1, f"const {name}: expected 1 match, got {n}"
    return new


def replace_once(js, old, new):
    n = js.count(old)
    assert n == 1, f"anchor matched {n} times (need 1): {old[:90]!r}"
    return js.replace(old, new)


# --------------------------------------------------------------------------------
# New copy. Mirrors automation/message-voice-revamp-2026-08.md exactly.
# --------------------------------------------------------------------------------

WINBACK_MSG = (
    "Hi ${firstName}, Yash and Nic here from The Bon Pet. It's been a while.\n\n"
    "How's your furkid doing? And if something about us didn't work for you, "
    "we'd rather hear it than not."
)

REACT_PAUSED = (
    "Hi {first_name}, Yash here from The Bon Pet. Your subscription's been paused a "
    "while and I wanted to check in rather than leave it sitting there.\n\n"
    "If it was freezer space, portions, or the delivery timing, any of those we can "
    "change. Reply and tell me which and I'll sort it.\n\n"
    "If you'd rather just pick up where you left off, there's 20% off waiting on this "
    "link and it applies by itself:\n"
    "https://thebonpet.com/discount/WELCOMEBACK%253C3THEBONPET?redirect=%2F%3Futm_source%3Dsub-reactivation%26utm_medium%3Dwhatsapp%26utm_campaign%3Dpaused%26utm_content%3Dwelcomeback-20\n\n"
    "Automated message, but the replies come to me and Nic."
)

REACT_CANCELLED = (
    "Hi {first_name}, Yash and Nic here from The Bon Pet. You cancelled a while back "
    "and we never asked why.\n\n"
    "One line on what didn't work would help us more than you'd think. Even if the "
    "answer is that it was too expensive.\n\n"
    "And if you ever want another go, there's 20% off on this link and it applies by "
    "itself:\n"
    "https://thebonpet.com/discount/WELCOMEBACK%253C3THEBONPET?redirect=%2F%3Futm_source%3Dsub-reactivation%26utm_medium%3Dwhatsapp%26utm_campaign%3Dcancelled%26utm_content%3Dwelcomeback-20\n\n"
    "Automated message, but we read every reply ourselves."
)

# The old greeting ends in "!", which does not flow into "Hi {name}, Yash here".
# Re-pointing the const to a comma keeps every downstream message using it.
GREET_OLD_FN = "const greeting = firstName ? `Hi ${firstName}!` : 'Hi!';"
GREET_NEW_FN = "const greeting = firstName ? `Hi ${firstName},` : 'Hi,';"
GREET_OLD_NAME = "const greeting = name ? `Hi ${name}!` : 'Hi!';"
GREET_NEW_NAME = "const greeting = name ? `Hi ${name},` : 'Hi,';"

SUBSAVE_PAUSE = (
    "${greeting} Yash here. Saw you paused your subscription, no rush at all.\n\n"
    "If you want it back at some point I can swap the proteins or stretch the gap "
    "between deliveries, whatever makes it easier. Just reply, I read these myself."
)
SUBSAVE_CANCEL = (
    "${greeting} Yash from The Bon Pet. Saw you cancelled, sorry to see you go.\n\n"
    "Would you mind telling me what changed? We're a small team here and it's the kind "
    "of thing that actually changes what we do next.\n\n"
    "If you ever want to come back, there's 20% off on this link and it applies by "
    "itself:\n"
    "https://thebonpet.com/discount/WELCOMEBACK%253C3THEBONPET?redirect=%2F%3Futm_source%3Dsub-save%26utm_medium%3Dwhatsapp%26utm_campaign%3Dcancelled%26utm_content%3Dwelcomeback-20\n\n"
    "Either way, thanks for giving us a go."
)
SUBSAVE_UPDATED = (
    "${greeting} your subscription's been updated.\n\n"
    "${protein}${qty ? ' x ' + qty : ''}${cadence ? ', every ' + cadence : ''}\n"
    "Next delivery: ${nextBill}\n\n"
    "If that wasn't you or something looks wrong, reply here and we'll fix it."
)
SUBSAVE_BILLING = (
    "${greeting} the payment for your subscription didn't go through.\n\n"
    "You can update your card at thebonpet.com/account, or reply here and we'll sort "
    "it out."
)

SWEEPER_MSG = (
    "${greeting} Yash here from The Bon Pet. You got partway through checkout earlier "
    "but didn't finish.\n\n"
    "Your cart's still there: ${cartUrl}${itemsLine}\n\n"
    "Anything holding you up? Reply here and I'll help.\n\n"
    "Automated nudge, but the replies come to me."
)

RW_THANKS = (
    "${greeting} Yash here from The Bon Pet. Your review just came through on Google "
    "${ratingPhrase} and I wanted to say thank you properly.\n\n"
    "Hope your furkid's still enjoying the food. Any feedback or ideas, just reply, "
    "I read these myself."
)
RW_APOLOGY = (
    "${greeting} Yash here from The Bon Pet. I saw your review and wanted to reach out "
    "directly. I'm sorry it wasn't what you hoped for.\n\n"
    "Can you tell me what went wrong? I'd like to fix it, and if a refund or a "
    "replacement is the right answer, just say so and I'll arrange it."
)

SAMPLER_MSG = (
    "Hi ${fn}, thanks for grabbing a Bon Pet Sampler Set.\n\n"
    "You've got 50% off your first order (new customers, one use). Tap whichever fits "
    "and the discount applies at checkout:\n"
    "Dog Sampler Set: ${dog}\n"
    "Cat Sampler Set: ${cat}\n\n"
    "One of each protein, delivered frozen island-wide, $9 cold-chain delivery. "
    "Reply here if you need a hand."
)

PATCHES = {}

# 1. Win-back -------------------------------------------------------------------
PATCHES["winback"] = {
    "id": "JgH4FFgotZSbNFFL",
    "nodes": {"Format Message": [("tpl", "msg", WINBACK_MSG)]},
}

# 2. Sub Reactivation -----------------------------------------------------------
PATCHES["sub_reactivation"] = {
    "id": "G33gVYy7VmvNHd4d",
    "nodes": {"Find Eligible Customers": [
        ("dq", "PAUSED_TEMPLATE", REACT_PAUSED),
        ("dq", "CANCELLED_TEMPLATE", REACT_CANCELLED)]},
}

# 3. Subscription Save ----------------------------------------------------------
PATCHES["subscription_save"] = {
    "id": "aHp12XVEld1s1ZBP",
    "nodes": {"Lookup + Format": [
        ("raw", GREET_OLD_FN, GREET_NEW_FN),
        ("tpl", "pauseMsg", SUBSAVE_PAUSE),
        ("tpl", "cancelMsg", SUBSAVE_CANCEL),
        ("tpl", "updatedMsg", SUBSAVE_UPDATED),
        ("tpl", "billingFailedMsg", SUBSAVE_BILLING)]},
}

# 4. Abandoned Cart Sweeper -----------------------------------------------------
PATCHES["cart_sweeper"] = {
    "id": "PHnGZ0zVIX5knHg5",
    "nodes": {"Compute Candidates": [
        ("raw", GREET_OLD_FN, GREET_NEW_FN),
        ("tpl", "msg", SWEEPER_MSG)]},
}

# 5. Review Watcher -------------------------------------------------------------
# The apology deliberately carries no automation disclosure: a complaint response that
# announces itself as automated is worthless.
PATCHES["review_watcher"] = {
    "id": "eCOC5Vjkgdm9PFSL",
    "nodes": {
        "Format Customer Thanks": [("raw", GREET_OLD_NAME, GREET_NEW_NAME),
                                   ("tpl", "msg", RW_THANKS)],
        "Format Customer Apology": [("raw", GREET_OLD_NAME, GREET_NEW_NAME),
                                    ("tpl", "msg", RW_APOLOGY)],
    },
}

# 6. Sampler auto-reply ---------------------------------------------------------
PATCHES["sampler_reply"] = {
    "id": "pkBxVYwFt3I5K1rg",
    "nodes": {"Parse Sampler": [("tpl", "message", SAMPLER_MSG)]},
}

# 7. Meal Plan Follow-up D3/D7 --------------------------------------------------
MEALPLAN_FN = """function buildMessage(step, name, petNames, species) {
  if (step === 1) {
    return 'Hi ' + name + ', Yash and Nic here from The Bon Pet. We put ' + petNames
      + "'s meal plan together a few days ago but the order hasn't come through.\\n\\n"
      + 'Anything we can help with? We can change the portions, swap proteins, or work around your delivery timing.\\n\\n'
      + "The plan's still saved here: " + planLink('d3-plan') + '\\n\\n'
      + "This one's automated, but the replies come straight to us.";
  }
  return 'Hi ' + name + ', last one about ' + petNames + "'s plan.\\n\\n"
    + 'If a full plan feels like a big first step, a sampler set is smaller: a few proteins in small packs, so you can see what ' + petNames + ' actually goes for before committing. First-timer pricing applies at checkout.\\n'
    + samplerLink(species) + '\\n\\n'
    + "If the timing just isn't right, no problem, I won't chase it again.";
}"""

PATCHES["meal_plan"] = {
    "id": "NJ3EctLBcHhaWV3I",
    "nodes": {"Compute Follow-ups (D3/D7)": [
        ("re", r"function buildMessage\(step, name, petNames, species\) \{.*?\n\}", MEALPLAN_FN)]},
}

# 8. Abandoned Checkout Recovery ------------------------------------------------
# Code strings dropped from the body: the /discount/ link auto-applies them, and
# naming them adds coupon-blast texture. Opt-out line stays (compliance).
CHECKOUT_TEMPLATES = """const templates = {
    sampler_cart: `Hi ${firstName}, your Sampler Set is still sitting in your cart.\\n\\nGood pick, it's one of each protein so your furkid can try them all before you commit to anything. The 50% first-timer discount applies by itself on this link:\\n${__ru('sampler-cart')}\\n\\n(Reply STOP to opt out)`,
    first_order: `Hi ${firstName}, you left some meals in your cart.\\n\\nSince it's your first order, starting a subscription takes 20% off on top of the 10% Subscribe & Save. You can skip, pause or cancel it any time. The discount applies by itself here:\\n${__ru('first-order')}\\n\\n(Reply STOP to opt out)`,
    returning: `Hi ${firstName}, your furkid's next batch is still in your cart.\\n\\nPick up where you left off: ${__ru('returning')}\\n\\n(Reply STOP to opt out)`
  };"""

PATCHES["abandoned_checkout"] = {
    "id": "ZyQBmsJXRyjOmxrE",
    "nodes": {"Parse & Gate": [
        ("re", r"const templates = \{\s*sampler_cart:.*?\n  \};", CHECKOUT_TEMPLATES)]},
}

# 9. POS Market Thank-You -------------------------------------------------------
PATCHES["pos_thankyou"] = {
    "id": "3ix7uDPOblAfWIJD",
    "nodes": {"Build message": [
        ("raw",
         "  ? `Hi ${firstName}! \U0001F43E Thank you so much for stopping by The Bon Pet booth at ${event} and treating your furkid to some goodies from The Bon Pet. It meant a lot to meet you!`\n"
         "  : `Hi ${firstName}! \U0001F43E Thank you so much for stopping by our booth and treating your furkid to some goodies from The Bon Pet. It meant a lot to meet you!`;",
         "  ? `Hi ${firstName}, thanks for stopping by our booth at ${event} and picking something up for your furkid. Good to meet you.`\n"
         "  : `Hi ${firstName}, thanks for stopping by our booth and picking something up for your furkid. Good to meet you.`;"),
        ("raw",
         "if (hasCat) linkLines.push('\U0001F431 Cats: ' + catsLink);\n"
         "if (hasDog) linkLines.push('\U0001F436 Dogs: ' + dogsLink);",
         "if (hasCat) linkLines.push('Cats: ' + catsLink);\n"
         "if (hasDog) linkLines.push('Dogs: ' + dogsLink);"),
        ("raw",
         "  `We'd love to keep in touch. Join our community chat for tips, questions, and first dibs on new drops:`,",
         "  `If you want to stay in touch, our community chat is where we post tips, answer questions and drop new proteins first:`,"),
        ("raw",
         "  `P.S. What's your furkid's name? Reply and tell us, we'd love to know who we're feeding! \U0001F43E`,\n"
         "  '',\n"
         "  'See you soon! The Bon Pet team ❤️'\n",
         "  `And what's your furkid called? Reply and tell us, we like knowing who we're feeding.`\n"),
    ]},
}


# --------------------------------------------------------------------------------
# Burst collapse. These two are the only patches that can double-message a customer.
#
# Both workflows emitted 3 items per candidate and both Skip Header nodes filter on
# `seq === 3`, so exactly one of the three reached Log Sent. Emitting a single item
# with seq 3 preserves that contract without touching the filter node, which is why
# the sent-log dedup keeps working per (phone, step).
# --------------------------------------------------------------------------------

POSTTRIAL_MSGS = """  // One message per touch (tbp-kb content/brand-voice.md). The 3-burst pattern this
  // replaces was already banned in May 2026 and had drifted back into production.
  // No promo codes, no shop links: replies route to humans who pitch contextually.
  let msg = '';
  if (stepNum === 1) {
    msg = `Hi ${firstName}, Yash here, one of the founders at The Bon Pet.\\n\\nYour trial pack went out about a week ago. How's ${petName} getting on with it, eating it straight away or taking some convincing?\\n\\nThis first message is automated, but the replies come to me and Nic and we read all of them.`;
  } else if (stepNum === 2) {
    msg = `Hi ${firstName}, you're about two weeks into the trial pack now. How's ${petName} finding it?\\n\\nIf you want to move onto regular packs, tell me ${petName}'s weight and roughly how active they are, and I'll work out the portion size for you.`;
  } else if (stepNum === 3) {
    msg = `Hi ${firstName}, last one from me about the trial pack.\\n\\nIf ${petName} liked it and you want to keep going, just reply and I'll sort it out. If not, that's completely fine and I won't bring it up again.`;
  }
"""

POSTTRIAL_EMIT = """  // One item per candidate. seq stays 3 so the existing Skip Header filter still
  // passes exactly this item through to Log Sent, keeping dedup per (phone, step_num).
  candidates.push({
    ...baseFields,
    seq: 3,
    target_phone: DRY_RUN ? YASH_PHONE : phone,
    message: DRY_RUN ? `\\u{1F9EA} [DRY \\u00b7 D${daysSince} \\u2192 ${firstName} ${phone}]\\n` + msg : msg,
  });
"""

REORDER_MSGS = """  // One message per touch (tbp-kb content/brand-voice.md). Replaces the 3-burst
  // pattern, which was banned in May 2026 and had drifted back into production.
  // No promo codes in body copy; replies route to humans who pitch contextually.
  let msg = '';
  if (reminderNum === 1) {
    msg = `Hi ${firstName}, Yash here from The Bon Pet. Your last order was ${daysSince} days ago, so you're probably getting low.\\n\\nSame order again here: ${__utm(cartLink, 'reminder-1')}\\n\\nThis is an automated reminder, but if you'd rather change proteins or portion size, reply and I'll do it for you.`;
  } else {
    msg = `Hi ${firstName}, it's been ${daysSince} days since your last order and I wanted to check you're not caught short.\\n\\nReorder here if it's useful: ${__utm(cartLink, 'reminder-2')}\\n\\nIf you'd rather I stopped sending these, just say so.`;
  }
"""

REORDER_EMIT = """  // One item per candidate. seq stays 3 so the existing Skip Header filter still
  // passes exactly this item through to Log Sent, keeping dedup per (phone, reminder).
  // Idempotency key keeps the :s3 suffix so it collides with any prior burst's third
  // send rather than opening a fresh slot at the DB UNIQUE constraint.
  candidates.push({
    ...baseFields,
    seq: 3,
    idempotency_key: 'reorder:' + phone.replace(/\\D/g, '') + ':' + last.order_id + ':r' + reminderNum + ':s3',
    target_phone: DRY_RUN ? YASH_PHONE : phone,
    message: DRY_RUN ? `\\u{1F9EA} [DRY \\u00b7 R${reminderNum} \\u2192 ${firstName} ${phone}]\\n` + msg : msg,
  });
"""

PATCHES["post_trial"] = {
    "id": "VR7jZxPaiRCwdIaP",
    "nodes": {"Compute Trial Candidates (D7/D14/D21)": [
        ("raw",
         "// Post-Trial Nurture v2 — 3-burst pattern (msg1 hello, msg2 founder, msg3 question).",
         "// Post-Trial Nurture v3: one message per touch (voice revamp 2026-08-09)."),
        ("re", r"  // 3-burst pattern:.*?\n  \}\n", POSTTRIAL_MSGS),
        ("re", r"  // Emit 3 items in burst order\..*?\n  \}\n", POSTTRIAL_EMIT)]},
}

PATCHES["reorder"] = {
    "id": "SUuwJMm0R6gNzXnm",
    "nodes": {"Compute Reorder Candidates": [
        ("re", r"  // 3-burst pattern:.*?\n  \}\n", REORDER_MSGS),
        ("re", r"  // Emit 3 items in burst order\..*?\n  \}\n", REORDER_EMIT)]},
}


def apply_ops(js, ops):
    for kind, name, value in ops:
        if kind == "dq":
            js = set_const(js, name, value, '"')
        elif kind == "tpl":
            js = set_const(js, name, value, "`")
        elif kind == "raw":
            js = replace_once(js, name, value)
        elif kind == "re":
            js, n = re.subn(name, lambda m: value, js, flags=re.S)
            assert n == 1, f"regex matched {n} times (need 1): {name[:70]}"
        else:
            raise ValueError(kind)
    return js


def syntax_check(label, js):
    """Parse the patched code with node. Catches broken escaping, which is the main
    risk when rewriting template literals. Code nodes use top-level `return`, so the
    body is wrapped in a function before parsing."""
    src = "(async function(){\n" + js + "\n});"
    r = subprocess.run(["node", "--check"], input=src, capture_output=True, text=True)
    assert r.returncode == 0, f"{label}: JS syntax error\n{r.stderr[:600]}"


def run(key, apply):
    spec = PATCHES[key]
    wf = get_wf(spec["id"])
    staged, nops = {}, 0
    for nname, ops in spec["nodes"].items():
        node = next((n for n in wf["nodes"] if n["name"] == nname), None)
        assert node, f"{key}: node {nname!r} not found"
        js = node["parameters"]["jsCode"]
        new_js = apply_ops(js, ops)
        assert new_js != js, f"{key}/{nname}: no change produced"
        syntax_check(f"{key}/{nname}", new_js)
        staged[nname] = new_js
        nops += len(ops)

    if not apply:
        print(f"  ✓ {key}: {nops} anchors matched across {len(staged)} node(s)")
        return

    os.makedirs(SNAPDIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = os.path.join(SNAPDIR, f"{key}-{spec['id']}-{stamp}.json")
    with open(snap, "w") as f:
        json.dump(wf, f)
    for n in wf["nodes"]:
        if n["name"] in staged:
            n["parameters"]["jsCode"] = staged[n["name"]]
    put_wf(spec["id"], wf)

    # Re-GET: a 200 from PUT is not proof the node changed.
    live = get_wf(spec["id"])
    for nname, expected in staged.items():
        lnode = next(n for n in live["nodes"] if n["name"] == nname)
        assert lnode["parameters"]["jsCode"] == expected, f"{key}/{nname}: live != PUT"
    print(f"  ✅ {key}: patched + verified live. snapshot: {os.path.relpath(snap, HERE)}")


def restore(path):
    wf = json.load(open(path))
    wid = wf["id"]
    put_wf(wid, wf)
    print(f"↩️  restored {wf['name']} ({wid}) from {path}")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore(os.environ["RESTORE"])
        sys.exit(0)
    apply = "--apply" in sys.argv
    targets = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not targets or targets == ["all"]:
        targets = list(PATCHES)
    print(("APPLYING" if apply else "CHECK ONLY") + f": {', '.join(targets)}")
    for t in targets:
        run(t, apply)

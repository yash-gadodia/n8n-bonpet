#!/usr/bin/env python3
"""Unit tests for migrate_builders_to_selfhosted.py.

Run: python3 test_migrate_builders.py
Returns exit 0 if all pass, 1 otherwise.
"""
import sys
from migrate_builders_to_selfhosted import fix_url, fix_key, add_ua_header

failures = []


def check(name, got, expected_substring=None, *, contains=None, missing=None):
    """Assert helper that collects failures instead of crashing on first."""
    ok = True
    if expected_substring is not None and expected_substring not in got:
        ok = False; failures.append(f"{name}: expected substring not found:\n  expected: {expected_substring!r}\n  got: {got!r}")
    if contains:
        for c in contains:
            if c not in got:
                ok = False; failures.append(f"{name}: missing required substring {c!r}\n  got: {got!r}")
    if missing:
        for m in missing:
            if m in got:
                ok = False; failures.append(f"{name}: forbidden substring {m!r} still present\n  got: {got!r}")
    print(f"  {'✓' if ok else '✗'} {name}")


# ─── fix_url ──────────────────────────────────────────────────────────────
print("\nfix_url:")
check(
    "API base URL replaced",
    fix_url('API = "https://thebonpet.app.n8n.cloud/api/v1"'),
    'API = "https://n8n.thebonpet.com/api/v1"',
)
check(
    "webhook URL in print() replaced",
    fix_url('print("Manual: curl -X POST https://thebonpet.app.n8n.cloud/webhook/foo")'),
    "https://n8n.thebonpet.com/webhook/foo",
)
check(
    "no URLs to change → idempotent",
    fix_url('API = "https://n8n.thebonpet.com/api/v1"'),
    'API = "https://n8n.thebonpet.com/api/v1"',
)
check(
    "doesn't touch unrelated URLs",
    fix_url('url = "https://api.thebonpet.com/whatsapp/send"'),
    "https://api.thebonpet.com/whatsapp/send",
)

# ─── fix_key ──────────────────────────────────────────────────────────────
print("\nfix_key:")
check(
    "old key path replaced",
    fix_key('open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()'),
    "~/.n8n-bonpet-newkey",
)
check(
    "newkey path NOT double-replaced",
    fix_key('open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()'),
    "~/.n8n-bonpet-newkey",
    missing=["newkeynewkey", "newnewkey"],
)
check(
    "doesn't touch other key files",
    fix_key('open(os.path.expanduser("~/.shopify-bonpet-key"))'),
    "~/.shopify-bonpet-key",
)

# ─── add_ua_header — Pattern A (with Accept) ─────────────────────────────
print("\nadd_ua_header — Pattern A (multi-line, with Accept):")
src_a = '''def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
'''
out, n = add_ua_header(src_a)
check("UA inserted (returns count > 0)", str(n), "1") if n == 1 else failures.append(f"Pattern A: expected n=1, got {n}")
check("UA in output", out, contains=['"User-Agent":', "Mozilla/5.0"])
check("Accept preserved", out, contains=['"Accept": "application/json"'])

# ─── add_ua_header — Pattern B (no Accept) ───────────────────────────────
print("\nadd_ua_header — Pattern B (multi-line, no Accept):")
src_b = '''def http(method, path, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
        },
    )
'''
out, n = add_ua_header(src_b)
if n != 1: failures.append(f"Pattern B: expected n=1, got {n}")
check("UA in output", out, contains=['"User-Agent":', "Mozilla/5.0"])
check("X-N8N-API-KEY preserved", out, contains=['"X-N8N-API-KEY":'])

# ─── add_ua_header — Pattern C (one-liner) ───────────────────────────────
print("\nadd_ua_header — Pattern C (one-liner):")
src_c = '''req = urllib.request.Request(f"{API}{path}",
    data=json.dumps(body).encode() if body is not None else None,
    method=method,
    headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"})
'''
out, n = add_ua_header(src_c)
if n != 1: failures.append(f"Pattern C: expected n=1, got {n}")
check("UA in output", out, contains=['"User-Agent": "Mozilla/5.0', '"Content-Type": "application/json"'])

# ─── add_ua_header — idempotent if already present ──────────────────────
print("\nadd_ua_header — idempotency:")
already_has_ua = '''headers={
    "X-N8N-API-KEY": api_key,
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
},
'''
out, n = add_ua_header(already_has_ua)
if n != 0: failures.append(f"idempotency: expected n=0 (no replacements), got {n}")
check("output unchanged", out, expected_substring=already_has_ua.strip())

# ─── add_ua_header — doesn't touch non-n8n headers ──────────────────────
print("\nadd_ua_header — only touches blocks with X-N8N-API-KEY:")
non_n8n = '''headers={
    "Authorization": "Bearer foo",
    "Content-Type": "application/json",
},
'''
out, n = add_ua_header(non_n8n)
if n != 0: failures.append(f"non-n8n headers: expected n=0, got {n}")
check("non-n8n block left alone (no UA injected)", out, missing=["User-Agent"])

# ─── End-to-end: a synthetic builder from-scratch round-trip ────────────
print("\nend-to-end: full-file transform:")
synthetic = '''import json, os, urllib.request

API = "https://thebonpet.app.n8n.cloud/api/v1"
WEBHOOK_URL = "https://thebonpet.app.n8n.cloud/webhook/foo"

def http(method, path, body=None):
    api_key = open(os.path.expanduser("~/.n8n-bonpet-key")).read().strip()
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
'''
out = fix_url(fix_key(synthetic))
out, _ = add_ua_header(out)
check("URL migrated",  out, missing=["thebonpet.app.n8n.cloud"])
check("URL new form",  out, contains=["https://n8n.thebonpet.com/api/v1", "https://n8n.thebonpet.com/webhook/foo"])
check("key migrated",  out, expected_substring="~/.n8n-bonpet-newkey", missing=[".n8n-bonpet-key\""])
check("UA injected",   out, contains=['"User-Agent":', "Mozilla/5.0"])

# ─── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if failures:
    print(f"❌ {len(failures)} failure(s):\n")
    for f in failures:
        print(f"  - {f}\n")
    sys.exit(1)
else:
    print("✅ all assertions passed")
    sys.exit(0)

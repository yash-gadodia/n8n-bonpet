#!/usr/bin/env python3
"""Pre-create 4 Google OAuth credentials on new n8n with Client ID/Secret pre-filled.
User still has to click 'Sign in with Google' in n8n UI to complete the OAuth flow per cred."""
import json, subprocess
from pathlib import Path
from urllib import request, error

NEW_HOST = "https://n8n.thebonpet.com"
NEW_KEY = Path.home().joinpath(".n8n-bonpet-newkey").read_text().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def kc(name):
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "thebonpet", "-s", name, "-w"],
        text=True
    ).strip()

CLIENT_ID = kc("google-oauth-n8n-clientid")
CLIENT_SECRET = kc("google-oauth-n8n-clientsecret")

def call(path, method="GET", body=None):
    url = f"{NEW_HOST}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("X-N8N-API-KEY", NEW_KEY)
    req.add_header("accept", "application/json")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

# Map credential name -> n8n credential type
CREDS_TO_CREATE = [
    ("Google Sheets account",          "googleSheetsOAuth2Api"),
    ("GSheets — BonPet Leads",         "googleSheetsOAuth2Api"),
    ("Google Sheets Trigger account 2","googleSheetsTriggerOAuth2Api"),
    ("Gmail account",                  "gmailOAuth2"),
]

# List existing creds (n8n API doesn't expose secrets, but does list names+types if we POST then GET, or via /credentials)
# n8n public API actually doesn't have a list-credentials endpoint. We'll just try to create and accept duplicates may happen.

results = []
for name, ctype in CREDS_TO_CREATE:
    payload = {
        "name": name,
        "type": ctype,
        "data": {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
    }
    code, resp = call("/api/v1/credentials", "POST", payload)
    if code in (200, 201):
        cid = resp.get("id")
        results.append((name, ctype, "OK", cid))
        print(f"  OK   {name:35s} ({ctype})  -> {cid}")
    else:
        msg = resp.get("message", str(resp))[:200]
        results.append((name, ctype, "FAIL", msg))
        print(f"  FAIL {name:35s} ({ctype}): {msg}")

print()
print(f"Created {sum(1 for r in results if r[2]=='OK')}/{len(results)} Google OAuth credentials")
print()
print("NEXT: in n8n UI, open each credential and click 'Sign in with Google' to complete OAuth flow.")
print("URL: https://n8n.thebonpet.com/home/credentials")

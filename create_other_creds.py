#!/usr/bin/env python3
"""Create the non-OAuth credentials on new n8n: Shopify x2, plus Anthropic + WA bearer if env provided."""
import json, os, subprocess
from pathlib import Path
from urllib import request, error

NEW_HOST = "https://n8n.thebonpet.com"
NEW_KEY = Path.home().joinpath(".n8n-bonpet-newkey").read_text().strip()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def kc(name):
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-a", "thebonpet", "-s", name, "-w"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        # Try without account
        return subprocess.check_output(
            ["security", "find-generic-password", "-s", name, "-w"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()

SHOPIFY_TOKEN = kc("shopify-bonpet-admin-token")
SHOPIFY_SECRET = kc("thebonpet-shopify-client-secret")
SHOP_SUBDOMAIN = "thegoodpetco"  # myshopify domain handle (https://thegoodpetco.myshopify.com)

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

CREDS = [
    # name, type, data
    ("Shopify Access Token n8n", "shopifyAccessTokenApi", {
        "shopSubdomain": SHOP_SUBDOMAIN,
        "accessToken": SHOPIFY_TOKEN,
        "appSecretKey": SHOPIFY_SECRET,
    }),
    ("Shopify Access Token account TBP", "shopifyAccessTokenApi", {
        "shopSubdomain": SHOP_SUBDOMAIN,
        "accessToken": SHOPIFY_TOKEN,
        "appSecretKey": SHOPIFY_SECRET,
    }),
]

# Add Anthropic if env var present
ANTHROPIC = os.environ.get("ANTHROPIC_API_KEY")
if ANTHROPIC:
    CREDS.append(("Anthropic account", "anthropicApi", {"apiKey": ANTHROPIC}))

# Add WA bearer if env var present
WA_BEARER = os.environ.get("BONPET_WA_BEARER")
if WA_BEARER:
    CREDS.append(("Bon Pet WA API", "httpHeaderAuth", {"name": "Authorization", "value": f"Bearer {WA_BEARER}"}))

for name, ctype, data in CREDS:
    payload = {"name": name, "type": ctype, "data": data}
    code, resp = call("/api/v1/credentials", "POST", payload)
    if code in (200, 201):
        print(f"  OK   {name:40s} -> {resp.get('id')}")
    else:
        print(f"  FAIL {name:40s} ({code}): {resp.get('message', resp)}")

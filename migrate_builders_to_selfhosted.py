#!/usr/bin/env python3
"""Migrate all build_*.py builders from n8n Cloud → self-hosted.

Three changes per file:
1. API URL: https://thebonpet.app.n8n.cloud/api/v1 → https://n8n.thebonpet.com/api/v1
2. Key path: ~/.n8n-bonpet-key → ~/.n8n-bonpet-newkey
3. Add User-Agent: Mozilla/5.0... header (Cloudflare blocks default Python-urllib UA)
"""
import glob
import os
import re

UA_HEADER = '"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",'

OLD_URL_RE   = re.compile(r'https?://thebonpet\.app\.n8n\.cloud(/api/v1)?')
NEW_URL      = "https://n8n.thebonpet.com/api/v1"
NEW_URL_BASE = "https://n8n.thebonpet.com"

OLD_KEY_RE = re.compile(r'~/\.n8n-bonpet-key\b')
NEW_KEY    = "~/.n8n-bonpet-newkey"


def fix_url(s):
    # Replace API base URL
    s = re.sub(r'"https?://thebonpet\.app\.n8n\.cloud/api/v1"', f'"{NEW_URL}"', s)
    # Replace webhook URL references
    s = re.sub(r'https?://thebonpet\.app\.n8n\.cloud', NEW_URL_BASE, s)
    return s


def fix_key(s):
    return OLD_KEY_RE.sub(NEW_KEY, s)


def add_ua_header(content):
    """Insert User-Agent into urllib.request.Request headers={} blocks that don't have it."""
    if "User-Agent" in content:
        return content, 0
    total = 0

    # Pattern A: multi-line headers with Accept line (`"Accept": "application/json",`)
    pat_a = re.compile(
        r'(headers=\{\s*\n'
        r'(?P<indent> +)"X-N8N-API-KEY":[^\n]+\n'
        r'(?P=indent)"Content-Type":[^\n]+\n'
        r'(?P=indent)"Accept":[^\n]+\n)'
        r'(\s*\},)'
    )
    def repl_a(m):
        return m.group(1) + m.group('indent') + UA_HEADER + "\n" + m.group(3)
    content, n = pat_a.subn(repl_a, content)
    total += n

    # Pattern B: multi-line headers without Accept
    pat_b = re.compile(
        r'(headers=\{\s*\n'
        r'(?P<indent> +)"X-N8N-API-KEY":[^\n]+\n'
        r'(?P=indent)"Content-Type":\s*"application/json",?\n)'
        r'(\s*\},)'
    )
    def repl_b(m):
        return m.group(1) + m.group('indent') + UA_HEADER + "\n" + m.group(3)
    content, n = pat_b.subn(repl_b, content)
    total += n

    # Pattern C: one-liner headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"}
    pat_c = re.compile(
        r'(headers=\{"X-N8N-API-KEY":\s*[^,]+,\s*"Content-Type":\s*"application/json")(\s*\})'
    )
    def repl_c(m):
        return m.group(1) + ', "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"' + m.group(2)
    content, n = pat_c.subn(repl_c, content)
    total += n

    return content, total


def process(path):
    src = open(path).read()
    orig = src
    src = fix_url(src)
    src = fix_key(src)
    src, n_ua = add_ua_header(src)
    if src != orig:
        open(path, "w").write(src)
        return True, n_ua
    return False, 0


def main():
    builders = sorted(glob.glob(os.path.expanduser("~/n8n-bonpet/build_*.py")))
    changed = 0
    no_ua = []
    for p in builders:
        ok, n_ua = process(p)
        name = os.path.basename(p)
        if ok:
            changed += 1
            mark = "✓" if n_ua > 0 else "·"
            print(f"  {mark} {name}{'  (UA inserted)' if n_ua > 0 else ''}")
            if n_ua == 0 and "User-Agent" not in open(p).read():
                no_ua.append(name)
        else:
            print(f"  - {name} (no change)")
    print(f"\n{changed}/{len(builders)} files updated.")
    if no_ua:
        print(f"\n⚠️  {len(no_ua)} files still missing User-Agent (regex didn't match — check headers block manually):")
        for n in no_ua:
            print(f"    {n}")


if __name__ == "__main__":
    main()

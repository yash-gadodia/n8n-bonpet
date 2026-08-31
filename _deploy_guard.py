"""Refuse a workflow PUT that would silently delete live nodes.

Why this exists (incident 2026-08-31): these build_*.py scripts are generators,
not a source of truth. Several live workflows were migrated off the retired
Google Sheet onto the OMS on 2026-08-25 by editing them in the n8n UI, and the
scripts here were never updated. Re-running build_new_order_alert.py and
build_selfcollect_alert.py to make one small change regenerated both from the
stale source and reverted that migration: `Fetch OMS Order` and
`Fetch Customer Stats` were deleted, the retired `Read Customers` sheet node
came back, and the message format reverted from HTML to markdown. Nothing
errored. The workflows stayed active and kept sending, just wrong.

Node removal is only half of it. build_selfcollect_alert.py removed nothing: it
just overwrote every Code node, reverting HTML formatting and OMS lookups back
to markdown and the retired sheet. So counting nodes is not enough.

The reliable signal is drift from OUR OWN last deploy. Each successful deploy
records a fingerprint of what it pushed. On the next run, if production no
longer matches that fingerprint, somebody edited it in the n8n UI since, and
this script is by definition stale. That is what gets blocked.

With no fingerprint yet (first run after this guard landed), there is nothing
to compare against, so it blocks once and asks you to confirm the baseline.

Usage in a build script, immediately before the PUT (record_deploy is optional;
preflight registers an atexit hook that records the fingerprint for you):

    from _deploy_guard import preflight
    preflight(existing_id, payload)
    status, body = http("PUT", f"/workflows/{existing_id}", payload)

Every call writes a timestamped backup to .snapshots/ (gitignored: these
contain resolved secrets) so a bad deploy is always recoverable.

To deploy anyway, when you have confirmed the removal is intended:
    python3 build_x.py --force
"""
import atexit, hashlib, json, os, sys, urllib.request, urllib.error
from datetime import datetime

API = "https://n8n.thebonpet.com/api/v1"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".snapshots")


def _key():
    return open(os.path.expanduser("~/.n8n-bonpet-newkey")).read().strip()


def _get(path):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"X-N8N-API-KEY": _key(), "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _forced():
    return "--force" in sys.argv or os.environ.get("N8N_ALLOW_NODE_REMOVAL") == "1"


def _fingerprint(wf):
    """Stable hash of the parts of a workflow a build script owns."""
    nodes = sorted(
        (n.get("name", ""), json.dumps(n.get("parameters", {}), sort_keys=True))
        for n in wf.get("nodes", [])
    )
    return hashlib.sha256(
        json.dumps({"nodes": nodes, "connections": wf.get("connections", {})},
                   sort_keys=True).encode()
    ).hexdigest()


def _node_diff(live, payload):
    ln = {n["name"]: json.dumps(n.get("parameters", {}), sort_keys=True) for n in live.get("nodes", [])}
    pn = {n["name"]: json.dumps(n.get("parameters", {}), sort_keys=True) for n in payload.get("nodes", [])}
    return (sorted(set(ln) - set(pn)),
            sorted(set(pn) - set(ln)),
            sorted(k for k in set(ln) & set(pn) if ln[k] != pn[k]))


def record_deploy(workflow_id, payload):
    """Remember what we just pushed, so the next run can detect UI edits."""
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(os.path.join(SNAP_DIR, f"{workflow_id}.deployed.json"), "w") as f:
        json.dump({"at": datetime.now().isoformat(timespec="seconds"),
                   "fingerprint": _fingerprint(payload)}, f)


def preflight(workflow_id, payload, force=None):
    """Back up production, then abort if it drifted from our last deploy."""
    if force is None:
        force = _forced()
    try:
        live = _get(f"/workflows/{workflow_id}")
    except urllib.error.HTTPError as e:
        print(f"[guard] could not read live workflow {workflow_id} (HTTP {e.code}); proceeding")
        return

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(SNAP_DIR, f"{workflow_id}-{stamp}.json")
    with open(backup, "w") as f:
        json.dump(live, f, indent=1)

    mark = os.path.join(SNAP_DIR, f"{workflow_id}.deployed.json")
    known = json.load(open(mark)) if os.path.exists(mark) else None
    drifted = (known is None) or (known.get("fingerprint") != _fingerprint(live))

    removed, added, modified = _node_diff(live, payload)

    # Only arm the fingerprint recorder on paths that actually deploy. atexit
    # also runs on sys.exit(), so arming it earlier would record a deploy that
    # never happened.
    if not drifted:
        print(f"[guard] production matches our last deploy ({known['at']}). backup: {backup}")
        atexit.register(record_deploy, workflow_id, payload)
        return
    if force:
        print(f"[guard] DRIFT DETECTED but --force given; deploying anyway. backup: {backup}")
        atexit.register(record_deploy, workflow_id, payload)
        return

    why = ("production has never been deployed by this guard, so there is no known-good "
           "baseline to compare against"
           if known is None else
           f"production no longer matches what this script last deployed ({known['at']}); "
           "somebody edited it in the n8n UI since")
    msg = [
        "",
        "=" * 74,
        "REFUSING TO DEPLOY: production has drifted from this script",
        f"  workflow: {live.get('name')} ({workflow_id})",
        "=" * 74,
        "",
        f"  Reason: {why}.",
        "",
        "  Deploying would overwrite production with whatever this script builds.",
        "  Nothing would error. The workflow would stay active and keep running,",
        "  just reverted. That is exactly how the 2026-08-31 incident happened.",
        "",
    ]
    if removed:
        msg += ["  Nodes in production that this script would DELETE:"] + [f"    - {n}" for n in removed] + [""]
    if modified:
        msg += ["  Nodes whose config this script would OVERWRITE:"] + [f"    ~ {n}" for n in modified] + [""]
    if added:
        msg += ["  Nodes this script would add:"] + [f"    + {n}" for n in added] + [""]
    msg += [
        f"  Production backed up to:\n    {backup}",
        "",
        "  What to do:",
        "    1. Open the workflow in n8n and compare against the backup above.",
        "    2. Port any UI-only changes into this script, OR make your edit",
        "       directly in n8n and do not run this script at all.",
        "    3. Re-run with --force ONLY once you are sure this script is current.",
        "",
    ]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(1)

"""Run a YAML test spec against local n8n + WA mock. Reports pass/fail.

Usage:
    python runner.py specs/reorder-reminder.example.yml

Status: SKELETON. The n8n webhook trigger call is stubbed (TODO line ~70).
Workflow must be manually imported into localhost:5678 before running.
"""
import sys
import time
import yaml
import requests

N8N_BASE = "http://localhost:5678"
WA_MOCK = "http://localhost:9999"


def fail(msg):
    print(f"❌ FAIL: {msg}")
    sys.exit(1)


def ok(msg):
    print(f"✓ {msg}")


def load_spec(path):
    with open(path) as f:
        return yaml.safe_load(f)


def reset_mocks():
    r = requests.post(f"{WA_MOCK}/reset", timeout=5)
    r.raise_for_status()
    return r.json()["cleared"]


def get_captured():
    r = requests.get(f"{WA_MOCK}/captured", timeout=5)
    r.raise_for_status()
    return r.json()["messages"]


def trigger_workflow(spec):
    """TODO: implement n8n webhook trigger.

    Blockers tracked in PLAN.md:
    - build_*.py needs --target local flag (or N8N_BASE_URL env var)
    - fixtures need to land somewhere readable by the workflow
      (mock Sheets endpoint OR direct injection)

    For now, this is a stub. Workflow must be imported and triggered manually
    while you flesh this out.
    """
    trigger = spec.get("trigger", {})
    if trigger.get("type") != "webhook":
        fail(f"unsupported trigger type: {trigger.get('type')}")
    path = trigger.get("path", "")
    url = f"{N8N_BASE}{path}"
    print(f"[stub] would POST {url} with payload {trigger.get('payload')}")
    print("[stub] until --target local exists, import + trigger workflow manually, then re-run runner with --skip-trigger")
    return None  # caller checks for None


def assert_messages(captured, expected):
    expected_msgs = expected.get("wa_messages", [])
    not_sent_to = set(expected.get("not_sent_to", []))
    expected_count = expected.get("assertions", {}).get("total_messages_sent")

    captured_phones = {m.get("phone") for m in captured}

    if expected_count is not None and len(captured) != expected_count:
        fail(f"expected {expected_count} messages, got {len(captured)}")
    ok(f"message count: {len(captured)}")

    for unexpected in not_sent_to:
        if unexpected in captured_phones:
            fail(f"message sent to {unexpected} but spec said not_sent_to")
    ok(f"no messages sent to excluded phones ({len(not_sent_to)} excluded)")

    for em in expected_msgs:
        match = next((c for c in captured if c.get("phone") == em["phone"]), None)
        if not match:
            fail(f"expected message to {em['phone']}, none found")
        if em.get("template") and match.get("template") != em["template"]:
            fail(f"{em['phone']}: expected template {em['template']}, got {match.get('template')}")
        body = str(match.get("raw", ""))
        for needle in em.get("must_contain", []):
            if needle not in body:
                fail(f"{em['phone']}: missing required string {needle!r}")
        for needle in em.get("must_not_contain", []):
            if needle in body:
                fail(f"{em['phone']}: contains forbidden string {needle!r}")
        ok(f"message to {em['phone']} matches spec")


def main():
    if len(sys.argv) < 2:
        print("usage: python runner.py <spec.yml>")
        sys.exit(2)

    spec = load_spec(sys.argv[1])
    print(f"=== {spec.get('workflow_id', '?')}: {spec.get('description', '').strip()}")

    cleared = reset_mocks()
    print(f"reset wa-mock (cleared {cleared} prior records)")

    skip_trigger = "--skip-trigger" in sys.argv
    if not skip_trigger:
        trigger_workflow(spec)
        # workflow is async; give n8n time to execute
        time.sleep(5)

    captured = get_captured()
    assert_messages(captured, spec.get("expected", {}))

    print(f"\n✅ PASS: {sys.argv[1]}")


if __name__ == "__main__":
    main()

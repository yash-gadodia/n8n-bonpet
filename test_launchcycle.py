#!/usr/bin/env python3
"""Unit tests for the Launch Cycle alert-mirroring wiring.

Guards against regressions whenever a builder is re-run: every team-alert builder
must keep posting to the "Launch Cycle X The Bon Pet" group, and the builders that
broadcast to the team over WhatsApp must keep Raghav + Siva on the recipient list.

Run:  python3 -m unittest test_launchcycle -v

Importing the builders is side-effect free (each guards its n8n PUT under
`if __name__ == "__main__"`), so this only constructs the workflow JSON locally.
It does read the local keychain / token files at import time, so run it on the
machine that has the Bon Pet credentials.
"""
import importlib
import json
import unittest

LC_CHAT = "-5177312185"          # Launch Cycle X The Bon Pet group
SIVA = "+6583513308"
RAGHAV = "+6588146498"

# builder module -> how to get its payload: build() call vs module-level `payload`
BUILDERS = {
    "build_morning_briefing": "build",
    "build_customer_metrics": "build",
    "build_top_sellers": "build",
    "build_big_order_alert": "build",
    "build_low_stock": "build",
    "build_review_watcher": "build",
    "build_weekly_stock_report": "build",
    "build_subscription_health_pulse": "build",
    "build_refund_cancel_alert": "build",
    "build_winback": "build",
    "build_sub_reactivation": "build",
    "build_linear_daily": "build",
    "build_reorder_reminder_v2": "payload",
    "build_post_trial_nurture": "payload",
    "build_selfcollect_alert": "payload",
}

# Builders that broadcast the alert/summary to the team over WhatsApp -> Raghav+Siva added.
# (reorder/sub_reactivation/post_trial/linear/selfcollect post the summary to Telegram only.)
WA_BUILDERS = {
    "build_morning_briefing", "build_customer_metrics", "build_top_sellers",
    "build_big_order_alert", "build_low_stock", "build_review_watcher",
    "build_weekly_stock_report", "build_subscription_health_pulse",
    "build_refund_cancel_alert", "build_winback",
}

# selfcollect routes to LC via a per-item job (data) through one shared send node,
# so it has no dedicated "LaunchCycle" node to assert on.
JOB_BASED = {"build_selfcollect_alert"}

# low_stock fans out one LC node per stock state (critical/low/ok).
EXPECTED_LC_NODE_COUNT = {"build_low_stock": 3}


def get_payload(mod_name, how):
    mod = importlib.import_module(mod_name)
    return mod.build() if how == "build" else mod.payload


def lc_nodes(payload):
    return [n for n in payload["nodes"] if "LaunchCycle" in n["name"]]


def node_chat_ids(node):
    bp = node["parameters"].get("bodyParameters", {}).get("parameters", [])
    return [p["value"] for p in bp if p.get("name") == "chat_id"]


class LaunchCycleWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payloads = {m: get_payload(m, how) for m, how in BUILDERS.items()}

    def test_lc_chat_present_in_every_builder(self):
        """Every builder's workflow JSON references the Launch Cycle group chat id."""
        for m, p in self.payloads.items():
            with self.subTest(builder=m):
                self.assertIn(LC_CHAT, json.dumps(p),
                              f"{m}: Launch Cycle chat {LC_CHAT} not found in payload")

    def test_lc_node_present_targeted_and_wired(self):
        """Node-based builders have LaunchCycle node(s) hitting LC_CHAT and wired in."""
        for m, p in self.payloads.items():
            if m in JOB_BASED:
                continue
            with self.subTest(builder=m):
                nodes = lc_nodes(p)
                expected = EXPECTED_LC_NODE_COUNT.get(m, 1)
                self.assertEqual(len(nodes), expected,
                                 f"{m}: expected {expected} LaunchCycle node(s), got {len(nodes)}")
                conns = json.dumps(p["connections"])
                for n in nodes:
                    self.assertEqual(node_chat_ids(n), [LC_CHAT],
                                     f"{m}: node {n['name']} chat_id != {LC_CHAT}")
                    self.assertIn(n["name"], conns,
                                  f"{m}: node {n['name']} is not wired in connections")

    def test_job_based_builder_targets_lc(self):
        """selfcollect routes a job to the LC group via its format code node."""
        for m in JOB_BASED:
            with self.subTest(builder=m):
                p = self.payloads[m]
                self.assertIn(LC_CHAT, json.dumps(p),
                              f"{m}: LC chat not injected into format code")

    def test_wa_builders_include_raghav_and_siva(self):
        """WA-broadcast builders carry both Raghav and Siva on the recipient list."""
        for m in WA_BUILDERS:
            with self.subTest(builder=m):
                blob = json.dumps(self.payloads[m])
                self.assertIn(SIVA, blob, f"{m}: Siva missing from WA recipients")
                self.assertIn(RAGHAV, blob, f"{m}: Raghav missing from WA recipients")

    def test_non_wa_builders_do_not_add_numbers(self):
        """Telegram-only summary builders must NOT add R/S to a WA list (would imply a customer-send leak path)."""
        for m in BUILDERS:
            if m in WA_BUILDERS:
                continue
            with self.subTest(builder=m):
                blob = json.dumps(self.payloads[m])
                self.assertNotIn(SIVA, blob, f"{m}: unexpected Siva number in a non-WA builder")
                self.assertNotIn(RAGHAV, blob, f"{m}: unexpected Raghav number in a non-WA builder")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Shared helper for the customer-messaging blacklist.

`BLACKLIST.txt` (sibling file) is the single source of truth — one normalized
phone per line. Lines starting with `#`, empty lines, and end-of-line `#` comments
are stripped.

Usage in a build_*.py:

    from _blacklist import BLACKLIST_JS_SNIPPET

    # In CODE_JS, after normalizePhone() is defined and the cooldown snippet:
    CODE_JS = (
        FIRST_PART
        + COOLDOWN_JS_SNIPPET
        + BLACKLIST_JS_SNIPPET
        + REST_OF_CODE
    )

    # Then in the per-customer filter loop, after other exclusions:
    #   if (isBlacklisted(phone)) {
    #     stats.skipped_blacklist = (stats.skipped_blacklist || 0) + 1;
    #     continue;
    #   }
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "BLACKLIST.txt")


def load_blacklist():
    phones = []
    with open(_PATH) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            phones.append(line)
    return phones


BLACKLIST_PHONES = load_blacklist()


# JS snippet — paste into a Code node AFTER normalizePhone() is defined.
# We re-normalize each blacklist entry at runtime so the source-of-truth file
# can use various phone formats and still match downstream lookups.
BLACKLIST_JS_SNIPPET = (
    r"""
// --- Customer messaging BLACKLIST (repo-versioned) ---
// Phones that NEVER get messaged by automated WA workflows.
// Source: BLACKLIST.txt in n8n-bonpet repo. Inlined at build time.
// To add: edit BLACKLIST.txt, run build_<workflow>.py to redeploy.
const _BLACKLIST_RAW = """
    + repr(BLACKLIST_PHONES)
    + r""";
const BLACKLIST = new Set(_BLACKLIST_RAW.map(p => normalizePhone(p)));
function isBlacklisted(phone) {
  return BLACKLIST.has(phone);
}
"""
)

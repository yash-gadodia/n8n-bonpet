"""Shared definition of an ONLINE sale.

All revenue/order reporting must count online sales only. Offline channels
(Shopify POS at markets and expos, HitPay POS, and manually keyed draft
orders) are excluded, because an expo weekend otherwise swamps the DTC
trend lines the reports exist to show.

Shopify's `source_name` is the channel marker:

    ONLINE   web                                 online store checkout
             subscription_contract               recurring sub renewal
             subscription_contract_checkout_one  first sub order
    OFFLINE  pos                                 Shopify POS (events/markets)
             HitPay POS                          legacy card terminal
             shopify_draft_order                 manually keyed in admin

Usage in a build_*.py:

    from _online_sales import IS_ONLINE_JS, ONLINE_FIELDS

    # 1. `source_name` MUST be in the REST `fields=` list or every order
    #    looks offline. Append ONLINE_FIELDS to the fields param.
    # 2. Paste IS_ONLINE_JS into the Code node, then gate the order loop:
    #        if (!isOnlineOrder(o)) continue;
"""

# Append to any Shopify REST `fields=` list that feeds an isOnlineOrder() check.
ONLINE_FIELDS = "source_name"

# Offline channels, lowercased for comparison.
OFFLINE_SOURCES = ["pos", "hitpay pos", "shopify_draft_order"]

IS_ONLINE_JS = r"""
// --- Online-sales filter (shared: _online_sales.py) ---
// Offline channels (POS at events/markets, manually keyed draft orders) are
// excluded from all revenue/order reporting so expo spikes don't distort DTC
// trends. Requires `source_name` in the Shopify REST fields= list.
const OFFLINE_SOURCES = new Set(['pos', 'hitpay pos', 'shopify_draft_order']);
function isOnlineOrder(o) {
  const src = String((o && o.source_name) || '').trim().toLowerCase();
  // Unknown/blank source_name is treated as online: the online store is the
  // default channel, and dropping unknowns would silently understate revenue.
  if (!src) return true;
  return !OFFLINE_SOURCES.has(src);
}
"""

"""Shared notifier helpers for n8n workflows.

Currently: Telegram send node pointing at the `weslee` topic in the Team Bon Pet
supergroup. Any team-alerting builder should call `telegram_send_node()` and
wire its output in parallel with the WA team broadcast fan-out — same upstream
node, same `$json.message`.

Plain text by default (no parse_mode) so Markdown-ish chars like `_` don't
break the send. Pass parse_mode='HTML' if you need formatting and have escaped
the content accordingly.
"""
import os
import uuid

# Weslee topic in "Team Bon Pet" supergroup (forum-style chat)
TELEGRAM_CHAT_ID = "-1002184573790"
TELEGRAM_THREAD_ID = 34253
TELEGRAM_TOKEN_FILE = os.path.expanduser("~/.telegram-weslee-bot-token")


def _telegram_token():
    return open(TELEGRAM_TOKEN_FILE).read().strip()


def telegram_send_node(name, pos, message_expr="={{ $json.message }}", parse_mode="Markdown"):
    # Legacy "Markdown" uses same syntax as WA (*bold*, _italic_) so team messages
    # written for WA render the same on Telegram. Stricter MarkdownV2 would need
    # per-char escaping. Pass parse_mode=None to force plain text.
    token = _telegram_token()
    params = [
        {"name": "chat_id", "value": TELEGRAM_CHAT_ID},
        {"name": "message_thread_id", "value": str(TELEGRAM_THREAD_ID)},
        {"name": "text", "value": message_expr},
    ]
    if parse_mode:
        params.append({"name": "parse_mode", "value": parse_mode})
    return {
        "parameters": {
            "method": "POST",
            "url": f"https://api.telegram.org/bot{token}/sendMessage",
            "sendBody": True,
            "bodyParameters": {"parameters": params},
            "options": {},
        },
        "id": str(uuid.uuid4()),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
    }

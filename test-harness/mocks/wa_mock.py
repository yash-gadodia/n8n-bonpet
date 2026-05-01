"""WhatsApp send mock. Captures POSTs that production sends to api.thebonpet.com/whatsapp/send.

Usage:
    python mocks/wa_mock.py            # listens on localhost:9999

Endpoints:
    POST /whatsapp/send  - mimics production. Returns 200 with synthetic message_id.
    GET  /captured       - returns list of all captured payloads (chronological)
    POST /reset          - clears captured state, returns count cleared
    GET  /health         - returns {"ok": true, "captured_count": N}
"""
from flask import Flask, request, jsonify
import uuid
import time

app = Flask(__name__)
captured = []


@app.route("/whatsapp/send", methods=["POST"])
def whatsapp_send():
    payload = request.get_json(force=True, silent=True) or {}
    record = {
        "received_at": time.time(),
        "phone": payload.get("phone"),
        "template": payload.get("template"),
        "params": payload.get("params"),
        "raw": payload,
    }
    captured.append(record)
    return jsonify({"message_id": f"mock-{uuid.uuid4().hex[:12]}", "status": "queued"}), 200


@app.route("/captured", methods=["GET"])
def get_captured():
    return jsonify({"count": len(captured), "messages": captured})


@app.route("/reset", methods=["POST"])
def reset():
    n = len(captured)
    captured.clear()
    return jsonify({"cleared": n})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "captured_count": len(captured)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False)
